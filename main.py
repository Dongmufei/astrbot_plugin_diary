# -*- coding: utf-8 -*-
"""让插件自己写日记：记录日常 -> 定时生成 -> 可以回看 -> 聊天时会想起来。

v2 变化：新增网页管理界面（插件页里的「日记本」视图），
并且日记改以 `diaries/YYYY-MM-DD.md` 为主存储，旧版 `diaries.jsonl` 自动迁移。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

# 注意：这里不能用 `import diary_core`。
# AstrBot 把插件当成 `data.plugins.<插件名>.main` 导入，插件目录不在 sys.path 里，
# 所以同目录的模块要按文件路径加载（见 diary_core.load_sibling）。
# 先用 importlib 把 diary_core 本身拉起来，再由它加载 webui。
def _bootstrap_siblings():
    import importlib.util
    import sys

    pkg = __name__.rpartition(".")[0]
    core_name = f"{pkg}.diary_core" if pkg else "diary_core"
    if core_name not in sys.modules:
        path = Path(__file__).resolve().parent / "diary_core.py"
        spec = importlib.util.spec_from_file_location(core_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"找不到 diary_core.py：{path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[core_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(core_name, None)
            raise
    core = sys.modules[core_name]
    return core, core.load_sibling("webui")


core, _webui = _bootstrap_siblings()
ensure_web_apis = _webui.ensure_web_apis

PLUGIN_NAME = core.PLUGIN_NAME
VERSION = "2.0.1"


def _optional_hook(name: str):
    """有些 AstrBot 版本没有某个钩子；没有就退化成普通函数，插件照样能加载。"""
    deco = getattr(filter, name, None)
    if deco is None:
        return lambda fn: fn
    try:
        return deco()
    except Exception:
        return lambda fn: fn


def _data_dir() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
    except Exception:
        return Path("data") / "plugin_data" / PLUGIN_NAME


@register(
    PLUGIN_NAME,
    "冬霂",
    "让她每天自己写日记：记录日常、定时生成、可以回看，聊天时还会想起来。",
    VERSION,
)
class DiaryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self._raw_conf: dict[str, Any] = {}
        if config is not None:
            try:
                self._raw_conf = dict(config)
            except Exception:
                self._raw_conf = {}
        self.cfg = core.normalize_conf(self._raw_conf)
        self.config_obj = config

        self.root = _data_dir()
        self.log_dir = self.root / "logs"
        self.md_dir = self.root / "diaries"
        self.state_path = self.root / "state.json"
        self.store = core.DiaryStore(self.root)

        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._state: dict = {}
        self._seen_ids: list[str] = []
        self._last_try = 0.0
        self._prefs_cache: dict | None = None
        self._pending_save = None

        self.tz = core.make_tz(self.cfg.get("timezone"))

    # ---------------------------------------------------------------- 基础设施

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _today(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def _prefs_path(self) -> Path:
        return self.root / "webui_prefs.json"

    def _load_state(self) -> dict:
        try:
            if self.state_path.exists():
                return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_state(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[diary] 保存状态失败: {e}")

    async def initialize(self) -> None:
        for d in (self.root, self.log_dir, self.md_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._state = self._load_state()
        self.merge_local_overrides()
        self.tz = core.make_tz(self.cfg.get("timezone"))
        moved = self.store.migrate_legacy()
        if moved:
            logger.info(f"[diary] 已把 {moved} 篇旧日记迁移到 diaries/*.md")
        core.cleanup_logs(self.root, int(self.cfg.get("keep_log_days") or 30), self._now())
        ensure_web_apis(self)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        logger.info(
            f"[diary] 日记插件 v{VERSION} 已启动，每天 {self.cfg.get('diary_time')} 写日记（{self.tz}）"
        )

    async def terminate(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
        self._task = None

    # ---------------------------------------------------------------- 配置

    def apply_conf(self, patch: dict) -> dict:
        """网页里改了配置：写回 AstrBot 的配置文件并立即生效。"""
        merged = dict(self.cfg)
        merged.update(core.normalize_conf({**self.cfg, **patch}))
        saved_as_config = False
        try:
            cfg = self.config_obj
            if cfg is not None:
                for k, v in merged.items():
                    try:
                        cfg[k] = v
                    except Exception:
                        pass
                save = getattr(cfg, "save_config", None)
                if callable(save):
                    r = save()
                    if r is not None and hasattr(r, "__await__"):
                        raise TypeError("async save")
                    saved_as_config = True
        except TypeError:
            saved_as_config = True
            try:
                self._pending_save = asyncio.get_running_loop().create_task(self.config_obj.save_config())
            except Exception:
                saved_as_config = False
        except Exception as e:
            logger.warning(f"[diary] 写回 AstrBot 配置失败（会用本地覆盖）：{e}")

        if not saved_as_config:
            prefs = self._load_prefs()
            ov = dict(prefs.get("overrides") or {})
            ov.update(merged)
            prefs["overrides"] = ov
            self._save_prefs(prefs)

        self.cfg = merged
        self.tz = core.make_tz(merged.get("timezone"))
        self._last_try = 0.0
        core.cleanup_logs(self.root, int(merged.get("keep_log_days") or 30), self._now())
        return dict(self.cfg)

    def _load_prefs(self) -> dict:
        if self._prefs_cache is not None:
            return self._prefs_cache
        data = {}
        try:
            if self._prefs_path().exists():
                data = json.loads(self._prefs_path().read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
        except Exception:
            data = {}
        self._prefs_cache = data
        return data

    def _save_prefs(self, data: dict) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._prefs_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self._prefs_cache = data
        except Exception as e:
            logger.warning(f"[diary] 保存网页设置失败: {e}")

    def merge_local_overrides(self) -> None:
        """AstrBot 配置写不进去时，用本地 webui_prefs.json 里的 overrides 兜底。"""
        overrides = (self._load_prefs() or {}).get("overrides") or {}
        if isinstance(overrides, dict) and overrides:
            self.cfg = core.normalize_conf({**self.cfg, **overrides})

    # ---------------------------------------------------------------- 记录日常

    def _should_record(self, event: AstrMessageEvent) -> bool:
        if not self.cfg.get("enabled", True):
            return False
        watch = self.cfg.get("watch_sessions") or []
        umo = getattr(event, "unified_msg_origin", "") or ""
        if watch and umo not in watch:
            return False
        return True

    def _record(self, event: AstrMessageEvent, text: str, from_bot: bool) -> None:
        text = (text or "").strip()
        if not text:
            return
        now = self._now()
        date_str = now.strftime("%Y-%m-%d")
        try:
            name = "" if from_bot else (event.get_sender_name() or "")
        except Exception:
            name = ""
        entry = {
            "t": now.strftime("%H:%M"),
            "n": name,
            "m": text[:600],
            "bot": from_bot,
        }
        try:
            gid = event.get_group_id()
            if gid:
                entry["g"] = gid
        except Exception:
            pass
        try:
            core.append_log(self.root, date_str, entry)
        except Exception as e:
            logger.warning(f"[diary] 记录消息失败: {e}")

    def _remember_umo(self, event: AstrMessageEvent) -> None:
        try:
            umo = event.unified_msg_origin
            if umo and self._state.get("last_umo") != umo:
                self._state["last_umo"] = umo
                self._save_state()
        except Exception:
            pass

    @filter.on_llm_request()
    async def _on_llm_request(self, event: AstrMessageEvent, req):
        try:
            self._remember_umo(event)
            if self._should_record(event):
                mid = ""
                try:
                    mid = event.message_obj.message_id or ""
                except Exception:
                    pass
                if not (mid and mid in self._seen_ids):
                    if mid:
                        self._seen_ids.append(mid)
                        if len(self._seen_ids) > 300:
                            self._seen_ids = self._seen_ids[-150:]
                    self._record(event, event.message_str, from_bot=False)

            block = self._memory_block()
            if block:
                self._inject(req, block)
        except Exception as e:
            logger.warning(f"[diary] on_llm_request 出错: {e}")

    @_optional_hook("on_llm_response")
    async def _on_llm_response(self, event=None, resp=None):
        try:
            if event is None or not self._should_record(event):
                return
            text = getattr(resp, "completion_text", "") or ""
            if text.strip():
                self._record(event, text, from_bot=True)
        except Exception as e:
            logger.warning(f"[diary] on_llm_response 出错: {e}")

    @staticmethod
    def _inject(req, block: str) -> None:
        try:
            from astrbot.core.agent.message import TextPart

            parts = getattr(req, "extra_user_content_parts", None)
            if parts is not None:
                parts.append(TextPart(text=block))
                return
        except Exception:
            pass
        try:
            req.system_prompt = ((req.system_prompt or "") + "\n\n" + block).strip()
        except Exception:
            pass

    # ---------------------------------------------------------------- 读写日记

    def _memory_block(self) -> str:
        n = int(self.cfg.get("inject_recent") or 0)
        if n <= 0:
            return ""
        try:
            return self.store.memory_block(self._today(), n)
        except Exception as e:
            logger.warning(f"[diary] 读日记失败: {e}")
            return ""

    # ---------------------------------------------------------------- 生成日记

    async def _persona_text(self) -> str:
        p = str(self.cfg.get("persona_prompt") or "").strip()
        if p:
            return p
        pid = str(self.cfg.get("persona_id") or "").strip()
        pm = getattr(self.context, "persona_manager", None)
        if not pid or pm is None:
            return ""
        for meth in ("get_persona", "get_persona_v3", "get_persona_by_id"):
            fn = getattr(pm, meth, None)
            if not fn:
                continue
            try:
                obj = fn(pid)
                if asyncio.iscoroutine(obj):
                    obj = await obj
                sp = getattr(obj, "system_prompt", "") if obj else ""
                if sp:
                    return str(sp).strip()
            except Exception:
                continue
        logger.warning(f"[diary] 没读到人格 {pid}，先用通用写法。建议在网页里填「她的设定」。")
        return ""

    def _any_umo(self) -> str:
        for umo in self.cfg.get("deliver_to") or []:
            if umo:
                return str(umo)
        return str(self._state.get("last_umo") or "")

    async def _provider_id(self, umo: str) -> str:
        pid = str(self.cfg.get("provider_id") or "").strip()
        if pid:
            return pid
        if umo:
            try:
                pid = await self.context.get_current_chat_provider_id(umo=umo)
                if pid:
                    return str(pid)
            except Exception as e:
                logger.debug(f"[diary] 取会话模型失败: {e}")
        return ""

    async def _call_llm(self, system_prompt: str, prompt: str, umo: str) -> str:
        pid = await self._provider_id(umo)
        resp = None
        if pid:
            try:
                resp = await self.context.llm_generate(
                    chat_provider_id=pid, prompt=prompt, system_prompt=system_prompt
                )
            except TypeError:
                logger.warning("[diary] 当前版本 llm_generate 不接受 system_prompt，改走默认调用")
                resp = await self.context.llm_generate(chat_provider_id=pid, prompt=prompt)
        if resp is None:
            provider = None
            try:
                provider = await self.context.get_using_provider(umo=umo)
            except TypeError:
                provider = await self.context.get_using_provider()
            resp = await provider.text_chat(prompt=prompt, system_prompt=system_prompt or None)
        text = (getattr(resp, "completion_text", "") or "").strip()
        if not text:
            raise RuntimeError("模型返回为空")
        return text

    async def generate(self, date_str: str, force: bool = False) -> dict | None:
        async with self._lock:
            if not force and self.store.get(date_str):
                return self.store.get(date_str)
            logs = core.read_logs(self.root, date_str)
            chats = core.format_logs(logs)
            recent = self.store.recent(date_str, 2)
            persona = await self._persona_text()
            sp = core.system_prompt(self.cfg, persona, date_str)
            up = core.user_prompt(date_str, chats, recent)
            raw = await self._call_llm(sp, up, self._any_umo())
            parsed = core.parse_llm_output(raw)
            if not parsed:
                raise RuntimeError("模型没有返回可用的日记内容")
            diary = self.store.save(
                {
                    "date": date_str,
                    "title": parsed["title"],
                    "content": parsed["content"],
                }
            )
            logger.info(f"[diary] 写好了 {date_str} 的日记：{diary.get('title')}")
            return diary

    @staticmethod
    def _display(d: dict, prefix: str = "") -> str:
        dt = core.parse_date(d.get("date", ""))
        head = f"🌙 {dt.month}月{dt.day}日 · {d.get('title', '')}" if dt else f"🌙 {d.get('title', '')}"
        body = (d.get("content") or "").strip()
        return f"{prefix}{head}\n\n{body}"

    async def _deliver(self, diary: dict) -> None:
        targets = [str(x) for x in (self.cfg.get("deliver_to") or []) if x]
        if not targets:
            return
        text = self._display(diary)
        for umo in targets:
            try:
                await self.context.send_message(umo, MessageChain().message(text))
            except Exception as e:
                logger.warning(f"[diary] 推送到 {umo} 失败: {e}")

    # ---------------------------------------------------------------- 定时

    def _target_minutes(self) -> int:
        raw = str(self.cfg.get("diary_time") or "23:30").strip()
        m = re.match(r"^(\d{1,2})[:：](\d{1,2})$", raw)
        if not m:
            return 23 * 60 + 30
        h, mi = int(m.group(1)), int(m.group(2))
        return min(max(h, 0), 23) * 60 + min(max(mi, 0), 59)

    async def _tick(self) -> None:
        now = self._now()
        today = now.strftime("%Y-%m-%d")
        mins = now.hour * 60 + now.minute
        if mins >= self._target_minutes() and not self.store.get(today):
            await self._auto(today)
            return
        # 跨零点补写：00:00-01:59 之间，如果昨天没写，补上
        if now.hour < 2:
            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            if (
                not self.store.get(yesterday)
                and core.log_path(self.root, yesterday).exists()
                and self._state.get("last_auto_date") != yesterday
            ):
                await self._auto(yesterday)

    async def _auto(self, date_str: str) -> None:
        if time.monotonic() - self._last_try < 300:
            return
        self._last_try = time.monotonic()
        try:
            diary = await self.generate(date_str)
            if diary:
                self._state["last_auto_date"] = date_str
                self._save_state()
                await self._deliver(diary)
        except Exception as e:
            logger.error(f"[diary] 自动写 {date_str} 的日记失败: {e}")

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[diary] 定时检查出错: {e}")
            await asyncio.sleep(45)

    # ---------------------------------------------------------------- 指令

    def _allowed(self, event: AstrMessageEvent) -> bool:
        if not self.cfg.get("admin_only", True):
            return True
        try:
            fn = getattr(event, "is_admin", None)
            if callable(fn):
                return bool(fn())
        except Exception:
            pass
        return str(getattr(event, "role", "") or "") == "admin"

    @filter.command("日记")
    async def cmd_read(self, event: AstrMessageEvent):
        """看她的日记：/日记 或 /日记 昨天 或 /日记 11-10"""
        if not self._allowed(event):
            yield event.plain_result("这是她的日记本，只有主人能翻。")
            return
        parts = (event.message_str or "").strip().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        date_str = core.resolve_date(arg, self._now().date())
        diary = self.store.get(date_str)
        if not diary:
            yield event.plain_result(f"{date_str} 那天没有日记。")
            return
        yield event.plain_result(self._display(diary, prefix="📖 "))

    @filter.command("翻日记")
    async def cmd_list(self, event: AstrMessageEvent):
        """看看她最近都写了什么"""
        if not self._allowed(event):
            yield event.plain_result("这是她的日记本，只有主人能翻。")
            return
        items = self.store.all()
        if not items:
            yield event.plain_result("本子还是空的。等她写第一篇吧。")
            return
        lines = [f"她一共写了 {len(items)} 篇日记。最近这些："]
        for k in self.store.sorted_dates()[:10]:
            d = items[k]
            dt = core.parse_date(k)
            lines.append(f"{dt.month}月{dt.day}日 · {d.get('title', '')}（{d.get('chars', 0)} 字）")
        lines.append("用 /日记 11-10 这样翻具体某天。")
        yield event.plain_result("\n".join(lines))

    @filter.command("写日记")
    async def cmd_write(self, event: AstrMessageEvent):
        """让她现在写一篇（测试用）：/写日记"""
        if not self._allowed(event):
            yield event.plain_result("只有主人能让她现在写日记。")
            return
        parts = (event.message_str or "").strip().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        date_str = core.resolve_date(arg, self._now().date())
        yield event.plain_result(f"（她翻开本子，开始写 {date_str} 的日记……）")
        try:
            diary = await self.generate(date_str, force=True)
        except Exception as e:
            logger.error(f"[diary] 手动写日记失败: {e}")
            yield event.plain_result(f"没写成：{e}")
            return
        if not diary:
            yield event.plain_result("这次什么也没写出来，再试一次？")
            return
        self._state["last_auto_date"] = date_str
        self._save_state()
        yield event.plain_result(self._display(diary, prefix="📖 "))
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
VERSION = "2.0.13"


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
        self.book = core.SessionBook(self.root)

        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._state: dict = {}
        self._seen_ids: list[str] = []
        self._injected_mids: list[str] = []
        self._pending_recall: list[str] = []
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
        # 这几步都不该把插件加载打断：出错只记日志，让插件照常起来
        for d in (self.root, self.log_dir, self.md_dir):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.error(f"[diary] 建目录 {d} 失败: {e}")
        try:
            self._state = self._load_state()
        except Exception:
            self._state = {}
        try:
            self.merge_local_overrides()
            self.tz = core.make_tz(self.cfg.get("timezone"))
        except Exception as e:
            logger.warning(f"[diary] 读取本地配置失败: {e}")
        try:
            moved = self.store.migrate_legacy()
            if moved:
                logger.info(f"[diary] 已把 {moved} 篇旧日记迁移到 diaries/*.md")
        except Exception as e:
            logger.warning(f"[diary] 迁移旧日记失败: {e}")
        try:
            if self.cfg.get("migrate_legacy_logs", True):
                moved = core.migrate_legacy_logs(
                    self.root,
                    self.book,
                    bot_name=self._bot_label(),
                    private_umo=self._private_umo(),
                )
                if moved:
                    logger.info(f"[diary] 已把旧聊天记录拆分到 {moved} 个会话文件（旧文件保留）")
        except Exception as e:
            logger.warning(f"[diary] 迁移旧聊天记录失败: {e}")
        try:
            core.cleanup_logs(self.root, int(self.cfg.get("keep_log_days") or 30), self._now())
        except Exception as e:
            logger.warning(f"[diary] 清理旧日志失败: {e}")
        try:
            ensure_web_apis(self)
        except Exception as e:
            logger.error(f"[diary] 注册网页接口失败: {e}")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        logger.info(
            f"[diary] 日记插件 v{VERSION} 已启动，每天 {self.cfg.get('diary_time')} 写日记（{self.tz}）"
        )

    async def terminate(self) -> None:
        # AstrBot 的启用/禁用开关会直接 await 这里：任何一个异常都会变成后台 500，
        # 所以整段吞掉异常（asyncio.CancelledError 是 BaseException，必须单独兜住）。
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except BaseException as e:  # noqa: BLE001
            logger.debug(f"[diary] 停止后台任务时出错: {e}")

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

    def _bot_label(self) -> str:
        name = str(self.cfg.get("bot_name") or "").strip()
        return name or "她"

    def _private_umo(self) -> str:
        for umo in self.cfg.get("watch_sessions") or []:
            if umo and "GroupMessage" not in str(umo):
                return str(umo)
        last = str(self._state.get("last_umo") or "")
        return last if last and "GroupMessage" not in last else ""

    def _session_title(self, event: AstrMessageEvent) -> str:
        """群名 / 对方昵称：只用于给日志文件起名和网页显示。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if "GroupMessage" in umo:
            fn = getattr(event, "get_group_name", None)
            if callable(fn):
                try:
                    name = fn()
                    if name and not asyncio.iscoroutine(name):
                        return str(name).strip()
                except Exception:
                    pass
            try:
                obj = getattr(event, "message_obj", None)
                group = getattr(obj, "group", None) if obj is not None else None
                name = getattr(group, "name", "") if group is not None else ""
                if name:
                    return str(name).strip()
            except Exception:
                pass
            return core.umo_tail(umo)
        try:
            return str(event.get_sender_name() or "").strip()
        except Exception:
            return ""

    def _sender_id(self, event: AstrMessageEvent) -> str:
        """发信人的 QQ 号。AstrBot 的 get_sender_id() 只在 user_id 是 str 时才返回，
        而 NapCat / OneBot 给的是 int，所以这里再自己兜一次底。"""
        for getter in ("get_sender_id",):
            fn = getattr(event, getter, None)
            if not callable(fn):
                continue
            try:
                value = str(fn() or "").strip()
            except Exception:
                value = ""
            if value:
                return value
        try:
            obj = getattr(event, "message_obj", None)
            sender = getattr(obj, "sender", None) if obj is not None else None
            uid = getattr(sender, "user_id", "")
            if uid:
                return str(uid).strip()
        except Exception:
            pass
        return ""

    def _record(self, event: AstrMessageEvent, text: str, from_bot: bool) -> None:
        text = (text or "").strip()
        if not text:
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        now = self._now()
        date_str = now.strftime("%Y-%m-%d")
        when = now.strftime("%Y-%m-%d %H:%M")
        kind = core.umo_kind(umo)
        try:
            rec = self.book.register(umo, kind, self._session_title(event), when=when)
        except Exception as e:
            logger.debug(f"[diary] 登记会话失败: {e}")
            rec = {}
        stem = rec.get("file") or core.sanitize_name(umo)
        head = {"umo": umo, "name": rec.get("name") or stem, "kind": kind, "since": date_str}
        entry: dict[str, Any] = {"t": now.strftime("%H:%M")}
        if from_bot:
            entry["bot"] = self._bot_label()
        else:
            entry["q"] = "" if kind != "group" else self._sender_id(event)
            try:
                entry["n"] = str(event.get_sender_name() or "")
            except Exception:
                entry["n"] = ""
        entry["m"] = text[:600]
        try:
            core.append_session_log(self.root, date_str, stem, head, entry)
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

            umo = str(getattr(event, "unified_msg_origin", "") or "")
            # 只有 /想想 手动唤醒才注入；自动注入（首次说话、/new、/reset、跨刷新时间）已取消。
            if not umo or umo not in self._pending_recall:
                return
            mid = ""
            try:
                mid = str(event.message_obj.message_id or "")
            except Exception:
                pass
            if mid and mid in self._injected_mids:
                return
            block = self._memory_block(event)
            if not block:
                return
            if self._inject(req, block):
                try:
                    self._pending_recall.remove(umo)
                except ValueError:
                    pass
                if mid:
                    self._injected_mids.append(mid)
                    if len(self._injected_mids) > 300:
                        self._injected_mids = self._injected_mids[-150:]
                logger.info(f"[diary] 注入回忆（/想想）：已挂到附加块，{len(block)} 字")
            else:
                logger.error("[diary] 注入回忆失败：内容生成了但没挂上附加块")
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
    def _inject(req, block: str) -> bool:
        """把回忆追加到请求的「附加块」里，真的挂上了才返回 True。

        为什么用附加块（v2.0.6 的做法）：
        - system_prompt / prompt / contexts 都会被 group_chat_plus 整段重写；
        - 附加块是它唯一不重建的通道，模型照样看得到。

        注意：这个位置会随这条用户消息一起写进对话历史（不打临时标记），
        所以之后几轮的请求里都会带上这一份。
        """
        if not block:
            return False
        try:
            from astrbot.core.agent.message import TextPart

            parts = getattr(req, "extra_user_content_parts", None)
            if not isinstance(parts, list):
                parts = []
                req.extra_user_content_parts = parts
            parts.append(TextPart(text=block))
            now = getattr(req, "extra_user_content_parts", None) or []
            return any(block in str(getattr(p, "text", "") or "") for p in now)
        except Exception as e:
            logger.error(f"[diary] 注入回忆失败：{e}")
            return False

    # ---------------------------------------------------------------- 回忆

    def _inject_enabled(self) -> bool:
        return int(self.cfg.get("inject_recent") or 0) > 0 or int(self.cfg.get("inject_log_days") or 0) > 0

    def _memory_block(self, event: AstrMessageEvent | None = None) -> str:
        budget = int(self.cfg.get("inject_max_chars") or 10000)
        n = int(self.cfg.get("inject_recent") or 0)
        days = int(self.cfg.get("inject_log_days") or 0)
        now = self._now()
        dstat: dict[str, Any] = {}
        diaries = ""
        if n > 0:
            try:
                diaries = self.store.recall_text(self._today(), n, stats=dstat)
            except Exception as e:
                logger.warning(f"[diary] 读日记失败: {e}")
        chat = ""
        cstat: dict[str, Any] = {}
        if days > 0:
            # 从今天开始往前数：今天、昨天、前天……
            dates = [(now.date() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
            umo = str(getattr(event, "unified_msg_origin", "") or "") if event is not None else ""
            remain = max(1200, budget - len(diaries))
            try:
                chat = core.build_chat_blocks(
                    self.root,
                    dates,
                    current_umo=umo,
                    total_budget=remain,
                    carry=str(self.cfg.get("carry_sessions") or "all"),
                    private_to_group=bool(self.cfg.get("carry_private_to_group", False)),
                    legacy_in_recall=bool(self.cfg.get("legacy_in_recall", True)),
                    stats=cstat,
                )
            except Exception as e:
                logger.warning(f"[diary] 读聊天记录失败: {e}")
        if not diaries and not chat:
            return ""
        self._log_recall(dstat, cstat, len(diaries) + len(chat))
        return core.build_recall(diaries, chat, now.strftime("%Y-%m-%d %H:%M"))

    @staticmethod
    def _span(first: str, last: str) -> str:
        a, b = core.month_day(first), core.month_day(last)
        return a if a == b else f"{a}~{b}"

    @classmethod
    def _log_recall(cls, dstat: dict, cstat: dict, total: int) -> None:
        """打一行能一眼看懂的注入记录。"""
        try:
            if dstat.get("count"):
                d = f"日记 {dstat['count']} 篇（{cls._span(dstat['first'], dstat['last'])}）"
            else:
                d = "日记 0 篇"
            cur = cstat.get("current") or {}
            if cur.get("count"):
                c = f"当前对话 {cur['count']} 段（{cls._span(cur['first'], cur['last'])}）"
            else:
                c = "当前对话 0 段"
            oth = cstat.get("others") or {}
            if oth.get("count"):
                o = f"其他对话 · {oth['count']} 段（{cls._span(oth['first'], oth['last'])}）"
            else:
                o = "其他对话 · 0 段"
            logger.info(f"[diary] 注入回忆：{d} · {c}{o} · {total} 字")
        except Exception as e:
            logger.debug(f"[diary] 注入日志失败: {e}")

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
        # 没有会话归属（比如网页上点「让她写」）就退回默认模型
        for meth in ("get_using_provider_async", "get_using_provider"):
            fn = getattr(self.context, meth, None)
            if not callable(fn):
                continue
            try:
                prov = fn() if meth.endswith("_async") else fn(None)
                if asyncio.iscoroutine(prov):
                    prov = await prov
                if prov is not None:
                    return str(prov.meta().id)
            except Exception as e:
                logger.debug(f"[diary] {meth} 取模型失败: {e}")
        # 最后随便挑一个已配置的模型，总比什么都不做强
        try:
            provs = self.context.get_all_providers() or []
            if provs:
                return str(provs[0].meta().id)
        except Exception as e:
            logger.debug(f"[diary] 枚举模型失败: {e}")
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
            if provider is None:
                raise RuntimeError(
                    "没找到能用的模型：请在「设置」里选一个「写日记用的模型」，"
                    "或者先在 QQ 里跟她说句话再试"
                )
            resp = await provider.text_chat(prompt=prompt, system_prompt=system_prompt or None)
        text = (getattr(resp, "completion_text", "") or "").strip()
        if not text:
            raise RuntimeError("模型返回为空（可能是模型拒绝或配额用完了）")
        return text

    async def generate(self, date_str: str, force: bool = False) -> dict | None:
        async with self._lock:
            if not force and self.store.get(date_str):
                return self.store.get(date_str)
            logs = core.read_logs(self.root, date_str)
            sources = [str(x) for x in (self.cfg.get("diary_sources") or []) if x]
            if sources:
                logs = [e for e in logs if str(e.get("_session") or "") in sources]
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
                and core.day_count(self.root, yesterday) > 0
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

    @filter.command("想想")
    async def cmd_recall(self, event: AstrMessageEvent):
        """让她把最近的日记和聊天重新读一遍：/想想"""
        if not self._allowed(event):
            yield event.plain_result("这个只有主人能让她想起来。")
            return
        if not self.cfg.get("enabled", True) or not self._inject_enabled():
            yield event.plain_result(
                "回忆现在是关着的。先去「设置」里把「带几篇日记」或「带几天对话」调大，再来喊她。"
            )
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if umo and umo not in self._pending_recall:
            self._pending_recall.append(umo)
            if len(self._pending_recall) > 50:
                del self._pending_recall[:-50]
        yield event.plain_result("（她翻出最近的日记和聊天记录，重新看了一遍）……嗯，我都记起来了，等下跟你说。")

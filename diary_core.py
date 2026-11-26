# -*- coding: utf-8 -*-
"""日记插件的主程序逻辑（与 AstrBot 版本无关的纯函数，方便测试）。

- 配置：conf 以 `_conf_schema.json` 为准，每项额外带 `_grp`(分组)、`_label`(短标签)、`_special`。
- 日记：数据目录下的 `diaries/YYYY-MM-DD.md` 是唯一真相，文件内容：
      # 2026-11-12 星期三 · 标题
      空行
      正文
  兼容旧版 `diaries.jsonl`（每行一条 json），首次运行时自动迁移。
- 日常：`logs/YYYY-MM-DD.jsonl`，只用来给写日记时提供素材，按天数清理。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PLUGIN_NAME = "astrbot_plugin_diary"

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# ---------------------------------------------------------------- 兄弟模块加载


def load_sibling(name: str):
    """加载同目录下的另一个 .py（diary_core / webui）。

    AstrBot 把插件当作 `data.plugins.<插件名>.main` 这样的子模块导入，
    插件目录本身并不在 sys.path 里，所以普通 `import diary_core` 会失败。
    这里按文件路径加载，并把模块名挂成 `<插件包名>.<name>`，
    重载插件时 AstrBot 能连带清掉 `data.plugins.<插件名>.*`，不会残留旧代码。
    """
    import importlib.util
    import sys

    pkg = __name__.rpartition(".")[0]
    mod_name = f"{pkg}.{name}" if pkg else name
    existing = sys.modules.get(mod_name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"找不到同目录模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    return module


# ---------------------------------------------------------------- 配置

# 网页侧分组（顺序即展示顺序）
GROUPS: list[tuple[str, str, str]] = [
    ("basic", "基本", "开关、时间、设定她是谁"),
    ("writing", "写作", "模型、篇幅、风格"),
    ("record", "记录与回忆", "记哪些会话、聊天时想得起什么"),
    ("deliver", "推送", "写完发到哪里"),
    ("advanced", "高级", "指令权限、日志保留"),
]


def _f(label: str, description: str, typ: str, default, group: str = "basic", **kw):
    d = {"_label": label, "description": description, "type": typ, "default": default, "_grp": group}
    d.update(kw)
    return d


CONF_SCHEMA: dict[str, dict] = {
    "enabled": _f("总开关", "是否记录日常并自动写日记", "bool", True, "basic"),
    "diary_time": _f(
        "写日记时间",
        "每天几点写日记（24 小时制，HH:MM）",
        "string",
        "23:30",
        "basic",
        hint="到了这个点，如果今天还没写，就会自动写一篇。服务器重启后也会补写。",
    ),
    "timezone": _f(
        "时区",
        "按哪个时区算“今天”和写日记时间",
        "string",
        "Asia/Shanghai",
        "basic",
        hint="云服务器常是 UTC，填 Asia/Shanghai 才会在当地晚上写。",
    ),
    "bot_name": _f("她的名字", "写日记时用来自称", "string", "她", "basic"),
    "persona_id": _f(
        "沿用人格",
        "沿用 AstrBot 人格（可选）",
        "string",
        "",
        "basic",
        _special="select_persona",
        hint="选了会尽量读取该人格的设定；读不到时用下面的人设。",
    ),
    "persona_prompt": _f(
        "她的设定",
        "她的人设 / 世界观（推荐填，越具体越好）",
        "text",
        "",
        "basic",
        hint="例如：你是一条住在 QQ 里的小鱼，住在阿冬的手机里，怕吵、爱吃、会因为主人很久不回消息而胡思乱想……",
    ),
    "life_setting": _f(
        "日常生活设定",
        "她的日常生活设定（可选）",
        "text",
        "",
        "basic",
        hint="她平时在哪、做什么、和谁住、有什么习惯。没聊天记录时，日记会靠这些写出“她自己的一天”。",
    ),
    "provider_id": _f(
        "写作模型",
        "用哪个模型写日记（可选）",
        "string",
        "",
        "writing",
        _special="select_provider",
        hint="留空就跟当前会话用同一个模型。",
    ),
    "diary_length": _f(
        "日记字数",
        "日记正文大概多少字",
        "int",
        450,
        "writing",
        slider={"min": 150, "max": 1200, "step": 50},
    ),
    "extra_style": _f(
        "写作风格",
        "写作风格补充（可选）",
        "text",
        "",
        "writing",
        hint="例如：句子短一点，不要用成语，偶尔写错别字。",
    ),
    "watch_sessions": _f(
        "记录哪些会话",
        "记录哪些会话的聊天（留空 = 全部）",
        "list",
        [],
        "record",
        _special="umo",
        hint="只把想让她记住的私聊/群加进来，别把无关群的聊天写进日记。",
    ),
    "diary_sources": _f(
        "日记素材来源",
        "写日记时参考哪些会话（留空 = 全部会话）",
        "list",
        [],
        "record",
        _special="umo",
        hint="只让某几个会话进日记，别的群聊得再热闹也不写进去。留空就是全都参考。",
    ),
    "inject_recent": _f(
        "带进最近几篇日记",
        "回忆里带进最近几篇日记（0 = 不带）",
        "int",
        2,
        "record",
        slider={"min": 0, "max": 5, "step": 1},
        hint="只在“这段对话第一次说话 / 你 /new 或 /reset 之后 / 过了刷新时间”时带一次，"
        "同一段对话里不会反复注入。每篇只取前 200 字。",
    ),
    "inject_log_days": _f(
        "带进几天聊天记录",
        "回忆里带进最近几天的对话（0 = 不带）",
        "int",
        1,
        "record",
        slider={"min": 0, "max": 5, "step": 1},
        hint="带进她自己视角的对话记录，分段标注来源，她自己说的话标成「你」。",
    ),
    "inject_max_chars": _f(
        "回忆字数上限",
        "一次带进多少字（日记 + 对话合计）",
        "int",
        10000,
        "record",
        slider={"min": 2000, "max": 20000, "step": 500},
        hint="大了记得清楚但费 token，小了省事但容易忘。10000 字大约 3000 token。",
    ),
    "inject_daily_time": _f(
        "记忆刷新时间",
        "已停用：现在只有 /想想 会带回忆，不再按时间自动注入（留着这一项只为兼容旧配置）",
        "string",
        "04:00",
        "record",
        hint="默认凌晨 4 点：这个点之后的第一句话，她会重新带一次回忆；同一天后面接着聊就不再带。",
    ),
    "carry_sessions": _f(
        "回忆范围",
        "回忆里可以带哪些会话的记录",
        "string",
        "all",
        "record",
        options=[
            {"value": "current", "label": "只当前会话（最干净）"},
            {"value": "private", "label": "当前会话 + 所有私聊"},
            {"value": "all", "label": "全部会话（默认）"},
        ],
        hint="范围管“能带多少”，下面那个开关管“哪些话不能跨群”。",
    ),
    "carry_private_to_group": _f(
        "私聊内容进群",
        "允许私聊里说过的话出现在群聊的回忆里",
        "bool",
        False,
        "record",
        hint="默认关。打开后她在群里可能提起你们私下说过的事——想清楚再开。",
    ),
    "inject_in_group": _f(
        "群聊里也带回忆",
        "（已停用）自动注入取消后，群聊和私聊一样——发 /想想 就会带",
        "bool",
        False,
        "record",
        hint="已停用：自动注入取消后，群聊和私聊一样——发 /想想 就会带。留这一项只为兼容旧配置。",
    ),
    "migrate_legacy_logs": _f(
        "转换旧聊天记录",
        "升级时把旧格式的聊天记录分散到各会话",
        "bool",
        True,
        "record",
        hint="只读取旧文件生成新文件，旧文件原样保留，不会删也不会改。",
    ),
    "legacy_in_recall": _f(
        "旧记录参与回忆",
        "旧格式聊天记录也进回忆（来源标注为历史记录）",
        "bool",
        True,
        "record",
        hint="关掉之后，旧记录只当写日记的素材，不会在聊天时被想起来。",
    ),
    "deliver_to": _f(
        "推送目标",
        "日记写好后发到哪里（留空 = 只存不发）",
        "list",
        [],
        "deliver",
        _special="umo",
        hint="一般选和你的私聊。也支持发到某个群。",
    ),
    "admin_only": _f("仅管理员", "只有管理员能用日记指令", "bool", True, "advanced"),
    "keep_log_days": _f(
        "日志保留天数",
        "聊天记录保留多少天（日记永久保留）",
        "int",
        30,
        "advanced",
        slider={"min": 3, "max": 180, "step": 1},
    ),
}

# 旧版配置键 → 新键（覆盖安装时把老配置搬过来）
LEGACY_ALIASES = {"watch_sessions": ("watch_umos", "watch"), "deliver_to": ("deliver_umos", "deliver")}


def default_conf() -> dict:
    return {k: (list(v["default"]) if isinstance(v["default"], list) else v["default"]) for k, v in CONF_SCHEMA.items()}


def coerce(key: str, value):
    spec = CONF_SCHEMA.get(key)
    if not spec:
        return value
    typ = spec["type"]
    try:
        if typ == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "y")
            return bool(value)
        if typ == "int":
            return int(float(value))
        if typ == "list":
            if isinstance(value, str):
                return [s.strip() for s in re.split(r"[\n,，]", value) if s.strip()]
            if isinstance(value, (list, tuple)):
                return [str(x).strip() for x in value if str(x).strip()]
            return [] if value is None else [str(value)]
        return "" if value is None else str(value)
    except Exception:
        default = spec["default"]
        return list(default) if isinstance(default, list) else default


def normalize_conf(raw) -> dict:
    """把任意来源（AstrBotConfig / 网页提交）的配置整形成合法 dict。"""
    out = default_conf()
    if not raw:
        return out
    try:
        items = dict(raw).items()
    except Exception:
        return out
    for k, v in items:
        if k in CONF_SCHEMA:
            if v is not None:
                out[k] = coerce(k, v)
            continue
        for real, aliases in LEGACY_ALIASES.items():
            if k in aliases and v:
                out[real] = coerce(real, v)
    return out


def schema_for_web() -> dict:
    """给网页用的 schema：去掉 AstrBot 自己的渲染字段，加上分组和标签。"""
    groups = [{"key": g, "name": n, "desc": d} for g, n, d in GROUPS]
    fields = []
    for key, spec in CONF_SCHEMA.items():
        item = {
            "key": key,
            "label": spec.get("_label", key),
            "type": spec["type"],
            "default": spec["default"],
            "desc": spec.get("description", ""),
            "hint": spec.get("hint", ""),
            "group": spec.get("_grp", "basic"),
            "special": spec.get("_special", ""),
            "slider": spec.get("slider"),
            "options": spec.get("options"),
        }
        fields.append(item)
    return {"plugin": PLUGIN_NAME, "groups": groups, "fields": fields}


# ---------------------------------------------------------------- 日期工具


def make_tz(name: str):
    try:
        return ZoneInfo(str(name or "").strip() or "Asia/Shanghai")
    except Exception:
        return datetime.now().astimezone().tzinfo


def parse_date(date_str: str) -> datetime | None:
    try:
        return datetime.strptime(str(date_str), "%Y-%m-%d")
    except Exception:
        return None


def human_date(date_str: str) -> str:
    dt = parse_date(date_str)
    if not dt:
        return date_str
    return f"{dt.month}月{dt.day}日 · {WEEKDAYS[dt.weekday()]}"


def title_from_content(content: str) -> str:
    for line in (content or "").splitlines():
        s = re.sub(r"\s+", " ", line).strip()
        if s:
            return s[:20]
    return "无题"


# ---------------------------------------------------------------- 日记本


class DiaryStore:
    """`diaries/*.md` 为主存储；`diaries.jsonl` 只作为旧数据迁移来源。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / "diaries"
        self.legacy_path = self.root / "diaries.jsonl"
        self.cache: dict[str, dict] | None = None

    def _ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def invalidate(self) -> None:
        self.cache = None

    # ---- 序列化

    @staticmethod
    def render(date_str: str, title: str, content: str) -> str:
        return f"# {date_str} {human_date(date_str)} · {title or '无题'}\n\n{(content or '').strip()}\n"

    @staticmethod
    def parse(date_str: str, text: str) -> dict:
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        title = ""
        if lines and lines[0].lstrip().startswith("#"):
            head = lines[0].strip().lstrip("#").strip()
            # 两种历史格式都要认：
            #   `2026-11-10 11月10日 · 星期二 · 标题`（v2 render 的输出）
            #   `2026-11-10 星期二 · 标题`（v1 的输出）
            while True:
                stripped = re.sub(r"^\d{4}-\d{1,2}-\d{1,2}\s*", "", head)
                stripped = re.sub(r"^\d{1,2}月\d{1,2}日\s*", "", stripped)
                stripped = re.sub(r"^(星期[一二三四五六日天]|周[一二三四五六日天])\s*", "", stripped)
                stripped = re.sub(r"^[·•\-—:|～~\s]+", "", stripped)
                if stripped == head:
                    break
                head = stripped
            title = head.strip()
            lines = lines[1:]
        content = "\n".join(lines).strip()
        return {
            "date": date_str,
            "title": title or title_from_content(content),
            "content": content,
        }

    # ---- 读写

    def get(self, date_str: str) -> dict | None:
        return self.all().get(date_str)

    def exists(self, date_str: str) -> bool:
        return (self.dir / f"{date_str}.md").exists() or date_str in self.all()

    def all(self) -> dict[str, dict]:
        if self.cache is not None:
            return self.cache
        items: dict[str, dict] = {}
        self._ensure()
        try:
            for f in sorted(self.dir.glob("*.md")):
                date_str = f.stem
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
                    continue
                try:
                    text = f.read_text(encoding="utf-8")
                except Exception:
                    continue
                d = self.parse(date_str, text)
                d["chars"] = len(d["content"])
                items[date_str] = d
        except Exception:
            pass
        self.cache = items
        return items

    def sorted_dates(self, desc: bool = True) -> list[str]:
        return sorted(self.all().keys(), reverse=desc)

    def recent(self, before: str, n: int = 2) -> list[dict]:
        keys = [k for k in self.sorted_dates() if k < before][:n]
        return [self.all()[k] for k in keys]

    def save(self, diary: dict) -> dict:
        date_str = str(diary.get("date") or "")
        if not parse_date(date_str):
            raise ValueError(f"日期不合法：{date_str}")
        title = str(diary.get("title") or "").strip()[:40]
        content = str(diary.get("content") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not content:
            raise ValueError("正文不能为空")
        # 标题里不能有换行/竖线之外的分隔符会破坏首行解析，这里统一压成一行
        title = re.sub(r"\s+", " ", title).strip()
        self._ensure()
        target = self.dir / f"{date_str}.md"
        tmp = target.with_suffix(".md.tmp")
        tmp.write_text(self.render(date_str, title, content), encoding="utf-8")
        tmp.replace(target)
        if self.cache is not None:
            if date_str in self.cache:
                item = self.cache[date_str]
            else:
                item = {"date": date_str, "created_at": datetime.now().isoformat(timespec="seconds"), "source": "manual"}
            item.update({"title": title or title_from_content(content), "content": content, "chars": len(content)})
            self.cache[date_str] = item
        return self.all()[date_str]

    def delete(self, date_str: str) -> bool:
        target = self.dir / f"{date_str}.md"
        ok = False
        try:
            if target.exists():
                target.unlink()
                ok = True
        except Exception:
            raise
        if self.cache is not None and date_str in self.cache:
            self.cache.pop(date_str, None)
            ok = True
        return ok

    # ---- 旧数据迁移

    def migrate_legacy(self) -> int:
        """把旧版 diaries.jsonl 里还没迁移的条目写进 diaries/。返回迁移条数。"""
        if not self.legacy_path.exists():
            return 0
        moved = 0
        try:
            lines = self.legacy_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            date_str = str(obj.get("date") or "")
            content = str(obj.get("content") or "").strip()
            title = str(obj.get("title") or "").strip()
            if not parse_date(date_str) or not content:
                continue
            target = self.dir / f"{date_str}.md"
            if target.exists():
                continue
            self._ensure()
            target.write_text(self.render(date_str, title, content), encoding="utf-8")
            moved += 1
        if moved:
            self.invalidate()
        return moved

    # ---- 给记忆/网页用

    def summary(self, date_str: str, limit: int = 200) -> str:
        d = self.all().get(date_str) or {}
        body = re.sub(r"\s+", " ", d.get("content") or "").strip()
        return body[:limit]

    def recall_text(self, today: str, n: int, limit: int = 200, stats: dict | None = None) -> str:
        """回忆里要用到的日记正文：最近 n 篇，按时间正序，每篇只取开头 limit 字。

        stats 传一个 dict 进来，会填上 count / first / last，方便打日志。
        """
        if n <= 0:
            return ""
        keys = [k for k in self.sorted_dates() if k <= today][:n]
        if not keys:
            return ""
        if stats is not None:
            stats.update(count=len(keys), first=min(keys), last=max(keys))
        out = []
        for k in sorted(keys):
            d = self.all()[k]
            out.append(f"# {k} {human_date(k)} · {d.get('title', '')}\n\n{self.summary(k, limit)}")
        return "\n\n".join(out)


# ---------------------------------------------------------------- 会话名对照表


def sanitize_name(text: str, limit: int = 40) -> str:
    """文件名能用的名字：去掉路径分隔符和 Windows 禁用字符。"""
    s = str(text or "").strip()
    for ch in '\\/:*?"<>|':
        s = s.replace(ch, "-")
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:limit] or "会话"


def umo_kind(umo: str) -> str:
    return "group" if "GroupMessage" in str(umo or "") else "private"


def umo_tail(umo: str) -> str:
    text = str(umo or "")
    return text.rsplit(":", 1)[-1] if ":" in text else text


def build_name(kind: str, title: str, umo: str) -> str:
    title = str(title or "").strip() or umo_tail(umo) or "未知"
    return sanitize_name(("群" if kind == "group" else "私聊") + "·" + title)


class SessionBook:
    """UMO → 可读名 / 日志文件名 的对照表（保存在 sessions.json）。

    只干一件事：把一长串号码翻译成人能看懂的文件名。
    - 第一次见到某个会话时定下文件名，之后改名不跟着变（否则记录会被劈成两半）
    - 两个会话撞名时，后来的加 -2、-3
    - 表丢了可以从日志的 _head 行重建，最坏情况是名字退回到号码
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "sessions.json"
        self._data: dict | None = None

    def _load(self) -> dict:
        if self._data is not None:
            return self._data
        data: dict = {}
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("sessions"), dict):
                    data = dict(raw["sessions"])
        except Exception:
            data = {}
        self._data = data
        return data

    def save(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"sessions": self._load()}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def all(self) -> dict[str, dict]:
        return self._load()

    def get(self, umo: str) -> dict:
        rec = self._load().get(str(umo or ""))
        return rec if isinstance(rec, dict) else {}

    def name(self, umo: str) -> str:
        rec = self.get(umo)
        return str(rec.get("name") or "") or build_name(umo_kind(umo), "", umo)

    def title(self, umo: str) -> str:
        rec = self.get(umo)
        return str(rec.get("title") or rec.get("name") or "") or self.name(umo)

    def stem(self, umo: str) -> str:
        rec = self.get(umo)
        return str(rec.get("file") or "") or sanitize_name(self.name(umo))

    @staticmethod
    def _unique(book: dict, base: str) -> str:
        used = {str(r.get("file")) for r in book.values() if isinstance(r, dict) and r.get("file")}
        stem, i = base, 2
        while stem in used:
            stem = f"{base}-{i}"
            i += 1
        return stem

    def register(self, umo: str, kind: str = "", title: str = "", when: str = "") -> dict:
        umo = str(umo or "").strip()
        if not umo:
            return {}
        book = self._load()
        kind = kind or umo_kind(umo)
        title = str(title or "").strip()
        rec = book.get(umo)
        if not isinstance(rec, dict):
            base = build_name(kind, title, umo)
            rec = {
                "name": base,
                "file": self._unique(book, base),
                "kind": kind,
                "title": title or base,
                "since": (when or "")[:10],
                "last": when or "",
            }
            book[umo] = rec
            self._data = book
            self.save()
            return dict(rec)
        changed = False
        if title and rec.get("title") != title:
            rec["title"] = title
            changed = True
        if when and rec.get("last") != when:
            rec["last"] = when
            changed = True
        if not rec.get("file"):
            base = build_name(kind, title, umo)
            rec["file"] = self._unique(book, base)
            rec.setdefault("name", base)
            changed = True
        if changed:
            self._data = book
            self.save()
        return dict(rec)


# ---------------------------------------------------------------- 聊天日志
#
# 新版：logs/YYYY-MM-DD/<可读名>.jsonl，一天一个目录，一个会话一个文件。
#   首行 {"_head":1,"umo":"…","name":"群·摸鱼小分队","kind":"group","since":"2026-11-23"}
#   消息 {"t":"13:20","q":"678901","n":"小红","m":"…"}
#        {"t":"13:25","bot":"霖霏","m":"…"}   ← 有 bot 字段就是她自己说的
# 旧版：logs/YYYY-MM-DD.jsonl（单文件，群消息多一个 g 字段），只读、只当素材。


def logs_root(root: Path) -> Path:
    return Path(root) / "logs"


def log_path(root: Path, date_str: str) -> Path:
    """旧版单文件日志的路径（迁移和兼容用）。"""
    return logs_root(root) / f"{date_str}.jsonl"


def day_dir(root: Path, date_str: str) -> Path:
    return logs_root(root) / str(date_str)


def session_path(root: Path, date_str: str, stem: str) -> Path:
    return day_dir(root, date_str) / f"{sanitize_name(stem)}.jsonl"


def read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        if not path.exists():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    except Exception:
        return out
    return out


def append_session_log(root: Path, date_str: str, stem: str, head: dict, entry: dict) -> None:
    path = session_path(root, date_str, stem)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    with path.open("a", encoding="utf-8") as f:
        if fresh:
            payload = dict(head or {})
            payload["_head"] = 1
            payload.setdefault("since", date_str)
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_sessions(root: Path, date_str: str) -> dict[str, dict]:
    """某天每个会话的记录：umo → {umo, name, kind, entries}。"""
    out: dict[str, dict] = {}
    d = day_dir(root, date_str)
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.jsonl")):
        head: dict = {}
        entries: list[dict] = []
        for obj in read_jsonl(f):
            if obj.get("_head"):
                head = obj
                continue
            entries.append(obj)
        if not entries:
            continue
        umo = str(head.get("umo") or f.stem)
        out[umo] = {
            "umo": umo,
            "name": str(head.get("name") or f.stem),
            "kind": str(head.get("kind") or umo_kind(umo)),
            "entries": entries,
        }
    return out


def read_legacy_entries(root: Path, date_str: str) -> list[dict]:
    return [o for o in read_jsonl(log_path(root, date_str)) if not o.get("_head")]


def day_count(root: Path, date_str: str) -> int:
    total = 0
    for rec in read_sessions(root, date_str).values():
        total += len(rec.get("entries") or [])
    return total + len(read_legacy_entries(root, date_str))


def read_logs(root: Path, date_str: str) -> list[dict]:
    """某天所有消息（新旧格式都算），按会话、时间排序，给写日记当素材用。"""
    items: list[dict] = []
    sessions = read_sessions(root, date_str)
    for umo, rec in sessions.items():
        for e in rec.get("entries") or []:
            item = dict(e)
            item["_session"] = umo
            item["_session_name"] = rec.get("name")
            items.append(item)
    if sessions and day_dir(root, date_str).is_dir():
        # 已经按会话分过文件了，旧的单文件只是留着备查，不再重复算一遍
        items.sort(key=lambda x: (str(x.get("_session") or ""), str(x.get("t") or "")))
        return items
    for e in read_legacy_entries(root, date_str):
        item = dict(e)
        item["_legacy"] = True
        item["_session"] = (
            f"aiocqhttp:GroupMessage:{e.get('g')}" if e.get("g") else "aiocqhttp:FriendMessage:unknown"
        )
        item["_session_name"] = "历史记录"
        items.append(item)
    items.sort(key=lambda x: (str(x.get("_session") or ""), str(x.get("t") or "")))
    return items


def append_log(root: Path, date_str: str, entry: dict) -> None:
    """旧接口：直接往当天单文件里追加（新代码走 append_session_log）。"""
    path = log_path(root, date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_stats(root: Path, date_str: str) -> dict:
    count = day_count(root, date_str)
    exists = day_dir(root, date_str).is_dir() or log_path(root, date_str).exists()
    return {"date": date_str, "count": count, "exists": bool(count or exists)}


def format_logs(logs: list[dict]) -> str:
    if not logs:
        return "（今天几乎没有人说话，是安静的一天。）"
    lines = []
    for e in logs[-200:]:
        who = "我" if e.get("bot") else (e.get("n") or "有人")
        body = str(e.get("m") or "").replace("\n", " ")
        lines.append(f"{e.get('t', '')} {who}：{body}")
    text = "\n".join(lines)
    if len(text) > 3600:
        text = "……\n" + text[-3600:]
    return text


def cleanup_logs(root: Path, keep_days: int, now: datetime) -> int:
    """按天数清理新旧两种格式，返回删掉的条目（文件/目录）数。"""
    removed = 0
    base = logs_root(root)
    try:
        cutoff = (now.date() - timedelta(days=max(1, int(keep_days)))).toordinal()
    except Exception:
        return 0
    try:
        for f in base.glob("*.jsonl"):
            try:
                d = datetime.strptime(f.stem, "%Y-%m-%d").date()
            except Exception:
                continue
            if d.toordinal() < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
        for sub in base.iterdir():
            if not sub.is_dir():
                continue
            try:
                d = datetime.strptime(sub.name, "%Y-%m-%d").date()
            except Exception:
                continue
            if d.toordinal() < cutoff:
                shutil.rmtree(sub, ignore_errors=True)
                removed += 1
    except Exception:
        return removed
    return removed


def migrate_legacy_logs(
    root: Path,
    book: SessionBook,
    *,
    bot_name: str = "",
    private_umo: str = "",
) -> int:
    """旧版单文件日志 → 按会话分文件。只读旧文件、只写新文件。返回转换的会话文件数。"""
    written = 0
    for f in sorted(logs_root(root).glob("*.jsonl")):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.stem):
            continue
        date_str = f.stem
        objs = [o for o in read_jsonl(f) if not o.get("_head")]
        if not objs:
            continue
        nicks: list[str] = []
        for o in objs:
            if o.get("bot") or o.get("g"):
                continue
            n = str(o.get("n") or "").strip()
            if n and n not in nicks:
                nicks.append(n)
        private_title = nicks[0] if len(nicks) == 1 else ""
        buckets: dict[str, dict] = {}
        for o in objs:
            gid = o.get("g")
            if gid:
                umo, kind, title = f"aiocqhttp:GroupMessage:{gid}", "group", ""
            else:
                umo = private_umo or "aiocqhttp:FriendMessage:unknown"
                kind = "private"
                title = private_title or ("" if private_umo else "历史记录")
            cur = buckets.get(umo)
            if cur is None:
                cur = buckets[umo] = {"umo": umo, "kind": kind, "title": title, "entries": []}
            cur["entries"].append(o)
        for umo, cur in buckets.items():
            rec = book.register(umo, cur["kind"], cur["title"], when=date_str)
            stem = rec.get("file") or sanitize_name(umo)
            path = session_path(root, date_str, stem)
            if path.exists():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as w:
                head = {"_head": 1, "umo": umo, "name": rec.get("name") or stem, "kind": cur["kind"], "since": date_str}
                w.write(json.dumps(head, ensure_ascii=False) + "\n")
                for o in cur["entries"]:
                    bot = o.get("bot")
                    if bot:
                        name = bot if isinstance(bot, str) else (bot_name or "我")
                        line = {"t": o.get("t", ""), "bot": name, "m": o.get("m", "")}
                    else:
                        line = {
                            "t": o.get("t", ""),
                            "q": str(o.get("q") or ""),
                            "n": o.get("n", ""),
                            "m": o.get("m", ""),
                        }
                    w.write(json.dumps(line, ensure_ascii=False) + "\n")
            written += 1
    return written


# ---------------------------------------------------------------- 回忆


RECALL_RULES = (
    "下面是你记得的、之前发生过的事，当背景资料看，不用一条条讲出来，也不要说\u201c我记得\u201d。\n"
    "聊天照人格设定说话，别学这里面的语气和说话方式。\n"
    "不要从里面推断现在几点、今天几号。\n"
    "对方现在说的和你记得的不一样，以对方为准。"
)


def hm_of(text: str, dh: int, dm: int) -> tuple[int, int]:
    m = re.match(r"^(\d{1,2})[:：](\d{1,2})$", str(text or "").strip())
    if not m:
        return dh, dm
    return min(max(int(m.group(1)), 0), 23), min(max(int(m.group(2)), 0), 59)


def recall_day(now: datetime, refresh: str = "04:00") -> str:
    """记忆的「这一天」从刷新时间算起：刷新时间之前，还算前一天。"""
    h, mi = hm_of(refresh, 4, 0)
    if now.hour * 60 + now.minute >= h * 60 + mi:
        return now.strftime("%Y-%m-%d")
    return (now - timedelta(days=1)).strftime("%Y-%m-%d")


def format_line(e: dict) -> str:
    t = str(e.get("t") or "")
    body = str(e.get("m") or "").replace("\n", " ").strip()
    bot = e.get("bot")
    if bot:
        if not isinstance(bot, str) or not bot.strip() or bot in ("我", "她"):
            who = "你"
        else:
            who = f"{bot}(你)"
        return f"{t} {who}：{body}"
    n = str(e.get("n") or "有人")
    q = str(e.get("q") or "").strip()
    return f"{t} {n}(QQ:{q})：{body}" if q else f"{t} {n}：{body}"


def fit_lines(lines: list[str], budget: int) -> list[str]:
    """从最新往回装，装不下就丢最老的；丢了就在开头留一句省略。"""
    if budget <= 0 or not lines:
        return []
    picked: list[str] = []
    used = 0
    for line in reversed(lines):
        cost = len(line) + 1
        if used + cost > budget:
            break
        picked.append(line)
        used += cost
    picked.reverse()
    if not picked:
        return []
    if len(picked) < len(lines):
        picked.insert(0, "……（更早的略）")
    return picked


def fit_entries(entries: list[dict], budget: int) -> tuple[list[str], list[dict]]:
    """从最新往回装，返回（装下的行，还没装下的记录）。"""
    lines = [format_line(e) for e in entries]
    if budget <= 0 or not lines:
        return [], list(entries)
    picked: list[str] = []
    used = 0
    i = len(lines)
    while i > 0:
        cost = len(lines[i - 1]) + 1
        if used + cost > budget:
            break
        i -= 1
        picked.append(lines[i])
        used += cost
    picked.reverse()
    if not picked:
        return [], list(entries)
    if i > 0:
        picked.insert(0, "……（更早的略）")
    return picked, list(entries[:i])


def month_day(date_str: str) -> str:
    """2026-11-22 → 11月22日"""
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", str(date_str or "").strip())
    if not m:
        return str(date_str or "")
    return f"{int(m.group(2))}月{int(m.group(3))}日"


def build_chat_blocks(
    root: Path,
    dates: list[str],
    *,
    current_umo: str = "",
    total_budget: int = 8000,
    carry: str = "all",
    private_to_group: bool = False,
    legacy_in_recall: bool = True,
    stats: dict | None = None,
) -> str:
    """把最近几天的对话排成带段头（含日期）的一段文字。

    额度这么分：当前会话先拿七成，其余会话平分剩下的三成。
    每个会话只装一次、内部按天从新往旧连续装，装到额度用完为止，
    剩下的额度不再回填——回填会把同一天拆成两截，时间线就断了。
    """
    if total_budget <= 0:
        return ""
    is_group = "GroupMessage" in str(current_umo or "")

    groups: dict[str, dict] = {}
    for date_str in dates:
        converted = day_dir(root, date_str).is_dir()
        for umo, rec in read_sessions(root, date_str).items():
            kind = str(rec.get("kind") or umo_kind(umo))
            if umo != current_umo:
                if carry == "current":
                    continue
                if carry == "private" and kind != "private":
                    continue
                if is_group and kind == "private" and not private_to_group:
                    continue
            g = groups.setdefault(umo, {"umo": umo, "name": rec.get("name") or umo, "days": []})
            if rec.get("entries"):
                g["days"].append((date_str, rec["entries"]))
        # 旧单文件只在当天没有转换后的会话文件时才用，避免同一批话进两遍
        if legacy_in_recall and not converted:
            entries = read_legacy_entries(root, date_str)
            if entries:
                g = groups.setdefault("", {"umo": "", "name": "历史记录", "days": []})
                g["days"].append((date_str, entries))

    groups = {k: v for k, v in groups.items() if v["days"]}
    if not groups:
        return ""
    for g in groups.values():
        g["days"].sort(key=lambda x: x[0], reverse=True)

    packed: list[tuple[str, str, str, list[str]]] = []

    def take(key: str, budget: int) -> int:
        """把这个会话按天从新往旧装进 budget，返回实际用掉的字数。"""
        g = groups[key]
        used = 0
        while g["days"] and used < budget - 120:
            date_str, entries = g["days"][0]
            left = budget - used
            if left < 120:
                break
            lines, rem = fit_entries(entries, left)
            if not lines:
                g["days"].pop(0)
                continue
            packed.append((date_str, str(g["name"]), key, lines))
            used += sum(len(x) + 6 for x in lines)
            if rem:
                g["days"][0] = (date_str, rem)
            else:
                g["days"].pop(0)
        return used

    used = 0
    if current_umo in groups:
        used += take(current_umo, max(600, int(total_budget * 0.7)))
    others = [k for k in groups if k != current_umo]
    if others:
        rest = max(0, total_budget - used)
        share = rest // len(others)
        if share >= 120:
            for k in others:
                used += take(k, share)

    if not packed:
        return ""
    # 每个会话只装一次、内部按天从新往旧连续装，所以直接按日期倒序输出即可
    out: list[str] = []
    for date_str, name, umo, lines in sorted(packed, key=lambda x: x[0], reverse=True):
        tag = month_day(date_str)
        out.append(f"【{tag} · {name} · {umo}】" if umo else f"【{tag} · {name}】")
        out.extend(lines)
        out.append("")
    if stats is not None:
        cur = {"count": 0, "first": "", "last": ""}
        oth = {"count": 0, "first": "", "last": ""}
        for date_str, _name, umo, _lines in packed:
            box = cur if umo and umo == current_umo else oth
            box["count"] += 1
            if not box["first"] or date_str > box["first"]:
                box["first"] = date_str
            if not box["last"] or date_str < box["last"]:
                box["last"] = date_str
        stats["current"] = cur
        stats["others"] = oth
    return "\n".join(out).strip()


def build_recall(diary_text: str, chat_text: str, stamp: str = "") -> str:
    parts = [f"【回忆】{stamp}".rstrip()]
    if diary_text:
        parts.append(f"<我的日记>\n{diary_text}\n</我的日记>")
    if chat_text:
        parts.append(f"<最近的对话>\n{chat_text}\n</最近的对话>")
    parts.append(
        "<说明>\n"
        "上面两段是你自己的东西：你写下的日记，和你真的在场的对话。\n"
        "它们是你记得的事，不是你现在的任务。\n\n"
        f"{RECALL_RULES}\n"
        "</说明>"
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 提示词


def system_prompt(conf: dict, persona: str, date_str: str) -> str:
    name = str(conf.get("bot_name") or "她").strip() or "她"
    dt = parse_date(date_str) or datetime.now()
    length = int(conf.get("diary_length") or 450)
    extra = str(conf.get("extra_style") or "").strip()
    lines = [f"你叫{name}。"]
    if persona:
        lines.append(f"你的人设与生活：\n{persona}")
    life = str(conf.get("life_setting") or "").strip()
    if life:
        lines.append(f"额外的背景设定：\n{life}")
    lines.append(
        "现在，你在写今天的日记。这是只给你自己看的本子，不是发给别人的消息，"
        "不需要照顾读者的感受。"
    )
    lines.append(
        "写法要求：\n"
        "1. 第一人称，用你自己的语气，可以有口头禅、小情绪、说不出口的话。\n"
        "2. 写具体的今天：场景、动作、对话、光线、声音、温度、味道。不要空泛抒情。\n"
        "3. 不要写总结报告，不要分点罗列，不要“今天真是充实的一天”这种套话。\n"
        "4. 不提 AI、模型、程序、设定、扮演，也不要解释自己在做什么。\n"
        "5. 可以写你没说出口的念头，也可以写无聊、烦躁、走神这些不完美的部分。\n"
        "6. 和最近几篇日记保持时间上的连贯，昨天提过的事今天可以接着写。\n"
        f"7. 正文 {length} 字左右，宁可少而真，不要多而空。"
    )
    if extra:
        lines.append(f"另外：{extra}")
    lines.append(
        "输出格式（严格遵守）：\n"
        f"第一行：{date_str} {WEEKDAYS[dt.weekday()]} · 标题（标题不超过 18 字，不要书名号）\n"
        "第二行：空行\n"
        "之后：正文"
    )
    return "\n".join(lines)


def user_prompt(date_str: str, chats: str, recent: list[dict]) -> str:
    dt = parse_date(date_str)
    parts = [f"今天：{date_str} {WEEKDAYS[dt.weekday()] if dt else ''}", ""]
    parts.append("今天你经历/听到的片段（时间顺序，可能不完整）：")
    parts.append(chats)
    if recent:
        parts.append("")
        parts.append("你前几天的日记（节选，仅供衔接，不要重复）：")
        for d in recent:
            body = re.sub(r"\s+", " ", (d.get("content") or "")).strip()
            parts.append(f"【{d['date']} · {d.get('title', '')}】{body[:220]}")
    parts.append("")
    parts.append("现在写今天的日记。")
    return "\n".join(parts)


def _strip_date_weekday(text: str) -> str:
    """把「2026-11-19 星期四 ·」这类前缀噪音一层层剥掉（模型很爱加）。"""
    pats = (
        r"^\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?",
        r"^\d{1,2}[-/.月]\d{1,2}日",
        r"^星期[一二三四五六日天]",
        r"^周[一二三四五六日天]",
        r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)",
    )
    out = text or ""
    for _ in range(5):
        before = out
        for pat in pats:
            out = re.sub(pat, "", out, flags=re.IGNORECASE)
        out = out.lstrip(" ·-—|:：　*#\t")
        if out == before:
            break
    return out


def parse_llm_output(raw: str) -> dict | None:
    """把模型输出拆成 {title, content}。"""
    text = (raw or "").strip()
    if not text:
        return None
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return None
    first = lines[0].strip()
    title = re.sub(r"^[#*\s]+", "", first).strip("*# ")
    title = _strip_date_weekday(title)
    m = re.match(r"^\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?\s*[·\-—|:：]?\s*(.*)$", title)
    if m:
        title = _strip_date_weekday(m.group(1))
    title = title.strip("·-—|:： 　")
    if len(title) > 36:
        title = title[:36]
    body = "\n".join(lines[1:]).strip()
    body = re.sub(r"^[#*\s]*正文[：:]\s*", "", body)
    if not body:
        body = first
    if not title:
        title = "无题"
    return {"title": title, "content": body}


def resolve_date(arg: str, today) -> str:
    arg = (arg or "").strip()
    if not arg or arg in ("今天", "today"):
        return today.strftime("%Y-%m-%d")
    if arg in ("昨天", "yesterday"):
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    if arg == "前天":
        return (today - timedelta(days=2)).strftime("%Y-%m-%d")
    m = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$", arg)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{1,2})[-/.月](\d{1,2})日?$", arg)
    if m:
        return f"{today.year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return today.strftime("%Y-%m-%d")


def valid_date(date_str: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date_str or ""))) and parse_date(date_str) is not None
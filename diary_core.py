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
    "inject_recent": _f(
        "带进最近几篇",
        "聊天时带进最近几篇日记（0 = 不带）",
        "int",
        2,
        "record",
        slider={"min": 0, "max": 5, "step": 1},
        hint="带进去她才会“记得自己写过什么”。每篇只取前 200 字，开销很小。",
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

    def memory_block(self, today: str, n: int) -> str:
        if n <= 0:
            return ""
        keys = [k for k in self.sorted_dates() if k <= today][:n]
        if not keys:
            return ""
        lines = [
            "<日记本>",
            "（下面是你自己写过的日记。它们是你的记忆，不要整段复述，只在合适的时候自然地想起或提起。）",
        ]
        for k in keys:
            d = self.all()[k]
            lines.append(f"{k} · {d.get('title', '')}：{self.summary(k)}")
        lines.append("</日记本>")
        return "\n".join(lines)


# ---------------------------------------------------------------- 聊天日志


def log_path(root: Path, date_str: str) -> Path:
    return Path(root) / "logs" / f"{date_str}.jsonl"


def read_logs(root: Path, date_str: str) -> list[dict]:
    path = log_path(root, date_str)
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return out


def append_log(root: Path, date_str: str, entry: dict) -> None:
    path = log_path(root, date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_stats(root: Path, date_str: str) -> dict:
    logs = read_logs(root, date_str)
    return {
        "date": date_str,
        "count": len(logs),
        "exists": log_path(root, date_str).exists(),
    }


def format_logs(logs: list[dict]) -> str:
    if not logs:
        return "（今天几乎没有人说话，是安静的一天。）"
    lines = []
    for e in logs[-150:]:
        who = "我" if e.get("bot") else (e.get("n") or "有人")
        lines.append(f"{e.get('t', '')} {who}：{e.get('m', '')}")
    text = "\n".join(lines)
    if len(text) > 3200:
        text = "……\n" + text[-3200:]
    return text


def cleanup_logs(root: Path, keep_days: int, now: datetime) -> int:
    removed = 0
    try:
        cutoff = (now.date() - timedelta(days=max(1, int(keep_days)))).toordinal()
        for f in (Path(root) / "logs").glob("*.jsonl"):
            try:
                d = datetime.strptime(f.stem, "%Y-%m-%d").date()
            except Exception:
                continue
            if d.toordinal() < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
    except Exception:
        return removed
    return removed


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
    m = re.match(r"^\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?\s*[·\-—|:：]?\s*(.*)$", title)
    if m:
        title = m.group(1).strip()
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
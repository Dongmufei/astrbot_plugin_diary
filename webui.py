# -*- coding: utf-8 -*-
"""网页管理界面用到的后端接口（AstrBot 插件 Web API）。

前端在 `views/diary/`，通过 bridge SDK 调用，路由统一以 /astrbot_plugin_diary/ 开头：
    GET    /astrbot_plugin_diary/diaries                 列表（分页/搜索）
    GET    /astrbot_plugin_diary/diaries/<date>          某一天（含用户当天聊天记录）
    PUT    /astrbot_plugin_diary/diaries/<date>          保存/新建
    DELETE /astrbot_plugin_diary/diaries/<date>          删除
    POST   /astrbot_plugin_diary/diaries/<date>/generate 让她重新写这一天
    GET    /astrbot_plugin_diary/stats                   概览
    GET    /astrbot_plugin_diary/settings                当前配置 + schema
    POST   /astrbot_plugin_diary/settings                保存配置
    GET    /astrbot_plugin_diary/export                  导出全部日记（markdown）
"""

from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger

# 同目录模块不能直接 import：见 diary_core.load_sibling 的说明。
def _sibling_core():
    import importlib.util
    import sys

    pkg = __name__.rpartition(".")[0]
    name = f"{pkg}.diary_core" if pkg else "diary_core"
    mod = sys.modules.get(name)
    if mod is not None:
        return mod
    from pathlib import Path

    path = Path(__file__).resolve().parent / "diary_core.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"找不到 diary_core.py：{path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


core = _sibling_core()

PLUGIN_NAME = core.PLUGIN_NAME


def _qr(request) -> dict:
    try:
        return request.query or {}
    except Exception:
        return {}


def _q1(q: dict, key: str) -> str:
    try:
        v = q.get(key)
    except Exception:
        return ""
    if isinstance(v, (list, tuple)):
        return str(v[0]) if v else ""
    return str(v or "")


def _qi(q: dict, key: str, default: int) -> int:
    try:
        return int(_q1(q, key) or default)
    except Exception:
        return default


def _req_body(kwargs: dict):
    """优先拿框架传进来的 request；取不到就用模块级的 request。"""
    req = kwargs.get("request")
    if req is None:
        try:
            from astrbot.api.web import request as req
        except Exception:
            req = None
    return req


async def _read_json(kwargs: dict) -> dict:
    req = _req_body(kwargs)
    if req is None:
        return {}
    try:
        getter = getattr(req, "json", None)
        data = getter(default={}) if callable(getter) else None
        if asyncio_iscoro(data):
            data = await data
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def ensure_web_apis(plugin) -> None:
    """注册网页接口；注册过就跳过（热重载同理）。"""
    if getattr(plugin, "_web_apis_registered", False):
        return
    reg = getattr(plugin.context, "register_web_api", None)
    if not callable(reg):
        logger.warning(
            "[diary] 当前 AstrBot 不支持插件网页接口：网页能打开但读写日记会失败，"
            "请升级 AstrBot 或改用 QQ 指令 /日记。"
        )
        return

    def add(route: str, handler, methods: list[str], desc: str) -> None:
        try:
            reg(route, handler, methods, desc)
        except Exception as e:
            logger.error(f"[diary] 注册接口 {route} 失败: {e}")

    add(f"/{PLUGIN_NAME}/diaries", list_diaries(plugin), ["GET"], "日记列表")
    add(f"/{PLUGIN_NAME}/diaries/<date>/generate", generate_diary(plugin), ["POST"], "让她写某天的日记")
    add(f"/{PLUGIN_NAME}/diaries/<date>", get_diary(plugin), ["GET"], "某一天的日记")
    add(f"/{PLUGIN_NAME}/diaries/<date>", save_diary(plugin), ["PUT", "POST"], "保存/修改日记")
    add(f"/{PLUGIN_NAME}/diaries/<date>", delete_diary(plugin), ["DELETE"], "删除日记")
    add(f"/{PLUGIN_NAME}/stats", stats(plugin), ["GET"], "日记本概览")
    add(f"/{PLUGIN_NAME}/settings", get_settings(plugin), ["GET"], "读取配置")
    add(f"/{PLUGIN_NAME}/settings", save_settings(plugin), ["POST", "PUT"], "保存配置")
    add(f"/{PLUGIN_NAME}/export", export_diaries(plugin), ["GET"], "导出日记")

    plugin._web_apis_registered = True
    logger.info("[diary] 网页接口已注册")


# ---------------------------------------------------------------- 辅助


def _diary_payload(date_str: str, d: dict) -> dict:
    dt = core.parse_date(date_str)
    return {
        "date": date_str,
        "title": d.get("title", ""),
        "content": d.get("content", ""),
        "chars": d.get("chars", len(d.get("content", ""))),
        "label": f"{dt.month}月{dt.day}日" if dt else date_str,
        "weekday": core.WEEKDAYS[dt.weekday()] if dt else "",
        "year": dt.year if dt else 0,
        "month": dt.month if dt else 0,
    }


def _user_texts(logs: list[dict], limit: int = 60) -> list[dict]:
    out = []
    for e in logs:
        if e.get("bot"):
            continue
        out.append({"t": e.get("t", ""), "n": e.get("n") or "有人", "m": e.get("m", "")})
    return out[-limit:]


def _next_date(diaries: dict[str, dict], today: str) -> str:
    keys = sorted(diaries.keys())
    if not keys:
        return today
    last = keys[-1]
    nt = (core.parse_date(last) or core.parse_date(today)).toordinal() + 1
    from datetime import date as _date

    nd = _date.fromordinal(nt).strftime("%Y-%m-%d")
    return nd if nd <= today else last


def _last_or_today(diaries: dict[str, dict], today: str) -> str:
    keys = sorted(diaries.keys())
    return keys[-1] if keys else today


# ---------------------------------------------------------------- 日记


def list_diaries(plugin):
    async def handler(**kwargs):
        from astrbot.api.web import json_response

        try:
            q = _qr(kwargs.get("request"))
            page = max(0, _qi(q, "page", 0))
            size = min(100, max(1, _qi(q, "size", 50)))
            keyword = _q1(q, "q").strip()
            items = plugin.store.all()
            keys = plugin.store.sorted_dates()
            if keyword:
                keys = [k for k in keys if keyword in items[k].get("title", "") or keyword in items[k].get("content", "")]
            total = len(keys)
            picked = keys[page * size : (page + 1) * size]
            today = plugin._today()
            next_date = _next_date(items, today)
            return json_response(
                {
                    "items": [_diary_payload(k, items[k]) for k in picked],
                    "total": total,
                    "page": page,
                    "size": size,
                    "dates": sorted(items.keys(), reverse=True),
                    "today": today,
                    "last_date": _last_or_today(items, today),
                    "next_date": next_date,
                    "has_today": today in items,
                }
            )
        except Exception as e:
            logger.error(f"[diary] 列表接口出错: {e}")
            return json_response({"items": [], "total": 0, "error": str(e)}, status_code=500)

    return handler


def get_diary(plugin):
    async def handler(date: str = "", **kwargs):
        from astrbot.api.web import json_response

        if not core.valid_date(date):
            return json_response({"error": "日期格式应为 YYYY-MM-DD"}, status_code=400)
        d = plugin.store.get(date)
        logs = core.read_logs(plugin.root, date)
        return json_response(
            {
                "date": date,
                "diary": _diary_payload(date, d) if d else None,
                "has_diary": bool(d),
                "log": {"count": len(logs), "exists": core.log_path(plugin.root, date).exists()},
                "messages": _user_texts(logs),
            }
        )

    return handler


def save_diary(plugin):
    async def handler(date: str = "", **kwargs):
        from astrbot.api.web import error_response, json_response

        if not core.valid_date(date):
            return error_response("日期格式应为 YYYY-MM-DD", status_code=400)
        data = await _read_json(kwargs)
        if not isinstance(data, dict):
            return error_response("请求体格式不对", status_code=400)
        title = str(data.get("title") or "").strip()
        content = str(data.get("content") or "").replace("\r\n", "\n").replace("\r", "\n")
        existed = plugin.store.get(date) is not None
        try:
            saved = plugin.store.save({"date": date, "title": title, "content": content})
        except Exception as e:
            return error_response(f"保存失败：{e}", status_code=400)
        logger.info(f"[diary] {'修改' if existed else '新建'}了 {date} 的日记")
        return json_response({"ok": True, "diary": _diary_payload(date, saved)})

    return handler


def delete_diary(plugin):
    async def handler(date: str = "", **kwargs):
        from astrbot.api.web import error_response, json_response

        if not core.valid_date(date):
            return error_response("日期格式应为 YYYY-MM-DD", status_code=400)
        try:
            ok = plugin.store.delete(date)
        except Exception as e:
            return error_response(f"删除失败：{e}", status_code=500)
        if not ok:
            return error_response("没有这一天的日记", status_code=404)
        logger.info(f"[diary] 删除了 {date} 的日记")
        return json_response({"ok": True, "deleted": date})

    return handler


def generate_diary(plugin):
    async def handler(date: str = "", **kwargs):
        from astrbot.api.web import error_response, json_response

        if not core.valid_date(date):
            return error_response("日期格式应为 YYYY-MM-DD", status_code=400)
        data = await _read_json(kwargs)
        force = bool((data or {}).get("force", True))
        try:
            diary = await plugin.generate(date, force=force)
        except Exception as e:
            logger.error(f"[diary] 网页生成 {date} 失败: {e}")
            return error_response(f"让她写失败：{e}", status_code=500)
        if not diary:
            return error_response("这次什么也没写出来", status_code=502)
        return json_response({"ok": True, "diary": _diary_payload(date, diary)})

    return handler


def stats(plugin):
    async def handler(**kwargs):
        from astrbot.api.web import json_response

        items = plugin.store.all()
        dates = plugin.store.sorted_dates()
        today = plugin._today()
        logs_today = core.read_logs(plugin.root, today)
        words = sum(len(d.get("content", "")) for d in items.values())
        return json_response(
            {
                "count": len(items),
                "words": words,
                "first_date": dates[-1] if dates else "",
                "last_date": dates[0] if dates else "",
                "today": today,
                "has_today": today in items,
                "today_logs": len(logs_today),
                "diary_time": plugin.cfg.get("diary_time"),
                "enabled": bool(plugin.cfg.get("enabled", True)),
                "provider_id": plugin.cfg.get("provider_id") or "",
                "deliver_to": plugin.cfg.get("deliver_to") or [],
                "last_umo": plugin._state.get("last_umo") or "",
            }
        )

    return handler


# ---------------------------------------------------------------- 配置


def get_settings(plugin):
    async def handler(**kwargs):
        from astrbot.api.web import json_response

        data = core.schema_for_web()
        data["values"] = dict(plugin.cfg)
        try:
            data["providers"] = await _list_providers(plugin)
        except Exception:
            data["providers"] = []
        try:
            data["personas"] = await _list_personas(plugin)
        except Exception:
            data["personas"] = []
        data["last_umo"] = plugin._state.get("last_umo") or ""
        data["cwd_hint"] = ""
        return json_response(data)

    return handler


def save_settings(plugin):
    async def handler(**kwargs):
        from astrbot.api.web import error_response, json_response

        data = await _read_json(kwargs)
        if not isinstance(data, dict):
            return error_response("请求体格式不对", status_code=400)
        patch = data.get("values") if isinstance(data.get("values"), dict) else data
        patch = {k: v for k, v in patch.items() if k in core.CONF_SCHEMA}
        if not patch:
            return error_response("没有可保存的配置", status_code=400)
        try:
            values = plugin.apply_conf(patch)
        except Exception as e:
            logger.error(f"[diary] 保存配置失败: {e}")
            return error_response(f"保存失败：{e}", status_code=500)
        data["values"] = values
        data["last_umo"] = plugin._state.get("last_umo") or ""
        return json_response({"ok": True, "settings": data, "values": values})

    return handler


async def _list_providers(plugin) -> list[dict]:
    out = []
    try:
        providers = plugin.context.get_all_providers()
    except Exception:
        return out
    try:
        providers = await providers if hasattr(providers, "__await__") else providers
    except Exception:
        return out
    for p in providers or []:
        try:
            out.append({"id": str(p.meta().id), "name": str(p.meta().model or p.meta().id)})
        except Exception:
            continue
    return out


async def _list_personas(plugin) -> list[dict]:
    out = []
    pm = getattr(plugin.context, "persona_manager", None)
    if pm is None:
        return out
    try:
        personas = pm.get_all_personas()
        if asyncio_iscoro(personas):
            personas = await personas
    except Exception:
        return out
    for p in personas or []:
        try:
            out.append({"id": str(p.persona_id), "name": str(p.name or p.persona_id)})
        except Exception:
            continue
    return out


def asyncio_iscoro(obj: Any) -> bool:
    import asyncio

    return asyncio.iscoroutine(obj) or hasattr(obj, "__await__")


def export_diaries(plugin):
    async def handler(**kwargs):
        from astrbot.api.web import stream_response

        async def gen():
            yield "# 她的日记本\n\n"
            for k in plugin.store.sorted_dates():
                d = plugin.store.all()[k]
                yield f"## {k} · {d.get('title', '')}\n\n{d.get('content', '')}\n\n---\n\n"

        r = stream_response(gen())
        return await r if asyncio_iscoro(r) else r

    return handler

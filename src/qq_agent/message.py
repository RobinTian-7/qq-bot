"""OneBot 11 消息解析：转纯文本 + 抽链接（含 QQ 分享卡片 / 小程序）。"""
from __future__ import annotations

import json
import re
from html import unescape
from typing import Any

# 文本里的裸链接。排除 CJK 汉字、中日韩标点、全角字符 —— QQ 里链接后面常直接跟中文。
_STOP = "\\s<>\"'\u3000-\u303f\uff00-\uffef\u4e00-\u9fff\u3400-\u4dbf\\[\\]()"
URL_RE = re.compile(rf"https?://[^{_STOP}]+", re.IGNORECASE)
# 老师有时只发 www / mp 开头
BARE_WWW_RE = re.compile(
    rf"(?<![\w.@/])((?:www|mp)\.[\w.-]+\.[a-z]{{2,}}[^{_STOP}]*)", re.IGNORECASE
)

CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)(?:,([^\]]*))?\]")

_CQ_UNESCAPE = {"&amp;": "&", "&#91;": "[", "&#93;": "]", "&#44;": ","}

# 分享卡片 JSON 里可能藏链接的字段名
_URL_KEYS = {"jumpurl", "qqdocurl", "url", "weburl", "detail_url", "link", "pcjumpurl"}


def _cq_unescape(s: str) -> str:
    for k, v in _CQ_UNESCAPE.items():
        s = s.replace(k, v)
    return s


def _parse_cq_string(msg: str) -> list[dict[str, Any]]:
    """把 CQ 码字符串转成 segment 数组，兼容老协议端。"""
    segs: list[dict[str, Any]] = []
    pos = 0
    for m in CQ_RE.finditer(msg):
        if m.start() > pos:
            segs.append({"type": "text", "data": {"text": _cq_unescape(msg[pos:m.start()])}})
        data: dict[str, str] = {}
        for kv in (m.group(2) or "").split(","):
            if "=" in kv:
                k, _, v = kv.partition("=")
                data[k.strip()] = _cq_unescape(v)
        segs.append({"type": m.group(1), "data": data})
        pos = m.end()
    if pos < len(msg):
        segs.append({"type": "text", "data": {"text": _cq_unescape(msg[pos:])}})
    return segs


def normalize_segments(message: Any) -> list[dict[str, Any]]:
    """NapCat 默认给数组；字符串格式也兜住。"""
    if isinstance(message, list):
        return [s for s in message if isinstance(s, dict)]
    if isinstance(message, str):
        return _parse_cq_string(message)
    return []


def _walk_json_for_urls(node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and k.lower() in _URL_KEYS and v.startswith("http"):
                out.append(unescape(v))
            else:
                _walk_json_for_urls(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_json_for_urls(v, out)


def segments_to_text(segs: list[dict[str, Any]]) -> str:
    """转成给模型看的纯文本，非文本内容用占位符标出来。"""
    parts: list[str] = []
    for s in segs:
        t = s.get("type")
        d = s.get("data") or {}
        if t == "text":
            parts.append(str(d.get("text", "")))
        elif t == "at":
            qq = d.get("qq", "")
            parts.append("@全体成员" if qq == "all" else f"@{d.get('name') or qq}")
        elif t == "image":
            parts.append(f"［图片{'：' + d['summary'] if d.get('summary') else ''}］")
        elif t == "file":
            parts.append(f"［文件：{d.get('file') or d.get('name') or '未知'}］")
        elif t == "face":
            parts.append("［表情］")
        elif t == "reply":
            parts.append("［回复上一条］")
        elif t in ("json", "share"):
            title, desc = _card_title_desc(s)
            parts.append(f"［分享卡片：{title}{' — ' + desc if desc else ''}］")
        elif t == "forward":
            parts.append("［合并转发］")
        elif t in ("record", "video"):
            parts.append(f"［{'语音' if t == 'record' else '视频'}］")
    return "".join(parts).strip()


def _card_title_desc(seg: dict[str, Any]) -> tuple[str, str]:
    d = seg.get("data") or {}
    if seg.get("type") == "share":
        return str(d.get("title", "")), str(d.get("content", ""))
    raw = d.get("data")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return "", ""
    if not isinstance(raw, dict):
        return "", ""
    prompt = str(raw.get("prompt", "")).strip()
    meta = raw.get("meta")
    if isinstance(meta, dict):
        for v in meta.values():
            if isinstance(v, dict):
                title = str(v.get("title") or v.get("tag") or prompt)
                desc = str(v.get("desc") or v.get("summary") or "")
                return title, desc
    return prompt, ""


def extract_urls(segs: list[dict[str, Any]]) -> list[str]:
    """从 segment 里抽出所有链接，按出现顺序去重。"""
    found: list[str] = []
    for s in segs:
        t = s.get("type")
        d = s.get("data") or {}
        if t == "text":
            text = str(d.get("text", ""))
            found.extend(URL_RE.findall(text))
            found.extend("http://" + u for u in BARE_WWW_RE.findall(text))
        elif t == "share":
            if isinstance(d.get("url"), str) and d["url"].startswith("http"):
                found.append(d["url"])
        elif t == "json":
            raw = d.get("data")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except json.JSONDecodeError:
                    raw = None
            if raw is not None:
                _walk_json_for_urls(raw, found)

    seen: set[str] = set()
    out: list[str] = []
    for u in found:
        u = unescape(u).rstrip(".,;:'\"）】》")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def has_forward(segs: list[dict[str, Any]]) -> str | None:
    """返回合并转发的 id，需要再调 get_forward_msg 展开。"""
    for s in segs:
        if s.get("type") == "forward":
            d = s.get("data") or {}
            fid = d.get("id") or d.get("res_id")
            if fid:
                return str(fid)
    return None

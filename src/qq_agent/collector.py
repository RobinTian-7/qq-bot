"""常驻监听：把群消息实时落进 SQLite。"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .config import Config
from .db import Store
from .message import (
    extract_urls,
    has_forward,
    normalize_segments,
    segments_to_text,
)
from .onebot import OneBotClient, OneBotError

log = logging.getLogger("qq_agent.collector")


class Collector:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.client = OneBotClient(
            cfg.onebot.ws_url, cfg.onebot.access_token, cfg.onebot.reconnect_delay
        )
        # group_id -> {user_id: role}
        self._roles: dict[int, dict[int, str]] = {}
        self._names: dict[int, dict[int, str]] = {}

    # ---------- 老师判定 ----------

    async def refresh_members(self) -> None:
        for gid in self.cfg.group.group_ids:
            try:
                members = await self.client.get_group_member_list(gid)
            except OneBotError as e:
                log.warning("群 %s 成员列表拉取失败：%s", gid, e)
                continue
            self._roles[gid] = {int(m["user_id"]): m.get("role", "member") for m in members}
            self._names[gid] = {
                int(m["user_id"]): (m.get("card") or m.get("nickname") or "") for m in members
            }
            n_admin = sum(1 for r in self._roles[gid].values() if r in ("owner", "admin"))
            log.info("群 %s：%d 名成员，其中 %d 名群主/管理员", gid, len(members), n_admin)

    def is_teacher(self, group_id: int, user_id: int, display_name: str) -> tuple[bool, str]:
        g = self.cfg.group
        role = self._roles.get(group_id, {}).get(user_id, "member")
        if not g.filters_teachers:
            return True, role                      # 没设任何条件 = 全收
        if user_id in g.teacher_qqs:
            return True, role
        if g.include_admins and role in ("owner", "admin"):
            return True, role
        name = display_name or self._names.get(group_id, {}).get(user_id, "")
        if any(k and k in name for k in g.teacher_name_keywords):
            return True, role
        return False, role

    # ---------- 消息处理 ----------

    async def _expand_forward(self, segs: list[dict[str, Any]]) -> tuple[str, list[str]]:
        """合并转发展开成文本 + 链接。老师转发学校通知时全靠这个。"""
        fid = has_forward(segs)
        if not fid:
            return "", []
        try:
            nodes = await self.client.get_forward_msg(fid)
        except OneBotError as e:
            log.debug("展开合并转发 %s 失败：%s", fid, e)
            return "", []
        lines: list[str] = []
        urls: list[str] = []
        for node in nodes:
            content = node.get("message") or node.get("content") or []
            sub = normalize_segments(content)
            sender = node.get("sender") or {}
            who = sender.get("card") or sender.get("nickname") or node.get("name") or ""
            body = segments_to_text(sub)
            if body:
                lines.append(f"  · {who}：{body}" if who else f"  · {body}")
            urls.extend(extract_urls(sub))
        return ("\n【转发内容】\n" + "\n".join(lines)) if lines else "", urls

    async def handle_event(self, ev: dict) -> None:
        if ev.get("post_type") != "message" or ev.get("message_type") != "group":
            return
        gid = int(ev.get("group_id", 0))
        if gid not in self.cfg.group.group_ids:
            return

        mid = int(ev.get("message_id", 0))
        uid = int(ev.get("user_id", 0))
        sender = ev.get("sender") or {}
        name = sender.get("card") or sender.get("nickname") or str(uid)

        segs = normalize_segments(ev.get("message"))
        text = segments_to_text(segs)
        urls = extract_urls(segs)

        fwd_text, fwd_urls = await self._expand_forward(segs)
        if fwd_text:
            text = (text + fwd_text).strip()
            urls = list(dict.fromkeys(urls + fwd_urls))

        teacher, role = self.is_teacher(gid, uid, name)
        if sender.get("role"):
            role = sender["role"]

        self.store.save_message({
            "message_id": mid,
            "group_id": gid,
            "user_id": uid,
            "sender_name": name,
            "sender_role": role,
            "is_teacher": teacher,
            "ts": int(ev.get("time") or time.time()),
            "text": text,
            "raw": segs,
            "urls": urls,
        })
        if teacher:
            preview = text[:60].replace("\n", " ")
            log.info("[%s] %s：%s%s", gid, name, preview, "…" if len(text) > 60 else "")

    # ---------- 主循环 ----------

    async def run_forever(self) -> None:
        delay = self.cfg.onebot.reconnect_delay
        while True:
            try:
                await self.client.connect()
                me = await self.client.get_login_info()
                log.info("Bot 已登录：%s(%s)", me.get("nickname"), me.get("user_id"))
                await self.refresh_members()
                async for ev in self.client.events():
                    if ev.get("post_type") == "_internal":
                        raise OneBotError("连接断开，准备重连")
                    try:
                        await self.handle_event(ev)
                    except Exception:
                        log.exception("处理事件出错，已跳过")
            except asyncio.CancelledError:
                await self.client.close()
                raise
            except Exception as e:
                log.warning("监听中断（%s），%d 秒后重连…", e, delay)
                await self.client.close()
                await asyncio.sleep(delay)

    # ---------- 补抓 ----------

    async def backfill(self, count: int = 200) -> int:
        """从协议端拉最近的历史消息补库（Bot 掉线期间的洞）。"""
        saved = 0
        await self.client.connect()
        try:
            await self.refresh_members()
            for gid in self.cfg.group.group_ids:
                seq = 0
                remaining = count
                while remaining > 0:
                    batch = await self.client.get_group_msg_history(
                        gid, message_seq=seq, count=min(50, remaining)
                    )
                    if not batch:
                        break
                    for ev in batch:
                        ev.setdefault("post_type", "message")
                        ev.setdefault("message_type", "group")
                        ev.setdefault("group_id", gid)
                        mid = int(ev.get("message_id", 0))
                        if mid and self.store.has_message(mid):
                            continue
                        await self.handle_event(ev)
                        saved += 1
                    remaining -= len(batch)
                    seq = int(batch[0].get("message_seq") or batch[0].get("message_id") or 0)
                    if not seq:
                        break
        finally:
            await self.client.close()
        return saved

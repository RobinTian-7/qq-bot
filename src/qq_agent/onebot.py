"""OneBot 11 正向 WebSocket 客户端（NapCat / Lagrange / go-cqhttp 通用）。

一条连接同时承载事件推送和 action 响应，用 echo 字段把响应配回请求。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator

import websockets

log = logging.getLogger("qq_agent.onebot")


class OneBotError(RuntimeError):
    pass


class OneBotClient:
    def __init__(self, ws_url: str, access_token: str = "", reconnect_delay: int = 5):
        self.ws_url = ws_url
        self.access_token = access_token
        self.reconnect_delay = reconnect_delay
        self._ws: websockets.ClientConnection | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._events: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
        self._reader: asyncio.Task | None = None

    # ---------- 连接 ----------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}

    async def connect(self) -> None:
        self._ws = await websockets.connect(
            self.ws_url,
            additional_headers=self._headers(),
            ping_interval=20,
            ping_timeout=20,
            max_size=32 * 1024 * 1024,   # 合并转发可能很大
            open_timeout=15,
        )
        self._reader = asyncio.create_task(self._read_loop())
        log.info("已连接协议端 %s", self.ws_url)

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws:
            await self._ws.close()

    async def _read_loop(self) -> None:
        assert self._ws
        try:
            async for raw in self._ws:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                echo = payload.get("echo")
                if echo and echo in self._pending:
                    fut = self._pending.pop(echo)
                    if not fut.done():
                        fut.set_result(payload)
                elif payload.get("post_type"):
                    try:
                        self._events.put_nowait(payload)
                    except asyncio.QueueFull:
                        log.warning("事件队列已满，丢弃一条事件")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # 连接断开
            log.warning("协议端连接断开：%s", e)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(OneBotError("连接已断开"))
            self._pending.clear()
            await self._events.put({"post_type": "_internal", "kind": "disconnected"})

    # ---------- API ----------

    async def call(self, action: str, params: dict | None = None, timeout: float = 30) -> Any:
        if not self._ws:
            raise OneBotError("尚未连接")
        echo = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        await self._ws.send(json.dumps(
            {"action": action, "params": params or {}, "echo": echo}, ensure_ascii=False
        ))
        try:
            resp = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            raise OneBotError(f"调用 {action} 超时")
        if resp.get("status") == "failed" or resp.get("retcode") not in (0, 1):
            raise OneBotError(f"{action} 失败：{resp.get('message') or resp}")
        return resp.get("data")

    async def events(self) -> AsyncIterator[dict]:
        while True:
            yield await self._events.get()

    # ---------- 常用封装 ----------

    async def get_login_info(self) -> dict:
        return await self.call("get_login_info")

    async def get_group_member_list(self, group_id: int) -> list[dict]:
        return await self.call("get_group_member_list", {"group_id": group_id}) or []

    async def get_forward_msg(self, msg_id: str) -> list[dict]:
        data = await self.call("get_forward_msg", {"id": msg_id, "message_id": msg_id})
        if isinstance(data, dict):
            return data.get("messages") or data.get("message") or []
        return data or []

    async def get_group_msg_history(
        self, group_id: int, message_seq: int = 0, count: int = 50
    ) -> list[dict]:
        """补抓历史消息。Bot 掉线后恢复数据用。"""
        params: dict[str, Any] = {"group_id": group_id, "count": count}
        if message_seq:
            params["message_seq"] = message_seq
        data = await self.call("get_group_msg_history", params)
        if isinstance(data, dict):
            return data.get("messages") or []
        return data or []

    async def send_private_msg(self, user_id: int, text: str) -> Any:
        return await self.call("send_private_msg", {"user_id": user_id, "message": text})

"""Webex integration — listener (polls for messages) and notifier (sends messages)."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

import httpx

logger = logging.getLogger(__name__)

WEBEX_API = "https://webexapis.com/v1"


class WebexNotifier:
    def __init__(self, bot_token: str, room_id: str) -> None:
        self.bot_token = bot_token
        self.room_id = room_id

    async def send(self, markdown: str) -> None:
        if not self.bot_token or not self.room_id:
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{WEBEX_API}/messages",
                headers={"Authorization": f"Bearer {self.bot_token}", "Content-Type": "application/json"},
                json={"roomId": self.room_id, "markdown": markdown},
            )
            if resp.status_code != 200:
                logger.warning(f"Webex send failed ({resp.status_code}): {resp.text[:200]}")


class WebexListener:
    def __init__(
        self,
        bot_token: str,
        room_id: str,
        bot_id: str,
        on_message: Optional[Callable] = None,
        poll_interval: float = 3.0,
    ) -> None:
        self.bot_token = bot_token
        self.room_id = room_id
        self.bot_id = bot_id
        self.on_message = on_message
        self.poll_interval = poll_interval
        self._running = False
        self._last_message_id: Optional[str] = None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.bot_token}"}

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{WEBEX_API}/messages",
                headers=self._headers(),
                params={"roomId": self.room_id, "max": 1},
            )
            if resp.status_code == 200:
                items = resp.json().get("items", [])
                if items:
                    self._last_message_id = items[0]["id"]
        logger.info("Webex listener started")
        asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._check_messages()
            except Exception as e:
                logger.warning(f"Webex poll error: {e}")
            await asyncio.sleep(self.poll_interval)

    async def _check_messages(self) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{WEBEX_API}/messages",
                headers=self._headers(),
                params={"roomId": self.room_id, "max": 5},
            )
            if resp.status_code != 200:
                return
            messages = resp.json().get("items", [])

        new_messages = []
        for msg in messages:
            if msg["id"] == self._last_message_id:
                break
            if msg.get("personId") == self.bot_id:
                continue
            new_messages.append(msg)

        if not new_messages:
            return
        self._last_message_id = messages[0]["id"]

        for msg in reversed(new_messages):
            await self._handle_message(msg)

    async def _handle_message(self, msg: dict) -> None:
        text = (msg.get("text") or "").strip()
        files = msg.get("files") or []
        sender = msg.get("personEmail", "someone")
        logger.info(f"Webex from {sender}: {text[:60]} (files: {len(files)})")

        image_bytes = None
        if files:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(files[0], headers=self._headers())
                if resp.status_code == 200:
                    image_bytes = resp.content

        if self.on_message:
            await self.on_message(text=text, image_bytes=image_bytes)

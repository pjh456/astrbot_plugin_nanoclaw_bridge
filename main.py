import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


DEFAULT_INBOUND_URL = "http://127.0.0.1:7801/astrbot/inbound"


def _to_iso(ts: Optional[int]) -> str:
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    # AstrBot timestamps are usually seconds; handle ms just in case.
    if ts > 10_000_000_000:
        ts = int(ts / 1000)
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _derive_control_url(inbound_url: str) -> str:
    if inbound_url.endswith("/astrbot/inbound"):
        return inbound_url[: -len("/astrbot/inbound")] + "/astrbot/control"
    return inbound_url.rstrip("/") + "/astrbot/control"


@register(
    "nanoclaw_bridge",
    "pjh456",
    "Forward AstrBot messages to NanoClaw",
    "0.1.3",
)
class NanoClawBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        cfg = config

        self.inbound_url: str = cfg.get("nanoclaw_inbound_url", DEFAULT_INBOUND_URL)
        raw_control_url = cfg.get("nanoclaw_control_url", "")
        self.control_url: str = (
            raw_control_url.strip()
            if isinstance(raw_control_url, str) and raw_control_url.strip()
            else _derive_control_url(self.inbound_url)
        )
        self.token: str = cfg.get("nanoclaw_token", "")
        self.forward_mode: str = cfg.get("forward_mode", "all")
        self.command_prefix: str = cfg.get("command_prefix", "/nc ")
        self.block_astrbot_on_forward: bool = bool(
            cfg.get("block_astrbot_on_forward", True)
        )
        self.ignore_self: bool = bool(cfg.get("ignore_self", True))
        self.timeout_ms: int = int(cfg.get("timeout_ms", 15000))

        # Avoid container-level proxy envs breaking local calls
        self._client = httpx.AsyncClient(
            timeout=self.timeout_ms / 1000, trust_env=False
        )
        logger.info(
            f"NanoClaw bridge config inbound={self.inbound_url} control={self.control_url}"
        )

    async def terminate(self):
        await self._client.aclose()

    def _is_self_message(self, event: AstrMessageEvent, sender_id: str) -> bool:
        try:
            self_id = getattr(event.message_obj, "self_id", None)
        except Exception:
            self_id = None
        if self_id is None:
            return False
        return str(self_id) == str(sender_id)

    def _should_forward(self, event: AstrMessageEvent, content: str) -> bool:
        if not content:
            return False
        if content.startswith("/nc_main") or content.startswith("/nc_use"):
            return False
        if content.startswith("/nc main") or content.startswith("/nc use"):
            return False
        mode = self.forward_mode.lower().strip()
        if mode == "all":
            return True
        if mode == "command":
            return content.startswith(self.command_prefix)
        if mode == "mention":
            is_at = getattr(event, "is_at_or_wake_command", False)
            return bool(is_at)
        return True

    async def _post(self, payload: Dict[str, Any]) -> None:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = await self._client.post(
                self.inbound_url, json=payload, headers=headers
            )
            if resp.status_code >= 300:
                logger.warning(
                    f"NanoClaw inbound failed {resp.status_code}: {resp.text}"
                )
        except Exception as exc:
            logger.warning(f"NanoClaw inbound error ({self.inbound_url}): {exc!r}")

    async def _post_control(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = await self._client.post(
                self.control_url, json=payload, headers=headers
            )
            if resp.status_code >= 300:
                logger.warning(
                    f"NanoClaw control failed {resp.status_code}: {resp.text}"
                )
                return None
            try:
                return resp.json()
            except Exception:
                return None
        except Exception as exc:
            logger.warning(f"NanoClaw control error ({self.control_url}): {exc!r}")
            return None

    @filter.command("nc_main")
    async def cmd_set_main(self, event: AstrMessageEvent):
        await self._handle_set_main(event)
        yield event.plain_result("已将当前会话设置为 NanoClaw 主控。")

    @filter.command("nc_use")
    async def cmd_set_main_alias(self, event: AstrMessageEvent):
        await self._handle_set_main(event)
        yield event.plain_result("已将当前会话设置为 NanoClaw 主控。")

    async def _handle_set_main(self, event: AstrMessageEvent) -> None:
        try:
            msg_obj = event.message_obj
            session_id = getattr(msg_obj, "session_id", None)
        except Exception:
            session_id = None

        try:
            umo = event.unified_msg_origin
        except Exception:
            umo = None

        sender_name = ""
        try:
            sender_name = event.get_sender_name() or ""
        except Exception:
            sender_name = ""

        payload: Dict[str, Any] = {
            "action": "set_main",
            "chat_id": str(umo or session_id or "unknown"),
            "umo": str(umo) if umo is not None else None,
            "group_name": sender_name or None,
            "sender_name": sender_name or None,
        }
        await self._post_control(payload)

    @filter.command("nc_status")
    async def cmd_status(self, event: AstrMessageEvent):
        payload: Dict[str, Any] = {"action": "status", "chat_id": "status"}
        data = await self._post_control(payload)
        if not data or not data.get("ok"):
            yield event.plain_result("NanoClaw 状态获取失败。")
            return
        main = data.get("main")
        if not main:
            yield event.plain_result("NanoClaw 尚未设置主控会话。")
            return
        msg = f"当前主控: {main.get('name')} ({main.get('jid')})"
        yield event.plain_result(msg)

    @filter.command("nc_ping")
    async def cmd_ping(self, event: AstrMessageEvent):
        start = asyncio.get_event_loop().time()
        payload: Dict[str, Any] = {"action": "status", "chat_id": "ping"}
        data = await self._post_control(payload)
        elapsed_ms = int((asyncio.get_event_loop().time() - start) * 1000)
        if not data or not data.get("ok"):
            yield event.plain_result(f"NanoClaw ping 失败（{elapsed_ms}ms）")
            return
        main = data.get("main")
        if not main:
            yield event.plain_result(f"NanoClaw ping OK（{elapsed_ms}ms），未设置主控")
            return
        msg = f"NanoClaw ping OK（{elapsed_ms}ms），主控: {main.get('name')} ({main.get('jid')})"
        yield event.plain_result(msg)

    @filter.command("nc_reset")
    async def cmd_reset(self, event: AstrMessageEvent):
        try:
            msg_obj = event.message_obj
            session_id = getattr(msg_obj, "session_id", None)
        except Exception:
            session_id = None

        try:
            umo = event.unified_msg_origin
        except Exception:
            umo = None

        payload: Dict[str, Any] = {
            "action": "reset_session",
            "chat_id": str(umo or session_id or "unknown"),
            "umo": str(umo) if umo is not None else None,
        }
        data = await self._post_control(payload)
        if not data or not data.get("ok"):
            yield event.plain_result("NanoClaw 会话重置失败。")
            return
        yield event.plain_result("NanoClaw 会话已重置。")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        content = event.message_str or ""
        is_nc_command = content.startswith(self.command_prefix)
        if not self._should_forward(event, content):
            return
        if self.block_astrbot_on_forward:
            # Prevent AstrBot default response whenever forwarding is triggered
            try:
                event.stop_event()
            except Exception:
                pass

        sender_name = ""
        sender_id = ""
        try:
            sender_name = event.get_sender_name() or ""
        except Exception:
            sender_name = ""
        try:
            sender_id = event.get_sender_id() or ""
        except Exception:
            sender_id = ""

        if self.ignore_self and sender_id and self._is_self_message(event, sender_id):
            return

        try:
            msg_obj = event.message_obj
            session_id = getattr(msg_obj, "session_id", None)
            message_id = getattr(msg_obj, "message_id", None)
            timestamp = getattr(msg_obj, "timestamp", None)
            group_id = getattr(msg_obj, "group_id", None)
        except Exception:
            session_id = None
            message_id = None
            timestamp = None
            group_id = None

        try:
            umo = event.unified_msg_origin
        except Exception:
            umo = None

        if is_nc_command:
            content = content[len(self.command_prefix) :].lstrip()
        payload: Dict[str, Any] = {
            "chat_id": str(umo or session_id or "unknown"),
            "umo": str(umo) if umo is not None else None,
            "sender_id": sender_id,
            "sender_name": sender_name or sender_id or "unknown",
            "content": content,
            "timestamp": _to_iso(timestamp),
            "is_group": bool(group_id),
            "message_id": str(message_id) if message_id is not None else None,
        }

        await self._post(payload)

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from quart import jsonify, request

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import At, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter


DEFAULT_INBOUND_URL = "http://127.0.0.1:7801/astrbot/inbound"
OUTBOUND_ROUTE = "/nanoclaw_bridge/outbound"
PENDING_EVENT_TTL_SECONDS = 30 * 60


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


def _pick_first(*values: Any) -> str:
    for value in values:
        s = _to_str(value).strip()
        if s:
            return s
    return ""


def _get_attr(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _extract_sender_fields(event: AstrMessageEvent) -> Dict[str, str]:
    sender_id = ""
    sender_name = ""
    sender_nickname = ""
    sender_username = ""
    sender_card = ""

    try:
        sender_id = event.get_sender_id() or ""
    except Exception:
        sender_id = ""
    try:
        sender_name = event.get_sender_name() or ""
    except Exception:
        sender_name = ""

    try:
        sender_obj = getattr(event.message_obj, "sender", None)
    except Exception:
        sender_obj = None

    sender_id = _pick_first(
        sender_id,
        _get_attr(sender_obj, "user_id"),
        _get_attr(sender_obj, "id"),
    )
    sender_nickname = _pick_first(
        sender_name,
        _get_attr(sender_obj, "nickname"),
        _get_attr(sender_obj, "nick"),
        _get_attr(sender_obj, "display_name"),
    )
    sender_username = _pick_first(
        _get_attr(sender_obj, "username"),
        _get_attr(sender_obj, "user_name"),
        _get_attr(sender_obj, "name"),
    )
    sender_card = _pick_first(
        _get_attr(sender_obj, "card"),
    )

    # Try raw message fallback (common across adapters)
    try:
        raw = getattr(event.message_obj, "raw_message", None)
    except Exception:
        raw = None

    for root in (raw, _get_attr(raw, "sender"), _get_attr(raw, "author")):
        if not root:
            continue
        sender_id = _pick_first(sender_id, _get_attr(root, "user_id"), _get_attr(root, "id"))
        sender_nickname = _pick_first(
            sender_nickname,
            _get_attr(root, "card"),
            _get_attr(root, "nickname"),
            _get_attr(root, "nick"),
            _get_attr(root, "display_name"),
        )
        sender_username = _pick_first(
            sender_username,
            _get_attr(root, "username"),
            _get_attr(root, "user_name"),
            _get_attr(root, "name"),
        )

    sender_display = _pick_first(
        sender_nickname,
        sender_username,
        sender_card,
        sender_id,
    )

    return {
        "sender_id": sender_id,
        "sender_name": sender_display,
        "sender_nickname": sender_nickname,
        "sender_username": sender_username,
        "sender_card": sender_card,
    }


def _extract_group_fields(event: AstrMessageEvent) -> Dict[str, str]:
    group_id = ""
    group_name = ""

    try:
        msg_obj = event.message_obj
    except Exception:
        msg_obj = None

    try:
        raw = getattr(event.message_obj, "raw_message", None)
    except Exception:
        raw = None

    group_id = _pick_first(
        _get_attr(msg_obj, "group_id"),
        _get_attr(_get_attr(msg_obj, "group"), "group_id"),
        _get_attr(_get_attr(msg_obj, "group"), "id"),
        _get_attr(raw, "group_id"),
        _get_attr(raw, "guild_id"),
        _get_attr(raw, "channel_id"),
    )
    group_name = _pick_first(
        _get_attr(_get_attr(msg_obj, "group"), "group_name"),
    )
    group_name = _pick_first(
        group_name,
        _get_attr(raw, "group_name"),
        _get_attr(raw, "guild_name"),
        _get_attr(raw, "channel_name"),
        _get_attr(raw, "name"),
        _get_attr(raw, "title"),
    )

    return {
        "group_id": group_id,
        "group_name": group_name,
    }


def _extract_sender_permissions(event: AstrMessageEvent) -> Dict[str, Any]:
    permissions: Dict[str, Any] = {}

    astrbot_role = _to_str(getattr(event, "role", "")).strip().lower()
    if astrbot_role:
        permissions["astrbot_role"] = astrbot_role

    try:
        is_astrbot_admin = bool(event.is_admin())
    except Exception:
        is_astrbot_admin = astrbot_role == "admin"
    if is_astrbot_admin:
        permissions["is_astrbot_admin"] = True

    try:
        sender_obj = getattr(event.message_obj, "sender", None)
    except Exception:
        sender_obj = None
    try:
        raw = getattr(event.message_obj, "raw_message", None)
    except Exception:
        raw = None

    platform_role = ""
    platform_title = ""
    for root in (sender_obj, _get_attr(raw, "sender"), _get_attr(raw, "author")):
        if not root:
            continue
        platform_role = _pick_first(
            platform_role,
            _get_attr(root, "role"),
            _get_attr(root, "sender_role"),
            _get_attr(root, "member_role"),
        )
        platform_title = _pick_first(
            platform_title,
            _get_attr(root, "title"),
            _get_attr(root, "role_name"),
            _get_attr(root, "honor"),
        )

    normalized_platform_role = platform_role.strip().lower()
    if normalized_platform_role:
        permissions["platform_role"] = normalized_platform_role
    elif platform_role:
        permissions["platform_role"] = platform_role
    if platform_title:
        permissions["platform_title"] = platform_title

    owner_roles = {"owner", "group_owner", "creator", "群主"}
    admin_roles = {"admin", "administrator", "管理员"}
    raw_role = normalized_platform_role or platform_role
    if raw_role in owner_roles:
        permissions["is_platform_owner"] = True
        permissions["is_platform_admin"] = True
    elif raw_role in admin_roles:
        permissions["is_platform_admin"] = True

    return permissions


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


def _derive_health_url(inbound_url: str) -> str:
    if inbound_url.endswith("/astrbot/inbound"):
        return inbound_url[: -len("/astrbot/inbound")] + "/healthz"
    return inbound_url.rstrip("/") + "/healthz"


def _bool_label(value: Any) -> str:
    return "已配置" if bool(value) else "未配置"


def _format_diag_message(
    health_ok: bool,
    health_elapsed_ms: int,
    diag_elapsed_ms: int,
    data: Optional[Dict[str, Any]],
    *,
    inbound_url: str,
    control_url: str,
    forward_mode: str,
    command_prefix: str,
    block_astrbot_on_forward: bool,
    ignore_self: bool,
) -> str:
    lines = ["NanoClaw 自检"]
    health_line = (
        f"健康检查: OK（{health_elapsed_ms}ms）"
        if health_ok
        else f"健康检查: 失败（{health_elapsed_ms}ms）"
    )
    lines.append(health_line)

    if not data or not data.get("ok"):
        lines.append(f"控制接口: 失败（{diag_elapsed_ms}ms）")
        lines.append(f"inbound: {inbound_url}")
        lines.append(f"control: {control_url}")
        lines.append(
            "插件配置: "
            f"forward_mode={forward_mode}, "
            f"command_prefix={command_prefix!r}, "
            f"block_forward={str(block_astrbot_on_forward).lower()}, "
            f"ignore_self={str(ignore_self).lower()}"
        )
        return "\n".join(lines)

    lines.append(f"控制接口: OK（{diag_elapsed_ms}ms）")

    main = data.get("main")
    if isinstance(main, dict) and main.get("jid"):
        lines.append(f"主控会话: {main.get('name')} ({main.get('jid')})")
    else:
        lines.append("主控会话: 未设置")

    diag = data.get("diag") if isinstance(data.get("diag"), dict) else {}
    channel = diag.get("channel") if isinstance(diag.get("channel"), dict) else {}
    openapi = diag.get("openapi") if isinstance(diag.get("openapi"), dict) else {}
    sessions = diag.get("sessions") if isinstance(diag.get("sessions"), dict) else {}
    model = diag.get("model") if isinstance(diag.get("model"), dict) else {}

    lines.append(
        "NanoClaw HTTP: "
        f"{channel.get('listenHost', '?')}:{channel.get('listenPort', '?')}, "
        f"token {_bool_label(channel.get('tokenConfigured'))}"
    )
    lines.append(
        "OpenAPI 回退: "
        f"{openapi.get('apiBase', '?')}, "
        f"key {_bool_label(openapi.get('apiKeyConfigured'))}"
    )
    lines.append(
        "模型: "
        f"{_to_str(model.get('model')).strip() or '未设置'} "
        f"@ {_to_str(model.get('anthropicBaseUrl')).strip() or '默认'}"
    )
    lines.append(
        "鉴权: "
        f"{_to_str(model.get('authMode')).strip() or 'unknown'} "
        f"(api_key={_bool_label(model.get('apiKeyConfigured'))}, "
        f"oauth={_bool_label(model.get('oauthConfigured'))})"
    )
    lines.append(
        f"已注册会话: {_to_str(sessions.get('registeredCount')).strip() or '0'}"
    )
    lines.append(
        "插件配置: "
        f"forward_mode={forward_mode}, "
        f"command_prefix={command_prefix!r}, "
        f"block_forward={str(block_astrbot_on_forward).lower()}, "
        f"ignore_self={str(ignore_self).lower()}"
    )
    lines.append(f"inbound: {inbound_url}")
    lines.append(f"control: {control_url}")
    return "\n".join(lines)


def _normalize_value(value: Any, depth: int = 0) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return repr(value)
    if depth >= 3:
        return _to_str(value)
    if isinstance(value, (list, tuple, set)):
        items = []
        for item in value:
            normalized = _normalize_value(item, depth + 1)
            if normalized not in (None, "", [], {}):
                items.append(normalized)
        return items
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for k, v in value.items():
            key = _to_str(k).strip()
            if not key or key.startswith("_"):
                continue
            normalized = _normalize_value(v, depth + 1)
            if normalized not in (None, "", [], {}):
                result[key] = normalized
        return result

    result: Dict[str, Any] = {}
    for key in (
        "type",
        "id",
        "message_id",
        "user_id",
        "group_id",
        "nickname",
        "username",
        "card",
        "name",
        "title",
        "text",
        "content",
        "url",
        "file",
        "path",
        "image",
        "data",
        "message",
        "messages",
        "message_chain",
        "time",
        "timestamp",
    ):
        try:
            raw = getattr(value, key, None)
        except Exception:
            raw = None
        normalized = _normalize_value(raw, depth + 1)
        if normalized not in (None, "", [], {}):
            result[key] = normalized
    return result or _to_str(value)


def _extract_segments_from_value(candidate: Any) -> List[Dict[str, Any]]:
    roots = [
        candidate,
        _get_attr(candidate, "segments"),
        _get_attr(candidate, "segment"),
        _get_attr(candidate, "chain"),
        _get_attr(candidate, "elements"),
        _get_attr(candidate, "message"),
        _get_attr(candidate, "messages"),
        _get_attr(candidate, "message_chain"),
        _get_attr(_get_attr(candidate, "raw_message"), "segments"),
        _get_attr(_get_attr(candidate, "raw_message"), "segment"),
        _get_attr(_get_attr(candidate, "raw_message"), "chain"),
        _get_attr(_get_attr(candidate, "raw_message"), "elements"),
        _get_attr(_get_attr(candidate, "raw_message"), "message"),
        _get_attr(_get_attr(candidate, "raw_message"), "messages"),
        _get_attr(_get_attr(candidate, "raw_message"), "message_chain"),
    ]

    for root in roots:
        if _looks_like_segment(root):
            return [_normalize_segment(root)]
        chain = _iter_message_chain(root)
        if chain:
            return [_normalize_segment(item) for item in chain]
    return []


def _iter_message_chain(message: Any) -> List[Any]:
    if message is None:
        return []
    if isinstance(message, dict):
        return []
    if isinstance(message, (list, tuple)):
        return list(message)
    try:
        return list(message)
    except Exception:
        return []


def _looks_like_segment(value: Any) -> bool:
    if value is None:
        return False
    for key in (
        "type",
        "component_type",
        "text",
        "content",
        "url",
        "image",
        "file",
        "path",
        "message_id",
        "id",
        "data",
    ):
        candidate = _get_attr(value, key)
        if candidate not in (None, "", [], {}):
            return True
    return False


def _normalize_segment(segment: Any) -> Dict[str, Any]:
    segment_type = _pick_first(
        _get_attr(segment, "type"),
        _get_attr(segment, "component_type"),
        getattr(getattr(segment, "__class__", None), "__name__", ""),
    ).lower() or "unknown"

    result: Dict[str, Any] = {"type": segment_type}
    for key in (
        "id",
        "message_id",
        "user_id",
        "qq",
        "name",
        "title",
        "text",
        "content",
        "url",
        "file",
        "path",
        "image",
        "voice",
        "video",
        "emoji_id",
        "face_id",
        "code",
        "data",
    ):
        normalized = _normalize_value(_get_attr(segment, key))
        if normalized not in (None, "", [], {}):
            result[key] = normalized

    if len(result) == 1:
        normalized = _normalize_value(segment)
        if isinstance(normalized, dict):
            for key, value in normalized.items():
                if key == "type":
                    continue
                result[key] = value
    return result


def _extract_message_segments(event: AstrMessageEvent) -> List[Dict[str, Any]]:
    try:
        msg_obj = event.message_obj
    except Exception:
        msg_obj = None

    roots = [
        _get_attr(msg_obj, "segments"),
        _get_attr(msg_obj, "segment"),
        _get_attr(msg_obj, "chain"),
        _get_attr(msg_obj, "elements"),
        _get_attr(msg_obj, "message"),
        _get_attr(_get_attr(msg_obj, "raw_message"), "segments"),
        _get_attr(_get_attr(msg_obj, "raw_message"), "segment"),
        _get_attr(_get_attr(msg_obj, "raw_message"), "chain"),
        _get_attr(_get_attr(msg_obj, "raw_message"), "elements"),
        _get_attr(_get_attr(msg_obj, "raw_message"), "message"),
        _get_attr(_get_attr(msg_obj, "raw_message"), "messages"),
        _get_attr(_get_attr(msg_obj, "raw_message"), "message_chain"),
    ]

    for root in roots:
        if _looks_like_segment(root):
            return [_normalize_segment(root)]
        chain = _iter_message_chain(root)
        if chain:
            return [_normalize_segment(item) for item in chain]
    return []


def _extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        parts = [_extract_text(item) for item in value]
        return " ".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        if isinstance(value.get("text"), str) and value.get("text"):
            return value["text"]
        if isinstance(value.get("content"), str) and value.get("content"):
            return value["content"]
        if isinstance(value.get("message"), (str, list, tuple, dict)):
            return _extract_text(value.get("message"))
        return ""

    for key in ("text", "content", "message", "raw_message"):
        extracted = _extract_text(_get_attr(value, key))
        if extracted:
            return extracted
    return ""


def _summarize_reply(candidate: Any) -> Optional[Dict[str, Any]]:
    if candidate is None:
        return None

    sender = _get_attr(candidate, "sender") or _get_attr(candidate, "author")
    message_id = _pick_first(
        _get_attr(candidate, "message_id"),
        _get_attr(candidate, "id"),
        _get_attr(candidate, "message_seq"),
        _get_attr(candidate, "msg_id"),
    )
    sender_id = _pick_first(
        _get_attr(candidate, "sender_id"),
        _get_attr(candidate, "user_id"),
        _get_attr(sender, "user_id"),
        _get_attr(sender, "id"),
    )
    sender_name = _pick_first(
        _get_attr(candidate, "sender_name"),
        _get_attr(candidate, "nickname"),
        _get_attr(sender, "card"),
        _get_attr(sender, "nickname"),
        _get_attr(sender, "name"),
        sender_id,
    )
    content = _extract_text(candidate)
    timestamp_raw = (
        _get_attr(candidate, "timestamp")
        or _get_attr(candidate, "time")
        or _get_attr(candidate, "time_seconds")
    )

    reply: Dict[str, Any] = {}
    if message_id:
        reply["message_id"] = message_id
    if sender_id:
        reply["sender_id"] = sender_id
    if sender_name:
        reply["sender_name"] = sender_name
    if content:
        reply["content"] = content
    if isinstance(timestamp_raw, (int, float)):
        reply["timestamp"] = _to_iso(int(timestamp_raw))
    elif timestamp_raw:
        reply["timestamp"] = _to_str(timestamp_raw)

    segments = _extract_segments_from_value(candidate)
    if segments:
        reply["segments"] = segments

    normalized = _normalize_value(candidate)
    if isinstance(normalized, dict):
        compact = {
            key: value
            for key, value in normalized.items()
            if key
            not in {
                "text",
                "content",
                "message",
                "messages",
                "message_chain",
                "raw_message",
                "segments",
            }
        }
        if compact:
            reply["raw"] = compact

    return reply or None


def _extract_reply(event: AstrMessageEvent, segments: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    try:
        msg_obj = event.message_obj
    except Exception:
        msg_obj = None

    raw = _get_attr(msg_obj, "raw_message")
    candidates = [
        _get_attr(msg_obj, "reply"),
        _get_attr(msg_obj, "quote"),
        _get_attr(msg_obj, "reply_to"),
        _get_attr(msg_obj, "quoted_message"),
        _get_attr(msg_obj, "reply_message"),
        _get_attr(msg_obj, "message_reference"),
        _get_attr(raw, "reply"),
        _get_attr(raw, "quote"),
        _get_attr(raw, "reply_to"),
        _get_attr(raw, "quoted_message"),
        _get_attr(raw, "reply_message"),
        _get_attr(raw, "message_reference"),
        _get_attr(raw, "source_message"),
    ]

    for candidate in candidates:
        reply = _summarize_reply(candidate)
        if reply:
            return reply

    for segment in segments:
        if segment.get("type") not in {"reply", "quote", "reference", "source"}:
            continue
        reply = _summarize_reply(segment)
        if reply:
            return reply
    return None


def _build_metadata(
    event: AstrMessageEvent,
    sender_fields: Dict[str, str],
    sender_permissions: Dict[str, Any],
    group_fields: Dict[str, str],
    segments: List[Dict[str, Any]],
    reply: Optional[Dict[str, Any]],
    platform_name: str,
    platform_id: str,
    session_id: Any,
    umo: Any,
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "source": "astrbot",
        "chat_id": str(umo or session_id or "unknown"),
        "is_group": bool(group_fields.get("group_id")),
    }
    if umo is not None:
        metadata["umo"] = str(umo)
    if platform_name:
        metadata["platform_name"] = platform_name
    if platform_id:
        metadata["platform_id"] = platform_id
    if session_id is not None:
        metadata["session_id"] = str(session_id)
    if group_fields.get("group_id"):
        metadata["group_id"] = group_fields["group_id"]
    if group_fields.get("group_name"):
        metadata["group_name"] = group_fields["group_name"]

    sender_profile = {
        "nickname": sender_fields.get("sender_nickname") or None,
        "username": sender_fields.get("sender_username") or None,
        "card": sender_fields.get("sender_card") or None,
    }
    sender_profile = {k: v for k, v in sender_profile.items() if v}
    if sender_profile:
        metadata["sender_profile"] = sender_profile
    if sender_permissions:
        metadata["sender_permissions"] = sender_permissions

    try:
        is_at = getattr(event, "is_at_or_wake_command", False)
    except Exception:
        is_at = False
    if is_at:
        metadata["is_at_or_wake_command"] = True

    if reply:
        metadata["reply"] = reply
    if segments:
        metadata["segments"] = segments

    return metadata


@register(
    "nanoclaw_bridge",
    "pjh456",
    "Forward AstrBot messages to NanoClaw",
    "0.2.3",
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
        self.health_url: str = _derive_health_url(self.inbound_url)
        self.token: str = cfg.get("nanoclaw_token", "")
        self.forward_mode: str = cfg.get("forward_mode", "all")
        self.command_prefix: str = cfg.get("command_prefix", "/nc ")
        self.block_astrbot_on_forward: bool = bool(
            cfg.get("block_astrbot_on_forward", True)
        )
        self.ignore_self: bool = bool(cfg.get("ignore_self", True))
        self.timeout_ms: int = int(cfg.get("timeout_ms", 15000))
        self.image_cache_max_mb: int = int(cfg.get("image_cache_max_mb", -1))
        self.image_cache_ttl_days: int = int(cfg.get("image_cache_ttl_days", -1))
        self._pending_events: Dict[str, Dict[str, Any]] = {}

        # Avoid container-level proxy envs breaking local calls
        self._client = httpx.AsyncClient(
            timeout=self.timeout_ms / 1000, trust_env=False
        )
        self.context.register_web_api(
            OUTBOUND_ROUTE,
            self._handle_outbound,
            ["POST"],
            "Deliver NanoClaw outbound messages back into AstrBot event context.",
        )
        logger.info(
            f"NanoClaw bridge config inbound={self.inbound_url} control={self.control_url}"
        )

    async def terminate(self):
        await self._client.aclose()

    def _cleanup_pending_events(self) -> None:
        now = datetime.now(timezone.utc).timestamp()
        expired_keys = [
            key
            for key, state in self._pending_events.items()
            if state.get("expires_at", 0) <= now
        ]
        for key in expired_keys:
            self._pending_events.pop(key, None)

    def _remember_event(
        self,
        chat_key: str,
        event: AstrMessageEvent,
        umo: Optional[str],
    ) -> None:
        self._cleanup_pending_events()
        self._pending_events[chat_key] = {
            "event": event,
            "umo": str(umo) if umo else "",
            "expires_at": datetime.now(timezone.utc).timestamp()
            + PENDING_EVENT_TTL_SECONDS,
        }

    def _resolve_pending_event(
        self,
        chat_id: str,
        umo: str,
    ) -> Optional[Dict[str, Any]]:
        self._cleanup_pending_events()
        if chat_id and chat_id in self._pending_events:
            return self._pending_events[chat_id]
        if umo:
            for state in self._pending_events.values():
                if state.get("umo") == umo:
                    return state
        return None

    def _build_outbound_chain(
        self,
        event: AstrMessageEvent,
        text: str,
        umo: str,
    ) -> MessageChain:
        cfg = self.context.get_config(umo or None)
        platform_settings = cfg.get("platform_settings", {}) if cfg else {}
        reply_prefix = _to_str(platform_settings.get("reply_prefix")).strip()
        reply_with_mention = bool(platform_settings.get("reply_with_mention", False))
        reply_with_quote = bool(platform_settings.get("reply_with_quote", False))

        if reply_prefix:
            text = reply_prefix + text

        chain = []
        if reply_with_quote:
            chain.append(
                Reply(
                    id=event.message_obj.message_id,
                    sender_id=event.get_sender_id(),
                    sender_nickname=event.get_sender_name(),
                    message_str=event.message_str,
                    text=event.message_str,
                    qq=event.get_sender_id(),
                )
            )
        if reply_with_mention and not event.is_private_chat():
            chain.append(At(qq=event.get_sender_id(), name=event.get_sender_name()))
            if text:
                text = "\n" + text
        if text:
            chain.append(Plain(text=text))
        return MessageChain(chain=chain)

    async def _handle_outbound(self):
        if self.token:
            auth = request.headers.get("Authorization", "")
            token = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
            header_token = request.headers.get("x-astrbot-token", "")
            if token != self.token and header_token != self.token:
                return jsonify({"ok": False, "error": "Unauthorized"}), 401

        payload = await request.get_json(silent=True) or {}
        chat_id = _to_str(payload.get("chat_id")).strip()
        umo = _to_str(payload.get("umo")).strip()
        text = _to_str(payload.get("text")).strip()
        if not chat_id and not umo:
            return jsonify({"ok": False, "error": "Missing chat_id"}), 400
        if not text:
            return jsonify({"ok": False, "error": "Missing text"}), 400

        pending = self._resolve_pending_event(chat_id, umo)
        if not pending:
            return jsonify({"ok": False, "error": "No pending event"}), 404

        event = pending["event"]
        try:
            chain = self._build_outbound_chain(event, text, umo or pending.get("umo", ""))
            await event.send(chain)
            return jsonify({"ok": True})
        except Exception as exc:
            logger.warning(f"NanoClaw outbound send failed: {exc!r}")
            return jsonify({"ok": False, "error": "Send failed"}), 500

    def _is_self_message(self, event: AstrMessageEvent, sender_id: str) -> bool:
        try:
            self_id = getattr(event.message_obj, "self_id", None)
        except Exception:
            self_id = None
        if self_id is None:
            return False
        return str(self_id) == str(sender_id)

    def _has_command_handler(self, event: AstrMessageEvent) -> bool:
        handlers = event.get_extra("activated_handlers", []) or []
        for handler in handlers:
            for f in getattr(handler, "event_filters", []) or []:
                if isinstance(f, (CommandFilter, CommandGroupFilter)):
                    return True
        return False

    def _should_forward(self, event: AstrMessageEvent, content: str) -> bool:
        if not content:
            return False
        # Never forward commands (system/plugin commands)
        if self._has_command_handler(event):
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

    def _should_send_context_only(
        self,
        event: AstrMessageEvent,
        content: str,
        is_nc_command: bool,
    ) -> bool:
        if is_nc_command or self._has_command_handler(event):
            return False
        try:
            if not event.get_group_id():
                return False
        except Exception:
            return False
        if content.strip():
            return True
        return bool(_extract_message_segments(event))

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

        sender_fields = _extract_sender_fields(event)
        group_fields = _extract_group_fields(event)
        sender_name = sender_fields.get("sender_name", "")
        group_name = group_fields.get("group_name", "")

        payload: Dict[str, Any] = {
            "action": "set_main",
            "chat_id": str(umo or session_id or "unknown"),
            "umo": str(umo) if umo is not None else None,
            "group_name": group_name or sender_name or None,
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

    @filter.command("nc_diag")
    async def cmd_diag(self, event: AstrMessageEvent):
        health_ok = False
        health_start = asyncio.get_event_loop().time()
        try:
            resp = await self._client.get(self.health_url)
            health_ok = resp.status_code < 300
        except Exception as exc:
            logger.warning(f"NanoClaw health check error ({self.health_url}): {exc!r}")
        health_elapsed_ms = int((asyncio.get_event_loop().time() - health_start) * 1000)

        diag_start = asyncio.get_event_loop().time()
        data = await self._post_control({"action": "diag", "chat_id": "diag"})
        diag_elapsed_ms = int((asyncio.get_event_loop().time() - diag_start) * 1000)

        yield event.plain_result(
            _format_diag_message(
                health_ok,
                health_elapsed_ms,
                diag_elapsed_ms,
                data,
                inbound_url=self.inbound_url,
                control_url=self.control_url,
                forward_mode=self.forward_mode,
                command_prefix=self.command_prefix,
                block_astrbot_on_forward=self.block_astrbot_on_forward,
                ignore_self=self.ignore_self,
            )
        )

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

        sender_fields = _extract_sender_fields(event)
        sender_permissions = _extract_sender_permissions(event)
        sender_id = sender_fields.get("sender_id", "")
        sender_name = sender_fields.get("sender_name", "")

        if self.ignore_self and sender_id and self._is_self_message(event, sender_id):
            return

        try:
            msg_obj = event.message_obj
            session_id = getattr(msg_obj, "session_id", None)
            message_id = getattr(msg_obj, "message_id", None)
            timestamp = getattr(msg_obj, "timestamp", None)
            group_id = getattr(msg_obj, "group_id", None)
            self_id = getattr(msg_obj, "self_id", None)
        except Exception:
            session_id = None
            message_id = None
            timestamp = None
            group_id = None
            self_id = None

        try:
            umo = event.unified_msg_origin
        except Exception:
            umo = None

        group_fields = _extract_group_fields(event)
        group_name = group_fields.get("group_name", "")
        group_id = _pick_first(group_id, group_fields.get("group_id", ""))

        try:
            platform_name = event.get_platform_name()
        except Exception:
            platform_name = ""
        try:
            platform_id = event.get_platform_id()
        except Exception:
            platform_id = ""

        segments = _extract_message_segments(event)
        reply = _extract_reply(event, segments)
        metadata = _build_metadata(
            event,
            sender_fields,
            sender_permissions,
            group_fields,
            segments,
            reply,
            platform_name,
            platform_id,
            session_id,
            umo,
        )
        if self.image_cache_max_mb >= 0 or self.image_cache_ttl_days >= 0:
            attachment_cache_policy: Dict[str, Any] = {}
            if self.image_cache_max_mb >= 0:
                attachment_cache_policy["max_bytes"] = self.image_cache_max_mb * 1024 * 1024
            else:
                attachment_cache_policy["max_bytes"] = -1
            if self.image_cache_ttl_days >= 0:
                attachment_cache_policy["ttl_hours"] = self.image_cache_ttl_days * 24
            else:
                attachment_cache_policy["ttl_hours"] = -1
            metadata["attachment_cache_policy"] = attachment_cache_policy
        should_forward = self._should_forward(event, content)
        if not should_forward and not self._should_send_context_only(
            event, content, is_nc_command
        ):
            return
        if not should_forward:
            metadata["context_only"] = True
        if self.block_astrbot_on_forward:
            # Prevent AstrBot default response whenever forwarding is triggered
            if should_forward:
                try:
                    event.stop_event()
                except Exception:
                    pass

        is_from_me = False
        if self_id and sender_id:
            is_from_me = str(self_id) == str(sender_id)

        if is_nc_command:
            content = content[len(self.command_prefix) :].lstrip()
        chat_key = str(umo or session_id or "unknown")
        self._remember_event(chat_key, event, str(umo) if umo is not None else None)
        payload: Dict[str, Any] = {
            "chat_id": chat_key,
            "umo": str(umo) if umo is not None else None,
            "sender_id": sender_id,
            "sender_name": sender_name or sender_id or "unknown",
            "sender_nickname": sender_fields.get("sender_nickname") or None,
            "sender_username": sender_fields.get("sender_username") or None,
            "sender_card": sender_fields.get("sender_card") or None,
            "content": content,
            "timestamp": _to_iso(timestamp),
            "is_group": bool(group_id),
            "group_id": str(group_id) if group_id else None,
            "group_name": group_name or None,
            "message_id": str(message_id) if message_id is not None else None,
            "platform_name": platform_name or None,
            "platform_id": platform_id or None,
            "session_id": str(session_id) if session_id is not None else None,
            "is_from_me": is_from_me,
            "metadata": metadata,
        }

        await self._post(payload)

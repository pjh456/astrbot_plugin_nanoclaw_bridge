# AstrBot NanoClaw Bridge

Forward AstrBot messages to NanoClaw via HTTP.

The bridge forwards more than plain `message_str`: it also includes structured
metadata such as reply/quote context, message segments, sender profile,
sender permissions, and platform/session identifiers so NanoClaw can see
closer-to-native AstrBot LLM context.

## Install

1. Copy this repo into your AstrBot plugins directory.
2. Enable the plugin in AstrBot.
3. Configure it via the plugin settings UI.

## Config

- `nanoclaw_inbound_url`: NanoClaw inbound URL
- `nanoclaw_control_url`: NanoClaw control URL (optional, auto-derived from inbound URL)
- `nanoclaw_token`: Shared token for NanoClaw inbound auth (optional)
- `forward_mode`: `all` | `command` | `mention`
- `command_prefix`: prefix for command mode
- `block_astrbot_on_forward`: stop AstrBot default reply whenever forwarding triggers
- `ignore_self`: ignore bot's own messages
- `capture_group_context`: keep recent non-trigger group chat in plugin memory
- `group_context_max_messages`: number of recent group messages attached to a trigger
- `timeout_ms`: HTTP timeout

Notes:
- Command messages are never forwarded to NanoClaw. Detection uses AstrBot's activated handlers and CommandFilter/CommandGroupFilter.
- With `forward_mode=mention` and `capture_group_context=true`, the plugin behaves closer to AstrBot's native group memory flow: ordinary group messages are buffered locally, and when the bot is actually mentioned the recent group history is attached to the forwarded payload.
- The buffered group context is in-memory only and is cleared on plugin restart or `/nc_reset`.

## NanoClaw side

Make sure NanoClaw has the AstrBot HTTP channel enabled and configured:

```
ASTRBOT_HTTP_HOST=127.0.0.1
ASTRBOT_HTTP_PORT=7801
ASTRBOT_HTTP_TOKEN=your_shared_secret
ASTRBOT_API_BASE=http://127.0.0.1:6185
ASTRBOT_API_KEY=abk_xxx
```

## Payload

The plugin posts JSON to NanoClaw:

```

## Commands

- `/nc_main` set current chat as NanoClaw main control
- `/nc_use` alias for `/nc_main`
- `/nc_status` show current NanoClaw main control chat
- `/nc_ping` ping NanoClaw control endpoint
- `/nc ` (prefix) forward only to NanoClaw and block AstrBot reply
{
  "chat_id": "<session_id>",
  "umo": "<unified_msg_origin>",
  "sender_id": "<sender_id>",
  "sender_name": "<sender_name>",
  "content": "<message_str>",
  "timestamp": "<ISO8601>",
  "is_group": true,
  "message_id": "<message_id>",
  "metadata": {
    "source": "astrbot",
    "umo": "<unified_msg_origin>",
    "platform_name": "<platform_name>",
    "platform_id": "<platform_id>",
    "session_id": "<session_id>",
    "group_name": "<group_name>",
    "sender_profile": {
      "nickname": "<sender_nickname>",
      "username": "<sender_username>",
      "card": "<sender_card>"
    },
    "sender_permissions": {
      "astrbot_role": "admin",
      "is_astrbot_admin": true,
      "platform_role": "owner",
      "platform_title": "<adapter-specific-title>",
      "is_platform_owner": true,
      "is_platform_admin": true
    },
    "reply": {
      "message_id": "<quoted_message_id>",
      "sender_name": "<quoted_sender>",
      "content": "<quoted_text>",
      "segments": [
        { "type": "image", "url": "<quoted_image_url>" }
      ]
    },
    "segments": [
      { "type": "reply", "id": "<quoted_message_id>" },
      { "type": "text", "text": "<segment_text>" }
    ],
    "recent_chat_history": [
      {
        "timestamp": "<ISO8601>",
        "sender_id": "<sender_id>",
        "sender_name": "<sender_name>",
        "content": "<recent group message>"
      }
    ]
  }
}
```

## License

MIT

# AstrBot NanoClaw Bridge

Forward AstrBot messages to NanoClaw via HTTP.

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
- `timeout_ms`: HTTP timeout

Notes:
- Command messages are never forwarded to NanoClaw. Detection uses AstrBot's activated handlers and CommandFilter/CommandGroupFilter.

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
  "message_id": "<message_id>"
}
```

## License

MIT

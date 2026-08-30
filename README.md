# TOXIC Relay — Single Python

This is the simplified single-process version of the Telegram VC relay. It replaces the old Node controller + Python engine architecture with one Python process using Telethon + PyTgCalls.

## Features

- `$admin` / `$panel` — owner control menu
- `$relay <target_chat_id>` — run inside the source VC/group to relay source VC audio to the target VC
- `$leave` / `$stop` — stop the current relay
- `$leaveall` — stop all active relays
- `$status` — show status
- `$volume <value>`
- `$gain <value>`
- `$loudness <value>`
- `$bass <value>`
- `$treble <value>`
- `$echo on/off`
- `$phone`, `$otp`, `$2fa` for first login when no saved session exists

Only `OWNER_ID` is authorized.

## Railway

1. Push these files to GitHub or deploy the folder with Railway CLI.
2. Add variables from `.env.example`.
3. Add a Railway Volume mounted at `/data` so the Telethon session survives restarts.
4. Deploy.
5. On first boot, if no session exists, the bot asks the owner for the phone/OTP/2FA through the controller bot. Use `$phone`, `$otp`, `$2fa` as instructed.
6. Keep the controller bot token and Telegram session private.

No public port is required by this worker-style bot.

## Important VC permissions

The user account must be a member of both voice chats and have whatever permissions Telegram requires for joining/recording/streaming audio.

## Local test

```bash
python -m venv .venv
# activate the venv
pip install -r requirements.txt
python toxic_relay.py
```

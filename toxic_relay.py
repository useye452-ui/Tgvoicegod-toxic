import asyncio
import os
from pathlib import Path
from array import array
from collections import deque
import math

from dotenv import load_dotenv
load_dotenv()

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from pytgcalls import PyTgCalls
from pytgcalls import filters as tg_filters
from pytgcalls.types import Device, Direction, ExternalMedia, MediaStream, RecordStream, StreamFrames
from pytgcalls.types.raw import AudioParameters

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
PHONE = os.getenv("PHONE_NUMBER", "").strip()
PREFIX = os.getenv("PREFIX", "$")
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
SESSION_PATH = os.getenv("SESSION_PATH", "./data/toxic_relay")

if not API_ID or not API_HASH or not BOT_TOKEN or not OWNER_ID:
    raise SystemExit("Missing API_ID, API_HASH, BOT_TOKEN or OWNER_ID in .env")

Path(SESSION_PATH).parent.mkdir(parents=True, exist_ok=True)

# The user account joins/records VC calls. The bot account is only the controller.
if SESSION_STRING:
    user = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
else:
    user = TelegramClient(SESSION_PATH, API_ID, API_HASH)
bot = TelegramClient(None, API_ID, API_HASH)
call = PyTgCalls(user)

allowed = {OWNER_ID}
relay = {}
relay_filter_state = {}

SAMPLE_RATE = 48000
CHANNELS = 2

fx = {
    "volume": 100.0,
    "gain": 0.0,
    "loudness": 100.0,
    "bass": 100.0,
    "treble": 100.0,
    "echo": "off",
}

# Waiting queues make first-login OTP/2FA work without stdin on Railway.
phone_queue = asyncio.Queue()
code_queue = asyncio.Queue()
password_queue = asyncio.Queue()


def is_allowed(event):
    return event.sender_id in allowed


def settings():
    return (fx["volume"], fx["gain"], fx["loudness"], fx["echo"], fx["bass"], fx["treble"])


def process_pcm(pcm: bytes) -> bytes:
    if not pcm:
        return pcm
    samples = array("h")
    usable = len(pcm) - (len(pcm) % 2)
    samples.frombytes(pcm[:usable])
    if not samples:
        return pcm

    volume, gain_db, loudness, echo, bass, treble = settings()
    state = relay_filter_state.setdefault(
        "global",
        {"low": [0.0, 0.0], "high_lp": [0.0, 0.0], "echo": deque(maxlen=SAMPLE_RATE * CHANNELS)},
    )

    # User-facing controls remain generous, while the live DSP uses a soft cap
    # to avoid turning high settings into unusable clipped audio.
    gain_linear = 10 ** (min(max(gain_db, 0.0), 24.0) / 20.0)
    gain_linear *= max(0.0, volume / 100.0)
    gain_linear *= max(0.0, loudness / 100.0)
    bass_strength = max(0.0, (bass - 100.0) / 100.0)
    treble_strength = max(0.0, (treble - 100.0) / 100.0)

    low_a = 1.0 - math.exp(-2.0 * math.pi * 180.0 / SAMPLE_RATE)
    high_a = 1.0 - math.exp(-2.0 * math.pi * 3500.0 / SAMPLE_RATE)
    echo_delay = int(SAMPLE_RATE * 0.42) * CHANNELS

    out = array("h")
    for i, raw in enumerate(samples):
        ch = i % CHANNELS
        x = raw / 32768.0
        low = state["low"][ch] + low_a * (x - state["low"][ch])
        state["low"][ch] = low
        high_lp = state["high_lp"][ch] + high_a * (x - state["high_lp"][ch])
        state["high_lp"][ch] = high_lp
        high = x - high_lp
        y = (x + low * bass_strength + high * treble_strength) * gain_linear
        if echo == "on" and len(state["echo"]) >= echo_delay:
            y += state["echo"][-echo_delay] * 0.32
        state["echo"].append(y)
        y = math.tanh(y)
        out.append(max(-32768, min(32767, int(y * 32767))))
    return out.tobytes()


async def frame_handler(_, update: StreamFrames):
    source = update.chat_id
    cfg = relay.get(source)
    if not cfg or not update.frames:
        return
    target = cfg["target"]

    sample_lists = []
    max_samples = 0
    for frame in update.frames:
        raw = frame.frame
        samples = array("h")
        samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
        if samples:
            sample_lists.append(samples)
            max_samples = max(max_samples, len(samples))
    if not max_samples:
        return

    mixed = array("h", [0] * max_samples)
    count = len(sample_lists)
    for samples in sample_lists:
        for i, value in enumerate(samples):
            mixed[i] += value // count
    mixed = array("h", (max(-32768, min(32767, x)) for x in mixed))

    try:
        await call.send_frame(target, Device.MICROPHONE, process_pcm(mixed.tobytes()))
    except Exception as e:
        print(f"[RELAY] send_frame: {type(e).__name__}: {e}", flush=True)


async def relay_on(source: int, target: int):
    if source == target:
        return "❌ Source and target VC must be different."
    if source in relay:
        return "⚠️ Relay is already ON in this source VC."

    params = AudioParameters(bitrate=SAMPLE_RATE, channels=CHANNELS)
    try:
        await call.record(source, RecordStream(audio=True, audio_parameters=params, camera=False, screen=False))
        await call.play(target, MediaStream(ExternalMedia.AUDIO, params))
    except Exception as e:
        return f"❌ Relay start failed: {type(e).__name__}: {e}"

    relay_filter_state["global"] = {"low": [0.0, 0.0], "high_lp": [0.0, 0.0], "echo": deque(maxlen=SAMPLE_RATE * CHANNELS)}
    relay[source] = {"target": target}
    return f"🎙️ <b>RELAY ON</b>\nSource: <code>{source}</code>\nTarget: <code>{target}</code>"


async def relay_off(source: int):
    cfg = relay.pop(source, None)
    if not cfg:
        return "ℹ️ Relay is already OFF."
    try:
        await call.leave_call(source)
    except Exception:
        pass
    try:
        await call.leave_call(cfg["target"])
    except Exception:
        pass
    return "⛔ <b>RELAY OFF</b>"


async def auth_user():
    await user.connect()
    if await user.is_user_authorized():
        me = await user.get_me()
        print(f"✅ Telegram user connected: {me.id}", flush=True)
        return

    phone = PHONE
    if not phone:
        await bot.send_message(OWNER_ID, f"📱 Send {PREFIX}phone +91XXXXXXXXXX")
        phone = await phone_queue.get()

    sent = await user.send_code_request(phone)
    await bot.send_message(OWNER_ID, f"🔐 Telegram OTP required. Send {PREFIX}otp 12345")
    code = await code_queue.get()
    try:
        await user.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
    except Exception as e:
        if "SessionPasswordNeeded" not in type(e).__name__:
            raise
        await bot.send_message(OWNER_ID, f"🔑 2FA required. Send {PREFIX}2fa YOUR_PASSWORD")
        password = await password_queue.get()
        await user.sign_in(password=password)

    me = await user.get_me()
    print(f"✅ Telegram user connected: {me.id}", flush=True)


@bot.on(events.NewMessage)
async def admin_cmd(event):
    if event.sender_id != OWNER_ID:
        return
    text = (event.raw_text or "").strip()
    if text.lower() == PREFIX + "admin":
        await event.reply(menu())
@bot.on(events.NewMessage)
async def controller(event):
    if event.sender_id != OWNER_ID:
        return
    text = (event.raw_text or "").strip()
    if not text.startswith(PREFIX):
        return
    parts = text[len(PREFIX):].split()
    if not parts:
        return
    cmd, args = parts[0].lower(), parts[1:]

    if cmd == "phone":
        if args:
            await phone_queue.put(args[0])
            await event.reply("✅ Phone received. Requesting Telegram code…")
        return
    if cmd == "otp":
        if args:
            await code_queue.put(args[0])
            await event.reply("✅ OTP received.")
        return
    if cmd == "2fa":
        if args:
            await password_queue.put(" ".join(args))
            await event.reply("✅ 2FA received.")
        return
    if cmd in {"start", "panel", "admin", "help"}:
        await event.reply(menu())
        return
    if cmd == "status":
        active = ", ".join(f"{s} → {c['target']}" for s, c in relay.items()) or "No active relay"
        await event.reply(f"🔥 <b>TOXIC RELAY</b>\n\nUser: online\nRelay: {active}")
        return

    # Effects
    if cmd in {"volume", "gain", "loudness", "bass", "treble"}:
        if not args:
            await event.reply(f"Usage: {PREFIX}{cmd} <value>")
            return
        try:
            fx[cmd] = float(args[0])
        except ValueError:
            await event.reply("❌ Value must be a number.")
            return
        await event.reply(f"✅ {cmd.upper()} = {fx[cmd]:g}")
        return
    if cmd == "echo":
        if args and args[0].lower() in {"on", "off"}:
            fx["echo"] = args[0].lower()
            await event.reply(f"🌊 Echo {fx['echo']}")
        else:
            await event.reply(f"Usage: {PREFIX}echo on/off")
        return

    if cmd == "relay":
        if len(args) != 1:
            await event.reply(f"Usage in source group: {PREFIX}relay <target_chat_id>")
            return
        try:
            target = int(args[0])
        except ValueError:
            await event.reply("❌ Target chat ID must be numeric.")
            return
        await event.reply(await relay_on(event.chat_id, target))
        return
    if cmd in {"leave", "stop"}:
        await event.reply(await relay_off(event.chat_id))
        return
    if cmd == "leaveall":
        if not relay:
            await event.reply("ℹ️ No active relays.")
            return
        results = []
        for source in list(relay):
            results.append(await relay_off(source))
        await event.reply("\n".join(results))
        return
    if cmd == "fx":
        await event.reply(effect_text())
        return

    await event.reply(menu())


def effect_text():
    return (
        "🔊 <b>MASTER FX</b>\n\n"
        f"Volume: <b>{fx['volume']:g}%</b>\n"
        f"Gain: <b>{fx['gain']:g} dB</b>\n"
        f"Loudness: <b>{fx['loudness']:g}%</b>\n"
        f"Bass: <b>{fx['bass']:g}%</b>\n"
        f"Treble: <b>{fx['treble']:g}%</b>\n"
        f"Echo: <b>{fx['echo']}</b>"
    )


def menu():
    return (
        "╔══════════════════════════════╗\n"
        "║   🔥 TOXIC RELAY • PYTHON   ║\n"
        "╚══════════════════════════════╝\n\n"
        "🎙 <b>VOICE CHAT RELAY</b>\n\n"
        f"{PREFIX}relay &lt;target_chat_id&gt; — relay current source VC\n"
        f"{PREFIX}leave — stop current relay\n"
        f"{PREFIX}leaveall — stop every relay\n"
        f"{PREFIX}status — live status\n\n"
        "🎛 <b>LIVE EFFECTS</b>\n"
        f"{PREFIX}volume &lt;value&gt;\n"
        f"{PREFIX}gain &lt;value&gt;\n"
        f"{PREFIX}loudness &lt;value&gt;\n"
        f"{PREFIX}bass &lt;value&gt;\n"
        f"{PREFIX}treble &lt;value&gt;\n"
        f"{PREFIX}echo on/off\n\n"
        "🔐 <b>FIRST LOGIN</b>\n"
        f"{PREFIX}phone +91XXXXXXXXXX\n"
        f"{PREFIX}otp 12345\n"
        f"{PREFIX}2fa YOUR_PASSWORD\n\n"
        f"Prefix: <b>{PREFIX}</b>"
    )


async def main():
    print("🔥 TOXIC RELAY • SINGLE PYTHON", flush=True)
    await bot.start(bot_token=BOT_TOKEN)
    await auth_user()
    await call.start()
    call.on_update(tg_filters.stream_frame(Direction.INCOMING, Device.MICROPHONE))(frame_handler)
    print("🟢 Controller + VC engine online", flush=True)
    await asyncio.gather(bot.run_until_disconnected(), user.run_until_disconnected())


if __name__ == "__main__":
    asyncio.run(main())

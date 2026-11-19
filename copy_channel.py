"""
Old channel ke saare audio/video ko new channel me copy karta hai (aapke USER account se).
Telegram server-side copy hota hai -> download/upload nahi.

Heroku (worker dyno) ke liye bana hai:
  * Settings environment variables (Config Vars) se aati hain, code me kuch bharna nahi.
  * Login ke liye SESSION_STRING use hota hai (generate_session.py se apne PC par banao),
    isliye OTP maangne ki zarurat nahi.
  * Progress ek number hai (old channel ka last processed message id) jo aapke
    Telegram "Saved Messages" me save hota hai. Heroku dyno har 24 ghante restart hota hai
    aur disk mit jaata hai, par progress safe rehta hai - restart ke baad wahin se aage badhta hai.

Local PC par bhi chalta hai: env vars set karo (ya .env file) aur `python copy_channel.py`.
"""
import asyncio
import os
import sys

import random

from pyrogram import Client, raw
from pyrogram.errors import FloodWait, InternalServerError

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]


def _chat(value: str):
    """'-100123...' -> int, '@name' / 'name' -> str."""
    value = value.strip()
    try:
        return int(value)
    except ValueError:
        return value


OLD_CHANNEL = _chat(os.environ["OLD_CHANNEL"])
NEW_CHANNEL = _chat(os.environ["NEW_CHANNEL"])
BATCH = max(1, min(int(os.getenv("BATCH", "50")), 100))   # copy_messages ek baar me max 100
DELAY = float(os.getenv("DELAY", "4"))                     # har batch ke baad seconds (kam mat karo)
SCAN = 200                                                 # ek baar me kitne message ids check karne hain

BOT_TOKEN = os.getenv("BOT_TOKEN")        # optional: phone par asli notification ke liye
NOTIFY_USER_ID = os.getenv("NOTIFY_USER_ID")  # optional: aapka Telegram user id

TAG = "CHANNEL_COPY_PROGRESS"   # Saved Messages me progress wale message ki pehchaan


def log(*a):
    print(*a, flush=True)


def wanted(m):
    if m is None or m.empty:
        return False
    if m.audio or m.video:
        return True
    return bool(m.document and (m.document.mime_type or "").startswith(("audio/", "video/")))


class Progress:
    """Saved Messages me ek message: 'CHANNEL_COPY_PROGRESS <old> <new> <last_id> <copied>'."""

    def __init__(self, app):
        self.app = app
        self.msg_id = None
        self.last_id = 0
        self.copied = 0
        self.key = f"{TAG} {OLD_CHANNEL} {NEW_CHANNEL}"

    async def load(self):
        async for m in self.app.get_chat_history("me", limit=100):
            if m.text and m.text.startswith(self.key):
                parts = m.text.split()
                self.msg_id = m.id
                self.last_id = int(parts[-2])
                self.copied = int(parts[-1])
                return

    async def save(self, last_id, copied):
        self.last_id, self.copied = last_id, copied
        text = f"{self.key} {last_id} {copied}"
        if self.msg_id is None:
            sent = await self.app.send_message("me", text)
            self.msg_id = sent.id
        else:
            await self.app.edit_message_text("me", self.msg_id, text)


async def notify(app, text):
    """Saved Messages me message + (agar BOT_TOKEN set hai to) bot se message bhejo."""
    try:
        await app.send_message("me", text)
    except Exception as e:
        log(f"Saved Messages notify fail: {e}")
    if BOT_TOKEN and NOTIFY_USER_ID:
        import urllib.parse
        import urllib.request

        def send():
            data = urllib.parse.urlencode({"chat_id": NOTIFY_USER_ID, "text": text}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data, timeout=20)
        try:
            await asyncio.get_running_loop().run_in_executor(None, send)
        except Exception as e:
            log(f"Bot notify fail: {e}")


async def copy_batch(app, ids):
    """Server-side copy (drop_author=True => 'Forwarded from' nahi dikhta), ek baar me 100 tak."""
    await app.invoke(
        raw.functions.messages.ForwardMessages(
            from_peer=await app.resolve_peer(OLD_CHANNEL),
            id=ids,
            random_id=[random.getrandbits(63) for _ in ids],
            to_peer=await app.resolve_peer(NEW_CHANNEL),
            drop_author=True,
        )
    )


async def flood_safe(coro_fn):
    """FloodWait aur Telegram ke temporary 500/network errors par ruk ke dobara try karo."""
    tries = 0
    while True:
        try:
            return await coro_fn()
        except FloodWait as e:
            log(f"FloodWait: {e.value}s ruk raha hoon...")
            await asyncio.sleep(e.value + 2)
        except Exception as e:
            msg = str(e).upper()
            temporary = (
                isinstance(e, (InternalServerError, TimeoutError, ConnectionError, OSError))
                or "WORKER_BUSY" in msg or "[500" in msg or "TIMEOUT" in msg
            )
            tries += 1
            if not temporary or tries > 30:
                raise
            wait = min(30 * tries, 300)
            log(f"Telegram temporary error ({str(e)[:80]}). {wait}s baad dobara try ({tries}/30)...")
            await asyncio.sleep(wait)


async def run():
    app = Client("copier", api_id=API_ID, api_hash=API_HASH,
                 session_string=SESSION_STRING, in_memory=True)
    async with app:
        # fresh session me channels ka access cache khaali hota hai, dialogs load karke bharo
        async for _ in app.get_dialogs():
            pass

        progress = Progress(app)
        await progress.load()
        last_id, copied = progress.last_id, progress.copied

        top = None
        async for m in app.get_chat_history(OLD_CHANNEL, limit=1):
            top = m.id
        if top is None:
            log("Old channel khaali hai ya access nahi hai.")
            return
        log(f"Old channel last message id: {top}. Resume from id {last_id + 1}. Pehle copy hue: {copied}")

        await notify(app, f"▶️ Channel copy start/resume hua. Id {last_id + 1} se shuru, total {top} tak. Pehle copy hue: {copied}")

        pending = []   # copy hone wale ids, batch bharne tak jama karte hain
        pos = last_id

        async def flush(upto):
            nonlocal pending, copied
            while pending:
                chunk, pending = pending[:BATCH], pending[BATCH:]
                await flood_safe(lambda: copy_batch(app, chunk))
                copied += len(chunk)
                await asyncio.sleep(DELAY)
            await progress.save(upto, copied)
            log(f"id {upto}/{top} dekh liye | {copied} songs/videos copy ho gaye")

        while pos < top:
            ids = list(range(pos + 1, min(pos + SCAN, top) + 1))
            msgs = await flood_safe(lambda: app.get_messages(OLD_CHANNEL, ids))
            pending += [m.id for m in msgs if wanted(m)]
            pos = ids[-1]
            if len(pending) >= BATCH or pos >= top:
                try:
                    await flush(pos)
                except Exception as e:
                    if "RESTRICTED" in str(e).upper() or "FORWARD" in str(e).upper():
                        log("❌ Channel par 'Restrict saving content' laga hai, copy nahi ho sakta.")
                    log(f"Error: {e}")
                    await notify(app, f"❌ Channel copy ruk gaya: {e}")
                    raise

        log(f"✅ Sab copy ho gaya! Total: {copied}")
        await notify(app, f"✅ Channel copy complete! {copied} audio/video copy ho gaye.")


def main():
    try:
        asyncio.run(run())
    except Exception as e:
        log(f"Script error: {e!r}")
        sys.exit(1)   # Heroku restart karke wahin se resume karega
    # Kaam khatam: Heroku exit hone par dyno dobara chalata hai, isliye idle rakho.
    # Aap `heroku ps:scale worker=0` chala do.
    log("Kaam ho gaya. Ab dyno band kar do:  heroku ps:scale worker=0")
    while True:
        import time
        time.sleep(3600)


if __name__ == "__main__":
    main()

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
import re
from contextlib import AsyncExitStack

from pyrogram import Client, raw
from pyrogram.errors import FloodWait, InternalServerError

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
# Accounts: ye sab Config Vars padhe jaate hain (jo bhi set ho):
#   SESSION_STRING, SESSION_STRING1, SESSION_STRING2, ... SESSION_STRING30
#   aur SESSION_STRINGS (comma/space se alag kai strings)
def _load_sessions():
    raw = []
    for name in ["SESSION_STRING"] + [f"SESSION_STRING{n}" for n in range(1, 31)] + ["SESSION_STRINGS"]:
        v = os.getenv(name)
        if v:
            raw += [x for x in re.split(r"[,\s]+", v.strip().strip("\"'")) if x]
    out = []
    for x in raw:
        if x not in out:      # same string do baar ho to ek hi baar
            out.append(x)
    if not out:
        raise SystemExit("Koi SESSION_STRING set nahi hai (SESSION_STRING, SESSION_STRING1, ...)")
    return out


SESSION_LIST = _load_sessions()


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


def media_uid(m):
    """File ki pehchaan. Server-side copy me same file ki file_unique_id wahi rehti hai."""
    for obj in (m.audio, m.video, m.document):
        if obj is not None:
            return obj.file_unique_id
    return None


async def existing_in_new(app):
    """New channel me pehle se maujood audio/video ki unique ids."""
    uids = set()
    n = 0
    async for m in app.get_chat_history(NEW_CHANNEL):
        n += 1
        if wanted(m):
            uids.add(media_uid(m))
        if n % 2000 == 0:
            log(f"  New channel scan: {n} messages dekhe, {len(uids)} files mili...")
    return uids


class Progress:
    """Saved Messages me ek message: 'CHANNEL_COPY_PROGRESS <old> <new> <last_id> <copied>'."""

    def __init__(self, app, worker=0, total=1):
        self.app = app
        self.msg_id = None
        self.last_id = 0
        self.copied = 0
        self.key = f"{TAG} {OLD_CHANNEL} {NEW_CHANNEL}"
        if total > 1:
            self.key += f" w{worker}/{total}"

    def _match(self, m):
        if not m.text or not m.text.startswith(self.key):
            return False
        return len(m.text.split()) == len(self.key.split()) + 2   # key + last_id + copied

    async def load(self):
        found = None
        try:
            async for m in self.app.search_messages("me", query=TAG, limit=20):
                if self._match(m):
                    found = m
                    break
        except Exception as e:
            log(f"Progress search fail: {e}")
        if found is None:
            async for m in self.app.get_chat_history("me", limit=3000):
                if self._match(m):
                    found = m
                    break
        if found is not None:
            parts = found.text.split()
            self.msg_id = found.id
            self.last_id = int(parts[-2])
            self.copied = int(parts[-1])
            log(f"Progress mila: id {self.last_id}, copied {self.copied}")
        else:
            log("Progress message nahi mila, shuru se start.")

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
            if not temporary or tries > 200:
                raise
            wait = min(30 * tries, 300)
            log(f"Telegram temporary error ({str(e)[:80]}). {wait}s baad dobara try ({tries}/200)...")
            await asyncio.sleep(wait)


async def worker(i, total, app, top, existing, notifier):
    """Account i: old channel ke blocks (SCAN ids) me se har total-wa block process karta hai."""
    tag = f"[acc{i + 1}/{total}]"
    progress = Progress(app, i, total)
    await progress.load()
    copied = progress.copied
    last_id = progress.last_id
    nblocks = (top + SCAN - 1) // SCAN
    b = nblocks if last_id >= top else last_id // SCAN
    while b < nblocks and b % total != i:
        b += 1
    log(f"{tag} resume: id {last_id + 1} ke baad, block {b}/{nblocks}, pehle copy hue {copied}")

    try:
        while b < nblocks:
            lo, hi = b * SCAN + 1, min((b + 1) * SCAN, top)
            ids = list(range(lo, hi + 1))
            msgs = await flood_safe(lambda: app.get_messages(OLD_CHANNEL, ids))
            pending = []
            for m in msgs:
                if wanted(m):
                    uid = media_uid(m)
                    if uid in existing:
                        continue          # pehle se new channel me hai ya old me duplicate
                    existing.add(uid)
                    pending.append(m.id)
            while pending:
                chunk, pending = pending[:BATCH], pending[BATCH:]
                await flood_safe(lambda: copy_batch(app, chunk))
                copied += len(chunk)
                await asyncio.sleep(DELAY)
            await progress.save(hi, copied)
            log(f"{tag} id {hi}/{top} dekh liye | {copied} copy ho gaye")
            b += total
    except Exception as e:
        if "RESTRICTED" in str(e).upper() or "FORWARD" in str(e).upper():
            log(f"{tag} ❌ Channel par 'Restrict saving content' laga hai, copy nahi ho sakta.")
        log(f"{tag} Error: {e}")
        await notify(notifier, f"❌ Channel copy ruk gaya ({tag}): {e}")
        raise
    return copied


async def run():
    total = len(SESSION_LIST)
    async with AsyncExitStack() as stack:
        apps = []
        for n, sess in enumerate(SESSION_LIST):
            app = Client(f"copier{n}", api_id=API_ID, api_hash=API_HASH,
                         session_string=sess, in_memory=True)
            apps.append(await stack.enter_async_context(app))

        async def warm(app):
            # fresh session me channels ka access cache khaali hota hai, dialogs load karke bharo
            async for _ in app.get_dialogs():
                pass
        await asyncio.gather(*(warm(a) for a in apps))
        log(f"{total} account(s) login ho gaye.")

        existing = set()
        if os.getenv("SKIP_EXISTING", "1") != "0":
            log("New channel me pehle se maujood files check ho rahi hain...")
            existing = await existing_in_new(apps[0])
            log(f"New channel me pehle se {len(existing)} audio/video hain, ye skip hongi.")

        top = None
        async for m in apps[0].get_chat_history(OLD_CHANNEL, limit=1):
            top = m.id
        if top is None:
            log("Old channel khaali hai ya access nahi hai.")
            return

        msg = f"▶️ Channel copy start/resume hua. {total} account, total id {top}. New channel me pehle se: {len(existing)} files"
        log(msg)
        await notify(apps[0], msg)

        results = await asyncio.gather(
            *(worker(i, total, a, top, existing, apps[0]) for i, a in enumerate(apps)),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            raise errors[0]
        done = sum(results)
        log(f"✅ Sab copy ho gaya! Is run me total: {done}")
        await notify(apps[0], f"✅ Channel copy complete! {done} audio/video copy hue (is run me).")


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

"""TikTok live -> Telegram notifier.

Polls TikTok every POLL_SECONDS; when a watched account goes live it sends
one Telegram message. Sends again only after the account has gone offline
and started a new stream.
"""
import asyncio
import json
import logging
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from TikTokLive import TikTokLiveClient

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
USERS = [u.strip().lstrip("@") for u in os.getenv("TIKTOK_USERS", "").split(",") if u.strip()]
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
STATE_FILE = BASE / "state.json"  # remembers the chat id learned from /start
API = f"https://api.telegram.org/bot{TOKEN}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tiktok-live-bot")


def load_chat_id():
    env = os.getenv("TELEGRAM_CHAT_ID")
    if env:
        return int(env)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text()).get("chat_id")
    return None


async def send(http, chat_id, text):
    r = await http.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text})
    r.raise_for_status()


async def wait_for_start(http):
    """Block until someone sends /start to the bot; remember that chat."""
    log.info("No chat id yet - send /start to your bot in Telegram")
    offset = None
    while True:
        r = await http.get(f"{API}/getUpdates", params={"timeout": 30, "offset": offset}, timeout=40)
        for upd in r.json().get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            if msg.get("text", "").startswith("/start"):
                chat_id = msg["chat"]["id"]
                STATE_FILE.write_text(json.dumps({"chat_id": chat_id}))
                await send(http, chat_id, "تمام ✅ رح توصلك إشعارات البث هون.\nأراقب: " + ", ".join("@" + u for u in USERS))
                return chat_id


async def watch(http, chat_id, user):
    client = TikTokLiveClient(unique_id=f"@{user}")
    was_live = False
    failures = 0
    while True:
        try:
            live = await client.is_live()
            failures = 0
            if live and not was_live:
                log.info("@%s went live", user)
                await send(http, chat_id, f"🔴 @{user} صار لايف على تيك توك!\nhttps://www.tiktok.com/@{user}/live")
            elif was_live and not live:
                log.info("@%s ended the stream", user)
            was_live = live
        except Exception as e:  # network blips, TikTok throttling
            failures += 1
            log.warning("check failed for @%s (%d): %s", user, failures, e)
            if failures == 10:
                await send(http, chat_id, f"⚠️ صار عندي 10 أخطاء متتالية في فحص @{user}: {e}")
        await asyncio.sleep(POLL_SECONDS * (2 if failures >= 3 else 1))


LIVE_STATE_FILE = BASE / "live_state.json"  # {user: bool}, used by --once (GitHub Actions)


async def run_once():
    """Single check of every account, for cron runners. Persists live flags."""
    chat_id = load_chat_id()
    if not chat_id:
        raise SystemExit("Set TELEGRAM_CHAT_ID (run: python bot.py --chat-id)")
    prev = json.loads(LIVE_STATE_FILE.read_text()) if LIVE_STATE_FILE.exists() else {}
    cur = dict(prev)
    async with httpx.AsyncClient(timeout=20) as http:
        for user in USERS:
            try:
                live = await TikTokLiveClient(unique_id=f"@{user}").is_live()
            except Exception as e:
                log.warning("check failed for @%s: %s", user, e)
                continue  # keep previous state, try again next run
            if live and not prev.get(user):
                await send(http, chat_id, f"🔴 @{user} صار لايف على تيك توك!\nhttps://www.tiktok.com/@{user}/live")
            cur[user] = live
            log.info("@%s live=%s", user, live)
    if cur != prev or not LIVE_STATE_FILE.exists():
        LIVE_STATE_FILE.write_text(json.dumps(cur, indent=1))


async def print_chat_id():
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(f"{API}/getUpdates")
        for upd in r.json().get("result", []):
            msg = upd.get("message") or {}
            if msg:
                print("chat id:", msg["chat"]["id"])
                return
        print("No messages yet - send /start to the bot first, then rerun")


async def main():
    if not USERS:
        raise SystemExit("Set TIKTOK_USERS in .env")
    async with httpx.AsyncClient(timeout=20) as http:
        chat_id = load_chat_id() or await wait_for_start(http)
        log.info("Watching %s -> chat %s", USERS, chat_id)
        await asyncio.gather(*(watch(http, chat_id, u) for u in USERS))


if __name__ == "__main__":
    import sys

    if "--chat-id" in sys.argv:
        asyncio.run(print_chat_id())
    elif "--once" in sys.argv:
        if not USERS:
            raise SystemExit("Set TIKTOK_USERS")
        asyncio.run(run_once())
    else:
        asyncio.run(main())

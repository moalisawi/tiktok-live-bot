"""TikTok live -> Telegram notifier with an editable watch list.

One invocation is a "session" (default ~4.5 min) meant to be started by a
GitHub Actions cron every 5 minutes. During a session the bot long-polls
Telegram (so /add, /remove and the delete buttons answer within seconds) and
checks TikTok every POLL_SECONDS.

The watch list and each account's last-known live flag live in a pinned
message of the owner's chat, so nothing private is stored in the repo.
"""
import asyncio
import logging
import os
import re
import sys
import time

import httpx
from dotenv import load_dotenv
from TikTokLive import TikTokLiveClient

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
SEED_USERS = [u.strip().lstrip("@").lower() for u in os.getenv("TIKTOK_USERS", "").split(",") if u.strip()]
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
API = f"https://api.telegram.org/bot{TOKEN}"
HEADER = "📋 قائمة المراقبة"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("tiktok-live-bot")

HELP = (
    "أهلاً 👋 أنا بنبهك لما حساب تيك توك يبدأ بث مباشر.\n\n"
    "• لإضافة حساب: ابعتلي اسمه أو رابطه (مثلاً @username)\n"
    "• /list — عرض الحسابات مع زر حذف لكل حساب\n"
    "• /remove username — حذف حساب"
)


class Watch:
    """Watch list {username: was_live}, persisted in a pinned message."""

    def __init__(self, http):
        self.http = http
        self.users: dict[str, bool] = {}
        self.message_id = None
        self._saved_text = None

    async def tg(self, method, timeout=30, **params):
        r = await self.http.post(f"{API}/{method}", json=params, timeout=timeout)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"{method}: {data.get('description')}")
        return data["result"]

    def render(self):
        lines = [HEADER] + [f"{'🔴' if live else '⚪'} {u}" for u, live in self.users.items()]
        if not self.users:
            lines.append("(فاضية)")
        return "\n".join(lines)

    async def load(self):
        pinned = (await self.tg("getChat", chat_id=CHAT_ID)).get("pinned_message") or {}
        text = pinned.get("text", "")
        if text.startswith(HEADER):
            self.message_id = pinned["message_id"]
            self.users = {m.group(2): m.group(1) == "🔴" for m in re.finditer(r"^(🔴|⚪) (\S+)$", text, re.M)}
            self._saved_text = text
            return
        self.users = {u: False for u in SEED_USERS}
        msg = await self.tg("sendMessage", chat_id=CHAT_ID, text=self.render())
        self.message_id = msg["message_id"]
        self._saved_text = self.render()
        await self.tg("pinChatMessage", chat_id=CHAT_ID, message_id=self.message_id, disable_notification=True)

    async def save(self):
        text = self.render()
        if text != self._saved_text:
            await self.tg("editMessageText", chat_id=CHAT_ID, message_id=self.message_id, text=text)
            self._saved_text = text

    # ---- messages -----------------------------------------------------
    def list_payload(self):
        if self.users:
            text = "الحسابات اللي براقبها:\n" + "\n".join(f"• @{u}" for u in self.users)
        else:
            text = "القائمة فاضية. ابعتلي اسم حساب لأضيفه."
        kb = [[{"text": f"🗑 حذف @{u}", "callback_data": f"del:{u}"}] for u in self.users]
        return {"text": text, "reply_markup": {"inline_keyboard": kb}}

    async def send_list(self):
        await self.tg("sendMessage", chat_id=CHAT_ID, **self.list_payload())

    async def say(self, text):
        await self.tg("sendMessage", chat_id=CHAT_ID, text=text)


def extract_user(text):
    text = re.sub(r"^/\w+(@\w+)?\s*", "", text.strip())
    m = re.search(r"tiktok\.com/@([\w.]+)", text) or re.search(r"^@?([\w.]{2,24})$", text)
    return m.group(1).lower().rstrip(".") if m else None


async def user_exists(user):
    try:
        await TikTokLiveClient(unique_id=f"@{user}").is_live()
        return True
    except Exception as e:
        if "notfound" in type(e).__name__.lower():
            return False
        log.warning("could not verify @%s (%s), adding anyway", user, e)
        return True


async def handle_message(w: Watch, msg):
    if msg["chat"]["id"] != CHAT_ID:
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if cmd in ("/start", "/help"):
        await w.say(HELP)
        await w.send_list()
    elif cmd == "/list":
        await w.send_list()
    elif cmd == "/remove":
        user = extract_user(text)
        if user in w.users:
            del w.users[user]
            await w.save()
            await w.say(f"🗑 حذفت @{user}")
        else:
            await w.say("ما لقيت هاد الحساب بالقائمة. جرّب /list")
    else:  # /add x, or any plain text / link
        user = extract_user(text)
        if not user:
            await w.say("ما فهمت اسم الحساب. ابعته هيك: @username أو رابط تيك توك")
        elif user in w.users:
            await w.say(f"@{user} موجود بالقائمة أصلاً")
        elif not await user_exists(user):
            await w.say(f"ما لقيت حساب @{user} على تيك توك، تأكد من الاسم")
        else:
            w.users[user] = False
            await w.save()
            await w.say(f"✅ أضفت @{user}")
            await w.send_list()


async def handle_callback(w: Watch, cb):
    if cb["message"]["chat"]["id"] != CHAT_ID:
        return
    data = cb.get("data", "")
    if data.startswith("del:"):
        user = data[4:]
        w.users.pop(user, None)
        await w.save()
        await w.tg("answerCallbackQuery", callback_query_id=cb["id"], text=f"تم حذف @{user}")
        await w.tg("editMessageText", chat_id=CHAT_ID, message_id=cb["message"]["message_id"], **w.list_payload())
    else:
        await w.tg("answerCallbackQuery", callback_query_id=cb["id"])


async def check_live(w: Watch):
    async def one(user):
        try:
            return user, await TikTokLiveClient(unique_id=f"@{user}").is_live()
        except Exception as e:
            log.warning("check failed for @%s: %s", user, e)
            return user, None  # keep previous flag

    changed = False
    for user, live in await asyncio.gather(*(one(u) for u in list(w.users))):
        if live is None or user not in w.users:
            continue
        if live and not w.users[user]:
            log.info("@%s went live", user)
            await w.say(f"🔴 @{user} صار لايف على تيك توك!\nhttps://www.tiktok.com/@{user}/live")
        if live != w.users[user]:
            w.users[user] = live
            changed = True
    if changed:
        await w.save()


async def session(seconds):
    deadline = time.monotonic() + seconds
    next_check = 0.0
    offset = None
    async with httpx.AsyncClient() as http:
        w = Watch(http)
        await w.load()
        log.info("watching %s", list(w.users))
        while (left := deadline - time.monotonic()) > 1:
            try:
                r = await http.post(
                    f"{API}/getUpdates",
                    json={"offset": offset, "timeout": int(min(25, left)), "allowed_updates": ["message", "callback_query"]},
                    timeout=45,
                )
                updates = r.json().get("result", [])
            except httpx.HTTPError as e:
                log.warning("getUpdates failed: %s", e)
                await asyncio.sleep(3)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    if "message" in u:
                        await handle_message(w, u["message"])
                    elif "callback_query" in u:
                        await handle_callback(w, u["callback_query"])
                except Exception:
                    log.exception("update handling failed")
            if time.monotonic() >= next_check:
                await check_live(w)
                next_check = time.monotonic() + POLL_SECONDS
        if offset:  # confirm handled updates so the next session skips them
            await http.post(f"{API}/getUpdates", json={"offset": offset, "timeout": 0})


if __name__ == "__main__":
    secs = int(sys.argv[sys.argv.index("--session") + 1]) if "--session" in sys.argv else 270
    asyncio.run(session(secs))

"""TikTok live -> Telegram notifier with a button-driven control panel.

One invocation is a "session" (default ~4.5 min) started by a GitHub Actions
cron every 5 minutes. During a session the bot long-polls Telegram, so menu
taps and messages are answered within seconds, and checks TikTok every
POLL_SECONDS.

All state (watch list, per-account flags, settings) lives in one pinned
message of the owner's chat, so nothing private is stored in the repo.
"""
import asyncio
import json
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
HEADER = "📋 قائمة المراقبة (لا تحذف هالرسالة المثبّتة)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("tiktok-live-bot")


def fmt_dur(sec):
    m = max(int(sec // 60), 1)
    h, m = divmod(m, 60)
    return f"{h}س {m}د" if h else f"{m}د"


def extract_user(text):
    m = re.search(r"tiktok\.com/@([\w.]+)", text) or re.search(r"^@?([\w.]{2,24})$", text.strip())
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


def btn(text, data=None, url=None):
    b = {"text": text}
    b["url" if url else "callback_data"] = url or data
    return b


class Bot:
    def __init__(self, http):
        self.http = http
        self.users: dict[str, dict] = {}
        self.settings = {"pause": False, "end": True}
        self.message_id = None
        self._saved = None
        self.checked_at = None

    # ---- Telegram ------------------------------------------------------
    async def tg(self, method, timeout=30, **params):
        r = await self.http.post(f"{API}/{method}", json=params, timeout=timeout)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"{method}: {data.get('description')}")
        return data["result"]

    async def send(self, text, kb=None):
        p = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if kb:
            p["reply_markup"] = {"inline_keyboard": kb}
        return await self.tg("sendMessage", **p)

    async def edit(self, message_id, text, kb=None):
        p = {"chat_id": CHAT_ID, "message_id": message_id, "text": text, "parse_mode": "HTML",
             "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": kb or []}}
        try:
            await self.tg("editMessageText", **p)
        except RuntimeError as e:
            if "not modified" not in str(e):
                raise

    async def show(self, message_id, text, kb):
        """Edit the panel in place when tapped from a button, else send a new one."""
        if message_id:
            await self.edit(message_id, text, kb)
        else:
            await self.send(text, kb)

    async def setup_profile(self):
        try:
            await self.tg("setMyCommands", commands=[
                {"command": "menu", "description": "لوحة التحكم"},
                {"command": "list", "description": "حساباتي"},
                {"command": "live", "description": "مين لايف الآن"},
                {"command": "add", "description": "إضافة حساب"},
                {"command": "help", "description": "مساعدة"},
            ])
            await self.tg("setMyShortDescription", short_description="ينبهك لما حسابات تيك توك تبدأ بث مباشر 🔴")
            await self.tg("setMyDescription", description="أضف أي حساب تيك توك وبوصلك إشعار فوري لما يبدأ بث مباشر، "
                          "مع زر مباشر لمشاهدة البث. تحكم كامل بالأزرار: إضافة، كتم، حذف، وإعدادات.")
        except Exception as e:
            log.warning("profile setup skipped: %s", e)

    # ---- state in pinned message ---------------------------------------
    def new_user(self):
        return {"live": False, "since": None, "mute": False, "last": None}

    def render_state(self):
        lines = [HEADER] + [f"{'🔴' if v['live'] else '⚪'} {u}{' 🔕' if v['mute'] else ''}" for u, v in self.users.items()]
        if not self.users:
            lines.append("(فاضية)")
        lines.append("DATA " + json.dumps({"u": self.users, "s": self.settings}, separators=(",", ":")))
        return "\n".join(lines)

    async def load(self):
        pinned = (await self.tg("getChat", chat_id=CHAT_ID)).get("pinned_message") or {}
        text = pinned.get("text", "")
        if text.startswith("📋 قائمة المراقبة"):
            self.message_id = pinned["message_id"]
            self._saved = text
            m = re.search(r"^DATA (\{.*\})$", text, re.M)
            if m:
                data = json.loads(m.group(1))
                self.users, self.settings = data["u"], {**self.settings, **data["s"]}
            else:  # old format: just emoji + name lines
                for em, u in re.findall(r"^(🔴|⚪) (\S+)$", text, re.M):
                    self.users[u] = {**self.new_user(), "live": em == "🔴"}
            for v in self.users.values():
                for k, d in self.new_user().items():
                    v.setdefault(k, d)
            return
        self.users = {u: self.new_user() for u in SEED_USERS}
        msg = await self.tg("sendMessage", chat_id=CHAT_ID, text=self.render_state())
        self.message_id, self._saved = msg["message_id"], self.render_state()
        await self.tg("pinChatMessage", chat_id=CHAT_ID, message_id=self.message_id, disable_notification=True)

    async def save(self):
        text = self.render_state()
        if text != self._saved:
            await self.tg("editMessageText", chat_id=CHAT_ID, message_id=self.message_id, text=text)
            self._saved = text

    # ---- screens ---------------------------------------------------------
    def last_check_text(self):
        if not self.checked_at:
            return "—"
        age = time.time() - self.checked_at
        return "قبل ثواني" if age < 60 else f"قبل {fmt_dur(age)}"

    def main_screen(self):
        live = sum(v["live"] for v in self.users.values())
        status = "⏸ موقوفة" if self.settings["pause"] else "🔔 مفعّلة"
        text = (f"🎛 <b>لوحة التحكم</b>\n\n👥 الحسابات: <b>{len(self.users)}</b>\n"
                f"🔴 لايف الآن: <b>{live}</b>\n🔔 الإشعارات: {status}\n"
                f"🕒 آخر فحص: {self.last_check_text()}")
        kb = [[btn("➕ إضافة حساب", "ad"), btn("📋 حساباتي", "l")],
              [btn("🔴 مين لايف الآن", "lv"), btn("⚙️ الإعدادات", "s")],
              [btn("🔄 افحص الآن", "ck"), btn("❓ مساعدة", "h")]]
        return text, kb

    def list_screen(self):
        if not self.users:
            return "📋 <b>حساباتي</b>\n\nالقائمة فاضية. ابعتلي اسم حساب أو رابطه لأضيفه.", \
                   [[btn("➕ إضافة حساب", "ad")], [btn("⬅️ رجوع", "m")]]
        kb = [[btn(f"{'🔴' if v['live'] else '⚪'} @{u}{' 🔕' if v['mute'] else ''}", f"a:{u}")] for u, v in self.users.items()]
        kb.append([btn("➕ إضافة حساب", "ad"), btn("⬅️ رجوع", "m")])
        return "📋 <b>حساباتي</b>\nاضغط على حساب للتحكم فيه:", kb

    def account_screen(self, u):
        v = self.users[u]
        if v["live"]:
            state = "🔴 <b>لايف الآن</b>" + (f" (من ~{fmt_dur(time.time() - v['since'])})" if v["since"] else "")
        else:
            state = "⚪ مش لايف"
            if v["last"]:
                state += f"\nآخر بث ملاحَظ: قبل {fmt_dur(time.time() - v['last'])}"
        text = f"👤 <b>@{u}</b>\n\nالحالة: {state}\nالإشعارات: {'🔕 مكتومة' if v['mute'] else '🔔 مفعّلة'}"
        links = [btn("🔗 الحساب", url=f"https://www.tiktok.com/@{u}")]
        if v["live"]:
            links.insert(0, btn("▶️ شاهد البث", url=f"https://www.tiktok.com/@{u}/live"))
        kb = [links,
              [btn("🔔 تفعيل الإشعارات" if v["mute"] else "🔕 كتم الإشعارات", f"mt:{u}")],
              [btn("🗑 حذف", f"dl:{u}"), btn("⬅️ رجوع", "l")]]
        return text, kb

    def live_screen(self):
        live = [u for u, v in self.users.items() if v["live"]]
        if not live:
            return "🔴 <b>لايف الآن</b>\n\nما في أي حساب من حساباتك لايف حالياً.", [[btn("🔄 افحص الآن", "ck"), btn("⬅️ رجوع", "m")]]
        kb = [[btn(f"▶️ @{u}", url=f"https://www.tiktok.com/@{u}/live")] for u in live]
        kb.append([btn("🔄 افحص الآن", "ck"), btn("⬅️ رجوع", "m")])
        return f"🔴 <b>لايف الآن ({len(live)})</b>", kb

    def settings_screen(self):
        s = self.settings
        text = ("⚙️ <b>الإعدادات</b>\n\n"
                f"🔔 الإشعارات: {'⏸ موقوفة مؤقتاً' if s['pause'] else 'مفعّلة'}\n"
                f"🏁 إشعار انتهاء البث: {'✅ مفعّل' if s['end'] else '❌ مطفي'}")
        kb = [[btn("▶️ تشغيل الإشعارات" if s["pause"] else "⏸ إيقاف مؤقت للكل", "sp")],
              [btn("🏁 إشعار الانتهاء: " + ("✅" if s["end"] else "❌"), "se")],
              [btn("⬅️ رجوع", "m")]]
        return text, kb

    HELP = ("❓ <b>مساعدة</b>\n\n"
            "• <b>إضافة:</b> ابعتلي اسم الحساب أو رابطه (@username). ممكن عدة حسابات مع بعض.\n"
            "• <b>كتم:</b> كل حساب له زر كتم بدون حذفه.\n"
            "• <b>إيقاف مؤقت:</b> من الإعدادات توقف كل الإشعارات.\n"
            "• أوامر: /menu /list /live /add /remove\n\n"
            "⏱ الفحص كل دقيقة تقريباً، فالإشعار ممكن يتأخر شوي.")

    # ---- actions ---------------------------------------------------------
    async def add_users(self, text):
        names = [extract_user(t) for t in re.split(r"[\s,،]+", text) if t.strip()]
        added, dup, missing, bad = [], [], [], 0
        for u in dict.fromkeys(n for n in names if n):
            if u in self.users:
                dup.append(u)
            elif await user_exists(u):
                self.users[u] = self.new_user()
                added.append(u)
            else:
                missing.append(u)
        bad = sum(1 for n in names if not n)
        if not (added or dup or missing):
            return await self.send("ما فهمت اسم الحساب. ابعته هيك: @username أو رابط تيك توك")
        await self.save()
        parts = []
        if added:
            parts.append("✅ أضفت: " + "، ".join(f"@{u}" for u in added))
        if dup:
            parts.append("↪️ موجود أصلاً: " + "، ".join(f"@{u}" for u in dup))
        if missing:
            parts.append("⚠️ ما لقيت على تيك توك: " + "، ".join(f"@{u}" for u in missing))
        if bad:
            parts.append(f"❔ تجاهلت {bad} نص غير مفهوم")
        await self.send("\n".join(parts))
        await self.show(None, *self.list_screen())

    async def on_message(self, msg):
        if msg["chat"]["id"] != CHAT_ID or not msg.get("text"):
            return
        text = msg["text"].strip()
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@")[0].lower() if text.startswith("/") else ""
        if cmd in ("/start", "/menu"):
            await self.show(None, *self.main_screen())
        elif cmd == "/list":
            await self.show(None, *self.list_screen())
        elif cmd == "/live":
            await self.show(None, *self.live_screen())
        elif cmd == "/help":
            await self.send(self.HELP, [[btn("⬅️ القائمة", "m")]])
        elif cmd == "/remove":
            u = extract_user(rest)
            if u in self.users:
                del self.users[u]
                await self.save()
                await self.send(f"🗑 حذفت @{u}")
            else:
                await self.send("ما لقيت هاد الحساب بالقائمة. جرّب /list")
        elif cmd == "/add" and not rest.strip():
            await self.send("ابعتلي اسم الحساب أو رابطه (@username). ممكن عدة حسابات بنفس الرسالة.")
        else:
            await self.add_users(rest if cmd == "/add" else text)

    async def on_callback(self, cb):
        if cb["message"]["chat"]["id"] != CHAT_ID:
            return
        mid, data = cb["message"]["message_id"], cb.get("data", "")
        key, _, arg = data.partition(":")
        toast = None
        if key == "ck":
            await self.tg("answerCallbackQuery", callback_query_id=cb["id"], text="🔄 جاري الفحص...")
            await self.check_live()
            on_live_screen = cb["message"].get("text", "").startswith("🔴")
            await self.edit(mid, *(self.live_screen() if on_live_screen else self.main_screen()))
            return
        if key == "ad":
            await self.send("➕ ابعتلي اسم الحساب أو رابطه (@username). ممكن عدة حسابات بنفس الرسالة.")
            return await self.tg("answerCallbackQuery", callback_query_id=cb["id"])
        if key == "mt" and arg in self.users:
            v = self.users[arg]
            v["mute"] = not v["mute"]
            toast = "🔕 تم الكتم" if v["mute"] else "🔔 تم تفعيل الإشعارات"
            await self.save()
        elif key == "dy" and arg in self.users:
            del self.users[arg]
            await self.save()
            toast = f"🗑 تم حذف @{arg}"
            key = "l"
        elif key == "sp":
            self.settings["pause"] = not self.settings["pause"]
            await self.save()
            toast = "⏸ موقوفة" if self.settings["pause"] else "▶️ مفعّلة"
        elif key == "se":
            self.settings["end"] = not self.settings["end"]
            await self.save()
        if key in ("a", "mt") and arg in self.users:
            screen = self.account_screen(arg)
        elif key == "dl" and arg in self.users:
            screen = (f"🗑 متأكد بدك تحذف <b>@{arg}</b>؟",
                      [[btn("✅ نعم، احذف", f"dy:{arg}"), btn("↩️ لا", f"a:{arg}")]])
        elif key in ("l", "dy", "a", "mt"):
            screen = self.list_screen()
        elif key == "lv":
            screen = self.live_screen()
        elif key in ("s", "sp", "se"):
            screen = self.settings_screen()
        elif key == "h":
            screen = (self.HELP, [[btn("⬅️ رجوع", "m")]])
        else:
            screen = self.main_screen()
        await self.tg("answerCallbackQuery", callback_query_id=cb["id"], **({"text": toast} if toast else {}))
        await self.edit(mid, *screen)

    async def check_live(self):
        async def one(user):
            try:
                return user, await TikTokLiveClient(unique_id=f"@{user}").is_live()
            except Exception as e:
                log.warning("check failed for @%s: %s", user, e)
                return user, None  # keep previous flag

        changed = False
        results = await asyncio.gather(*(one(u) for u in list(self.users)))
        self.checked_at = time.time()
        for user, live in results:
            v = self.users.get(user)
            if live is None or v is None or live == v["live"]:
                continue
            quiet = v["mute"] or self.settings["pause"]
            now = time.time()
            if live:
                log.info("@%s went live", user)
                v["live"], v["since"] = True, int(now)
                if not quiet:
                    await self.send(f"🔴 <b>@{user}</b> بدأ بث مباشر الآن!",
                                    [[btn("▶️ شاهد البث", url=f"https://www.tiktok.com/@{user}/live")]])
            else:
                log.info("@%s ended the stream", user)
                dur = f"\nالمدة: ~{fmt_dur(now - v['since'])}" if v["since"] else ""
                v["live"], v["last"], v["since"] = False, int(now), None
                if self.settings["end"] and not quiet:
                    await self.send(f"🏁 انتهى بث <b>@{user}</b>{dur}")
            changed = True
        if changed:
            await self.save()


async def session(seconds):
    deadline = time.monotonic() + seconds
    next_check, offset = 0.0, None
    async with httpx.AsyncClient() as http:
        bot = Bot(http)
        await bot.load()
        await bot.setup_profile()
        log.info("watching %s", list(bot.users))
        while (left := deadline - time.monotonic()) > 1:
            try:
                r = await http.post(f"{API}/getUpdates", timeout=45, json={
                    "offset": offset, "timeout": int(min(25, left)),
                    "allowed_updates": ["message", "callback_query"]})
                updates = r.json().get("result", [])
            except httpx.HTTPError as e:
                log.warning("getUpdates failed: %s", e)
                await asyncio.sleep(3)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    if "message" in u:
                        await bot.on_message(u["message"])
                    elif "callback_query" in u:
                        await bot.on_callback(u["callback_query"])
                except Exception:
                    log.exception("update handling failed")
            if time.monotonic() >= next_check:
                await bot.check_live()
                next_check = time.monotonic() + POLL_SECONDS
        if offset:  # confirm handled updates so the next session skips them
            await http.post(f"{API}/getUpdates", json={"offset": offset, "timeout": 0})


if __name__ == "__main__":
    secs = int(sys.argv[sys.argv.index("--session") + 1]) if "--session" in sys.argv else 270
    asyncio.run(session(secs))

"""Records TikTok live streams for accounts flagged "record" in the bot panel.

Runs on the owner's PC (needs ffmpeg and to stay on). It only READS the watch
list from the bot's pinned Telegram message, so it never competes with the
GitHub-hosted bot for Telegram updates.

    python recorder.py

Recordings land in REC_DIR/<username>/<date>_p1.mp4 (extra parts only if the
stream connection drops and comes back).
"""
import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from TikTokLive import TikTokLiveClient

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
REC_DIR = Path(os.getenv("REC_DIR", r"D:\تسجيلات-تيك-توك"))
FFMPEG = os.getenv("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
POLL = int(os.getenv("REC_POLL_SECONDS", "30"))
SEND = os.getenv("REC_SEND", "1") != "0"                       # upload finished recordings to Telegram
SEND_MAX_MB = int(os.getenv("REC_SEND_MAX_MB", "1024"))        # bigger recordings are only announced
TG_LIMIT = 49 * 1024 * 1024                                     # Bot API upload cap is 50 MB
CHUNK_TARGET = 40 * 1024 * 1024
QUALITIES = ("origin", "uhd", "hd", "sd", "ld")
API = f"https://api.telegram.org/bot{TOKEN}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0 Safari/537.36"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("recorder")


def fmt_dur(sec):
    m = max(int(sec // 60), 1)
    h, m = divmod(m, 60)
    return f"{h}س {m}د" if h else f"{m}د"


def fmt_size(path_list):
    mb = sum(p.stat().st_size for p in path_list if p.exists()) / 1_048_576
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.0f} MB" if mb >= 1 else "<1 MB"


def ffprobe():
    return shutil.which("ffprobe") or str(Path(FFMPEG).with_name("ffprobe.exe"))


async def duration_of(path: Path):
    proc = await asyncio.create_subprocess_exec(
        ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    return float(out.decode().strip())


async def split_for_telegram(path: Path):
    """Return [path] if it fits Telegram's 50 MB cap, else cut it into smaller mp4 parts."""
    size = path.stat().st_size
    if size <= TG_LIMIT:
        return [path]
    seg = await duration_of(path) * CHUNK_TARGET / size
    parts = []
    for _ in range(4):  # keyframe cuts are uneven: shrink the segment length until every part fits
        for old in path.parent.glob(f"{path.stem}_part*.mp4"):
            old.unlink()
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-y", "-loglevel", "error", "-i", str(path), "-c", "copy", "-f", "segment",
            "-segment_time", f"{seg:.1f}", "-reset_timestamps", "1", "-movflags", "+faststart",
            str(path.with_name(path.stem + "_part%02d.mp4")))
        await proc.wait()
        parts = sorted(path.parent.glob(f"{path.stem}_part*.mp4"))
        if parts and all(p.stat().st_size <= TG_LIMIT for p in parts):
            return parts
        seg *= 0.7
    return parts


async def send_file(http, path: Path, caption):
    for method, field in (("sendVideo", "video"), ("sendDocument", "document")):
        with open(path, "rb") as f:
            r = await http.post(
                f"{API}/{method}", timeout=900,
                data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML", "supports_streaming": "true"},
                files={field: (path.name, f, "video/mp4")})
        data = r.json()
        if data.get("ok"):
            return
        log.warning("%s failed for %s: %s", method, path.name, data.get("description"))
    raise RuntimeError(f"Telegram refused {path.name}: {data.get('description')}")


async def deliver(http, notify, user, file: Path):
    """Upload a finished recording (split into <50 MB parts when needed) and tidy up the parts."""
    size_mb = file.stat().st_size / 1_048_576
    if not SEND:
        return
    if size_mb > SEND_MAX_MB:
        return await notify(f"📦 تسجيل @{user} كبير ({size_mb / 1024:.1f} GB) فما رفعته على تلجرام. موجود على جهازك.")
    parts = await split_for_telegram(file)
    for i, part in enumerate(parts, 1):
        label = f" · جزء {i}/{len(parts)}" if len(parts) > 1 else ""
        await send_file(http, part, f"🎥 @{user} · {file.stem[:16].replace('_', ' ')}{label}")
        if part != file:
            part.unlink()


async def resolve_stream(user):
    """Return the best direct stream URL for a currently-live user."""
    client = TikTokLiveClient(unique_id=f"@{user}")
    if os.getenv("TIKTOK_SESSIONID"):  # only needed for age-restricted streams
        client.web.set_session(os.environ["TIKTOK_SESSIONID"], os.getenv("TIKTOK_TT_TARGET_IDC"))
    room_id = int(await client.web.fetch_room_id_from_api(user))
    info = await client.web.fetch_room_info(room_id=room_id)
    data = json.loads(info["stream_url"]["live_core_sdk_data"]["pull_data"]["stream_data"])["data"]
    for q in QUALITIES:
        main = (data.get(q) or {}).get("main") or {}
        url = main.get("flv") or main.get("hls")
        if url:
            return url
    raise RuntimeError("no stream url in room info")


async def is_live(user):
    return await TikTokLiveClient(unique_id=f"@{user}").is_live()


async def run_ffmpeg(url, path, stop: asyncio.Event):
    """Copy the stream to `path` until it ends or `stop` is set."""
    net = ["-user_agent", UA, "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"] \
        if url.startswith("http") else []
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-y", "-loglevel", "error", *net, "-i", url, "-c", "copy", str(path),
        stdin=asyncio.subprocess.PIPE)
    waiter = asyncio.create_task(proc.wait())
    stopper = asyncio.create_task(stop.wait())
    await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
    stopper.cancel()
    if proc.returncode is None:  # asked to stop: let ffmpeg finalise the file
        try:
            proc.stdin.write(b"q")
            await proc.stdin.drain()
            await asyncio.wait_for(proc.wait(), 20)
        except Exception:
            proc.kill()
            await proc.wait()
    await waiter


async def remux(mkv: Path):
    """mkv -> mp4 (stream copy). Keeps the mkv if anything goes wrong."""
    mp4 = mkv.with_suffix(".mp4")
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-y", "-loglevel", "error", "-i", str(mkv), "-c", "copy", "-movflags", "+faststart", str(mp4))
    await proc.wait()
    if proc.returncode == 0 and mp4.exists() and mp4.stat().st_size > 0:
        mkv.unlink()
        return mp4
    return mkv


async def record_stream(user, stop, notify, resolve=resolve_stream, live_check=is_live, out_root=None, send=None):
    out_dir = (out_root or REC_DIR) / user
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    started, parts, part = time.time(), [], 1
    await notify(f"🎥 بدأ تسجيل بث <b>@{user}</b>")
    while True:
        url = await resolve(user)
        path = out_dir / f"{stamp}_p{part}.mkv"
        await run_ffmpeg(url, path, stop)
        if path.exists() and path.stat().st_size > 0:
            parts.append(path)
        if stop.is_set():
            break
        await asyncio.sleep(5)  # ffmpeg ended on its own: stream over, or a drop?
        try:
            if not await live_check(user):
                break
        except Exception:
            break
        part += 1
    files = [await remux(p) for p in parts]
    if files:
        names = "\n".join(f"<code>{f}</code>" for f in files)
        await notify(f"✅ انتهى تسجيل <b>@{user}</b>\nالمدة: ~{fmt_dur(time.time() - started)} · الحجم: {fmt_size(files)}\n{names}")
        for f in files:
            if send:
                try:
                    await send(user, f)
                except Exception as e:
                    log.exception("upload failed")
                    await notify(f"⚠️ ما قدرت أرفع {f.name} على تلجرام: {e}")
    else:
        await notify(f"⚠️ ما انحفظ أي ملف لتسجيل @{user}")
    return files


async def read_record_list(http):
    chat = (await http.post(f"{API}/getChat", json={"chat_id": CHAT_ID})).json().get("result", {})
    m = re.search(r"^DATA (\{.*\})$", (chat.get("pinned_message") or {}).get("text", ""), re.M)
    if not m:
        return set()
    return {u for u, v in json.loads(m.group(1))["u"].items() if v.get("rec")}


async def main():
    tasks: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}
    cooldown: dict[str, float] = {}  # user -> don't retry before this time (after a crash)
    async with httpx.AsyncClient(timeout=30) as http:
        async def notify(text):
            try:
                await http.post(f"{API}/sendMessage", json={
                    "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
            except httpx.HTTPError as e:
                log.warning("notify failed: %s", e)

        log.info("recorder started, saving to %s", REC_DIR)
        while True:
            try:
                wanted = await read_record_list(http)
            except Exception as e:
                log.warning("could not read watch list: %s", e)
                wanted = None
            for user in list(tasks):
                task, stop = tasks[user]
                if task.done():
                    if task.exception():
                        log.error("recording @%s crashed: %r", user, task.exception())
                        err = str(task.exception())
                        hint = ("\nالبث مقيّد بالعمر: لازم TIKTOK_SESSIONID في ملف .env (شوف README)"
                                if "Age restricted" in err else "")
                        await notify(f"❌ تعطل تسجيل @{user}: {err[:200]}{hint}\nبعيد المحاولة بعد 10 دقائق.")
                        cooldown[user] = time.time() + 600
                    del tasks[user]
                elif wanted is not None and user not in wanted:
                    stop.set()  # recording switched off in the panel
            for user in (wanted or ()):
                if user in tasks or time.time() < cooldown.get(user, 0):
                    continue
                try:
                    if await is_live(user):
                        stop = asyncio.Event()
                        tasks[user] = (asyncio.create_task(record_stream(
                            user, stop, notify, send=lambda u, f: deliver(http, notify, u, f))), stop)
                except Exception as e:
                    log.warning("check failed for @%s: %s", user, e)
            await asyncio.sleep(POLL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

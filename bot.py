import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote, urlparse
import aiohttp
from pyrogram import Client, filters, idle
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
GOFILE_TOKEN = os.environ.get("GOFILE_TOKEN", "").strip()
OWNER_USERNAME = "@little_brother_0"
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
RETENTION_SECONDS = 4 * 3600

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

app = Client(
    "audio_rename_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

sessions = {}
last_progress = {}
last_msg_text = {}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Asplayz Track Editor Alive")

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.environ.get("PORT", "7860"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def fmt_size(num_bytes: float) -> str:
    val = float(max(0.0, num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0:
            return f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{val:.2f} PB"


def make_bar(pct: float, width: int = 12) -> str:
    filled = max(0, min(width, int(round((pct / 100.0) * width))))
    return "■" * filled + "□" * (width - filled)


def sanitize_title(raw: str) -> str:
    return re.sub(r"\s*\[[^\]]*\]\s*$", "", raw.strip()).strip()


def extract_filename_from_url(url: str) -> str:
    name = os.path.basename(unquote(urlparse(url).path or ""))
    return name if name and "." in name else "video_download.mkv"


def cancel_active_task(sess: dict):
    sess["cancelled"] = True
    task = sess.get("active_task")
    if task and not task.done():
        task.cancel()
    proc = sess.get("proc")
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except Exception:
            pass


def cleanup_paths(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def cleanup_session(uid: int):
    sess = sessions.pop(uid, None)
    if not sess:
        return
    cancel_active_task(sess)
    paths = [sess.get("path"), sess.get("out_path")]
    for item in sess.get("added_subs", []):
        paths.append(item.get("path"))
    cleanup_paths(*paths)


async def periodic_disk_sweeper():
    while True:
        await asyncio.sleep(900)
        now = time.time()
        active_paths = set()
        for s in list(sessions.values()):
            for p in (s.get("path"), s.get("out_path")):
                if p:
                    active_paths.add(os.path.abspath(p))
            for sub in s.get("added_subs", []):
                if sub.get("path"):
                    active_paths.add(os.path.abspath(sub["path"]))
        for folder in ("downloads", "out_files"):
            if not os.path.isdir(folder):
                continue
            for entry in os.listdir(folder):
                full_p = os.path.abspath(os.path.join(folder, entry))
                if full_p in active_paths:
                    continue
                try:
                    if (now - os.path.getmtime(full_p)) > RETENTION_SECONDS:
                        os.remove(full_p)
                except OSError:
                    pass


async def safe_edit(msg, text: str, reply_markup=None):
    prev = last_msg_text.get(msg.id)
    if prev == text:
        return
    last_msg_text[msg.id] = text
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except MessageNotModified:
        pass
    except Exception:
        pass


async def update_progress_msg(msg, key: str, title: str, current: int, total: int, start_ts: float):
    now = time.monotonic()
    if (now - last_progress.get(key, 0.0)) < 2.5:
        return
    last_progress[key] = now
    speed = current / max(now - start_ts, 0.1)
    if total > 0:
        pct = min(100.0, (current / total) * 100.0)
        body = f"`[{make_bar(pct)}]` **{pct:.1f}%**\nSize: `{fmt_size(current)}` / `{fmt_size(total)}`"
    else:
        body = f"`[{make_bar(0.0)}]`\nProcessed: `{fmt_size(current)}`"
    text = f"**{title}**\n{body}\nSpeed: `{fmt_size(speed)}/s`"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="cancel_job")]])
    await safe_edit(msg, text, reply_markup=kb)


class StreamFileReader:
    def __init__(self, file_path: str, on_chunk):
        self.file_path = file_path
        self.on_chunk = on_chunk
        self.sent = 0
        self.total = os.path.getsize(file_path)

    async def __aiter__(self):
        with open(self.file_path, "rb") as f:
            while True:
                data = f.read(1024 * 1024)
                if not data:
                    break
                self.sent += len(data)
                await self.on_chunk(self.sent, self.total)
                yield data


async def upload_to_gofile(file_path: str, file_name: str, on_progress) -> str:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)
    headers = {}
    if GOFILE_TOKEN:
        headers["Authorization"] = f"Bearer {GOFILE_TOKEN}"

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as http:
        server = "store1"
        try:
            async with http.get("https://api.gofile.io/servers") as srv_resp:
                if srv_resp.status == 200:
                    srv_json = await srv_resp.json()
                    servers = (srv_json.get("data") or {}).get("servers") or []
                    if servers and servers[0].get("name"):
                        server = servers[0]["name"]
        except Exception:
            pass

        upload_url = f"https://{server}.gofile.io/contents/uploadfile"
        form = aiohttp.FormData()
        reader = StreamFileReader(file_path, on_progress)
        form.add_field(
            "file",
            reader,
            filename=file_name,
            content_type="application/octet-stream",
        )
        async with http.post(upload_url, data=form) as up_resp:
            raw_text = await up_resp.text()
            if up_resp.status not in (200, 201):
                raise RuntimeError(f"Gofile HTTP {up_resp.status}: {raw_text[:200]}")
            res = json.loads(raw_text)
            if res.get("status") != "ok":
                raise RuntimeError(f"Gofile error: {res}")
            data = res.get("data") or {}
            link = data.get("downloadPage") or data.get("directLink")
            if not link:
                raise RuntimeError("Gofile did not return a download link.")
            return link


def get_media_streams(file_path: str):
    cmd_a = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index:stream_tags=title,language", "-of", "json", file_path,
    ]
    res_a = subprocess.run(cmd_a, capture_output=True, text=True, check=True)
    audios = [
        {
            "idx": i,
            "title": (st.get("tags") or {}).get("title") or f"Audio {i + 1}",
            "lang": (st.get("tags") or {}).get("language") or "und",
        }
        for i, st in enumerate(json.loads(res_a.stdout).get("streams", []))
    ]

    cmd_s = [
        "ffprobe", "-v", "error", "-select_streams", "s",
        "-show_entries", "stream=index:stream_tags=title,language", "-of", "json", file_path,
    ]
    res_s = subprocess.run(cmd_s, capture_output=True, text=True, check=True)
    subs = [
        {
            "idx": i,
            "title": (st.get("tags") or {}).get("title") or f"Sub {i + 1}",
            "lang": (st.get("tags") or {}).get("language") or "und",
        }
        for i, st in enumerate(json.loads(res_s.stdout).get("streams", []))
    ]
    return audios, subs


def build_menu_view(uid: int, view: str = "main", sub_idx: int = 0):
    sess = sessions[uid]
    _, orig_ext = os.path.splitext(sess["name"])
    orig_ext = (orig_ext or ".mkv").lower()
    _, out_ext = os.path.splitext(sess["out_name"])
    out_ext = (out_ext or orig_ext).lower()

    active_audios = len(sess["audios"]) - len(sess["removed_audios"])
    active_subs = (len(sess["subs"]) - len(sess["removed_subs"])) + len(sess["added_subs"])

    header = (
        f"**File:** `{sess['name']}`\n"
        f"**Output:** `{sess['out_name']}` (`{out_ext.upper()}`)\n"
        f"**Audio Tracks:** `{active_audios}/{len(sess['audios'])}` | "
        f"**Subtitles:** `{active_subs}`\n\n"
        "⚠️ **Note:** You can edit only **one file at a time**. "
        "If you send a second file or link, the current file and progress will be deleted."
    )

    if view == "ren_audio":
        lines = [header, "", "**Select an audio track to rename:**"]
        buttons = []
        for a in sess["audios"]:
            idx = a["idx"]
            cur = sess["renamed_audios"].get(idx, a["title"])
            tag = " (Removed)" if idx in sess["removed_audios"] else ""
            buttons.append([
                InlineKeyboardButton(
                    f"Track {idx + 1}: {cur} ({a['lang']}){tag}",
                    callback_data=f"ren_pick:{idx}",
                )
            ])
        if not buttons:
            lines.append("• No audio tracks found in this file.")
        buttons.append([InlineKeyboardButton("Back", callback_data="nav:main")])
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    if view == "fmt":
        target_ext = ".mp4" if orig_ext == ".mkv" else ".mkv"
        target_label = f"Convert to {target_ext.upper().lstrip('.')}"
        lines = [
            header,
            "",
            f"**Original Format:** `{orig_ext.upper()}`",
            f"**Selected Output Format:** `{out_ext.upper()}`",
        ]
        buttons = [[InlineKeyboardButton(target_label, callback_data=f"set_ext:{target_ext}")]]
        if out_ext != orig_ext:
            buttons.append([InlineKeyboardButton(f"Keep Original ({orig_ext.upper()})", callback_data=f"set_ext:{orig_ext}")])
        buttons.append([InlineKeyboardButton("Back", callback_data="nav:main")])
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    if view == "subs":
        lines = [header, "", "**Subtitles:**"]
        buttons = []
        for s in sess["subs"]:
            idx = s["idx"]
            st = " [Deleted]" if idx in sess["removed_subs"] else ""
            buttons.append([
                InlineKeyboardButton(
                    f"Sub {idx + 1}: {s['title']} ({s['lang']}){st}",
                    callback_data=f"sub_item:{idx}",
                )
            ])
        for i, added in enumerate(sess["added_subs"]):
            buttons.append([
                InlineKeyboardButton(
                    f"+ Added Sub {i + 1}: {added['title']} (Remove)",
                    callback_data=f"sub_rm_added:{i}",
                )
            ])
        if not sess["subs"] and not sess["added_subs"]:
            lines.append("• No embedded subtitles found.")
        buttons.append([InlineKeyboardButton("+ Add Subtitle", callback_data="sub_add")])
        buttons.append([InlineKeyboardButton("Back", callback_data="nav:main")])
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    if view == "sub_detail":
        s = sess["subs"][sub_idx]
        is_del = sub_idx in sess["removed_subs"]
        lines = [
            header,
            "",
            f"**Subtitle {sub_idx + 1}:** `{s['title']}` (`{s['lang']}`)",
            f"**Status:** `{'Marked for Delete' if is_del else 'Kept'}`",
        ]
        buttons = [
            [
                InlineKeyboardButton("Export (.srt)", callback_data=f"sub_exp:{sub_idx}"),
                InlineKeyboardButton("Restore" if is_del else "Delete", callback_data=f"sub_del:{sub_idx}"),
            ],
            [InlineKeyboardButton("Back", callback_data="nav:subs")],
        ]
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    if view == "rm_audio":
        lines = [header, "", "**Select audio tracks to remove or restore:**"]
        buttons = []
        for a in sess["audios"]:
            idx = a["idx"]
            cur = sess["renamed_audios"].get(idx, a["title"])
            is_rm = idx in sess["removed_audios"]
            buttons.append([
                InlineKeyboardButton(f"Track {idx + 1}: {cur} ({a['lang']})", callback_data=f"noop:{idx}"),
                InlineKeyboardButton("Restore" if is_rm else "Remove", callback_data=f"rm_tog:{idx}"),
            ])
        if not buttons:
            lines.append("• No audio tracks found in this file.")
        buttons.append([InlineKeyboardButton("Back", callback_data="nav:main")])
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    buttons = [
        [
            InlineKeyboardButton("Rename Audio", callback_data="nav:ren_audio"),
            InlineKeyboardButton("Format Convert", callback_data="nav:fmt"),
        ],
        [
            InlineKeyboardButton("Subtitles", callback_data="nav:subs"),
            InlineKeyboardButton("Remove Audio", callback_data="nav:rm_audio"),
        ],
        [
            InlineKeyboardButton("Save & Process", callback_data="run_job"),
            InlineKeyboardButton("Cancel", callback_data="cancel_job"),
        ],
    ]
    return header, InlineKeyboardMarkup(buttons)


async def refresh_menu(uid: int, chat_id: int, fallback_msg=None):
    sess = sessions.get(uid)
    if not sess:
        return
    text, markup = build_menu_view(uid, sess.get("view", "main"), sess.get("sub_idx", 0))
    if sess.get("box_msg_id"):
        try:
            await app.edit_message_text(chat_id=chat_id, message_id=sess["box_msg_id"], text=text, reply_markup=markup)
            return
        except Exception:
            pass
    if fallback_msg:
        sent = await fallback_msg.reply_text(text, reply_markup=markup)
        sess["box_msg_id"] = sent.id


def start_text(first_name: str) -> str:
    return (
        f"Hi **{first_name or 'User'}**\n\n"
        "I am **Asplayz Track Editor**.\n\n"
        "Send any video/MKV file or direct video link (up to **2 GB**) to rename audio tracks, "
        "remove audio tracks, manage subtitles, or convert video format (`MP4` / `MKV`), "
        "and receive your processed **Gofile** download link.\n\n"
        f"If any issues arise, contact 👉 **{OWNER_USERNAME}**"
    )


def start_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Functions", callback_data="info_funcs"),
            InlineKeyboardButton("Commands", callback_data="info_cmds"),
        ],
        [InlineKeyboardButton("Close", callback_data="close_start")],
    ])


def new_session(uid: int, fname: str, path: str, msg_id: int):
    sessions[uid] = {
        "name": fname,
        "out_name": fname,
        "path": path,
        "out_path": None,
        "audios": [],
        "subs": [],
        "renamed_audios": {},
        "removed_audios": set(),
        "removed_subs": set(),
        "added_subs": [],
        "pending_sub_path": None,
        "await_mode": None,
        "await_idx": None,
        "prompt_msg_id": None,
        "box_msg_id": msg_id,
        "view": "main",
        "sub_idx": 0,
        "cancelled": False,
        "active_task": None,
        "proc": None,
    }


@app.on_message(filters.command("start"))
async def start_cmd(_, message):
    first = message.from_user.first_name if message.from_user else "User"
    await message.reply_text(start_text(first), reply_markup=start_markup())


@app.on_message(filters.command("cancel"))
async def cancel_cmd(_, message):
    uid = message.from_user.id if message.from_user else 0
    if uid in sessions:
        cleanup_session(uid)
        await message.reply_text("❌ Your active processes have been cancelled and temporary files cleaned.")
    else:
        await message.reply_text("No active process to cancel.")


async def prepare_session_from_file(uid: int, fname: str, downloaded_path: str, status_msg):
    sess = sessions.get(uid)
    if not sess or sess.get("cancelled"):
        cleanup_paths(downloaded_path)
        return
    size = os.path.getsize(downloaded_path) if os.path.exists(downloaded_path) else 0
    if size > MAX_FILE_BYTES:
        await safe_edit(status_msg, f"❌ File exceeds the **2 GB** limit (`{fmt_size(size)}`).")
        cleanup_session(uid)
        return
    audios, subs = get_media_streams(downloaded_path)
    sess["name"] = fname
    sess["out_name"] = fname
    sess["path"] = downloaded_path
    sess["audios"] = audios
    sess["subs"] = subs
    sess["active_task"] = None
    sess["view"] = "main"
    text, markup = build_menu_view(uid, "main")
    await safe_edit(status_msg, text, reply_markup=markup)


@app.on_message(filters.video | filters.document)
async def file_handler(_, message):
    uid = message.from_user.id if message.from_user else 0
    media = message.video or message.document
    fname = media.file_name or "video.mkv"
    fsize = media.file_size or 0
    ext = os.path.splitext(fname)[1].lower()

    sess = sessions.get(uid)
    if sess and sess.get("await_mode") == "sub_file" and ext in (".srt", ".ass", ".vtt"):
        os.makedirs("downloads", exist_ok=True)
        sub_path = os.path.join("downloads", f"sub_{uid}_{int(time.time())}_{os.path.basename(fname)}")
        downloaded_sub = await message.download(file_name=sub_path)
        sess["pending_sub_path"] = downloaded_sub
        sess["await_mode"] = "sub_title"
        try:
            await message.delete()
        except Exception:
            pass
        if sess.get("prompt_msg_id"):
            try:
                await app.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=sess["prompt_msg_id"],
                    text="Send subtitle title/language (e.g. `Telugu`):",
                )
            except Exception:
                prompt = await message.reply_text("Send subtitle title/language (e.g. `Telugu`):")
                sess["prompt_msg_id"] = prompt.id
        return

    if fsize > MAX_FILE_BYTES:
        await message.reply_text(f"❌ File exceeds the **2 GB** limit (`{fmt_size(fsize)}`).")
        return

    if uid in sessions:
        cleanup_session(uid)

    os.makedirs("downloads", exist_ok=True)
    in_path = os.path.join("downloads", f"in_{uid}_{int(time.time())}_{os.path.basename(fname)}")
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="cancel_job")]])
    status_msg = await message.reply_text(
        f"**Downloading from Telegram...**\n`[{make_bar(0)}]` **0.0%**", reply_markup=cancel_kb
    )
    new_session(uid, fname, in_path, status_msg.id)
    dl_start = time.monotonic()
    prog_key = f"dl:{uid}:{dl_start}"

    async def on_dl_progress(current, total):
        s = sessions.get(uid)
        if not s or s.get("cancelled"):
            raise asyncio.CancelledError()
        await update_progress_msg(status_msg, prog_key, "Downloading from Telegram...", current, total or fsize, dl_start)

    async def do_tg_download():
        try:
            downloaded = await message.download(file_name=in_path, progress=on_dl_progress)
            await prepare_session_from_file(uid, fname, downloaded, status_msg)
        except asyncio.CancelledError:
            cleanup_paths(in_path)
            sessions.pop(uid, None)
        except Exception as e:
            s = sessions.get(uid)
            if s and not s.get("cancelled"):
                await safe_edit(statu

import asyncio, json, os, re, shutil, time
from urllib.parse import unquote, urlparse
import aiohttp
from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

API_ID = int(os.environ.get("API_ID", "25626879"))
API_HASH = os.environ.get("API_HASH", "6624a2b7d641819dc13e1693b663e556")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "9099811538:AAH_ffJ-s3AUsKAchEF7nY7vLr0EI7-q0mM")
TWO_GB = 2 * 1024 * 1024 * 1024

BASE_DIR = os.environ.get("ASPLAYZ_DIR", "/content/asplayz-track-editor")
DL_DIR = os.path.join(BASE_DIR, "downloads")
OUT_DIR = os.path.join(BASE_DIR, "outputs")

app = Client(
    "asplayz_track_editor_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=BASE_DIR,
)

sessions, user_prefs, last_progress, speed_state = {}, {}, {}, {}


def get_dest(uid):
    return user_prefs.get(uid, "gofile")


def set_dest(uid, d):
    user_prefs[uid] = "telegram" if d == "telegram" else "gofile"


def fmt_sz(n):
    if not n or n <= 0:
        return "0 B"
    u, i, v = ["B", "KB", "MB", "GB", "TB"], 0, float(n)
    while v >= 1024 and i < len(u) - 1:
        v /= 1024.0
        i += 1
    return f"{v:.2f} {u[i]}"


def bar(pct):
    p = max(0.0, min(100.0, pct))
    f = int(round(p / 100.0 * 12))
    return ("■" * f) + ("□" * (12 - f))


def clean_paths(*paths):
    for p in paths:
        if not p:
            continue
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)


def clean_sess(uid):
    s = sessions.pop(uid, None)
    if not s:
        return
    pr = s.get("proc")
    if pr and pr.returncode is None:
        pr.kill()
    clean_paths(s.get("path"), s.get("out_path"))
    for sub in s.get("added_subs") or []:
        clean_paths(sub.get("path"))


async def edit_msg(msg, txt, kb=None):
    if not msg:
        return
    try:
        await msg.edit_text(txt, reply_markup=kb)
    except MessageNotModified:
        pass
    except Exception as e:
        print(f"[edit_err] {e}", flush=True)


async def show_prog(msg, key, title, done, tot, pct):
    now = time.monotonic()
    st = speed_state.setdefault(key, {"t": now, "d": done, "spd": 0.0})
    dt = now - st["t"]
    if dt >= 0.35:
        st["spd"] = max(0.0, (done - st["d"]) / dt)
        st["t"] = now
        st["d"] = done
    if now - last_progress.get(key, 0.0) < 1.5 and pct < 100.0:
        return
    last_progress[key] = now
    spd_s = fmt_sz(int(st["spd"])) + "/s"
    tot_s = fmt_sz(tot) if tot > 0 else "Unknown"
    body = f"**{title}**\n`[{bar(pct)}]` **{pct:.1f}%**\nSize: `{fmt_sz(done)}` / `{tot_s}`\nSpeed: `{spd_s}`"
    await edit_msg(msg, body, InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="cancel_job")]]))


def url_fname(u):
    n = re.sub(r'[\\/:*?"<>|]+', "_", os.path.basename(unquote(urlparse(u).path or ""))).strip()
    if not n:
        n = f"video_{int(time.time())}.mkv"
    return n if "." in n else n + ".mkv"


async def download_from_url(uid, url, status_msg):
    sess = sessions.get(uid)
    if not sess:
        return
    raw_name = url_fname(url)
    os.makedirs(DL_DIR, exist_ok=True)
    out_path = os.path.join(DL_DIR, f"url_{uid}_{int(time.time())}_{raw_name}")
    sess["name"] = sess["out_name"] = raw_name
    sess["path"] = out_path
    pkey = f"dl_url_{uid}_{status_msg.id}"
    last_progress.pop(pkey, None)
    speed_state.pop(pkey, None)
    try:
        await edit_msg(
            status_msg,
            f"**Downloading...**\n`{raw_name}`",
            InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="cancel_job")]]),
        )
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
        headers = {"User-Agent": "Mozilla/5.0"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as http:
            async with http.get(url, allow_redirects=True) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}")
                cl = resp.headers.get("Content-Length")
                tot_sz = int(cl) if (cl and cl.isdigit() and int(cl) > 0) else 0
                if tot_sz > TWO_GB:
                    sess["dest"] = "gofile"
                written = 0
                speed_state[pkey] = {"t": time.monotonic(), "d": 0, "spd": 0.0}
                with open(out_path, "wb") as fp:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        if uid not in sessions or sessions[uid] is not sess:
                            raise RuntimeError("Cancelled")
                        if not chunk:
                            continue
                        fp.write(chunk)
                        written += len(chunk)
                        pct_now = min(99.5, (written / tot_sz * 100.0)) if tot_sz > 0 else 0.0
                        await show_prog(status_msg, pkey, f"Downloading: {raw_name}", written, tot_sz, pct_now)
                pct_done = 100.0 if tot_sz > 0 else 0.0
                await show_prog(status_msg, pkey, f"Downloading: {raw_name}", written, tot_sz or written, pct_done)
        if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
            raise RuntimeError("Empty file downloaded")
        if os.path.getsize(out_path) > TWO_GB:
            sess["dest"] = "gofile"
        await open_menu(uid, status_msg)
    except Exception as e:
        clean_paths(out_path)
        if uid in sessions and sessions[uid] is sess:
            await edit_msg(status_msg, f"Link failed: `{e}`")


def aud_order(s):
    t = len(s.get("audios") or [])
    o = s.get("audio_order")
    if not isinstance(o, list) or len(o) != t or set(o) != set(range(t)):
        s["audio_order"] = list(range(t))
    return s["audio_order"]


async def probe(path):
    p = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, _ = await p.communicate()
    d = json.loads(out.decode("utf-8", errors="ignore") or "{}")
    audios, subs, ai, si = [], [], 0, 0
    for st in d.get("streams") or []:
        ct = st.get("codec_type")
        tags = st.get("tags") or {}
        lang = tags.get("language") or "und"
        title = tags.get("title") or lang.upper()
        if ct == "audio":
            audios.append({"index": ai, "stream_index": st.get("index"), "title": title, "lang": lang})
            ai += 1
        elif ct == "subtitle":
            subs.append({"index": si, "stream_index": st.get("index"), "title": title, "lang": lang, "codec": (st.get("codec_name") or "").lower()})
            si += 1
    ext = os.path.splitext(path)[1].lstrip(".").lower() or "mkv"
    return {"audios": audios, "subs": subs, "ext": ext}


async def gofile_up(path, msg, uid):
    fsize = os.path.getsize(path)
    fname = os.path.basename(path)
    servers = ["store1", "store2", "store3", "store4"]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            async with http.get("https://api.gofile.io/servers") as r:
                sd = await r.json()
                got = [x["name"] for x in ((sd.get("data") or {}).get("servers") or []) if isinstance(x, dict) and x.get("name")]
                if got:
                    servers = got
    except Exception:
        pass

    last_err = None
    for srv in servers[:6]:
        pkey = f"gf_{uid}_{srv}_{int(time.monotonic()*1000)}"
        last_progress.pop(pkey, None)
        speed_state.pop(pkey, None)
        prog_file = f"{path}.{srv}.prog"
        clean_paths(prog_file)
        title = f"Uploading {fname} ({srv})..."
        await show_prog(msg, pkey, title, 0, fsize, 0.0)

        cmd = [
            "curl", "-sS", "-L",
            "--speed-limit", "200000", "--speed-time", "25",
            "--max-time", "7200",
            "-F", f"file=@{path};filename={fname}",
            f"https://{srv}.gofile.io/uploadFile",
        ]
        try:
            with open(prog_file, "w", encoding="utf-8") as pf:
                p = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=pf,
                )
            if uid in sessions:
                sessions[uid]["proc"] = p

            while p.returncode is None:
                await asyncio.sleep(1.5)
                cur_bytes = 0
                cur_spd = 0.0
                if os.path.exists(prog_file):
                    try:
                        raw = open(prog_file, "rb").read().decode("utf-8", errors="ignore")
                        parts = [x.strip() for x in re.split(r"[\r\n]+", raw) if x.strip()]
                        if parts:
                            last_line = parts[-1]
                            m_pct = re.search(r"(\d+(?:\.\d+)?)%", last_line)
                            if m_pct:
                                pct_v = float(m_pct.group(1))
                                cur_bytes = min(fsize, int(fsize * pct_v / 100.0))
                            m_spd = re.search(r"(\d+(?:\.\d+)?)([KMG])", last_line)
                            if m_spd:
                                val = float(m_spd.group(1))
                                unit = m_spd.group(2)
                                mult = {"K": 1024, "M": 1024**2, "G": 1024**3}.get(unit, 1)
                                cur_spd = val * mult
                    except Exception:
                        pass
                pct_show = min(99.5, (cur_bytes / fsize * 100.0)) if fsize > 0 else 0.0
                speed_state[pkey] = {"t": time.monotonic(), "d": cur_bytes, "spd": cur_spd}
                await show_prog(msg, pkey, title, cur_bytes, fsize, pct_show)

            out_b, _ = await p.communicate()
            if uid in sessions:
                sessions[uid]["proc"] = None
            clean_paths(prog_file)

            if p.returncode != 0:
                last_err = f"curl exit {p.returncode} on {srv}"
                continue

            res = json.loads((out_b or b"").decode("utf-8", errors="ignore"))
            lnk = (res.get("data") or {}).get("downloadPage")
            if res.get("status") == "ok" and lnk:
                await show_prog(msg, pkey, title, fsize, fsize, 100.0)
                return lnk
            last_err = res.get("status") or f"bad response from {srv}"
        except Exception as ex:
            clean_paths(prog_file)
            last_err = f"{srv}: {ex}"
            continue

    raise RuntimeError(f"Gofile failed: {last_err}")


def welcome_ui(uid):
    is_gf = get_dest(uid) == "gofile"
    dlbl = "Gofile Link" if is_gf else "Direct Telegram"
    txt = (
        "Hello Little! Welcome to **Asplayz Track Editor**.\n\n"
        "Send any video/MKV file or direct video link to:\n"
        "• Rename or remove audio tracks\n"
        "• Add, export, or remove subtitles\n"
        "• Convert container format (MP4 / MKV)\n\n"
        f"**Current Upload Destination:** {dlbl}\n"
        "*(Files over 2 GB automatically upload to Gofile; files up to 2 GB support both Gofile and Telegram.)*\n\n"
        "Need help? Contact 👉 @little_brother_0"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Functions", callback_data="info_funcs"), InlineKeyboardButton("Commands", callback_data="info_cmds")],
        [InlineKeyboardButton(f"Destination: {dlbl}", callback_data="toggle_dest")],
        [InlineKeyboardButton("Close", callback_data="close_menu")],
    ])
    return txt, kb


def menu_ui(uid, view="main"):
    s = sessions.get(uid)
    if not s:
        return "No active session.", None
    s["view"] = view
    dest = s.get("dest") or get_dest(uid)
    dlbl = "Gofile Link" if dest == "gofile" else "Direct Telegram"
    sz = fmt_sz(os.path.getsize(s["path"])) if os.path.exists(s.get("path") or "") else "Unknown"
    if view == "ren_audio":
        lines = [f"**Rename & Order Audio**\nFile: `{s['out_name']}`", "Tap track to rename:"]
        btns = []
        for a in s["audios"]:
            i = a["index"]
            cur = s["renamed_audios"].get(i, a["title"])
            btns.append([InlineKeyboardButton(f"Track {i+1}: {cur} ({a['lang']})", callback_data=f"ren_pick:{i}")])
        ao = [i for i in aud_order(s) if i not in s["removed_audios"]]
        if len(ao) > 1:
            for p, ai in enumerate(ao):
                cn = s["renamed_audios"].get(ai, s["audios"][ai]["title"])
                row = [InlineKeyboardButton(f"#{p+1} {cn}", callback_data="noop:0")]
                if p > 0:
                    row.append(InlineKeyboardButton("UP", callback_data=f"aud_move:{ai}:up"))
                if p < len(ao) - 1:
                    row.append(InlineKeyboardButton("DN", callback_data=f"aud_move:{ai}:dn"))
                btns.append(row)
        btns.append([InlineKeyboardButton("Back", callback_data="nav:main")])
        return "\n".join(lines), InlineKeyboardMarkup(btns)
    if view == "rm_audio":
        btns = []
        for a in s["audios"]:
            i = a["index"]
            cur = s["renamed_audios"].get(i, a["title"])
            ir = i in s["removed_audios"]
            btns.append([InlineKeyboardButton(f"Track {i+1}: {cur}", callback_data=f"noop:{i}"), InlineKeyboardButton("Restore" if ir else "Remove", callback_data=f"rm_tog:{i}")])
        btns.append([InlineKeyboardButton("Back", callback_data="nav:main")])
        return f"**Remove Audio**\nFile: `{s['out_name']}`", InlineKeyboardMarkup(btns)
    if view == "fmt":
        oe = s.get("orig_ext", "mkv")
        te = s.get("target_ext", oe)
        ae = "mp4" if oe != "mp4" else "mkv"
        return f"**Format**\nTarget: `{te.upper()}`", InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Convert to {ae.upper()}", callback_data=f"set_ext:{ae}")],
            [InlineKeyboardButton(f"Keep {oe.upper()}", callback_data=f"set_ext:{oe}")],
            [InlineKeyboardButton("Back", callback_data="nav:main")],
        ])
    if view == "subs":
        btns = []
        for sub in s["subs"]:
            i = sub["index"]
            rm = " [Removed]" if i in s["removed_subs"] else ""
            btns.append([InlineKeyboardButton(f"Sub {i+1}: {sub['title']}{rm}", callback_data=f"sub_item:{i}")])
        for i, asub in enumerate(s["added_subs"]):
            btns.append([InlineKeyboardButton(f"Remove Sub: {asub['name']}", callback_data=f"sub_rm_added:{i}")])
        btns.append([InlineKeyboardButton("+ Add Sub", callback_data="sub_add"), InlineKeyboardButton("Back", callback_data="nav:main")])
        return f"**Subtitles**\nFile: `{s['out_name']}`", InlineKeyboardMarkup(btns)
    fu = (s.get("target_ext") or s.get("orig_ext") or "mkv").upper()
    txt = f"**Track Editor**\n• File: `{s['out_name']}`\n• Size: `{sz}`\n• Audio: `{len(s['audios'])}` | Subs: `{len(s['subs'])}`\n• Format: `{fu}`\n• Dest: `{dlbl}`"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Audio", callback_data="nav:ren_audio"), InlineKeyboardButton("Rm Audio", callback_data="nav:rm_audio")],
        [InlineKeyboardButton("Format", callback_data="nav:fmt"), InlineKeyboardButton("Subs", callback_data="nav:subs")],
        [InlineKeyboardButton("Rename File", callback_data="ren_file"), InlineKeyboardButton(f"Dest: {dlbl}", callback_data="toggle_dest")],
        [InlineKeyboardButton("Save & Process", callback_data="run_job"), InlineKeyboardButton("Cancel", callback_data="cancel_job")],
    ])
    return txt, kb


async def open_menu(uid, msg):
    s = sessions.get(uid)
    if not s:
        return
    pr = await probe(s["path"])
    s["audios"], s["subs"] = pr["audios"], pr["subs"]
    s["orig_ext"] = s["target_ext"] = pr["ext"]
    aud_order(s)
    t, k = menu_ui(uid, "main")
    await edit_msg(msg, t, k)


async def run_job(uid, msg):
    s = sessions.get(uid)
    if not s:
        return
    te = (s.get("target_ext") or s.get("orig_ext") or "mkv").lstrip(".").lower()
    fn = f"{os.path.splitext(s.get('out_name') or s['name'])[0]}.{te}"
    s["out_name"] = fn
    cd = s.get("dest") or get_dest(uid)
    in_p = s["path"]
    in_sz = os.path.getsize(in_p) if os.path.exists(in_p) else 0
    if in_sz > TWO_GB:
        s["dest"] = cd = "gofile"
    out_p = os.path.join(OUT_DIR, f"out_{uid}_{int(time.time())}_{fn}")
    s["out_path"] = out_p
    kept_a = [ai for ai in aud_order(s) if ai not in s["removed_audios"]]
    cmd = ["ffmpeg", "-y", "-i", in_p, "-map", "0:v?"]
    for oa, sa in enumerate(kept_a):
        cmd += ["-map", f"0:{s['audios'][sa]['stream_index']}"]
        if sa in s["renamed_audios"]:
            rt = s["renamed_audios"][sa]
            cmd += ["-metadata:s:a:" + str(oa), "title=" + rt, "-metadata:s:a:" + str(oa), "language=und"]
        cmd += ["-disposition:a:" + str(oa), "default" if oa == 0 else "0"]
    text_codecs = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
    kept_s = 0
    for sub in s["subs"]:
        if sub["index"] not in s["removed_subs"]:
            sc = (sub.get("codec") or "").lower()
            if te == "mp4" and sc and sc not in text_codecs:
                continue
            cmd += ["-map", f"0:{sub['stream_index']}"]
            if te == "mp4":
                cmd += ["-c:s:" + str(kept_s), "mov_text"]
            kept_s += 1
    for asub in s["added_subs"]:
        cmd += ["-i", asub["path"]]
    if s["added_subs"]:
        for ei in range(len(s["added_subs"])):
            cmd += ["-map", f"{ei + 1}:0", "-c:s:" + str(kept_s + ei), "srt" if te == "mkv" else "mov_text"]
    cmd += ["-c:v", "copy", "-c:a", "copy"]
    if te == "mp4":
        cmd += ["-movflags", "+faststart"]
    cmd += [out_p]
    pkey = f"ff_{uid}_{msg.id}"
    await edit_msg(msg, f"**Converting / Processing...**\n`{fn}`", InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="cancel_job")]]))
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    s["proc"] = p
    while p.returncode is None:
        await asyncio.sleep(1.5)
        cur = os.path.getsize(out_p) if os.path.exists(out_p) else 0
        pct = min(99.5, (cur / in_sz * 100.0)) if in_sz > 0 else 0.0
        await show_prog(msg, pkey, f"Converting / Processing {fn}...", cur, in_sz, pct)
    _, err = await p.communicate()
    s["proc"] = None
    if p.returncode != 0 or not os.path.exists(out_p) or os.path.getsize(out_p) <= 0:
        clean_sess(uid)
        await edit_msg(msg, f"Processing failed: `{err.decode('utf-8', errors='ignore').splitlines()[-1:]}`")
        return
    if cd == "gofile" or os.path.getsize(out_p) > TWO_GB:
        lnk = await gofile_up(out_p, msg, uid)
        await edit_msg(msg, f"**Done!**\nFile: `{fn}`\nLink: {lnk}")
    else:
        await msg.reply_document(out_p, caption=f"`{fn}`")
        await msg.delete()
    clean_sess(uid)


@app.on_message(filters.command("start"))
async def start_cmd(_, m):
    uid = m.from_user.id if m.from_user else 0
    t, k = welcome_ui(uid)
    await m.reply_text(t, reply_markup=k)


@app.on_message(filters.command("cancel"))
async def cancel_cmd(_, m):
    uid = m.from_user.id if m.from_user else 0
    clean_sess(uid)
    await m.reply_text("Cancelled.")


@app.on_message(filters.document | filters.video | filters.text)
async def on_msg(_, m):
    uid = m.from_user.id if m.from_user else 0
    if not uid:
        return
    s = sessions.get(uid)
    if m.text and not m.text.startswith("/"):
        t = m.text.strip()
        if s and s.get("await_mode"):
            mode, idx = s["await_mode"], s.get("await_idx")
            s["await_mode"] = s["await_idx"] = None
            if mode == "ren_audio" and idx is not None:
                s["renamed_audios"][idx] = t
                tx, k = menu_ui(uid, "ren_audio")
                await m.reply_text(tx, reply_markup=k)
                return
            if mode == "ren_file":
                ext = os.path.splitext(s.get("out_name") or s["name"])[1]
                cl = re.sub(r'[\\/:*?"<>|]+', "_", t).strip()
                if cl:
                    s["out_name"] = (cl + ext) if ("." not in cl and ext) else cl
                tx, k = menu_ui(uid, "main")
                await m.reply_text(tx, reply_markup=k)
                return
        if t.startswith("http://") or t.startswith("https://"):
            if uid in sessions:
                clean_sess(uid)
            sm = await m.reply_text("Preparing link download...")
            fn = url_fname(t)
            sessions[uid] = {
                "name": fn, "out_name": fn, "path": None, "out_path": None,
                "audios": [], "subs": [], "renamed_audios": {}, "removed_audios": set(),
                "removed_subs": set(), "added_subs": [], "audio_order": [],
                "await_mode": None, "await_idx": None,
                "dest": get_dest(uid), "proc": None,
            }
            asyncio.create_task(download_from_url(uid, t, sm))
            return
    media = m.video or m.document
    if not media:
        return
    fname = getattr(media, "file_name", None) or f"media_{int(time.time())}.mkv"
    if s and s.get("await_mode") == "add_sub":
        s["await_mode"] = None
        sp = os.path.join(DL_DIR, f"sub_{uid}_{int(time.time())}_{fname}")
        await m.download(file_name=sp)
        s["added_subs"].append({"name": fname, "path": sp})
        tx, k = menu_ui(uid, "subs")
        await m.reply_text(tx, reply_markup=k)
        return
    if uid in sessions:
        clean_sess(uid)
    safe_fn = re.sub(r'[\\/:*?"<>|]+', "_", fname).strip() or f"media_{int(time.time())}.mkv"
    dp = os.path.abspath(os.path.join(DL_DIR, f"tg_{uid}_{int(time.time())}_{safe_fn}"))
    tot = getattr(media, "file_size", 0) or 0
    sm = await m.reply_text(f"**Downloading...**\n`{safe_fn}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="cancel_job")]]))
    sessions[uid] = {
        "name": safe_fn, "out_name": safe_fn, "path": dp, "out_path": None,
        "audios": [], "subs": [], "renamed_audios": {}, "removed_audios": set(),
        "removed_subs": set(), "added_subs": [], "audio_order": [],
        "await_mode": None, "await_idx": None,
        "dest": "gofile" if tot > TWO_GB else get_dest(uid), "proc": None,
    }
    pk = f"dl_{uid}_{sm.id}"

    async def _cb(cur, t):
        tsz = t if (t and t > 0) else tot
        await show_prog(sm, pk, f"Downloading: {safe_fn}", cur, tsz, (cur / tsz * 100.0) if tsz > 0 else 0.0)

    saved = await m.download(file_name=dp, progress=_cb)
    act = os.path.abspath(saved) if saved else dp
    sessions[uid]["path"] = act
    if os.path.getsize(act) > TWO_GB:
        sessions[uid]["dest"] = "gofile"
    await open_menu(uid, sm)


@app.on_callback_query()
async def on_cb(_, cq: CallbackQuery):
    uid = cq.from_user.id if cq.from_user else 0
    data = cq.data or ""
    s = sessions.get(uid)
    try:
        if data.startswith("noop:"):
            await cq.answer()
            return
        if data == "close_menu":
            await cq.message.delete()
            await cq.answer()
            return
        if data == "info_funcs":
            await edit_msg(cq.message, "**Functions**\n• Rename & Order Audio\n• Remove Audio Tracks\n• Add / Remove Subtitles\n• Convert MKV ↔ MP4\n• Rename Output File", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="back_start")]]))
            await cq.answer()
            return
        if data == "info_cmds":
            await edit_msg(cq.message, "**Commands**\n/start - Welcome menu\n/cancel - Cancel task", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="back_start")]]))
            await cq.answer()
            return
        if data == "back_start":
            t, k = welcome_ui(uid)
            await edit_msg(cq.message, t, k)
            await cq.answer()
            return
        if data == "cancel_job":
            clean_sess(uid)
            await edit_msg(cq.message, "Cancelled.")
            await cq.answer("Cancelled")
            return
        if data == "toggle_dest":
            nxt = "telegram" if get_dest(uid) == "gofile" else "gofile"
            set_dest(uid, nxt)
            if uid in sessions:
                sessions[uid]["dest"] = nxt
            await cq.answer("Updated")
            if s:
                t, k = menu_ui(uid, s.get("view") or "main")
            else:
                t, k = welcome_ui(uid)
            await edit_msg(cq.message, t, k)
            return
        if not s:
            await cq.answer("Send a file first.", show_alert=True)
            return
        if data.startswith("nav:"):
            t, k = menu_ui(uid, data.split(":", 1)[1])
            await edit_msg(cq.message, t, k)
            await cq.answer()
            return
        if data.startswith("ren_pick:"):
            s["await_mode"] = "ren_audio"
            s["await_idx"] = int(data.split(":", 1)[1])
            await cq.answer()
            await cq.message.reply_text("New audio title:")
            return
        if data == "ren_file":
            s["await_mode"] = "ren_file"
            await cq.answer()
            await cq.message.reply_text("New output name:")
            return
        if data.startswith("rm_tog:"):
            ai = int(data.split(":", 1)[1])
            rms = s["removed_audios"]
            if ai in rms:
                rms.remove(ai)
            else:
                rms.add(ai)
            t, k = menu_ui(uid, "rm_audio")
            await edit_msg(cq.message, t, k)
            await cq.answer()
            return
        if data.startswith("set_ext:"):
            s["target_ext"] = data.split(":", 1)[1]
            t, k = menu_ui(uid, "fmt")
            await edit_msg(cq.message, t, k)
            await cq.answer()
            return
        if data == "sub_add":
            s["await_mode"] = "add_sub"
            await cq.answer()
            await cq.message.reply_text("Send subtitle file:")
            return
        if data.startswith("sub_rm_added:"):
            i = int(data.split(":", 1)[1])
            s["added_subs"].pop(i)
            t, k = menu_ui(uid, "subs")
            await edit_msg(cq.message, t, k)
            await cq.answer()
            return
        if data.startswith("sub_item:"):
            si = int(data.split(":", 1)[1])
            await edit_msg(cq.message, f"Sub {si+1}", InlineKeyboardMarkup([[InlineKeyboardButton("Delete", callback_data=f"sub_del:{si}")], [InlineKeyboardButton("Back", callback_data="nav:subs")]]))
            await cq.answer()
            return
        if data.startswith("sub_del:"):
            si = int(data.split(":", 1)[1])
            rms = s["removed_subs"]
            if si in rms:
                rms.remove(si)
            else:
                rms.add(si)
            t, k = menu_ui(uid, "subs")
            await edit_msg(cq.message, t, k)
            await cq.answer()
            return
        if data.startswith("aud_move:"):
            _, idx_s, d = data.split(":")
            ai = int(idx_s)
            o = aud_order(s)
            pos = o.index(ai)
            np = pos - 1 if d == "up" else pos + 1
            if 0 <= np < len(o):
                o[pos], o[np] = o[np], o[pos]
                t, k = menu_ui(uid, "ren_audio")
                await edit_msg(cq.message, t, k)
            await cq.answer()
            return
        if data == "run_job":
            await cq.answer("Starting...")
            asyncio.create_task(run_job(uid, cq.message))
            return
        await cq.answer()
    except Exception as e:
        print(f"[cb_err] {data} -> {e}", flush=True)
        await cq.answer(f"Err: {e}", show_alert=True)


if __name__ == "__main__":
    os.makedirs(DL_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    print(">>> BOT_STARTED", flush=True)
    app.run()

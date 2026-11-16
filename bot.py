# ============================================================
#  TELEGRAM LINK -> CHANNEL VIDEO UPLOADER  (KOYEB VERSION)
#  Queue (mid mid) + FastTelethon + auto split + compress + intro
#  Dhammaan settings-ka waxaa laga qaadayaa ENVIRONMENT VARIABLES (Koyeb dashboard)
# ============================================================
import os, re, time, asyncio, subprocess, json, contextvars, shutil
from urllib.parse import urlparse, unquote
import aiohttp
from aiohttp import web
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo
from FastTelethonhelper import upload_file   # parallel upload
import FastTelethon as _ft

# Telethon wuxuu u baahan yahay loop la sameeyay ka hor client-ka
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

def _env_bool(name, default):
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")

# ===================== SETTINGS (ENV VARIABLES) =====================
API_ID         = int(os.environ["API_ID"])
API_HASH       = os.environ["API_HASH"]
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")      # ikhtiyaar: Premium account (4GB)
CHANNEL_ID     = int(os.environ["CHANNEL_ID"])
OWNER_IDS      = [int(x) for x in os.environ["OWNER_IDS"].replace(" ", "").split(",") if x]
MAX_PARALLEL   = int(os.environ.get("MAX_PARALLEL", 1))
MAX_UPLOADS    = int(os.environ.get("MAX_UPLOADS", 1))
AUTO_SPLIT     = _env_bool("AUTO_SPLIT", False)
COMPRESS_BIG   = _env_bool("COMPRESS_BIG", True)
COMPRESS_TARGET_MB = int(os.environ.get("COMPRESS_TARGET_MB", 1000))
ADD_INTRO      = _env_bool("ADD_INTRO", True)
INTRO_URL      = os.environ.get("INTRO_URL", "https://gdrive.khaancdn.workers.dev/intro.mp4")
CUT_FIRST      = int(os.environ.get("CUT_FIRST", 10))
# ====================================================================

if not SESSION_STRING and not BOT_TOKEN:
    raise SystemExit("Waa in la dejiyaa BOT_TOKEN ama SESSION_STRING")

# Xadka cabbirka hal fayl (bot 2000 MiB / Premium 4000 MiB)
MAX_FILE = (3995 if SESSION_STRING else 1995) * 1024 * 1024

# ---- Ku xir connection-nada upload-ka hawl kasta (si cancel u xirto kuwiisa oo keliya)
_cur = contextvars.ContextVar("cur_transfers")

if not getattr(_ft.ParallelTransferrer, "_cancel_patched", False):
    def _make_patch(orig):
        def _patched_init(self, *a, **k):
            orig(self, *a, **k)
            lst = _cur.get(None)
            if lst is not None:
                lst.append(self)
        return _patched_init
    _ft.ParallelTransferrer.__init__ = _make_patch(_ft.ParallelTransferrer.__init__)
    _ft.ParallelTransferrer._cancel_patched = True

async def cleanup_transfers(transfers):
    while transfers:
        t = transfers.pop()
        try:
            if getattr(t, "senders", None):
                await t._cleanup()
        except Exception:
            pass

DL_DIR = os.environ.get("DL_DIR", "/tmp/downloads")
os.makedirs(DL_DIR, exist_ok=True)
jobs = {}
job_counter = 0

class Cancelled(Exception):
    pass

job_sem    = asyncio.Semaphore(MAX_PARALLEL)
upload_sem = asyncio.Semaphore(MAX_UPLOADS)
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, sequential_updates=True)


# ----------------------- HELPERS ----------------------------
def human(n):
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.2f} {u}"
        n /= 1024
    return f"{n:.2f} PB"

def human_time(s):
    s = int(max(s, 0))
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

def bar(p):
    full = int(p // 10)
    return "█" * full + "░" * (10 - full)

def filename_from_url(url):
    name = unquote(os.path.basename(urlparse(url).path)) or f"video_{int(time.time())}.mp4"
    name = name.replace("/", "_")
    if "." not in name:
        name += ".mp4"
    return name

# ---- Taxano (series) vs Filim: ogaanshaha S01E01 ee magaca ku jira
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv", ".wmv")
# Qaababka la fahmo: S01E01 | S1E5 | S01 E01 | S01.E01 | 1x05 | Season 1 Episode 5
SERIES_RE  = re.compile(
    r"(?<![A-Za-z0-9])(?:S(\d{1,2})[ ._-]?E(\d{1,3})"
    r"|(\d{1,2})x(\d{2,3})"
    r"|Season[ ._-]?(\d{1,2})[ ._-]*Episode[ ._-]?(\d{1,3}))(?![0-9])", re.I)

def _se_tag(m):
    g = [x for x in m.groups() if x is not None]
    return f"S{int(g[0]):02d}E{int(g[1]):02d}"

def ensure_ext(name, url):
    """Haddii magaca aan lahayn extension muqaal (tus. 'Strike.Back.S01E01 AAD'), ku dar .mp4."""
    if name.lower().endswith(VIDEO_EXTS):
        return name
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext not in VIDEO_EXTS:
        ext = ".mp4"
    return name + ext

def is_series(base):
    return bool(SERIES_RE.search(base))

def icon_for(base):
    return "📺" if is_series(base) else "🎬"

def make_caption(base):
    """Taxano: 📺 Strike Back S01E01 AAD   |   Filim: 🎬 magaca sida uu yahay."""
    if not is_series(base):
        return f"🎬 {base}"
    t = re.sub(r"[._]+", " ", base)
    t = SERIES_RE.sub(_se_tag, t)
    t = re.sub(r"(?<![A-Za-z])AAD(?![A-Za-z])", " ", t, flags=re.I)   # "AAD" ka saar caption-ka
    t = re.sub(r"\s+", " ", t).strip()
    return f"📺 {t}"

async def safe_edit(msg, text, buttons=None):
    try:
        await msg.edit(text, buttons=buttons)
    except Exception:
        pass

def cancel_markup(jid):
    if SESSION_STRING:
        return None
    return [Button.inline("❌ Cancel", data=f"cancel:{jid}".encode())]

def parse_links(text):
    items = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not re.match(r"^https?://", line):
            continue
        parts = line.split(None, 1)
        url = parts[0]
        name = parts[1].strip() if len(parts) > 1 else filename_from_url(url)
        name = ensure_ext(name.replace("/", "_"), url)
        items.append((url, name))
    return items


# ----------------------- DOWNLOAD ---------------------------
async def download(url, path, msg, job, markup, tag):
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
        async with s.get(url, allow_redirects=True) as r:
            if r.status != 200:
                raise Exception(f"HTTP {r.status} - link-gu ma shaqeynayo ama wuu dhacay")
            total = int(r.headers.get("Content-Length", 0))
            done, last, last_done = 0, time.time(), 0
            with open(path, "wb") as f:
                async for chunk in r.content.iter_chunked(1024 * 1024):
                    if job["cancel"]:
                        raise Cancelled()
                    f.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last >= 3:
                        speed = (done - last_done) / (now - last)
                        last, last_done = now, done
                        if total:
                            p = done * 100 / total
                            eta = (total - done) / speed if speed else 0
                            txt = (f"{tag}\n⬇️ **Downloading...**\n\n{bar(p)} {p:.1f}%\n"
                                   f"📦 {human(done)} / {human(total)}\n"
                                   f"🚀 Speed: {human(speed)}/s\n⏳ ETA: {human_time(eta)}")
                        else:
                            txt = (f"{tag}\n⬇️ **Downloading...**\n📦 {human(done)}\n"
                                   f"🚀 Speed: {human(speed)}/s")
                        await safe_edit(msg, txt, markup)
            if total and done != total:
                raise Exception("Download-ku wuu dhammaystirmay (fayl wuu go'ay)")
    return done


# ----------------------- DOWNLOAD (TELEGRAM FILE) -----------
async def download_tg(tmsg, path, msg, job, markup, tag):
    """Ka soo deji file/muqaal Telegram-ka lagu soo diray (forward) - fast, haddii fashilmo caadi."""
    st = {"last": time.time(), "last_done": 0}
    def cb(current, total):
        if job["cancel"]:
            raise Cancelled()
        now = time.time()
        if now - st["last"] < 3 and current != total:
            return
        speed = (current - st["last_done"]) / max(now - st["last"], 0.001)
        st["last"], st["last_done"] = now, current
        p = current * 100 / total if total else 0
        eta = (total - current) / speed if (speed and total) else 0
        asyncio.get_event_loop().create_task(safe_edit(msg,
            f"{tag}\n⬇️ **Downloading...**\n\n{bar(p)} {p:.1f}%\n"
            f"📦 {human(current)} / {human(total)}\n"
            f"🚀 Speed: {human(speed)}/s\n⏳ ETA: {human_time(eta)}", markup))
    transfers = []
    _cur.set(transfers)
    try:
        try:
            with open(path, "wb") as f:
                await _ft.download_file(client, tmsg.media.document, f, progress_callback=cb)
        except Cancelled:
            raise
        except Exception:
            await cleanup_transfers(transfers)
            st["last"], st["last_done"] = time.time(), 0
            await client.download_media(tmsg, file=path, progress_callback=cb)
    finally:
        await cleanup_transfers(transfers)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise Exception("Fayl-ka Telegram laga soo dejin waayay")
    return os.path.getsize(path)


# ----------------------- VIDEO INFO / SPLIT -----------------
def video_info(path):
    dur = w = h = 0
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json", path])
        j = json.loads(out)
        w = j["streams"][0]["width"]
        h = j["streams"][0]["height"]
        dur = int(float(j["format"]["duration"]))
    except Exception:
        pass
    thumb = path + ".jpg"
    try:
        ss = "5" if dur > 20 else "1"
        subprocess.run(["ffmpeg", "-y", "-ss", ss, "-i", path, "-vframes", "1",
                        "-vf", "scale=320:-1", thumb],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    except Exception:
        pass
    if not os.path.exists(thumb):
        thumb = None
    return dur, w, h, thumb

def split_video(path, dur, size):
    """Ku googooy muqaalka qaybo ka yar MAX_FILE (copy, dib-u-xarrameyn la'aan)."""
    if dur <= 0:
        raise Exception("Muqaalku aad buu u weyn yahay oo duration lama helin, lama googooyi karo")
    seg_time = max(60, int(dur * (MAX_FILE * 0.85) / size))
    base, ext = os.path.splitext(path)
    pattern = f"{base}_part%03d{ext or '.mp4'}"
    subprocess.run([
        "ffmpeg", "-y", "-i", path, "-map", "0:v:0", "-map", "0:a?",
        "-c", "copy", "-f", "segment", "-segment_time", str(seg_time),
        "-reset_timestamps", "1", pattern],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    parts, i = [], 0
    while True:
        p = pattern % i
        if not os.path.exists(p):
            break
        parts.append(p)
        i += 1
    if not parts:
        raise Exception("Googoynta ffmpeg ma shaqeynin")
    return parts


# ----------------------- COMPRESS (GPU NVENC -> ~1GB) -------
async def compress_video(path, dur, h, status, job, markup, tag):
    """Yaree muqaalka ilaa COMPRESS_TARGET_MB (hal fayl). GPU (NVENC) ayaa marka hore la isticmaalaa,
    haddii aan la helin -> CPU x264. Soo celinaya path-ka muqaalka cusub (MP4)."""
    if dur <= 0:
        raise Exception("Muqaalku duration lama helin, lama yareyn karo")
    out = path + ".small.mp4"
    a_kbps = 128
    v_kbps = max(int(COMPRESS_TARGET_MB * 1024 * 1024 * 8 / dur / 1000 * 0.96) - a_kbps, 200)
    vf = ["-vf", "scale=-2:720"] if (h > 720 and v_kbps < 1800) else []   # bitrate hoose -> 720p
    common = ["-map", "0:v:0", "-map", "0:a:0?"] + vf + ["-pix_fmt", "yuv420p"]
    audio = ["-c:a", "aac", "-b:a", f"{a_kbps}k", "-ac", "2"]
    rate = ["-b:v", f"{v_kbps}k", "-maxrate", f"{int(v_kbps * 1.4)}k", "-bufsize", f"{v_kbps * 2}k"]
    gpu_enc = ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr"] + rate
    cpu_enc = ["-c:v", "libx264", "-preset", "veryfast"] + rate
    attempts = [("GPU ⚡ (NVENC)", ["-hwaccel", "cuda"], gpu_enc),
                ("GPU ⚡ (NVENC)", [], gpu_enc),
                ("CPU (x264)", [], cpu_enc)]
    total_us = dur * 1_000_000
    for label, pre, venc in attempts:
        cmd = (["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
               + pre + ["-i", path] + common + venc + audio
               + ["-movflags", "+faststart", "-f", "mp4", out])
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        t0 = time.time()
        last = 0
        speed_txt = ""
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            if job["cancel"]:
                proc.kill()
                await proc.wait()
                if os.path.exists(out):
                    os.remove(out)
                raise Cancelled()
            line = line.decode(errors="ignore").strip()
            if line.startswith("speed="):
                speed_txt = line.split("=", 1)[1].strip()
            elif line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    cur = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                now = time.time()
                if now - last < 3:
                    continue
                last = now
                pct = min(cur * 100 / total_us, 100)
                eta = (now - t0) * (100 - pct) / pct if pct > 0.5 else 0
                await safe_edit(status,
                    f"{tag}\n🗜 **Waa la yareynayaa ({label})...**\n\n{bar(pct)} {pct:.1f}%\n"
                    f"🎯 Hadaf: ~{COMPRESS_TARGET_MB} MB\n"
                    f"🚀 Xawaare: {speed_txt or '...'}\n⏳ ETA: {human_time(eta)}", markup)
        rc = await proc.wait()
        if rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return out
        if os.path.exists(out):
            os.remove(out)
    raise Exception("Yareynta muqaalka ma shaqeynin (ffmpeg)")


# ----------------------- STREAMABLE (MP4 + FASTSTART) -------
def make_streamable(path):
    """U beddel muqaalka MP4 + faststart si Telegram ugu daawan karo (stream) isagoo aan la soo dejin.
    Muqaalka/sawirka waa la koobiyeeyaa (tayo dhimis la'aan, degdeg). Codec-ka codka haddii aan MP4 ku haboonayn -> AAC.
    Haddii wax fashilmaan, fayl-kii asalka ahaa ayaa la isticmaalayaa. Soo celinaya (path_cusub, waa_la_beddelay)."""
    if path.endswith(".intro.mp4"):      # intro-ga la geliyay: mar hore waa MP4 + faststart
        return path, False
    out = path + ".stream.mp4"
    try:
        acodec = ""
        try:
            acodec = subprocess.check_output([
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
                timeout=60).decode().strip().lower()
        except Exception:
            pass
        a_opt = ["-c:a", "copy"] if acodec in ("aac", "mp3", "") else ["-c:a", "aac", "-b:a", "192k"]
        subprocess.run([
            "ffmpeg", "-y", "-i", path, "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy"] + a_opt + [
            "-movflags", "+faststart", "-f", "mp4", out],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out, True
    except Exception:
        pass
    if os.path.exists(out):
        try:
            os.remove(out)
        except Exception:
            pass
    return path, False


# ----------------------- UPLOAD -----------------------------
def make_upload_cb(msg, st, job, markup, tag, label):
    def cb(current, total):
        if job["cancel"]:
            raise Cancelled()
        now = time.time()
        if now - st["last"] < 3 and current != total:
            return
        speed = (current - st["last_done"]) / max(now - st["last"], 0.001)
        st["last"], st["last_done"] = now, current
        p = current * 100 / total
        eta = (total - current) / speed if speed else 0
        asyncio.get_event_loop().create_task(safe_edit(msg,
            f"{tag}\n⬆️ **Uploading{label}...**\n\n{bar(p)} {p:.1f}%\n"
            f"📦 {human(current)} / {human(total)}\n"
            f"🚀 Speed: {human(speed)}/s\n⏳ ETA: {human_time(eta)}", markup))
    return cb

async def upload_one(path, name, caption, status, job, markup, tag, label=""):
    # u beddel MP4 faststart (si loogu daawan karo Telegram stream ahaan)
    await safe_edit(status, f"{tag}\n🎞 Streamable ayaa loo diyaarinayaa...", markup)
    path, converted = await asyncio.to_thread(make_streamable, path)
    if converted:
        name = os.path.splitext(name)[0] + ".mp4"
    dur, w, h, thumb = await asyncio.to_thread(video_info, path)
    transfers = []
    _cur.set(transfers)
    try:
        st = {"last": time.time(), "last_done": 0}
        cb = make_upload_cb(status, st, job, markup, tag, label)
        try:
            # 1) FAST (parallel) - qaybo ka yar xadka way ku habboon yihiin
            with open(path, "rb") as f:
                uploaded = await upload_file(client, f, name, progress_callback=cb)
        except Cancelled:
            raise
        except Exception:
            # 2) Haddii fast fashilmo -> upload caadi ah (hal connection)
            await cleanup_transfers(transfers)
            st["last"], st["last_done"] = time.time(), 0
            uploaded = await client.upload_file(
                path, file_name=name, part_size_kb=512, progress_callback=cb)
        if job["cancel"]:
            raise Cancelled()
        await client.send_file(
            CHANNEL_ID,
            file=uploaded,
            caption=caption,
            attributes=[DocumentAttributeVideo(duration=dur, w=w, h=h,
                                               supports_streaming=True)],
            mime_type="video/mp4",
            thumb=thumb,
            supports_streaming=True,
            force_document=False,
        )
    finally:
        await cleanup_transfers(transfers)
        if thumb and os.path.exists(thumb):
            os.remove(thumb)
        if converted and os.path.exists(path):
            os.remove(path)   # fayl-ka MP4 ee ku-meel-gaarka ah


# ----------------------- INTRO (10s hore la jaro, intro-gaaga ugu horeeyo) -------
# Habka degdegga ah: kaliya 10s ugu horeeya + intro-ga + qaybta ilaa keyframe-ka xiga ayaa dib loo encode-gareeyaa
# (yar, ilbiriqsiyo), inta kale oo muqaalka ah waa la KOOBIYEEYAA (copy) -> filim 2GB ah daqiiqado kuma qaadanayo.
INTRO_DIR = os.path.join(DL_DIR, "_intro")
os.makedirs(INTRO_DIR, exist_ok=True)
INTRO_SRC = os.path.join(INTRO_DIR, "intro_src.mp4")
_intro_lock = asyncio.Lock()
_prep_lock  = asyncio.Lock()

async def fetch_intro():
    """Intro-ga hal mar ka soo deji (cache) - mar kasta dib looma soo dejinayo."""
    if os.path.exists(INTRO_SRC) and os.path.getsize(INTRO_SRC) > 0:
        return INTRO_SRC
    async with _intro_lock:
        if os.path.exists(INTRO_SRC) and os.path.getsize(INTRO_SRC) > 0:
            return INTRO_SRC
        tmp = INTRO_SRC + ".part"
        timeout = aiohttp.ClientTimeout(total=300, sock_connect=30, sock_read=60)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}) as s:
            async with s.get(INTRO_URL, allow_redirects=True) as r:
                if r.status != 200:
                    raise Exception(f"Intro HTTP {r.status} - INTRO_URL ma shaqeynayo")
                with open(tmp, "wb") as f:
                    async for chunk in r.content.iter_chunked(1024 * 1024):
                        f.write(chunk)
        if os.path.getsize(tmp) == 0:
            os.remove(tmp)
            raise Exception("Intro-ga waa madhan yahay")
        os.replace(tmp, INTRO_SRC)
    return INTRO_SRC

def _fr(s):
    try:
        n, d = (s or "").split("/")
        n, d = int(n), int(d)
        if n > 0 and d > 0:
            return f"{n}/{d}"
    except Exception:
        pass
    return None

def probe_av(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,codec_name,profile,width,height,r_frame_rate,avg_frame_rate,pix_fmt,"
        "sample_aspect_ratio,sample_rate,channels:format=duration,start_time",
        "-of", "json", path], timeout=120)
    j = json.loads(out)
    sts = j.get("streams", [])
    v = next((x for x in sts if x.get("codec_type") == "video"), None)
    a = next((x for x in sts if x.get("codec_type") == "audio"), None)
    if not v:
        raise Exception("Muqaalka video stream lagama helin")
    sar = v.get("sample_aspect_ratio") or "1:1"
    if ":" not in sar or sar.startswith("0"):
        sar = "1:1"
    fmt = j.get("format", {})
    info = {
        "vcodec": v.get("codec_name"), "w": int(v["width"]), "h": int(v["height"]),
        "fps": _fr(v.get("r_frame_rate")) or _fr(v.get("avg_frame_rate")) or "25/1",
        "pix": v.get("pix_fmt") or "yuv420p", "sar": sar,
        "dur": float(fmt.get("duration") or 0), "st": float(fmt.get("start_time") or 0),
        "has_audio": a is not None, "acodec": (a or {}).get("codec_name"),
        "aprofile": (a or {}).get("profile"),
        "sr": int((a or {}).get("sample_rate") or 48000), "ch": int((a or {}).get("channels") or 2),
    }
    info["copy_v"] = info["vcodec"] in ("h264", "hevc")                  # video-ga copy ma ku samayn karaa?
    info["tgt"]    = info["vcodec"] if info["copy_v"] else "h264"        # codec-ka ugu dambeeya
    info["copy_a"] = bool(info["has_audio"] and info["acodec"] == "aac"
                          and (info["aprofile"] or "LC") == "LC")        # codka copy ma ku samayn karaa?
    info["tsr"]    = info["sr"]
    info["tch"]    = info["ch"] if info["copy_a"] else min(info["ch"], 2)
    return info

def next_keyframe(path, at):
    """Keyframe-ka ugu horeeya ee pts >= at (ilbiriqsi, pts dhab ah)."""
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags", "-read_intervals", f"{at}%+90",
        "-of", "csv=p=0", path], timeout=180).decode(errors="ignore")
    best = None
    for line in out.splitlines():
        p = line.strip().split(",")
        if len(p) >= 2 and "K" in p[1]:
            try:
                t = float(p[0])
            except ValueError:
                continue
            if t >= at - 1e-3 and (best is None or t < best):
                best = t
    return best

async def run_ff(cmd, job):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    comm = asyncio.ensure_future(proc.communicate())
    while not comm.done():
        if job["cancel"]:
            proc.kill()
            await comm
            raise Cancelled()
        await asyncio.wait({comm}, timeout=1)
    _, err = comm.result()
    return proc.returncode, (err or b"").decode(errors="ignore")

def _aenc(info):
    return ["-c:a", "aac", "-b:a", "192k", "-ar", str(info["tsr"]), "-ac", str(info["tch"])]

async def encode_piece(job, src, out, info, ss=None, t=None, intro=False, silent=False):
    """Qayb yar dib u encode-garee (GPU NVENC marka hore, kadib CPU) -> MPEG-TS."""
    last_err = ""
    for gpu in ([True, False] if info["pix"] == "yuv420p" else [False]):
        cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error"]
        if ss is not None:
            cmd += ["-ss", f"{ss:.6f}"]
        cmd += ["-i", src]
        if silent:
            cmd += ["-f", "lavfi", "-i",
                    f"anullsrc=r={info['tsr']}:cl={'mono' if info['tch'] == 1 else 'stereo'}"]
        if t is not None:
            cmd += ["-t", f"{t:.4f}"]
        cmd += ["-map", "0:v:0"]
        if info["has_audio"]:
            cmd += ["-map", "1:a:0" if silent else "0:a:0?"]
        else:
            cmd += ["-an"]
        if intro:   # intro-ga ku beddel cabbirka/fps/pixel-ka muqaalka (si copy-ga ula mid noqdo)
            w, h = info["w"], info["h"]
            cmd += ["-vf", (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar={info['sar'].replace(':', '/')},"
                            f"fps={info['fps']},format={info['pix']}")]
        if gpu:
            cmd += ["-c:v", "hevc_nvenc" if info["tgt"] == "hevc" else "h264_nvenc",
                    "-preset", "p4", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
        elif info["tgt"] == "hevc":
            cmd += ["-c:v", "libx265", "-preset", "veryfast", "-crf", "20", "-x265-params", "log-level=error"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
        cmd += ["-pix_fmt", info["pix"]]
        if info["has_audio"]:
            cmd += _aenc(info)
            if silent:
                cmd += ["-shortest"]
        cmd += ["-f", "mpegts", out]
        rc, err = await run_ff(cmd, job)
        if rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return
        last_err = err
        if os.path.exists(out):
            os.remove(out)
    raise Exception(f"ffmpeg encode fashilmay: {last_err.strip()[-200:]}")

async def copy_tail(job, src, out, info, ss):
    """Inta kale ee muqaalka - COPY (dib u encode la'aan) laga bilaabo keyframe."""
    cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-ss", f"{ss:.6f}", "-i", src, "-map", "0:v:0"]
    cmd += ["-map", "0:a:0?"] if info["has_audio"] else ["-an"]
    cmd += ["-c:v", "copy"]
    if info["has_audio"]:
        cmd += ["-c:a", "copy"] if info["copy_a"] else _aenc(info)
    cmd += ["-avoid_negative_ts", "make_zero", "-f", "mpegts", out]
    rc, err = await run_ff(cmd, job)
    if rc != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        raise Exception(f"ffmpeg copy fashilmay: {err.strip()[-200:]}")

async def prepare_intro(job, intro_src, info, iinfo):
    """Intro-ga ku diyaari qaabka muqaalka (cache - hal mar ayaa la sameeyaa qaab kasta)."""
    key = re.sub(r"[^A-Za-z0-9]+", "_",
                 f"{info['tgt']}_{info['w']}x{info['h']}_{info['fps']}_{info['pix']}_{info['sar']}_"
                 f"{info['tsr']}_{info['tch']}_{int(info['has_audio'])}")
    p = os.path.join(INTRO_DIR, f"intro_{key}.ts")
    async with _prep_lock:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
        tmp = p + ".part"
        await encode_piece(job, intro_src, tmp, info, intro=True,
                           silent=(info["has_audio"] and not iinfo["has_audio"]))
        os.replace(tmp, p)
    return p

async def add_intro(path, status, job, markup, tag):
    """Muqaalka: [INTRO-gaaga] + [muqaalka oo 10s ugu horeeya laga jaray]. Soo celinaya path-ka MP4 cusub."""
    await safe_edit(status, f"{tag}\n🎞 Intro-ga ayaa la diyaarinayaa...", markup)
    intro_src = await fetch_intro()
    info  = await asyncio.to_thread(probe_av, path)
    iinfo = await asyncio.to_thread(probe_av, intro_src)
    dur, T = info["dur"], float(CUT_FIRST)
    if dur <= 0:
        raise Exception("Muqaalka duration lama helin")
    if dur <= T + 0.1:
        raise Exception(f"Muqaalku aad buu u gaaban yahay (ka yar {CUT_FIRST}s)")
    intro_ts = await prepare_intro(job, intro_src, info, iinfo)

    work = os.path.join(DL_DIR, f"_iw_{int(time.time() * 1000)}")
    out = path + ".intro.mp4"
    os.makedirs(work, exist_ok=True)
    try:
        await safe_edit(status, f"{tag}\n🎞 {CUT_FIRST}s hore ayaa la jarayaa, intro-gaaga ayaa la gelinayaa...", markup)
        segs = [intro_ts]     # intro-gaaga ayaa ugu horeeya (10s hore ee muqaalka lama isticmaalayo)

        K = None
        if info["copy_v"]:
            K = await asyncio.to_thread(next_keyframe, path, info["st"] + T)
            if K is not None:
                K -= info["st"]
        if K is not None and K < dur - 0.2:
            if K - T > 0.05:      # ilbiriqsiyada u dhexeeya 10s iyo keyframe-ka xiga (encode yar)
                mid = os.path.join(work, "c_mid.ts")
                await encode_piece(job, path, mid, info, ss=T, t=K - T - 0.0005)
                segs.append(mid)
            tail = os.path.join(work, "d_tail.ts")
            await copy_tail(job, path, tail, info, K - 0.0005)     # inta kale = COPY (degdeg)
            segs.append(tail)
        else:                     # codec aan copy lahayn / keyframe lama helin -> encode (GPU)
            tail = os.path.join(work, "d_tail.ts")
            await encode_piece(job, path, tail, info, ss=T)
            segs.append(tail)

        lst = os.path.join(work, "list.txt")
        with open(lst, "w") as f:
            for sgm in segs:
                f.write(f"file '{sgm}'\n")
        cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
               "-map", "0:v:0"] + (["-map", "0:a:0?"] if info["has_audio"] else ["-an"]) + [
               "-c", "copy", "-bsf:a", "aac_adtstoasc"]
        if info["tgt"] == "hevc":
            cmd += ["-tag:v", "hvc1"]
        cmd += ["-movflags", "+faststart", "-f", "mp4", out]
        rc, err = await run_ff(cmd, job)
        if rc != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            raise Exception(f"ffmpeg xirid fashilmay: {err.strip()[-200:]}")
        return out
    except BaseException:
        if os.path.exists(out):
            os.remove(out)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ----------------------- HAWL KASTA -------------------------
async def process_job(event, url, name, jid, status, tmsg=None):
    job = jobs[jid]
    markup = cancel_markup(jid)
    tag = f"{icon_for(os.path.splitext(name)[0])} `{name}`"
    path = os.path.join(DL_DIR, f"{jid}_{name}")
    parts = []

    try:
        async with job_sem:   # hal mar hal link - kuwa kale safka ayay sugaan (FIFO)
            try:
                if job["cancel"]:
                    raise Cancelled()
                await safe_edit(status, f"{tag}\n⏳ Waa la bilaabayaa...", markup)

                t0 = time.time()
                if tmsg is not None:   # file Telegram-ka lagu soo diray
                    size = await download_tg(tmsg, path, status, job, markup, tag)
                else:                  # link toos ah
                    size = await download(url, path, status, job, markup, tag)
                dl_time = time.time() - t0

                base, ext = os.path.splitext(name)
                caption_base = make_caption(base)   # 📺 taxano (S01E01) ama 🎬 filim

                orig_size = None
                if size > MAX_FILE and COMPRESS_BIG:
                    await safe_edit(status,
                        f"{tag}\n🗜 Muqaalku wuu weyn yahay ({human(size)}), ~{COMPRESS_TARGET_MB} MB ayaa loo yareynayaa...",
                        markup)
                    dur, _, vh, t = await asyncio.to_thread(video_info, path)
                    if t and os.path.exists(t):
                        os.remove(t)
                    small = await compress_video(path, dur, vh, status, job, markup, tag)
                    os.remove(path)
                    path = small
                    orig_size, size = size, os.path.getsize(small)

                if ADD_INTRO:
                    try:
                        new_path = await add_intro(path, status, job, markup, tag)
                        os.remove(path)
                        path = new_path
                        size = os.path.getsize(path)
                        name, ext = base + ".mp4", ".mp4"
                    except Cancelled:
                        raise
                    except Exception as e:
                        await safe_edit(status,
                            f"{tag}\n⚠️ Intro lama gelin karin ({str(e)[:150]}) - muqaalka caadiga ah ayaa la upload-gareynayaa...",
                            markup)
                        await asyncio.sleep(3)

                if size > MAX_FILE and not AUTO_SPLIT:
                    raise Exception(
                        f"Muqaalku waa {human(size)} - wuu ka weyn yahay xadka Telegram "
                        f"({human(MAX_FILE)}). Isticmaal Premium SESSION_STRING ama AUTO_SPLIT = True.")
                if size > MAX_FILE:
                    await safe_edit(status,
                        f"{tag}\n✂️ Muqaalku wuu weyn yahay ({human(size)}), qaybo ayaa loo googooynayaa...",
                        markup)
                    dur, _, _, t = await asyncio.to_thread(video_info, path)
                    if t and os.path.exists(t):
                        os.remove(t)
                    parts = await asyncio.to_thread(split_video, path, dur, size)
                    os.remove(path)   # meel ka bax
                else:
                    parts = [path]

                await safe_edit(status, f"{tag}\n⏳ Upload-ka ayaa la sugayaa...", markup)
                t1 = time.time()
                async with upload_sem:
                    for i, p in enumerate(parts, 1):
                        if job["cancel"]:
                            raise Cancelled()
                        if len(parts) > 1:
                            pname = f"{base} - Part {i}{ext or '.mp4'}"
                            cap = f"{caption_base} (Part {i}/{len(parts)})"
                            label = f" {i}/{len(parts)}"
                        else:
                            pname, cap, label = name, caption_base, ""
                        await upload_one(p, pname, cap, status, job, markup, tag, label)
                up_time = time.time() - t1

                await safe_edit(status,
                    f"{tag}\n✅ **Waa la dhameeyay!**\n\n📦 Size: {human(size)}"
                    + (f" (asal: {human(orig_size)}, la yareeyay 🗜)" if orig_size else "")
                    + (f" ({len(parts)} qaybood)" if len(parts) > 1 else "") + "\n"
                    f"⬇️ Download: {human_time(dl_time)} ({human(size / max(dl_time, 1))}/s)\n"
                    f"⬆️ Upload: {human_time(up_time)} ({human(size / max(up_time, 1))}/s)\n"
                    f"🗑 Muqaalka server-ka laga tirtiray")
            except Cancelled:
                await safe_edit(status, f"{tag}\n🛑 **Waa la joojiyay.**\n🗑 Muqaalka server-ka laga tirtiray.")
            except Exception as e:
                await safe_edit(status, f"{tag}\n❌ Khalad: {e}")
            finally:
                for f in [path] + parts:
                    if f and os.path.exists(f):
                        os.remove(f)
    finally:
        jobs.pop(jid, None)


# ----------------------- /start -----------------------------
@client.on(events.NewMessage(pattern=r"^/start", func=lambda e: e.is_private))
async def start_cmd(event):
    if event.sender_id not in OWNER_IDS:
        if not SESSION_STRING:
            await event.reply("🔒 Bot-kan waa gaar, adiga kuma shaqeynayo.")
        return
    sender = await event.get_sender()
    name = getattr(sender, "first_name", "") or ""
    await event.reply(
        f"👋 **Soo dhawoow {name}!**\n\n"
        "Waxaan soo dejiyaa muqaalka link-ga oo ku shubaa channel-kaaga "
        "(muqaal ladaawan karo ah).\n\n"
        "**Sida loo isticmaalo:**\n"
        "1️⃣ Ii soo dir link toos ah (`https://...mp4`)\n"
        "2️⃣ Ikhtiyaar: ku dar magac cusub: `https://site.com/a.mp4 Magac.mp4`\n"
        "3️⃣ **Link badan halmar:** fariin keliya ku qor line kasta hal link:\n"
        "`https://site.com/1.mp4`\n"
        "`https://site.com/2.mp4 Magac 2.mp4`\n"
        "`https://site.com/3.mp4`\n\n"
        "📺 **Taxano:** magaca ku dar `S01E01` (tus: `https://site.com/a.mp4 Strike.Back.S01E01 AAD`) "
        "— caption-ka wuxuu noqonayaa `📺 Strike Back S01E01 AAD`. Haddii `S01E01` aan jirin, waa filim (`🎬`).\n\n"
        "📁 **File Telegram:** forward/u dir muqaalo (10 ama in ka badan) - mid mid ayaa loo qaadayaa sida link-yada.\n\n"
        "⚡ Link-yada waxay u socdaan **mid mid** sida loo xigxigay: kan hore markuu dhammaado ayaa kan xiga bilaabmaa.\n"
        "✂️ Muqaal aad u weyn si toos ah ayaa loo googooyaa qaybo.\n"
        "❌ Link kasta wuxuu leeyahay Cancel button u gaar ah. /cancel wuxuu joojiyaa dhammaan."
    )


# ----------------------- CANCEL -----------------------------
@client.on(events.CallbackQuery(pattern=rb"cancel:(\d+)"))
async def cancel_cb(event):
    if event.sender_id not in OWNER_IDS:
        await event.answer("🔒 Bot-kan waa gaar.", alert=True)
        return
    job = jobs.get(int(event.pattern_match.group(1)))
    if job:
        job["cancel"] = True
        await event.answer("🛑 Waa la joojinayaa...")
    else:
        await event.answer("Hawshan hore ayay u dhammaatay.")

@client.on(events.NewMessage(pattern=r"^/cancel", from_users=OWNER_IDS,
                             func=lambda e: e.is_private))
async def cancel_cmd(event):
    if not jobs:
        await event.reply("ℹ️ Wax socda ma jiraan.")
        return
    for j in jobs.values():
        j["cancel"] = True
    await event.reply("🛑 Dhammaan hawlaha waa la joojinayaa...")


# ----------------------- LINK HANDLER -----------------------
@client.on(events.NewMessage(pattern=r"(?s)^\s*https?://", from_users=OWNER_IDS,
                             func=lambda e: e.is_private))
async def handler(event):
    global job_counter
    items = parse_links(event.raw_text)
    if not items:
        return
    if len(items) > 1:
        await event.reply(f"📥 **{len(items)} link** ayaa la helay. Mid mid ayaa loo soo dejinayaa (sida loo xigxigay)...")

    for url, name in items:
        job_counter += 1
        jid = job_counter
        jobs[jid] = {"cancel": False}
        # haddii hawl kale horey u jirto (shaqeyneysa ama sugaysa), kan wuu sugayaa
        waiting = ("⏳ Waa safka ku jiraa (wuxuu sugayaa kii ka horeeyay)..." if len(jobs) > MAX_PARALLEL
                   else "⏳ Waa la bilaabayaa...")
        status = await event.reply(f"{icon_for(os.path.splitext(name)[0])} `{name}`\n{waiting}",
                                   buttons=cancel_markup(jid))
        asyncio.create_task(process_job(event, url, name, jid, status))


# ----------------------- FILE HANDLER (Telegram files) ------
@client.on(events.NewMessage(from_users=OWNER_IDS,
                             func=lambda e: e.is_private and e.media is not None
                             and not re.match(r"(?s)^\s*https?://", e.raw_text or "")))
async def file_handler(event):
    global job_counter
    f = event.file
    if not f or not getattr(event.media, "document", None):
        return
    fname = (f.name or "").replace("/", "_")
    mime = f.mime_type or ""
    if not (event.video or mime.startswith("video/") or fname.lower().endswith(VIDEO_EXTS)):
        return   # ma aha muqaal
    if not fname:
        fname = f"video_{event.id}{f.ext or '.mp4'}"
    name = ensure_ext(fname, "")

    job_counter += 1
    jid = job_counter
    jobs[jid] = {"cancel": False}
    waiting = ("⏳ Waa safka ku jiraa (wuxuu sugayaa kii ka horeeyay)..." if len(jobs) > MAX_PARALLEL
               else "⏳ Waa la bilaabayaa...")
    status = await event.reply(f"{icon_for(os.path.splitext(name)[0])} `{name}`\n{waiting}",
                               buttons=cancel_markup(jid))
    asyncio.create_task(process_job(event, None, name, jid, status, tmsg=event.message))


# ----------------------- HEALTH SERVER (Koyeb web service) --
async def start_health_server():
    app = web.Application()
    async def ok(_):
        return web.Response(text="ok")
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8000))).start()


# ----------------------- DISK CLEANUP -----------------------
def sweep_downloads(min_age=0):
    """Tirtir fayl/gal kasta oo DL_DIR ku jira (marka laga reebo intro cache-ka)
    ee da'diisu ka weyn tahay min_age ilbiriqsi."""
    now = time.time()
    for entry in os.listdir(DL_DIR):
        p = os.path.join(DL_DIR, entry)
        if p == INTRO_DIR:
            continue
        try:
            if now - os.path.getmtime(p) < min_age:
                continue
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
        except Exception:
            pass

async def sweeper():
    """10 daqiiqo kasta: haddii hawl socota aysan jirin, tirtir faylasha hadhay (>10 daqiiqo)."""
    while True:
        await asyncio.sleep(600)
        if not jobs:
            await asyncio.to_thread(sweep_downloads, 600)


# ----------------------- START ------------------------------
async def main():
    sweep_downloads(0)               # bilowga: nadiifi wax kasta oo hore u hadhay
    asyncio.create_task(sweeper())   # nadiifin xilliyeed
    await start_health_server()
    if SESSION_STRING:
        await client.start()
    else:
        await client.start(bot_token=BOT_TOKEN)
    print("✅ Bot-ku wuu shaqeynayaa (QUEUE mode: mid mid). Link(s) u dir bot-ka (private).", flush=True)
    await client.run_until_disconnected()

if __name__ == "__main__":
    loop.run_until_complete(main())

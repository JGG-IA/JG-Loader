"""
JG-Loader - Plataforma para descargar audio y video de YouTube (y otros sitios soportados por yt-dlp).
Backend: FastAPI + yt-dlp + ffmpeg.
"""
import os
import re
import threading
import time
import uuid
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", BASE_DIR / "downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
FILE_TTL_SECONDS = int(os.environ.get("FILE_TTL_SECONDS", 3600))   # borrar archivos tras 1h
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", 3))

AUDIO_FORMATS = {"mp3", "m4a", "wav", "flac", "opus", "aac"}
VIDEO_FORMATS = {"360", "480", "720", "1080", "1440", "2160", "best"}

# Cookies de YouTube (para evitar "Sign in to confirm you're not a bot").
# Opción A: archivo secreto en Render -> /etc/secrets/cookies.txt
# Opción B: variable de entorno YT_COOKIES con el contenido del cookies.txt
COOKIES_SRC = Path(os.environ.get("COOKIES_FILE", "/etc/secrets/cookies.txt"))
COOKIES_RUNTIME = DOWNLOAD_DIR / ".cookies.txt"   # copia escribible (yt-dlp la actualiza)


def setup_cookies() -> bool:
    if os.environ.get("YT_COOKIES"):
        COOKIES_RUNTIME.write_text(os.environ["YT_COOKIES"].replace("\\n", "\n"))
        return True
    if COOKIES_SRC.is_file():
        COOKIES_RUNTIME.write_bytes(COOKIES_SRC.read_bytes())
        return True
    return False


HAS_COOKIES = setup_cookies()
print(f"[JG-Loader] cookies de YouTube: {'SÍ' if HAS_COOKIES else 'NO'}", flush=True)


def base_opts() -> dict:
    opts = {"noplaylist": True, "quiet": True, "no_warnings": True}
    if HAS_COOKIES:
        opts["cookiefile"] = str(COOKIES_RUNTIME)
    return opts


def friendly_error(e: Exception) -> str:
    msg = str(e)
    if "confirm you" in msg and "bot" in msg:
        if HAS_COOKIES:
            return ("YouTube bloqueó al servidor aunque tiene cookies: puede que hayan caducado "
                    "o que YouTube rechace la IP del servidor. Revisa /api/status.")
        return ("YouTube bloqueó al servidor (protección anti-bots). "
                "Falta configurar las cookies de YouTube: revisa /api/status.")
    if "Private video" in msg:
        return "Este video es privado."
    if "Video unavailable" in msg:
        return "El video no está disponible."
    if "age" in msg.lower() and "confirm" in msg.lower():
        return "Este video tiene restricción de edad."
    return msg.replace("ERROR: ", "")[:300]


app = FastAPI(title="JG-Loader")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
slots = threading.Semaphore(MAX_CONCURRENT)


class InfoRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format: str  # "mp3", "m4a", ... o "720", "1080", "best"


URL_RE = re.compile(r"^https?://", re.I)


def validate_url(url: str) -> str:
    url = url.strip()
    if not URL_RE.match(url):
        raise HTTPException(400, "URL inválida")
    return url


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "", name).strip()
    return name[:150] or "download"


@app.get("/api/status")
def status():
    import shutil
    lines = 0
    if HAS_COOKIES:
        lines = sum(1 for l in COOKIES_RUNTIME.read_text(errors="ignore").splitlines()
                    if l.strip() and not l.startswith("#") and "youtube.com" in l)
    return {
        "app": "JG-Loader",
        "cookies_cargadas": HAS_COOKIES,
        "cookies_de_youtube": lines,
        "origen_cookies": "YT_COOKIES" if os.environ.get("YT_COOKIES") else str(COOKIES_SRC),
        "yt_dlp": yt_dlp.version.__version__,
        "deno": bool(shutil.which("deno")),
        "ffmpeg": bool(shutil.which("ffmpeg")),
    }


@app.post("/api/info")
def info(req: InfoRequest):
    url = validate_url(req.url)
    opts = {**base_opts(), "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as e:
        raise HTTPException(400, friendly_error(e))
    heights = sorted({f.get("height") for f in data.get("formats", []) if f.get("height")})
    return {
        "title": data.get("title"),
        "thumbnail": data.get("thumbnail"),
        "duration": data.get("duration"),
        "uploader": data.get("uploader"),
        "heights": heights,
    }


def build_opts(job_id: str, fmt: str) -> dict:
    out = str(DOWNLOAD_DIR / job_id / "%(title).150B.%(ext)s")

    def hook(d):
        job = jobs[job_id]
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            pct = (done / total * 100) if total else 0
            # Si hay 2 streams (video+audio), repartimos el progreso
            job["progress"] = round(min(pct, 100) * 0.9, 1)
            job["speed"] = d.get("_speed_str", "").strip()
            job["eta"] = d.get("_eta_str", "").strip()
            job["status"] = "downloading"
        elif d["status"] == "finished":
            job["status"] = "processing"
            job["progress"] = 92

    opts = {
        **base_opts(),
        "outtmpl": out,
        "progress_hooks": [hook],
        "restrictfilenames": False,
    }
    if fmt in AUDIO_FORMATS:
        opts["format"] = "bestaudio/best"
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
        if fmt == "mp3":
            pp["preferredquality"] = "320"
        opts["postprocessors"] = [pp, {"key": "FFmpegMetadata"}]
    else:
        if fmt == "best":
            opts["format"] = "bestvideo+bestaudio/best"
        else:
            h = int(fmt)
            opts["format"] = (
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
            )
        opts["merge_output_format"] = "mp4"
    return opts


def run_job(job_id: str, url: str, fmt: str):
    job = jobs[job_id]
    with slots:
        try:
            job["status"] = "starting"
            with yt_dlp.YoutubeDL(build_opts(job_id, fmt)) as ydl:
                data = ydl.extract_info(url, download=True)
                job["title"] = data.get("title")
            files = [p for p in (DOWNLOAD_DIR / job_id).iterdir() if p.is_file()]
            if not files:
                raise RuntimeError("No se generó ningún archivo")
            f = max(files, key=lambda p: p.stat().st_size)
            job.update(status="done", progress=100, file=str(f), filename=f.name,
                       size=f.stat().st_size)
        except Exception as e:
            job.update(status="error", error=friendly_error(e))


@app.post("/api/download")
def download(req: DownloadRequest):
    url = validate_url(req.url)
    if req.format not in AUDIO_FORMATS | VIDEO_FORMATS:
        raise HTTPException(400, "Formato no soportado")
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "progress": 0, "created": time.time()}
    (DOWNLOAD_DIR / job_id).mkdir(parents=True, exist_ok=True)
    threading.Thread(target=run_job, args=(job_id, url, req.format), daemon=True).start()
    return {"id": job_id}


@app.get("/api/progress/{job_id}")
def progress(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Trabajo no encontrado")
    return {k: v for k, v in job.items() if k not in ("file", "created")}


@app.get("/api/file/{job_id}")
def get_file(job_id: str):
    job = jobs.get(job_id)
    if not job or job.get("status") != "done" or not Path(job["file"]).exists():
        raise HTTPException(404, "Archivo no disponible")
    return FileResponse(job["file"], filename=job["filename"],
                        media_type="application/octet-stream")


def cleanup_loop():
    import shutil
    while True:
        now = time.time()
        for jid, job in list(jobs.items()):
            if now - job["created"] > FILE_TTL_SECONDS:
                shutil.rmtree(DOWNLOAD_DIR / jid, ignore_errors=True)
                jobs.pop(jid, None)
        time.sleep(300)


threading.Thread(target=cleanup_loop, daemon=True).start()
app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")

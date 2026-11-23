#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
resolve-ingest — convierte automáticamente medios para DaVinci Resolve en Linux.

No es un plugin de Resolve (Resolve no expone un hook de importación). Es un
vigilante de carpeta + conversor con ffmpeg + importador opcional vía la API de
scripts de Resolve.

Un solo archivo, sin dependencias fuera de ffmpeg/ffprobe y Python 3.11+.

Uso rápido:
    chmod +x resolve-ingest.py
    ./resolve-ingest.py install --resolve-script
    # deja archivos en ~/Videos/ResolveIngest

Licencia: MIT.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import fcntl
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable

APP = "resolve-ingest"
VERSION = "1.0.0"
HOME = Path.home()
DEFAULT_CONFIG = HOME / ".config" / APP / "config.toml"
DEFAULT_STATE_DIR = HOME / ".local" / "state" / APP
DEFAULT_INBOX = HOME / "Videos" / "ResolveIngest"
SERVICE_NAME = f"{APP}.service"
RESOLVE_PYTHON = Path("/opt/resolve/bin/ResolvePython")
RESOLVE_BIN = Path("/opt/resolve/bin/resolve")
RESOLVE_SCRIPT_API = Path("/opt/resolve/Developer/Scripting")
RESOLVE_SCRIPT_LIB = Path("/opt/resolve/libs/Fusion/fusionscript.so")

VIDEO_EXTS = {
    ".mov", ".mxf", ".mp4", ".m4v", ".mkv", ".avi", ".webm", ".mts", ".m2ts",
    ".ts", ".wmv", ".flv", ".mpg", ".mpeg", ".mpe", ".3gp", ".3g2", ".ogv",
    ".vob", ".insv", ".lrv", ".m2v", ".mxf",
}
AUDIO_EXTS = {
    ".wav", ".bwf", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".oga", ".opus",
    ".aiff", ".aif", ".aifc", ".wma", ".ac3", ".eac3", ".dts", ".mka", ".caf",
}
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".exr", ".dpx", ".bmp", ".tga",
    ".gif", ".webp", ".psd", ".dng",
}
NATIVE_EXTS = {".braw", ".r3d", ".ari", ".arw", ".cr2", ".cr3", ".nef"}
SIDECAR_EXTS = {
    ".drp", ".fcpxml", ".xml", ".edl", ".srt", ".ass", ".ssa", ".vtt", ".cube",
    ".lut", ".txt", ".md", ".json", ".dat", ".db", ".log", ".part", ".tmp",
    ".crdownload", ".download", ".aria2", ".ytdl", ".!qb",
}
TEMP_SUFFIXES = (".part", ".tmp", ".crdownload", ".download", ".aria2", ".ytdl")
ALL_MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS | NATIVE_EXTS

# Códecs que Resolve suele aceptar en Linux. Ante la duda, se transcodifica:
# es mejor gastar disco que perder tiempo con un clip offline.
SAFE_VIDEO_COMMON = {
    "dnxhd", "cfhd", "v210", "rawvideo", "mpeg2video", "dvvideo", "mjpeg",
}
SAFE_VIDEO_STUDIO = {"h264", "hevc"}
SAFE_AUDIO_COMMON = {
    "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le",
    "pcm_s16be", "pcm_s24be", "pcm_s32be", "pcm_u8", "pcm_alaw", "pcm_mulaw",
}
SAFE_AUDIO_STUDIO = {"aac", "ac3", "eac3", "mp3", "alac", "flac"}
SAFE_CONTAINERS = {"mov", "mp4", "m4a", "mxf", "wav", "aiff", "image2", "braw"}
COPYABLE_VIDEO = {"dnxhd", "cfhd", "v210", "rawvideo", "prores", "h264", "hevc"}

PROFILES = {"dnxhr_lb", "dnxhr_sq", "dnxhr_hq", "dnxhr_hqx", "dnxhr_444"}
PROFILE_PIX_FMT = {
    "dnxhr_hqx": "yuv422p10le",
    "dnxhr_444": "yuv444p10le",
}
for _p in ("dnxhr_lb", "dnxhr_sq", "dnxhr_hq"):
    PROFILE_PIX_FMT[_p] = "yuv422p"

DEFAULT_CONFIG_TEXT = """\
# Configuración de resolve-ingest
# Los cambios se aplican al reiniciar el servicio:
#   systemctl --user restart resolve-ingest

[paths]
# Carpeta donde dejas tus archivos. El servicio la vigila.
inbox = "~/Videos/ResolveIngest"
# Por defecto: <inbox>/Convertidos
# output = "~/Videos/ResolveIngest/Convertidos"
# failed = "~/Videos/ResolveIngest/_fallidos"
# state_dir = "~/.local/state/resolve-ingest"

[transcode]
# auto elige HQ para <=1080p, SQ para <=4K y LB para más resolución.
# Valores: auto, dnxhr_lb, dnxhr_sq, dnxhr_hq, dnxhr_hqx, dnxhr_444
video_profile = "auto"
audio_codec = "pcm_s24le"
audio_rate = 48000
# Segundos que un archivo debe estar quieto antes de procesarlo.
stability_seconds = 5
poll_seconds = 2
# ProRes suele funcionar en Resolve Linux, pero si tu versión no lo abre,
# ponlo en false para convertirlo también.
accept_prores = true

[resolve]
# Importar automáticamente al proyecto abierto.
# "auto" = solo si la edición permite scripting externo (normalmente Studio).
# true/false para forzar.
auto_import = "auto"
bin_name = "Resolve Ingest"
# auto, free o studio. auto detecta el paquete/API; ante la duda usa free.
edition = "auto"
scripting_timeout = 30

[behavior]
# El original nunca se borra por defecto.
keep_original = true
# Si true, mueve el original a <inbox>/_originales después de convertir.
move_original = false
notify = true
skip_hidden = true
retry_failed = false
"""

IMPORT_HELPER = r'''
import json
import os
import sys
import traceback


def main():
    result = {"ok": False, "error": "sin resultado"}
    try:
        import DaVinciResolveScript as dvr

        job = json.loads(os.environ.get("RESOLVE_INGEST_JOB", "{}"))
        resolve = dvr.scriptapp("Resolve")
        if not resolve:
            result["error"] = (
                "No se pudo conectar a Resolve. Abre Resolve y activa "
                "Preferencias > Sistema > General > External scripting = Local."
            )
        else:
            project = resolve.GetProjectManager().GetCurrentProject()
            if not project:
                result["error"] = "No hay ningún proyecto abierto en Resolve."
            else:
                media_pool = project.GetMediaPool()
                root = media_pool.GetRootFolder()
                bin_name = job.get("bin") or "Resolve Ingest"
                target = root
                for sub in (root.GetSubFolderList() or []):
                    if sub.GetName() == bin_name:
                        target = sub
                        break
                else:
                    target = media_pool.AddSubFolder(root, bin_name) or root

                media_pool.SetCurrentFolder(target)
                paths = job.get("paths", [])
                existing = set()
                for clip in (target.GetClipList() or []):
                    try:
                        clip_path = clip.GetClipProperty("File Path")
                    except Exception:
                        clip_path = ""
                    if clip_path:
                        existing.add(os.path.abspath(clip_path))
                        existing.add(os.path.realpath(clip_path))
                new_paths = [
                    p for p in paths
                    if os.path.abspath(p) not in existing
                    and os.path.realpath(p) not in existing
                ]
                if not new_paths:
                    result = {"ok": True, "imported": [], "skipped": len(paths)}
                else:
                    version = resolve.GetVersion() or []
                    try:
                        major = int(version[0])
                    except Exception:
                        major = 0
                    infos = [{"FilePath": p} for p in new_paths] if major >= 21 else list(new_paths)
                    imported = media_pool.ImportMedia(infos) or []
                    result = {"ok": bool(imported), "imported": [], "skipped": len(paths) - len(new_paths)}
                    for clip in imported:
                        try:
                            result["imported"].append(clip.GetClipProperty("File Path"))
                        except Exception:
                            try:
                                result["imported"].append(clip.GetName())
                            except Exception:
                                result["imported"].append("?")
                    if not imported:
                        result["error"] = "ImportMedia no devolvió ningún clip."
    except Exception as exc:
        result = {"ok": False, "error": repr(exc), "trace": traceback.format_exc()}

    out = os.environ.get("RESOLVE_INGEST_RESULT")
    if out:
        try:
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

STOP = False
LOG = logging.getLogger(APP)
_LAST_IMPORT_WARNING = 0.0


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def expand_path(value: Any, default: Path | None = None) -> Path:
    if value is None or str(value).strip() == "":
        if default is None:
            raise ValueError("ruta vacía")
        return default
    return Path(os.path.abspath(os.path.expandvars(os.path.expanduser(str(value)))))


def human_size(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024.0 or unit == "TiB":
            return f"{num:0.1f} {unit}"
        num /= 1024.0
    return f"{num:0.1f} TiB"


def parse_fraction(text: Any) -> float | None:
    try:
        if not text or text in ("0/0", "N/A"):
            return None
        if "/" in str(text):
            num, den = str(text).split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else None
        return float(text)
    except Exception:
        return None


def tails(text: str, lines: int = 8) -> str:
    rows = [r for r in (text or "").splitlines() if r.strip()]
    return " | ".join(rows[-lines:])


def which(name: str) -> str | None:
    return shutil.which(name)


def systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(cfg: "Config", title: str, body: str, urgency: str = "normal") -> None:
    if not cfg.notify:
        return
    exe = which("notify-send")
    if not exe:
        return
    try:
        subprocess.run(
            [exe, "-a", "Resolve Ingest", "-u", urgency, title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "si", "sí", "on")
    return bool(value)


@dataclasses.dataclass
class Config:
    path: Path
    inbox: Path
    output: Path
    failed: Path
    state_dir: Path
    log_file: Path
    ffmpeg: str
    ffprobe: str
    video_profile: str
    audio_codec: str
    audio_rate: int
    stability: float
    poll: float
    accept_prores: bool
    auto_import: bool
    bin_name: str
    edition: str
    scripting_timeout: int
    keep_original: bool
    move_original: bool
    notify_enabled: bool
    skip_hidden: bool
    retry_failed: bool
    resolved_edition: str = "free"
    auto_import_auto: bool = False

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "watch.lock"

    @property
    def notify(self) -> bool:
        return self.notify_enabled


def ensure_config(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")


def load_config(path: Path) -> Config:
    path = expand_path(path)
    ensure_config(path)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"Error de sintaxis en {path}: {exc}") from exc
    except OSError as exc:
        raise SystemExit(f"No puedo leer {path}: {exc}") from exc

    paths = data.get("paths", {}) or {}
    tr = data.get("transcode", {}) or {}
    rs = data.get("resolve", {}) or {}
    bh = data.get("behavior", {}) or {}

    inbox = expand_path(paths.get("inbox"), DEFAULT_INBOX)
    output = expand_path(paths.get("output"), inbox / "Convertidos")
    failed = expand_path(paths.get("failed"), inbox / "_fallidos")
    state_dir = expand_path(paths.get("state_dir"), DEFAULT_STATE_DIR)
    log_file = expand_path(paths.get("log_file"), state_dir / f"{APP}.log")

    ffmpeg = str(
        tr.get("ffmpeg")
        or which("ffmpeg")
        or ("/usr/bin/ffmpeg" if Path("/usr/bin/ffmpeg").exists() else "ffmpeg")
    )
    ffprobe = str(
        tr.get("ffprobe")
        or which("ffprobe")
        or ("/usr/bin/ffprobe" if Path("/usr/bin/ffprobe").exists() else "ffprobe")
    )
    profile = str(tr.get("video_profile", "auto")).strip().lower()
    if profile not in PROFILES | {"auto"}:
        raise SystemExit(
            f"video_profile inválido: {profile!r}. Usa auto, {', '.join(sorted(PROFILES))}."
        )
    edition = str(rs.get("edition", "auto")).strip().lower()
    if edition not in ("auto", "free", "studio"):
        raise SystemExit("edition inválido: usa auto, free o studio.")

    try:
        audio_rate = int(tr.get("audio_rate", 48000))
        stability = float(tr.get("stability_seconds", 5))
        poll = float(tr.get("poll_seconds", 2))
        timeout = int(rs.get("scripting_timeout", 30))
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"Valor numérico inválido en {path}: {exc}") from exc

    auto_raw = rs.get("auto_import", "auto")
    auto_import_auto = isinstance(auto_raw, str) and auto_raw.strip().lower() == "auto"
    auto_import = True if auto_import_auto else parse_bool(auto_raw, True)

    return Config(
        path=path,
        inbox=inbox,
        output=output,
        failed=failed,
        state_dir=state_dir,
        log_file=log_file,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        video_profile=profile,
        audio_codec=str(tr.get("audio_codec", "pcm_s24le")),
        audio_rate=audio_rate,
        stability=max(0.0, stability),
        poll=max(0.5, poll),
        accept_prores=parse_bool(tr.get("accept_prores", True), True),
        auto_import=auto_import,
        auto_import_auto=auto_import_auto,
        bin_name=str(rs.get("bin_name", "Resolve Ingest")),
        edition=edition,
        scripting_timeout=max(5, timeout),
        keep_original=parse_bool(bh.get("keep_original", True), True),
        move_original=parse_bool(bh.get("move_original", False), False),
        notify_enabled=parse_bool(bh.get("notify", True), True),
        skip_hidden=parse_bool(bh.get("skip_hidden", True), True),
        retry_failed=parse_bool(bh.get("retry_failed", False), False),
    )


def setup_logging(cfg: Config | None, verbose: bool = False, console: bool = True) -> None:
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOG.propagate = False
    for handler in list(LOG.handlers):
        LOG.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    if console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        LOG.addHandler(sh)
    if cfg is not None:
        try:
            cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                cfg.log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
            )
            fh.setFormatter(fmt)
            LOG.addHandler(fh)
        except OSError as exc:
            LOG.warning("No puedo escribir el log %s: %s", cfg.log_file, exc)


# ---------------------------------------------------------------------------
# Estado persistente y bloqueo
# ---------------------------------------------------------------------------


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {"version": 1, "processed": {}, "pending_import": []}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data["version"] = raw.get("version", 1)
                processed = raw.get("processed", {})
                if isinstance(processed, dict):
                    self.data["processed"] = processed
                pending = raw.get("pending_import", [])
                if isinstance(pending, list):
                    self.data["pending_import"] = [
                        str(item) for item in pending if isinstance(item, str)
                    ]
        except Exception as exc:
            bad = self.path.with_name(self.path.name + f".corrupt-{int(time.time())}")
            try:
                self.path.replace(bad)
            except OSError:
                pass
            LOG.warning("Estado corrupto (%s); empiezo de cero. Copia: %s", exc, bad)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=".state-", suffix=".json", dir=str(self.path.parent)
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except OSError as exc:
            LOG.warning("No pude guardar el estado %s: %s", self.path, exc)

    @staticmethod
    def key(path: Path | str) -> str:
        return os.path.abspath(str(path))

    def get(self, path: Path | str) -> dict[str, Any] | None:
        value = self.data["processed"].get(self.key(path))
        return value if isinstance(value, dict) else None

    def mark(
        self,
        source: Path,
        stat: os.stat_result,
        status: str,
        output: Path | None,
        mode: str,
        reason: str,
        error: str = "",
    ) -> None:
        self.data["processed"][self.key(source)] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "status": status,
            "output": str(output) if output else None,
            "mode": mode,
            "reason": reason,
            "error": error,
            "time": now_iso(),
        }

    def pending(self) -> list[Path]:
        result: list[Path] = []
        seen: set[str] = set()
        for item in self.data.get("pending_import", []):
            if item in seen:
                continue
            seen.add(item)
            p = Path(item)
            if p.exists():
                result.append(p)
        return result

    def add_pending(self, path: Path) -> None:
        value = self.key(path)
        if value not in self.data["pending_import"]:
            self.data["pending_import"].append(value)

    def remove_pending(self, paths: Iterable[Path]) -> None:
        remove = {self.key(p) for p in paths}
        self.data["pending_import"] = [
            p for p in self.data["pending_import"] if p not in remove
        ]

    def prune_pending(self) -> None:
        self.data["pending_import"] = [
            p for p in self.data["pending_import"] if Path(p).exists()
        ]


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.fh: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        try:
            self.fh.seek(0)
            self.fh.truncate()
            self.fh.write(str(os.getpid()))
            self.fh.flush()
        except OSError:
            pass
        return True

    def release(self) -> None:
        if self.fh is not None:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Sondeo y compatibilidad
# ---------------------------------------------------------------------------


class MediaError(Exception):
    pass


@dataclasses.dataclass
class MediaInfo:
    path: Path
    kind: str
    format_name: str
    video_codec: str | None
    audio_codecs: list[str]
    width: int | None
    height: int | None
    fps: float | None
    vfr: bool
    duration: float | None
    pix_fmt: str | None
    sample_rate: int | None

    @property
    def pixels(self) -> int:
        return (self.width or 0) * (self.height or 0)


@dataclasses.dataclass
class Plan:
    needed: bool
    mode: str
    output_ext: str
    reason: str
    profile: str | None = None


@dataclasses.dataclass
class ProcessResult:
    source: Path
    output: Path | None
    converted: bool
    ok: bool
    mode: str
    reason: str
    error: str = ""


def probe_media(path: Path, cfg: Config) -> MediaInfo:
    cmd = [
        cfg.ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise MediaError(f"ffprobe no encontrado: {cfg.ffprobe}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("ffprobe tardó demasiado (¿archivo en red?)") from exc
    if proc.returncode != 0:
        raise MediaError(f"ffprobe falló: {tails(proc.stderr, 3)}")
    try:
        raw = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError("ffprobe devolvió JSON inválido") from exc

    streams = raw.get("streams", []) or []
    fmt = raw.get("format", {}) or {}
    video = None
    for stream in streams:
        if stream.get("codec_type") == "video":
            if (stream.get("disposition") or {}).get("attached_pic"):
                continue
            video = stream
            break
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    ext = path.suffix.lower()

    if ext in IMAGE_EXTS:
        kind = "image"
    elif ext in NATIVE_EXTS:
        kind = "native"
    elif video is not None:
        kind = "video"
    elif audios:
        kind = "audio"
    else:
        kind = "other"

    duration: float | None = None
    for value in (fmt.get("duration"), video.get("duration") if video else None):
        try:
            if value not in (None, "N/A"):
                duration = float(value)
                break
        except (TypeError, ValueError):
            continue

    fps = None
    vfr = False
    if video:
        avg = parse_fraction(video.get("avg_frame_rate"))
        real = parse_fraction(video.get("r_frame_rate"))
        fps = avg or real
        if avg and real and avg > 0:
            if real / avg > 1.05 or avg / real > 1.05:
                vfr = True
                fps = avg

    sample_rate = None
    if audios:
        try:
            sample_rate = int(audios[0].get("sample_rate") or 0) or None
        except (TypeError, ValueError):
            sample_rate = None

    return MediaInfo(
        path=path,
        kind=kind,
        format_name=str(fmt.get("format_name") or ""),
        video_codec=(video.get("codec_name") if video else None),
        audio_codecs=[str(s.get("codec_name") or "?") for s in audios],
        width=(int(video["width"]) if video and video.get("width") else None),
        height=(int(video["height"]) if video and video.get("height") else None),
        fps=fps,
        vfr=vfr,
        duration=duration,
        pix_fmt=(video.get("pix_fmt") if video else None),
        sample_rate=sample_rate,
    )


def safe_sets(cfg: Config) -> tuple[set[str], set[str]]:
    videos = set(SAFE_VIDEO_COMMON)
    audios = set(SAFE_AUDIO_COMMON)
    if cfg.accept_prores:
        videos.add("prores")
    if cfg.resolved_edition == "studio":
        videos |= SAFE_VIDEO_STUDIO
        audios |= SAFE_AUDIO_STUDIO
    return videos, audios


def target_profile(info: MediaInfo, cfg: Config) -> str:
    if cfg.video_profile != "auto":
        return cfg.video_profile
    pixels = info.pixels
    if pixels <= int(1920 * 1080 * 1.1):
        return "dnxhr_hq"
    if pixels <= int(3840 * 2160 * 1.1):
        return "dnxhr_sq"
    return "dnxhr_lb"


def plan_for(info: MediaInfo, cfg: Config) -> Plan:
    if info.kind in ("image", "native", "other"):
        return Plan(False, "none", info.path.suffix.lower(), "formato nativo")

    videos, audios = safe_sets(cfg)
    container_ok = any(
        token in SAFE_CONTAINERS for token in info.format_name.split(",") if token
    )
    audio_ok = all(codec in audios for codec in info.audio_codecs)

    if info.kind == "audio":
        if audio_ok and container_ok:
            return Plan(False, "none", info.path.suffix.lower(), "audio compatible")
        reasons = []
        if not audio_ok:
            reasons.append("audio " + ", ".join(sorted(set(info.audio_codecs))))
        if not container_ok:
            reasons.append(f"contenedor {info.format_name or '?'}")
        return Plan(True, "audio_file", ".wav", "no soportado: " + ", ".join(reasons))

    video_ok = bool(info.video_codec and info.video_codec in videos)
    if video_ok and audio_ok and container_ok:
        return Plan(False, "none", info.path.suffix.lower(), "compatible")

    reasons = []
    if not video_ok:
        reasons.append(f"vídeo {info.video_codec or '?'}")
    if not audio_ok:
        reasons.append("audio " + ", ".join(sorted(set(info.audio_codecs))))
    if not container_ok:
        reasons.append(f"contenedor {info.format_name or '?'}")
    reason = "no soportado: " + ", ".join(reasons)

    if video_ok and info.video_codec in COPYABLE_VIDEO:
        if audio_ok and not container_ok:
            return Plan(True, "remux", ".mov", reason)
        if not audio_ok:
            return Plan(True, "audio", ".mov", reason)

    return Plan(True, "full", ".mov", reason, target_profile(info, cfg))


def build_ffmpeg_cmd(
    info: MediaInfo, plan: Plan, source: Path, dest: Path, cfg: Config
) -> list[str]:
    cmd = [
        cfg.ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel", "error",
        "-progress", "pipe:1",
        "-nostats",
        "-i", str(source),
    ]
    if plan.mode == "full":
        profile = plan.profile or "dnxhr_hq"
        pix_fmt = PROFILE_PIX_FMT.get(profile, "yuv422p")
        cmd += [
            "-map", "0:v:0?",
            "-map", "0:a?",
            "-map_metadata", "0",
            "-map_chapters", "0",
            "-c:v", "dnxhd",
            "-profile:v", profile,
            "-pix_fmt", pix_fmt,
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:a", cfg.audio_codec,
            "-ar", str(cfg.audio_rate),
            "-metadata:s:v:0", "rotate=0",
            "-max_muxing_queue_size", "4096",
        ]
        if info.vfr and info.fps:
            cmd += ["-fps_mode", "cfr", "-r", f"{info.fps:.6f}"]
    elif plan.mode == "audio":
        cmd += [
            "-map", "0:v:0",
            "-map", "0:a?",
            "-map_metadata", "0",
            "-map_chapters", "0",
            "-c:v", "copy",
            "-c:a", cfg.audio_codec,
            "-ar", str(cfg.audio_rate),
        ]
    elif plan.mode == "remux":
        cmd += [
            "-map", "0:v:0",
            "-map", "0:a?",
            "-map_metadata", "0",
            "-map_chapters", "0",
            "-c", "copy",
        ]
    elif plan.mode == "audio_file":
        cmd += [
            "-map", "0:a:0",
            "-map_metadata", "0",
            "-c:a", cfg.audio_codec,
            "-ar", str(cfg.audio_rate),
            "-vn",
        ]
    else:
        raise MediaError(f"Modo de conversión desconocido: {plan.mode}")
    cmd.append(str(dest))
    return cmd


def run_ffmpeg(cmd: list[str], duration: float | None, label: str) -> None:
    LOG.debug("ffmpeg: %s", " ".join(shlex.quote(x) for x in cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise MediaError("ffmpeg no encontrado") from exc

    errors: list[str] = []
    last_pct = -10
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            key, sep, value = line.partition("=")
            if sep and key in ("out_time_us", "out_time_ms"):
                try:
                    seconds = int(value) / 1_000_000.0
                except ValueError:
                    continue
                if duration and duration > 0:
                    pct = int(min(99, max(0, seconds / duration * 100)))
                    if pct >= last_pct + 10:
                        last_pct = pct
                        LOG.info("  %s: %d%%", label, pct)
            elif sep and key == "progress":
                if value == "end":
                    LOG.debug("  %s: 100%%", label)
            else:
                errors.append(line)
                if len(errors) > 40:
                    errors = errors[-40:]
            if STOP and proc.poll() is None:
                proc.terminate()
        rc = proc.wait()
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        raise

    if STOP:
        raise InterruptedError("interrumpido")
    if rc != 0:
        raise MediaError(f"ffmpeg falló (código {rc}): {tails(' | '.join(errors), 4)}")


# ---------------------------------------------------------------------------
# Procesamiento
# ---------------------------------------------------------------------------


def output_path_for(path: Path, plan: Plan, cfg: Config, state: State) -> Path:
    cfg.output.mkdir(parents=True, exist_ok=True)
    stem = path.stem
    if cfg.output == path.parent:
        stem += " [convertido]"
    dest = cfg.output / f"{stem}{plan.output_ext}"
    record = state.get(path)
    same_output = bool(record and record.get("output") == str(dest))
    if dest.exists() and not same_output:
        index = 2
        while True:
            candidate = cfg.output / f"{stem} ({index}){plan.output_ext}"
            if not candidate.exists():
                dest = candidate
                break
            index += 1
    return dest


def should_skip(path: Path, stat: os.stat_result, cfg: Config, state: State) -> bool:
    record = state.get(path)
    if not record:
        return False
    if record.get("size") != stat.st_size or record.get("mtime_ns") != stat.st_mtime_ns:
        return False
    status = record.get("status")
    if status == "failed":
        return not cfg.retry_failed
    if status in ("done", "native"):
        out = record.get("output")
        if not out:
            return False
        try:
            return Path(out).exists() and Path(out).stat().st_size > 0
        except OSError:
            return False
    return False


def process_file(
    path: Path,
    cfg: Config,
    state: State,
    import_after: bool = True,
    force: bool = False,
) -> ProcessResult:
    path = Path(os.path.abspath(str(path)))
    try:
        stat = path.stat()
    except OSError as exc:
        return ProcessResult(path, None, False, False, "none", "", str(exc))

    if not force and should_skip(path, stat, cfg, state):
        record = state.get(path) or {}
        if record.get("status") == "failed":
            return ProcessResult(
                path,
                None,
                False,
                False,
                str(record.get("mode", "none")),
                str(record.get("reason", "")),
                str(record.get("error", "falló en un intento anterior")),
            )
        return ProcessResult(
            path,
            Path(record["output"]) if record.get("output") else None,
            bool(record.get("mode") not in (None, "none")),
            True,
            str(record.get("mode", "none")),
            str(record.get("reason", "ya procesado")),
        )

    try:
        info = probe_media(path, cfg)
    except MediaError as exc:
        LOG.error("No puedo leer %s: %s", path.name, exc)
        state.mark(path, stat, "failed", None, "probe", "", str(exc))
        state.save()
        notify(cfg, "No se pudo leer un archivo", f"{path.name}: {exc}", "critical")
        return ProcessResult(path, None, False, False, "probe", "", str(exc))

    plan = plan_for(info, cfg)
    if not plan.needed:
        state.mark(path, stat, "native", path, "none", plan.reason)
        state.save()
        LOG.info("Compatible, no se toca: %s (%s)", path.name, plan.reason)
        if import_after and cfg.auto_import:
            state.add_pending(path)
            state.save()
        return ProcessResult(path, path, False, True, "none", plan.reason)

    dest = output_path_for(path, plan, cfg, state)
    temp = dest.with_name(f"{dest.stem}.part{dest.suffix}")
    try:
        temp.unlink(missing_ok=True)
    except OSError:
        pass

    LOG.info(
        "Convirtiendo %s [%s] → %s (%s)",
        path.name,
        plan.reason,
        dest.name,
        plan.profile or plan.mode,
    )
    try:
        cmd = build_ffmpeg_cmd(info, plan, path, temp, cfg)
        try:
            run_ffmpeg(cmd, info.duration, path.name)
        except MediaError:
            if plan.mode in ("remux", "audio"):
                LOG.warning("El copiado directo falló; reintento con transcodificación completa.")
                plan = Plan(True, "full", ".mov", plan.reason, target_profile(info, cfg))
                dest = output_path_for(path, plan, cfg, state)
                temp = dest.with_name(f"{dest.stem}.part{dest.suffix}")
                cmd = build_ffmpeg_cmd(info, plan, path, temp, cfg)
                run_ffmpeg(cmd, info.duration, path.name)
            else:
                raise

        if not temp.exists() or temp.stat().st_size == 0:
            raise MediaError("ffmpeg no generó un archivo válido")
        out_info = probe_media(temp, cfg)
        if plan.mode == "full" and out_info.video_codec != "dnxhd":
            raise MediaError(
                f"la salida no es DNxHR (codec: {out_info.video_codec or '?'})"
            )
        if not out_info.video_codec and not out_info.audio_codecs:
            raise MediaError("la salida no tiene pistas de vídeo ni audio")

        after = path.stat()
        if after.st_size != stat.st_size or after.st_mtime_ns != stat.st_mtime_ns:
            raise MediaError("el archivo original cambió durante la conversión")
        os.replace(temp, dest)

        state.mark(path, stat, "done", dest, plan.mode, plan.reason)
        state.save()
        LOG.info("Listo: %s (%s)", dest.name, human_size(dest.stat().st_size))
        notify(cfg, "Conversión completada", f"{path.name} → {dest.name}")

        if cfg.move_original:
            originals = cfg.inbox / "_originales"
            originals.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(path), str(originals / path.name))
                LOG.info("Original movido a %s", originals)
            except OSError as exc:
                LOG.warning("No pude mover el original %s: %s", path.name, exc)

        if import_after and cfg.auto_import:
            state.add_pending(dest)
            state.save()
        return ProcessResult(path, dest, True, True, plan.mode, plan.reason)
    except InterruptedError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except Exception as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        LOG.error("Error convirtiendo %s: %s", path.name, exc)
        state.mark(path, stat, "failed", None, plan.mode, plan.reason, str(exc))
        state.save()
        notify(cfg, "Falló una conversión", f"{path.name}: {exc}", "critical")
        return ProcessResult(path, None, False, False, plan.mode, plan.reason, str(exc))


# ---------------------------------------------------------------------------
# Importación a Resolve
# ---------------------------------------------------------------------------


def resolve_running() -> bool:
    for pattern in ("resolve", "/opt/resolve/bin/resolve"):
        try:
            proc = subprocess.run(
                ["pgrep", "-x" if pattern == "resolve" else "-f", pattern],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if proc.returncode == 0:
                return True
        except FileNotFoundError:
            return False
    return False


def resolve_api_env() -> dict[str, str]:
    env = os.environ.copy()
    env["RESOLVE_SCRIPT_API"] = str(RESOLVE_SCRIPT_API)
    env["RESOLVE_SCRIPT_LIB"] = str(RESOLVE_SCRIPT_LIB)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(RESOLVE_SCRIPT_API / "Modules") + (os.pathsep + existing if existing else "")
    )
    return env


def resolve_python() -> tuple[str, dict[str, str]]:
    if RESOLVE_PYTHON.exists():
        return str(RESOLVE_PYTHON), os.environ.copy()
    return sys.executable, resolve_api_env()


def detect_edition(cfg: Config) -> str:
    if cfg.edition in ("free", "studio"):
        return cfg.edition

    pacman = which("pacman")
    if pacman:
        for package, edition in (
            ("davinci-resolve-studio", "studio"),
            ("davinci-resolve", "free"),
        ):
            try:
                if subprocess.run(
                    [pacman, "-Q", package],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode == 0:
                    return edition
            except OSError:
                pass

    if resolve_running():
        python, env = resolve_python()
        code = (
            "import DaVinciResolveScript as d\n"
            "r=d.scriptapp('Resolve')\n"
            "print('STUDIO' if r and r.IsStudio() else ('FREE' if r else ''))\n"
        )
        try:
            proc = subprocess.run(
                [python, "-c", code],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=15,
            )
            out = (proc.stdout or "").strip()
            if "STUDIO" in out:
                return "studio"
            if "FREE" in out:
                return "free"
        except Exception:
            pass
    return "free"


def import_into_resolve(cfg: Config, paths: list[Path]) -> tuple[bool, str]:
    if not paths:
        return True, ""
    if not resolve_running():
        return False, "Resolve no está abierto"

    python, env = resolve_python()
    job = {
        "paths": [str(p) for p in paths],
        "bin": cfg.bin_name,
    }
    fd, result_name = tempfile.mkstemp(
        prefix="import-", suffix=".json", dir=str(cfg.state_dir)
    )
    os.close(fd)
    try:
        env = env.copy()
        env["RESOLVE_INGEST_JOB"] = json.dumps(job, ensure_ascii=False)
        env["RESOLVE_INGEST_RESULT"] = result_name
        try:
            proc = subprocess.run(
                [python, "-c", IMPORT_HELPER],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=cfg.scripting_timeout,
            )
        except subprocess.TimeoutExpired:
            return False, "la API de Resolve no respondió a tiempo"

        payload: dict[str, Any] = {}
        try:
            payload = json.loads(Path(result_name).read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if not payload:
            detail = tails(proc.stderr or proc.stdout or "", 3)
            return False, f"sin respuesta de la API de Resolve ({detail or proc.returncode})"
        if payload.get("ok"):
            count = len(payload.get("imported") or [])
            LOG.info("Importados %d clip(s) en Resolve.", count)
            return True, ""
        return False, str(payload.get("error") or "error desconocido")
    finally:
        try:
            Path(result_name).unlink(missing_ok=True)
        except OSError:
            pass


def enqueue_outputs(cfg: Config, state: State) -> None:
    if not cfg.auto_import or not cfg.output.exists():
        return
    changed = False
    for path in cfg.output.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in ALL_MEDIA_EXTS:
            continue
        if ".part" in path.name:
            continue
        before = len(state.data["pending_import"])
        state.add_pending(path)
        changed = changed or len(state.data["pending_import"]) != before
    if changed:
        state.save()


def retry_imports(cfg: Config, state: State, force: bool = False) -> None:
    global _LAST_IMPORT_WARNING
    if not cfg.auto_import:
        return
    pending = state.pending()
    if not pending:
        return
    if not resolve_running():
        LOG.debug("Hay %d archivo(s) pendientes de importar; Resolve está cerrado.", len(pending))
        return
    ok, error = import_into_resolve(cfg, pending)
    if ok:
        state.remove_pending(pending)
        state.save()
        notify(cfg, "Medios importados en Resolve", f"{len(pending)} clip(s) añadidos.")
    else:
        now = time.monotonic()
        if force or now - _LAST_IMPORT_WARNING > 300:
            LOG.warning("No pude importar en Resolve: %s", error)
            _LAST_IMPORT_WARNING = now
        else:
            LOG.debug("No pude importar en Resolve: %s", error)
        if force:
            notify(cfg, "No pude importar en Resolve", error, "critical")


def import_with_current_resolve(
    resolve: Any, paths: list[Path], bin_name: str
) -> tuple[bool, str]:
    try:
        project = resolve.GetProjectManager().GetCurrentProject()
        if not project:
            return False, "no hay ningún proyecto abierto"
        media_pool = project.GetMediaPool()
        root = media_pool.GetRootFolder()
        target = root
        for sub in root.GetSubFolderList() or []:
            if sub.GetName() == bin_name:
                target = sub
                break
        else:
            target = media_pool.AddSubFolder(root, bin_name) or root
        media_pool.SetCurrentFolder(target)
        existing: set[str] = set()
        for clip in target.GetClipList() or []:
            try:
                clip_path = clip.GetClipProperty("File Path")
            except Exception:
                clip_path = ""
            if clip_path:
                existing.add(os.path.abspath(clip_path))
                existing.add(os.path.realpath(clip_path))
        new_paths = [
            p for p in paths
            if os.path.abspath(str(p)) not in existing
            and os.path.realpath(str(p)) not in existing
        ]
        if not new_paths:
            return True, ""
        version = resolve.GetVersion() or []
        try:
            major = int(version[0])
        except Exception:
            major = 0
        infos = [{"FilePath": str(p)} for p in new_paths] if major >= 21 else [str(p) for p in new_paths]
        imported = media_pool.ImportMedia(infos) or []
        if not imported:
            return False, "ImportMedia no devolvió clips"
        return True, ""
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Vigilante
# ---------------------------------------------------------------------------


def iter_media_files(cfg: Config) -> Iterable[Path]:
    if not cfg.inbox.exists():
        return
    skip_roots = {cfg.output, cfg.failed}
    for root, dirs, files in os.walk(cfg.inbox):
        root_path = Path(root)
        pruned = []
        for name in dirs:
            candidate = root_path / name
            if cfg.skip_hidden and name.startswith("."):
                continue
            if candidate in skip_roots:
                continue
            if any(candidate == skip for skip in skip_roots):
                continue
            pruned.append(name)
        dirs[:] = pruned

        for name in files:
            if cfg.skip_hidden and name.startswith("."):
                continue
            lower = name.lower()
            if lower.endswith(TEMP_SUFFIXES):
                continue
            path = root_path / name
            if cfg.output == cfg.inbox and "[convertido]" in name:
                continue
            if path.suffix.lower() not in ALL_MEDIA_EXTS:
                continue
            if path.suffix.lower() in SIDECAR_EXTS:
                continue
            try:
                if not path.is_file() or path.stat().st_size == 0:
                    continue
            except OSError:
                continue
            yield path


class Watcher:
    def __init__(self, cfg: Config, state: State):
        self.cfg = cfg
        self.state = state
        self.stable: dict[str, tuple[tuple[int, int], float]] = {}
        self.retry_after: dict[str, float] = {}
        self.last_import_attempt = 0.0
        self.last_edition_check = 0.0

    def scan(self, force: bool = False) -> int:
        processed = 0
        for path in list(iter_media_files(self.cfg)):
            key = str(path)
            try:
                stat = path.stat()
            except OSError:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            previous = self.stable.get(key)
            if not force:
                if previous is None or previous[0] != signature:
                    self.stable[key] = (signature, time.monotonic())
                    continue
                if time.monotonic() - previous[1] < self.cfg.stability:
                    continue
            if not force and should_skip(path, stat, self.cfg, self.state):
                continue
            if not force:
                retry_at = self.retry_after.get(key)
                if retry_at is not None:
                    if time.monotonic() < retry_at:
                        continue
                    self.retry_after.pop(key, None)
            try:
                result = process_file(path, self.cfg, self.state, import_after=True, force=force)
                processed += 1
                if result.ok:
                    self.stable.pop(key, None)
                    self.retry_after.pop(key, None)
                else:
                    self.retry_after[key] = time.monotonic() + 300
            except InterruptedError:
                raise
            except Exception as exc:
                LOG.exception("Error inesperado con %s: %s", path, exc)
        return processed

    def maybe_import(self) -> None:
        if (
            self.cfg.auto_import_auto
            and not self.cfg.auto_import
            and resolve_running()
            and time.monotonic() - self.last_edition_check > 60
        ):
            self.last_edition_check = time.monotonic()
            edition = detect_edition(self.cfg)
            if edition == "studio":
                self.cfg.auto_import = True
                LOG.info("Studio detectado: activo la importación automática en Resolve.")
                enqueue_outputs(self.cfg, self.state)
        if not self.state.pending():
            return
        if time.monotonic() - self.last_import_attempt < 20:
            return
        self.last_import_attempt = time.monotonic()
        retry_imports(self.cfg, self.state)

    def run(self) -> int:
        LOG.info("Vigilando %s", self.cfg.inbox)
        LOG.info("Salida: %s", self.cfg.output)
        LOG.info("Edición de Resolve: %s", self.cfg.resolved_edition)
        while not STOP:
            try:
                self.maybe_import()
                self.scan()
            except InterruptedError:
                break
            except Exception as exc:
                LOG.exception("Error en el vigilante: %s", exc)
            for _ in range(int(max(1, self.cfg.poll * 5))):
                if STOP:
                    break
                time.sleep(0.2)
        LOG.info("Detenido.")
        return 0


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------


def prepare_runtime(cfg: Config) -> None:
    for directory in (cfg.inbox, cfg.output, cfg.failed, cfg.state_dir):
        directory.mkdir(parents=True, exist_ok=True)
    resolve_obj = globals().get("resolve")
    if resolve_obj is not None:
        try:
            cfg.resolved_edition = "studio" if resolve_obj.IsStudio() else "free"
        except Exception:
            cfg.resolved_edition = detect_edition(cfg)
    else:
        cfg.resolved_edition = detect_edition(cfg)
    if cfg.auto_import_auto:
        cfg.auto_import = cfg.resolved_edition == "studio"
    LOG.info("Auto-importar en Resolve: %s", "sí" if cfg.auto_import else "no")
    if cfg.resolved_edition == "free":
        LOG.info(
            "Edición free: la importación automática externa puede estar limitada. "
            "La conversión funciona igual; para importar usa Convertidos o el script "
            "Workspace > Scripts > Utility > Resolve Ingest."
        )


def flatten_paths(paths: list[str], cfg: Config) -> list[Path]:
    result: list[Path] = []
    for raw in paths:
        path = Path(os.path.abspath(os.path.expanduser(raw)))
        if path.is_dir():
            for child in iter_media_files(
                dataclasses.replace(cfg, inbox=path, output=cfg.output, failed=cfg.failed)
            ):
                result.append(child)
        elif path.is_file():
            result.append(path)
        else:
            LOG.error("No existe: %s", path)
    return result


def queue_for_watcher(paths: list[Path], cfg: Config) -> int:
    cfg.inbox.mkdir(parents=True, exist_ok=True)
    queued = 0
    for path in paths:
        if path.suffix.lower() not in ALL_MEDIA_EXTS or not path.is_file():
            continue
        if path.parent == cfg.inbox:
            LOG.info("Ya está en la carpeta vigilada: %s", path.name)
            queued += 1
            continue
        target = cfg.inbox / path.name
        if target.exists():
            try:
                if target.samefile(path):
                    continue
            except OSError:
                pass
            index = 2
            while target.exists():
                target = cfg.inbox / f"{path.stem} ({index}){path.suffix}"
                index += 1
        try:
            os.link(path, target)
            LOG.info("Encolado (hardlink): %s", target.name)
        except OSError:
            try:
                os.symlink(path, target)
                LOG.info("Encolado (symlink): %s", target.name)
            except OSError:
                try:
                    shutil.copy2(path, target)
                    LOG.info("Encolado (copia): %s", target.name)
                except OSError as exc:
                    LOG.error("No pude encolar %s: %s", path, exc)
                    continue
        queued += 1
    return queued


def cmd_watch(cfg: Config, args: argparse.Namespace) -> int:
    prepare_runtime(cfg)
    lock = InstanceLock(cfg.lock_path)
    if not lock.acquire():
        LOG.error("Ya hay otro resolve-ingest vigilando. Revisa: systemctl --user status %s", SERVICE_NAME)
        return 1
    try:
        state = State(cfg.state_path)
        state.prune_pending()
        enqueue_outputs(cfg, state)
        retry_imports(cfg, state)
        watcher = Watcher(cfg, state)
        return watcher.run()
    finally:
        lock.release()


def cmd_once(cfg: Config, args: argparse.Namespace) -> int:
    prepare_runtime(cfg)
    paths = flatten_paths(args.paths, cfg)
    if not paths:
        LOG.error("No hay archivos que procesar.")
        return 2

    lock = InstanceLock(cfg.lock_path)
    if not lock.acquire():
        if args.no_import:
            LOG.error("El servicio está activo; no puedo procesar en paralelo sin importar.")
            return 1
        LOG.info("El servicio está activo; encolando en la carpeta vigilada.")
        queued = queue_for_watcher(paths, cfg)
        print(f"Encolados {queued} archivo(s) en {cfg.inbox}")
        return 0 if queued else 1

    failures = 0
    try:
        state = State(cfg.state_path)
        for path in paths:
            try:
                result = process_file(
                    path,
                    cfg,
                    state,
                    import_after=not args.no_import,
                    force=args.force,
                )
            except InterruptedError:
                return 130
            if not result.ok:
                failures += 1
                print(f"ERROR: {path.name}: {result.error}")
            elif result.converted:
                print(f"OK: {path.name} → {result.output}")
            else:
                print(f"OK: {path.name} ya era compatible")
        if not args.no_import and cfg.auto_import:
            enqueue_outputs(cfg, state)
            retry_imports(cfg, state, force=True)
    finally:
        lock.release()
    return 0 if failures == 0 else 1


def cmd_scan(cfg: Config, args: argparse.Namespace) -> int:
    prepare_runtime(cfg)
    lock = InstanceLock(cfg.lock_path)
    if not lock.acquire():
        LOG.error("El servicio ya está vigilando; no hace falta scan.")
        return 1
    try:
        state = State(cfg.state_path)
        watcher = Watcher(cfg, state)
        processed = watcher.scan(force=True)
        retry_imports(cfg, state, force=True)
        print(f"Procesados {processed} archivo(s).")
        return 0
    finally:
        lock.release()


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    ok = True
    print(f"resolve-ingest {VERSION}")
    print(f"Config: {cfg.path}")
    print(f"Inbox:  {cfg.inbox}")
    print(f"Salida: {cfg.output}")
    print()

    for label, exe in (("ffmpeg", cfg.ffmpeg), ("ffprobe", cfg.ffprobe)):
        path = which(exe) if not os.path.isabs(exe) else (exe if Path(exe).exists() else None)
        if path:
            try:
                out = subprocess.run(
                    [path, "-version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, timeout=15
                ).stdout.splitlines()[0]
            except Exception:
                out = path
            print(f"[OK] {label}: {out}")
        else:
            print(f"[FALLO] {label} no encontrado")
            ok = False

    try:
        encoders = subprocess.run(
            [cfg.ffmpeg, "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20
        ).stdout
        for codec in ("dnxhd", "pcm_s24le"):
            if codec in encoders:
                print(f"[OK] encoder {codec}")
            else:
                print(f"[FALLO] falta el encoder {codec} en ffmpeg")
                ok = False
    except Exception as exc:
        print(f"[FALLO] no pude listar encoders: {exc}")
        ok = False

    for directory in (cfg.inbox, cfg.output, cfg.failed, cfg.state_dir):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / f".write-test-{os.getpid()}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            print(f"[OK] escritura: {directory}")
        except OSError as exc:
            print(f"[FALLO] sin escritura en {directory}: {exc}")
            ok = False

    try:
        free = shutil.disk_usage(cfg.output).free
        print(f"[INFO] espacio libre en salida: {human_size(free)}")
        if free < 2 * 1024**3:
            print("[AVISO] menos de 2 GiB libres; una conversión DNxHR puede llenar el disco.")
    except OSError:
        pass

    if RESOLVE_BIN.exists():
        print(f"[OK] Resolve instalado: {RESOLVE_BIN}")
    else:
        print(f"[AVISO] no encontré {RESOLVE_BIN}")
    if RESOLVE_PYTHON.exists():
        print(f"[OK] intérprete de Resolve: {RESOLVE_PYTHON}")
    else:
        print("[AVISO] no encontré ResolvePython; la importación automática usará Python del sistema.")

    cfg.resolved_edition = detect_edition(cfg)
    print(f"[INFO] edición detectada: {cfg.resolved_edition}")
    if cfg.resolved_edition == "free":
        print("[INFO] en la versión free se transcodifican H.264/H.265; es lo esperado en Linux.")
    else:
        print("[INFO] Studio: H.264/H.265 se consideran compatibles.")

    if resolve_running():
        python, env = resolve_python()
        code = (
            "import DaVinciResolveScript as d\n"
            "r=d.scriptapp('Resolve')\n"
            "if not r:\n"
            "    print('SIN_CONEXION')\n"
            "else:\n"
            "    print('CONECTADO', r.GetProductName(), 'STUDIO' if r.IsStudio() else 'FREE')\n"
        )
        try:
            proc = subprocess.run(
                [python, "-c", code], env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=20
            )
            out = (proc.stdout or "").strip()
            if "CONECTADO" in out:
                print(f"[OK] API de Resolve: {out}")
            else:
                print("[AVISO] Resolve está abierto pero la API no conecta.")
                if cfg.resolved_edition == "free":
                    print("        La edición free no suele permitir scripting externo.")
                    print("        No pasa nada: convierte igual y usa el script del menú de Resolve.")
                print("        Si tu versión lo permite: Preferencias > Sistema > General > External scripting = Local.")
        except Exception as exc:
            print(f"[AVISO] no pude probar la API de Resolve: {exc}")
    else:
        print("[INFO] Resolve no está abierto; no pruebo la API de importación.")

    print("\nAutoprueba de conversión (1 segundo)...")
    try:
        with tempfile.TemporaryDirectory(prefix="resolve-ingest-doctor-") as tmp:
            tmp_path = Path(tmp)
            sample = tmp_path / "muestra.mp4"
            gen = [
                cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                "-t", "1", "-c:v", "mpeg4", "-q:v", "5",
                "-c:a", "aac", "-shortest", str(sample),
            ]
            subprocess.run(gen, check=True, timeout=30)
            test_cfg = dataclasses.replace(
                cfg,
                output=tmp_path / "out",
                failed=tmp_path / "failed",
                state_dir=tmp_path / "state",
                log_file=tmp_path / "state" / "test.log",
                auto_import=False,
                notify_enabled=False,
                video_profile="dnxhr_hq",
                edition="free",
                resolved_edition="free",
            )
            test_cfg.output.mkdir(parents=True, exist_ok=True)
            test_cfg.state_dir.mkdir(parents=True, exist_ok=True)
            state = State(test_cfg.state_path)
            result = process_file(sample, test_cfg, state, import_after=False, force=True)
            if result.ok and result.output and result.output.exists():
                info = probe_media(result.output, test_cfg)
                print(f"[OK] muestra convertida: {info.video_codec} + {', '.join(info.audio_codecs)}")
            else:
                print(f"[FALLO] autoprueba: {result.error}")
                ok = False
    except Exception as exc:
        print(f"[FALLO] autoprueba: {exc}")
        ok = False

    print("\nResultado:", "todo bien" if ok else "hay fallos que revisar")
    return 0 if ok else 1


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    state = State(cfg.state_path)
    processed = state.data.get("processed", {})
    done = sum(1 for r in processed.values() if r.get("status") in ("done", "native"))
    failed = sum(1 for r in processed.values() if r.get("status") == "failed")
    pending = len(state.pending())
    cfg.resolved_edition = detect_edition(cfg)
    print(f"resolve-ingest {VERSION}")
    print(f"Config:  {cfg.path}")
    print(f"Inbox:   {cfg.inbox}")
    print(f"Salida:  {cfg.output}")
    print(f"Edición: {cfg.resolved_edition}")
    print(f"Estado:  {done} procesados, {failed} fallidos, {pending} pendientes de importar")
    if pending and cfg.resolved_edition == "free":
        print("        (en free, importa desde Convertidos o con el script del menú de Resolve)")
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE_NAME],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5
        )
        print(f"Servicio: {proc.stdout.strip() or 'desconocido'}")
    except Exception:
        print("Servicio: no pude consultar systemd")
    if failed:
        print("\nÚltimos fallos:")
        shown = 0
        for path, record in reversed(list(processed.items())):
            if record.get("status") == "failed" and shown < 5:
                print(f"  {path}: {record.get('error', '')}")
                shown += 1
    if cfg.log_file.exists():
        print(f"\nÚltimas líneas de {cfg.log_file}:")
        try:
            for line in cfg.log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]:
                print("  " + line)
        except OSError:
            pass
    return 0


def cmd_config(cfg: Config, args: argparse.Namespace) -> int:
    print(f"# {cfg.path}\n")
    print(cfg.path.read_text(encoding="utf-8"))
    return 0


# ---------------------------------------------------------------------------
# Instalación
# ---------------------------------------------------------------------------


def service_unit(binary: Path, config: Path) -> str:
    return f"""\
[Unit]
Description=Resolve Ingest - conversion automatica para DaVinci Resolve
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={systemd_quote(str(binary))} --config {systemd_quote(str(config))} watch
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def desktop_entries(binary: Path, cfg: Config) -> dict[Path, str]:
    apps = HOME / ".local" / "share" / "applications"
    open_entry = f"""\
[Desktop Entry]
Type=Application
Name=Abrir carpeta de ingesta para Resolve
Comment=Abre la carpeta que vigila resolve-ingest
Exec=xdg-open {shlex.quote(str(cfg.inbox))}
Icon=folder-video
Terminal=false
Categories=AudioVideo;Video;
"""
    convert_entry = f"""\
[Desktop Entry]
Type=Application
Name=Convertir para DaVinci Resolve
Comment=Convierte los archivos seleccionados a un formato compatible con Resolve
Exec={shlex.quote(str(binary))} --config {shlex.quote(str(cfg.path))} once %F
Icon=applications-multimedia
Terminal=true
MimeType=video/*;audio/*;
Categories=AudioVideo;Video;
"""
    return {
        apps / "resolve-ingest-open.desktop": open_entry,
        apps / "resolve-ingest-convert.desktop": convert_entry,
    }


def cmd_install(cfg: Config, args: argparse.Namespace) -> int:
    prepare_runtime(cfg)
    source = Path(__file__).resolve()
    binary = HOME / ".local" / "bin" / APP
    binary.parent.mkdir(parents=True, exist_ok=True)
    if source != binary:
        shutil.copy2(source, binary)
    binary.chmod(0o755)
    print(f"Instalado: {binary}")

    unit = HOME / ".config" / "systemd" / "user" / SERVICE_NAME
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(service_unit(binary, cfg.path), encoding="utf-8")
    print(f"Unidad systemd: {unit}")

    for path, content in desktop_entries(binary, cfg).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"Acceso: {path}")

    if args.resolve_script:
        scripts = HOME / ".local" / "share" / "DaVinciResolve" / "Fusion" / "Scripts" / "Utility"
        scripts.mkdir(parents=True, exist_ok=True)
        target = scripts / "Resolve Ingest.py"
        if source != target:
            shutil.copy2(source, target)
        target.chmod(0o755)
        print(f"Script de Resolve: {target}")
        print("  Reinicia Resolve para verlo en Workspace > Scripts > Utility.")

    if not args.no_service:
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=20)
            subprocess.run(
                ["systemctl", "--user", "enable", "--now", SERVICE_NAME],
                check=False, timeout=30,
            )
            subprocess.run(
                ["systemctl", "--user", "restart", SERVICE_NAME],
                check=False, timeout=30,
            )
            active = subprocess.run(
                ["systemctl", "--user", "is-active", SERVICE_NAME],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10
            ).stdout.strip()
            print(f"Servicio: {active or 'no se pudo iniciar'}")
            if active != "active":
                print(f"  Revisa: systemctl --user status {SERVICE_NAME}")
        except Exception as exc:
            print(f"No pude activar systemd: {exc}")
            print(f"  Manual: systemctl --user daemon-reload && systemctl --user enable --now {SERVICE_NAME}")
    else:
        print("Servicio no activado (--no-service).")

    print("\nSiguiente paso: deja un archivo en " + str(cfg.inbox))
    if cfg.resolved_edition == "free":
        print("Nota: en la edición free la importación externa a Resolve puede no estar disponible.")
        print("      Los archivos convertidos quedan en " + str(cfg.output))
        print("      y puedes importarlos con Workspace > Scripts > Utility > Resolve Ingest.")
    print("Comprueba todo con: " + str(binary) + " doctor")
    return 0


def cmd_uninstall(cfg: Config, args: argparse.Namespace) -> int:
    try:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", SERVICE_NAME],
            check=False, timeout=30,
        )
    except Exception:
        pass
    unit = HOME / ".config" / "systemd" / "user" / SERVICE_NAME
    unit.unlink(missing_ok=True)
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=20)
    except Exception:
        pass
    binary = HOME / ".local" / "bin" / APP
    binary.unlink(missing_ok=True)
    for name in ("resolve-ingest-open.desktop", "resolve-ingest-convert.desktop"):
        (HOME / ".local" / "share" / "applications" / name).unlink(missing_ok=True)
    script = (
        HOME / ".local" / "share" / "DaVinciResolve" / "Fusion" / "Scripts"
        / "Utility" / "Resolve Ingest.py"
    )
    script.unlink(missing_ok=True)
    if args.purge:
        cfg.path.unlink(missing_ok=True)
        shutil.rmtree(cfg.state_dir, ignore_errors=True)
        print("Configuración y estado eliminados. Los medios no se tocan.")
    print("Desinstalado. La carpeta de ingesta y los medios se conservan.")
    return 0


# ---------------------------------------------------------------------------
# Modo script de Resolve
# ---------------------------------------------------------------------------


def resolve_menu() -> int:
    config_path = Path(os.environ.get("RESOLVE_INGEST_CONFIG", str(DEFAULT_CONFIG)))
    cfg = load_config(config_path)
    setup_logging(cfg, verbose=True)
    prepare_runtime(cfg)
    state = State(cfg.state_path)

    resolve = globals().get("resolve")
    if resolve is None:
        print("Este modo solo funciona dentro de DaVinci Resolve.")
        return 1
    try:
        cfg.resolved_edition = "studio" if resolve.IsStudio() else "free"
    except Exception:
        pass
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        print("Abre un proyecto antes de ejecutar Resolve Ingest.")
        return 1
    media_pool = project.GetMediaPool()
    clips = media_pool.GetSelectedClips() or []
    if not clips:
        candidates = sorted(
            p for p in cfg.output.iterdir()
            if p.is_file() and p.suffix.lower() in ALL_MEDIA_EXTS
        ) if cfg.output.exists() else []
        if not candidates:
            print("No hay clips seleccionados y la carpeta Convertidos está vacía.")
            print("Deja archivos en " + str(cfg.inbox) + " o selecciona clips en el Media Pool.")
            return 1
        print(f"Sin selección: importando {len(candidates)} archivo(s) de Convertidos...")
        ok, error = import_with_current_resolve(resolve, candidates, cfg.bin_name)
        if ok:
            print(f"Importados en el bin '{cfg.bin_name}'.")
            return 0
        print(f"No pude importar: {error}")
        return 1

    outputs: list[Path] = []
    for clip in clips:
        try:
            raw_path = clip.GetClipProperty("File Path")
        except Exception:
            raw_path = ""
        if not raw_path:
            print(f"Sin ruta de archivo (quizá sea un clip generado): {clip.GetName()}")
            continue
        path = Path(raw_path)
        if not path.exists():
            print(f"No existe: {path}")
            continue
        print(f"Procesando {path.name}...")
        try:
            result = process_file(path, cfg, state, import_after=False, force=False)
        except InterruptedError:
            print("Interrumpido.")
            return 130
        if not result.ok:
            print(f"  ERROR: {result.error}")
        elif result.converted:
            print(f"  Convertido: {result.output}")
            outputs.append(result.output)
        else:
            print("  Ya era compatible; no hace falta convertirlo.")

    if outputs:
        ok, error = import_with_current_resolve(resolve, outputs, cfg.bin_name)
        if ok:
            print(f"Importados {len(outputs)} clip(s) en el bin '{cfg.bin_name}'.")
        else:
            print(f"No pude importar automáticamente: {error}")
            print("Los archivos convertidos están en: " + str(cfg.output))
    return 0


# ---------------------------------------------------------------------------
# Entrada
# ---------------------------------------------------------------------------


def install_signal_handlers() -> None:
    def handler(signum: int, frame: Any) -> None:
        global STOP
        STOP = True
        LOG.info("Señal %s recibida; terminando tras el archivo actual...", signum)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP,
        description="Convierte automáticamente medios para DaVinci Resolve en Linux.",
        epilog="Sin comando: vigila la carpeta de ingesta (modo servicio).",
    )
    parser.add_argument("--config", default=os.environ.get("RESOLVE_INGEST_CONFIG", str(DEFAULT_CONFIG)),
                        help="ruta del config.toml")
    parser.add_argument("--verbose", action="store_true", help="log detallado")
    parser.add_argument("--version", action="version", version=f"{APP} {VERSION}")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("watch", help="vigila la carpeta (por defecto)")
    once = sub.add_parser("once", help="procesa archivos o carpetas ahora")
    once.add_argument("paths", nargs="+", help="archivos o carpetas")
    once.add_argument("--force", action="store_true", help="reprocesa aunque ya esté en el estado")
    once.add_argument("--no-import", action="store_true", help="no importar en Resolve")
    sub.add_parser("scan", help="procesa todo lo que ya está en la carpeta")
    sub.add_parser("doctor", help="diagnóstico y autoprueba")
    sub.add_parser("status", help="estado del servicio y de la cola")
    sub.add_parser("config", help="muestra la configuración")
    install = sub.add_parser("install", help="instala el servicio y accesos directos")
    install.add_argument("--no-service", action="store_true", help="no activar systemd")
    install.add_argument("--resolve-script", action="store_true",
                         help="instala el script en el menú Workspace > Scripts de Resolve")
    uninstall = sub.add_parser("uninstall", help="desinstala servicio y accesos directos")
    uninstall.add_argument("--purge", action="store_true", help="borra también configuración y estado")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if (not argv and "resolve" in globals()) or (
        argv and argv[0] == "--resolve-menu"
    ):
        return resolve_menu()

    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(Path(args.config))
    setup_logging(cfg, args.verbose)
    install_signal_handlers()

    command = args.cmd or "watch"
    try:
        if command == "watch":
            return cmd_watch(cfg, args)
        if command == "once":
            return cmd_once(cfg, args)
        if command == "scan":
            return cmd_scan(cfg, args)
        if command == "doctor":
            return cmd_doctor(cfg, args)
        if command == "status":
            return cmd_status(cfg, args)
        if command == "config":
            return cmd_config(cfg, args)
        if command == "install":
            return cmd_install(cfg, args)
        if command == "uninstall":
            return cmd_uninstall(cfg, args)
        parser.error(f"comando desconocido: {command}")
        return 2
    except KeyboardInterrupt:
        LOG.info("Interrumpido por el usuario.")
        return 130


if __name__ == "__main__":
    sys.exit(main())

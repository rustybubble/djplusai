"""Adding tracks to the DJ library from YouTube.

Audio is fetched with yt-dlp, converted to 320 kbps MP3 with pydub (which
drives ffmpeg), tagged with title/artist/duration and stored in the download
folder (``~/dj-library/downloads`` by default, or ``$DJPLUSAI_DOWNLOAD_DIR``).
Every MP3 gets a sidecar ``<name>.source`` JSON file recording the original URL
so the same video is never downloaded twice.

yt-dlp and pydub are optional (``pip install "djplusai[youtube]"``) and only
imported when a search or download actually runs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

DOWNLOAD_DIR_ENV = "DJPLUSAI_DOWNLOAD_DIR"
DEFAULT_DOWNLOAD_DIR = Path.home() / "dj-library" / "downloads"
MP3_BITRATE = "320k"
SOURCE_SUFFIX = ".source"

FFMPEG_INSTALL_HELP = (
    "ffmpeg is required to convert audio to MP3 but was not found on PATH.\n"
    "Install it and try again:\n"
    "  macOS:          brew install ffmpeg\n"
    "  Debian/Ubuntu:  sudo apt install ffmpeg\n"
    "  Fedora:         sudo dnf install ffmpeg\n"
    "  Windows:        winget install ffmpeg  (or choco install ffmpeg)\n"
    "Or set FFMPEG_PATH to the full path of the ffmpeg binary."
)

MISSING_DEPS_HELP = 'YouTube support needs yt-dlp and pydub: pip install "djplusai[youtube]"'

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
# Bracketed decorations that don't belong in a DJ library title.
_TITLE_NOISE_RE = re.compile(
    r"\s*[\(\[][^\)\]]*\b(official|lyrics?|audio|video|visuali[sz]er|hd|hq|4k|mv)\b[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class DownloadError(RuntimeError):
    """Raised when a track cannot be fetched or converted."""

    @classmethod
    def from_ytdlp(cls, exc: Exception) -> "DownloadError":
        message = re.sub(r"^ERROR:\s*", "", str(exc))
        message = re.sub(r";\s*please report this issue on.*$", "", message, flags=re.DOTALL)
        return cls(message.strip())


class FFmpegNotFoundError(DownloadError):
    """Raised when ffmpeg/ffprobe are not installed."""

    def __init__(self, message: str = FFMPEG_INSTALL_HELP) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class SearchResult:
    video_id: str
    title: str
    uploader: str | None
    duration: int | None
    url: str

    @property
    def label(self) -> str:
        parts = [self.title]
        if self.uploader:
            parts.append(f"- {self.uploader}")
        if self.duration is not None:
            parts.append(f"({format_duration(self.duration)})")
        return " ".join(parts)


@dataclass(frozen=True)
class TrackMetadata:
    title: str
    artist: str | None
    duration: int | None


def _yt_dlp():
    try:
        import yt_dlp
    except ImportError as exc:
        raise DownloadError(MISSING_DEPS_HELP) from exc
    return yt_dlp


class _QuietLogger:
    """Swallow yt-dlp's console output; errors are re-raised as DownloadError."""

    def debug(self, msg: str) -> None:
        pass

    info = warning = error = debug


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def download_track(
    url: str,
    library_dir: str | os.PathLike[str] | None = None,
    *,
    force: bool = False,
) -> Path:
    """Download the audio of ``url`` into the library as a tagged 320 kbps MP3.

    Returns the path of the MP3, ready to be loaded into Mixxx. If the video
    was already downloaded (matched via the ``.source`` sidecar files) the
    existing file is returned without touching the network, unless ``force``
    is set.
    """
    library = get_library_dir(library_dir)
    library.mkdir(parents=True, exist_ok=True)

    if not force:
        cached = find_cached(url, library)
        if cached:
            return cached

    ffmpeg, ffprobe = find_ffmpeg()

    yt_dlp = _yt_dlp()

    with tempfile.TemporaryDirectory(prefix="djplusai-") as tmp:
        opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": _QuietLogger(),
            "ffmpeg_location": str(Path(ffmpeg).parent),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                info = _first_entry(info)
                if not info:
                    raise DownloadError(f"No playable video found at {url}")

                # The URL may not have been in a form we could parse offline
                # (short links, redirects, ...). Re-check with the real id.
                if not force:
                    cached = find_cached(info.get("id"), library)
                    if cached:
                        return cached

                ydl.process_ie_result(info, download=True)
        except yt_dlp.utils.DownloadError as exc:
            raise DownloadError.from_ytdlp(exc) from exc

        downloaded = _find_downloaded_file(Path(tmp))
        metadata = build_metadata(info)

        dest = _destination_path(library, metadata, info.get("id") or "")
        tmp_mp3 = Path(tmp) / "converted.mp3"
        _convert_to_mp3(downloaded, tmp_mp3, metadata, info, ffmpeg, ffprobe)
        shutil.move(str(tmp_mp3), dest)

    _write_source(dest, url, info, metadata)
    return dest


def search_tracks(query: str, limit: int = 3) -> list[SearchResult]:
    """Search YouTube for ``query`` and return the top ``limit`` results.

    Equivalent to ``yt-dlp --dump-json "ytsearch3:<query>"`` but uses flat
    extraction so only a single request is made.
    """
    query = query.strip()
    if not query:
        return []

    yt_dlp = _yt_dlp()

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "logger": _QuietLogger(),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadError.from_ytdlp(exc) from exc

    results = []
    for entry in (info or {}).get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        video_id = entry["id"]
        duration = entry.get("duration")
        results.append(
            SearchResult(
                video_id=video_id,
                title=entry.get("title") or video_id,
                uploader=entry.get("uploader") or entry.get("channel"),
                duration=int(duration) if duration is not None else None,
                url=f"https://www.youtube.com/watch?v={video_id}",
            )
        )
    return results[:limit]


# --------------------------------------------------------------------------- #
# Library / cache helpers
# --------------------------------------------------------------------------- #


def get_library_dir(library_dir: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the download folder: explicit arg > $DJPLUSAI_DOWNLOAD_DIR > default."""
    if library_dir is not None:
        return Path(library_dir).expanduser()
    env = os.environ.get(DOWNLOAD_DIR_ENV)
    if env:
        return Path(env).expanduser()
    return DEFAULT_DOWNLOAD_DIR


def extract_video_id(url: str) -> str | None:
    """Pull the 11-character video id out of a YouTube URL, if present."""
    url = url.strip()
    if _VIDEO_ID_RE.match(url):
        return url
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    path_parts = [p for p in parsed.path.split("/") if p]

    candidate = None
    if host in ("youtu.be", "www.youtu.be"):
        candidate = path_parts[0] if path_parts else None
    elif host in _YOUTUBE_HOSTS:
        if parsed.path == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [None])[0]
        elif len(path_parts) >= 2 and path_parts[0] in ("shorts", "embed", "live", "v"):
            candidate = path_parts[1]

    if candidate and _VIDEO_ID_RE.match(candidate):
        return candidate
    return None


def find_cached(url_or_id: str | None, library_dir: str | os.PathLike[str] | None = None) -> Path | None:
    """Return the MP3 previously downloaded for ``url_or_id``, if any."""
    if not url_or_id:
        return None
    video_id = extract_video_id(url_or_id)
    for mp3, source in _iter_sources(get_library_dir(library_dir)):
        if video_id and source.get("video_id") == video_id:
            return mp3
        if url_or_id in (source.get("url"), source.get("webpage_url")):
            return mp3
    return None


def read_source(mp3_path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Load the ``.source`` sidecar for a downloaded MP3."""
    source_path = Path(mp3_path).with_suffix(SOURCE_SUFFIX)
    if not source_path.is_file():
        return None
    return _load_source(source_path)


def find_ffmpeg() -> tuple[str, str]:
    """Locate ffmpeg and ffprobe, raising FFmpegNotFoundError if missing."""
    ffmpeg = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")
    if not ffmpeg or not Path(ffmpeg).exists():
        raise FFmpegNotFoundError()
    # ffprobe ships alongside ffmpeg; pydub needs it to inspect input streams.
    sibling = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)
    ffprobe = str(sibling) if sibling.exists() else shutil.which("ffprobe")
    if not ffprobe:
        raise FFmpegNotFoundError(
            f"Found ffmpeg at {ffmpeg} but not ffprobe, which pydub also needs.\n"
            + FFMPEG_INSTALL_HELP
        )
    return ffmpeg, ffprobe


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #


def build_metadata(info: dict[str, Any]) -> TrackMetadata:
    """Derive title/artist/duration from a yt-dlp info dict."""
    raw_title = (info.get("title") or info.get("id") or "Unknown").strip()
    title = info.get("track")
    artist = info.get("artist") or info.get("creator")

    if not title:
        cleaned = clean_title(raw_title)
        if not artist:
            parsed_artist, parsed_title = split_artist_title(cleaned)
            if parsed_artist:
                artist, cleaned = parsed_artist, parsed_title
        title = cleaned

    if not artist:
        uploader = info.get("uploader") or info.get("channel")
        if uploader:
            # YouTube Music auto-generated channels are named "<Artist> - Topic".
            artist = re.sub(r"\s+-\s+Topic$", "", uploader).strip() or None

    duration = info.get("duration")
    return TrackMetadata(
        title=title.strip(),
        artist=artist.strip() if artist else None,
        duration=int(round(duration)) if duration is not None else None,
    )


def clean_title(title: str) -> str:
    """Strip "(Official Video)"-style decorations from a video title."""
    cleaned = _TITLE_NOISE_RE.sub("", title)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -|")
    return cleaned or title


def split_artist_title(title: str) -> tuple[str | None, str]:
    """Split "Artist - Title" into its parts."""
    for sep in (" - ", " – ", " — ", " | "):
        if sep in title:
            artist, rest = title.split(sep, 1)
            if artist.strip() and rest.strip():
                return artist.strip(), rest.strip()
    return None, title


def format_duration(seconds: int | float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _first_entry(info: dict[str, Any] | None) -> dict[str, Any] | None:
    """Playlist-style results wrap the video in ``entries``; unwrap it."""
    while info and info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        info = entries[0] if entries else None
    return info


def _find_downloaded_file(directory: Path) -> Path:
    files = [
        p
        for p in directory.iterdir()
        if p.is_file() and not p.name.endswith((".part", ".ytdl", ".json"))
    ]
    if not files:
        raise DownloadError("yt-dlp finished but produced no audio file")
    return max(files, key=lambda p: p.stat().st_size)


def _safe_filename(name: str, max_length: int = 180) -> str:
    name = _UNSAFE_FILENAME_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:max_length].rstrip(" .") or "track"


def _destination_path(library: Path, metadata: TrackMetadata, video_id: str) -> Path:
    base = f"{metadata.artist} - {metadata.title}" if metadata.artist else metadata.title
    stem = _safe_filename(base)
    dest = library / f"{stem}.mp3"
    if dest.exists() or dest.with_suffix(SOURCE_SUFFIX).exists():
        dest = library / f"{stem} [{video_id or 'dup'}].mp3"
    counter = 2
    while dest.exists() or dest.with_suffix(SOURCE_SUFFIX).exists():
        dest = library / f"{stem} [{video_id or 'dup'}] ({counter}).mp3"
        counter += 1
    return dest


def _convert_to_mp3(
    src: Path,
    dest: Path,
    metadata: TrackMetadata,
    info: dict[str, Any],
    ffmpeg: str,
    ffprobe: str,
) -> None:
    try:
        from pydub import AudioSegment
    except ImportError as exc:
        raise DownloadError(MISSING_DEPS_HELP) from exc

    AudioSegment.converter = ffmpeg
    AudioSegment.ffmpeg = ffmpeg
    AudioSegment.ffprobe = ffprobe

    try:
        audio = AudioSegment.from_file(str(src))
    except Exception as exc:  # pydub surfaces ffmpeg failures as generic errors
        raise DownloadError(f"ffmpeg could not decode {src.name}: {exc}") from exc

    duration_ms = metadata.duration * 1000 if metadata.duration else len(audio)
    tags = {
        "title": metadata.title,
        "TLEN": str(duration_ms),
        "comment": info.get("webpage_url") or "",
    }
    if metadata.artist:
        tags["artist"] = metadata.artist
    if info.get("album"):
        tags["album"] = info["album"]
    year = info.get("release_year") or (info.get("upload_date") or "")[:4]
    if year:
        tags["date"] = str(year)

    try:
        audio.export(
            str(dest),
            format="mp3",
            bitrate=MP3_BITRATE,
            tags=tags,
            id3v2_version="3",
        ).close()
    except Exception as exc:
        raise DownloadError(f"ffmpeg could not encode MP3: {exc}") from exc


def _write_source(mp3: Path, url: str, info: dict[str, Any], metadata: TrackMetadata) -> None:
    data = {
        "url": url,
        "webpage_url": info.get("webpage_url"),
        "video_id": info.get("id"),
        "title": metadata.title,
        "artist": metadata.artist,
        "duration": metadata.duration,
        "file": mp3.name,
        "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    mp3.with_suffix(SOURCE_SUFFIX).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _load_source(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Tolerate hand-written sidecars that contain just the URL.
        return {"url": text, "video_id": extract_video_id(text)} if text else {}
    return data if isinstance(data, dict) else {}


def _iter_sources(library: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    if not library.is_dir():
        return
    for source_path in library.glob(f"*{SOURCE_SUFFIX}"):
        mp3 = source_path.with_suffix(".mp3")
        if mp3.is_file():
            yield mp3, _load_source(source_path)

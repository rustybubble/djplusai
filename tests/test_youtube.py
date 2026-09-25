from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yt_dlp

from djplusai import youtube
from djplusai.runtime import build_runtime
from djplusai.youtube import (
    DownloadError,
    FFmpegNotFoundError,
    build_metadata,
    download_track,
    extract_video_id,
    find_cached,
    read_source,
    search_tracks,
)

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
VIDEO_ID = "xpVfcZ0ZcFM"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


@pytest.mark.parametrize(
    "url",
    [
        URL,
        f"https://youtube.com/watch?v={VIDEO_ID}&list=PL123&t=42",
        f"https://youtu.be/{VIDEO_ID}?si=abc",
        f"https://m.youtube.com/watch?v={VIDEO_ID}",
        f"https://music.youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube.com/embed/{VIDEO_ID}",
        f"youtube.com/watch?v={VIDEO_ID}",
        VIDEO_ID,
    ],
)
def test_extract_video_id(url):
    assert extract_video_id(url) == VIDEO_ID


@pytest.mark.parametrize(
    "url", ["https://example.com/watch?v=xpVfcZ0ZcFM", "https://www.youtube.com/@drake", "not a url"]
)
def test_extract_video_id_rejects_other_urls(url):
    assert extract_video_id(url) is None


def test_build_metadata_parses_artist_and_cleans_title():
    meta = build_metadata(
        {"title": "Drake - God's Plan (Official Video)", "uploader": "DrakeVEVO", "duration": 198.6}
    )
    assert meta.artist == "Drake"
    assert meta.title == "God's Plan"
    assert meta.duration == 199


def test_build_metadata_prefers_music_fields():
    meta = build_metadata(
        {"title": "whatever", "track": "God's Plan", "artist": "Drake", "uploader": "x", "duration": 198}
    )
    assert (meta.artist, meta.title) == ("Drake", "God's Plan")


def test_build_metadata_falls_back_to_topic_channel():
    meta = build_metadata({"title": "Sunset Groove", "uploader": "Some Artist - Topic"})
    assert (meta.artist, meta.title, meta.duration) == ("Some Artist", "Sunset Groove", None)


def test_find_cached_matches_by_video_id(tmp_path):
    mp3 = tmp_path / "Drake - God's Plan.mp3"
    mp3.write_bytes(b"")
    mp3.with_suffix(".source").write_text(json.dumps({"url": URL, "video_id": VIDEO_ID}))

    assert find_cached(f"https://youtu.be/{VIDEO_ID}", tmp_path) == mp3
    assert find_cached("https://youtu.be/aaaaaaaaaaa", tmp_path) is None


def test_find_cached_accepts_plain_text_source(tmp_path):
    mp3 = tmp_path / "track.mp3"
    mp3.write_bytes(b"")
    mp3.with_suffix(".source").write_text(URL + "\n")
    assert find_cached(VIDEO_ID, tmp_path) == mp3


def test_find_cached_ignores_orphan_source(tmp_path):
    (tmp_path / "gone.source").write_text(json.dumps({"url": URL, "video_id": VIDEO_ID}))
    assert find_cached(URL, tmp_path) is None


def test_download_returns_cached_without_network(tmp_path, monkeypatch):
    mp3 = tmp_path / "cached.mp3"
    mp3.write_bytes(b"")
    mp3.with_suffix(".source").write_text(json.dumps({"url": URL, "video_id": VIDEO_ID}))

    def boom(*args, **kwargs):
        raise AssertionError("network should not be used")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", boom)
    assert download_track(f"https://youtu.be/{VIDEO_ID}", tmp_path) == mp3


def test_download_dir_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DJPLUSAI_DOWNLOAD_DIR", str(tmp_path / "lib"))
    assert youtube.get_library_dir() == tmp_path / "lib"
    monkeypatch.delenv("DJPLUSAI_DOWNLOAD_DIR")
    assert youtube.get_library_dir() == Path.home() / "dj-library" / "downloads"


def test_missing_ffmpeg_gives_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(FFmpegNotFoundError, match="brew install ffmpeg"):
        download_track(URL, tmp_path)


class FakeYoutubeDL:
    """Stands in for yt_dlp.YoutubeDL; 'downloads' a generated audio file."""

    info = {
        "id": VIDEO_ID,
        "title": "Test Artist - Test Song (Official Audio)",
        "uploader": "Test Artist",
        "duration": 2,
        "webpage_url": URL,
        "upload_date": "20240101",
    }
    calls = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        self.calls.append(("extract_info", url, download))
        if "private" in url:
            raise yt_dlp.utils.DownloadError("ERROR: Private video")
        return dict(self.info)

    def process_ie_result(self, info, download=True):
        self.calls.append(("process_ie_result", info["id"], download))
        out = Path(self.opts["outtmpl"].replace("%(id)s", info["id"]).replace("%(ext)s", "m4a"))
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-c:a", "aac", str(out)],
            check=True,
        )
        return info


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYoutubeDL.calls = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)
    return FakeYoutubeDL


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_download_converts_tags_and_caches(tmp_path, fake_ydl):
    path = download_track(URL, tmp_path)

    assert path == tmp_path / "Test Artist - Test Song.mp3"
    assert path.is_file()

    probe = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            check=True, capture_output=True, text=True,
        ).stdout
    )
    stream = probe["streams"][0]
    tags = {k.lower(): v for k, v in probe["format"]["tags"].items()}
    assert stream["codec_name"] == "mp3"
    assert stream["bit_rate"] == "320000"
    assert tags["title"] == "Test Song"
    assert tags["artist"] == "Test Artist"
    assert tags["date"] == "2024"

    source = read_source(path)
    assert source["url"] == URL
    assert source["video_id"] == VIDEO_ID
    assert source["duration"] == 2

    # Second request (different URL form) is served from the cache.
    fake_ydl.calls.clear()
    assert download_track(f"https://youtu.be/{VIDEO_ID}", tmp_path) == path
    assert fake_ydl.calls == []
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "Test Artist - Test Song.mp3",
        "Test Artist - Test Song.source",
    ]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_download_rechecks_cache_after_resolving_id(tmp_path, fake_ydl):
    mp3 = tmp_path / "existing.mp3"
    mp3.write_bytes(b"")
    mp3.with_suffix(".source").write_text(json.dumps({"url": "old", "video_id": VIDEO_ID}))

    # An URL we can't parse offline still resolves to the cached file.
    assert download_track("https://example.com/redirect", tmp_path) == mp3
    assert [c[0] for c in fake_ydl.calls] == ["extract_info"]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_download_force_and_name_collision(tmp_path, fake_ydl):
    first = download_track(URL, tmp_path)
    second = download_track(URL, tmp_path, force=True)
    assert first != second
    assert second.name == f"Test Artist - Test Song [{VIDEO_ID}].mp3"
    assert second.with_suffix(".source").is_file()


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_download_error_is_wrapped(tmp_path, fake_ydl):
    with pytest.raises(DownloadError, match="Private video"):
        download_track("https://www.youtube.com/private", tmp_path)


def test_search_tracks(monkeypatch):
    seen = {}

    class FakeSearch:
        def __init__(self, opts):
            seen["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, query, download=True):
            seen["query"] = query
            return {
                "_type": "playlist",
                "entries": [
                    {"id": "aaaaaaaaaaa", "title": "Song A", "uploader": "Artist A", "duration": 199.0},
                    None,
                    {"id": "bbbbbbbbbbb", "title": "Song B", "channel": "Artist B", "duration": None},
                    {"id": "ccccccccccc", "title": "Song C"},
                ],
            }

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeSearch)
    results = search_tracks("gods plan")

    assert seen["query"] == "ytsearch3:gods plan"
    assert seen["opts"]["extract_flat"] == "in_playlist"
    assert [r.video_id for r in results] == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
    assert results[0].url == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    assert results[0].label == "Song A - Artist A (3:19)"
    assert results[1].label == "Song B - Artist B"
    assert results[2].label == "Song C"


def test_search_empty_query_skips_network(monkeypatch):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", None)
    assert search_tracks("   ") == []


def test_missing_yt_dlp_gives_install_hint(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_yt_dlp(name, *args, **kwargs):
        if name == "yt_dlp":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_yt_dlp)
    with pytest.raises(DownloadError, match=r"djplusai\[youtube\]"):
        search_tracks("anything")


# ------------------------------------------------------------ chat tools


@pytest.fixture
async def rt(tmp_path):
    runtime = build_runtime(backend="sim", watch=False)
    runtime.tools.download_dir = tmp_path
    await runtime.start()
    yield runtime
    await runtime.close()


@pytest.fixture
def fake_download(monkeypatch):
    """Stub the network + ffmpeg part: write an MP3 placeholder and its .source sidecar."""
    calls = []

    def download(url, library_dir=None, *, force=False):
        cached = find_cached(url, library_dir)
        if cached:
            return cached
        calls.append(url)
        info = {"id": extract_video_id(url), "webpage_url": url, "title": "Drake - God's Plan", "duration": 199}
        meta = build_metadata(info)
        path = Path(library_dir) / "Drake - God's Plan.mp3"
        path.write_bytes(b"ID3")
        youtube._write_source(path, url, info, meta)
        return path

    monkeypatch.setattr(youtube, "download_track", download)
    return calls


def test_youtube_tools_are_registered(rt):
    for name in ("search_youtube", "add_from_youtube"):
        assert name in rt.tools.tools
        assert rt.tools.tools[name].schema["type"] == "object"


async def test_search_youtube_tool_numbers_results_and_flags_downloads(rt, tmp_path, monkeypatch):
    mp3 = tmp_path / "have.mp3"
    mp3.write_bytes(b"")
    mp3.with_suffix(".source").write_text(json.dumps({"url": "x", "video_id": "bbbbbbbbbbb"}))
    monkeypatch.setattr(
        youtube,
        "search_tracks",
        lambda q, limit: [
            youtube.SearchResult("aaaaaaaaaaa", "God's Plan", "Drake", 199, "https://www.youtube.com/watch?v=aaaaaaaaaaa"),
            youtube.SearchResult("bbbbbbbbbbb", "God's Plan (Lyrics)", None, None, "https://www.youtube.com/watch?v=bbbbbbbbbbb"),
        ][:limit],
    )
    res = await rt.tools.call("search_youtube", {"query": "gods plan"})
    assert "error" not in res, res
    assert [r["n"] for r in res["results"]] == [1, 2]
    assert res["results"][0] == {
        "n": 1, "title": "God's Plan", "channel": "Drake", "duration": "3:19",
        "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
    }
    assert res["results"][1]["already_downloaded"] == str(mp3)


async def test_add_from_youtube_loads_onto_deck(rt, fake_download):
    res = await rt.tools.call("add_from_youtube", {"url": URL, "deck": 2})
    assert "error" not in res, res
    assert res["title"] == "God's Plan" and res["artist"] == "Drake" and res["duration_s"] == 199
    assert res["in_mixxx_library"] is True and res["already_downloaded"] is False
    assert res["load"]["ok"] is True
    assert rt.dj.deck_track(2).title == "God's Plan"

    # The new track is searchable, and asking again reuses the download and library entry.
    assert rt.dj.find_tracks("gods plan")[0]["id"] == res["track"]["id"]
    again = await rt.tools.call("add_from_youtube", {"url": f"https://youtu.be/{VIDEO_ID}"})
    assert again["already_downloaded"] is True
    assert again["track"]["id"] == res["track"]["id"]
    assert fake_download == [URL]


async def test_add_from_youtube_waits_for_mixxx_scan(rt, fake_download, monkeypatch):
    monkeypatch.setattr(rt.dj.library, "add_file", lambda *a, **k: None)  # like MixxxLibrary
    res = await rt.tools.call("add_from_youtube", {"url": URL, "deck": 1})
    assert "error" not in res, res
    assert res["in_mixxx_library"] is False
    assert "Rescan" in res["next_step"] and "reload_library" in res["next_step"]
    assert "load" not in res


async def test_add_from_youtube_reports_errors(rt, monkeypatch):
    def fail(url, library_dir=None, **kw):
        raise FFmpegNotFoundError()

    monkeypatch.setattr(youtube, "download_track", fail)
    res = await rt.tools.call("add_from_youtube", {"url": URL})
    assert "ffmpeg" in res["error"]
    res = await rt.tools.call("add_from_youtube", {"url": URL, "deck": 9})
    assert "deck must be" in res["error"]

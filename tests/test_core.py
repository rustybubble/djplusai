from __future__ import annotations

import sqlite3

import pytest

from djplusai import music, protocol
from djplusai.library import MixxxLibrary, Track
from djplusai.lyrics import LyricsProvider, find_phrase, parse_lrc

# ------------------------------------------------------------------ protocol


def test_protocol_roundtrip_is_ascii():
    frame = protocol.encode({"id": 1, "title": "Beyoncé"})
    assert frame[0] == 0xF0 and frame[-1] == 0xF7
    assert all(b < 0x80 for b in frame[1:-1])
    echoed = [*frame[:4], protocol.FROM_MIXXX, *frame[5:]]
    assert protocol.decode(echoed) == {"id": 1, "title": "Beyoncé"}
    assert protocol.decode(frame) is None  # our own direction is ignored
    assert protocol.decode([0xF0, 0x41, 0x10, 0xF7]) is None


def test_protocol_splits_large_batches():
    ops = [{"op": "set", "g": "[Channel1]", "k": f"hotcue_{i}_enabled", "v": 0} for i in range(60)]
    chunks = protocol.split_ops(ops, {"id": 0, "op": "batch"})
    assert len(chunks) > 1 and sum(len(c) for c in chunks) == 60
    for c in chunks:
        assert len(protocol.encode({"id": 99999, "op": "batch", "ops": c})) <= protocol.MAX_FRAME_TO_MIXXX


# --------------------------------------------------------------------- music


@pytest.mark.parametrize(
    "text,expected",
    [("Am", "8A"), ("A minor", "8A"), ("C", "8B"), ("F#m", "11A"), ("Bbm", "3A"), ("8a", "8A"), ("1m", "8A"), ("", None)],
)
def test_camelot(text, expected):
    assert music.to_camelot(text) == expected


def test_camelot_from_mixxx_key_id():
    assert music.to_camelot_from_id(1) == "8B"  # C major
    assert music.to_camelot_from_id(22) == "8A"  # A minor
    assert music.to_camelot_from_id(0) is None


def test_key_compatibility_and_tempo():
    assert music.key_compatibility("8A", "8A") == 1.0
    assert music.key_compatibility("8A", "9A") == 0.9
    assert music.key_compatibility("8A", "8B") == 0.9
    assert music.key_compatibility("8A", "2B") < 0.5
    half = music.tempo_match(140.0, 70.0)
    assert half.multiplier == 2.0 and abs(half.percent) < 0.01
    assert not music.tempo_match(124.0, 174.0).blendable


def test_recommend_transition_by_genre_and_tempo():
    assert music.recommend_transition(124, 125, "8A", "9A", "house")["style"] == "bass_swap"
    rec = music.recommend_transition(96, 128, "", "", "reggaeton")
    assert rec["style"] in ("echo_out", "cut") and rec["sync"] is False


def test_snap_to_grid():
    assert music.snap_to_grid(10.26, anchor=0.1, period=0.5) == pytest.approx(10.1)
    assert music.snap_to_grid(10.26, anchor=0.1, period=0.5, mode="ceil") == pytest.approx(10.6)
    assert music.snap_to_grid(10.26, anchor=0.1, period=0.5, mode="nearest") == pytest.approx(10.1)


# -------------------------------------------------------------------- lyrics

LRC = """[ar:Someone]
[offset:+500]
[00:10.00]Intro line
[00:20.00][01:20.00]Rollie on my wrist tonight
[00:26.00]Something else entirely
[00:40.00]<00:40.00>Tick <00:40.50>tock <00:41.00>rollie <00:41.60>tick
"""


def test_parse_lrc_handles_offsets_repeats_and_word_tags():
    lyr = parse_lrc(LRC)
    assert [round(ln.time, 2) for ln in lyr.lines] == [9.5, 19.5, 25.5, 39.5, 79.5]
    assert lyr.lines[3].words[2] == (40.5, "rollie")


def test_find_phrase_interpolates_and_uses_word_tags():
    hits = find_phrase(parse_lrc(LRC), "rollie")
    assert [h.method for h in hits] == ["interpolated", "word-tags", "interpolated"]
    assert hits[0].time == pytest.approx(19.5)  # first word of its line
    assert hits[1].time == pytest.approx(40.5)
    assert [h.occurrence for h in hits] == [1, 2, 3]
    # Spelling variants still match; "after" filters by time but keeps numbering.
    later = find_phrase(parse_lrc(LRC), "rollies", after=30)
    assert [h.occurrence for h in later] == [2, 3]


def test_word_position_inside_line():
    lyr = parse_lrc("[00:10.00]one two three rollie\n[00:14.00]next\n")
    [hit] = find_phrase(lyr, "rollie")
    assert 10.0 + 4 * 0.85 * 0.6 < hit.time < 14.0


async def test_sidecar_lrc_is_preferred(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"")
    (tmp_path / "song.lrc").write_text("[00:05.00]hello rollie\n")
    provider = LyricsProvider(use_lrclib=False)
    lyr = await provider.get(Track(1, "A", "B", location=str(audio)))
    assert lyr is not None and lyr.lines[0].text == "hello rollie"


def test_lrclib_search_prefers_matching_duration():
    t = Track(1, "Drake", "Life Is Good", duration=237.0)
    results = [
        {"artistName": "Drake", "trackName": "Life Is Good", "duration": 260, "syncedLyrics": "[00:01.00]a"},
        {"artistName": "Drake feat. Future", "trackName": "Life Is Good", "duration": 237, "syncedLyrics": "[00:01.00]b"},
        {"artistName": "Other", "trackName": "Else", "duration": 237, "syncedLyrics": "[00:01.00]c"},
    ]
    assert LyricsProvider._best_search_result(t, results)["syncedLyrics"].endswith("b")


# ------------------------------------------------------------------- library


@pytest.fixture
def mixxx_db(tmp_path):
    path = tmp_path / "mixxxdb.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE track_locations (id INTEGER PRIMARY KEY, location TEXT, filename TEXT, directory TEXT,
            filesize INTEGER, fs_deleted INTEGER, needs_verification INTEGER);
        CREATE TABLE library (id INTEGER PRIMARY KEY, artist TEXT, title TEXT, album TEXT, genre TEXT,
            location INTEGER, duration FLOAT, samplerate INTEGER, bpm FLOAT, key TEXT, key_id INTEGER,
            mixxx_deleted INTEGER);
        INSERT INTO track_locations VALUES (1, '/m/life.mp3', 'life.mp3', '/m', 1, 0, 0);
        INSERT INTO track_locations VALUES (2, '/m/rollie.mp3', 'rollie.mp3', '/m', 1, 0, 0);
        INSERT INTO track_locations VALUES (3, '/m/gone.mp3', 'gone.mp3', '/m', 1, 1, 0);
        INSERT INTO track_locations VALUES (4, '/m/house.flac', 'house.flac', '/m', 1, 0, 0);
        INSERT INTO library VALUES (1, 'Drake & Future', 'Life Is Good', 'Life Is Good', 'Hip-Hop', 1, 237.5, 44100, 142.0, '', 22, 0);
        INSERT INTO library VALUES (2, 'Ayo & Teo', 'Rolex', 'Rolex', 'Hip-Hop', 2, 238.0, 44100, 145.0, 'C#m', 0, 0);
        INSERT INTO library VALUES (3, 'Deleted', 'Gone', '', '', 3, 100, 44100, 120.0, '', 0, 0);
        INSERT INTO library VALUES (4, 'Some DJ', 'Deep House Groove', '', 'Deep House', 4, 400, 48000, 123.0, '', 0, 0);
        """
    )
    conn.commit()
    conn.close()
    return path


def test_mixxx_library_search(mixxx_db):
    lib = MixxxLibrary(mixxx_db)
    assert {t.id for t in lib.all()} == {1, 2, 4}  # deleted files are skipped
    top = lib.search("pull up drake and future's life is good")[0][0]
    assert top.title == "Life Is Good" and top.key == "8A" and top.location == "/m/life.mp3"
    assert lib.search("rolex ayo teo")[0][0].id == 2
    assert lib.get(2).key == "12A"
    assert [t.id for t in lib.filter(genre="house")] == [4]

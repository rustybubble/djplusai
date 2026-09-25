from __future__ import annotations

import math
import struct
import wave

import pytest

from djplusai import recommend as R
from djplusai.analysis import TrackAnalysis, analyse_samples, decode
from djplusai.library import Track
from djplusai.lyrics import instrumental_windows, parse_lrc, parse_plain, vocal_spans
from djplusai.runtime import build_runtime


def prof(tid, title, artist="Someone", genre="Hip-Hop", bpm=140.0, key="5A", duration=200.0, lrc=None, analysis=None):
    t = Track(tid, artist, title, genre=genre, bpm=bpm, key=key, duration=duration)
    return R.build_profile(t, parse_lrc(lrc) if lrc else None, analysis)


def ctx(out, inn, pos=0.0, playing=True, loaded=False):
    return R.Context(out, inn, 1, 2, pos, playing, out.track.bpm, 1.0, 0.0, loaded)


def techniques(ideas):
    return [i.technique for i in ideas]


OUT_LRC = """[00:20.00]Started from the corner with a dream
[00:30.00]Now I got money on the line tonight
[00:40.00]Riding through the midnight train again
[00:50.00]Shout out Luna Verde on the radio
"""


def test_title_drop_loops_the_title_then_cuts_into_its_hook():
    out = prof(1, "Night Drive", lrc=OUT_LRC)
    inn = prof(2, "Money On The Line (feat. Somebody)", lrc="[00:08.00]Intro talk\n[00:16.00]Money on the line, money on the line\n")
    [idea] = R.title_drop(ctx(out, inn, pos=10))
    assert idea.technique == "title_drop" and idea.score > 0.9
    assert 30 < idea.at_s < 38 and idea.in_s == pytest.approx(idea.at_s - 10)
    assert idea.incoming_start_s == pytest.approx(16.0)
    tools = [s.get("tool") or "wait" for s in idea.plan]
    assert tools == ["load_track", "loop", "wait", "wait", "transition"]
    assert idea.plan[1]["args"]["phrase"] == "money on the line"
    assert idea.plan[-1]["args"]["style"] == "cut" and idea.plan[-1]["args"]["start_at_s"] == 16.0
    # Once the line has passed there is no opportunity left.
    assert R.title_drop(ctx(out, inn, pos=36)) == []


def test_single_common_word_titles_are_not_title_drops():
    out = prof(1, "A", lrc="[00:10.00]I love you baby\n")
    assert R.title_drop(ctx(out, prof(2, "Baby"))) == []
    assert R.title_drop(ctx(out, prof(3, "You"))) == []


def test_word_handoff_finds_shared_phrases():
    out = prof(1, "Night Drive", lrc=OUT_LRC)
    inn = prof(2, "Rails", lrc="[00:05.00]Hear the whistle blow\n[00:12.00]On the midnight train to you\n[00:20.00]midnight train\n")
    ideas = R.word_handoff(ctx(out, inn, pos=5))
    assert ideas and ideas[0].evidence["phrase"] == "midnight train"
    assert 12.0 < ideas[0].incoming_start_s < 16.0  # inside "On the midnight train to you", just before the phrase
    assert "midnight train" in ideas[0].why


def test_shared_phrases_ignore_filler():
    a = parse_lrc("[00:01.00]yeah yeah oh baby I know\n")
    b = parse_lrc("[00:01.00]oh yeah baby you know\n")
    assert R.shared_phrases(a, b) == []


def test_name_drop():
    out = prof(1, "Night Drive", lrc=OUT_LRC)
    inn = prof(2, "Calor", artist="Luna Verde & Friend", genre="Reggaeton", bpm=96, key="4A")
    [idea] = R.name_drop(ctx(out, inn, pos=5))
    assert idea.evidence["name"] == "Luna Verde"
    assert idea.plan[-1]["args"]["style"] == "echo_out"  # 140 vs 96 BPM can't be beatmatched


def test_remix_flip_switches_on_a_shared_line():
    lrc = "[00:10.00]Siente el calor\n[00:30.00]Baila conmigo\n"
    out = prof(1, "Calor", genre="Reggaeton", bpm=96, key="4A", lrc=lrc)
    inn = prof(2, "Calor (Club Remix)", genre="Reggaeton", bpm=98, key="4A", lrc="[00:44.00]Siente el calor\n[01:10.00]Baila conmigo\n")
    [idea] = R.remix_flip(ctx(out, inn, pos=15))
    assert idea.at_s == pytest.approx(30.0) and idea.incoming_start_s == pytest.approx(70.0)
    assert R.remix_flip(ctx(out, prof(3, "Calor", genre="Reggaeton", bpm=96))) == []  # same version


def test_harmonic_blend_and_vocal_clash_warning():
    house_a = "[00:30.00]Feel the sunrise\n[03:00.00]Hold on\n"
    out = prof(1, "Warehouse", genre="House", bpm=124, key="8A", duration=360, lrc=house_a)
    clean = prof(2, "Deeper", genre="House", bpm=125, key="9A", duration=330, lrc="[01:00.00]Take me deeper\n")
    [idea] = R.harmonic_blend(ctx(out, clean, pos=200))
    assert idea.plan[-1]["args"]["style"] == "bass_swap" and not idea.warnings
    assert idea.at_s >= 200
    early = prof(3, "Vocal", genre="House", bpm=125, key="8A", duration=330, lrc="[00:02.00]Singing straight away\n")
    [idea] = R.harmonic_blend(ctx(out, early, pos=200))
    assert idea.warnings
    assert R.harmonic_blend(ctx(out, prof(4, "Clash", genre="House", bpm=124, key="2B"))) == []


def test_energy_boost_needs_a_lift_and_a_key_move():
    out = prof(1, "A", genre="House", bpm=122, key="8A")
    up = prof(2, "B", genre="Techno", bpm=130, key="10A")
    [idea] = R.energy_boost(ctx(out, up))
    assert "+2" in idea.evidence["key_move"]
    assert R.energy_boost(ctx(up, out)) == []  # 10A -> 8A with less energy is a drop, not a boost


def test_halftime_tempo_ride_and_exits():
    hiphop = prof(1, "A", genre="Hip-Hop", bpm=87, key="5A")
    dnb = prof(2, "B", genre="Drum & Bass", bpm=174, key="5A")
    assert techniques(R.halftime_bridge(ctx(hiphop, dnb)))[0] == "halftime_bridge"
    ride = R.tempo_ride(ctx(prof(3, "C", genre="House", bpm=120), prof(4, "D", genre="House", bpm=130)))
    assert ride and ride[0].evidence["ride_to_bpm"] == pytest.approx(125.0)
    bpm_steps = [s["args"]["bpm"] for s in ride[0].plan if s.get("tool") == "set_tempo"]
    assert bpm_steps == sorted(bpm_steps) and bpm_steps[-1] == pytest.approx(125.0)
    exits = techniques(R.exits(ctx(prof(5, "E", genre="Hip-Hop", bpm=96), prof(6, "F", genre="House", bpm=128))))
    assert exits == ["echo_out", "spinback", "brake"]
    assert R.exits(ctx(prof(7, "G", genre="House", bpm=124), prof(8, "H", genre="House", bpm=125))) == []


def test_vocal_ride_over_instrumental_intro():
    out = prof(1, "A", genre="House", bpm=124, key="8A",
               lrc="[00:10.00]First line\n[01:00.00]Ride this vocal over the beat\n[01:06.00]Keep on riding\n"
                   "[01:12.00]All night long\n[01:18.00]And again\n[01:40.00]Outro\n")
    inn = prof(2, "B", genre="House", bpm=124, key="8A", lrc="[01:05.00]Late vocal\n")
    [idea] = R.vocal_ride(ctx(out, inn, pos=20))
    assert idea.evidence["vocal_line"] == "Ride this vocal over the beat"
    assert any(s.get("tool") == "set_eq" and s["args"].get("low") == 0 for s in idea.plan)


def test_double_drop_needs_analysis():
    a = TrackAnalysis(4 * 60 / 128, 0.0, [1.0], 0.8, 15.0, 300.0, drops=[60.0, 180.0])
    b = TrackAnalysis(4 * 60 / 128, 0.0, [1.0], 0.8, 15.0, 300.0, drops=[45.0])
    out = prof(1, "A", genre="Techno", bpm=128, key="8A", analysis=a)
    inn = prof(2, "B", genre="Techno", bpm=128, key="8A", analysis=b)
    [idea] = R.double_drop(ctx(out, inn, pos=100))
    assert idea.evidence["outgoing_drop_s"] == 180.0
    assert idea.at_s == pytest.approx(180 - 16 * 4 * 60 / 128)


def test_loop_and_drop_prefers_the_hook():
    lrc = "[00:10.00]Verse line here\n[00:20.00]Shine bright tonight\n[00:30.00]Other words\n[00:40.00]Shine bright tonight\n"
    [idea] = R.loop_and_drop(ctx(prof(1, "A", lrc=lrc), prof(2, "B"), pos=5))
    assert idea.evidence["line"] == "Shine bright tonight" and idea.evidence["word"] == "tonight"


def test_rank_penalises_moments_that_are_too_close():
    out = prof(1, "Night Drive", lrc=OUT_LRC)
    inn = prof(2, "Money On The Line")
    far = R.rank(R.title_drop(ctx(out, inn, pos=10)))[0]
    near = R.rank(R.title_drop(ctx(out, inn, pos=27)))[0]
    assert near.score < far.score and near.warnings


def test_vocal_windows_and_plain_lyrics():
    lyr = parse_lrc("[00:20.00]one two three\n[00:24.00]four five\n[01:00.00]late line\n")
    spans = vocal_spans(lyr)
    assert spans[0][0] == 20.0 and spans[1][0] == 60.0
    windows = instrumental_windows(lyr, 120.0, min_len=8)
    assert windows[0] == (0.0, 20.0) and windows[-1][1] == 120.0
    plain = parse_plain("line one\n\nline two\n")
    assert not plain.synced and [ln.text for ln in plain.lines] == ["line one", "line two"]
    assert vocal_spans(plain) == [] and instrumental_windows(plain, 100) == []


# ---------------------------------------------------------------- audio analysis


def test_analysis_finds_intro_drop_breakdown_and_outro(tmp_path):
    np = pytest.importorskip("numpy")
    sr, bpm = 8000, 120.0  # 2-second bars
    levels = [0.05] * 4 + [0.8] * 8 + [0.2] * 4 + [0.9] * 8 + [0.05] * 4
    t = np.arange(int(2 * sr)) / sr
    x = np.concatenate([lvl * np.sin(2 * math.pi * 110 * t) for lvl in levels]).astype(np.float32)
    path = tmp_path / "song.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", int(v * 32000)) for v in x))
    res = analyse_samples(decode(str(path)), bpm)
    assert res.intro_end == pytest.approx(8.0, abs=0.1)
    assert res.outro_start == pytest.approx(48.0, abs=0.1)
    assert any(abs(d - 8.0) < 0.1 for d in res.drops) and any(abs(d - 32.0) < 0.1 for d in res.drops)
    assert any(abs(b - 24.0) < 0.1 for b in res.breakdowns)


# ---------------------------------------------------------------- live, end to end


@pytest.fixture
async def rt():
    runtime = build_runtime(backend="sim", time_scale=25.0, watch=False)
    await runtime.start()
    yield runtime
    await runtime.close()


async def test_live_opportunity_then_run_it(rt):
    t = rt.tools
    await t.call("load_track", {"deck": 1, "query": "Gold Watch"})
    await t.call("transport", {"deck": 1, "action": "seek", "position_s": 18})
    await t.call("transport", {"deck": 1, "action": "play"})
    opp = await t.call("get_opportunities", {"horizon_s": 60})
    best = opp["opportunities"][0]
    assert best["technique"] == "title_drop" and best["incoming"]["title"] == "Money On The Line"
    assert 5 < best["in_s"] < 20
    job = await t.call("run_idea", {"idea_id": best["id"]})
    done = await t.jobs.wait(job["job_id"], timeout=30)
    assert done.status == "done", done.info()
    st = await t.call("get_status", {})
    assert not st["decks"][0]["playing"]
    assert st["decks"][1]["playing"] and st["decks"][1]["track"]["title"] == "Money On The Line"
    # Started on its hook (sync may nudge it by up to half a beat to line up the phase).
    assert st["decks"][1]["position_s"] >= 11.5


async def test_recommend_next_moves_and_pair(rt):
    t = rt.tools
    await t.call("load_track", {"deck": 1, "query": "Gold Watch", "play": True})
    res = await t.call("recommend_transitions", {"from_deck": 1, "limit": 8})
    names = {(i["technique"], i["incoming"]["title"]) for i in res["ideas"]}
    assert ("title_drop", "Money On The Line") in names
    assert all(i["plan"] and i["id"].startswith("idea-") for i in res["ideas"])
    pair = await t.call("recommend_transitions", {"from_deck": 1, "track_query": "Warehouse Sunrise"})
    assert {i["technique"] for i in pair["ideas"]} & {"echo_out", "spinback", "brake"}
    missing = await t.call("recommend_transitions", {"from_deck": 1, "track_query": "qqqq zzzz"})
    assert "where_to_get_it" in missing


async def test_watcher_announces_new_opportunities(rt):
    t = rt.tools
    heard = []
    t.watcher.listeners.append(heard.append)
    await t.call("load_track", {"deck": 1, "query": "Gold Watch"})
    await t.call("transport", {"deck": 1, "action": "seek", "position_s": 15})
    await t.call("transport", {"deck": 1, "action": "play"})
    await t.watcher.scan()
    await t.watcher.scan()  # announced once, not on every scan
    assert [i.incoming.title for i in heard].count("Money On The Line") == 1
    st = await t.call("get_status", {})
    assert st["opportunities"][0]["name"] == "Title drop"


async def test_get_lyrics_and_spinback_brake(rt):
    t = rt.tools
    lyr = await t.call("get_lyrics", {"track_query": "Gold Watch"})
    assert lyr["synced"] and lyr["lines"][0]["time_s"] == 10.0
    assert lyr["instrumental_windows_s"][0][0] == 0.0
    for style in ("spinback", "brake"):
        await t.call("load_track", {"deck": 1, "query": "Gold Watch", "play": True})
        await t.call("load_track", {"deck": 2, "query": "Warehouse Sunrise"})
        res = await t.call("transition", {"from_deck": 1, "to_deck": 2, "style": style, "background": False})
        assert res["style"] == style
        st = await t.call("get_status", {})
        assert not st["decks"][0]["playing"] and st["decks"][1]["playing"]
        await t.call("transport", {"deck": 2, "action": "pause"})


async def test_next_phrase_wait(rt):
    t = rt.tools
    await t.call("load_track", {"deck": 1, "query": "Warehouse Sunrise"})  # 124 BPM, grid from 0.1 s
    await t.call("transport", {"deck": 1, "action": "seek", "position_s": 20})
    await t.call("transport", {"deck": 1, "action": "play"})
    await rt.backend.sleep(0.1)
    await rt.dj.wait_for_next_phrase(1)
    pos = rt.backend.state.deck(1).position(rt.backend.now())
    phrase = 32 * 60 / 124
    assert pos == pytest.approx(0.1 + phrase * math.ceil((20 - 0.1) / phrase), abs=0.15)

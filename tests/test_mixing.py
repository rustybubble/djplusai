"""End-to-end behaviour against the simulated Mixxx (fast-forwarded)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from djplusai.backends.sim import SimBackend
from djplusai.runtime import build_runtime

SPEED = 25.0


@pytest.fixture
async def rt():
    runtime = build_runtime(backend="sim", time_scale=SPEED)
    await runtime.start()
    yield runtime
    await runtime.close()


async def call(rt, name, **args):
    res = await rt.tools.call(name, args)
    assert "error" not in res, res
    return res


async def test_loop_armed_ahead_engages_only_when_reached():
    be = SimBackend(time_scale=1.0)
    from djplusai.library import Track

    await be.load_track(1, Track(1, "a", "b", bpm=120.0, duration=60.0))
    g = "[Channel1]"
    await be.set(g, "playposition", 10 / 60)
    start, end = 20 * 44100 * 2, 22 * 44100 * 2
    await be.set_loop(g, start, end, enable=True)
    assert be.decks[1].pos == pytest.approx(10.0)  # no jump to the loop
    await be.set(g, "play", 1)
    for _ in range(1300):  # 13 s in 10 ms steps
        be.advance(0.01)
    assert 20.0 <= be.decks[1].pos <= 22.0 and be.decks[1].loop_wraps >= 1


async def test_rejects_loop_end_before_start():
    be = SimBackend()
    from djplusai.library import Track

    await be.load_track(1, Track(1, "a", "b", bpm=120.0, duration=60.0))
    await be.set("[Channel1]", "loop_start_position", 1000)
    assert await be.set("[Channel1]", "loop_end_position", 500) == -1


async def test_lyric_loop_then_transition_plan(rt):
    await call(rt, "load_track", deck=1, query="Gold Watch")
    await call(rt, "load_track", deck=2, query="Tick Tock Rollie")
    hits = await call(rt, "find_lyric", deck=1, phrase="rollie")
    assert len(hits["occurrences"]) == 2
    await call(rt, "transport", deck=1, action="seek", position_s=25.0)
    await call(rt, "transport", deck=1, action="play")
    plan = await call(
        rt,
        "run_mix_plan",
        steps=[
            {"tool": "loop", "args": {"deck": 1, "action": "at_lyric", "phrase": "rollie", "beats": 2}},
            {"wait": {"deck": 1, "loop_active": True}},
            {"wait": {"deck": 1, "beats": 4}},
            {"tool": "transition", "args": {"from_deck": 1, "to_deck": 2, "style": "bass_swap", "bars": 2}},
        ],
    )
    job = await rt.tools.jobs.wait(plan["job_id"], timeout=30)
    assert job.status == "done", job.info()
    loop_info = job.result[0]
    word = loop_info["lyric"]["time_s"]
    # The loop starts on a beat at/just before the word and contains it.
    assert loop_info["loop_start_s"] <= word < loop_info["loop_end_s"]
    period = 60.0 / 142.0
    assert ((loop_info["loop_start_s"] - 0.40) / period) == pytest.approx(
        round((loop_info["loop_start_s"] - 0.40) / period), abs=1e-3
    )
    st = await call(rt, "get_status")
    d1, d2 = st["decks"][0], st["decks"][1]
    assert not d1["playing"] and d1["loop"] is None and d1["eq_low_mid_high"] == [1.0, 1.0, 1.0]
    assert d2["playing"] and d2["volume"] == pytest.approx(1.0, abs=0.02)
    assert d2["eq_low_mid_high"][0] == pytest.approx(1.0, abs=0.02)
    assert d2["bpm"] == pytest.approx(142.0, abs=0.1)  # beatmatched to the outgoing track


@pytest.mark.parametrize("style", ["crossfade", "filter_sweep", "echo_out", "cut"])
async def test_transition_styles_finish_cleanly(rt, style):
    await call(rt, "load_track", deck=1, query="Warehouse Sunrise", play=True)
    await call(rt, "load_track", deck=2, query="Deeper Room")
    res = await call(rt, "transition", from_deck=1, to_deck=2, style=style, bars=1, background=False)
    assert res["style"] == style
    await rt.backend.sleep(0.5)
    st = await call(rt, "get_status")
    assert not st["decks"][0]["playing"] and st["decks"][1]["playing"]
    assert st["decks"][1]["volume"] == pytest.approx(1.0, abs=0.02)
    assert st["decks"][1]["filter"] == pytest.approx(0.5, abs=0.02)


async def test_volume_eq_filter_tempo_tools(rt):
    await call(rt, "load_track", deck=1, query="Calor", play=True)
    await call(rt, "set_volume", target=1, level=0.5, fade_s=0)
    res = await call(rt, "set_volume", target=1, change=0.1, fade_s=0)
    assert res["volume"] == pytest.approx(0.6)
    await call(rt, "set_volume", target="master", change=0.2, fade_s=0)
    assert rt.backend.master["gain"] == pytest.approx(1.2)
    await call(rt, "set_eq", deck=1, low=0.0)
    await call(rt, "set_filter", deck=1, amount=-0.5)
    assert rt.backend.decks[1].eq[0] == 0.0 and rt.backend.decks[1].filter == pytest.approx(0.25)
    res = await call(rt, "set_tempo", deck=1, percent=5)
    assert res["bpm"] == pytest.approx(100.8)


async def test_errors_are_reported_not_raised(rt):
    res = await rt.tools.call("load_track", {"deck": 1, "query": "zzzz nonexistent qqqq"})
    assert "nothing in the Mixxx library" in res["error"]
    res = await rt.tools.call("run_mix_plan", {"steps": [{"wait": {"lyric": "x"}}]})
    assert "needs a deck" in res["error"]
    await call(rt, "load_track", deck=1, query="Amen Rider", play=True)
    res = await rt.tools.call("load_track", {"deck": 1, "query": "Calor"})
    assert "playing" in res["message"]
    res = await rt.tools.call("find_lyric", {"deck": 1, "phrase": "hello"})
    assert "no time-synced lyrics" in res["error"]


async def test_suggest_next_prefers_compatible_tracks(rt):
    await call(rt, "load_track", deck=1, query="Warehouse Sunrise")
    res = await call(rt, "suggest_next_tracks", deck=1, limit=3)
    assert res["suggestions"][0]["title"] in ("Deeper Room", "Robot Heart")


async def test_cancel_job(rt):
    await call(rt, "load_track", deck=1, query="Gold Watch", play=True)
    plan = await call(rt, "run_mix_plan", steps=[{"wait": {"deck": 1, "position_s": 190}}])
    assert (await call(rt, "cancel_job", job_id=plan["job_id"]))["cancelled"] == [plan["job_id"]]
    job = await rt.tools.jobs.wait(plan["job_id"], timeout=2)
    assert job.status == "cancelled"


# ------------------------------------------------------------------ agent loop


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw, default=lambda o: o.__dict__)))
        return self.responses.pop(0)


def block(**kw):
    return SimpleNamespace(**kw)


async def test_claude_agent_runs_tools_and_returns_text(rt):
    from djplusai.agent import ClaudeDJ

    responses = [
        SimpleNamespace(
            stop_reason="tool_use",
            content=[
                block(type="text", text="Loading it."),
                block(type="tool_use", id="t1", name="load_track", input={"deck": 1, "query": "Summer Radio"}),
                block(type="tool_use", id="t2", name="transport", input={"deck": 1, "action": "play"}),
            ],
        ),
        SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="Summer Radio is playing on deck 1.")]),
    ]
    fake = FakeMessages(responses)
    agent = ClaudeDJ(rt.tools, client=SimpleNamespace(beta=SimpleNamespace(messages=fake)))
    events = []
    reply = await agent.send("play summer radio", on_tool=lambda *e: events.append(e[0]))
    assert reply == "Summer Radio is playing on deck 1."
    assert events == ["load_track", "transport"]
    assert rt.backend.decks[1].playing
    second = fake.requests[1]
    assert second["model"] == "claude-opus-5" and second["fallbacks"] == "default"
    results = second["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]  # one user message with all results
    assert all(r["type"] == "tool_result" for r in results)


async def test_claude_agent_recovers_from_refusal(rt):
    from djplusai.agent import ClaudeDJ

    fake = FakeMessages([SimpleNamespace(stop_reason="refusal", content=[])])
    agent = ClaudeDJ(rt.tools, client=SimpleNamespace(beta=SimpleNamespace(messages=fake)))
    await agent.send("something")
    assert agent.messages == []


# ------------------------------------------------------------------------ MCP


async def test_mcp_server_lists_and_calls_tools(rt):
    mcp = pytest.importorskip("mcp")
    from djplusai.mcp_server import build_server

    server = build_server(rt)
    async with mcp.Client(server) as client:
        listed = await client.list_tools()
        names = {t.name for t in listed.tools}
        assert {"load_track", "loop", "run_mix_plan", "transition"} <= names
        res = await client.call_tool("search_library", {"query": "calor"})
        payload = json.loads(res.content[0].text)
        assert payload["tracks"][0]["title"] == "Calor"
        bad = await client.call_tool("load_track", {"deck": 1, "query": "qqqq zzzz"})
        assert bad.is_error

"""Run the real Mixxx controller script under Node against a fake engine."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from djplusai import protocol

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src/djplusai/mixxx_mapping/DJPlusAI.js"
HARNESS = Path(__file__).with_name("js_harness.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


class Script:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            ["node", str(HARNESS), str(SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.boot = self._read_until_done(initial=True)

    def _read_until_done(self, initial: bool = False) -> list[dict]:
        msgs = []
        while True:
            line = self.proc.stdout.readline()
            if not line:
                break
            obj = json.loads(line)
            if obj.get("done"):
                break
            if "out" in obj:
                decoded = protocol.decode(obj["out"])
                assert decoded is not None, obj
                msgs.append(decoded)
            else:
                msgs.append(obj)
            if initial:
                break
        return msgs

    def send(self, cmd: dict) -> list[dict]:
        self.proc.stdin.write(json.dumps(cmd) + "\n")
        self.proc.stdin.flush()
        return self._read_until_done()

    def request(self, msg: dict) -> list[dict]:
        return self.send({"frame": protocol.encode(msg)})

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=5)


@pytest.fixture
def script():
    s = Script()
    yield s
    s.close()


def test_hello_on_init(script):
    assert script.boot[0]["t"] == "hello"


def test_set_get_and_reply(script):
    [reply] = script.request({"id": 7, "op": "set", "g": "[Channel1]", "k": "volume", "v": 0.25})
    assert reply == {"t": "reply", "id": 7, "ok": True, "v": 0.25}
    [reply] = script.request({"id": 8, "op": "get", "g": "[Channel1]", "k": "volume"})
    assert reply["v"] == 0.25


def test_batch_and_unknown_op(script):
    [reply] = script.request(
        {"id": 1, "op": "batch", "ops": [
            {"op": "set", "g": "[Master]", "k": "gain", "v": 1.2},
            {"op": "get", "g": "[Master]", "k": "gain"},
        ]}
    )
    assert reply["v"] == [1.2, 1.2]
    [reply] = script.request({"id": 2, "op": "explode"})
    assert reply["ok"] is False and "unknown op" in reply["err"]


def test_ignores_own_direction_and_foreign_sysex(script):
    echoed = protocol.encode({"id": 3, "op": "hello"}, direction=protocol.FROM_MIXXX)
    assert script.send({"frame": echoed}) == []
    assert script.send({"frame": [0xF0, 0x00, 0x20, 0x6B, 0x01, 0xF7]}) == []


def test_loop_is_armed_atomically(script):
    g = "[Channel1]"
    [reply] = script.request({"id": 4, "op": "loop", "g": g, "s": 1000.0, "e": 5000.0, "enable": True})
    assert reply["v"] == [1000.0, 5000.0, 1]
    # Re-arming elsewhere releases the old loop first, then re-enables.
    [reply] = script.request({"id": 5, "op": "loop", "g": g, "s": 8000.0, "e": 9000.0, "enable": True})
    assert reply["v"] == [8000.0, 9000.0, 1]


def test_ramp_steps_to_target(script):
    script.request({"id": 1, "op": "set", "g": "[Channel2]", "k": "volume", "v": 1.0})
    script.request({"id": 2, "op": "ramp", "g": "[Channel2]", "k": "volume", "to": 0.0, "ms": 100})
    script.send({"tick": 2})
    dump = script.send({"dump": True})
    mid = [m for m in dump if "controls" in m][0]["controls"]["[Channel2],volume"]
    assert 0.0 < mid < 1.0
    script.send({"tick": 5})
    dump = script.send({"dump": True})
    assert [m for m in dump if "controls" in m][0]["controls"]["[Channel2],volume"] == 0.0


def test_cancel_ramps_covers_deck_effect_groups(script):
    eq = "[EqualizerRack1_[Channel1]_Effect1]"
    script.request({"id": 1, "op": "set", "g": eq, "k": "parameter3", "v": 1.0})
    script.request({"id": 2, "op": "ramp", "g": eq, "k": "parameter3", "to": 0.0, "ms": 1000})
    script.request({"id": 3, "op": "ramp", "g": "[Channel2]", "k": "volume", "to": 0.0, "ms": 1000})
    script.request({"id": 4, "op": "cancel_ramps", "g": "[Channel1]"})
    script.send({"tick": 3})
    controls = [m for m in script.send({"dump": True}) if "controls" in m][0]["controls"]
    assert controls[eq + ",parameter3"] == 1.0
    assert controls["[Channel2],volume"] < 1.0


def test_state_push_with_non_ascii_metadata(script):
    script.send({"set": ["[Channel1]", "play", 1]})
    script.send({"set": ["[Channel1]", "duration", 200]})
    msgs = script.send({"tick": 1})
    decks = [m for m in msgs if m.get("t") == "deck"]
    assert [d["n"] for d in decks] == [1]  # idle decks are only sent on full pushes
    assert decks[0]["s"]["duration"] == 200
    assert decks[0]["s"]["meta"]["artist"] == "Björk [Channel1]"
    assert decks[0]["s"]["meta"]["title"] == "Jóga"
    msgs = script.send({"tick": 9})
    assert {m["n"] for m in msgs if m.get("t") == "deck"} == {1, 2, 3, 4}
    assert any(m.get("t") == "master" for m in msgs)

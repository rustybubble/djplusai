"""MidiBackend <-> the real DJPlusAI.js script (in Node), with the MIDI port replaced by pipes."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from djplusai.backends.midi import MidiBackend

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


class NodePort:
    """Stands in for both rtmidi ports: frames go to the script, its SysEx comes back."""

    def __init__(self, backend: MidiBackend) -> None:
        self.backend = backend
        self.proc = subprocess.Popen(
            ["node", str(Path(__file__).with_name("js_harness.js")), str(ROOT / "src/djplusai/mixxx_mapping/DJPlusAI.js")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            obj = json.loads(line)
            if "out" in obj:
                self.backend._on_midi((obj["out"], 0.0))

    def _write(self, cmd: dict) -> None:
        with self.lock:
            self.proc.stdin.write(json.dumps(cmd) + "\n")
            self.proc.stdin.flush()

    def send_message(self, frame: list[int]) -> None:
        self._write({"frame": frame})

    def tick(self, n: int = 1) -> None:
        self._write({"tick": n})

    def set(self, group: str, key: str, value: float) -> None:
        self._write({"set": [group, key, value]})

    def close_port(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=5)


@pytest.fixture
async def bridge():
    be = MidiBackend(request_timeout=3.0)
    be._loop = asyncio.get_running_loop()
    port = NodePort(be)
    be._midi_out = port
    await be.handshake()
    yield be, port
    await be.close()
    port.close_port()


async def test_handshake_and_controls(bridge):
    be, _ = bridge
    assert be.connected
    assert await be.set("[Channel1]", "volume", 0.3) == pytest.approx(0.3)
    assert await be.get("[Channel1]", "volume") == pytest.approx(0.3)
    await be.press("[Channel1]", "LoadSelectedTrack")


async def test_large_batches_are_split_across_frames(bridge):
    be, _ = bridge
    ops = [{"op": "set", "g": "[Channel1]", "k": f"hotcue_{i}_color", "v": i} for i in range(80)]
    out = await be.batch(ops)
    assert out == list(range(80))


async def test_state_push_updates_deck_state(bridge):
    be, port = bridge
    port.set("[Channel2]", "play", 1)
    port.set("[Channel2]", "duration", 180)
    port.set("[Channel2]", "playposition", 0.5)
    port.set("[Channel2]", "file_bpm", 128)
    port.set("[Channel2]", "bpm", 128)
    port.tick(1)
    assert await be.wait_until(lambda: be.state.deck(2).playing, timeout=3)
    d = be.state.deck(2)
    assert d.position() == pytest.approx(90.0)
    assert d.title == "Jóga"


async def test_loop_command_through_script(bridge):
    be, _ = bridge
    res = await be.set_loop("[Channel1]", 1000.0, 3000.0, enable=True)
    assert res == [1000.0, 3000.0, 1]

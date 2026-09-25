"""Wire format between djplusai and the DJPlusAI Mixxx controller script.

Messages travel as MIDI System Exclusive frames so they work with a stock
Mixxx build (no fork, no OSC patch):

    F0 7D 44 4A <direction> <ASCII JSON payload> F7

* ``7D`` is the MIDI "non-commercial / educational" manufacturer ID.
* ``44 4A`` is "DJ" so other SysEx traffic on the same port is ignored.
* ``direction`` is ``01`` for bridge -> Mixxx and ``02`` for Mixxx -> bridge.
  Loopback ports (loopMIDI on Windows, the IAC bus on macOS) echo every frame
  back to its sender, so each side drops frames tagged with its own direction.

JSON is encoded with ``ensure_ascii`` so every payload byte is < 0x80, which
SysEx requires. Mixxx's PortMidi input buffer is 1024 bytes, so frames sent
*to* Mixxx must stay below :data:`MAX_FRAME_TO_MIXXX`.
"""

from __future__ import annotations

import json
from typing import Any

SYSEX_START = 0xF0
SYSEX_END = 0xF7
MANUFACTURER = 0x7D
MAGIC = (0x44, 0x4A)
TO_MIXXX = 0x01
FROM_MIXXX = 0x02
HEADER_LEN = 5  # F0 7D 44 4A dir

# Mixxx drops SysEx input larger than MIXXX_SYSEX_BUFFER_LEN (1024 bytes).
MAX_FRAME_TO_MIXXX = 1000


class FrameTooLarge(ValueError):
    pass


def _payload(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=True, separators=(",", ":"))


def frame_size(message: dict[str, Any]) -> int:
    return HEADER_LEN + len(_payload(message)) + 1


def encode(message: dict[str, Any], direction: int = TO_MIXXX) -> list[int]:
    payload = _payload(message)
    frame = [SYSEX_START, MANUFACTURER, *MAGIC, direction, *payload.encode("ascii"), SYSEX_END]
    if direction == TO_MIXXX and len(frame) > MAX_FRAME_TO_MIXXX:
        raise FrameTooLarge(f"frame of {len(frame)} bytes exceeds Mixxx's SysEx buffer")
    return frame


def decode(data: bytes | list[int], expect_direction: int = FROM_MIXXX) -> dict[str, Any] | None:
    """Return the JSON message in ``data`` or ``None`` if it is not ours."""
    raw = bytes(data)
    if len(raw) < HEADER_LEN + 1:
        return None
    if raw[0] != SYSEX_START or raw[1] != MANUFACTURER or tuple(raw[2:4]) != MAGIC:
        return None
    if raw[4] != expect_direction:
        return None
    end = len(raw) - 1 if raw[-1] == SYSEX_END else len(raw)
    try:
        msg = json.loads(raw[HEADER_LEN:end].decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return msg if isinstance(msg, dict) else None


def split_ops(ops: list[dict[str, Any]], base: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Split a batch of operations into chunks whose frames fit in Mixxx's buffer."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for op in ops:
        trial = current + [op]
        if frame_size({**base, "ops": trial}) > MAX_FRAME_TO_MIXXX - 16:  # headroom for the id
            if not current:
                raise FrameTooLarge("a single operation does not fit in one SysEx frame")
            chunks.append(current)
            current = [op]
        else:
            current = trial
    if current:
        chunks.append(current)
    return chunks

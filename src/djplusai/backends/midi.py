"""Talk to a running Mixxx through the DJPlusAI controller mapping over MIDI SysEx."""

from __future__ import annotations

import asyncio
import itertools
import logging
import sys
from typing import Any

from .. import protocol
from ..library import Track
from .base import Backend, LoadResult, MixxxError, NotConnected

log = logging.getLogger(__name__)

SETUP_HINT = (
    "Mixxx is not answering. Make sure Mixxx is running, then in Preferences > Controllers "
    "select the 'DJPlusAI' device, choose the 'DJPlusAI Bridge' mapping and tick Enabled. "
    "If you use virtual ports, start djplusai before Mixxx (Mixxx only scans MIDI devices at startup). "
    "Run `djplusai doctor` for details."
)


class MidiBackend(Backend):
    name = "mixxx-midi"

    def __init__(
        self,
        port_name: str = "DJPlusAI",
        virtual: bool | None = None,
        request_timeout: float = 2.0,
        loader: Any = None,
    ) -> None:
        super().__init__()
        self.port_name = port_name
        # rtmidi cannot create virtual ports on Windows; use loopMIDI there.
        self.virtual = (sys.platform != "win32") if virtual is None else virtual
        self.request_timeout = request_timeout
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._midi_in: Any = None
        self._midi_out: Any = None
        self._hello: dict[str, Any] | None = None
        self.loader = loader  # set by the controller (needs a library)

    # --- lifecycle -----------------------------------------------------
    async def start(self) -> None:
        try:
            import rtmidi  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise MixxxError("python-rtmidi is not installed: pip install 'djplusai[midi]'") from exc
        self._loop = asyncio.get_running_loop()
        self._midi_in = rtmidi.MidiIn(name="djplusai")
        self._midi_out = rtmidi.MidiOut(name="djplusai")
        self._midi_in.ignore_types(sysex=False, timing=True, active_sense=True)
        if self.virtual:
            self._midi_in.open_virtual_port(self.port_name)
            self._midi_out.open_virtual_port(self.port_name)
        else:
            self._open_existing(self._midi_in, "input")
            self._open_existing(self._midi_out, "output")
        self._midi_in.set_callback(self._on_midi)
        try:
            await self.handshake()
        except NotConnected:
            log.warning(SETUP_HINT)

    def _open_existing(self, port: Any, kind: str) -> None:
        names = port.get_ports()
        for i, name in enumerate(names):
            if self.port_name.lower() in name.lower():
                port.open_port(i)
                return
        raise MixxxError(
            f"No MIDI {kind} port containing '{self.port_name}' (found: {names}). "
            "On Windows create one with loopMIDI; on macOS enable an IAC bus; or use virtual ports."
        )

    async def close(self) -> None:
        for port in (self._midi_in, self._midi_out):
            if port is not None:
                try:
                    port.close_port()
                except Exception:  # pragma: no cover - best effort
                    pass
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(NotConnected("bridge closed"))
        self._pending.clear()

    async def handshake(self) -> dict[str, Any]:
        self._hello = await self.request({"op": "hello"})
        await self.refresh()
        return self._hello

    @property
    def connected(self) -> bool:
        return self._hello is not None and (self.now() - self.state.last_update) < 5.0 * self.time_scale

    # --- MIDI I/O ------------------------------------------------------
    def _on_midi(self, event: tuple[list[int], float], _data: Any = None) -> None:
        message, _delta = event
        msg = protocol.decode(message)
        if msg is None or self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._dispatch, msg)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        t = msg.get("t")
        now = self.now()
        if t == "reply":
            fut = self._pending.pop(msg.get("id"), None)
            if fut and not fut.done():
                if msg.get("ok", True):
                    fut.set_result(msg.get("v"))
                else:
                    fut.set_exception(MixxxError(msg.get("err", "Mixxx reported an error")))
        elif t == "deck":
            self.state.apply_deck(int(msg["n"]), msg.get("s") or {}, now)
        elif t == "master":
            self.state.master.update(msg.get("s") or {})
            self.state.last_update = now
        elif t == "hello":
            self._hello = msg
            self.state.last_update = now
        elif t == "bye":
            self._hello = None
        elif t == "error":
            log.warning("Mixxx script error: %s", msg.get("err"))

    async def request(self, msg: dict[str, Any]) -> Any:
        if self._midi_out is None:
            raise NotConnected("MIDI ports are not open")
        if msg.get("op") == "batch":
            ops = msg["ops"]
            chunks = protocol.split_ops(ops, {"id": 0, "op": "batch"})
            if len(chunks) > 1:
                out: list[Any] = []
                for chunk in chunks:
                    out.extend(await self.request({"op": "batch", "ops": chunk}))
                return out
        assert self._loop is not None
        req_id = next(self._ids)
        fut: asyncio.Future[Any] = self._loop.create_future()
        self._pending[req_id] = fut
        self._midi_out.send_message(protocol.encode({**msg, "id": req_id}))
        try:
            return await asyncio.wait_for(fut, self.request_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            self._hello = None
            raise NotConnected(SETUP_HINT) from None

    # --- library --------------------------------------------------------
    async def load_track(self, deck: int, track: Track) -> LoadResult:
        if self.loader is None:
            return LoadResult(False, deck, track, "no track loader configured")
        return await self.loader.load(deck, track)

"""Command line: `djplusai chat | mcp | demo | call | install-mapping | doctor`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any


def _runtime_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--sim", action="store_true", help="use the built-in Mixxx simulator instead of real Mixxx")
    p.add_argument("--db", help="path to mixxxdb.sqlite (default: Mixxx's settings folder)")
    p.add_argument("--port", help="MIDI port name (default: DJPlusAI)")
    p.add_argument("--lyrics-dir", help="folder with 'Artist - Title.lrc' files")
    p.add_argument("--whisper", action="store_true", help="refine lyric timing with faster-whisper")


def _runtime_kwargs(a: argparse.Namespace) -> dict[str, Any]:
    return {
        "backend": "sim" if a.sim else None,
        "db_path": a.db,
        "port_name": a.port,
        "lyrics_dir": a.lyrics_dir,
        "use_whisper": True if a.whisper else None,
    }


async def _chat(a: argparse.Namespace) -> int:
    from .agent import ClaudeDJ, format_tool_event
    from .runtime import build_runtime

    rt = build_runtime(**_runtime_kwargs(a))
    await rt.start()
    agent = ClaudeDJ(rt.tools, model=a.model, effort=a.effort)
    print(f"DJ+AI ({rt.backend.name}, model {agent.model}). Tell me what you want to hear. Ctrl-D to quit.")
    try:
        while True:
            try:
                text = await asyncio.to_thread(input, "you> ")
            except EOFError:
                print()
                return 0
            if not text.strip():
                continue
            try:
                reply = await agent.send(text, on_tool=lambda *ev: print(format_tool_event(*ev)))
            except Exception as exc:  # keep the set going if one request fails
                print(f"  (request failed: {type(exc).__name__}: {exc})")
                continue
            print(f"dj> {reply}")
    finally:
        await rt.close()


async def _call(a: argparse.Namespace) -> int:
    from .runtime import build_runtime

    rt = build_runtime(**_runtime_kwargs(a))
    await rt.start()
    try:
        args = json.loads(a.args) if a.args else {}
        res = await rt.tools.call(a.tool, args)
        if a.wait_jobs:
            for job in list(rt.tools.jobs.jobs.values()):
                await rt.tools.jobs.wait(job.id)
            res["jobs"] = rt.tools.jobs.list()
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 1 if "error" in res else 0
    finally:
        await rt.close()


async def _demo(a: argparse.Namespace) -> int:
    """Scripted run of the README example against the simulator (no Mixxx or API key needed)."""
    from .runtime import build_runtime

    rt = build_runtime(backend="sim", time_scale=a.speed)
    await rt.start()
    t = rt.tools

    async def step(name: str, args: dict[str, Any]) -> dict[str, Any]:
        res = await t.call(name, args)
        print(f"-> {name} {json.dumps(args)}\n   {json.dumps(res)[:300]}")
        return res

    try:
        await step("load_track", {"deck": 1, "query": "Demo MC Gold Watch"})
        await step("load_track", {"deck": 2, "query": "Tick Tock Rollie"})
        await step("transport", {"deck": 1, "action": "play"})
        plan = await step(
            "run_mix_plan",
            {
                "description": "loop the first 'rollie', then echo out into Tick Tock Rollie",
                "steps": [
                    {"tool": "loop", "args": {"deck": 1, "action": "at_lyric", "phrase": "rollie", "beats": 4}},
                    {"wait": {"deck": 1, "loop_active": True}},
                    {"wait": {"deck": 1, "beats": 8}},
                    {"tool": "transition", "args": {"from_deck": 1, "to_deck": 2, "style": "echo_out", "bars": 2}},
                ],
            },
        )
        job = await t.jobs.wait(plan["job_id"], timeout=120)
        print(json.dumps(job.info(), indent=2))
        print(json.dumps(await t.call("get_status", {}), indent=2)[:2000])
        return 0 if job.status == "done" else 1
    finally:
        await rt.close()


def _doctor(a: argparse.Namespace) -> int:
    from .loader import Typist
    from .runtime import mixxx_settings_dir

    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"[{'ok' if good else '!!'}] {label}{': ' + detail if detail else ''}")

    settings = mixxx_settings_dir()
    check("Mixxx settings folder", settings.exists(), str(settings))
    db = Path(a.db) if a.db else settings / "mixxxdb.sqlite"
    check("Mixxx library database", db.exists(), str(db))
    mapping = settings / "controllers" / "DJPlusAI.midi.xml"
    check("DJPlusAI mapping installed", mapping.exists(), f"{mapping} (run `djplusai install-mapping`)")
    try:
        import rtmidi  # type: ignore

        ins, outs = rtmidi.MidiIn().get_ports(), rtmidi.MidiOut().get_ports()
        check("python-rtmidi", True, f"inputs={ins} outputs={outs}")
    except Exception as exc:
        check("python-rtmidi", False, f"{exc} (pip install 'djplusai[midi]')")
    typist = Typist().available()
    check("keyboard automation for track loading", typist is not None, typist or "install xdotool or pip install 'djplusai[keyboard]'")
    for mod, extra in (("mcp", "mcp"), ("anthropic", "agent")):
        try:
            __import__(mod)
            check(f"{mod} package", True)
        except ImportError:
            print(f"[--] {mod} package not installed (optional: pip install 'djplusai[{extra}]')")

    async def handshake() -> None:
        from .backends.midi import MidiBackend

        be = MidiBackend(port_name=a.port or "DJPlusAI")
        try:
            await be.start()
            if not be.connected:
                print("   waiting 10 s for Mixxx to open the DJPlusAI device...")
                for _ in range(20):
                    await asyncio.sleep(0.5)
                    try:
                        await be.handshake()
                        break
                    except Exception:
                        continue
            check("Mixxx answering on the DJPlusAI port", be.connected, "" if be.connected else "see README 'Setup'")
        except Exception as exc:
            check("Mixxx answering on the DJPlusAI port", False, str(exc))
        finally:
            await be.close()

    if not a.skip_midi:
        asyncio.run(handshake())
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="djplusai", description="Let AI agents DJ with Mixxx.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("chat", help="talk to a Claude-powered DJ in your terminal")
    _runtime_args(p)
    p.add_argument("--model", help="Claude model id (default: claude-opus-5 or $DJPLUSAI_MODEL)")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])

    p = sub.add_parser("mcp", help="run the MCP server on stdio (for Claude Desktop, Claude Code, Cursor...)")
    _runtime_args(p)

    p = sub.add_parser("call", help="call one tool directly, e.g. djplusai call get_status")
    _runtime_args(p)
    p.add_argument("tool")
    p.add_argument("args", nargs="?", help="JSON arguments")
    p.add_argument("--wait-jobs", action="store_true", help="wait for background jobs to finish")

    p = sub.add_parser("demo", help="run the lyric-loop + transition example in the simulator")
    p.add_argument("--speed", type=float, default=8.0, help="simulation speed multiplier")

    p = sub.add_parser("install-mapping", help="copy the DJPlusAI controller mapping into Mixxx")
    p.add_argument("--dir", help="target folder (default: <Mixxx settings>/controllers)")

    p = sub.add_parser("doctor", help="check the Mixxx connection and setup")
    p.add_argument("--db")
    p.add_argument("--port")
    p.add_argument("--skip-midi", action="store_true")

    a = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )
    if a.cmd == "chat":
        return asyncio.run(_chat(a))
    if a.cmd == "mcp":
        from .mcp_server import serve_stdio

        asyncio.run(serve_stdio(**_runtime_kwargs(a)))
        return 0
    if a.cmd == "call":
        return asyncio.run(_call(a))
    if a.cmd == "demo":
        return asyncio.run(_demo(a))
    if a.cmd == "install-mapping":
        from .runtime import install_mapping

        for path in install_mapping(Path(a.dir).expanduser() if a.dir else None):
            print(f"wrote {path}")
        print("Now restart Mixxx, open Preferences > Controllers > DJPlusAI, pick 'DJPlusAI Bridge' and enable it.")
        return 0
    if a.cmd == "doctor":
        return _doctor(a)
    return 2


if __name__ == "__main__":
    sys.exit(main())

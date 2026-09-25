"""Background jobs: timed mix plans and long transitions that outlive a single tool call.

A *mix plan* is an ordered list of steps. Each step is either a tool call::

    {"tool": "load_track", "args": {"deck": 2, "query": "Rollie Ayo & Teo"}}

or a wait::

    {"wait": {"deck": 1, "lyric": "rollie"}}          # until the phrase is sung
    {"wait": {"deck": 1, "position_s": 95.0}}         # until a track position
    {"wait": {"deck": 1, "beats": 8}}                 # for 8 beats of played music
    {"wait": {"deck": 1, "loop_active": true}}        # until the armed loop engages
    {"wait": {"deck": 1, "remaining_s": 30}}          # until 30 s before the end
    {"wait": {"deck": 1, "next_bar": true}}           # to the next bar boundary
    {"wait": {"seconds": 4}}                          # plain delay

Plans run in the background so the agent (and the user) can keep talking while
the music plays; jobs can be listed and cancelled.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .controller import DJ

ToolCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass
class Job:
    id: int
    description: str
    status: str = "running"  # running | done | failed | cancelled
    created: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    result: Any = None
    error: str | None = None
    task: asyncio.Task[Any] | None = None
    step: int = 0
    total_steps: int = 0

    def info(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "step": f"{self.step}/{self.total_steps}" if self.total_steps else None,
            "log": self.log[-8:],
            "error": self.error,
            "result": self.result if self.status == "done" else None,
        }


class JobManager:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.jobs: dict[int, Job] = {}

    def start(self, description: str, factory: Callable[[Job], Awaitable[Any]]) -> Job:
        job = Job(id=next(self._ids), description=description)

        async def runner() -> None:
            try:
                job.result = await factory(job)
                job.status = "done"
            except asyncio.CancelledError:
                job.status = "cancelled"
                raise
            except Exception as exc:  # reported to the agent via list_jobs
                job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"

        def finished(task: asyncio.Task[Any]) -> None:
            # A task cancelled before it first runs never enters runner().
            if task.cancelled():
                job.status = "cancelled"

        job.task = asyncio.create_task(runner())
        job.task.add_done_callback(finished)
        self.jobs[job.id] = job
        return job

    def list(self, include_finished: bool = True) -> list[dict[str, Any]]:
        return [
            j.info() for j in self.jobs.values() if include_finished or j.status == "running"
        ][-20:]

    def cancel(self, job_id: int | None = None) -> list[int]:
        cancelled = []
        for j in self.jobs.values():
            if (job_id is None or j.id == job_id) and j.status == "running" and j.task:
                j.task.cancel()
                cancelled.append(j.id)
        return cancelled

    async def wait(self, job_id: int, timeout: float | None = None) -> Job:
        job = self.jobs[job_id]
        if job.task:
            try:
                await asyncio.wait_for(asyncio.shield(job.task), timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        return job


WAIT_KEYS = {"seconds", "position_s", "lyric", "beats", "loop_active", "remaining_s", "next_bar", "next_beat"}


def validate_plan(steps: list[dict[str, Any]], tool_names: set[str]) -> None:
    if not isinstance(steps, list) or not steps:
        raise ValueError("a plan needs at least one step")
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise ValueError(f"step {i} must be an object")
        if "tool" in step:
            if step["tool"] not in tool_names:
                raise ValueError(f"step {i}: unknown tool '{step['tool']}'")
            if not isinstance(step.get("args", {}), dict):
                raise ValueError(f"step {i}: args must be an object")
        elif "wait" in step:
            w = step["wait"]
            if not isinstance(w, dict) or not (WAIT_KEYS & w.keys()):
                raise ValueError(f"step {i}: wait needs one of {sorted(WAIT_KEYS)}")
            if (WAIT_KEYS - {"seconds"}) & w.keys() and "deck" not in w:
                raise ValueError(f"step {i}: this wait needs a deck")
        else:
            raise ValueError(f"step {i} needs 'tool' or 'wait'")


async def run_wait(dj: DJ, w: dict[str, Any]) -> str:
    timeout = float(w.get("timeout_s", 900))
    deck = int(w["deck"]) if "deck" in w else None
    if "seconds" in w:
        await dj.backend.sleep(float(w["seconds"]))
        return f"waited {w['seconds']}s"
    assert deck is not None
    if "lyric" in w:
        hit = await dj.wait_for_lyric(
            deck, w["lyric"], w.get("occurrence"), float(w.get("lead_s", 0.0)), timeout
        )
        return f"heard '{w['lyric']}' on deck {deck} at {hit['time_s']}s"
    ok = True
    if "position_s" in w:
        ok = await dj.wait_for_position(deck, float(w["position_s"]), timeout)
    elif "beats" in w:
        ok = await dj.wait_for_beats(deck, float(w["beats"]), timeout)
    elif "loop_active" in w:
        ok = await dj.wait_for_loop(deck, bool(w["loop_active"]), timeout)
    elif "remaining_s" in w:
        ok = await dj.wait_for_remaining(deck, float(w["remaining_s"]), timeout)
    elif "next_bar" in w:
        ok = await dj.wait_for_next_beat(deck, every=4)
    elif "next_beat" in w:
        ok = await dj.wait_for_next_beat(deck, every=1)
    if not ok:
        raise TimeoutError(f"wait {w} timed out after {timeout}s")
    return f"wait {w} satisfied"


async def run_plan(dj: DJ, steps: list[dict[str, Any]], call_tool: ToolCaller, job: Job) -> list[Any]:
    job.total_steps = len(steps)
    results = []
    for i, step in enumerate(steps, 1):
        job.step = i
        if "wait" in step:
            msg = await run_wait(dj, step["wait"])
            job.log.append(f"{i}: {msg}")
            results.append(msg)
        else:
            args = dict(step.get("args") or {})
            # Inside a plan, run transitions inline so later steps follow them.
            if step["tool"] == "transition":
                args["background"] = False
            res = await call_tool(step["tool"], args)
            if isinstance(res, dict) and res.get("error"):
                raise RuntimeError(f"step {i} ({step['tool']}) failed: {res['error']}")
            job.log.append(f"{i}: {step['tool']} ok")
            results.append(res)
    return results

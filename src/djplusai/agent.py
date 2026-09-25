"""A natural-language DJ: Claude turns what you type into djplusai tool calls."""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from .guide import DJ_GUIDE
from .tools import DJTools

DEFAULT_MODEL = "claude-opus-5"
# DJ requests are latency-sensitive ("turn it up" should be instant), so the
# default effort is one notch below the API default. Override with DJPLUSAI_EFFORT.
DEFAULT_EFFORT = "medium"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

ToolEvent = Callable[[str, dict[str, Any], str, bool], None]


class ClaudeDJ:
    def __init__(
        self,
        tools: DJTools,
        model: str | None = None,
        effort: str | None = None,
        client: Any = None,
    ) -> None:
        import anthropic

        self.tools = tools
        self.model = model or os.environ.get("DJPLUSAI_MODEL", DEFAULT_MODEL)
        self.effort = effort or os.environ.get("DJPLUSAI_EFFORT", DEFAULT_EFFORT)
        self.client = client or anthropic.AsyncAnthropic()
        self.messages: list[dict[str, Any]] = []
        self.tool_defs = [
            {"name": t.name, "description": t.description, "input_schema": t.schema}
            for t in tools.tools.values()
        ]

    async def send(self, text: str, on_tool: ToolEvent | None = None) -> str:
        turn_start = len(self.messages)
        self.messages.append({"role": "user", "content": text})
        while True:
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=DJ_GUIDE,
                tools=self.tool_defs,
                messages=self.messages,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
            if response.stop_reason == "refusal":
                # Keep the history valid for the next turn: drop this whole exchange.
                del self.messages[turn_start:]
                return "Sorry, I can't help with that request."
            self.messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                reply = "".join(b.text for b in response.content if b.type == "text").strip()
                if response.stop_reason == "max_tokens":
                    reply += "\n[response was cut off]"
                return reply
            results = []
            # Run tool calls in order: DJ actions often depend on each other
            # (load, then play) and library loading drives the UI one track at a time.
            for block in response.content:
                if block.type != "tool_use":
                    continue
                args = block.input if isinstance(block.input, dict) else {}
                out, is_error = await self.tools.call_json(block.name, args)
                if on_tool:
                    on_tool(block.name, args, out, is_error)
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": out, "is_error": is_error}
                )
            self.messages.append({"role": "user", "content": results})


def format_tool_event(name: str, args: dict[str, Any], out: str, is_error: bool) -> str:
    arg_text = json.dumps(args, ensure_ascii=False)
    if len(arg_text) > 160:
        arg_text = arg_text[:157] + "..."
    mark = "!!" if is_error else "->"
    detail = ""
    if is_error:
        try:
            detail = " " + json.loads(out).get("error", "")
        except (json.JSONDecodeError, AttributeError):
            detail = ""
    return f"  {mark} {name} {arg_text}{detail}"

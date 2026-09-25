"""The DJ tool set, defined once and exposed through MCP and the Claude chat agent."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import music, youtube
from .backends.base import MixxxError, deck_group
from .controller import DJ, TrackNotFound
from .library import Track
from .jobs import JobManager, run_plan, validate_plan
from .lyrics import instrumental_windows
from .recommend import OpportunityWatcher, Recommender
from .transitions import STYLES, transition

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[Any]]

DECK = {"type": "integer", "minimum": 1, "maximum": 4, "description": "Deck number (1-4)."}
FADE = {"type": "number", "minimum": 0, "description": "Fade time in seconds (0 = instant)."}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Handler


class DJTools:
    def __init__(
        self,
        dj: DJ,
        jobs: JobManager | None = None,
        recommender: Recommender | None = None,
        watcher: OpportunityWatcher | None = None,
        download_dir: str | Path | None = None,
    ) -> None:
        self.dj = dj
        self.download_dir = download_dir
        self.jobs = jobs or JobManager()
        self.rec = recommender or Recommender(dj)
        self.watcher = watcher
        self.tools: dict[str, Tool] = {t.name: t for t in self._build()}

    # ----------------------------------------------------------------- calling
    async def call(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        tool = self.tools.get(name)
        if tool is None:
            return {"error": f"unknown tool {name}"}
        try:
            result = await tool.handler(dict(args or {}))
        except TrackNotFound as exc:
            return {
                "error": str(exc),
                "not_in_library": exc.query,
                "where_to_get_it": exc.where_to_get_it(),
            }
        except (MixxxError, youtube.DownloadError, ValueError, KeyError, TimeoutError) as exc:
            return {"error": str(exc)}
        return result if isinstance(result, dict) else {"result": result}

    async def call_json(self, name: str, args: dict[str, Any] | None) -> tuple[str, bool]:
        res = await self.call(name, args)
        return json.dumps(res, ensure_ascii=False), "error" in res

    def _library_track_for(self, path: Path, source: dict[str, Any]) -> Track | None:
        """The loadable library track for a downloaded file, if Mixxx has it (or we can add it)."""
        lib = self.dj.library
        track = lib.find_by_location(path)
        if track is None:
            self.dj.reload_library()  # Mixxx may have scanned it since we last looked
            track = lib.find_by_location(path)
        if track is None:
            track = lib.add_file(
                path, source.get("artist") or "", source.get("title") or path.stem, float(source.get("duration") or 0)
            )
        return track

    # --------------------------------------------------------------- handlers
    def _build(self) -> list[Tool]:
        dj = self.dj
        tools: list[Tool] = []

        def tool(name: str, description: str, schema: dict[str, Any]):
            def deco(fn: Handler) -> Handler:
                tools.append(Tool(name, description, schema, fn))
                return fn

            return deco

        @tool("get_status", "What is loaded and playing on every deck: track, position, BPM, volume, EQ, loop state, plus running jobs.", _obj({}))
        async def get_status(_: dict[str, Any]) -> dict[str, Any]:
            st = await dj.status()
            st["jobs"] = self.jobs.list(include_finished=False)
            if self.watcher and self.watcher.current:
                st["opportunities"] = [
                    {k: i.as_dict()[k] for k in ("id", "name", "why", "in_s", "incoming")}
                    for i in self.watcher.current[:3]
                ]
            return st

        @tool(
            "search_library",
            "Fuzzy-search the user's Mixxx library by artist, title and album. Returns track ids, BPM, Camelot key and genre.",
            _obj({"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 25}}, ["query"]),
        )
        async def search_library(a: dict[str, Any]) -> dict[str, Any]:
            return {"tracks": dj.find_tracks(a["query"], int(a.get("limit", 8)))}

        @tool(
            "load_track",
            "Load a track from the library onto a deck (the deck must not be playing). Give a search query "
            "(e.g. 'Drake Future Life Is Good') or a track_id from search_library.",
            _obj({"deck": DECK, "query": {"type": "string"}, "track_id": {"type": "integer"},
                  "play": {"type": "boolean", "description": "Start playing immediately."}}, ["deck"]),
        )
        async def load_track(a: dict[str, Any]) -> dict[str, Any]:
            return await dj.load(a["deck"], a.get("query"), a.get("track_id"), bool(a.get("play", False)))

        @tool(
            "search_youtube",
            "Search YouTube for a song to add to the library. Returns the top results numbered from 1 (default 3) "
            "with title, channel, duration and url; show them as a numbered list and pass the chosen url to "
            "add_from_youtube.",
            _obj({"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, ["query"]),
        )
        async def search_youtube(a: dict[str, Any]) -> dict[str, Any]:
            results = await asyncio.to_thread(youtube.search_tracks, a["query"], int(a.get("limit", 3)))
            out = []
            for n, r in enumerate(results, 1):
                item: dict[str, Any] = {
                    "n": n,
                    "title": r.title,
                    "channel": r.uploader,
                    "duration": youtube.format_duration(r.duration) if r.duration is not None else None,
                    "url": r.url,
                }
                cached = youtube.find_cached(r.video_id, self.download_dir)
                if cached:
                    item["already_downloaded"] = str(cached)
                out.append(item)
            return {"query": a["query"], "results": out}

        @tool(
            "add_from_youtube",
            "Add a YouTube video's audio to the library as a tagged 320 kbps MP3 (an earlier download of the "
            "same video is reused). Give deck (and play) to load it straight away once it is in the library.",
            _obj({"url": {"type": "string"}, "deck": DECK,
                  "play": {"type": "boolean", "description": "Start playing after loading."}}, ["url"]),
        )
        async def add_from_youtube(a: dict[str, Any]) -> dict[str, Any]:
            if a.get("deck") is not None:
                dj.check_deck(a["deck"])
            already = youtube.find_cached(a["url"], self.download_dir) is not None
            path = await asyncio.to_thread(youtube.download_track, a["url"], self.download_dir)
            source = youtube.read_source(path) or {}
            out: dict[str, Any] = {
                "file": str(path),
                "artist": source.get("artist"),
                "title": source.get("title") or path.stem,
                "duration_s": source.get("duration"),
                "already_downloaded": already,
            }
            track = self._library_track_for(path, source)
            if track is None:
                out["in_mixxx_library"] = False
                out["next_step"] = (
                    f"Mixxx has not scanned this file yet. Make sure {path.parent} is one of Mixxx's library "
                    "folders (Preferences > Library), run Library > Rescan Library in Mixxx, then call "
                    "reload_library and load_track."
                )
                return out
            out["in_mixxx_library"] = True
            out["track"] = track.brief()
            if a.get("deck") is not None:
                out["load"] = await dj.load(a["deck"], track_id=track.id, play=bool(a.get("play", False)))
            return out

        @tool(
            "suggest_next_tracks",
            "Rank library tracks that would mix well after the track on a deck (tempo, Camelot key, genre).",
            _obj({"deck": DECK, "limit": {"type": "integer", "minimum": 1, "maximum": 25}, "genre": {"type": "string"}}, ["deck"]),
        )
        async def suggest_next_tracks(a: dict[str, Any]) -> dict[str, Any]:
            return {"suggestions": dj.suggest_next(a["deck"], int(a.get("limit", 8)), a.get("genre"))}

        @tool(
            "transport",
            "Play, pause, seek (seconds), jump by beats, jump to a lyric, or trigger a hotcue on a deck.",
            _obj({
                "deck": DECK,
                "action": {"type": "string", "enum": ["play", "pause", "seek", "beatjump", "seek_to_lyric", "hotcue"]},
                "position_s": {"type": "number", "description": "For seek: track position in seconds."},
                "beats": {"type": "number", "description": "For beatjump: beats (negative = backwards)."},
                "phrase": {"type": "string", "description": "For seek_to_lyric."},
                "occurrence": {"type": "integer", "minimum": 1},
                "hotcue": {"type": "integer", "minimum": 1, "maximum": 36},
                "hotcue_action": {"type": "string", "enum": ["activate", "set", "goto", "gotoandplay", "clear"]},
            }, ["deck", "action"]),
        )
        async def transport(a: dict[str, Any]) -> dict[str, Any]:
            deck, action = a["deck"], a["action"]
            out: dict[str, Any] = {"deck": deck, "action": action}
            if action == "play":
                await dj.play(deck)
            elif action == "pause":
                await dj.pause(deck)
            elif action == "seek":
                await dj.seek(deck, float(a["position_s"]))
            elif action == "beatjump":
                await dj.beatjump(deck, float(a["beats"]))
            elif action == "seek_to_lyric":
                out["lyric"] = await dj.seek_to_lyric(deck, a["phrase"], a.get("occurrence"))
            elif action == "hotcue":
                await dj.hotcue(deck, int(a["hotcue"]), a.get("hotcue_action", "activate"))
            out["deck_state"] = (await dj.status())["decks"][deck - 1] if dj.backend.connected else None
            return out

        @tool(
            "set_volume",
            "Set or change a deck's volume fader (0-1) or the master gain (1.0 = unity). Use 'change' for relative "
            "requests like 'a bit louder' (+0.1).",
            _obj({
                "target": {"description": "Deck number 1-4 or 'master'.", "anyOf": [DECK, {"type": "string", "enum": ["master"]}]},
                "level": {"type": "number", "minimum": 0, "maximum": 2},
                "change": {"type": "number", "minimum": -1, "maximum": 1},
                "fade_s": FADE,
            }, ["target"]),
        )
        async def set_volume(a: dict[str, Any]) -> dict[str, Any]:
            await dj.backend.refresh()
            fade = float(a.get("fade_s", 0.5))
            if str(a["target"]) == "master":
                cur = float(dj.backend.state.master.get("gain", 1.0))
                level = a.get("level", cur + float(a.get("change", 0.0)))
                level = min(float(level), 2.0)
                await dj.set_master_gain(level, fade)
                return {"master_gain": round(level, 3), "fade_s": fade}
            deck = int(a["target"])
            cur = dj.deck_state(deck).v("volume", 1.0)
            level = a.get("level", cur + float(a.get("change", 0.0)))
            level = max(0.0, min(1.0, float(level)))
            await dj.set_volume(deck, level, fade)
            return {"deck": deck, "volume": round(level, 3), "fade_s": fade}

        @tool(
            "set_eq",
            "Set a deck's EQ bands. 1.0 = neutral, 0 = kill, up to 2 = boost. Omit bands you don't want to change.",
            _obj({"deck": DECK, "low": {"type": "number", "minimum": 0, "maximum": 2},
                  "mid": {"type": "number", "minimum": 0, "maximum": 2},
                  "high": {"type": "number", "minimum": 0, "maximum": 2}, "fade_s": FADE}, ["deck"]),
        )
        async def set_eq(a: dict[str, Any]) -> dict[str, Any]:
            changed = {}
            for band in ("low", "mid", "high"):
                if band in a:
                    await dj.set_eq(a["deck"], band, float(a[band]), float(a.get("fade_s", 0.0)))
                    changed[band] = a[band]
            return {"deck": a["deck"], "eq": changed}

        @tool(
            "set_filter",
            "Sweep a deck's filter: 0 = off, negative = low-pass (muffled, -1 max), positive = high-pass (thin, +1 max).",
            _obj({"deck": DECK, "amount": {"type": "number", "minimum": -1, "maximum": 1}, "fade_s": FADE}, ["deck", "amount"]),
        )
        async def set_filter(a: dict[str, Any]) -> dict[str, Any]:
            value = 0.5 + float(a["amount"]) / 2.0
            await dj.set_filter(a["deck"], value, float(a.get("fade_s", 0.0)))
            return {"deck": a["deck"], "filter": a["amount"]}

        @tool(
            "crossfader",
            "Move the crossfader: -1 = full left (usually deck 1), 0 = centre, 1 = full right (usually deck 2).",
            _obj({"position": {"type": "number", "minimum": -1, "maximum": 1}, "fade_s": FADE}, ["position"]),
        )
        async def crossfader(a: dict[str, Any]) -> dict[str, Any]:
            await dj.set_crossfader(float(a["position"]), float(a.get("fade_s", 0.0)))
            return {"crossfader": a["position"]}

        @tool(
            "set_tempo",
            "Change a deck's tempo (absolute bpm or percent from the original), toggle sync to another deck, or keylock.",
            _obj({"deck": DECK, "bpm": {"type": "number", "minimum": 20, "maximum": 300},
                  "percent": {"type": "number", "minimum": -50, "maximum": 50},
                  "sync": {"type": "boolean"}, "sync_to": DECK, "keylock": {"type": "boolean"}}, ["deck"]),
        )
        async def set_tempo(a: dict[str, Any]) -> dict[str, Any]:
            deck = a["deck"]
            out: dict[str, Any] = {"deck": deck}
            if "keylock" in a:
                await dj.set_keylock(deck, bool(a["keylock"]))
                out["keylock"] = bool(a["keylock"])
            if "sync" in a:
                await dj.set_sync(deck, bool(a["sync"]), leader=a.get("sync_to"))
                out["sync"] = bool(a["sync"])
            if "bpm" in a or "percent" in a:
                out["bpm"] = round(await dj.set_tempo(deck, a.get("bpm"), a.get("percent")), 2)
            return out

        @tool(
            "loop",
            "Loops. action 'now': loop the current beats; 'at_time': arm a loop at position_s; 'at_lyric': arm a loop "
            "where a phrase is sung (Mixxx engages it exactly when reached); 'off': release the loop and keep playing.",
            _obj({"deck": DECK, "action": {"type": "string", "enum": ["now", "at_time", "at_lyric", "off"]},
                  "beats": {"type": "number", "minimum": 0.03125, "maximum": 64, "description": "Loop length in beats (default 4 = one bar)."},
                  "position_s": {"type": "number"}, "phrase": {"type": "string"},
                  "occurrence": {"type": "integer", "minimum": 1, "description": "Which time the phrase is sung (default: next one)."}},
                 ["deck", "action"]),
        )
        async def loop(a: dict[str, Any]) -> dict[str, Any]:
            deck, action, beats = a["deck"], a["action"], float(a.get("beats", 4))
            if action == "now":
                return {"deck": deck, "loop": await dj.loop_now(deck, beats)}
            if action == "at_time":
                return await dj.loop_at(deck, float(a["position_s"]), beats)
            if action == "at_lyric":
                return await dj.loop_at_lyric(deck, a["phrase"], beats, a.get("occurrence"))
            await dj.loop_off(deck)
            return {"deck": deck, "loop": "off"}

        @tool(
            "find_lyric",
            "Find when a word or phrase is sung in the track on a deck (or any library track). Returns every occurrence with times.",
            _obj({"phrase": {"type": "string"}, "deck": DECK, "track_query": {"type": "string"}}, ["phrase"]),
        )
        async def find_lyric(a: dict[str, Any]) -> dict[str, Any]:
            track = dj.resolve(a["track_query"]) if a.get("track_query") else None
            hits = await dj.lyric_hits(a.get("deck"), a["phrase"], track)
            return {"phrase": a["phrase"], "occurrences": [h.as_dict() for h in hits]}

        @tool(
            "recommend_transition",
            "Suggest a transition style, length and whether to beatmatch, from both decks' tempo, key and genre.",
            _obj({"from_deck": DECK, "to_deck": DECK}, ["from_deck", "to_deck"]),
        )
        async def recommend_transition(a: dict[str, Any]) -> dict[str, Any]:
            await dj.backend.refresh()
            da, dn = dj.deck_state(a["from_deck"]), dj.deck_state(a["to_deck"])
            ta, tn = dj.deck_track(a["from_deck"]), dj.deck_track(a["to_deck"])
            return music.recommend_transition(
                da.v("bpm") or (ta.bpm if ta else 0), dn.v("file_bpm") or (tn.bpm if tn else 0),
                ta.key if ta else "", tn.key if tn else "",
                f"{ta.genre if ta else ''} {tn.genre if tn else ''}",
            )

        @tool(
            "transition",
            "Mix from the playing deck into another loaded deck, starting on the next beat. style: "
            + ", ".join(STYLES) + " or auto. Runs in the background by default and returns a job id.",
            _obj({"from_deck": DECK, "to_deck": DECK,
                  "style": {"type": "string", "enum": [*STYLES, "auto"]},
                  "bars": {"type": "number", "minimum": 0, "maximum": 64},
                  "sync": {"type": "boolean", "description": "Beatmatch the incoming deck (default: when tempos are close)."},
                  "start_at_s": {"type": "number", "description": "Start the incoming track from this position."},
                  "background": {"type": "boolean"}}, ["from_deck", "to_deck"]),
        )
        async def transition_tool(a: dict[str, Any]) -> dict[str, Any]:
            async def run(job=None):
                return await transition(
                    dj, a["from_deck"], a["to_deck"], a.get("style", "auto"), a.get("bars"),
                    a.get("sync"), a.get("start_at_s"),
                    log=(job.log.append if job else (lambda _m: None)),
                )

            if a.get("background", True):
                job = self.jobs.start(f"transition deck {a['from_deck']} -> {a['to_deck']}", run)
                return {"job_id": job.id, "status": "started"}
            return await run()

        @tool(
            "run_mix_plan",
            "Run a timed sequence in the background. Steps are {\"tool\": name, \"args\": {...}} or {\"wait\": {...}} "
            "where wait is one of: {\"seconds\": s}, {\"deck\": d, \"position_s\": t}, {\"deck\": d, \"lyric\": phrase, "
            "\"occurrence\"?: n, \"lead_s\"?: s}, {\"deck\": d, \"beats\": n}, {\"deck\": d, \"loop_active\": true}, "
            "{\"deck\": d, \"remaining_s\": s}, {\"deck\": d, \"next_bar\": true}. Use it for anything triggered by lyrics "
            "or musical events.",
            _obj({"steps": {"type": "array", "minItems": 1, "items": {"type": "object"}},
                  "description": {"type": "string"}}, ["steps"]),
        )
        async def run_mix_plan(a: dict[str, Any]) -> dict[str, Any]:
            steps = a["steps"]
            validate_plan(steps, set(self.tools) - {"run_mix_plan"})
            job = self.jobs.start(
                a.get("description") or f"mix plan ({len(steps)} steps)",
                lambda job: run_plan(dj, steps, self.call, job),
            )
            return {"job_id": job.id, "status": "started", "steps": len(steps)}

        @tool("list_jobs", "Show background transitions and mix plans with their progress and errors.", _obj({}))
        async def list_jobs(_: dict[str, Any]) -> dict[str, Any]:
            return {"jobs": self.jobs.list()}

        @tool(
            "cancel_job",
            "Cancel a running job (omit job_id to cancel all). Fades in progress stop where they are.",
            _obj({"job_id": {"type": "integer"}}),
        )
        async def cancel_job(a: dict[str, Any]) -> dict[str, Any]:
            ids = self.jobs.cancel(a.get("job_id"))
            await dj.backend.cancel_ramps()
            return {"cancelled": ids}

        @tool(
            "recommend_transitions",
            "Recommend how to get from the track on from_deck into another song, based on the songs themselves: "
            "wordplay (title drops, shared lyrics, name drops), harmonic blends, energy boosts, half-time bridges, "
            "tempo rides, vocal rides, double drops, echo outs and more. Name a target with to_deck, track_query or "
            "track_id; omit all three to also pick the best next tracks from the library. Each idea has an id "
            "for run_idea, a reason, when it happens and a ready plan.",
            _obj({"from_deck": DECK, "to_deck": DECK, "track_query": {"type": "string"}, "track_id": {"type": "integer"},
                  "genre": {"type": "string", "description": "Only consider next tracks of this genre."},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 12}}, ["from_deck"]),
        )
        async def recommend_transitions(a: dict[str, Any]) -> dict[str, Any]:
            limit = int(a.get("limit", 6))
            target = None
            if a.get("track_id") is not None or a.get("track_query"):
                target = dj.resolve(a.get("track_query"), a.get("track_id"))
            elif a.get("to_deck") is not None:
                target = dj.deck_track(a["to_deck"])
                if target is None:
                    raise MixxxError(f"nothing known is loaded on deck {a['to_deck']}")
            await dj.backend.refresh()
            if target is not None:
                ideas = await self.rec.for_pair(a["from_deck"], target, a.get("to_deck"), limit)
            else:
                ideas = await self.rec.next_moves(a["from_deck"], limit, genre=a.get("genre"))
            return {"ideas": [i.as_dict() for i in ideas]}

        @tool(
            "get_opportunities",
            "Transition opportunities coming up in the music right now, e.g. the playing song is about to sing "
            "another library track's title. Sorted best first, with in_s = seconds until the moment.",
            _obj({"horizon_s": {"type": "number", "minimum": 10, "maximum": 600}}),
        )
        async def get_opportunities(a: dict[str, Any]) -> dict[str, Any]:
            await dj.backend.refresh()
            horizon = float(a.get("horizon_s", 90))
            ideas = []
            for n in range(1, dj.num_decks + 1):
                if dj.deck_state(n).playing:
                    ideas.extend(await self.rec.opportunities(n, horizon, fetch=False))
            ideas.sort(key=lambda i: -i.score)
            return {"opportunities": [i.as_dict() for i in ideas[:8]]}

        @tool(
            "run_idea",
            "Carry out a recommended transition idea (from recommend_transitions or get_opportunities) by id. "
            "It runs in the background like run_mix_plan and loads the incoming track first if needed.",
            _obj({"idea_id": {"type": "string"}}, ["idea_id"]),
        )
        async def run_idea(a: dict[str, Any]) -> dict[str, Any]:
            idea = self.rec.ideas.get(a["idea_id"])
            if idea is None:
                raise MixxxError(f"unknown or expired idea {a['idea_id']}; ask for fresh recommendations")
            validate_plan(idea.plan, set(self.tools) - {"run_mix_plan"})
            job = self.jobs.start(
                f"{idea.name} into {idea.incoming.title}",
                lambda job: run_plan(dj, idea.plan, self.call, job),
            )
            return {"job_id": job.id, "status": "started", "idea": idea.name, "why": idea.why}

        @tool(
            "get_lyrics",
            "The lyrics of the track on a deck, or of any library track: lines with timestamps when synced, "
            "plus the vocal-free windows that make clean mix points.",
            _obj({"deck": DECK, "track_query": {"type": "string"}, "track_id": {"type": "integer"}}),
        )
        async def get_lyrics(a: dict[str, Any]) -> dict[str, Any]:
            if a.get("track_id") is not None or a.get("track_query"):
                track = dj.resolve(a.get("track_query"), a.get("track_id"))
            elif a.get("deck") is not None:
                track = dj.deck_track(a["deck"])
                if track is None:
                    raise MixxxError(f"nothing known is loaded on deck {a['deck']}")
            else:
                raise MixxxError("give a deck, track_query or track_id")
            lyr = await dj.lyrics.get(track)
            if lyr is None:
                return {"track": track.brief(), "lyrics": None, "message": "no lyrics found"}
            lines = [
                {"time_s": round(ln.time, 2) if lyr.synced else None, "text": ln.text}
                for ln in lyr.lines if ln.text.strip()
            ]
            return {
                "track": track.brief(),
                "source": lyr.source,
                "synced": lyr.synced,
                "lines": lines[:200],
                "instrumental_windows_s": [
                    [round(s, 1), round(e, 1)] for s, e in instrumental_windows(lyr, track.duration)
                ],
            }

        @tool(
            "analyze_track",
            "Audio structure of a track (needs numpy, and ffmpeg for non-WAV files): energy, intro end, outro "
            "start, drops and breakdowns in seconds.",
            _obj({"deck": DECK, "track_query": {"type": "string"}, "track_id": {"type": "integer"}}),
        )
        async def analyze_track(a: dict[str, Any]) -> dict[str, Any]:
            if a.get("track_id") is not None or a.get("track_query"):
                track = dj.resolve(a.get("track_query"), a.get("track_id"))
            else:
                track = dj.deck_track(a.get("deck", 1))
            if track is None:
                raise MixxxError("no track to analyse")
            prof = await self.rec.profile(track)
            return {
                "track": track.brief(),
                "genre_family": prof.family,
                "energy": prof.energy,
                "analysis": prof.analysis.to_dict() if prof.analysis else None,
                "note": None if prof.analysis else "audio analysis unavailable (install numpy and ffmpeg, or the file is missing)",
            }

        @tool(
            "reload_library",
            "Re-read the Mixxx library after new music was added and Mixxx rescanned it.",
            _obj({}),
        )
        async def reload_library(_: dict[str, Any]) -> dict[str, Any]:
            return {"tracks": dj.reload_library()}

        @tool(
            "raw_control",
            "Escape hatch: read or write any Mixxx control object (group like '[Channel1]', key like 'pfl'). "
            "Give value to set it, press=true for momentary buttons, neither to read.",
            _obj({"group": {"type": "string"}, "key": {"type": "string"}, "value": {"type": "number"},
                  "press": {"type": "boolean"}}, ["group", "key"]),
        )
        async def raw_control(a: dict[str, Any]) -> dict[str, Any]:
            g, k = a["group"], a["key"]
            if a.get("press"):
                await dj.backend.press(g, k)
            elif "value" in a:
                await dj.backend.set(g, k, float(a["value"]))
            return {"group": g, "key": k, "value": await dj.backend.get(g, k)}

        return tools


__all__ = ["DJTools", "Tool", "deck_group"]

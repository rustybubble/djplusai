"""Transition recommendations from the songs themselves: lyrics, tempo, key, energy, structure.

Each technique below is one way working DJs get from one record to the next. A
technique looks at the outgoing and incoming songs and, when it fits, returns an
:class:`Idea`: a scored suggestion with the reason, the moment it applies and an
executable mix plan (the same step format as ``run_mix_plan``).

Techniques
----------
Lyric-driven (need lyrics):
  title_drop     the outgoing song sings the incoming song's title: loop it, slam the new song in
  word_handoff   both songs share a distinctive word or phrase: loop it in one, the other answers
  name_drop      the outgoing song names the incoming artist: cut in right after the name
  remix_flip     original <-> remix/edit: switch versions on the same lyric line
  loop_and_drop  loop a hook word from the outgoing song, then drop the new one on the one
  vocal_ride     let the outgoing vocal ride over the incoming song's instrumental intro (live mashup)
Musical:
  harmonic_blend long EQ/bass-swap blend in a vocal-free window when keys and tempos agree
  energy_boost   key move that lifts the room (+1, +2, relative or +7) into a higher-energy track
  double_drop    line up both drops (needs audio analysis)
  halftime_bridge  70<->140 / 87<->174: tracks lock together at half or double time
  tempo_ride     close the tempo gap gradually before blending
  echo_out / spinback / brake   clean exits across big tempo or genre gaps
"""

from __future__ import annotations

import asyncio
import itertools
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import music
from .library import Track, normalize
from .lyrics import Lyrics, _norm_tokens, find_phrase, instrumental_windows, vocal_spans

if TYPE_CHECKING:  # pragma: no cover
    from .analysis import Analyzer, TrackAnalysis
    from .controller import DJ

STOPWORDS = set(
    """a an the and or but if then so of to in on at by for with from up down out over under into onto off
    i me my mine you your yours he him his she her hers it its we us our they them their this that these those
    is am are was were be been being do does did doing have has had having will would shall should can could
    may might must not no yes just like get got go goes going gone come came make made take took know say said
    what when where who why how all any some every each more most much many very too also only own same than
    there here now again still ever never always all yeah yea yeh uh uhh oh ooh ohh ah ahh la na hey woah whoa
    huh ay ayy aye yo mm hmm eh ha let lets dont cant wont aint im youre hes shes its were theyre ive id ill
    gonna wanna gotta baby girl boy man gon tryna cause cuz one two""".split()
)
VERSION_TAGS = re.compile(
    r"[\(\[][^\)\]]*\b(remix|edit|mix|version|vip|bootleg|flip|rework|dub|extended|radio|clean|dirty|instrumental|acapella|live)\b[^\)\]]*[\)\]]",
    re.I,
)
FEAT = re.compile(r"[\(\[]?\b(feat\.?|ft\.?|featuring|with)\b.*$", re.I)

# Genre families: base energy and whether long blends are the norm.
FAMILIES = [
    ("dnb", ("drum", "dnb", "jungle", "neurofunk"), 0.9),
    ("techno", ("techno", "trance", "hard"), 0.85),
    ("house", ("house", "garage", "edm", "dance", "electro", "disco"), 0.72),
    ("afro", ("afro", "amapiano", "gqom"), 0.62),
    ("latin", ("reggaeton", "latin", "dembow", "dancehall", "moombah"), 0.66),
    ("hiphop", ("hip", "rap", "trap", "drill", "grime"), 0.6),
    ("rnb", ("r&b", "rnb", "soul", "neo"), 0.45),
    ("rock", ("rock", "metal", "punk", "indie", "alternative"), 0.7),
    ("pop", ("pop", "country", "k-pop", "funk"), 0.58),
]
BLEND_FAMILIES = {"house", "techno", "dnb", "afro"}


def family_of(genre: str) -> tuple[str, float]:
    g = (genre or "").lower()
    for name, words, energy in FAMILIES:
        if any(w in g for w in words):
            return name, energy
    return "other", 0.55


def _stem(tok: str) -> str:
    return tok[:-1] if len(tok) > 4 and tok.endswith("s") and not tok.endswith("ss") else tok


def _tokens(text: str) -> list[str]:
    text = text.lower().replace("'", "").replace("’", "")
    return [_stem(t) for t in re.findall(r"[a-z0-9$]+", text)]


def base_title(title: str) -> str:
    return normalize(FEAT.sub("", VERSION_TAGS.sub("", title)))


def title_phrase(title: str) -> str:
    """The title as it would be sung: no (feat. ...) / (Remix) tags, punctuation stripped."""
    return " ".join(_norm_tokens(FEAT.sub("", VERSION_TAGS.sub("", title))))


def is_version(title: str) -> bool:
    return bool(VERSION_TAGS.search(title))


def artist_names(artist: str) -> list[str]:
    parts = re.split(r"\s*(?:,|&|\+|\band\b|\bx\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|\bvs\.?)\s*", artist, flags=re.I)
    return [p.strip() for p in parts if p and len(p.strip()) >= 3]


@dataclass
class SongProfile:
    track: Track
    lyrics: Lyrics | None
    analysis: TrackAnalysis | None
    family: str
    energy: float

    @property
    def synced(self) -> bool:
        return bool(self.lyrics and self.lyrics.synced and self.lyrics.lines)

    @property
    def bar_s(self) -> float:
        return 4 * 60.0 / self.track.bpm if self.track.bpm else 2.0

    def first_vocal(self) -> float | None:
        spans = vocal_spans(self.lyrics) if self.synced else []
        return spans[0][0] if spans else None

    def intro_len(self) -> float:
        """Seconds of instrumental intro (vocal-free, and low energy if analysed)."""
        fv = self.first_vocal()
        if fv is not None:
            return fv
        return self.analysis.intro_end if self.analysis else 0.0

    def hook_lines(self) -> list[str]:
        if not self.lyrics:
            return []
        counts = Counter(normalize(ln.text) for ln in self.lyrics.lines if ln.text.strip())
        return [line for line, n in counts.most_common(3) if n >= 2]


def build_profile(track: Track, lyrics: Lyrics | None, analysis: TrackAnalysis | None = None) -> SongProfile:
    fam, energy = family_of(track.genre)
    if track.bpm:
        energy += max(-0.1, min(0.1, (track.bpm - 120) / 400))
    if analysis:
        energy = 0.5 * energy + 0.5 * analysis.energy
    return SongProfile(track, lyrics, analysis, fam, round(max(0.0, min(1.0, energy)), 3))


@dataclass
class Idea:
    technique: str
    name: str
    score: float
    why: str
    incoming: Track
    from_deck: int | None
    to_deck: int | None
    at_s: float | None = None  # outgoing-track time the move happens
    in_s: float | None = None  # seconds from now, while the outgoing deck plays
    incoming_start_s: float = 0.0
    plan: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    id: str = ""

    def key(self) -> tuple:
        return (self.technique, self.incoming.id, round(self.at_s or -1, 0))

    def moment(self) -> tuple:
        return (self.incoming.id, round((self.at_s or -1) / 8.0))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "technique": self.technique,
            "name": self.name,
            "score": round(self.score, 3),
            "why": self.why,
            "incoming": self.incoming.brief(),
            "from_deck": self.from_deck,
            "to_deck": self.to_deck,
            "at_s": None if self.at_s is None else round(self.at_s, 2),
            "in_s": None if self.in_s is None else round(self.in_s, 1),
            "incoming_start_s": round(self.incoming_start_s, 2),
            "evidence": self.evidence,
            "warnings": self.warnings,
            "plan": self.plan,
        }


@dataclass
class Context:
    out: SongProfile
    inn: SongProfile
    from_deck: int | None
    to_deck: int | None
    pos: float  # outgoing position (s)
    playing: bool
    out_bpm: float  # outgoing tempo right now
    speed: float  # outgoing playback speed
    first_beat: float  # outgoing beat-grid offset (s)
    incoming_loaded: bool

    @property
    def tempo(self) -> music.TempoMatch | None:
        return music.tempo_match(self.out_bpm, self.inn.track.bpm)

    @property
    def key_score(self) -> float:
        return music.key_compatibility(self.out.track.key, self.inn.track.key)

    def in_s(self, at: float | None) -> float | None:
        if at is None or not self.playing:
            return None
        return max(0.0, (at - self.pos) / max(self.speed, 1e-6))

    def next_phrase(self, after: float, beats: int = 32) -> float:
        period = 60.0 / self.out.track.bpm if self.out.track.bpm else 0.5
        n = math.ceil((after - self.first_beat) / (beats * period) - 1e-6)
        return self.first_beat + n * beats * period


# ------------------------------------------------------------------ plan pieces


def _load(ctx: Context) -> list[dict[str, Any]]:
    if ctx.incoming_loaded or ctx.to_deck is None:
        return []
    return [{"tool": "load_track", "args": {"deck": ctx.to_deck, "track_id": ctx.inn.track.id}}]


def _transition(ctx: Context, style: str, bars: float, start: float | None = None, sync: bool | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"from_deck": ctx.from_deck, "to_deck": ctx.to_deck, "style": style, "bars": bars}
    if start is not None:
        args["start_at_s"] = round(max(0.0, start), 2)
    if sync is not None:
        args["sync"] = sync
    return {"tool": "transition", "args": args}


def _loop_then(ctx: Context, phrase: str, occurrence: int, beats: float, then: list[dict[str, Any]]) -> list[dict[str, Any]]:
    a = ctx.from_deck
    return _load(ctx) + [
        {"tool": "loop", "args": {"deck": a, "action": "at_lyric", "phrase": phrase, "beats": beats, "occurrence": occurrence}},
        {"wait": {"deck": a, "loop_active": True}},
        {"wait": {"deck": a, "beats": beats * 2}},
        *then,
    ]


def _blendable(ctx: Context) -> bool:
    tm = ctx.tempo
    return tm is not None and abs(tm.percent) <= 6.0


def _cut_style(ctx: Context) -> str:
    return "cut" if _blendable(ctx) else "echo_out"


def _upcoming_ok(ctx: Context, t: float, lead: float = 3.0) -> bool:
    return not ctx.playing or t >= ctx.pos + lead


# ------------------------------------------------------------------ techniques


def title_drop(ctx: Context) -> list[Idea]:
    if not ctx.out.synced:
        return []
    title = title_phrase(ctx.inn.track.title)
    toks = title.split()
    if not toks or (len(toks) == 1 and (toks[0] in STOPWORDS or len(toks[0]) < 4)):
        return []
    if all(t in STOPWORDS for t in toks):
        return []
    hits = [h for h in find_phrase(ctx.out.lyrics, title) if _upcoming_ok(ctx, h.time)]
    if not hits:
        return []
    h = hits[0]
    in_hits = find_phrase(ctx.inn.lyrics, title) if ctx.inn.synced else []
    start = in_hits[0].line_time if in_hits else max(0.0, (ctx.inn.first_vocal() or ctx.inn.bar_s) - ctx.inn.bar_s)
    where = "right on its own hook" if in_hits else "at its first verse"
    plan = _loop_then(ctx, title, h.occurrence, 2, [_transition(ctx, "cut", 0, start, sync=_blendable(ctx))])
    return [Idea(
        "title_drop", "Title drop", 0.9 + min(0.08, 0.02 * len(toks)) + (0.02 if h.score >= 95 else 0),
        f'"{h.line}" sings the title "{ctx.inn.track.title}". Loop "{title}" twice, then slam {ctx.inn.track.title} in {where}.',
        ctx.inn.track, ctx.from_deck, ctx.to_deck, h.time, ctx.in_s(h.time), start, plan,
        {"outgoing_line": h.line, "phrase": title, "occurrence": h.occurrence,
         "incoming_line": in_hits[0].line if in_hits else None},
    )]


def name_drop(ctx: Context) -> list[Idea]:
    if not ctx.out.synced:
        return []
    for name in artist_names(ctx.inn.track.artist):
        phrase = normalize(name)
        toks = phrase.split()
        if not toks or all(t in STOPWORDS for t in toks) or len(phrase) < 4:
            continue
        hits = [h for h in find_phrase(ctx.out.lyrics, phrase) if _upcoming_ok(ctx, h.time)]
        if not hits:
            continue
        h = hits[0]
        start = max(0.0, (ctx.inn.first_vocal() or ctx.inn.bar_s) - ctx.inn.bar_s)
        plan = _load(ctx) + [
            {"wait": {"deck": ctx.from_deck, "lyric": phrase, "occurrence": h.occurrence, "lead_s": -0.6}},
            _transition(ctx, _cut_style(ctx), 1 if _cut_style(ctx) == "echo_out" else 0, start, sync=_blendable(ctx)),
        ]
        same_artist = normalize(name) in normalize(ctx.out.track.artist)
        return [Idea(
            "name_drop", "Name drop", 0.74 - (0.1 if same_artist else 0),
            f'"{h.line}" name-checks {name}. Cut into {ctx.inn.track.artist} - {ctx.inn.track.title} right after the name.',
            ctx.inn.track, ctx.from_deck, ctx.to_deck, h.time, ctx.in_s(h.time), start, plan,
            {"outgoing_line": h.line, "name": name, "occurrence": h.occurrence},
        )]
    return []


def _ngrams(toks: list[str], n: int) -> list[tuple[int, tuple[str, ...]]]:
    return [(i, tuple(toks[i : i + n])) for i in range(len(toks) - n + 1)]


def _distinct(gram: tuple[str, ...]) -> bool:
    content = [t for t in gram if t not in STOPWORDS]
    if not content:
        return False
    if len(gram) > 1 and (gram[0] in STOPWORDS or gram[-1] in STOPWORDS):
        return False  # "the midnight train" is really "midnight train"
    if len(gram) == 1:
        return len(gram[0]) >= 4
    return sum(len(t) for t in content) >= 5


def shared_phrases(out: Lyrics, inn: Lyrics, after: float | None = None, limit: int = 3) -> list[dict[str, Any]]:
    """Distinctive words/phrases both songs sing, best first."""
    inn_index: dict[tuple[str, ...], tuple[int, int]] = {}
    inn_counts: Counter[tuple[str, ...]] = Counter()
    for li, line in enumerate(inn.lines):
        toks = _tokens(line.text)
        for n in (3, 2, 1):
            for i, g in _ngrams(toks, n):
                if _distinct(g):
                    inn_counts[g] += 1
                    inn_index.setdefault(g, (li, i))
    found: dict[tuple[str, ...], dict[str, Any]] = {}
    for line in out.lines:
        if after is not None and out.synced and line.time < after:
            continue
        toks = _tokens(line.text)
        for n in (3, 2, 1):
            for _i, g in _ngrams(toks, n):
                if g in inn_index and g not in found:
                    avg = sum(len(t) for t in g) / n
                    weight = {3: 1.0, 2: 0.86, 1: 0.62}[n] * min(1.0, 0.55 + avg / 12)
                    if inn_counts[g] >= 2:
                        weight *= 1.12  # the incoming song repeats it: probably its hook
                    li, _ = inn_index[g]
                    found[g] = {"phrase": " ".join(g), "weight": round(min(weight, 1.0), 3),
                                "outgoing_line": line.text, "incoming_line": inn.lines[li].text}
    # Drop phrases contained in a longer shared phrase.
    items = sorted(found.values(), key=lambda x: (-len(x["phrase"].split()), -x["weight"]))
    kept: list[dict[str, Any]] = []
    for it in items:
        if not any(f" {it['phrase']} " in f" {k['phrase']} " for k in kept):
            kept.append(it)
    kept.sort(key=lambda x: -x["weight"])
    return kept[:limit]


def word_handoff(ctx: Context) -> list[Idea]:
    if not (ctx.out.synced and ctx.inn.lyrics):
        return []
    ideas = []
    for sp in shared_phrases(ctx.out.lyrics, ctx.inn.lyrics, ctx.pos + 3 if ctx.playing else None, limit=2):
        hits = [h for h in find_phrase(ctx.out.lyrics, sp["phrase"]) if _upcoming_ok(ctx, h.time)]
        if not hits:
            continue
        h = hits[0]
        in_hits = find_phrase(ctx.inn.lyrics, sp["phrase"]) if ctx.inn.synced else []
        start = max(0.0, in_hits[0].time - 0.3) if in_hits else 0.0
        plan = _loop_then(ctx, sp["phrase"], h.occurrence, 1, [_transition(ctx, _cut_style(ctx), 0 if _blendable(ctx) else 1, start, sync=_blendable(ctx))])
        warn = [] if in_hits else ["incoming lyrics have no timestamps: cue its line by hand for the answer"]
        ideas.append(Idea(
            "word_handoff", "Wordplay handoff", 0.62 + 0.3 * sp["weight"],
            f'Both songs sing "{sp["phrase"]}". Loop it out of {ctx.out.track.title} ("{sp["outgoing_line"]}") '
            f'and let {ctx.inn.track.title} answer with "{sp["incoming_line"]}".',
            ctx.inn.track, ctx.from_deck, ctx.to_deck, h.time, ctx.in_s(h.time), start, plan,
            {**sp, "occurrence": h.occurrence}, warn,
        ))
    return ideas


def remix_flip(ctx: Context) -> list[Idea]:
    a, b = ctx.out.track, ctx.inn.track
    if base_title(a.title) != base_title(b.title) or a.id == b.id or not (is_version(a.title) or is_version(b.title)):
        return []
    at, start, line = None, 0.0, None
    if ctx.out.synced and ctx.inn.synced:
        inn_lines = {normalize(ln.text): ln.time for ln in ctx.inn.lyrics.lines if ln.text.strip()}
        for ln in ctx.out.lyrics.lines:
            if _upcoming_ok(ctx, ln.time, lead=8) and normalize(ln.text) in inn_lines:
                at, start, line = ln.time, inn_lines[normalize(ln.text)], ln.text
                break
    if at is not None:
        plan = _load(ctx) + [
            {"tool": "set_tempo", "args": {"deck": ctx.to_deck, "sync": True, "sync_to": ctx.from_deck}},
            {"wait": {"deck": ctx.from_deck, "position_s": round(at - 0.05, 2)}},
            _transition(ctx, "cut", 0, start, sync=True),
        ]
        why = f'Same song, different version: switch mid-lyric on "{line}" so the vocal carries straight on.'
    else:
        at = ctx.next_phrase(ctx.pos + 8) if ctx.playing else None
        plan = _load(ctx) + [{"wait": {"deck": ctx.from_deck, "next_phrase": True}}, _transition(ctx, "cut", 0, 0.0, sync=True)]
        why = "Same song, different version: flip to the other version on the next phrase."
    return [Idea("remix_flip", "Remix flip", 0.85 if line else 0.7, why, b, ctx.from_deck, ctx.to_deck,
                 at, ctx.in_s(at), start, plan, {"shared_line": line})]


def _mix_window(ctx: Context) -> tuple[float | None, float]:
    """Where to start a long blend in the outgoing track, and how long it may last."""
    out = ctx.out
    dur = out.track.duration or 0
    candidates: list[tuple[float, float]] = []
    if out.synced and dur:
        candidates = [w for w in instrumental_windows(out.lyrics, dur, min_len=4 * out.bar_s) if w[1] >= dur - 2 * out.bar_s]
    if out.analysis and not candidates and dur:
        candidates = [(out.analysis.outro_start, dur)]
    if not candidates:
        if not dur:
            return None, 16 * out.bar_s
        candidates = [(max(0.0, dur - 32 * out.bar_s), dur)]
    start, end = candidates[-1]
    start = max(start, ctx.pos + 4 if ctx.playing else start)
    start = ctx.next_phrase(start, 16)
    return start, max(0.0, end - start)


def harmonic_blend(ctx: Context) -> list[Idea]:
    tm = ctx.tempo
    if tm is None or abs(tm.percent) > 6 or tm.multiplier != 1.0 or ctx.key_score < 0.9:
        return []
    start, room = _mix_window(ctx)
    bar = ctx.out.bar_s
    intro = ctx.inn.intro_len()
    bars = max(4, min(32, int(min(room, intro if intro > 0 else room) / bar) // 4 * 4 or 8))
    style = "bass_swap" if {ctx.out.family, ctx.inn.family} & BLEND_FAMILIES else "crossfade"
    warns = []
    if intro < bars * bar * 0.5 and ctx.inn.synced:
        warns.append(f"{ctx.inn.track.title} starts singing after {intro:.0f}s: keep the overlap short to avoid a vocal clash")
    plan = _load(ctx) + ([{"wait": {"deck": ctx.from_deck, "position_s": round(start, 2)}}] if start is not None else []) + [
        _transition(ctx, style, bars, 0.0, sync=True)]
    rel = "the same key" if ctx.key_score == 1.0 else "neighbouring keys"
    return [Idea(
        "harmonic_blend", "Harmonic blend", 0.6 + 0.2 * ctx.key_score + (0.08 if not warns else -0.05),
        f"{ctx.out.track.key} -> {ctx.inn.track.key} are {rel} and the tempos are {abs(tm.percent):.1f}% apart: "
        f"a {bars}-bar {style.replace('_', ' ')} in the vocal-free outro sounds like one continuous record.",
        ctx.inn.track, ctx.from_deck, ctx.to_deck, start, ctx.in_s(start), 0.0, plan,
        {"bars": bars, "window_s": round(room, 1), "incoming_intro_s": round(intro, 1)}, warns,
    )]


def _key_move(a: str, b: str) -> str | None:
    pa, pb = music.camelot_parts(a), music.camelot_parts(b)
    if not pa or not pb:
        return None
    step = (pb[0] - pa[0]) % 12
    if pa[1] == pb[1] and step == 1:
        return "+1 (smooth lift)"
    if pa[1] == pb[1] and step == 2:
        return "+2 (energy boost)"
    if pa[1] == pb[1] and step == 7:
        return "+7 (one semitone up, the 'jaws' lift)"
    if step == 0 and pa[1] != pb[1]:
        return "relative major/minor switch (mood flip)"
    return None


def energy_boost(ctx: Context) -> list[Idea]:
    move = _key_move(ctx.out.track.key, ctx.inn.track.key)
    lift = ctx.inn.energy - ctx.out.energy
    if not move or (lift < 0.03 and "relative" not in move):
        return []
    drop = ctx.inn.analysis.drops[0] if ctx.inn.analysis and ctx.inn.analysis.drops else None
    at = ctx.next_phrase(ctx.pos + 8) if ctx.playing else None
    if drop is not None:
        plan = _load(ctx) + [{"wait": {"deck": ctx.from_deck, "next_phrase": True}}, _transition(ctx, "cut", 0, drop, sync=_blendable(ctx))]
        how = f"cut straight into its drop at {drop:.0f}s on the next phrase"
    else:
        plan = _load(ctx) + [{"wait": {"deck": ctx.from_deck, "next_phrase": True}}, _transition(ctx, "filter_sweep", 8, 0.0, sync=_blendable(ctx))]
        how = "filter-sweep it in over 8 bars on the next phrase"
    return [Idea(
        "energy_boost", "Energy boost", 0.58 + min(0.25, max(0.0, lift)),
        f"Key move {ctx.out.track.key} -> {ctx.inn.track.key} is a {move} and {ctx.inn.track.title} hits harder: {how}.",
        ctx.inn.track, ctx.from_deck, ctx.to_deck, at, ctx.in_s(at), drop or 0.0, plan,
        {"key_move": move, "energy_lift": round(lift, 2)},
    )]


def double_drop(ctx: Context) -> list[Idea]:
    a, b = ctx.out.analysis, ctx.inn.analysis
    if not (a and b and a.drops and b.drops) or not _blendable(ctx) or ctx.key_score < 0.9:
        return []
    bar = ctx.out.bar_s
    drop_a = next((d for d in a.drops if d - 16 * bar > (ctx.pos + 4 if ctx.playing else 0)), None)
    drop_b = next((d for d in b.drops if d >= 16 * ctx.inn.bar_s), None)
    if drop_a is None or drop_b is None:
        return []
    go = drop_a - 16 * bar
    fa, fb = ctx.from_deck, ctx.to_deck
    plan = _load(ctx) + [
        {"tool": "set_tempo", "args": {"deck": fb, "sync": True, "sync_to": fa}},
        {"tool": "set_eq", "args": {"deck": fb, "low": 0}},
        {"tool": "set_volume", "args": {"target": fb, "level": 0, "fade_s": 0}},
        {"tool": "transport", "args": {"deck": fb, "action": "seek", "position_s": round(drop_b - 16 * ctx.inn.bar_s, 2)}},
        {"wait": {"deck": fa, "position_s": round(go, 2)}},
        {"tool": "transport", "args": {"deck": fb, "action": "play"}},
        {"tool": "set_volume", "args": {"target": fb, "level": 1, "fade_s": round(8 * bar, 2)}},
        {"wait": {"deck": fa, "beats": 64}},
        {"tool": "set_eq", "args": {"deck": fb, "low": 1}},
        {"tool": "set_eq", "args": {"deck": fa, "low": 0}},
        {"wait": {"deck": fa, "beats": 32}},
        {"tool": "set_volume", "args": {"target": fa, "level": 0, "fade_s": round(4 * bar, 2)}},
        {"wait": {"seconds": round(4 * bar, 2)}},
        {"tool": "transport", "args": {"deck": fa, "action": "pause"}},
        {"tool": "set_eq", "args": {"deck": fa, "low": 1}},
        {"tool": "set_volume", "args": {"target": fa, "level": 1, "fade_s": 0}},
    ]
    return [Idea(
        "double_drop", "Double drop", 0.88,
        f"Both tracks drop hard and sit in compatible keys: bring {ctx.inn.track.title} in 16 bars early so both drops land together, then swap the bass.",
        ctx.inn.track, fa, fb, go, ctx.in_s(go), drop_b - 16 * ctx.inn.bar_s, plan,
        {"outgoing_drop_s": round(drop_a, 2), "incoming_drop_s": round(drop_b, 2)},
    )]


def halftime_bridge(ctx: Context) -> list[Idea]:
    tm = ctx.tempo
    if tm is None or tm.multiplier == 1.0 or abs(tm.percent) > 6:
        return []
    at = ctx.next_phrase(ctx.pos + 8) if ctx.playing else None
    plan = _load(ctx) + [{"wait": {"deck": ctx.from_deck, "next_phrase": True}}, _transition(ctx, "bass_swap", 8, 0.0, sync=True)]
    rel = "half" if tm.multiplier == 2.0 else "double"
    return [Idea(
        "halftime_bridge", "Half-time bridge", 0.7 + 0.1 * ctx.key_score,
        f"{ctx.out_bpm:.0f} and {ctx.inn.track.bpm:.0f} BPM lock together at {rel} time: blend them for 8 bars and the groove switches feel without a tempo jump.",
        ctx.inn.track, ctx.from_deck, ctx.to_deck, at, ctx.in_s(at), 0.0, plan, {"multiplier": tm.multiplier},
    )]


def tempo_ride(ctx: Context) -> list[Idea]:
    tm = ctx.tempo
    if tm is None or tm.multiplier != 1.0 or not 6 < abs(tm.percent) <= 16:
        return []
    target = ctx.out_bpm + (ctx.inn.track.bpm - ctx.out_bpm) / 2  # each deck travels half the gap
    steps: list[dict[str, Any]] = []
    for i in range(1, 5):
        steps.append({"tool": "set_tempo", "args": {"deck": ctx.from_deck, "bpm": round(ctx.out_bpm + (target - ctx.out_bpm) * i / 4, 2)}})
        steps.append({"wait": {"deck": ctx.from_deck, "beats": 16}})
    at = ctx.next_phrase(ctx.pos + 8) if ctx.playing else None
    plan = _load(ctx) + [{"wait": {"deck": ctx.from_deck, "next_phrase": True}}, *steps, _transition(ctx, "crossfade", 8, 0.0, sync=True)]
    return [Idea(
        "tempo_ride", "Tempo ride", 0.55 + 0.1 * ctx.key_score,
        f"{abs(tm.percent):.0f}% apart is too far for a straight blend: ride {ctx.out.track.title} halfway to {target:.0f} BPM over 16 bars "
        "(nobody hears a slow change), then beatmatch the rest.",
        ctx.inn.track, ctx.from_deck, ctx.to_deck, at, ctx.in_s(at), 0.0, plan, {"ride_to_bpm": round(target, 1)},
        ["needs a pitch range of at least ±8% in Mixxx"],
    )]


def vocal_ride(ctx: Context) -> list[Idea]:
    if not (ctx.out.synced and _blendable(ctx) and ctx.key_score >= 0.9):
        return []
    intro = ctx.inn.intro_len()
    bar = ctx.out.bar_s
    if intro < 8 * ctx.inn.bar_s:
        return []
    spans = [s for s in vocal_spans(ctx.out.lyrics) if s[0] > (ctx.pos + 2 * bar + 3 if ctx.playing else 2 * bar)]
    if not spans:
        return []
    v_start, v_end = spans[0]
    ride = min(v_end - v_start, intro - 2 * ctx.inn.bar_s)
    if ride < 4 * bar:
        return []
    fa, fb = ctx.from_deck, ctx.to_deck
    go = v_start - 2 * bar
    ride_beats = max(8, int(ride / bar) * 4)
    plan = _load(ctx) + [
        {"tool": "set_tempo", "args": {"deck": fb, "sync": True, "sync_to": fa}},
        {"tool": "set_volume", "args": {"target": fb, "level": 0, "fade_s": 0}},
        {"tool": "transport", "args": {"deck": fb, "action": "seek", "position_s": 0}},
        {"wait": {"deck": fa, "position_s": round(go, 2)}},
        {"tool": "transport", "args": {"deck": fb, "action": "play"}},
        {"tool": "set_volume", "args": {"target": fb, "level": 1, "fade_s": round(2 * bar, 2)}},
        {"tool": "set_eq", "args": {"deck": fa, "low": 0, "high": 0.6, "fade_s": round(2 * bar, 2)}},
        {"wait": {"deck": fa, "beats": ride_beats}},
        {"tool": "set_volume", "args": {"target": fa, "level": 0, "fade_s": round(2 * bar, 2)}},
        {"wait": {"seconds": round(2 * bar, 2)}},
        {"tool": "transport", "args": {"deck": fa, "action": "pause"}},
        {"tool": "set_eq", "args": {"deck": fa, "low": 1, "mid": 1, "high": 1}},
        {"tool": "set_volume", "args": {"target": fa, "level": 1, "fade_s": 0}},
    ]
    line = next((ln.text for ln in ctx.out.lyrics.lines if abs(ln.time - v_start) < 0.5), "")
    return [Idea(
        "vocal_ride", "Vocal ride (live mashup)", 0.64 + 0.12 * ctx.key_score,
        f"{ctx.inn.track.title} has a {intro:.0f}s instrumental intro in a compatible key: kill the bass on {ctx.out.track.title} "
        f'and let its vocal ("{line}") ride over the new beat before fading it out.',
        ctx.inn.track, fa, fb, go, ctx.in_s(go), 0.0, plan, {"vocal_line": line, "ride_s": round(ride, 1)},
    )]


def loop_and_drop(ctx: Context) -> list[Idea]:
    if not ctx.out.synced:
        return []
    hooks = set(ctx.out.hook_lines())
    lines = [ln for ln in ctx.out.lyrics.lines if ln.text.strip() and _upcoming_ok(ctx, ln.time, lead=6)]
    if not lines:
        return []
    line = next((ln for ln in lines if normalize(ln.text) in hooks), lines[0])
    content = [t for t in re.findall(r"[A-Za-z0-9$']+", line.text) if normalize(t) not in STOPWORDS and len(normalize(t)) >= 3]
    if not content:
        return []
    word = normalize(content[-1])
    hits = [h for h in find_phrase(ctx.out.lyrics, word) if h.time >= line.time - 0.01 and _upcoming_ok(ctx, h.time)]
    if not hits:
        return []
    h = hits[0]
    start = max(0.0, (ctx.inn.first_vocal() or ctx.inn.bar_s) - ctx.inn.bar_s) if ctx.inn.synced else 0.0
    plan = _loop_then(ctx, word, h.occurrence, 1, [_transition(ctx, _cut_style(ctx), 0 if _blendable(ctx) else 1, start, sync=_blendable(ctx))])
    bonus = 0.08 if ctx.out.family in ("hiphop", "pop", "latin", "rnb") else 0.0
    return [Idea(
        "loop_and_drop", "Loop and drop", 0.52 + bonus + (0.05 if normalize(line.text) in hooks else 0),
        f'Loop "{word}" from the {"hook" if normalize(line.text) in hooks else "line"} "{line.text}" for a beat, let it stutter, then drop {ctx.inn.track.title} on the one.',
        ctx.inn.track, ctx.from_deck, ctx.to_deck, h.time, ctx.in_s(h.time), start, plan, {"word": word, "line": line.text},
    )]


def exits(ctx: Context) -> list[Idea]:
    """Clean exits when the records don't blend: echo out, spinback, brake."""
    tm = ctx.tempo
    gap = abs(tm.percent) if tm else 100.0
    genre_jump = ctx.out.family != ctx.inn.family
    if gap <= 6 and not genre_jump:
        return []
    at = ctx.next_phrase(ctx.pos + 8) if ctx.playing else None
    start = max(0.0, (ctx.inn.first_vocal() or 0.0) - ctx.inn.bar_s) if ctx.inn.synced else 0.0
    wait = [{"wait": {"deck": ctx.from_deck, "next_phrase": True}}]
    ideas = [Idea(
        "echo_out", "Echo out", 0.56 + (0.06 if gap > 12 else 0),
        f"{'Tempos are ' + format(gap, '.0f') + '% apart' if gap > 6 else 'Different genres'}: stutter {ctx.out.track.title} out with shrinking loop rolls and drop {ctx.inn.track.title} clean.",
        ctx.inn.track, ctx.from_deck, ctx.to_deck, at, ctx.in_s(at), start,
        _load(ctx) + wait + [_transition(ctx, "echo_out", 2, start, sync=False)], {"tempo_gap_percent": round(gap, 1)},
    )]
    if ctx.out.family in ("hiphop", "pop", "rock", "latin", "rnb", "other"):
        ideas.append(Idea(
            "spinback", "Spinback", 0.5 + (0.05 if genre_jump else 0),
            f"A spinback on {ctx.out.track.title} resets the ear, so the jump to {ctx.inn.track.genre or 'the next track'} at {ctx.inn.track.bpm:.0f} BPM lands as a statement.",
            ctx.inn.track, ctx.from_deck, ctx.to_deck, at, ctx.in_s(at), start,
            _load(ctx) + wait + [_transition(ctx, "spinback", 0, start, sync=False)], {},
        ))
    ideas.append(Idea(
        "brake", "Power-down brake", 0.44,
        f"Slow {ctx.out.track.title} to a halt like a turntable losing power, then kick {ctx.inn.track.title} in: dramatic, works across any tempo.",
        ctx.inn.track, ctx.from_deck, ctx.to_deck, at, ctx.in_s(at), start,
        _load(ctx) + wait + [_transition(ctx, "brake", 0, start, sync=False)], {},
    ))
    return ideas


LYRIC_TECHNIQUES = (title_drop, name_drop, word_handoff, remix_flip, loop_and_drop, vocal_ride)
MUSICAL_TECHNIQUES = (harmonic_blend, energy_boost, double_drop, halftime_bridge, tempo_ride, exits)
MOMENT_TECHNIQUES = (title_drop, name_drop, word_handoff, remix_flip)


def rank(ideas: list[Idea]) -> list[Idea]:
    for i in ideas:
        if i.in_s is not None:
            if i.in_s < 6:
                i.score *= 0.6
                i.warnings.append("very soon: only works if the next track is already loaded")
            elif i.in_s <= 90:
                i.score *= 1.06
            elif i.in_s > 240:
                i.score *= 0.92
        i.score = round(min(i.score, 1.0), 3)
    return sorted(ideas, key=lambda i: -i.score)


def ideas_for(ctx: Context, techniques=LYRIC_TECHNIQUES + MUSICAL_TECHNIQUES) -> list[Idea]:
    out: list[Idea] = []
    for tech in techniques:
        out.extend(tech(ctx))
    if ctx.out.synced and ctx.inn.synced and _blendable(ctx):
        # A long blend over two vocals is the classic clash: flag it on blend ideas.
        for i in out:
            if i.technique == "harmonic_blend" and ctx.inn.intro_len() < 8 * ctx.inn.bar_s:
                i.warnings.append("both songs have vocals in the overlap")
    return rank(out)


# ------------------------------------------------------------------ service


class Recommender:
    """Builds profiles (lyrics + optional audio analysis) and runs the techniques."""

    def __init__(self, dj: DJ, analyzer: Analyzer | None = None, max_ideas: int = 200) -> None:
        self.dj = dj
        self.analyzer = analyzer
        self.ideas: dict[str, Idea] = {}
        self._ids = itertools.count(1)
        self._max = max_ideas

    async def profile(self, track: Track, fetch: bool = True, analyse: bool = True) -> SongProfile:
        lyrics = await self.dj.lyrics.get(track) if fetch else self.dj.lyrics.cached(track)
        analysis = None
        if analyse and self.analyzer and track.location:
            analysis = await asyncio.to_thread(self.analyzer.analyse, track.location, track.bpm, str(track.id))
        return build_profile(track, lyrics, analysis)

    def _context(self, out: SongProfile, inn: SongProfile, from_deck: int | None, to_deck: int | None) -> Context:
        pos, playing, bpm, speed, first_beat = 0.0, False, out.track.bpm, 1.0, 0.0
        loaded = False
        if from_deck is not None:
            b = self.dj.backend
            d = b.state.deck(from_deck)
            now = b.now()
            playing = d.playing
            pos = d.position(now)
            bpm = d.v("bpm") or out.track.bpm
            speed = d.speed
            anchor = d.beat_anchor()
            if anchor:
                first_beat = anchor[0] % anchor[1]
        if to_deck is not None:
            t = self.dj.deck_track(to_deck)
            loaded = t is not None and t.id == inn.track.id
        return Context(out, inn, from_deck, to_deck, pos, playing, bpm, speed, first_beat, loaded)

    def _store(self, ideas: list[Idea]) -> list[Idea]:
        for i in ideas:
            i.id = f"idea-{next(self._ids)}"
            self.ideas[i.id] = i
        while len(self.ideas) > self._max:
            self.ideas.pop(next(iter(self.ideas)))
        return ideas

    async def for_pair(self, from_deck: int, incoming: Track, to_deck: int | None = None, limit: int = 6) -> list[Idea]:
        current = self.dj.deck_track(from_deck)
        if current is None:
            raise ValueError(f"no known track on deck {from_deck}")
        to_deck = to_deck or self.dj.other_deck(from_deck)
        out, inn = await asyncio.gather(self.profile(current), self.profile(incoming))
        ideas = ideas_for(self._context(out, inn, from_deck, to_deck))
        return self._store(ideas[:limit])

    async def next_moves(self, from_deck: int, limit: int = 6, candidates: int = 8, genre: str | None = None) -> list[Idea]:
        """Best transition ideas into the most promising library tracks."""
        current = self.dj.deck_track(from_deck)
        if current is None:
            raise ValueError(f"no known track on deck {from_deck}")
        to_deck = self.dj.other_deck(from_deck)
        picks = [self.dj.library.get(s["id"]) for s in self.dj.suggest_next(from_deck, limit=candidates, genre=genre)]
        # Lyric links are worth finding even for tracks the tempo/key ranking missed.
        out = await self.profile(current)
        if out.lyrics:
            words = {t for ln in out.lyrics.lines for t in _tokens(ln.text) if t not in STOPWORDS and len(t) >= 4}
            for t in self.dj.library.all():
                if t.id != current.id and t not in picks and set(_tokens(base_title(t.title))) & words:
                    picks.append(t)
        profiles = await asyncio.gather(*(self.profile(t) for t in picks if t is not None))
        ideas: list[Idea] = []
        for inn in profiles:
            ideas.extend(ideas_for(self._context(out, inn, from_deck, to_deck)))
        ideas = rank(ideas)
        # Keep the list varied: at most two ideas per incoming track.
        per_track: Counter[int] = Counter()
        chosen = []
        for i in ideas:
            if per_track[i.incoming.id] < 2:
                chosen.append(i)
                per_track[i.incoming.id] += 1
            if len(chosen) >= limit:
                break
        return self._store(chosen)

    async def opportunities(self, deck: int, horizon: float = 90.0, fetch: bool = False) -> list[Idea]:
        """Lyric moments coming up on ``deck`` that set up a great transition right now."""
        current = self.dj.deck_track(deck)
        d = self.dj.deck_state(deck)
        if current is None or not d.playing:
            return []
        out = await self.profile(current)
        if not out.synced:
            return []
        to_deck = self.dj.other_deck(deck)
        found: list[Idea] = []
        for t in self.dj.library.all():
            if t.id == current.id:
                continue
            inn = await self.profile(t, fetch=fetch, analyse=False)
            ctx = self._context(out, inn, deck, to_deck)
            for tech in MOMENT_TECHNIQUES:
                for idea in tech(ctx):
                    if idea.in_s is not None and 4 <= idea.in_s <= horizon:
                        found.append(idea)
        # One suggestion per incoming track: a title drop and a handoff on the same
        # line are the same opportunity, and one strong idea per record is plenty.
        best: dict[int, Idea] = {}
        for i in rank(found):
            best.setdefault(i.incoming.id, i)
        return self._store(list(best.values())[:8])

    async def prefetch(self, deck: int, n: int = 8) -> None:
        """Fetch lyrics for the likeliest next tracks so wordplay can be spotted live."""
        try:
            picks = [self.dj.library.get(s["id"]) for s in self.dj.suggest_next(deck, limit=n)]
        except Exception:
            return
        await asyncio.gather(*(self.dj.lyrics.get(t) for t in picks if t is not None), return_exceptions=True)


class OpportunityWatcher:
    """Background loop that keeps a fresh list of in-the-moment transition opportunities."""

    def __init__(self, rec: Recommender, interval: float = 4.0, horizon: float = 90.0) -> None:
        self.rec = rec
        self.interval = interval
        self.horizon = horizon
        self.current: list[Idea] = []
        self.listeners: list[Any] = []
        self._seen: set[tuple] = set()
        self._prefetched: set[tuple[int, int]] = set()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def scan(self) -> list[Idea]:
        dj = self.rec.dj
        ideas: list[Idea] = []
        for n in range(1, dj.num_decks + 1):
            d = dj.backend.state.deck(n)
            track = dj.deck_track(n)
            if not d.playing or track is None:
                continue
            if (n, track.id) not in self._prefetched:
                self._prefetched.add((n, track.id))
                asyncio.create_task(self.rec.prefetch(n))
            ideas.extend(await self.rec.opportunities(n, self.horizon))
        self.current = rank(ideas)[:6]
        for idea in self.current:
            if idea.moment() not in self._seen:
                self._seen.add(idea.moment())
                for fn in self.listeners:
                    try:
                        fn(idea)
                    except Exception:  # a listener must never stop the watcher
                        pass
        return self.current

    async def _run(self) -> None:
        while True:
            try:
                await self.scan()
            except Exception:  # keep watching even if one scan fails (e.g. Mixxx restarting)
                pass
            await self.rec.dj.backend.sleep(self.interval)

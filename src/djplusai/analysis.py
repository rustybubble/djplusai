"""Listen to the audio itself: energy per bar, intro/outro length, drops and breakdowns.

Optional: needs ``numpy`` (``pip install 'djplusai[analysis]'``), plus ``ffmpeg`` on the
PATH for anything that isn't a WAV file. Results are cached as JSON per track.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SAMPLE_RATE = 22050


@dataclass
class TrackAnalysis:
    bar_seconds: float
    first_beat: float
    bar_energy: list[float]  # RMS per bar, normalised so the loudest bar is 1.0
    energy: float  # 0-1 overall intensity (median bar energy)
    intro_end: float  # seconds: where the low-energy intro ends
    outro_start: float  # seconds: where the low-energy outro begins
    drops: list[float] = field(default_factory=list)  # seconds where energy jumps up
    breakdowns: list[float] = field(default_factory=list)  # seconds where energy falls away

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("bar_energy")
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()} | {
            "drops": [round(x, 2) for x in self.drops],
            "breakdowns": [round(x, 2) for x in self.breakdowns],
        }


def available() -> bool:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def decode(path: str) -> Any:
    """Mono float32 samples at SAMPLE_RATE."""
    import numpy as np

    if path.lower().endswith(".wav"):
        with wave.open(path, "rb") as w:
            rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        dtype = {1: np.uint8, 2: np.int16, 4: np.int32}[width]
        x = np.frombuffer(raw, dtype=dtype).astype(np.float32)
        if width == 1:
            x = (x - 128.0) / 128.0
        else:
            x /= float(2 ** (8 * width - 1))
        x = x.reshape(-1, channels).mean(axis=1)
        if rate != SAMPLE_RATE:
            idx = np.arange(0, len(x), rate / SAMPLE_RATE)
            x = np.interp(idx, np.arange(len(x)), x).astype(np.float32)
        return x
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is needed to analyse non-WAV audio")
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"],
        check=True,
        capture_output=True,
        timeout=180,
    ).stdout
    return np.frombuffer(out, dtype=np.float32)


def analyse_samples(x: Any, bpm: float, first_beat: float = 0.0) -> TrackAnalysis:
    import numpy as np

    bar = 4 * 60.0 / bpm
    n = int(bar * SAMPLE_RATE)
    start = int(max(0.0, first_beat) * SAMPLE_RATE)
    bars = []
    for i in range(start, len(x) - n // 2, n):
        seg = x[i : i + n]
        bars.append(float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0)
    e = np.array(bars) if bars else np.zeros(1)
    e = e / (e.max() or 1.0)
    median = float(np.median(e))
    low = 0.6 * median
    intro = 0
    while intro < len(e) and e[intro] < low:
        intro += 1
    outro = len(e)
    while outro > intro and e[outro - 1] < low:
        outro -= 1
    drops, breakdowns = [], []
    for i in range(4, len(e)):
        before = float(e[i - 4 : i].mean())
        if e[i] > 1.5 * before and e[i] >= median and before > 0:
            if not drops or (i * bar + first_beat) - drops[-1] > 8 * bar:
                drops.append(i * bar + first_beat)
        if e[i] < 0.55 * before and before >= median:
            if not breakdowns or (i * bar + first_beat) - breakdowns[-1] > 8 * bar:
                breakdowns.append(i * bar + first_beat)
    return TrackAnalysis(
        bar_seconds=bar,
        first_beat=first_beat,
        bar_energy=[round(float(v), 4) for v in e],
        energy=median,
        intro_end=intro * bar + first_beat,
        outro_start=outro * bar + first_beat,
        drops=drops,
        breakdowns=breakdowns,
    )


class Analyzer:
    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir
        self._mem: dict[str, TrackAnalysis | None] = {}

    def analyse(self, path: str, bpm: float, key: str | None = None) -> TrackAnalysis | None:
        """Analyse (or load the cached analysis of) an audio file. None when impossible."""
        if not path or bpm <= 0 or not available():
            return None
        key = key or f"{Path(path).stem}-{bpm:.2f}"
        if key in self._mem:
            return self._mem[key]
        cache = self.cache_dir / f"{key}.json" if self.cache_dir else None
        result = None
        if cache and cache.is_file():
            try:
                result = TrackAnalysis(**json.loads(cache.read_text()))
            except (OSError, ValueError, TypeError):
                result = None
        if result is None:
            try:
                result = analyse_samples(decode(path), bpm)
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, KeyError) as exc:
                log.info("audio analysis skipped for %s: %s", path, exc)
                result = None
            if result and cache:
                try:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps(asdict(result)))
                except OSError:
                    pass
        self._mem[key] = result
        return result

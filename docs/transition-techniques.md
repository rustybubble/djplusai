# Transition techniques in the recommendation engine

`recommend_transitions` and the live opportunity watcher (`get_opportunities`) are built from the
techniques below. Each one looks at the outgoing and the incoming song and, when it fits, returns
an idea with:

- a score,
- the reason,
- the moment it happens (track time, and seconds from now while the song plays),
- a ready-to-run plan (`run_idea`).

Code: `src/djplusai/recommend.py`.

## Lyric-driven techniques (wordplay)

| Technique | When it fires | What it does |
|---|---|---|
| **Title drop** | The playing song is about to sing another track's title. Titles that are only filler words, like "Baby", are ignored. | Loops the title for 2 beats, then cuts into the new song on its own hook if the song sings its title, otherwise on its first verse. |
| **Wordplay handoff** | Both songs sing the same distinctive word or phrase. Phrases are compared as 3-, 2- and 1-word n-grams, with filler words ("yeah", "baby", "the"...) and plural forms normalised away. A phrase the incoming song repeats is weighted higher, since it is probably the hook. | Loops the phrase out of the outgoing song, and the incoming song starts right before it sings the same phrase. |
| **Name drop** | The lyrics mention the incoming track's artist. | Cuts in right after the name. |
| **Remix flip** | Original and remix, edit or VIP of the same song. | Switches versions mid-vocal on a lyric line both versions share. |
| **Loop and drop** | Any synced song. It prefers the hook, meaning a line sung at least twice. | Loops a hook word for a beat, then drops the next record on the one. |
| **Vocal ride** (live mashup) | The incoming song has an instrumental intro of 8 bars or more, the keys are compatible and the tempos are within 6%. | Kills the bass on the outgoing song and lets its vocal ride over the new beat, then fades it out. |

## Musical techniques

| Technique | When it fires | What it does |
|---|---|---|
| **Harmonic blend** | Same or neighbouring Camelot key, and tempos within 6%. | A bass swap (house, techno, DnB, afro) or crossfade in the outgoing song's vocal-free outro, aligned to a 16-beat phrase. It warns when both vocals would overlap. |
| **Energy boost** | Key move of +1, +2, +7 ("jaws") or a relative major/minor switch, into a higher-energy track. | Cuts into the incoming drop if the audio analysis found one, otherwise does an 8-bar filter sweep. It lands on the next phrase. |
| **Double drop** | Both tracks have analysed drops, compatible keys and close tempos. | Starts the incoming track 16 bars early so both drops land together, then swaps the bass. |
| **Half-time bridge** | Tempos match at half or double time (70/140, 87/174). | An 8-bar blend at the shared pulse. |
| **Tempo ride** | 6-16% apart. | Moves the outgoing tempo halfway in 4 steps over 16 bars, then beatmatches the rest. |
| **Echo out / spinback / brake** | Big tempo gap or genre jump. | Clean exits. Spinback and brake use Mixxx's own turntable effects. |

Scores go up for moments 6-90 seconds away. Moments less than 6 seconds away are marked as only
workable if the next track is already loaded.

## What the engine "listens" to

- **Lyrics**, from sidecar `.lrc` files, `--lyrics-dir` or LRCLIB. When no synced version exists,
  LRCLIB's plain lyrics are used: they still work for wordplay, but they have no timing.
  - Synced lyrics also give *vocal-free windows*, meaning the intros, breaks and outros where a
    long blend won't cause a vocal clash.
- **Audio** (optional, `pip install "djplusai[analysis]"` plus ffmpeg for non-WAV files):
  - energy per bar,
  - where the intro ends and the outro starts,
  - drops and breakdowns.
- **Metadata:** BPM, Camelot key, genre family and energy.

## Research sources

Techniques and rules of thumb were taken from DJ education material, including:

- Wordplay transitions: a shared lyric bridges unrelated tracks, even across a 30 BPM change
  ([SampleFocus](https://samplefocus.com/blog/advanced-transition-techniques-dj/),
  [PulseDJ open-format guide](https://blog.pulsedj.com/open-format-djing),
  [Heavy Hits wordplay edits](https://heavyhits.com/playlist/wordplay-edits-3/)).
- Energy boosts on the Camelot wheel (+2, +7 "jaws") and phrase mixing at 16 and 32 bars
  ([Vibes: Camelot wheel](https://vibesdj.io/learn/techniques/camelot-wheel),
  [Vibes: phrase mixing](https://vibesdj.io/learn/techniques/phrase-mixing),
  [Mixed In Key](https://mixedinkey.com/harmonic-mixing-guide/)).
- Half-time and double-time bridges, echo cuts, spinbacks and acapella swaps across BPM gaps
  ([Mixgraph](https://www.mixgraph.io/learn/dj-mashup-transitions-that-work-live),
  [DJ.Studio tempo-change techniques](https://dj.studio/blog/dj-tempo-change-techniques)).
- Gradual tempo moves of 1-2 BPM every 8 bars
  ([Vibes: tempo changes](https://vibesdj.io/learn/techniques/auto-bpm-transition)).
- Avoiding vocal clashes by mixing in instrumental sections and keeping any vocal overlap to about
  4 bars ([Mixgraph vocal mixing guide](https://www.mixgraph.io/learn/vocal-mixing-guide)).
- Sample-source transitions, for example playing the original that a track samples
  ([DJ TechTools](https://djtechtools.com/2015/04/21/7-ways-to-use-serato-flip/)).

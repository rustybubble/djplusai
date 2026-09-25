# djplusai: let AI agents DJ with Mixxx

Tell an AI what you want to hear, and it drives [Mixxx](https://mixxx.org), the open-source DJ
app:

> "Pull up Drake and Future's *Life Is Good*. When Drake says *rollie*, loop that part, then
> transition into *Rolex* by Ayo & Teo."
>
> "Turn it up a bit." · "Mix into some deep house over 16 bars." · "Kill the bass on deck 2."
> · "Echo out and drop the next track."

djplusai works with **stock Mixxx**, with no fork or special build. It has three parts:

- a **Mixxx controller mapping** (JavaScript) that executes commands and streams deck state
  back, sent as JSON over MIDI SysEx on a virtual port;
- a **DJ engine** (Python) with library search, time-synced lyrics, beat-grid math, transitions
  and a real-time scheduler for "when X happens, do Y";
- two front ends that share one tool set: an **MCP server** for Claude Desktop, Claude Code,
  Cursor and other MCP clients, and a **Claude-powered terminal chat**.

It is genre-agnostic. Transitions and track suggestions use tempo (including half and double
time), Camelot key compatibility and genre, so they work for house, techno, DnB, hip-hop, pop,
reggaeton and more.

Before building this I surveyed existing projects. See [docs/prior-art.md](docs/prior-art.md)
for the comparison and for what stock Mixxx does and does not allow.

## How it works

```
 you ──► Claude (MCP client or `djplusai chat`)
              │ tool calls: load_track, loop, transition, run_mix_plan, ...
              ▼
        djplusai (Python) ── library search (mixxxdb.sqlite, read-only)
              │            ── lyrics (.lrc sidecars / LRCLIB / optional Whisper)
              │            ── scheduler (waits on lyrics, beats, loops, time left)
              │ JSON in SysEx frames over a virtual MIDI port
              ▼
        DJPlusAI.js mapping inside Mixxx ── engine.setValue / ramps / loops
              │ deck state pushed back 20x per second
              ▼
            Mixxx audio engine
```

**Lyric-cued loops are sample-accurate.** djplusai finds when the word is sung and snaps that
time to the track's beat grid. It then *arms* a loop at exact sample positions ahead of the
playhead, and Mixxx engages it when the playhead arrives. Nothing depends on MIDI latency or on
how fast the AI reacts.

**"Then do X" is a background mix plan.** The agent sends a list of steps (tool calls, and waits
such as "until the loop engages" or "for 8 beats"). djplusai runs the steps in real time while
you keep talking.

## Try it without Mixxx

```bash
pip install -e ".[all]"
djplusai demo                  # scripted lyric loop + echo-out transition in the simulator
djplusai chat --sim            # talk to the simulated decks (needs an Anthropic API key)
```

## Setup with Mixxx

1. **Install.** Use Python 3.10+ and Mixxx 2.4 or newer.
   ```bash
   pip install -e ".[all]"
   djplusai install-mapping      # copies DJPlusAI.midi.xml + DJPlusAI.js into Mixxx's controllers folder
   ```
2. **Create the MIDI port.**
   - **Linux and macOS:** djplusai creates a virtual port called `DJPlusAI` automatically. Mixxx
     only scans MIDI devices when it starts, so start djplusai before Mixxx. The alternative is a
     persistent port: the IAC Driver bus on macOS, or `snd-virmidi` on Linux, used with
     `--port <name>`.
   - **Windows:** install [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html) and
     create a port named `DJPlusAI`.
3. **Enable the mapping in Mixxx.** Go to *Preferences → Controllers → DJPlusAI*, choose the
   **DJPlusAI Bridge** mapping, and tick **Enabled**.
4. **Allow track loading.** Stock Mixxx can only load the track selected in its library, so
   djplusai types a precise search into Mixxx's search box and then verifies what loaded. This
   needs keyboard automation: `xdotool` on Linux/X11, `osascript` on macOS (grant Accessibility
   permission), or `pip install "djplusai[keyboard]"` on Windows.
5. **Check the setup.** Run `djplusai doctor`.

### Connect an AI agent

**Claude Code:**
```bash
claude mcp add djplusai -- djplusai mcp
```

**Claude Desktop** (`claude_desktop_config.json`):
```json
{ "mcpServers": { "djplusai": { "command": "djplusai", "args": ["mcp"] } } }
```

**Terminal chat** (uses the Anthropic API; set `ANTHROPIC_API_KEY` or run `ant auth login`):
```bash
djplusai chat
```
The chat uses `claude-opus-5` by default. Change it with `--model` or `DJPLUSAI_MODEL`. Effort
defaults to `medium` because DJ requests are latency-sensitive; change it with `--effort` or
`DJPLUSAI_EFFORT`.

**Scripts or other agents** can call any tool directly:
```bash
djplusai call search_library '{"query": "life is good"}'
djplusai call set_volume '{"target": "master", "change": 0.1}'
```

## Tools

| Tool | What it does |
|---|---|
| `get_status` | Decks: track, position, BPM, volume, EQ, filter, loop, sync; running jobs |
| `search_library`, `load_track`, `suggest_next_tracks` | Fuzzy library search, verified loading, harmonic and tempo-aware suggestions |
| `transport` | Play, pause, seek, beatjump, jump to a lyric, hotcues |
| `set_volume`, `set_eq`, `set_filter`, `crossfader` | Mixer, with fades and relative changes ("a bit louder") |
| `set_tempo` | BPM or percent change, sync to another deck, keylock |
| `loop` | Loop now, at a time, or **at a lyric**; release |
| `find_lyric` | Every time a word or phrase is sung, with timestamps |
| `recommend_transition`, `transition` | Crossfade, bass swap, filter sweep, echo out, cut; beat-aligned, background by default |
| `run_mix_plan`, `list_jobs`, `cancel_job` | Timed sequences with waits on lyrics, beats, loop engagement, position, time left |
| `raw_control` | Escape hatch to any [Mixxx control](https://manual.mixxx.org/latest/en/chapters/appendix/mixxx_controls) |

The example at the top turns into:

```json
load_track {"deck": 1, "query": "Drake Future Life Is Good"}
load_track {"deck": 2, "query": "Ayo Teo Rolex"}
transport  {"deck": 1, "action": "play"}
run_mix_plan {"steps": [
  {"tool": "loop", "args": {"deck": 1, "action": "at_lyric", "phrase": "rollie", "beats": 4}},
  {"wait": {"deck": 1, "loop_active": true}},
  {"wait": {"deck": 1, "beats": 8}},
  {"tool": "transition", "args": {"from_deck": 1, "to_deck": 2, "style": "echo_out", "bars": 2}}
]}
```

## Lyrics and timing accuracy

djplusai looks for time-synced lyrics in this order:

1. `song.lrc` next to `song.mp3`
2. `--lyrics-dir/Artist - Title.lrc`
3. [LRCLIB](https://lrclib.net), a free database that needs no API key and is matched by artist,
   title and duration

Line-level LRC puts a word within about ±0.5 s. djplusai interpolates inside the line and then
snaps to the beat, which is usually enough to catch the right bar. Enhanced LRC with per-word
tags is used when available. Pass `--whisper` (or set `DJPLUSAI_WHISPER=1`) with
`pip install "djplusai[align]"` to transcribe the audio around the estimate with faster-whisper
for word-level timing.

## Limitations

- **Track loading uses the library search box.** Mixxx has no "load this file" control, so this
  step uses keyboard automation. Mixxx is briefly brought to the front, and the search does not
  work on pure Wayland sessions without an X11 or Xwayland window. If typing is unavailable,
  djplusai reports which track to load by hand.
- **Only tracks in your Mixxx library.** djplusai does not download music.
- **Lyrics can't tell who is singing.** In "when Drake says…", the lyric search finds the word
  itself, not the performer.
- **Transitions use each deck's volume fader.** The crossfader is left alone unless you ask.
- **The EQ and quick-effect groups follow Mixxx's default rack names**
  (`[EqualizerRack1_[ChannelN]_Effect1]`, `[QuickEffectRack1_[ChannelN]]`).

## Development

```bash
pip install -e ".[dev,mcp,agent,midi]"
pytest
```

The test suite covers the following:

- It runs the real `DJPlusAI.js` under Node against a fake Mixxx engine.
- It wires the Python MIDI backend to that script through pipes.
- It exercises every transition and a full lyric-loop mix plan against the simulator.
- It drives the MCP server with an in-process client.
- It checks the Claude agent loop with a scripted client.

Layout: `src/djplusai/mixxx_mapping/` (Mixxx side), `backends/` (`midi.py` for real Mixxx,
`sim.py` for the simulator), `controller.py` (DJ operations), `transitions.py`, `jobs.py` (mix
plans), `lyrics.py`, `library.py`, `music.py`, `tools.py` (tool schemas), `mcp_server.py`,
`agent.py`, `cli.py`.

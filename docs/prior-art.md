# Prior art: AI control of Mixxx (surveyed September 2026)

Before building djplusai I looked for projects already doing this. Summary of what exists and
what each one leaves open:

| Project | How it reaches Mixxx | Stock Mixxx? | Loads specific tracks | Lyric cues | Notes |
|---|---|---|---|---|---|
| [sandraschi/mixx-dj-mcp](https://github.com/sandraschi/mixx-dj-mcp) (MIT; fork: [alexyyyander/mixx-dj-mcp](https://github.com/alexyyyander/mixx-dj-mcp)) | OSC (UDP 11118/11119) | Unclear: its README says to enable OSC in Mixxx preferences, but mainline Mixxx has no OSC server (see below) | Yes, via its library tool | No | Biggest feature set (40-80 MCP operations, Demucs stems, video decks via the "mixxxxx" fork, web UI). |
| [cloudygetty-ai/mixxx-mcp](https://github.com/cloudygetty-ai/mixxx-mcp) (MIT) | MIDI CC into a JS mapping; state back over OSC/UDP from the script | Yes | Not addressed | No | Small (2 commits). A CC value only carries 7 bits, which limits precision for positions and loop points. |
| [alexyyyander/mixxx-api-bridge](https://github.com/alexyyyander/mixxx-api-bridge) (MIT) | HTTP API -> MIDI SysEx -> JS mapping, with ACK and feedback frames | Yes | Not addressed | No | Closest design to djplusai's transport layer; checked against Mixxx 2.5.6. No agent layer. |
| [VeltriaAI/dj-treta-being](https://github.com/VeltriaAI/dj-treta-being) ("DJClaw", MIT) | HTTP API on port 7778 added to a **forked** Mixxx (`treta` branch) | No | Yes | No (uses Gemini audio analysis for structure) | Autonomous multi-agent DJ with 5 transition types; many hours of unattended sets. |
| [niblarto/AI_DJ](https://github.com/niblarto/AI_DJ) | Writes M3U playlists for Mixxx | n/a | n/a | No | Natural language to setlist, no live control. |
| [kckDeepak/AI-DJ-Mixing-System](https://github.com/kckDeepak/AI-DJ-Mixing-System) | None (renders an MP3 offline) | n/a | n/a | No | Offline mix generation. |

## What stock Mixxx offers for remote control

Checked against the Mixxx source (`main`, commit `bcfb795`, 2026-09-25):

- **No OSC server.** OSC support has been requested for years
  ([mixxxdj/mixxx#5082](https://github.com/mixxxdj/mixxx/issues/5082)) but is not in mainline.
- **Controller scripts** (JavaScript, `QJSEngine`) can read and write every control object
  (`engine.getValue/setValue`), run timers, and send and receive **MIDI SysEx**
  (`midi.sendSysexMsg`, `<prefix>.incomingData`). Inbound SysEx is limited to 1024 bytes
  (`MIXXX_SYSEX_BUFFER_LEN` in `portmidicontroller.h`).
- Newer builds expose loaded-track metadata to scripts through `engine.getPlayer(group)`.
- **There is no control for "load file X into deck N".** Scripts can only load the track
  *selected in the library view* (`[ChannelN],LoadSelectedTrack`), and there is no control that
  sets the search text. Library navigation controls exist (`[Library],focused_widget`,
  `MoveDown`, ...).
- Loops can be placed at exact sample positions (`loop_start_position` / `loop_end_position`),
  and `reloop_toggle` on a loop that is still ahead of the playhead arms it without jumping. The
  engine then engages it sample-accurately when the playhead arrives.

## Gaps djplusai fills

1. **Works on a stock Mixxx install.** No fork and no OSC build: JSON over SysEx through a
   bundled controller mapping (the same approach as mixxx-api-bridge).
2. **Loads the exact track you asked for.** Fuzzy search runs against Mixxx's own library
   database. The track is then loaded by driving Mixxx's library search, and djplusai checks the
   title, artist, duration and BPM that come back, trying further results if the first is wrong.
3. **Lyric-cued actions.** Time-synced lyrics (sidecar `.lrc` files, or LRCLIB), with optional
   faster-whisper refinement, let you say "when he says *rollie*, loop it". The loop points are
   computed ahead of time and snapped to the beat grid, then the engine engages them. Nothing
   depends on MIDI latency or the LLM's reaction time.
4. **Timed mix plans.** A small declarative plan (tool calls plus waits on lyrics, beats, loop
   engagement or time remaining) runs in the background. The agent describes what should happen,
   and djplusai handles the timing.
5. **Genre-aware transitions.** Crossfade, bass swap, filter sweep, echo out and cut, chosen by
   tempo compatibility (including half and double time), Camelot key distance and genre.
6. **One tool set, two front ends.** An MCP server (Claude Desktop, Claude Code, Cursor, ...) and a
   Claude-powered terminal chat, plus a simulator so all of it can be tried without Mixxx.

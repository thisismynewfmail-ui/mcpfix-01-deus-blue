# CHATSGI / TERMINAL

```
┌──────────────────────────────────────────────┐
│  Section 9  ·  rev. 2.0                      │
│  Stand Alone Complex · Maintenance Terminal  │
└──────────────────────────────────────────────┘
```

A local maintenance terminal for an OpenAI-compatible LLM endpoint with full
**MCP (Model Context Protocol)** support and a Ghost in the Shell: S.A.C.
(2002) themed UI. Python backend spawns MCP servers as stdio subprocesses
(the Claude-Desktop pattern); a single-page browser UI talks to it over HTTP,
with a Server-Sent-Events bus that keeps multiple browser instances mirrored
in real time across the LAN.

## ◢ Files

| file                | purpose                                              |
|---------------------|------------------------------------------------------|
| `start.py`          | HTTP server + MCP stdio bridge + SSE event bus       |
| `index.html`        | UI, served by `start.py` at `/`                      |
| `tools.json`        | MCP server config (Claude-Desktop format)            |
| `browser_mcp_server.py` | local, self-hosted Playwright browser MCP server (stdio) |
| `browser_setup.py`  | one-shot installer for the local browser engine (Playwright + Chromium) |
| `settings.json`     | persisted UI settings (auto-created)                 |
| `data/`             | per-session chat files (one `.json` per chat) + `index.json` |
| `voices/`           | Piper TTS voice models (`<key>.onnx` + `.onnx.json`) + `previews/` |
| `chats.json`        | legacy bundled history — migrated into `data/` on boot |
| `conversation.json` | legacy single-chat log — migrated into `data/`       |
| `requirements.txt`  | stdlib-only core; lists optional `piper-tts` for neural speech |

## ◢ Run

```bash
python start.py
```

Defaults bind to `127.0.0.1:8765`. Toggle **§ 7 NETWORK → LAN VISIBILITY** in
the UI to bind `0.0.0.0` on the next launch, or override directly:

```bash
python start.py --host 0.0.0.0 --port 9000   # custom bind / LAN visibility
python start.py --no-mcp                      # don't auto-spawn MCP servers
```

Requires Python ≥ 3.10. No `pip install` needed for the core — stdlib only.

**Optional — neural speech:** to use the Piper TTS voice engine (Settings →
**§ 7 SPEECH**) install Piper and drop a voice into `voices/`:

```bash
pip install piper-tts
# then add e.g. en_US-lessac-medium.onnx + .onnx.json to voices/
# (catalogue: https://huggingface.co/rhasspy/piper-voices)
```

Without it, speech falls back to the built-in villager-talk blip SFX.

**Optional — local browser tool:** the in-terminal **BROWSER** (Settings → **§ 6
BROWSER**) drives a real, headed Chromium *locally* via Playwright — no npx, no
Docker, no external Playwright server. Install the engine once (the UI's
**INSTALL / REPAIR ENGINE** button does this for you, and the backend also
auto-installs on first launch when the `browser` server is enabled):

```bash
python browser_setup.py          # installs playwright + Chromium into ./.browser/
```

Everything (engine binaries + a persistent login profile + screenshots) lives in
a project-local `./.browser/` directory. Cross-platform (Windows + Linux). Power
it on/off with the **browser** switch in **§ 2 MCP / NODES**.

## ◢ Features

- **Multi-chat sessions** — left sidebar lists every chat; each session lives
  in its own file under `data/`. Consolidated `▾ MENU` button (New / Rename /
  Duplicate / Copy / Export / Delete the active chat) and per-row `⋮` menu for
  the same actions on any other chat. Inline rename (click ▾ MENU → RENAME).
- **Collapsible side panels** — toggle the left chat list and the right
  options/MCP/telemetry panel independently using the edge buttons on the
  main pane. Collapsed state persists across reloads.
- **Cross-instance sync** — every browser/window subscribes to a Server-Sent
  Events stream. Send a message on one monitor and it appears on every other
  connected viewer immediately. Settings, chat list, active selection, and
  MCP status all mirror. Spoken replies (SFX blips or Piper neural speech) are
  broadcast too, so the audio response plays through every synced window — not
  just the live one that ran the inference.
- **Context cropping (two methods)** — the full conversation is *always* kept
  in the chat interface; nothing is ever deleted from the transcript. Only the
  model's view is trimmed when token usage approaches the budget. Pick the
  method in **Settings → § 3 CONTEXT CROPPING → Cropping Method**; the relevant
  controls appear for whichever is selected:
    - **Rolling Block** *(cut %)* — keeps a fixed recent block of `Context
      Depth` pairs and drops the oldest *middle* turns once usage crosses the
      cutoff (`context × Roll Threshold%`). The model view moves in blocks.
    - **Standard Culling** *(fall-off)* — a continuous rolling buffer with no
      fixed recent block: the oldest non-anchor turn peels off one at a time the
      moment usage reaches the **Fall-off Location** (`context × fall-off%`), so
      history slides off naturally as it ages.
  Both methods always preserve the system prompt and the **first two**
  back-and-forth exchanges (the original task), and both drop tool-call /
  tool-result rounds *atomically* — a whole tool block falls off together, so
  the trace never orphans a `tool_call_id` or leaves a partial block in the sent
  context. The `ROLL` status pill in the bottom status bar spins while it runs;
  the `CTX` meter marker tracks the active method's cutoff. Each tool round gets
  a fresh `max_tokens` budget so tool calls are not counted against the
  output-token cap.
- **Per-MCP-server toggle** — flip an individual server on/off from the right
  panel. Disabled servers are not started and their tools are withheld from
  the model. Persists in `settings.json` (`mcpEnabled`).
- **Role-distinct chat bubbles** — every turn renders as its own framed card
  with a tinted "back" keyed to the speaker, so user / assistant / tool-call /
  tool-result / thinking blocks read apart at a glance. Fully themed: cyan-amber
  HUD cards under *Stand Alone Complex*, beveled gunmetal datavault panels with a
  UNATCO-blue / conspiracy-gold rail under the *Deus Ex* skin.
- **Per-message EDIT / CONTINUE** — hover any user or assistant bubble to reveal
  two controls that otherwise stay hidden. **EDIT** swaps the bubble for an inline
  textarea (Ctrl+Enter saves, Esc cancels) and rewrites just that message's text
  in place, leaving its reasoning / tool calls / images intact. **CONTINUE** drops
  everything after the message and resumes from it — extending an assistant reply
  straight into the same bubble, or regenerating the answer to a user turn. For
  the QWEN3 / GEMMA templates the final assistant turn is left genuinely *open* so
  the model writes on from where it stopped; for API mode the request ends on the
  assistant message with `add_generation_prompt:false` / `continue_final_message`.
- **Collapsed tool calls** — every tool call and tool result is collapsed by
  default; click the header to expand. Toggle the default in settings.
- **Connection retry** — the LLM proxy retries transient network failures
  (and 5xx responses) with exponential backoff, both server-side and
  (for direct-mode) client-side.
- **Sampling controls** — temperature, top_p, top_k, min_p, repeat_penalty,
  frequency_penalty, presence_penalty, seed, stop sequences. Extended params
  are sent both top-level and inside `extra_body` for llama.cpp / ollama /
  koboldcpp compatibility.
- **Tool-call recursion limit** — configurable cap on consecutive tool rounds
  before the agent must finalise a text reply.
- **Speech / voice output** — all voice settings live in one place,
  Settings → **§ 7 SPEECH / VOICE OUTPUT**. A single **ENABLE VOICE FEEDBACK**
  master toggle turns playback on/off (it only gates playback), and a **TTS
  Engine** selector switches between two methods:
    - **Villager talk** — Animal Crossing-style synthesised blip per streamed
      character (WebAudio, no asset dependency). Volume + pitch.
    - **Piper TTS** — offline neural speech via `piper-tts`. As a reply
      streams, each finished block (sentence / line) is synthesised on the
      backend the moment it arrives and the resulting clips are prefetched in
      parallel but played **strictly in order**, so speech begins almost
      immediately. Code blocks and `<think>` reasoning are skipped. Pick a
      voice, set volume + speed, and click **GENERATE MISSING PREVIEWS** to
      synthesise a short audition clip next to each not-yet-previewed voice.
      Only one model is held in memory at a time; switching voices or leaving
      the Piper engine frees it (`gc.collect()` + `/api/tts/unload`).
- **Local browser tool** — a self-hosted Playwright browser exposed as the
  `browser` MCP server (`browser_mcp_server.py`). It runs entirely on the local
  machine (no npx / Docker / remote Playwright). Toggling the **browser** switch
  in **§ 2 MCP / NODES** spawns/kills the server and fully closes the Chromium
  window. Settings → **§ 6 BROWSER** sets the spawn **viewport resolution**, an
  **idle auto-close duration**, headless on/off, and the install/repair button.
  The window always opens with an explicit viewport; a persistent profile keeps
  logins/cookies; adaptive **bot-detection prevention** (masked `webdriver`,
  realistic UA/locale, WebGL + plugin spoofing, `AutomationControlled` off) lets
  it work on account / secure sites. Tools include `browser_navigate`,
  `browser_click`, `browser_type`, `browser_screenshot`, tab control, and both
  **`browser_snapshot`** (full a11y tree with `[ref=eN]` handles) and
  **`browser_snapshot_compact`** (interactive + landmarks only) so the agent can
  trade detail for saved context. Actions also auto-return a snapshot
  (compact/full/off, configurable).
- **Sampling presets** — save the full sampling bundle *and* the system prompt
  as a named preset (Settings → **§ 2**), then Load / Save / Rename / Copy /
  Delete from the dropdown. Stored in `settings.json` (`samplingPresets`).
- **`<time_local>` prompt token** — drop the literal token `<time_local>`
  anywhere in the system prompt (Settings → **§ 4**) and it is substituted, on
  every send, with the machine's current local date and time formatted as
  `MONTH, DAY, YEAR, HH:MM AM/PM` (e.g. `June, 25, 2026, 3:07 PM`). It is
  re-evaluated per request from the same system clock the scheduler fires on, so
  the model always sees an accurate "now" — including inside scheduled jobs. The
  token is replaced only in what is *sent*; the prompt you typed is preserved.
- **Chat template selector** — Settings → **§ 4** chooses how the prompt reaches
  the model: **API PROVIDED** (default; structured `/chat/completions`, the
  endpoint applies the model's template), **QWEN3** (ChatML `<|im_start|>`,
  `<think>`, `<tool_call>`), or **GEMMA** (`<start_of_turn>`/`<end_of_turn>`,
  system folded into the first user turn). QWEN3/GEMMA render the exact prompt
  client-side — special/channel tokens, thinking and tool-call structure — and
  POST it to `/completions`, parsing tool calls back out of the raw output.

## ◢ HTTP API

Used by the UI; usable directly if you want to script it.

| route                                | what it does                                |
|--------------------------------------|---------------------------------------------|
| `GET  /`                             | serves `index.html`                         |
| `GET  /api/config`                   | returns `settings.json`                     |
| `POST /api/config`                   | merges + writes `settings.json`             |
| `GET  /api/tools-config`             | returns `tools.json`                        |
| `POST /api/tools-config`             | writes `tools.json` and respawns MCP        |
| `GET  /api/tools`                    | lists OpenAI-format tools from running MCP  |
| `GET  /api/servers`                  | per-server status, recent stderr, errors    |
| `POST /api/servers/reload`           | respawns all MCP servers                    |
| `POST /api/servers/toggle`           | `{name, enabled}` → start/stop ONE server   |
| `POST /api/tools/call`               | `{name, arguments}` → MCP `tools/call`      |
| `GET  /api/browser/status`           | local browser engine readiness + install log |
| `POST /api/browser/install`          | install/repair the Playwright Chromium engine |
| `GET  /api/chats`                    | list chats + active id                      |
| `POST /api/chats`                    | create chat                                 |
| `GET  /api/chats/{id}`               | fetch full chat                             |
| `POST /api/chats/{id}`               | update messages / name                      |
| `DELETE /api/chats/{id}`             | delete chat                                 |
| `POST /api/chats/{id}/rename`        | rename                                      |
| `POST /api/chats/{id}/duplicate`     | duplicate                                   |
| `POST /api/chats/active`             | set active chat id                          |
| `GET  /api/events`                   | Server-Sent Events bus (sync)               |
| `POST /api/events/publish`           | relay an event (e.g. `speak`) to other windows |
| `POST /api/llm/proxy`                | streaming proxy to the OpenAI endpoint      |
| `GET  /api/tts/status`               | Piper availability + voice list             |
| `GET  /api/tts/voices`               | list voices in `voices/` (+ `hasPreview`)   |
| `POST /api/tts/speak`                | `{text, voice, …}` → `audio/wav` (one block)|
| `POST /api/tts/previews/generate`    | synthesise previews for voices missing one  |
| `GET  /api/tts/preview?voice=<key>`  | serve a voice's preview WAV                 |
| `POST /api/tts/unload`               | free the in-memory Piper model              |
| `GET  /api/health`                   | liveness                                    |

## ◢ tools.json

Standard MCP config — same shape Claude Desktop uses. Each entry under
`mcpServers` is keyed by display name and specifies the executable to launch.
Each server must be on `PATH` and speak MCP over stdio.

The bundled local browser ships as a normal entry:

```json
{ "mcpServers": { "browser": { "command": "python", "args": ["browser_mcp_server.py"] } } }
```

`start.py` resolves `python` to the interpreter running the backend (so it works
on Windows and Linux without a `PATH` `python`), makes the script path absolute,
and injects the browser settings + project-local engine/profile paths as
`BROWSER_*` / `PLAYWRIGHT_BROWSERS_PATH` env vars when it spawns.

## ◢ Aesthetic

Cool teal/cyan HUD palette, Share Tech Mono + Chakra Petch, slow rotating
target reticle, subtle scanlines and roaming sweep line. Visual debt to the
*Ghost in the Shell: Stand Alone Complex* (2002) tachikoma diagnostic
displays and Section 9 mission HUDs — dense, labelled, instrumental.

```
A stand-alone complex is a phenomenon by which unrelated copycats are mistakenly
thought to be an organised conspiracy.
```

# demo-record

Deterministic browser demo recorder. It runs a JSON action pipeline in Chromium,
records an MP4 of the whole session, saves a screenshot after every step, and
reports a strict success/error contract. **No LLM, no agent loop** — the pipeline
is written once (typically by your coding agent) and replayed deterministically.

Built on top of [`browser-use`](https://github.com/browser-use/browser-use) for
its Chromium/CDP plumbing (video recorder, per-keystroke typing, screenshots),
but the agent/LLM layer is unused.

## Install

```bash
pip install -e .
```

Requires Python ≥ 3.11 and a Chrome/Chromium binary (browser-use finds it automatically).
The `browser-use[video]` dependency (video encoding via ffmpeg) is installed
automatically by `pip install -e .` since it's declared in `pyproject.toml`.

## Usage

```bash
demo-record demo.json           # validate + run a pipeline
demo-record --schema            # print the JSON Schema for a pipeline
demo-record --example           # print a filled-in example pipeline
demo-record --validate demo.json  # validate without running
```

## Pipeline format

A pipeline is a JSON object:

| field        | type    | default       | meaning |
|--------------|---------|---------------|---------|
| `viewport`   | object  | `{1280, 800}` | Browser viewport size. |
| `headless`   | bool    | `false`       | Run without a visible window (video still records). |
| `output_dir` | string  | `./demo_out`  | Where `video.mp4`, `steps/`, `error.png` go. |
| `delay_ms`   | int     | `250`         | Pause after **every** step. Use `wait` steps for longer pauses. |
| `steps`      | array   | —             | Ordered list of steps (below). |

Each step is a flat object with an `action` plus its params:

```json
{ "action": "navigate", "url": "http://localhost:3000", "wait_seconds": 1.0 }
{ "action": "wait", "seconds": 1.0 }
{ "action": "click", "selector": "#get-started" }
{ "action": "type", "selector": "input[name=email]", "text": "demo@acme.com", "type_delay_ms": 70, "clear": true }
{ "action": "select", "selector": "select#plan", "option": "pro" }
{ "action": "scroll", "selector": "#pricing" }
{ "action": "assert_text", "text": "Welcome" }
{ "action": "done" }
```

Notes:

- **Elements are targeted by CSS selector** (not element index). Your agent reads
  the selectors from the code it wrote.
- **Auto-scroll into center**: `click`, `type`, `select`, and `scroll` first
  smooth-scroll the element to the vertical center of the viewport and pause, so
  interactions are visible on video and you never hand-tune scroll offsets.
- **`type`** types the final text letter-by-letter (`type_delay_ms` controls the
  cadence) — it looks human on video. Set `clear: false` to append.
- **`select`** scrolls to a native `<select>`, picks the option (by value or
  visible label), and fires `input`/`change` events.
- **`assert_text`** stops the pipeline (error) if the text is not in the page body.

## Output contract

- **Success** → exit code `0`, prints `VIDEO_OK <output_dir>/video.mp4`
  (plus `steps/step_01.png` … for review).
- **Failure** → exit code `1`, prints
  `ERROR step N <action>: <message>` and `LAST_SCREENSHOT <output_dir>/error.png`,
  plus `VIDEO_PARTIAL <output_dir>/video.mp4` (footage up to the break).

The video is saved even on failure, so you can watch exactly where it broke.

## Using it from your coding agents

Drop this into your project's `AGENTS.md` so the building agent authors and
self-checks a demo pipeline:

```markdown
## Demo pipeline

After building the app, write `demo.json` describing a short happy-path
walkthrough of what you built. Schema: run `demo-record --schema`; a filled
example: `demo-record --example`.

Rules:
- Target elements by the CSS selectors / ids present in the code you wrote.
- Do NOT invent element indices. Always use selectors.
- Set `headless` to whatever the user asked for (default false).
- Use top-level `delay_ms` for pacing; add `wait` steps for longer transitions.
- ALWAYS run `demo-record --validate demo.json` and fix any errors until it
  prints OK before telling the user it's ready.

The user runs `demo-record demo.json` to produce the video. On failure the tool
prints `ERROR step N ...` and `LAST_SCREENSHOT <path>` — report that back.
```

## What it does NOT do (by design)

- No LLM / exploration / self-healing. The pipeline is fixed.
- No element re-matching or recording of an exploratory session.
- No post-production polish (zoom, captions). Raw MP4 is enough for review.

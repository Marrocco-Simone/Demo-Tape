# demo-record

Deterministic browser demo recorder. It runs a JSON action pipeline in Chromium,
records an MP4 of the whole session, and reports a strict success/error
contract. **No LLM, no agent loop** — the pipeline is written once (typically by
your coding agent) and replayed deterministically.

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

### Global CLI (run from any repo)

`pip install -e .` only activates inside this project's venv. To get a global
`demo-record` command, symlink it onto your PATH:

```bash
ln -s "$(pwd)/.venv/bin/demo-record" ~/.local/bin/demo-record
```

The symlink runs through the project venv (editable install), so code changes
apply immediately. To update the tool later: `git pull && pip install -e .`

**Browser choice:** a standalone Chromium is preferred automatically when one is
installed (Playwright cache — `playwright install chromium` — or
`/Applications/Chromium.app`); otherwise it falls back to system Google Chrome.

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
| `viewport`   | object  | `{1536, 864}` | Page viewport = video size (16:9, fits laptops; both multiples of 16 so the MP4 has no black bars). |
| `headless`   | bool    | `false`       | Run without a visible window (video still records). |
| `output_dir` | string  | `./demo_out`  | Where `video.mp4` and `error.png` go. |
| `delay_ms`   | int     | `500`         | Pause after **every** step. Use `wait` steps for longer pauses. |
| `steps`      | array   | —             | Ordered list of steps (below). |

Each step is a flat object with an `action` plus its params:

```json
{ "action": "navigate", "url": "http://localhost:3000", "wait_seconds": 1.0 }
{ "action": "back", "wait_seconds": 1.0 }
{ "action": "wait", "seconds": 1.0 }
{ "action": "click", "selector": "#get-started" }
{ "action": "type", "selector": "input[name=email]", "text": "demo@acme.com", "type_delay_ms": 70, "clear": true }
{ "action": "select", "selector": "select#plan", "option": "pro" }
{ "action": "scroll", "selector": "#pricing" }
{ "action": "highlight", "selector": "#pricing", "duration_seconds": 2.0, "spotlight": false }
{ "action": "title", "text": "Sign up", "position": "top", "align": "left" }
{ "action": "description", "text": "Choose a plan to unlock athletes", "position": "bottom", "align": "center" }
{ "action": "assert_text", "text": "Welcome" }
{ "action": "assert_value", "selector": "input[name=email]", "value": "demo@acme.com" }
{ "action": "done" }
```

Notes:

- **Elements are targeted by CSS selector** (not element index). Your agent reads
  the selectors from the code it wrote.
- **Click/typing feedback is automatic**: every `click` first draws a breathing
  ring around the element, then ripple waves spread from its center (like circles
  on still water), then the real click lands. `type` rings the input for as long
  as the typing takes. All effects are DOM overlays injected by the recorder —
  they work on any page and never touch the app's own styles.
- **`highlight`** draws the same ring on any element (a section, a div, a card)
  without clicking it — use it to guide attention. Add `"spotlight": true` to dim
  the rest of the page while the element is highlighted. `duration_seconds`
  controls how long the ring stays (default 2s).
- **Auto-scroll into center**: `click`, `type`, `select`, `highlight`, and
  `scroll` first smooth-scroll the element to the vertical center of the viewport
  and pause, so interactions are visible on video and you never hand-tune scroll
  offsets.
- **`type`** types the final text letter-by-letter (`type_delay_ms` controls the
  cadence) — it looks human on video, and each character is inserted via CDP
  `Input.insertText`, so **React-controlled inputs keep the text** (plain key
  events alone get reverted by React's synthetic event layer). Set
  `clear: false` to append.
- **`select`** scrolls to a native `<select>`, picks the option (by value or
  visible label), and fires `input`/`change` events.
- **`assert_text`** stops the pipeline (error) if the text is not in the page
  body. Matching is **case-insensitive by default** (CSS `text-transform` like
  uppercase labels would otherwise break asserts written from source); pass
  `"case_sensitive": true` for exact matching. On failure the error includes
  the **current URL** and a slice of what the page actually says.
- **`assert_value`** stops the pipeline unless the element (input, textarea,
  select) holds exactly `value`. Use it right after `type`/`select` to catch a
  field silently dropping input — `assert_text` can't see input values.
- **`title` / `description`** render narration text on the video, live: fixed,
  pointer-transparent pills injected into the app page. The description uses a
  smaller font and can carry more text; long text wraps instead of being cut
  off. Placement is up to the model via `position` (`top`/`bottom`, defaults
  title→top, description→bottom) and `align` (`left`/`center`/`right`, default
  `center`). If both share the same position they merge into a single box —
  title on the first line, description under it (the box follows the title's
  align). An empty `text` hides that slot.
  - **Paired steps are atomic**: when a `title` step is immediately followed by
    a `description` step (nothing between them), the inter-step pause is
    skipped, so the viewer never sees a mismatched pair (new title + old
    description). Emit them as a pair at every chapter change.
  - **Change text AFTER the action it describes**: an overlay change that
    directly follows an action (click, type, navigate, …) applies immediately,
    with no pause — the narration lands on the frame the action produced.
    Changing text before an action keeps the normal pause first. Two overlay
    changes back to back (title → title) also keep the pause between them, so
    each state stays readable.
  - **Narration survives navigation**: set it once and it re-appears on every
    page — including before the first `navigate` — and it re-applies itself if
    the app replaces its DOM mid-run (loading screens, soft navigation).
  - The bars render in Chrome's top layer, so app CSS (transforms, overflow)
    cannot hide or clip them.

## Tips

- **Dev servers**: a first `navigate` against a dev server (Next.js, etc.) compiles
  the route on first request, so give it room: `{ "action": "navigate", "url": "...",
  "wait_seconds": 8 }`. Tune down once the route is warm.
- **Pacing**: `delay_ms` (default `500`) is the rhythm between steps. Lower it for a
  snappier video; add explicit `wait` steps where the app needs a real pause.

## Output contract

- **Success** → exit code `0`, prints `VIDEO_OK <output_dir>/video.mp4`.
- **Failure** → exit code `1`, prints
  `ERROR step N <action>: <message>` (or `ERROR startup: <message>`) and
  `LAST_SCREENSHOT <output_dir>/error.png`, plus `VIDEO_PARTIAL
  <output_dir>/video.mp4` when footage exists (up to the break).
- **Interrupt** (Ctrl+C) → exit code `130`, prints `STOPPED interrupted (Ctrl+C)`
  plus `LAST_SCREENSHOT` and `VIDEO_PARTIAL`.
- **Browser closed** → exit code `1`, prints
  `STOPPED browser was closed (<reason>)` plus `LAST_SCREENSHOT` and
  `VIDEO_PARTIAL`.

The video is saved even on failure or stop, so you can watch exactly where it
broke or what was happening when the run ended.

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
- Use top-level `delay_ms` (default 500) for pacing; add `wait` steps for longer transitions.
- Narrate chapters with a `title` step immediately followed by a `description`
  step (adjacent, nothing between) — the tool then applies them as one change.
- Emit narration AFTER the action it describes (click → title/description), not
  before: text changes right after an action apply instantly.
- If you target a dev server, give the first `navigate` a high `wait_seconds` (~8s) — routes compile on first request.
- ALWAYS run `demo-record --validate demo.json` and fix any errors until it
  prints OK before telling the user it's ready.

The user runs `demo-record demo.json` to produce the video. Exit codes: `0` ok,
`1` failure or browser closed, `130` interrupted. On failure the tool prints
`ERROR step N ...` (for assert_text it also prints the final URL + what the
page says) and `LAST_SCREENSHOT <path>`; on a stop it prints `STOPPED <reason>`
— report that back.
```

## What it does NOT do (by design)

- No LLM / exploration / self-healing. The pipeline is fixed.
- No element re-matching or recording of an exploratory session.
- No zoom/pan/caption post-production. Narration text is burned in live and the
  MP4 is the final product.

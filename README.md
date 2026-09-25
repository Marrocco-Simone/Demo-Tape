# demo-tape

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

For text-to-speech narration, install the optional extra (Kokoro runs fully
local on CPU):

```bash
pip install -e ".[tts]"
```

### Global CLI (run from any repo)

`pip install -e .` only activates inside this project's venv. To get a global
`demotape` command, symlink it onto your PATH:

```bash
ln -s "$(pwd)/.venv/bin/demotape" ~/.local/bin/demotape
```

The symlink runs through the project venv (editable install), so code changes
apply immediately. To update the tool later: `git pull && pip install -e .`

**Browser choice:** a standalone Chromium is preferred automatically when one is
installed (Playwright cache — `playwright install chromium` — or
`/Applications/Chromium.app`); otherwise it falls back to system Google Chrome.

## Usage

```bash
demotape demo.json             # validate + run a pipeline
demotape --schema              # print the JSON Schema for a pipeline
demotape --example             # print a filled-in example pipeline
demotape --validate demo.json  # validate without running
demotape --estimate demo.json  # print the estimated duration without running
```

## Pipeline format

A pipeline is a JSON object:

| field        | type    | default       | meaning |
|--------------|---------|---------------|---------|
| `viewport`   | object  | `{1536, 864}` | Page viewport = video size (16:9, fits laptops; both multiples of 16 so the MP4 has no black bars). |
| `headless`   | bool    | `false`       | Run without a visible window (video still records). |
| `output_dir` | string  | `./demo_out`  | Where `video.mp4` and `error.png` go. |
| `delay_ms`   | int     | `500`         | Pause between two consecutive actions. A text change and the action it describes run back-to-back — author **text-first** (title/description, then the action), and the action starts immediately. |
| `text_to_speech` | bool | `false`      | Narrate the overlay text with a local TTS model (see below). |
| `tts_voice`  | string  | `af_heart`    | Kokoro voice; the first letter picks the language (e.g. `im_nicola` Italian, `bf_emma` British English). Only with `text_to_speech`. |
| `reading_pace` | float | —             | Silent-reading pace in words/second (measured: 3.5). When set, every caption stays up for at least `words / reading_pace` seconds (clamped 2.3–11s) before the next text change — measured on the video's clock, so app work that ran under the caption counts and a slow app adds no dead air. With `text_to_speech` the hold is `max(voice end, reading time)`. Unset: no reading hold. |
| `steps`      | array   | —             | Ordered list of steps (below). |

Each step is a flat object with an `action` plus its params:

```json
{ "action": "navigate", "url": "http://localhost:3000", "wait_seconds": 1.0 }
{ "action": "back", "wait_seconds": 1.0 }
{ "action": "wait", "seconds": 1.0 }
{ "action": "click", "selector": "#get-started" }
{ "action": "type", "selector": "input[name=email]", "text": "demo@acme.com", "type_delay_ms": 70, "clear": true }
{ "action": "drag", "selector": "[role=slider]", "to": 0.7, "duration_seconds": 0.6, "axis": "x" }
{ "action": "drag", "selector": "#task-card-3", "to_selector": "#column-done" }
{ "action": "select", "selector": "select#plan", "option": "pro" }
{ "action": "scroll", "selector": "#pricing" }
{ "action": "highlight", "selector": "#pricing", "duration_seconds": 2.0, "spotlight": false }
{ "action": "title", "text": "Sign up", "position": "top", "align": "left" }
{ "action": "description", "text": "Choose a plan to unlock athletes", "position": "bottom", "align": "center" }
{ "action": "say", "title": "Choosing a plan", "description": "Pro unlocks the athletes section", "position": "top", "align": "left" }
{ "action": "assert_text", "text": "Welcome" }
{ "action": "assert_value", "selector": "input[name=email]", "value": "demo@acme.com" }
{ "action": "done" }
```

Notes:

- **Progress lines**: every recorded event prints one parseable line to stdout,
  timestamped on the video's clock, so an agent (or human) can follow what the
  recording shows — text changes, narration start/finish, clicks, errors:

  ```
  [T+   1.4] step 2 title: "Benvenuti in demotape"
  [T+   1.4] narration started (11.0s): "Questo video ha un narratore locale…"
  [T+  12.5] narration finished
  [T+  12.9] step 4 click: #get-started
  VIDEO_OK ./demo_out/video.mp4
  ```

- **Elements are targeted by CSS selector** (not element index). Your agent reads
  the selectors from the code it wrote.
- **Click/typing feedback is automatic**: every `click` draws a breathing ring
  around the element, and 0.2s later the real click lands while ripple waves
  spread from its center. `type` rings the input for as long
  as the typing takes. All effects are DOM overlays injected by the recorder —
  they work on any page and never touch the app's own styles.
- **`highlight`** draws the same ring on any element (a section, a div, a card)
  without clicking it — use it to guide attention. Add `"spotlight": true` to dim
  the rest of the page while the element is highlighted. `duration_seconds`
  controls how long the ring stays (default 2s).
- **Auto-scroll into center**: `click`, `type`, `select`, `highlight`, `drag`, and
  `scroll` first smooth-scroll the element to the vertical center of the viewport
  and pause, so interactions are visible on video and you never hand-tune scroll
  offsets.
- **`type`** types the final text letter-by-letter (`type_delay_ms` controls the
  cadence) — it looks human on video, and each character is inserted via CDP
  `Input.insertText`, so **React-controlled inputs keep the text** (plain key
  events alone get reverted by React's synthetic event layer). Date and time
  inputs (`time`, `date`, `datetime-local`, `month`, `week`) receive keypress
  events instead, because they ignore `insertText`: type the digits and
  meridiem as the field expects them (`0900AM` gives `09:00`). Set
  `clear: false` to append.
- **`select`** scrolls to a native `<select>`, picks the option (by value or
  visible label), and fires `input`/`change` events.
- **`text_to_speech`** adds a voiceover: every overlay text change (the
  `description`, or the `title` when there is no description) is spoken by
  **Kokoro-82M** — a fully local, Apache-licensed model. The voice starts when
  the text appears, **actions keep running while it speaks**, and the video
  only pauses right before the *next* text change (or at the end), so speech
  is never cut off. Clips are mixed into `video.mp4` as its audio track,
  aligned to the exact moment the text appeared. Requires
  `pip install 'demo-tape[tts]'` plus `ffmpeg` on PATH; the model (~330MB)
  downloads automatically from Hugging Face on first use and is cached after
  that. Repeated identical text is narrated once.
- **`drag`** moves the pointer with real CDP mouse events (press → interpolated
  moves → release). Without `to_selector` it drags across `selector` itself —
  use for React Native Web sliders (`role="slider"` divs; a plain click moves
  nothing because the value comes from the pointer position): `to`/`from` are
  positions along the element's box (0.0 edge, 0.5 center, 1.0 other edge),
  `axis: "y"` for vertical tracks. With `to_selector` it drags element A and
  releases on element B — drag-to-reorder lists, drop zones. The travel is a
  straight line spread over `duration_seconds` so it looks human, both elements
  are ringed, and both must be on screen at once.
  Sliders snap their value to the *press* point, so same-element drags start at
  the edge by default. To move a slider that already holds a value without that
  snap, press on its thumb and release on the track:
  `{ "action": "drag", "selector": "[role=slider] .thumb", "to_selector": "[role=slider]", "to": 0.7 }`.
- **`assert_text`** stops the pipeline (error) if the text is not in the page
  body. Matching is **case-insensitive by default** (CSS `text-transform` like
  uppercase labels would otherwise break asserts written from source); pass
  `"case_sensitive": true` for exact matching. On failure the error includes
  the **current URL** and a slice of what the page actually says.
- **`assert_value`** stops the pipeline unless the element (input, textarea,
  select) holds exactly `value`. Use it right after `type`/`select` to catch a
  field silently dropping input — `assert_text` can't see input values.
- **`say`** is the one-step alternative to the `title`+`description` pair:
  `{"action": "say", "title": "Choosing a plan", "description": "Pro unlocks the
  athletes section"}` sets both bars as ONE text change — one render, one
  narration event, no pairing rules to get wrong. The `description` is the
  spoken text (the `title` when the description is empty); an empty field hides
  that slot, and `say` with both empty clears the caption. Both bars share
  `position` (default `top`), so they render as one card — title on the first
  line, description under it — and `align` (default `center`) places it.
  Prefer `say` over pairs in new pipelines; pairs keep working for
  backward compatibility.
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
    description). Emit them as a pair at every chapter change. The pair ends at
    the description, so `title, description, title, description` is two pairs
    and each one is narrated.
  - **Change text BEFORE the action it describes**: the action after a text
    change starts immediately, so the narration covers what is about to happen
    (and, with `text_to_speech`, the voice speaks while the action runs). Two
    overlay changes back to back (title → title) keep the pause between them,
    so each state stays readable.
  - **Narration survives navigation**: set it once and it re-appears on every
    page — including before the first `navigate` — and it re-applies itself if
    the app replaces its DOM mid-run (loading screens, soft navigation).
  - The bars render in Chrome's top layer, so app CSS (transforms, overflow)
    cannot hide or clip them.

## Tips

- **Dev servers**: a first `navigate` against a dev server (Next.js, etc.) compiles
  the route on first request, so give it room: `{ "action": "navigate", "url": "...",
  "wait_seconds": 8 }`. Tune down once the route is warm.
- **Pacing**: `delay_ms` (default `500`) is the rhythm between two consecutive
  actions — a text change and the action it describes run back-to-back. Lower
  it for a snappier video; add explicit `wait` steps where the app needs a
  real pause.

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
walkthrough of what you built. Schema: run `demotape --schema`; a filled
example: `demotape --example`.

Rules:
- Target elements by the CSS selectors / ids present in the code you wrote.
- Do NOT invent element indices. Always use selectors.
- Set `headless` to whatever the user asked for (default false).
- Use top-level `delay_ms` (default 500) for pacing; add `wait` steps for longer transitions.
- Narrate chapters with a `say` step (`{"action": "say", "title": ..., "description": ...}`)
  — both bars render as one text change. A `title` step immediately followed by
  a `description` step (adjacent, nothing between) also works.
- Emit narration BEFORE the action it describes (say → click):
  the action starts immediately after its text change.
- If you target a dev server, give the first `navigate` a high `wait_seconds` (~8s) — routes compile on first request.
- ALWAYS run `demotape --validate demo.json` and fix any errors until it
  prints OK before telling the user it's ready.

The user runs `demotape demo.json` to produce the video. Exit codes: `0` ok,
`1` failure or browser closed, `130` interrupted. On failure the tool prints
`ERROR step N ...` (for assert_text it also prints the final URL + what the
page says) and `LAST_SCREENSHOT <path>`; on a stop it prints `STOPPED <reason>`
— report that back.
```

## Writing a generator (instead of hand-writing JSON)

Past ~50 steps, hand-written pipeline JSON breaks down: repeated selector soup,
no comments, no computation, no way to state invariants. The pattern that
scales is a **small generator script** (Python, or whatever your agent writes)
that holds the selectors and emits the JSON — regenerate the JSON right before
every recording, and treat the JSON, not the script, as the record-time
artifact. The tool ships a thin builder for exactly this:

```python
from demotape import Pipeline

# reading_pace defaults to the measured 3.5 words/s; pass None to disable
p = Pipeline(base_url="http://localhost:3000", output_dir="./demo_out")
p.say("Sign up", "Create your account in seconds")
p.click("#get-started")
p.type("input[name=email]", "demo@acme.com")
p.assert_value("input[name=email]", "demo@acme.com")
p.write("demo.json")  # appends `done`, writes JSON, prints ~duration
```

Rules that keep generated pipelines maintainable:

- **Keep selectors as named constants** — one point of edit when a brittle
  Tailwind-class selector changes; every video follows the fix.
- **Write selector functions for positional targets** (`:nth-of-type(n)`,
  "the n-th chip") instead of scattering the indices through the steps.
- **Pair every `say` before the action it describes** — the action starts
  immediately after the text renders, so the narration covers what is about to
  happen.
- **Follow every `type` with `assert_value`** — a silently empty input surfaces
  much later as a validation error on an unrelated screen; catch it where it
  happens.
- **Regenerate the JSON right before recording** — the script is the source of
  truth; a stale JSON quietly records an old story.

The runner never executes user code: the builder only produces JSON, and the
JSON file stays the contract (`demotape --validate` / `--estimate` read it).

## What it does NOT do (by design)

- No LLM / exploration / self-healing. The pipeline is fixed.
- No element re-matching or recording of an exploratory session.
- No zoom/pan/caption post-production. Narration text is burned in live and the
  MP4 is the final product.

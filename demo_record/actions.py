"""Action handlers.

Every element-targeting action resolves a CSS selector, scrolls the element to
the vertical center of the viewport, waits a beat (so the motion is visible on
video), then interacts. Element interaction runs through a small JS snippet via
CDP `Runtime.evaluate` for reliability; typing additionally uses per-keystroke
CDP `Input.dispatchKeyEvent` so text appears letter-by-letter on the recording.

Title/description overlays are injected straight into the app page as fixed,
pointer-transparent bars, and re-injected after every navigate (navigation
wipes the DOM). The current overlay state lives on the runner.

Each handler raises ActionError with a human-readable message naming the
selector on failure, which the runner turns into the ERROR contract line.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from browser_use.browser.session import BrowserSession, CDPSession

from demo_record.spec import Step

# Seconds to wait after scrolling an element into view, so the smooth
# scroll motion is captured on video before the interaction happens.
SETTLE_AFTER_SCROLL_S = 0.4

_OVERLAY_BAR_CSS = (
    "display:flex;flex-direction:column;gap:6px;"
    "background:rgba(15,23,42,0.78);padding:14px 24px;border-radius:16px;"
    "backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);"
    "border:1px solid rgba(148,163,184,0.25);box-shadow:0 12px 40px rgba(0,0,0,0.4);"
    "max-width:min(680px, 82%);box-sizing:border-box;"
)
_TITLE_LINE_CSS = "font-size:26px;font-weight:700;line-height:1.25;color:#f8fafc;"
_DESCRIPTION_LINE_CSS = "font-size:17px;font-weight:400;line-height:1.45;color:#cbd5e1;white-space:pre-wrap;"


class ActionError(Exception):
    """A step failed in a way that should be surfaced to the user/model."""


class DoneSignal(Exception):
    """Raised by the `done` step to end the pipeline cleanly."""


# Keys that should produce a real character via `Input.insertText` rather than a
# synthesized keydown/keyup pair. This keeps input events natural for the page.
_TYPEABLE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 `~!@#$%^&*()_+-=[]{}\\|;:'\",<.>/?")

_ALIGN_TO_JUSTIFY = {"left": "flex-start", "center": "center", "right": "flex-end"}


class ActionRunner:
    def __init__(self, browser: BrowserSession) -> None:
        self._browser = browser
        self._overlay: dict[str, dict[str, str]] = {}

    async def _cdp(self) -> tuple[Any, str]:
        cdp_session: CDPSession = await self._browser.get_or_create_cdp_session()
        return cdp_session.cdp_client, cdp_session.session_id

    async def _eval(self, expression: str) -> Any:
        """Evaluate a JS expression in the page and return its value."""
        client, session_id = await self._cdp()
        result = await client.send.Runtime.evaluate(
            params={"expression": expression, "returnByValue": True, "awaitPromise": True},
            session_id=session_id,
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = details.get("text", "JS exception")
            exc = details.get("exception", {})
            description = exc.get("description") if isinstance(exc, dict) else None
            raise ActionError(f"page script error: {description or text}")
        inner = result.get("result", {})
        if inner.get("subtype") == "error":
            raise ActionError(f"page script error: {inner.get('description', 'evaluation failed')}")
        return inner.get("value")

    # -- overlay ----------------------------------------------------------------

    async def _render_overlay(self) -> None:
        """Rebuild the overlay bars from state. Idempotent; safe after navigation.

        Title and description that share the same position render as one merged
        bar (title line stacked over description line), aligned by the title's
        align. Long text wraps instead of being cut off.
        """
        if not self._overlay:
            return
        state = {
            "title": self._overlay.get("title"),
            "description": self._overlay.get("description"),
        }
        expr = f"""
(() => {{
  const state = {json.dumps(state)};
  const title = state.title && state.title.text ? state.title : null;
  const desc = state.description && state.description.text ? state.description : null;
  if (!title && !desc) return {{ ok: true, empty: true }};
  let root = document.getElementById('demo-record-overlay');
  if (!root) {{
    root = document.createElement('div');
    root.id = 'demo-record-overlay';
    root.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;'
      + 'font-family:-apple-system,Segoe UI,Roboto,sans-serif;';
    (document.body || document.documentElement).appendChild(root);
  }}
  const justify = a => a === 'left' ? 'flex-start' : a === 'right' ? 'flex-end' : 'center';
  const makeBar = () => {{
    const bar = document.createElement('div');
    bar.style.cssText = {json.dumps(_OVERLAY_BAR_CSS)};
    return bar;
  }};
  const addLine = (bar, text, kind) => {{
    const line = document.createElement('div');
    line.style.cssText = kind === 'title' ? {json.dumps(_TITLE_LINE_CSS)} : {json.dumps(_DESCRIPTION_LINE_CSS)};
    line.textContent = text;
    bar.appendChild(line);
  }};
  const rendered = [];
  for (const position of ['top', 'bottom']) {{
    const slotTitle = title && title.position === position ? title : null;
    const slotDesc = desc && desc.position === position ? desc : null;
    if (!slotTitle && !slotDesc) continue;
    const bar = makeBar();
    if (slotTitle) addLine(bar, slotTitle.text, 'title');
    if (slotDesc) addLine(bar, slotDesc.text, 'description');
    const wrap = document.createElement('div');
    wrap.style.cssText = 'position:absolute;left:0;right:0;display:flex;padding:20px 28px;box-sizing:border-box;'
      + (position === 'top' ? 'top:0;' : 'bottom:0;')
      + 'justify-content:' + justify((slotTitle || slotDesc).align) + ';';
    wrap.appendChild(bar);
    rendered.push({{ position, wrap }});
  }}
  root.replaceChildren(...rendered.map(r => r.wrap));
  for (const r of rendered) {{
    r.wrap.firstChild.animate(
      [{{ opacity: 0, transform: 'translateY(' + (r.position === 'top' ? '-8px' : '8px') + ')' }},
       {{ opacity: 1, transform: 'none' }}],
      {{ duration: 320, easing: 'ease-out' }}
    );
  }}
  return {{ ok: true }};
}})()
"""
        value = await self._eval(expr)
        if not value or not value.get("ok"):
            raise ActionError("could not inject overlay into the page")

    async def title(self, text: str, position: str, align: str) -> None:
        self._overlay["title"] = {"text": text, "position": position, "align": align}
        await self._render_overlay()

    async def description(self, text: str, position: str, align: str) -> None:
        self._overlay["description"] = {"text": text, "position": position, "align": align}
        await self._render_overlay()

    # -- shared helpers --------------------------------------------------------

    async def _scroll_into_center(self, selector: str) -> None:
        """Bring `selector` to the center of the viewport, raising if missing."""
        expr = f"""
(() => {{
  const el = document.querySelector({json.dumps(selector)});
  if (!el) return {{ found: false }};
  el.scrollIntoView({{ block: 'center', inline: 'center', behavior: 'smooth' }});
  return {{ found: true }};
}})()
"""
        value = await self._eval(expr)
        if not value or not value.get("found"):
            raise ActionError(f'selector "{selector}" not found in page')
        await asyncio.sleep(SETTLE_AFTER_SCROLL_S)

    async def _wait_for_load(self, timeout_s: float = 15.0) -> None:
        """Wait until document.readyState is 'complete'."""
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            state = await self._eval("document.readyState")
            if state == "complete":
                return
            if asyncio.get_running_loop().time() > deadline:
                raise ActionError(f"page did not finish loading within {timeout_s:.0f}s")
            await asyncio.sleep(0.1)

    # -- individual actions -------------------------------------------------

    async def navigate(self, url: str, wait_seconds: float) -> None:
        client, session_id = await self._cdp()
        await client.send.Page.navigate(params={"url": url}, session_id=session_id)
        await self._wait_for_load()
        if self._overlay:
            # Navigation wiped the DOM; restore the current overlay state.
            await self._render_overlay()
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)

    async def wait(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def click(self, selector: str) -> None:
        await self._scroll_into_center(selector)
        expr = f"""
(() => {{
  const el = document.querySelector({json.dumps(selector)});
  if (!el) return {{ ok: false }};
  el.click();
  return {{ ok: true }};
}})()
"""
        value = await self._eval(expr)
        if not value or not value.get("ok"):
            raise ActionError(f'could not click selector "{selector}"')

    async def type(self, selector: str, text: str, type_delay_ms: int, clear: bool) -> None:
        await self._scroll_into_center(selector)
        focus_expr = f"""
(() => {{
  const el = document.querySelector({json.dumps(selector)});
  if (!el) return {{ ok: false }};
  el.focus();
  if ({str(clear).lower()}) {{
    if ('value' in el) el.value = '';
    else el.textContent = '';
  }}
  return {{ ok: true }};
}})()
"""
        value = await self._eval(focus_expr)
        if not value or not value.get("ok"):
            raise ActionError(f'could not focus selector "{selector}" for typing')

        client, session_id = await self._cdp()
        for ch in text:
            if ch in _TYPEABLE:
                # keyDown -> insertText -> keyUp produces a real char + input events.
                await client.send.Input.dispatchKeyEvent(
                    params={"type": "keyDown", "text": ch}, session_id=session_id
                )
                await client.send.Input.dispatchKeyEvent(
                    params={"type": "keyUp", "text": ch}, session_id=session_id
                )
            else:
                # Non-ASCII / symbols: use rawKeyDown + insertText for reliability.
                await client.send.Input.dispatchKeyEvent(
                    params={"type": "rawKeyDown"}, session_id=session_id
                )
                await client.send.Input.insertText(params={"text": ch}, session_id=session_id)
                await client.send.Input.dispatchKeyEvent(
                    params={"type": "keyUp"}, session_id=session_id
                )
            if type_delay_ms > 0:
                await asyncio.sleep(type_delay_ms / 1000.0)

    async def select(self, selector: str, option: str) -> None:
        await self._scroll_into_center(selector)
        expr = f"""
(() => {{
  const el = document.querySelector({json.dumps(selector)});
  if (!el || el.tagName !== 'SELECT') return {{ ok: false, reason: 'not a <select>' }};
  const want = {json.dumps(option)};
  let idx = -1;
  for (let i = 0; i < el.options.length; i++) {{
    if (el.options[i].value === want || el.options[i].text === want) {{ idx = i; break; }}
  }}
  if (idx === -1) return {{ ok: false, reason: 'option "' + want + '" not found' }};
  el.selectedIndex = idx;
  el.dispatchEvent(new Event('input', {{ bubbles: true }}));
  el.dispatchEvent(new Event('change', {{ bubbles: true }}));
  return {{ ok: true }};
}})()
"""
        value = await self._eval(expr)
        if not value or not value.get("ok"):
            reason = (value or {}).get("reason", "unknown")
            raise ActionError(f'could not select option on "{selector}": {reason}')

    async def scroll(self, selector: str) -> None:
        await self._scroll_into_center(selector)

    async def assert_text(self, text: str) -> None:
        expr = """
(() => {
  const want = %s;
  const url = location.href;
  const body = document.body ? document.body.innerText : '';
  const re = /\\s+/g;
  const norm = s => s.replace(re, ' ');
  // Plain substring first (exact presence).
  if (body.includes(want)) return { found: true, url, snippet: '' };
  // Whitespace-tolerant check (survives newlines/extra spaces in the page).
  if (norm(body).includes(norm(want))) return { found: true, url, snippet: '' };
  return { found: false, url, snippet: norm(body).slice(0, 200) };
})()
""" % json.dumps(text)
        value = await self._eval(expr)
        if not value or not value.get("found"):
            url = (value or {}).get("url", "unknown url")
            snippet = (value or {}).get("snippet", "")
            raise ActionError(
                f'assert_text failed: "{text}" not found at {url}. page says: "{snippet}..."'
            )

    async def done(self) -> None:
        raise DoneSignal()

    # -- dispatch ------------------------------------------------------------

    async def run(self, step: Step) -> None:
        action = step.action
        if action == "navigate":
            await self.navigate(step.url, step.wait_seconds)
        elif action == "wait":
            await self.wait(step.seconds)
        elif action == "click":
            await self.click(step.selector)
        elif action == "type":
            await self.type(step.selector, step.text, step.type_delay_ms, step.clear)
        elif action == "select":
            await self.select(step.selector, step.option)
        elif action == "scroll":
            await self.scroll(step.selector)
        elif action == "title":
            await self.title(step.text, step.position, step.align)
        elif action == "description":
            await self.description(step.text, step.position, step.align)
        elif action == "assert_text":
            await self.assert_text(step.text)
        elif action == "done":
            await self.done()
        else:  # pragma: no cover - pydantic discriminates, so unreachable
            raise ActionError(f"unknown action: {action}")

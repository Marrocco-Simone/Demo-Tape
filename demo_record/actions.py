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

# Click/typing feedback: sky blue reads on light and dark apps alike.
_FX_ACCENT = "#38bdf8"
# Click choreography: ring appears first, then ripples, then the real click.
CLICK_RING_MS = 1400
RING_LEAD_S = 0.45
RIPPLE_LEAD_S = 0.55

_FX_SCRIPT = f"""
(() => {{
  if (!window.__demoFx) {{
  if (!document.getElementById('demo-record-fx-style')) {{
    const style = document.createElement('style');
    style.id = 'demo-record-fx-style';
    style.textContent = `
      @keyframes demoRingBreath {{ 0%,100% {{ transform: scale(1); }} 50% {{ transform: scale(1.03); }} }}
      @keyframes demoRipple {{
        from {{ transform: translate(-50%,-50%) scale(1); opacity: 0.85; }}
        to {{ transform: translate(-50%,-50%) scale(9); opacity: 0; }}
      }}
    `;
    document.head.appendChild(style);
  }}
  window.__demoFx = {{
    ring(selector, opts) {{
      const el = document.querySelector(selector);
      if (!el) return {{ ok: false }};
      const ms = (opts && opts.durationMs) || 1500;
      const spotlight = !!(opts && opts.spotlight);
      const r = el.getBoundingClientRect();
      const pad = 8;
      const ring = document.createElement('div');
      ring.id = 'demo-record-fx-ring';
      ring.style.cssText = 'position:fixed;pointer-events:none;z-index:2147483644;'
        + 'border:3px solid {_FX_ACCENT};border-radius:12px;'
        + 'box-shadow:0 0 0 4px rgba(56,189,248,0.25), 0 0 26px rgba(56,189,248,0.55)'
        + (spotlight ? ', 0 0 0 9999px rgba(15,23,42,0.45);' : ';')
        + 'left:' + (r.left - pad) + 'px;top:' + (r.top - pad) + 'px;'
        + 'width:' + (r.width + pad*2) + 'px;height:' + (r.height + pad*2) + 'px;'
        + 'animation:demoRingBreath 1.1s ease-in-out infinite;';
      (document.body || document.documentElement).appendChild(ring);
      setTimeout(() => ring.remove(), ms);
      return {{ ok: true }};
    }},
    ripple(selector) {{
      const el = document.querySelector(selector);
      if (!el) return {{ ok: false }};
      const r = el.getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      for (let i = 0; i < 3; i++) {{
        const wave = document.createElement('div');
        wave.style.cssText = 'position:fixed;pointer-events:none;z-index:2147483645;'
          + 'left:' + cx + 'px;top:' + cy + 'px;width:14px;height:14px;border-radius:50%;'
          + 'border:2.5px solid {_FX_ACCENT};'
          + 'animation:demoRipple 0.85s ease-out forwards;'
          + 'animation-delay:' + (i * 0.13) + 's;opacity:0;'
          + 'transform:translate(-50%,-50%);';
        (document.body || document.documentElement).appendChild(wave);
        setTimeout(() => wave.remove(), 1400 + i * 130);
      }}
      return {{ ok: true }};
    }},
    clear() {{
      const ring = document.getElementById('demo-record-fx-ring');
      if (ring) ring.remove();
      return {{ ok: true }};
    }},
  }};
  }}
  return {{ ok: true }};
}})()
"""


class ActionError(Exception):
    """A step failed in a way that should be surfaced to the user/model."""


class DoneSignal(Exception):
    """Raised by the `done` step to end the pipeline cleanly."""


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
        """Install/refresh the overlay in the page from the runner's state.

        The renderer is installed once into the page and driven by state, with
        a MutationObserver re-applying it when the app replaces its DOM (loading
        screens, soft navigation) outside a navigate step. Bars render in the
        top layer (Popover API) so page CSS can't re-anchor or clip them.
        """
        if not self._overlay:
            return
        state = {
            "title": self._overlay.get("title"),
            "description": self._overlay.get("description"),
        }
        expr = f"""
(() => {{
  window.__demoRecordOverlayState = {json.dumps(state)};
  if (!window.__demoRecordRenderOverlay) {{
    const justify = a => a === 'left' ? 'flex-start' : a === 'right' ? 'flex-end' : 'center';
    const BAR_CSS = {json.dumps(_OVERLAY_BAR_CSS)};
    const TITLE_CSS = {json.dumps(_TITLE_LINE_CSS)};
    const DESC_CSS = {json.dumps(_DESCRIPTION_LINE_CSS)};
    window.__demoRecordRenderOverlay = (state) => {{
      const title = state.title && state.title.text ? state.title : null;
      const desc = state.description && state.description.text ? state.description : null;
      let root = document.getElementById('demo-record-overlay');
      if (!root) {{
        root = document.createElement('div');
        root.id = 'demo-record-overlay';
        root.style.cssText = 'position:fixed;inset:0;pointer-events:none;'
          + 'font-family:-apple-system,Segoe UI,Roboto,sans-serif;';
        (document.body || document.documentElement).appendChild(root);
      }}
      const rendered = [];
      for (const position of ['top', 'bottom']) {{
        const slotTitle = title && title.position === position ? title : null;
        const slotDesc = desc && desc.position === position ? desc : null;
        if (!slotTitle && !slotDesc) continue;
        const bar = document.createElement('div');
        bar.style.cssText = BAR_CSS;
        const addLine = (text, kind) => {{
          const line = document.createElement('div');
          line.style.cssText = kind === 'title' ? TITLE_CSS : DESC_CSS;
          line.textContent = text;
          bar.appendChild(line);
        }};
        if (slotTitle) addLine(slotTitle.text, 'title');
        if (slotDesc) addLine(slotDesc.text, 'description');
        const wrap = document.createElement('div');
        // Neutralize the UA popover defaults (white Canvas background, solid
        // border, fit-content size, auto margins) before positioning.
        wrap.style.cssText = 'position:fixed;inset:auto;left:0;right:0;margin:0;'
          + 'width:auto;height:auto;background:none;border:none;overflow:visible;color:inherit;'
          + 'display:flex;padding:20px 28px;box-sizing:border-box;pointer-events:none;'
          + (position === 'top' ? 'top:0;' : 'bottom:0;')
          + 'justify-content:' + justify((slotTitle || slotDesc).align) + ';';
        wrap.appendChild(bar);
        rendered.push({{ position, wrap }});
      }}
      root.replaceChildren(...rendered.map(r => r.wrap));
      // Top layer (Popover API): page styles like transforms on body would
      // otherwise re-anchor position:fixed and clip the bars.
      for (const r of rendered) {{
        try {{
          r.wrap.popover = 'manual';
          r.wrap.showPopover();
        }} catch (e) {{ /* older Chromium: fall back to normal fixed positioning */ }}
      }}
    }};
  }}
  if (!window.__demoRecordOverlayObserver) {{
    window.__demoRecordOverlayObserver = new MutationObserver(() => {{
      const st = window.__demoRecordOverlayState;
      const hasContent = st && ((st.title && st.title.text) || (st.description && st.description.text));
      const root = document.getElementById('demo-record-overlay');
      if (hasContent && (!root || !root.childElementCount)) {{
        requestAnimationFrame(() => window.__demoRecordRenderOverlay(window.__demoRecordOverlayState));
      }}
    }});
    window.__demoRecordOverlayObserver.observe(document.documentElement, {{ childList: true, subtree: true }});
  }}
  window.__demoRecordRenderOverlay(window.__demoRecordOverlayState);
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

    # -- click/type feedback (ring + ripples) -----------------------------------

    async def _ensure_fx(self) -> None:
        value = await self._eval(_FX_SCRIPT)
        if not value or not value.get("ok"):
            raise ActionError("could not inject click feedback effects into the page")

    async def _fx_ring(self, selector: str, duration_ms: int, spotlight: bool = False) -> None:
        await self._ensure_fx()
        expr = (
            f"window.__demoFx.ring({json.dumps(selector)}, "
            f"{{durationMs: {int(duration_ms)}, spotlight: {str(spotlight).lower()}}})"
        )
        value = await self._eval(expr)
        if not value or not value.get("ok"):
            raise ActionError(f'selector "{selector}" not found for highlight')

    async def _fx_ripple(self, selector: str) -> None:
        await self._ensure_fx()
        value = await self._eval(f"window.__demoFx.ripple({json.dumps(selector)})")
        if not value or not value.get("ok"):
            raise ActionError(f'selector "{selector}" not found for ripple')

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

    async def back(self, wait_seconds: float) -> None:
        client, session_id = await self._cdp()
        history = await client.send.Page.getNavigationHistory(session_id=session_id)
        idx = history.get("currentIndex", 0)
        if idx <= 0:
            raise ActionError("no previous page in history to go back to")
        entry_id = history["entries"][idx - 1]["id"]
        await client.send.Page.navigateToHistoryEntry(
            params={"entryId": entry_id}, session_id=session_id
        )
        await self._wait_for_load()
        if self._overlay:
            await self._render_overlay()
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)

    async def highlight(self, selector: str, duration_seconds: float, spotlight: bool) -> None:
        await self._scroll_into_center(selector)
        await self._fx_ring(selector, max(int(duration_seconds * 1000), 300), spotlight)
        if duration_seconds > 0:
            await asyncio.sleep(duration_seconds)

    async def click(self, selector: str) -> None:
        await self._scroll_into_center(selector)
        # Visible choreography before the real click: the element lights up,
        # waves spread from its center, then the click lands.
        await self._fx_ring(selector, CLICK_RING_MS)
        await asyncio.sleep(RING_LEAD_S)
        await self._fx_ripple(selector)
        await asyncio.sleep(RIPPLE_LEAD_S)
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
        # The click may have replaced the page under the ring; drop it now
        # instead of letting it float over new content until its timeout.
        try:
            await self._eval("window.__demoFx.clear()")
        except ActionError:  # noqa: BLE001 - a navigation already wiped the fx
            pass

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

        # Ring the field for as long as the typing takes, so the recording
        # shows which element is receiving the text.
        type_duration_ms = len(text) * type_delay_ms + 900
        await self._fx_ring(selector, type_duration_ms)

        client, session_id = await self._cdp()
        for ch in text:
            # Every character goes through Input.insertText: it fires a real
            # input event that React-controlled inputs accept. Plain
            # dispatchKeyEvent with only type+text is dropped by React's
            # synthetic event layer (the DOM keeps the char, state does not,
            # so controlled inputs revert to empty).
            await client.send.Input.dispatchKeyEvent(
                params={"type": "keyDown", "key": ch}, session_id=session_id
            )
            await client.send.Input.insertText(params={"text": ch}, session_id=session_id)
            await client.send.Input.dispatchKeyEvent(
                params={"type": "keyUp", "key": ch}, session_id=session_id
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

    async def assert_text(self, text: str, case_sensitive: bool = False) -> None:
        expr = """
(() => {
  const want = %s;
  const caseSensitive = %s;
  const url = location.href;
  const body = document.body ? document.body.innerText : '';
  const re = /\\s+/g;
  const norm = s => { const t = s.replace(re, ' '); return caseSensitive ? t : t.toLowerCase(); };
  // Plain substring first (exact presence).
  if (body.includes(want) || norm(body).includes(norm(want))) return { found: true, url, snippet: '' };
  return { found: false, url, snippet: norm(body).slice(0, 200) };
})()
""" % (json.dumps(text), json.dumps(case_sensitive))
        value = await self._eval(expr)
        if not value or not value.get("found"):
            url = (value or {}).get("url", "unknown url")
            snippet = (value or {}).get("snippet", "")
            raise ActionError(
                f'assert_text failed: "{text}" not found at {url}. page says: "{snippet}..."'
            )

    async def assert_value(self, selector: str, value: str) -> None:
        await self._scroll_into_center(selector)
        expr = f"""
(() => {{
  const el = document.querySelector({json.dumps(selector)});
  if (!el) return {{ ok: false, reason: 'selector not found', url: location.href, actual: null }};
  if (el.value === undefined) return {{ ok: false, reason: 'element has no value', url: location.href, actual: null }};
  return {{ ok: true, url: location.href, actual: String(el.value) }};
}})()
"""
        result = await self._eval(expr)
        if not result or not result.get("ok"):
            reason = (result or {}).get("reason", "unknown")
            url = (result or {}).get("url", "unknown url")
            raise ActionError(f'assert_value failed on "{selector}" ({reason}) at {url}')
        actual = result.get("actual") or ""
        if actual != value:
            raise ActionError(
                f'assert_value failed: "{selector}" holds "{actual}", expected "{value}" at {result.get("url")}'
            )

    async def done(self) -> None:
        raise DoneSignal()

    # -- dispatch ------------------------------------------------------------

    async def run(self, step: Step) -> None:
        action = step.action
        if action == "navigate":
            await self.navigate(step.url, step.wait_seconds)
        elif action == "back":
            await self.back(step.wait_seconds)
        elif action == "wait":
            await self.wait(step.seconds)
        elif action == "click":
            await self.click(step.selector)
        elif action == "highlight":
            await self.highlight(step.selector, step.duration_seconds, step.spotlight)
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
            await self.assert_text(step.text, step.case_sensitive)
        elif action == "assert_value":
            await self.assert_value(step.selector, step.value)
        elif action == "done":
            await self.done()
        else:  # pragma: no cover - pydantic discriminates, so unreachable
            raise ActionError(f"unknown action: {action}")

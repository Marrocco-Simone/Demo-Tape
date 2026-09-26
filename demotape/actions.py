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
import re
from pathlib import Path
from typing import Any

from browser_use.browser.session import BrowserSession, CDPSession

from demotape.spec import Step

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
# Click choreography: the ring marks the target, then the ripples start in
# the same instant as the real click.
CLICK_RING_MS = 800
RING_LEAD_S = 0.2

_FX_SCRIPT = f"""
(() => {{
  if (!window.__demoFx) {{
  if (!document.getElementById('demotape-fx-style')) {{
    const style = document.createElement('style');
    style.id = 'demotape-fx-style';
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
      ring.id = 'demotape-fx-ring';
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
          + 'animation:demoRipple 0.5s ease-out forwards;'
          + 'animation-delay:' + (i * 0.08) + 's;opacity:0;'
          + 'transform:translate(-50%,-50%);';
        (document.body || document.documentElement).appendChild(wave);
        setTimeout(() => wave.remove(), 700 + i * 80);
      }}
      return {{ ok: true }};
    }},
    clear() {{
      const ring = document.getElementById('demotape-fx-ring');
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


_INPUT_VALUE_FORMATS = {
    "date": r"\d{4}-\d{2}-\d{2}",
    "time": r"\d{2}:\d{2}(:\d{2})?",
    "datetime-local": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?",
    "month": r"\d{4}-\d{2}",
    "week": r"\d{4}-W\d{2}",
}


def _is_input_value(input_type: object, text: str) -> bool:
    pattern = _INPUT_VALUE_FORMATS.get(str(input_type))
    return pattern is not None and re.fullmatch(pattern, text) is not None


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
  window.__demotapeOverlayState = {json.dumps(state)};
  if (!window.__demotapeRenderOverlay) {{
    const justify = a => a === 'left' ? 'flex-start' : a === 'right' ? 'flex-end' : 'center';
    const BAR_CSS = {json.dumps(_OVERLAY_BAR_CSS)};
    const TITLE_CSS = {json.dumps(_TITLE_LINE_CSS)};
    const DESC_CSS = {json.dumps(_DESCRIPTION_LINE_CSS)};
    window.__demotapeRenderOverlay = (state) => {{
      const title = state.title && state.title.text ? state.title : null;
      const desc = state.description && state.description.text ? state.description : null;
      let root = document.getElementById('demotape-overlay');
      if (!root) {{
        root = document.createElement('div');
        root.id = 'demotape-overlay';
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
  if (!window.__demotapeOverlayObserver) {{
    window.__demotapeOverlayObserver = new MutationObserver(() => {{
      const st = window.__demotapeOverlayState;
      const hasContent = st && ((st.title && st.title.text) || (st.description && st.description.text));
      const root = document.getElementById('demotape-overlay');
      if (hasContent && (!root || !root.childElementCount)) {{
        requestAnimationFrame(() => window.__demotapeRenderOverlay(window.__demotapeOverlayState));
      }}
    }});
    window.__demotapeOverlayObserver.observe(document.documentElement, {{ childList: true, subtree: true }});
  }}
  window.__demotapeRenderOverlay(window.__demotapeOverlayState);
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

    async def say(self, title: str, description: str, position: str, align: str) -> None:
        """Set both bars as one text change.

        Unlike the title-then-description pair convention (two renders, paired
        only when adjacent), `say` writes both slots and renders once - so the
        pair can never be split by an action in between, and a stale bar from
        an earlier chapter can't survive beside the new text.
        """
        self._overlay["title"] = {"text": title, "position": position, "align": align}
        self._overlay["description"] = {"text": description, "position": position, "align": align}
        await self._render_overlay()

    def narration_text(self) -> str:
        """The text a narration voice speaks for the current overlay state.

        The description carries the narration; the title is spoken only when
        it is the sole text on screen, so a title+description pair reads as
        one spoken beat with no duplicated headline.
        """
        description = (self._overlay.get("description") or {}).get("text", "").strip()
        if description:
            return description
        return (self._overlay.get("title") or {}).get("text", "").strip()

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

    async def _wait_for_page_ready(
        self,
        timeout_s: float = 15.0,
        min_index: int | None = None,
        max_index: int | None = None,
    ) -> None:
        """Wait for a navigation to commit and the page to settle.

        Page.navigate and history-entry navigation resolve when the navigation
        starts, so document.readyState can still describe the outgoing document.
        Commit is detected by two signals: a fresh document (no __demotapeToken)
        whose readyState is complete, or - for same-document navigation, where
        the document and the token never go away - the session's history index
        passing `min_index` (navigations move it forward) or `max_index`
        (history.back moves it backward). SPA routers and bfcache restores
        change history without reloading, and unlike a URL comparison an index
        also works when two consecutive entries share the same URL. After the
        index matches, two consecutive complete polls give the router a beat
        to react. A navigation that never commits times out.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        settled = 0
        while True:
            try:
                state = await self._eval(
                    "({ href: location.href, complete: document.readyState === 'complete',"
                    " fresh: window.__demotapeToken === undefined })"
                )
            except ActionError:  # noqa: BLE001 - the old execution context dies mid-navigation
                state = None
                settled = 0
            if state and state.get("complete"):
                if state.get("fresh"):
                    return
                if min_index is not None or max_index is not None:
                    try:
                        client, session_id = await self._cdp()
                        history = await client.send.Page.getNavigationHistory(
                            session_id=session_id
                        )
                        idx = history.get("currentIndex", -1)
                        reached = (min_index is not None and idx >= min_index) or (
                            max_index is not None and idx <= max_index
                        )
                        if reached:
                            settled += 1
                            if settled >= 2:
                                return
                            await asyncio.sleep(0.1)
                            continue
                    except ActionError:  # noqa: BLE001
                        pass
                    settled = 0
            if loop.time() > deadline:
                raise ActionError(f"page did not finish loading within {timeout_s:.0f}s")
            await asyncio.sleep(0.15)

    async def _plant_navigation_token(self) -> None:
        """Mark the outgoing document so _wait_for_page_ready can detect the new one."""
        try:
            await self._eval("window.__demotapeToken = 1")
        except ActionError:  # noqa: BLE001 - context can already be gone
            pass

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
        history = await client.send.Page.getNavigationHistory(session_id=session_id)
        current_index = history.get("currentIndex", 0)
        await self._plant_navigation_token()
        await client.send.Page.navigate(params={"url": url}, session_id=session_id)
        # The history index is the commit signal that works for every
        # navigation shape: cross-document, same-document (SPA routers) and
        # same-URL reloads alike - href matching cannot decide a reload, and
        # a fragment-only change never produces a new document. Redirect
        # chains can land past the expected entry, hence "at least".
        await self._wait_for_page_ready(min_index=current_index + 1)
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
        entry = history["entries"][idx - 1]
        await self._plant_navigation_token()
        await client.send.Page.navigateToHistoryEntry(
            params={"entryId": entry["id"]}, session_id=session_id
        )
        # Same-document back (SPA routers, bfcache) keeps the document alive,
        # so the token never clears; the history index is the landing signal,
        # and unlike the entry's URL it also works when two consecutive
        # history entries share the same URL (a second back in a hash router,
        # pushState to the same path, ...).
        await self._wait_for_page_ready(max_index=idx - 1)
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
        # The element lights up, then the waves spread from its center as the
        # click lands.
        await self._fx_ring(selector, CLICK_RING_MS)
        await asyncio.sleep(RING_LEAD_S)
        await self._fx_ripple(selector)
        if not await self._press_at_center(selector):
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
    if ('value' in el) {{
      // A plain `el.value = ''` is invisible to React: the component keeps its
      // state and restores the old text on the next keystroke, so the typed
      // text lands appended. Going through the native setter and firing a real
      // input event makes the controlled component drop the old value.
      const proto = Object.getPrototypeOf(el);
      const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
      if (descriptor && descriptor.set) descriptor.set.call(el, '');
      else el.value = '';
      el.dispatchEvent(new Event('input', {{ bubbles: true }}));
    }} else {{
      el.textContent = '';
    }}
  }}
  return {{ ok: true, keyed: ['time','date','datetime-local','month','week'].includes(el.type), type: el.type }};
}})()
"""
        value = await self._eval(focus_expr)
        if not value or not value.get("ok"):
            raise ActionError(f'could not focus selector "{selector}" for typing')

        # Ring the field for as long as the typing takes, so the recording
        # shows which element is receiving the text.
        type_duration_ms = len(text) * type_delay_ms + 900
        await self._fx_ring(selector, type_duration_ms)

        keyed = bool(value.get("keyed"))
        if keyed and _is_input_value(value.get("type"), text):
            # The text is already the field's own value format: set it at once,
            # without keys. Key events follow the browser locale's field order,
            # and a tab can stop receiving them after browser UI takes focus.
            await asyncio.sleep(len(text) * type_delay_ms / 1000.0)
            await self._set_value(selector, text)
            return
        client, session_id = await self._cdp()
        for ch in text:
            if keyed:
                # Date and time fields take characters only from keypress events,
                # which Chromium emits for a keyDown that carries text; they
                # ignore Input.insertText.
                await client.send.Input.dispatchKeyEvent(
                    params={"type": "keyDown", "key": ch, "text": ch, "unmodifiedText": ch},
                    session_id=session_id,
                )
            else:
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

    async def _set_value(self, selector: str, text: str) -> None:
        expr = f"""
(() => {{
  const el = document.querySelector({json.dumps(selector)});
  if (!el) return false;
  const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
  if (descriptor && descriptor.set) descriptor.set.call(el, {json.dumps(text)});
  else el.value = {json.dumps(text)};
  el.dispatchEvent(new Event('input', {{ bubbles: true }}));
  el.dispatchEvent(new Event('change', {{ bubbles: true }}));
  return true;
}})()
"""
        if not await self._eval(expr):
            raise ActionError(f'could not set the value of "{selector}"')

    async def _press_at_center(self, selector: str) -> bool:
        """Click the center of `selector` with a full pointer sequence.

        `el.click()` fires the click event alone, so a control that listens for
        mousedown - a menu row that has to act before the input it belongs to
        loses focus - never runs. Pointer events are dispatched too, because
        Radix, dnd-kit and React Native Web Pressable bind onPointerDown etc.;
        the real browser order is pointerdown -> mousedown -> pointerup ->
        mouseup -> click. The events are dispatched from the page instead of
        through CDP input: CDP mouse events do not reach the page while the
        recorder drives it, and a bubbling event is what the framework
        listens for either way.

        Returns False when the element is gone, so the caller can report it.
        """
        box = await self._measure_box(selector)
        if not box:
            return False
        x = box["x"] + box["w"] / 2
        y = box["y"] + box["h"] / 2
        expr = f"""
(() => {{
  const el = document.querySelector({json.dumps(selector)});
  if (!el) return {{ ok: false }};
  const at = {{ bubbles: true, cancelable: true, view: window,
    clientX: {x}, clientY: {y}, button: 0, buttons: 1 }};
  if (typeof PointerEvent === 'function') {{
    el.dispatchEvent(new PointerEvent('pointerdown',
      {{ ...at, pointerId: 1, pointerType: 'mouse', isPrimary: true }}));
  }}
  el.dispatchEvent(new MouseEvent('mousedown', at));
  if (typeof PointerEvent === 'function') {{
    el.dispatchEvent(new PointerEvent('pointerup',
      {{ ...at, buttons: 0, pointerId: 1, pointerType: 'mouse', isPrimary: true }}));
  }}
  el.dispatchEvent(new MouseEvent('mouseup', {{ ...at, buttons: 0 }}));
  el.dispatchEvent(new MouseEvent('click', {{ ...at, buttons: 0 }}));
  return {{ ok: true }};
}})()
"""
        value = await self._eval(expr)
        return bool(value and value.get("ok"))

    async def _measure_box(self, selector: str) -> dict[str, Any] | None:
        """Viewport-relative bounding box of `selector`, or None if missing."""
        return await self._eval(
            f"""
(() => {{
  const el = document.querySelector({json.dumps(selector)});
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return {{
    x: r.x, y: r.y, w: r.width, h: r.height,
    visible: r.top >= 0 && r.left >= 0
      && r.bottom <= window.innerHeight && r.right <= window.innerWidth,
  }};
}})()
"""
        )

    async def _drag_point(
        self, box: dict[str, float], fraction: float, axis: str
    ) -> tuple[float, float]:
        """Point at `fraction` along the box, clamped 1px inside so boundary
        events hit the element instead of its neighbor."""
        if axis == "y":
            px = box["x"] + box["w"] / 2
            py = box["y"] + box["h"] * fraction
        else:
            px = box["x"] + box["w"] * fraction
            py = box["y"] + box["h"] / 2
        px = min(max(px, box["x"] + 1), box["x"] + box["w"] - 1)
        py = min(max(py, box["y"] + 1), box["y"] + box["h"] - 1)
        return px, py

    async def drag(
        self,
        selector: str,
        to: float,
        from_: float | None,
        to_selector: str | None,
        duration_seconds: float,
        axis: str,
    ) -> None:
        """Press on `selector` and drag the pointer to `to_selector` (or across
        `selector` itself) with real CDP mouse events.

        React Native Web sliders (and similar pointer-driven controls) read the
        value from the responder system's pointer position, so el.click() does
        nothing for them — and they snap the value to the press point, which is
        why same-element drags start at the edge by default. Drag-and-drop
        lists reorder on the same press → interpolated moves → release sequence.
        """
        if from_ is None:
            from_ = 0.5 if to_selector and to_selector != selector else 0.0
        await self._scroll_into_center(selector)

        press_box = await self._measure_box(selector)
        if not press_box or press_box["w"] <= 0 or press_box["h"] <= 0:
            raise ActionError(f'selector "{selector}" not found (or has no size) for drag')

        release_on_self = not to_selector or to_selector == selector
        if not release_on_self:
            to_sel: str = to_selector
            release_box = await self._measure_box(to_sel)
            if not release_box or release_box["w"] <= 0 or release_box["h"] <= 0:
                raise ActionError(
                    f'selector "{to_sel}" not found (or has no size) for drag release'
                )
            if not release_box["visible"]:
                await self._scroll_into_center(to_sel)
                release_box = await self._measure_box(to_sel)
                if (
                    not release_box
                    or release_box["w"] <= 0
                    or release_box["h"] <= 0
                    or not release_box["visible"]
                ):
                    raise ActionError(
                        f'cannot drag between "{selector}" and "{to_sel}": '
                        "they do not fit in the viewport at the same time"
                    )
            # Scrolling the drop target in can push the press target out of
            # view; the press point must then be re-derived or the press event
            # lands outside the window and hits nothing.
            press_box = await self._measure_box(selector)
            if (
                not press_box
                or press_box["w"] <= 0
                or press_box["h"] <= 0
                or not press_box["visible"]
            ):
                raise ActionError(
                    f'cannot drag between "{selector}" and "{to_sel}": '
                    "they do not fit in the viewport at the same time"
                )
        else:
            release_box = press_box

        # Rings go on after the geometry is final: a drop target scrolled in
        # from off-screen would otherwise carry a ring parked at its old,
        # possibly off-screen position.
        ring_ms = max(int(duration_seconds * 1000), 300)
        await self._fx_ring(selector, ring_ms)
        if not release_on_self:
            await self._fx_ring(to_selector, ring_ms)

        x0, y0 = await self._drag_point(press_box, from_, axis)
        x1, y1 = await self._drag_point(release_box, to, axis)

        client, session_id = await self._cdp()
        await client.send.Input.dispatchMouseEvent(
            params={"type": "mouseMoved", "x": x0, "y": y0, "button": "none", "buttons": 0},
            session_id=session_id,
        )
        await asyncio.sleep(0.05)
        await client.send.Input.dispatchMouseEvent(
            params={
                "type": "mousePressed",
                "x": x0,
                "y": y0,
                "button": "left",
                "clickCount": 1,
                "buttons": 1,
            },
            session_id=session_id,
        )
        await asyncio.sleep(0.08)
        moves = min(60, max(12, int(duration_seconds / 0.03)))
        step_delay = duration_seconds / moves
        for i in range(1, moves + 1):
            # Ease-in-out: a hand accelerates into the drag and settles at the
            # end; constant velocity reads as robotic on video.
            t = i / moves
            e = 4 * t * t * t if t < 0.5 else 1 - ((-2 * t + 2) ** 3) / 2
            await client.send.Input.dispatchMouseEvent(
                params={
                    "type": "mouseMoved",
                    "x": x0 + (x1 - x0) * e,
                    "y": y0 + (y1 - y0) * e,
                    "button": "left",
                    "buttons": 1,
                },
                session_id=session_id,
            )
            if step_delay > 0:
                await asyncio.sleep(step_delay)
        await client.send.Input.dispatchMouseEvent(
            params={
                "type": "mouseReleased",
                "x": x1,
                "y": y1,
                "button": "left",
                "clickCount": 1,
                "buttons": 0,
            },
            session_id=session_id,
        )

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

    async def upload(self, selector: str, path: str) -> None:
        file_path = Path(path).expanduser().resolve()
        if not file_path.is_file():
            raise ActionError(f'upload failed: file "{file_path}" does not exist')
        client, session_id = await self._cdp()
        document = await client.send.DOM.getDocument(params={"depth": 0}, session_id=session_id)
        found = await client.send.DOM.querySelector(
            params={"nodeId": document["root"]["nodeId"], "selector": selector},
            session_id=session_id,
        )
        node_id = found.get("nodeId") if found else None
        if not node_id:
            raise ActionError(f'upload failed: selector "{selector}" not found')
        await client.send.DOM.setFileInputFiles(
            params={"files": [str(file_path)], "nodeId": node_id}, session_id=session_id
        )

    async def scroll(self, selector: str) -> None:
        await self._scroll_into_center(selector)

    async def assert_text(self, text: str, case_sensitive: bool = False) -> None:
        expr = f"""
(() => {{
  const want = {json.dumps(text)};
  const caseSensitive = {json.dumps(case_sensitive)};
  const url = location.href;
  const body = document.body ? document.body.innerText : '';
  const norm = s => s.replace(/\\s+/g, ' ').toLowerCase();
  // case_sensitive means exact matching - no case folding, no whitespace
  // collapsing. The default folds both, because CSS text-transform (e.g.
  // uppercase labels) and wrapping change what innerText returns.
  const found = caseSensitive ? body.includes(want) : norm(body).includes(norm(want));
  if (found) return {{ found: true, url, snippet: '' }};
  return {{ found: false, url, snippet: norm(body).slice(0, 200) }};
}})()
"""
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
        elif action == "drag":
            await self.drag(
                step.selector, step.to, step.from_, step.to_selector, step.duration_seconds, step.axis
            )
        elif action == "select":
            await self.select(step.selector, step.option)
        elif action == "upload":
            await self.upload(step.selector, step.path)
        elif action == "scroll":
            await self.scroll(step.selector)
        elif action == "title":
            await self.title(step.text, step.position, step.align)
        elif action == "description":
            await self.description(step.text, step.position, step.align)
        elif action == "say":
            await self.say(step.title, step.description, step.position, step.align)
        elif action == "assert_text":
            await self.assert_text(step.text, step.case_sensitive)
        elif action == "assert_value":
            await self.assert_value(step.selector, step.value)
        elif action == "done":
            await self.done()
        else:  # pragma: no cover - pydantic discriminates, so unreachable
            raise ActionError(f"unknown action: {action}")

"""Runner: launch Chromium, record video, execute the step pipeline, report result.

The output contract is intentionally strict so a calling agent or human can act
on it without inspecting anything else:

  success -> exit 0, prints:  VIDEO_OK <path>/video.mp4
  failure -> exit 1, prints:  ERROR step N <action>: <message>
                              LAST_SCREENSHOT <path>/error.png
The partial video is saved on failure so the breakage point is visible.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import platform
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from browser_use.browser.events import BrowserStoppedEvent
from browser_use.browser.profile import ViewportSize
from browser_use.browser.session import BrowserSession
from browser_use.browser.video_recorder import VideoRecorderService

from demotape.actions import ActionError, ActionRunner, DoneSignal
from demotape.spec import DemoSpec
from demotape.tts import NarrationClip, Narrator, mix_clips

logger = logging.getLogger("demotape")

# Narration steps pair into one visual change; used by the step-loop pacing.
OVERLAY_ACTIONS = ("title", "description")


@dataclass
class RunResult:
    status: str  # "ok" | "error" | "interrupted" | "stopped"
    video_path: Path
    error: str | None = None
    last_screenshot: Path | None = None
    stop_reason: str | None = None


class _ScreencastRecorder:
    """Drive browser-use's VideoRecorderService from a CDP screencast.

    Screencast is repaint-driven and ack-gated, so two rules keep it honest:
    ack before anything else (Chromium pauses streaming until acked), and
    never encode on the event loop. The worker thread expands idle gaps into
    duplicate frames; expanding inline once blocked the loop for ~0.9s per
    pause and ate the first half of every transition. Wall-clock padding of
    the tail happens in stop().
    """

    def __init__(self, browser: BrowserSession, output_path: Path, size: ViewportSize, framerate: int) -> None:
        self._browser = browser
        self._size = size
        self._framerate = framerate
        self._recorder = VideoRecorderService(output_path=output_path, size=size, framerate=framerate)
        self._client: Any = None
        self._session_id: str | None = None
        self._queue: queue.Queue[tuple[str | None, int, str] | None] = queue.Queue(maxsize=framerate * 4)
        self._worker: threading.Thread | None = None
        self._pending: set[asyncio.Task] = set()
        self._last_data: str | None = None
        self._last_ts: float | None = None
        self._written = 0
        self._dropped = 0
        self._started_at = 0.0

    async def start(self) -> None:
        cdp_session = await self._browser.get_or_create_cdp_session()
        self._client = cdp_session.cdp_client
        self._session_id = cdp_session.session_id

        self._recorder.start()
        if not self._recorder._is_active:
            raise RuntimeError(
                'video recorder failed to start - install video deps with: pip install "browser-use[video]"'
            )

        self._client.register.Page.screencastFrame(self._on_frame)
        await self._client.send.Page.startScreencast(
            params={
                "format": "png",
                "quality": 90,
                "maxWidth": self._size["width"],
                "maxHeight": self._size["height"],
                "everyNthFrame": 1,
            },
            session_id=self._session_id,
        )
        self._worker = threading.Thread(target=self._drain, daemon=True)
        self._worker.start()
        self._started_at = time.monotonic()

    def _on_frame(self, event, session_id: str | None) -> None:
        if self._session_id and session_id != self._session_id:
            return
        # Ack first, always: Chromium streams the next frame only after the
        # previous one is acked, and a delayed ack eats the animation.
        self._ack_soon(event)
        data = event["data"]
        ts = (event.get("metadata") or {}).get("timestamp")
        gap = 0
        if ts is not None and self._last_ts is not None:
            elapsed = ts - self._last_ts
            # Repaints can exceed the target framerate during animations;
            # sub-frame frames are skipped and the next kept frame covers them.
            if elapsed < 0.85 / self._framerate:
                return
            # Chromium only repaints on change, so an idle second sends no
            # frames. The worker repeats the previous frame across the gap so
            # pauses survive in the video.
            if self._last_data is not None:
                gap = int(round(elapsed * self._framerate)) - 1
                gap = max(0, min(gap, self._framerate * 30))
        try:
            self._queue.put_nowait((self._last_data, gap, data))
        except queue.Full:
            self._dropped += 1
            return
        if ts is not None:
            self._last_ts = ts
        self._last_data = data

    def _drain(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                last_data, gap, data = item
                if gap and last_data is not None:
                    for _ in range(gap):
                        self._recorder.add_frame(last_data)
                        self._written += 1
                self._recorder.add_frame(data)
                self._written += 1
        except Exception:  # noqa: BLE001
            logger.exception("video worker thread died")

    def _ack_soon(self, event) -> None:
        task = asyncio.ensure_future(self._ack(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _ack(self, event) -> None:
        try:
            await self._client.send.Page.screencastFrameAck(
                params={"sessionId": event["sessionId"]}, session_id=self._session_id
            )
        except Exception:  # noqa: BLE001 - ack failures must not break recording
            pass

    def _fill_to(self, now: float) -> None:
        """Pad the tail so the file matches elapsed wall-clock.

        Screencast emits nothing once the page goes static, so the stretch
        between the last frame and the end of the run must be filled with the
        final frame or the closing narration collapses. Under encoder
        back-pressure fills are dropped (counted in _dropped), which can leave
        the file slightly shorter than wall-clock.
        """
        if self._last_data is None:
            return
        target = int((now - self._started_at) * self._framerate)
        while self._written < target:
            try:
                self._queue.put_nowait((self._last_data, 0, self._last_data))
            except queue.Full:
                self._dropped += 1
                return
            self._written += 1

    def elapsed(self) -> float:
        """Seconds since the screencast started (the video's time base)."""
        return time.monotonic() - self._started_at

    async def stop(self) -> Path:
        if self._session_id:
            try:
                await self._client.send.Page.stopScreencast(session_id=self._session_id)
            except Exception:  # noqa: BLE001
                pass
        # Let in-flight frames land before padding the tail.
        await asyncio.sleep(0.15)
        self._fill_to(time.monotonic())
        # Bounded: a full or dead worker must not hang the event loop here.
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        loop = asyncio.get_running_loop()
        if self._worker is not None:
            await loop.run_in_executor(None, lambda: self._worker.join(timeout=5.0))
        if self._dropped:
            logger.warning("video: %d frames dropped (encoder fell behind)", self._dropped)
        await loop.run_in_executor(None, self._recorder.stop_and_save)
        return self._recorder.output_path


def _find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _mux_narration(video_path: Path, clips: list[NarrationClip], ffmpeg: str) -> None:
    """Mix narration clips into `video_path` as its audio track.

    Best-effort by design: a finished silent video beats a half-muxed one, so
    any failure here is a warning, never a run failure. ffprobe measures the
    video so the narration timeline matches the file, not the wall clock
    (dropped encoder frames can make those differ).
    """
    try:
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            logger.warning("narration skipped: ffprobe not found (install ffmpeg)")
            return
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, check=True,
        )
        duration_s = float(probe.stdout.strip())
        wav_path = video_path.parent / "narration.wav"
        mix_clips(clips, duration_s, wav_path)
        muxed = video_path.with_name(video_path.stem + ".mux.mp4")
        subprocess.run(
            [ffmpeg, "-y", "-i", str(video_path), "-i", str(wav_path),
             "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
             "-movflags", "+faststart", str(muxed)],
            capture_output=True, check=True,
        )
        muxed.replace(video_path)
        wav_path.unlink(missing_ok=True)
        logger.info("narration muxed into %s", video_path)
    except Exception:  # noqa: BLE001 - narration must not break the output contract
        logger.warning("could not mux narration audio", exc_info=True)


async def _save_error_screenshot(browser: BrowserSession, path: Path) -> Path | None:
    try:
        data: bytes = await browser.take_screenshot()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path
    except Exception:  # noqa: BLE001 - a failed screenshot must not mask the real error
        logger.warning("could not capture error screenshot", exc_info=True)
        return None


def _version_sorted(paths: list[str]) -> list[str]:
    """Sort newest-first, numerically: chromium-1181 beats chromium-999."""
    return sorted(
        paths,
        key=lambda p: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p)],
        reverse=True,
    )


def _find_chromium() -> Path | None:
    """Locate a standalone Chromium, preferring it over system Chrome.

    browser-use's fallback only knows the old Playwright layout (Chromium.app);
    Playwright >=1.5x ships 'Google Chrome for Testing.app' instead, so without
    this resolver an installed Chromium is silently skipped.
    """
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        candidates = [
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            str(home / "Applications/Chromium.app/Contents/MacOS/Chromium"),
            str(home / "Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"),
            str(home / "Library/Caches/ms-playwright/chromium-*/chrome-mac*/*.app/Contents/MacOS/*"),
        ]
    elif system == "Linux":
        candidates = [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/local/bin/chromium",
            "/snap/bin/chromium",
            str(home / ".cache/ms-playwright/chromium-*/chrome-linux*/chrome"),
        ]
    else:  # Windows
        candidates = [
            r"C:\Program Files\Chromium\Application\chrome.exe",
            r"C:\Program Files (x86)\Chromium\Application\chrome.exe",
            str(home / r"AppData\Local\Chromium\Application\chrome.exe"),
            str(home / r"AppData\Local\ms-playwright\chromium-*\chrome-win*\chrome.exe"),
        ]
    for pattern in candidates:
        for match in _version_sorted(glob.glob(os.path.expandvars(os.path.expanduser(pattern)))):
            exe = Path(match)
            if exe.is_file() and os.access(exe, os.X_OK):
                return exe
    return None


def _create_quiet_profile() -> Path:
    """Fresh user-data-dir with every browser popup disabled.

    A default profile nags through recordings: save-password bubbles, password
    breach warnings, save-IBAN/card autofill prompts, translate bars, default
    browser and first-run promos. Prewriting Chromium's Preferences turns them
    all off before the first launch.
    """
    user_data_dir = Path(tempfile.mkdtemp(prefix="demotape-profile-"))
    default_dir = user_data_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs = {
        "credentials_enable_service": False,
        "credentials_enable_autosignin": False,
        "PasswordLeakDetectionEnabled": False,
        "autofill": {
            "credit_card_enabled": False,
            "profile_enabled": False,
            "iban_enabled": False,
        },
        "translate": {"enabled": False},
        "translate_blocked_languages": ["en"],
        "browser": {"has_seen_welcome_page": True, "check_default_browser": False},
        "distribution": {
            "make_chrome_default": False,
            "suppress_default_browser_prompt_for_new_chrome": True,
            "import_bookmarks": False,
            "import_search_engine": False,
            "skip_first_run_ui": True,
        },
        "sync_promo": {"show_on_first_run_allowed": False},
        "privacy_sandbox": {
            "m1": {"row_notice_acknowledged": True, "restricted_notice_acknowledged": True}
        },
        "default_apps_install_state": 3,
    }
    (default_dir / "Preferences").write_text(json.dumps(prefs))
    (user_data_dir / "First Run").write_text("")
    return user_data_dir


async def run_pipeline(spec: DemoSpec) -> RunResult:
    output_dir = Path(spec.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    legacy_steps = output_dir / "steps"
    if legacy_steps.exists():
        shutil.rmtree(legacy_steps)
    # Leftovers from an earlier run (a stale error.png or video.mp4, or a
    # steps/ dir from versions that wrote per-step screenshots) must not sit
    # beside this run's artifacts.
    for stale in ("error.png", "video.mp4"):
        stale_path = output_dir / stale
        if stale_path.exists():
            stale_path.unlink()
    video_path = output_dir / "video.mp4"
    size = ViewportSize(width=spec.viewport.width, height=spec.viewport.height)

    # In headed mode the browser UI (tab strip + address bar) takes the top of
    # the window, so a window the size of the viewport cuts off the bottom of
    # the page when watching live. Headless has no UI; sizes match exactly.
    window_height = spec.viewport.height + (120 if not spec.headless else 0)

    chromium_exe = _find_chromium()
    if chromium_exe is not None:
        logger.info("using Chromium at %s", chromium_exe)
    user_data_dir = _create_quiet_profile()

    narrator: Narrator | None = None
    ffmpeg_bin: str | None = None
    if spec.text_to_speech:
        ffmpeg_bin = _find_ffmpeg()
        if ffmpeg_bin is None:
            return RunResult(
                status="error",
                video_path=video_path,
                error="startup: text_to_speech requires ffmpeg on PATH (it muxes the "
                "narration audio track); install it with `brew install ffmpeg`",
            )
        narrator = Narrator(spec.tts_voice)

    # Narration clips with their offsets on the video's time base; mixed into
    # the file after the recorder finalizes. Defined before the try so the
    # finally-mux can never hit an unbound name after a startup failure.
    clips: list[NarrationClip] = []
    last_narrated = [""]

    browser = BrowserSession(
        headless=spec.headless,
        viewport={"width": spec.viewport.width, "height": spec.viewport.height},
        window_size={"width": spec.viewport.width, "height": window_height},
        user_data_dir=user_data_dir,
        # browser-use merges this with its own --disable-features list. The
        # breach warning ignores the Preferences-only kill switch, so the
        # feature flags are the reliable layer.
        args=[
            "--disable-features=PasswordLeakDetection,PasswordManagerOnboarding,"
            "AutofillEnableAccountWalletStorage,TranslateUI,DefaultBrowserPromptEnabled"
        ],
        # Prefer a standalone Chromium when one is installed (playwright cache
        # or /Applications); otherwise browser-use's fallback finds system
        # Chrome. executable_path is strict: no silent fallback beyond this.
        executable_path=chromium_exe,
        # Browser-use downloads its ad-block/cookie extensions at startup, which
        # noisily fails (and is useless for demos). Turn them off. These launch
        # flags exist for its agent/stealth behavior and each triggers Chrome's
        # "unsupported command-line flag" warning banner in headed mode.
        enable_default_extensions=False,
        ignore_default_args=[
            "--extensions-on-chrome-urls",
            "--disable-blink-features=AutomationControlled",
            "--allow-pre-commit-input",
        ],
        # Recording is owned by _ScreencastRecorder below; keep browser-use's
        # auto-recorder off.
        record_video_dir=None,
    )

    recorder: _ScreencastRecorder | None = None
    actions = ActionRunner(browser)

    # Stop machinery: Ctrl+C / SIGTERM, or the browser dying (user closed the
    # window), set a flag checked between steps. The video is finalized either
    # way. A second Ctrl+C hard-exits immediately.
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    stop_reason: str | None = None
    stop_interrupted = False

    def _request_stop(reason: str, interrupted: bool = False) -> None:
        nonlocal stop_reason, stop_interrupted
        if stop_reason is None:
            stop_reason = reason
            stop_interrupted = interrupted
        stop_event.set()

    def _on_browser_stopped(event: Any) -> None:
        reason = getattr(event, "reason", None) or "disconnected"
        _request_stop(f"browser was closed ({reason})")

    browser.event_bus.on(BrowserStoppedEvent, _on_browser_stopped)

    # A second Ctrl+C hard-exits immediately; the first is always graceful,
    # even when the browser-closed event already requested a stop (skipping
    # cleanup there would truncate the video).
    signal_count = 0

    def _on_signal() -> None:
        nonlocal signal_count
        signal_count += 1
        if signal_count >= 2:
            os._exit(130)  # second Ctrl+C: force exit without cleanup
        _request_stop("interrupted (Ctrl+C)", interrupted=True)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:  # pragma: no cover - non-unix
            pass

    try:
        try:
            # Model download (first run) and engine load happen before the
            # browser so a broken tts setup fails with the startup contract,
            # not mid-recording.
            if narrator is not None:
                await narrator.ensure_ready()
            await browser.start()
            recorder = _ScreencastRecorder(browser, video_path, size, framerate=30)
            await recorder.start()
        except Exception as exc:  # noqa: BLE001 - startup failures still honor the contract
            return RunResult(
                status="error",
                video_path=video_path,
                error=f"startup: {type(exc).__name__}: {exc}",
            )

        # Mixed into the file after the recorder finalizes (see finally).

        async def _narrate(step_index: int) -> bool:
            """Speak the current overlay text, holding the video while it plays.

            Returns True when a clip was placed (the caller then skips the
            inter-step pause - narration IS the pacing). Synthesis or engine
            failures degrade to a silent video: the recording is too valuable
            to lose over one bad clip.
            """
            if narrator is None or recorder is None:
                return False
            text = actions.narration_text()
            if not text or text == last_narrated[0]:
                return False
            last_narrated[0] = text
            try:
                clip = await narrator.synthesize(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "step %d: narration failed (%s); continuing silent", step_index, exc
                )
                return False
            clip.start_s = max(0.0, recorder.elapsed())
            clips.append(clip)
            logger.info("step %d: narrating %.1fs", step_index, len(clip.samples) / clip.sample_rate)
            await asyncio.sleep(len(clip.samples) / clip.sample_rate)
            return True

        for index, step in enumerate(spec.steps, start=1):
            if stop_event.is_set():
                logger.info("stopped before step %d", index)
                break
            try:
                await actions.run(step)
            except DoneSignal:
                logger.info("step %d: done", index)
                break
            except ActionError as exc:
                shot = await _save_error_screenshot(browser, output_dir / "error.png")
                return RunResult(
                    status="error",
                    video_path=video_path,
                    error=f"step {index} {step.action}: {exc}",
                    last_screenshot=shot,
                )
            except Exception as exc:  # noqa: BLE001 - CDP/JS failures still honor the contract
                shot = await _save_error_screenshot(browser, output_dir / "error.png")
                return RunResult(
                    status="error",
                    video_path=video_path,
                    error=f"step {index} {step.action}: {type(exc).__name__}: {exc}",
                    last_screenshot=shot,
                )
            if stop_event.is_set():
                break
            # Overlay pacing: a title/description change right after an action
            # applies immediately (no pause), so narration lands on the frame
            # the action produced. An adjacent title+description pair is one
            # visual change. A pause still applies between two overlay changes
            # (title -> title keeps its beat) and before a following action.
            next_step = spec.steps[index] if index < len(spec.steps) else None
            next_is_overlay = next_step is not None and next_step.action in OVERLAY_ACTIONS
            current_is_overlay = step.action in OVERLAY_ACTIONS
            # Narration speaks once the overlay pair is complete (the next
            # step is a real action or the end), replacing the inter-step
            # pause: the video holds until the clip finishes, so the next
            # text change can never cut the speech off.
            if current_is_overlay and not next_is_overlay and await _narrate(index):
                continue
            if next_is_overlay and (
                not current_is_overlay
                or (step.action == "title" and next_step.action == "description")
            ):
                continue
            if spec.delay_ms > 0:
                await asyncio.sleep(spec.delay_ms / 1000.0)

        if stop_reason is not None:
            shot = await _save_error_screenshot(browser, output_dir / "error.png")
            return RunResult(
                status="interrupted" if stop_interrupted else "stopped",
                video_path=video_path,
                stop_reason=stop_reason,
                last_screenshot=shot,
            )

        return RunResult(status="ok", video_path=video_path)
    finally:
        if recorder is not None:
            try:
                await recorder.stop()
                if clips and ffmpeg_bin is not None:
                    await asyncio.get_running_loop().run_in_executor(
                        None, _mux_narration, video_path, clips, ffmpeg_bin
                    )
            except Exception:  # noqa: BLE001
                logger.warning("failed to finalize video", exc_info=True)
        try:
            await browser.kill()
        except Exception:  # noqa: BLE001
            logger.warning("failed to close browser cleanly", exc_info=True)
        shutil.rmtree(user_data_dir, ignore_errors=True)


def run(spec: DemoSpec) -> RunResult:
    """Synchronous entry point."""
    return asyncio.run(run_pipeline(spec))

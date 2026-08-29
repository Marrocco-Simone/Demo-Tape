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
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from browser_use.browser.profile import ViewportSize
from browser_use.browser.session import BrowserSession
from browser_use.browser.video_recorder import VideoRecorderService

from demo_record.actions import ActionError, ActionRunner, DoneSignal
from demo_record.spec import DemoSpec

logger = logging.getLogger("demo_record")


@dataclass
class RunResult:
    ok: bool
    video_path: Path
    error: str | None = None
    last_screenshot: Path | None = None


class _ScreencastRecorder:
    """Drive browser-use's VideoRecorderService from a CDP screencast.

    Mirrors the frame-handling of browser-use's RecordingWatchdog without the
    Agent machinery (no tab-switching needed for a single-page pipeline).
    """

    def __init__(self, browser: BrowserSession, output_path: Path, size: ViewportSize, framerate: int) -> None:
        self._browser = browser
        self._size = size
        self._recorder = VideoRecorderService(output_path=output_path, size=size, framerate=framerate)
        self._client: Any = None
        self._session_id: str | None = None
        self._pending: set[asyncio.Task] = set()

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

    def _on_frame(self, event, session_id: str | None) -> None:
        if self._session_id and session_id != self._session_id:
            return
        self._recorder.add_frame(event["data"])
        # Acknowledge so Chromium keeps sending frames. Keep a strong reference
        # to the task so it isn't garbage-collected mid-flight.
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

    async def stop(self) -> Path:
        if self._session_id:
            try:
                await self._client.send.Page.stopScreencast(session_id=self._session_id)
            except Exception:  # noqa: BLE001
                pass
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._recorder.stop_and_save)
        return self._recorder.output_path


async def _save_error_screenshot(browser: BrowserSession, path: Path) -> Path | None:
    try:
        data: bytes = await browser.take_screenshot()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path
    except Exception:  # noqa: BLE001 - a failed screenshot must not mask the real error
        logger.warning("could not capture error screenshot", exc_info=True)
        return None


async def run_pipeline(spec: DemoSpec) -> RunResult:
    output_dir = Path(spec.output_dir).expanduser().resolve()
    steps_dir = output_dir / "steps"
    steps_dir.mkdir(parents=True, exist_ok=True)
    video_path = output_dir / "video.mp4"
    size = ViewportSize(width=spec.viewport.width, height=spec.viewport.height)

    browser = BrowserSession(
        headless=spec.headless,
        viewport={"width": spec.viewport.width, "height": spec.viewport.height},
        window_size={"width": spec.viewport.width, "height": spec.viewport.height},
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
        # We manage recording ourselves; disable browser-use's auto-recorder.
        record_video_dir=None,
    )

    recorder: _ScreencastRecorder | None = None
    actions = ActionRunner(browser)

    try:
        await browser.start()
        recorder = _ScreencastRecorder(browser, video_path, size, framerate=30)
        await recorder.start()

        for index, step in enumerate(spec.steps, start=1):
            try:
                await actions.run(step)
            except DoneSignal:
                logger.info("step %d: done", index)
                break
            except ActionError as exc:
                shot = await _save_error_screenshot(browser, output_dir / "error.png")
                return RunResult(
                    ok=False,
                    video_path=video_path,
                    error=f"step {index} {step.action}: {exc}",
                    last_screenshot=shot,
                )
            except Exception as exc:  # noqa: BLE001 - CDP/JS failures still honor the contract
                shot = await _save_error_screenshot(browser, output_dir / "error.png")
                return RunResult(
                    ok=False,
                    video_path=video_path,
                    error=f"step {index} {step.action}: {type(exc).__name__}: {exc}",
                    last_screenshot=shot,
                )
            # Per-step screenshot for review.
            try:
                shot_bytes: bytes = await browser.take_screenshot()
                (steps_dir / f"step_{index:02d}.png").write_bytes(shot_bytes)
            except Exception:  # noqa: BLE001 - screenshots are best-effort
                logger.warning("step %d: screenshot failed", index, exc_info=True)

            if spec.delay_ms > 0:
                await asyncio.sleep(spec.delay_ms / 1000.0)

        return RunResult(ok=True, video_path=video_path)
    finally:
        if recorder is not None:
            try:
                await recorder.stop()
            except Exception:  # noqa: BLE001
                logger.warning("failed to finalize video", exc_info=True)
        try:
            await browser.kill()
        except Exception:  # noqa: BLE001
            logger.warning("failed to close browser cleanly", exc_info=True)


def run(spec: DemoSpec) -> RunResult:
    """Synchronous entry point."""
    return asyncio.run(run_pipeline(spec))

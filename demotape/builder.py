"""Author-time pipeline builder: `from demotape import Pipeline`.

A thin step accumulator with the same verb names as the actions. It only
produces JSON — the runner never executes user code, and the JSON file stays
the record-time artifact and the contract. `write()` appends the `done` step,
dumps the pipeline, and prints the duration estimate.

Selectors stay the author's own constants: pass them in from your module, do
not inline Tailwind-class soup into calls you cannot find again.
"""

from __future__ import annotations

import json
from pathlib import Path

from demotape.estimate import estimate_spec
from demotape.spec import READING_WORDS_PER_SECOND, DemoSpec


class Pipeline:
    def __init__(
        self,
        base_url: str = "",
        output_dir: str = "./demo_out",
        delay_ms: int = 500,
        viewport: tuple[int, int] = (1536, 864),
        headless: bool = False,
        text_to_speech: bool = False,
        tts_voice: str = "af_heart",
        reading_pace: float | None = READING_WORDS_PER_SECOND,
        narration_align: str = "left",
    ) -> None:
        self.base_url = base_url
        self.output_dir = output_dir
        self.delay_ms = delay_ms
        self.viewport = viewport
        self.headless = headless
        self.text_to_speech = text_to_speech
        self.tts_voice = tts_voice
        self.reading_pace = reading_pace
        self.narration_align = narration_align
        self.steps: list[dict[str, object]] = []

    def _add(self, **step: object) -> None:
        self.steps.append(step)

    # -- narration ------------------------------------------------------------

    def say(self, title: str, description: str = "", position: str = "top") -> None:
        """One narration beat: both bars as one text change, one voice clip.

        Emit it BEFORE the action it describes — the action starts as soon as
        the text renders, so the narration covers what is about to happen.
        """
        self._add(
            action="say",
            title=title,
            description=description,
            position=position,
            align=self.narration_align,
        )

    # -- actions ---------------------------------------------------------------

    def navigate(self, url: str, wait_seconds: float = 1.0) -> None:
        full = url if url.startswith(("http://", "https://")) else f"{self.base_url}{url}"
        self._add(action="navigate", url=full, wait_seconds=wait_seconds)

    def back(self, wait_seconds: float = 1.0) -> None:
        self._add(action="back", wait_seconds=wait_seconds)

    def wait(self, seconds: float) -> None:
        self._add(action="wait", seconds=seconds)

    def click(self, selector: str) -> None:
        self._add(action="click", selector=selector)

    def type(self, selector: str, text: str, type_delay_ms: int = 70) -> None:
        self._add(
            action="type", selector=selector, text=text, type_delay_ms=type_delay_ms
        )

    def highlight(
        self, selector: str, duration_seconds: float = 2.0, spotlight: bool = False
    ) -> None:
        self._add(
            action="highlight",
            selector=selector,
            duration_seconds=duration_seconds,
            spotlight=spotlight,
        )

    def scroll(self, selector: str) -> None:
        self._add(action="scroll", selector=selector)

    def drag(
        self,
        selector: str,
        to_selector: str | None = None,
        to: float = 0.5,
        duration_seconds: float = 0.6,
        axis: str = "x",
    ) -> None:
        step: dict[str, object] = {
            "action": "drag",
            "selector": selector,
            "to": to,
            "duration_seconds": duration_seconds,
            "axis": axis,
        }
        if to_selector is not None:
            step["to_selector"] = to_selector
        self.steps.append(step)

    def select(self, selector: str, option: str) -> None:
        self._add(action="select", selector=selector, option=option)

    def upload(self, selector: str, path: str) -> None:
        self._add(action="upload", selector=selector, path=path)

    def assert_text(self, text: str) -> None:
        self._add(action="assert_text", text=text)

    def assert_value(self, selector: str, value: str) -> None:
        self._add(action="assert_value", selector=selector, value=value)

    # -- output ----------------------------------------------------------------

    def write(self, path: str | Path, headless: bool | None = None) -> None:
        """Write the JSON pipeline and print the duration estimate.

        Appends the `done` step (once), so call this last. Every typed field
        should already carry its `assert_value` — the builder does not add it
        for you, because only you know whether the field is read back verbatim.
        """
        if headless is not None:
            self.headless = headless
        if not self.steps or self.steps[-1].get("action") != "done":
            self._add(action="done")
        payload = {
            "viewport": {"width": self.viewport[0], "height": self.viewport[1]},
            "headless": self.headless,
            "output_dir": self.output_dir,
            "delay_ms": self.delay_ms,
            "text_to_speech": self.text_to_speech,
            "steps": self.steps,
        }
        if self.reading_pace is not None:
            payload["reading_pace"] = self.reading_pace
        if self.text_to_speech:
            payload["tts_voice"] = self.tts_voice
        spec = DemoSpec.model_validate(payload)
        Path(path).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"{Path(path).name}: {estimate_spec(spec).format()}")

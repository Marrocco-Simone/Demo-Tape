"""Duration estimate for a pipeline, without launching a browser.

An approximation, biased low: every step pays `delay_ms`, explicit waits and
timed actions (highlight, drag, navigate settle, keystroke time) pay their
duration, and narration holds are the words on screen divided by the pace —
the voice's (`TTS_WORDS_PER_SECOND`) when text_to_speech is on, `reading_pace`
when set. The runner actually skips `delay_ms` around text changes and holds
captions on the video clock, so the real recording can differ either way.
Keep the arithmetic in sync with the runner's pacing constants (spec.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from demotape.spec import (
    READING_CEILING_S,
    READING_FLOOR_S,
    TTS_WORDS_PER_SECOND,
    DemoSpec,
    narration_event_texts,
)


@dataclass
class Estimate:
    steps: int
    waits: int
    words: int
    seconds: float

    def format(self) -> str:
        minutes = self.seconds / 60
        return (
            f"~{minutes:.1f} minutes ({self.steps} steps, "
            f"{self.waits} waits, {self.words} words)"
        )


def estimate_spec(spec: DemoSpec) -> Estimate:
    waits = 0
    timed_s = 0.0
    for step in spec.steps:
        if step.action == "wait":
            waits += 1
            timed_s += step.seconds
        elif step.action == "navigate":
            timed_s += step.wait_seconds
        elif step.action == "back":
            timed_s += step.wait_seconds
        elif step.action == "highlight":
            timed_s += step.duration_seconds
        elif step.action == "drag":
            timed_s += step.duration_seconds
        elif step.action == "type":
            timed_s += len(step.text) * step.type_delay_ms / 1000

    seconds = timed_s + len(spec.steps) * spec.delay_ms / 1000

    events = narration_event_texts(spec.steps)
    words = sum(len(text.split()) for text in events)
    if spec.text_to_speech:
        seconds = max(seconds, words / TTS_WORDS_PER_SECOND)
    if spec.reading_pace is not None:
        reading_s = sum(
            min(max(len(text.split()) / spec.reading_pace, READING_FLOOR_S), READING_CEILING_S)
            for text in events
        )
        seconds = max(seconds, reading_s)

    return Estimate(steps=len(spec.steps), waits=waits, words=words, seconds=seconds)

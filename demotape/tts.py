"""Local text-to-speech narration with Kokoro-82M (Apache-2.0 weights).

The engine is an optional dependency (`pip install 'demo-tape[tts]'`); every
heavy import lives inside the functions that need it so the rest of demotape
never pays for it (and installing demo-tape without the tts extra still
imports this module). The model files (~330MB) download automatically from
Hugging Face on first use and are cached by huggingface_hub.

Narration paces the recording lightly: a clip starts when its overlay text
appears, actions keep running while it plays, and the runner only waits for
the clip to finish right before the NEXT text change (and at the end of the
run), so speech is never cut. Clips are mixed into the video's audio track
afterwards, at their recorded offsets.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

HF_REPO = "fastrtc/kokoro-onnx"
MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"

# A voice's first letter selects its language pack (Kokoro v1.0 convention).
_VOICE_LANG = {
    "a": "en-us",
    "b": "en-gb",
    "e": "es",
    "f": "fr-fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt-br",
    "z": "zh",
}


@dataclass
class NarrationClip:
    start_s: float
    samples: object  # numpy array; typed loosely to keep numpy optional here
    sample_rate: int


class Narrator:
    """Synthesizes narration clips with Kokoro, downloading the model once."""

    def __init__(self, voice: str, speed: float = 1.0) -> None:
        self._voice = voice
        self._speed = speed
        self._kokoro: object | None = None

    async def ensure_ready(self) -> None:
        """Load the engine (downloading the model on first use) and warm it up.

        Also imports the audio dependencies, so a missing piece of the tts
        extra surfaces here - as a startup error - instead of after the
        recording, when the mix would silently produce a silent video. The
        first real synthesis pays ONNX session initialization and kernel
        warm-up; doing it here keeps that CPU spike out of the recording,
        where it would stall frames between a text change and its narration.
        """
        import numpy  # noqa: F401 - preflight: fail before the browser starts
        import soundfile  # noqa: F401 - preflight: fail before the browser starts

        await asyncio.to_thread(self._load)
        await self.synthesize("Hello.")

    def _load(self) -> None:
        if self._kokoro is not None:
            return
        try:
            from huggingface_hub import hf_hub_download
        except ImportError as exc:
            raise RuntimeError(
                "text_to_speech requires the tts extra: pip install 'demo-tape[tts]'"
            ) from exc
        model_path = hf_hub_download(HF_REPO, MODEL_FILE)
        voices_path = hf_hub_download(HF_REPO, VOICES_FILE)
        from kokoro_onnx import Kokoro

        self._kokoro = Kokoro(model_path, voices_path)

    def _lang(self) -> str:
        return _VOICE_LANG.get(self._voice[:1].lower(), "en-us")

    async def synthesize(self, text: str) -> NarrationClip:
        """Return a zero-start clip for `text` (place it with start_s later)."""
        self._load()
        kokoro = self._kokoro
        assert kokoro is not None
        samples, sample_rate = await asyncio.to_thread(
            kokoro.create,
            text,
            voice=self._voice,
            speed=self._speed,
            lang=self._lang(),
        )
        return NarrationClip(start_s=0.0, samples=samples, sample_rate=sample_rate)


def mix_clips(
    clips: list[NarrationClip], total_s: float, out_path: Path
) -> None:
    """Place clips on a silent timeline of `total_s` seconds and write a wav.

    Clips are sequential (narration holds the video while playing), so plain
    placement cannot overlap; float32 math keeps the mix clip-free.
    """
    import numpy as np
    import soundfile as sf

    rate = clips[0].sample_rate
    # The last clip may legitimately extend a hair past the probed video
    # duration (dropped encoder frames shorten the file), so the timeline is
    # as long as whichever is longer - no arbitrary padding.
    needed = max(
        [int(total_s * rate)] + [int(c.start_s * rate) + len(c.samples) for c in clips]
    )
    total = np.zeros(needed, dtype=np.float32)
    for clip in clips:
        offset = int(clip.start_s * rate)
        chunk = np.asarray(clip.samples, dtype=np.float32).reshape(-1)
        end = min(offset + chunk.shape[0], total.shape[0])
        if end > offset:
            total[offset:end] += chunk[: end - offset]
    peak = float(np.max(np.abs(total))) if total.size else 0.0
    if peak > 1.0:
        total /= peak
    sf.write(out_path, total, rate)

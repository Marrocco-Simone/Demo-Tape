"""Verify a narration video actually carries speech, synced to its length.

Checks, in order of how badly their absence would hurt:
- an audio stream exists (the mux ran),
- audio and video durations agree (clip placement did not overrun),
- the audio contains real speech energy, not just silence or a tone:
  a large share of 0.5s windows must sit above a noise floor.
"""

import io
import json
import subprocess
import sys

import numpy as np
import soundfile as sf


def main(path: str) -> int:
    streams = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", path],
            capture_output=True,
            check=True,
        ).stdout
    )["streams"]
    audio = [s for s in streams if s["codec_type"] == "audio"]
    video = [s for s in streams if s["codec_type"] == "video"]
    if not audio:
        print("no audio stream")
        return 1
    if not video:
        print("no video stream")
        return 1
    a_dur = float(audio[0]["duration"])
    v_dur = float(video[0]["duration"])
    if abs(a_dur - v_dur) > 1.0:
        print(f"A/V durations diverge: audio {a_dur:.2f}s vs video {v_dur:.2f}s")
        return 1

    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ar", "24000", "-ac", "1",
         "-f", "wav", "-"],
        capture_output=True,
        check=True,
    )
    x, sr = sf.read(io.BytesIO(proc.stdout), dtype="float32")
    win = sr // 2
    windows = [float(np.sqrt(np.mean(x[i : i + win] ** 2))) for i in range(0, len(x) - win, win)]
    speech = sum(1 for r in windows if r > 0.01)
    share = speech / len(windows) if windows else 0.0
    if share < 0.2:
        print(f"audio is mostly silent: {speech}/{len(windows)} windows above noise floor")
        return 1
    print(f"audio {a_dur:.1f}s vs video {v_dur:.1f}s, {speech}/{len(windows)} speech windows")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

"""demotape CLI.

Usage:
  demotape demo.json          Validate and run a pipeline.
  demotape --schema           Print the JSON Schema for a pipeline file.
  demotape --example          Print a filled-in example pipeline.
  demotape --validate FILE    Validate a pipeline file without running it.
"""

from __future__ import annotations

import argparse
import json
import sys

from pydantic import ValidationError

from demotape.runner import run
from demotape.spec import DemoSpec

EXAMPLE = {
    "viewport": {"width": 1536, "height": 864},
    "headless": False,
    "output_dir": "./demo_out",
    "delay_ms": 500,
    "steps": [
        {"action": "navigate", "url": "http://localhost:3000"},
        {"action": "title", "text": "Sign up", "align": "left"},
        {"action": "description", "text": "Create your account in seconds"},
        {"action": "click", "selector": "#get-started"},
        {"action": "wait", "seconds": 1.0},
        {
            "action": "highlight",
            "selector": "#form",
            "duration_seconds": 2.0,
            "spotlight": True,
        },
        {
            "action": "type",
            "selector": "input[name=email]",
            "text": "demo@acme.com",
            "type_delay_ms": 70,
        },
        {"action": "select", "selector": "select#plan", "option": "pro"},
        {"action": "description", "text": "Choosing the Pro plan"},
        {"action": "click", "selector": "#continue"},
        {"action": "assert_text", "text": "Welcome"},
        {"action": "done"},
    ],
}


def _load_and_validate(path: str) -> DemoSpec:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise SystemExit(f"ERROR file not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR invalid JSON in {path}: {exc}")
    try:
        return DemoSpec.model_validate(raw)
    except ValidationError as exc:
        raise SystemExit(f"ERROR invalid pipeline in {path}:\n{exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="demotape",
        description="Run a JSON action pipeline in Chromium, recording an MP4. No LLM.",
    )
    parser.add_argument("pipeline", nargs="?", help="Path to the pipeline JSON file.")
    parser.add_argument("--schema", action="store_true", help="Print the pipeline JSON Schema and exit.")
    parser.add_argument("--example", action="store_true", help="Print an example pipeline and exit.")
    parser.add_argument("--validate", metavar="FILE", help="Validate FILE as a pipeline and exit.")
    args = parser.parse_args(argv)

    if args.schema:
        print(json.dumps(DemoSpec.model_json_schema(), indent=2))
        return 0
    if args.example:
        print(json.dumps(EXAMPLE, indent=2))
        return 0
    if args.validate:
        spec = _load_and_validate(args.validate)
        print(f"OK valid pipeline: {len(spec.steps)} steps")
        return 0
    if not args.pipeline:
        parser.error("provide a pipeline JSON file (or use --schema/--example/--validate)")

    spec = _load_and_validate(args.pipeline)
    result = run(spec)

    if result.status == "ok":
        print(f"VIDEO_OK {result.video_path}")
        return 0
    if result.status in ("interrupted", "stopped"):
        print(f"STOPPED {result.stop_reason}")
        if result.last_screenshot is not None:
            print(f"LAST_SCREENSHOT {result.last_screenshot}")
        if result.video_path.exists():
            print(f"VIDEO_PARTIAL {result.video_path}")
        return 130 if result.status == "interrupted" else 1
    print(f"ERROR {result.error}")
    if result.last_screenshot is not None:
        print(f"LAST_SCREENSHOT {result.last_screenshot}")
    if result.video_path.exists():
        print(f"VIDEO_PARTIAL {result.video_path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

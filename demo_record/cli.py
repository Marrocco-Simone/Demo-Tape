"""demo-record CLI.

Usage:
  demo-record demo.json          Validate and run a pipeline.
  demo-record --schema           Print the JSON Schema for a pipeline file.
  demo-record --example          Print a filled-in example pipeline.
  demo-record --validate FILE    Validate a pipeline file without running it.
"""

from __future__ import annotations

import argparse
import json
import sys

from pydantic import ValidationError

from demo_record.runner import run
from demo_record.spec import DemoSpec

EXAMPLE = {
    "viewport": {"width": 1280, "height": 800},
    "headless": False,
    "output_dir": "./demo_out",
    "delay_ms": 250,
    "steps": [
        {"action": "navigate", "url": "http://localhost:3000"},
        {"action": "click", "selector": "#get-started"},
        {"action": "wait", "seconds": 1.0},
        {
            "action": "type",
            "selector": "input[name=email]",
            "text": "demo@acme.com",
            "type_delay_ms": 70,
        },
        {"action": "select", "selector": "select#plan", "option": "pro"},
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
        prog="demo-record",
        description="Run a JSON action pipeline in Chromium, recording an MP4 and screenshots. No LLM.",
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

    if result.ok:
        print(f"VIDEO_OK {result.video_path}")
        return 0
    print(f"ERROR {result.error}")
    if result.last_screenshot is not None:
        print(f"LAST_SCREENSHOT {result.last_screenshot}")
    print(f"VIDEO_PARTIAL {result.video_path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

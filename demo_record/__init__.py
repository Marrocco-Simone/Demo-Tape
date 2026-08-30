"""demo-record: deterministic browser demo recorder.

Runs a JSON action pipeline against Chromium, records an MP4, and reports a
strict success/error contract. No LLM involved.
"""

from demo_record.spec import DemoSpec

__all__ = ["DemoSpec"]
__version__ = "0.1.0"

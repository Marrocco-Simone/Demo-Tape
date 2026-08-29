"""Overlay frame: an openscreen-style backdrop page that hosts the app in an
iframe, with a title slot at the top and a description slot at the bottom.

The frame is served from a loopback-only ephemeral HTTP server so the recording
captures backdrop + text + app as one composited page, in both headless and
headed mode. The app itself is cross-origin, so actions script it through a CDP
isolated world (see actions.py), not through frame JS.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FRAME_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>demo</title>
<style>
  html, body { margin: 0; height: 100%; overflow: hidden;
    background: radial-gradient(1200px 800px at 50% -10%, #1e293b 0%, #0f172a 55%, #020617 100%);
    font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; }
  .stage { height: 100%; box-sizing: border-box; padding: 26px 40px;
    display: flex; flex-direction: column; align-items: center; gap: 16px; }
  #dr-title { color: #f8fafc; font-size: 34px; font-weight: 700; letter-spacing: -0.02em;
    min-height: 44px; line-height: 44px; white-space: nowrap; }
  #dr-description { color: #94a3b8; font-size: 19px; min-height: 26px; line-height: 26px;
    white-space: nowrap; }
  #app-frame { width: min(1120px, 100%); flex: 1; min-height: 0; border-radius: 14px;
    border: 1px solid rgba(148, 163, 184, 0.25); background: #fff;
    box-shadow: 0 30px 80px rgba(0, 0, 0, 0.55); }
</style>
</head>
<body>
<div class="stage">
  <div id="dr-title" class="dr-hidden"></div>
  <iframe id="app-frame"></iframe>
  <div id="dr-description" class="dr-hidden"></div>
</div>
<script>
  const q = new URLSearchParams(location.search);
  const title = document.getElementById('dr-title');
  const description = document.getElementById('dr-description');
  title.textContent = q.get('title') || '';
  description.textContent = q.get('description') || '';
  document.getElementById('app-frame').src = q.get('app') || 'about:blank';
  for (const el of [title, description]) {
    if (el.textContent) el.classList.remove('dr-hidden');
  }
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        if self.path.split("?")[0] in ("/", "/frame.html"):
            body = FRAME_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, format: str, *args) -> None:  # silence request logs
        pass


class FrameServer:
    """Serves the overlay frame page on 127.0.0.1 at an ephemeral port."""

    def __init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url: str | None = None

    def start(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self.base_url = None

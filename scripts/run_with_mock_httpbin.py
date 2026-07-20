from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _MockHttpbinHandler(BaseHTTPRequestHandler):
    def _send(self, status: int = 200, payload: dict[str, object] | None = None) -> None:
        body = json.dumps(payload or {"ok": True, "path": self.path}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._send()

    def do_POST(self) -> None:  # noqa: N802
        self._send()

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a command against a tiny local httpbin stub.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("missing command after --")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), _MockHttpbinHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    env = os.environ.copy()
    env["HTTPBIN_URL"] = f"http://127.0.0.1:{args.port}/"
    try:
        completed = subprocess.run(" ".join(command), shell=True, env=env)
        return completed.returncode
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())

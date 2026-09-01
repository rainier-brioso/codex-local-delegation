#!/usr/bin/env python3
"""Mock Responses-compatible provider for testing."""

import argparse
import http.server
import json
import os
import sys
import threading
import time
from urllib.parse import urlparse


class _MockHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that returns mock Responses API responses."""

    request_count = 0
    max_requests: int = 5
    ready_file: str = ""

    def log_message(self, format, *args):
        pass  # Suppress log output

    def do_GET(self):
        _MockHandler.request_count += 1
        parsed = urlparse(self.path)

        if parsed.path == "/v1/models":
            body = json.dumps({
                "object": "list",
                "data": [{"id": "local-test-model", "object": "model"}],
            })
        elif parsed.path == "/api/version":
            body = json.dumps({"version": "0.1.0"})
        elif parsed.path == "/api/tags":
            body = json.dumps({
                "models": [{"name": "local-test-model"}],
            })
        elif parsed.path == "/health":
            body = json.dumps({"status": "ok"})
        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "not found"}).encode())
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        _MockHandler.request_count += 1
        parsed = urlparse(self.path)
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length)
        payload = json.loads(body_bytes.decode("utf-8"))

        if parsed.path == "/v1/responses":
            if payload.get("stream") is True:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                sse = (
                    'event: response.output_text.delta'
                    '\ndata: {"type":"response.output_text.delta","delta":"ready"}'
                    '\n\ndata: [DONE]\n\n'
                )
                self.wfile.write(sse.encode())
            else:
                has_call_output = False
                for inp in payload.get("input", []):
                    if isinstance(inp, dict) and inp.get("type") == "function_call_output":
                        has_call_output = True
                        break
                if has_call_output:
                    body = json.dumps({
                        "id": "resp-2",
                        "object": "response",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [
                                    {"type": "output_text", "text": "pong"}
                                ],
                            }
                        ],
                    })
                else:
                    body = json.dumps({
                        "id": "resp-1",
                        "object": "response",
                        "status": "completed",
                        "output": [
                            {
                                "type": "function_call",
                                "name": "local_delegate_doctor_echo",
                                "call_id": "call-1",
                                "arguments": json.dumps({"value": "ping"}),
                            }
                        ],
                    })
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body.encode())
            return

        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": "not found"}).encode())


def main() -> int:
    parser = argparse.ArgumentParser(description="Start a mock Responses provider.")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--ready-file", default="", help="Path to write when ready.")
    parser.add_argument("--max-requests", type=int, default=5)
    args = parser.parse_args()

    _MockHandler.max_requests = args.max_requests

    server = http.server.HTTPServer(("127.0.0.1", args.port), _MockHandler)
    server.request_count = 0

    if args.ready_file:
        with open(args.ready_file, "w") as f:
            f.write("ready")

    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

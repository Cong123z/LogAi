"""A local HTTP server for the embeddings and LLM clients. handler(path, body) -> (status, payload)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeHttp:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls.append((self.path, body))
                status, payload = outer.handler(self.path, body)
                raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def axis(i: int, dims: int = 1024, x: float = 1.0) -> list[float]:
    """A vector along one axis (plus a small shared part): cosine between axis(i) and axis(j) is controlled."""
    v = [0.0] * dims
    v[i] = x
    return v


def embeddings_handler(vectors: dict[str, list[float]]):
    """Embeddings for known texts; any other text gets axis 1023."""
    def handler(path, body):
        data = [{"index": i, "embedding": vectors.get(t, axis(1023))} for i, t in enumerate(body["input"])]
        return 200, {"object": "list", "data": data, "model": body["model"]}
    return handler

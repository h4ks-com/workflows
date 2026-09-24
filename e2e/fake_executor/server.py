import json
import os
import threading
import time
import urllib.request
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_KEY = os.environ.get("FAKE_EXECUTOR_API_KEY", "")
STEP_DELAY_SECONDS = 0.2


def _callback(callback_url: str, callback_token: str, body: Mapping[str, object]) -> None:
    request = urllib.request.Request(
        callback_url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {callback_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    urllib.request.urlopen(request, timeout=5)


def _run_job(job: dict[str, object]) -> None:
    callback_url = str(job["callback_url"])
    callback_token = str(job["callback_token"])
    steps = job["steps"]
    for step in steps if isinstance(steps, list) else []:
        _callback(callback_url, callback_token, {"kind": "step", "step": step})
        time.sleep(STEP_DELAY_SECONDS)
    if "FAIL" in json.dumps(job.get("params")):
        _callback(callback_url, callback_token, {"kind": "error", "message": "forced failure"})
        return
    result = {
        "kind": "result",
        "files": [
            {
                "url": f"http://fake-executor/results/{job['job_id']}.wav",
                "name": "result.wav",
                "mime": "audio/wav",
            }
        ],
        "title": "fake result",
    }
    _callback(callback_url, callback_token, result)


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body: dict[str, object]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self, header_name: str) -> bool:
        return not API_KEY or self.headers.get(header_name) == API_KEY

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/v1/info":
            if not self._authorized("x-api-key"):
                self._json(401, {"detail": "invalid api key"})
                return
            self._json(200, {"duration": 180, "title": "test"})
            return
        if self.path.startswith("/webhook/"):
            if not self._authorized("X-API-Key"):
                self._json(401, {"detail": "invalid api key"})
                return
            self._json(202, {"status": "accepted"})
            threading.Thread(target=_run_job, args=(body,), daemon=True).start()
            return
        self._json(404, {"detail": "not found"})

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        self._json(404, {"detail": "not found"})

    def log_message(self, fmt: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "9000"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

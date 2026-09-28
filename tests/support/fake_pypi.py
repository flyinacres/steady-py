"""F3: a local, strict stand-in for PyPI's JSON API, with failure injection. It serves only the
fields steady-py reads. Use it through the `pypi` fixture (tests/conftest.py)."""
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from packaging.version import InvalidVersion, Version

FAILURES = ("404", "500", "drop", "truncate")  # drop: no response; truncate: body cut short


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()  # PEP 503


class FakePyPI:
    def __init__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        self.reset()
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def reset(self) -> None:
        self.projects: dict = {}
        self.failures: dict = {}
        self.unknown: list = []
        self.strict = True

    def add(self, name: str, releases: dict) -> None:
        """releases: {version: {requires_dist, requires_python, yanked, yanked_reason, upload_time}},
        every field optional. upload_time defaults to 30 days ago, so staleness never creeps in."""
        self.projects[_normalize(name)] = (name, {v: dict(r or {}) for v, r in releases.items()})

    def fail(self, name: str, mode: str, version: Optional[str] = None) -> None:
        """Fail lookups of one project, or of one version when given."""
        if mode not in FAILURES:
            raise ValueError(mode)
        self.failures[(_normalize(name), version)] = mode

    def allow_unknown(self) -> None:
        """Lookups of unregistered projects get 404 without failing the test."""
        self.strict = False

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def respond(self, path: str):
        """(status, body) for a request path: an HTTP code as text, or a FAILURES mode."""
        parts = path.split("?")[0].strip("/").split("/")
        if parts[0] != "pypi" or parts[-1] != "json" or len(parts) not in (3, 4):
            return "404", None
        key, version = _normalize(parts[1]), (parts[2] if len(parts) == 4 else None)
        mode = self.failures.get((key, version)) or self.failures.get((key, None))
        if mode:
            return mode, None
        if key not in self.projects:
            self.unknown.append(parts[1])
            return "404", None
        name, releases = self.projects[key]
        if version is None:
            return "200", {"info": _info(name, _latest(releases), releases), "releases": {
                v: [{"upload_time_iso_8601": _upload_time(r), "yanked": bool(r.get("yanked"))}]
                for v, r in releases.items()}}
        match = next((v for v in releases if _same_version(v, version)), None)  # PyPI accepts 1.0.0 for 1.0
        return ("200", {"info": _info(name, match, releases)}) if match else ("404", None)


def _same_version(a: str, b: str) -> bool:
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return a == b


def _latest(releases: dict) -> str:
    final = [v for v in releases if not Version(v).is_prerelease] or list(releases)
    return max(final, key=Version)


def _upload_time(release: dict) -> str:
    return release.get("upload_time") or \
        (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _info(name: str, version: str, releases: dict) -> dict:
    r = releases[version]
    return {"name": name, "version": version, "requires_dist": r.get("requires_dist"),
            "requires_python": r.get("requires_python"), "yanked": bool(r.get("yanked")),
            "yanked_reason": r.get("yanked_reason"), "project_urls": None}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, payload = self.server.fake.respond(self.path)
        if status == "drop":
            return  # HTTP/1.0 handler closes the connection with no status line
        body = json.dumps(payload).encode() if payload is not None else b"{}"
        self.send_response(200 if status == "truncate" else int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body) + 100 if status == "truncate" else len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

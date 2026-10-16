from __future__ import annotations
import json
import logging
import logging.handlers
import re
from pathlib import Path
from typing import Any
from .core import dumps, now

SENSITIVE = re.compile(r"(?i)(authorization|password|passwd|secret|cookie|access.?token|refresh.?token|id.?token|api.?key|auth.?url|auth.?code|device.?code|email|raw.?text|raw.?english|observed.?text|teacher.?transcription|prompt.?variables|template.?text|answer.?spec)")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")
SECRET = re.compile(r"(?i)(bearer\s+|sk-)[A-Za-z0-9_.\-]+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
PATH = re.compile(r"[A-Za-z]:\\[^\s\"<>]+|/Users/[^/\s]+|/home/[^/\s]+")
URL = re.compile(r"https?://[^\s\"<>]+")


def sanitize(value: Any):
    if isinstance(value, dict):
        return {str(k): "[redacted]" if SENSITIVE.search(str(k)) else sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        text = SECRET.sub("[credential]", EMAIL.sub("[email]", value))
        text = URL.sub(lambda m: m[0].split("?")[0].split("#")[0], text)
        text = re.sub(r"(?i)(token|password|authorization|cookie|code)=([^\s&]+)", r"\1=[redacted]", text)
        return PATH.sub("[local-path]", text)[:12000]
    return value


class Diagnostics:
    def __init__(self, store, settings):
        self.store = store
        self.directory = store.root / "diagnostics"
        self.directory.mkdir(exist_ok=True)
        self.logger = logging.getLogger("autoreview." + store.manifest["library_id"])
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)
        handler = logging.handlers.RotatingFileHandler(
            self.directory / "diagnostic.jsonl", maxBytes=int(settings.get("diagnostic_mb", 10)) * 1024 * 1024,
            backupCount=max(2, int(settings.get("diagnostic_files", 5))) - 1, encoding="utf-8")
        self.logger.addHandler(handler)
        self.days = max(1, int(settings.get("diagnostic_days", 30)))
        self.prune()

    def prune(self):
        import time
        threshold = time.time() - self.days * 86400
        for path in self.directory.glob("diagnostic.jsonl.*"):
            if path.is_file() and path.stat().st_mtime < threshold:
                path.unlink()
        from datetime import datetime, timezone
        cutoff = datetime.fromtimestamp(threshold, timezone.utc).isoformat()
        with self.store.connection(write=True) as db:
            db.execute('DELETE FROM diagnostic_events WHERE created_at < ?', (cutoff,))

    def record(self, code, message, *, level="error", component="application", **context):
        record = sanitize({"time": now(), "level": level, "error_code": code, "message": message,
                           "component": component, **context})
        self.logger.log(logging.ERROR if level == "error" else logging.INFO, dumps(record))
        with self.store.connection(write=True) as db:
            self.store.put(db, "diagnostic_events", record)
        return record

    def rows(self, limit=200, code=None):
        with self.store.connection() as db:
            rows = self.store.list(db, "diagnostic_events")
        rows = [row for row in rows if not code or row.get("error_code") == code]
        return rows[-min(max(int(limit), 1), 2000):][::-1]

    def close(self):
        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)

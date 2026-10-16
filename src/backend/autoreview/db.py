from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.image_dir = data_dir / 'images'
        self.temp_dir = data_dir / 'work'
        self.path = data_dir / 'autoreview.sqlite3'
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level='DEFERRED')
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as db:
            db.execute('PRAGMA journal_mode = WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, class_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'collecting',
                    answer_version INTEGER NOT NULL DEFAULT 0, final_snapshot TEXT
                );
                CREATE TABLE IF NOT EXISTS answer_versions (
                    batch_id TEXT NOT NULL REFERENCES batches(id), version INTEGER NOT NULL,
                    items_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(batch_id, version)
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
                    tag TEXT NOT NULL, created_at TEXT NOT NULL, paper_feedback TEXT NOT NULL DEFAULT 'pending',
                    UNIQUE(batch_id, tag)
                );
                CREATE TABLE IF NOT EXISTS pages (
                    id TEXT PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES submissions(id),
                    input_seq INTEGER NOT NULL, file_name TEXT NOT NULL, file_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                    machine_json TEXT, review_json TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0,
                    reviewed INTEGER NOT NULL DEFAULT 0, config_json TEXT NOT NULL,
                    error_code TEXT, UNIQUE(input_seq)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, page_id TEXT NOT NULL REFERENCES pages(id),
                    status TEXT NOT NULL, requested_model TEXT NOT NULL, requested_effort TEXT NOT NULL,
                    requested_fast INTEGER NOT NULL DEFAULT 0, answer_version INTEGER NOT NULL,
                    queued_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, elapsed_ms INTEGER,
                    usage_json TEXT, events_json TEXT, turn_id TEXT, error_code TEXT,
                    quota_before_json TEXT, quota_after_json TEXT
                );
                CREATE TABLE IF NOT EXISTS review_history (
                    page_id TEXT NOT NULL REFERENCES pages(id), revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL, reviewed INTEGER NOT NULL,
                    action TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(page_id, revision)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL REFERENCES batches(id),
                    event_type TEXT NOT NULL, entity_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    request_id TEXT PRIMARY KEY, page_id TEXT NOT NULL REFERENCES pages(id)
                );
                CREATE INDEX IF NOT EXISTS pages_submission_idx ON pages(submission_id, input_seq);
                CREATE INDEX IF NOT EXISTS pages_queue_idx ON pages(status, input_seq);
                CREATE INDEX IF NOT EXISTS events_batch_idx ON events(batch_id, id);
            ''')
            attempt_columns = {row['name'] for row in db.execute('PRAGMA table_info(attempts)').fetchall()}
            if 'quota_before_json' not in attempt_columns:
                db.execute('ALTER TABLE attempts ADD COLUMN quota_before_json TEXT')
            if 'quota_after_json' not in attempt_columns:
                db.execute('ALTER TABLE attempts ADD COLUMN quota_after_json TEXT')

    @staticmethod
    def dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

    @staticmethod
    def load(value: str | None, fallback: Any = None) -> Any:
        if value is None:
            return fallback
        return json.loads(value)

    def add_event(self, db: sqlite3.Connection, batch_id: str, event_type: str, entity_id: str, payload: dict[str, Any]) -> int:
        cursor = db.execute(
            'INSERT INTO events(batch_id,event_type,entity_id,payload_json,created_at) VALUES(?,?,?,?,?)',
            (batch_id, event_type, entity_id, self.dump(payload), now_iso()),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def new_id() -> str:
        return str(uuid4())

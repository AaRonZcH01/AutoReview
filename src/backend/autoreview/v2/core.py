from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


DEFAULT_BATCH_PARALLELISM = 10
DEFAULT_CAPTURE_PARALLELISM = 10


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return uuid4().hex


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def digest(value: Any) -> str:
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


class Problem(Exception):
    def __init__(self, code: str, message: str, status: int = 422, **details: Any):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details


def require(condition: Any, code: str, message: str, status: int = 422, **details: Any):
    if not condition:
        raise Problem(code, message, status, **details)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (uid() + ".tmp")  # Keep Windows temporary paths shorter than the final asset name.
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def documents_dir() -> Path:
    if os.name == "nt":
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        # CSIDL_PERSONAL resolves redirected/OneDrive Documents, not a guessed username.
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0:
            return Path(buffer.value)
    return Path.home() / "Documents"


class MachineSettings:
    def __init__(self, home: Path | None = None):
        from .account_storage import instance_home, load_index
        self.home = (home or instance_home()).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "settings.json"
        self._lock = threading.RLock()
        base = documents_dir() / "AutoReview"
        defaults = {
            "schema_version": 2, "library_location": str(base / "Library"), "cli_check_on_startup": False,
            "export_directory": str(base / "Exports"), "last_import_directory": "",
            "last_intake_mode": "whole_submission", "gallery_mode": "image",
            "onboarding_step": 0, "onboarding_complete": False,
            "active_profile_id": None, "profiles": [], "activations": {},
            "model": "gpt-6.1-sol", "effort": "medium", "speed": "standard",
            "reference_model": "gpt-6.1-sol", "reference_effort": "high",
            "parallelism": DEFAULT_BATCH_PARALLELISM, "conservative_review": False, "verification_effort": "medium",
            "auto_orient_images": True,
            "capture_countdown_seconds": 2, "capture_parallelism": DEFAULT_CAPTURE_PARALLELISM,
            "camera_profiles": {},
            "diagnostic_days": 30, "diagnostic_files": 5, "diagnostic_mb": 10,
        }
        if self.path.exists():
            try:
                defaults.update(json.loads(self.path.read_text("utf-8")))
            except (OSError, ValueError):
                raise Problem("SETTINGS_UNREADABLE", "设置文件无法读取，请从备份恢复，原文件已保留。")
        # User-requested v1.4.1 defaults apply once; later explicit choices stay intact.
        if not defaults.get('v141_execution_defaults_applied'):
            defaults['conservative_review'] = False
            if defaults.get('capture_countdown_seconds') == 3:
                defaults['capture_countdown_seconds'] = 2
            defaults['v141_execution_defaults_applied'] = True
            if self.path.exists():
                atomic_write(self.path, dumps(defaults).encode('utf-8'))
        self.data = defaults
        self.data.update(load_index(self.home))
        if any(k in defaults for k in ('profiles', 'active_profile_id')) and self.path.exists():
            stored = json.loads(self.path.read_text('utf-8'))
            if 'profiles' in stored or 'active_profile_id' in stored:
                from .account_storage import save_index
                save_index(self.home, self.data)
                atomic_write(self.path, dumps({k:v for k,v in self.data.items() if k not in ('profiles','active_profile_id','pending_profiles')}).encode('utf-8'))

    def save(self, **changes: Any) -> dict:
        with self._lock:
            self.data.update(changes)
            from .account_storage import save_index
            if any(k in changes for k in ('profiles', 'active_profile_id', 'pending_profiles')):
                save_index(self.home, self.data)
            public = {k: v for k, v in self.data.items() if k not in ('profiles', 'active_profile_id', 'pending_profiles')}
            atomic_write(self.path, dumps(public).encode("utf-8"))
            return dict(self.data)


class LibraryLock:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.file = (root / ".writer.lock").open("a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise Problem("LIBRARY_LOCKED", "资料库正被另一个 AutoReview 实例使用。", 409)

    def close(self):
        if self.file.closed:
            return
        if os.name == "nt":
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()


TABLES = {
    "folders": "folders", "class_presets": None, "tasks": "folders",
    "groups": "tasks", "reference_drafts": "tasks", "reference_versions": "tasks",
    "reference_metadata": "tasks", "scoring_drafts": "tasks", "scoring_versions": "tasks", "score_snapshots": "submissions", "submission_reviews": "submissions",
    "assets": None, "submissions": "groups", "pages": "submissions",
    "image_versions": "pages", "intake_sessions": "groups", "cohorts": "groups",
    "capture_sessions": "tasks", "capture_frames": "capture_sessions",
    "continuation_groups": "tasks", "runs": "continuation_groups", "work_items": "runs",
    "attempts": "work_items", "checkpoints": "attempts", "recovery_adoptions": "checkpoints",
    "observations": "attempts", "review_revisions": "pages", "report_snapshots": "tasks",
    "exports": "report_snapshots", "activity_sessions": "tasks",
    "prompt_versions": None, "review_feedback": "pages", "diagnostic_events": None,
    "quota_snapshots": None, "account_identity_refs": None,
    "image_operation_groups": None,
    "local_image_jobs": None, "review_batches": "tasks", "review_batch_jobs": "tasks",
    "resources": None, "archive_jobs": None,
    "intake_drafts": "tasks", "trash_entries": None, "purge_jobs": None, "usage_ledger": None,
}
IMMUTABLE = {"scoring_versions", "score_snapshots", "submission_reviews", "reference_versions", "image_versions", "observations", "review_revisions",
             "report_snapshots", "prompt_versions", "checkpoints", "recovery_adoptions", "review_feedback"}


class Store:
    """Separate typed entity tables; stable indexed identity + versioned JSON payload.

    Application services perform relationship validation in the same write transaction.
    SQL additionally protects primary identities, page slots, tags and immutable history.
    """
    def __init__(self, root: Path, lock: bool = True):
        self.root = root.resolve()
        self.lock = LibraryLock(self.root) if lock else None
        self.path = self.root / "database" / "autoreview.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        manifest = self.root / "library.json"
        if manifest.exists():
            self.manifest = json.loads(manifest.read_text("utf-8"))
            require(self.manifest.get("schema_version") in (2, 3, 4, 5), "LIBRARY_VERSION",
                    "资料库版本不兼容，禁止写入。", 409)
        else:
            self.manifest = {"library_id": uid(), "schema_version": 5, "minimum_writer": "0.7", "created_at": now()}
            atomic_write(manifest, dumps(self.manifest).encode("utf-8"))
        if self.manifest["schema_version"] in (2, 3, 4):
            # Preserve pre-upgrade metadata before changing the format marker.
            backup = self.root / 'migration-history' / ('schema-' + str(self.manifest['schema_version']) + '-' + uid()[:8])
            backup.mkdir(parents=True)
            atomic_write(backup / 'library.json', manifest.read_bytes())
            if self.path.exists():
                with sqlite3.connect(self.path) as source, sqlite3.connect(backup / 'autoreview.sqlite') as target:
                    source.backup(target)
            atomic_write(backup / 'README.txt', '升级前元数据快照。图片仍属于现有资料库；此目录不是完整备份，不可单独替换当前数据库。'.encode('utf-8'))

        self.owner_id = uid()
        with sqlite3.connect(self.path) as initial:
            initial.execute("PRAGMA journal_mode=WAL")
        with self.connection(write=True) as db:
            for table, parent in TABLES.items():
                fk = f" REFERENCES {parent}(id)" if parent else ""
                db.execute(f"""CREATE TABLE IF NOT EXISTS {table}(
                    id TEXT PRIMARY KEY, parent_id TEXT{fk}, task_id TEXT, group_id TEXT,
                    seq INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'active',
                    revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, data TEXT NOT NULL CHECK(json_valid(data)))""")
                db.execute(f"CREATE INDEX IF NOT EXISTS {table}_parent ON {table}(parent_id,seq)")
                db.execute(f"CREATE INDEX IF NOT EXISTS {table}_scope ON {table}(task_id,group_id,state)")
                if table in IMMUTABLE:
                    db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_immutable
                        BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'immutable_version'); END""")
                    db.execute(f"DROP TRIGGER IF EXISTS {table}_retain")
                    db.execute(f"""CREATE TRIGGER {table}_retain
                        BEFORE DELETE ON {table} WHEN autoreview_purge_allowed()=0
                        BEGIN SELECT RAISE(ABORT,'immutable_version'); END""")
            # Withdrawn capture rows retain their full immutable audit identity,
            # but no longer occupy a live paper tag or page slot. Ordinary trash
            # rows still occupy theirs; only explicit capture withdrawal releases.
            for index, table, key in (('submission_tag', 'submissions', 'paper_tag'), ('page_slot', 'pages', 'slot')):
                existing = db.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?', ('index', index)).fetchone()
                if existing and 'capture_withdrawn' not in existing[0]:
                    db.execute(f'DROP INDEX {index}')
                db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {index} ON {table}(parent_id,json_extract(data,'$.{key}')) "
                           "WHERE COALESCE(json_extract(data,'$.capture_withdrawn'),0)=0")
            db.executescript("""
                CREATE UNIQUE INDEX IF NOT EXISTS run_order ON work_items(parent_id,seq);
                CREATE TABLE IF NOT EXISTS events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
                    entity_id TEXT, task_id TEXT, run_id TEXT, attempt_id TEXT, trace_id TEXT,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_scope ON events(task_id,event_id);
                CREATE TABLE IF NOT EXISTS idempotency(
                    key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY,value INTEGER NOT NULL);
            """)

        with self.connection() as db:
            require(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "LIBRARY_INTEGRITY", "资料库完整性检查失败，未更新格式标记。")
        self.manifest.update(schema_version=5, minimum_writer="0.7")
        atomic_write(manifest, dumps(self.manifest).encode("utf-8"))

    @contextmanager
    def connection(self, write=False, *, purge=False):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.create_function('autoreview_purge_allowed', 0, lambda: int(purge))
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        raw = dict(row)
        data = json.loads(raw.pop("data"))
        return {**data, **raw}

    def get(self, db, table: str, identifier: str, optional=False) -> dict | None:
        require(table in TABLES, "INVALID_ENTITY", "未知实体。")
        item = self.decode(db.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone())
        require(item is not None or optional, "NOT_FOUND", "该记录不存在。", 404)
        return item

    def list(self, db, table: str, *, parent_id=None, task_id=None, group_id=None, state=None,
             include_deleted=True) -> list[dict]:
        require(table in TABLES, "INVALID_ENTITY", "未知实体。")
        clauses, params = [], []
        for key, value in {"parent_id": parent_id, "task_id": task_id, "group_id": group_id, "state": state}.items():
            if value is not None:
                clauses.append(key + "=?")
                params.append(value)
        if not include_deleted:
            clauses.append("state != 'deleted'")
        query = " WHERE " + " AND ".join(clauses) if clauses else ""
        return [self.decode(row) for row in db.execute(
            f"SELECT * FROM {table}{query} ORDER BY seq,created_at,id", params).fetchall()]

    def put(self, db, table: str, data: dict, *, expected: int | None = None) -> dict:
        require(table in TABLES, "INVALID_ENTITY", "未知实体。")
        item = dict(data)
        identifier = item.get("id") or uid()
        old = self.get(db, table, identifier, optional=True)
        if old:
            require(table not in IMMUTABLE, "IMMUTABLE_VERSION", "历史版本不可覆盖。", 409)
            require(expected is None or old["revision"] == expected, "REVISION_CONFLICT",
                    "记录已经更新，请刷新后重试。", 409, current_revision=old["revision"])
        timestamp = now()
        meta = {key: item.pop(key, default) for key, default in {
            "id": identifier, "parent_id": None, "task_id": None, "group_id": None,
            "seq": 0, "state": "active", "revision": 1,
            "created_at": timestamp, "updated_at": timestamp}.items()}
        meta.update(id=identifier, revision=(old["revision"] + 1) if old else 1,
                    updated_at=timestamp, created_at=old["created_at"] if old else meta["created_at"])
        values = tuple(meta[key] for key in
                       ("id", "parent_id", "task_id", "group_id", "seq", "state", "revision", "created_at", "updated_at")) + (dumps(item),)
        if old:
            db.execute(f"""UPDATE {table} SET parent_id=?,task_id=?,group_id=?,seq=?,state=?,
                revision=?,created_at=?,updated_at=?,data=? WHERE id=?""", values[1:] + (identifier,))
        else:
            db.execute(f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?,?,?,?)", values)
        return {**item, **meta}

    def event(self, db, event_type, entity: dict | None = None, **payload):
        from .telemetry import sanitize
        entity = entity or {}
        clean = sanitize(payload)
        cursor = db.execute("""INSERT INTO events(event_type,entity_id,task_id,run_id,attempt_id,trace_id,created_at,payload)
            VALUES(?,?,?,?,?,?,?,?)""", (event_type, entity.get("id"), entity.get("task_id"),
                payload.get("run_id"), payload.get("attempt_id"), payload.get("trace_id"), now(), dumps(clean)))
        return cursor.lastrowid

    def sequence(self, db, key: str) -> int:
        db.execute("INSERT INTO counters VALUES(?,1) ON CONFLICT(name) DO UPDATE SET value=value+1", (key,))
        return db.execute("SELECT value FROM counters WHERE name=?", (key,)).fetchone()[0]

    def once(self, db, key: str, request: dict):
        require(bool(key), "REQUEST_ID_REQUIRED", "缺少请求编号。")
        row = db.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
        if row:
            require(row["fingerprint"] == digest(request), "REQUEST_ID_REUSED", "请求编号已用于不同内容。", 409)
            return json.loads(row["response"])
        return None

    def remember(self, db, key, request, response):
        db.execute("INSERT INTO idempotency VALUES(?,?,?)", (key, digest(request), dumps(response)))
        return response

    def asset_path(self, asset: dict) -> Path:
        path = (self.root / asset["relative_path"]).resolve()
        require(path.is_relative_to(self.root), "INVALID_ASSET_PATH", "资源路径超出资料库。")
        from .resources import Resources
        return Resources(self).resolve(asset["relative_path"])

    def close(self):
        if self.lock:
            self.lock.close()

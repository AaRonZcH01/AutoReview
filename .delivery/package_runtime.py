"""Package the self-contained candidate runtime and verify its file hashes."""
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "release/runtime/AutoReview-Service"
VERSION = json.loads((ROOT / 'package.json').read_text('utf-8'))['version']
ARCHIVE = ROOT / f"release/AutoReview-{VERSION}-runtime-win-x64.zip"


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


files = sorted(p for p in RUNTIME.rglob("*") if p.is_file()
               and p.name != "distribution-manifest.json")
assert (RUNTIME / "AutoReview-Service.exe").is_file()
assert not any(p.name.lower() in {"auth.json", "settings.json", "library.sqlite3"}
               or p.suffix.lower() in {".sqlite", ".db", ".sqlite3"} for p in files)
assert not any('.autoreview-private' in p.parts or p.name in {'accounts.dpapi','codex_auth.age'} for p in files)
manifest = [{"path": p.relative_to(RUNTIME).as_posix(), "sha256": sha256(p),
             "bytes": p.stat().st_size} for p in files]
manifest_path = RUNTIME / "distribution-manifest.json"
manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
with ZipFile(ARCHIVE, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
    for path in files + [manifest_path]:
        archive.write(path, "AutoReview-Service/" + path.relative_to(RUNTIME).as_posix())
with ZipFile(ARCHIVE) as archive:
    assert archive.testzip() is None
    for entry in manifest:
        with archive.open("AutoReview-Service/" + entry["path"]) as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == entry["sha256"]
report = {"archive": ARCHIVE.relative_to(ROOT).as_posix(), "sha256": sha256(ARCHIVE),
          "bytes": ARCHIVE.stat().st_size, "runtime_files": len(files),
          "all_archived_file_hashes_verified": True,
          "zip_crc_verified": True,
          "distribution_kind": "self-contained browser runtime candidate",
          "tauri_desktop_build_verified": False}
evidence = ROOT / f".validation/release-{VERSION}/distribution-check.json"
evidence.parent.mkdir(parents=True, exist_ok=True)
evidence.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))

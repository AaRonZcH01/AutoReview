"""Fail closed on private-state and debug artifacts in a public directory.

This structural gate complements a separate secret scan and human review.
It does not promise detection of every possible secret.
"""
from pathlib import Path
import json
import sys

FORBIDDEN_DIRS = {'.autoreview-private', '.validation', 'profiles', 'attempt-artifacts', 'diagnostics'}
FORBIDDEN_NAMES = {'accounts.dpapi', 'auth.json', 'codex_auth.age', 'settings.json', 'library.json', '.env'}
FORBIDDEN_SUFFIXES = {'.sqlite', '.sqlite3', '.db', '.pdb', '.log', '.age', '.key'}


def check(root):
    root = Path(root)
    failures = []
    count = 0
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        if any(part in {'.git', '.venv', 'node_modules', 'target', '__pycache__', '.pytest_cache'} for part in relative.parts):
            continue
        if path.is_symlink() or any(part in FORBIDDEN_DIRS for part in relative.parts):
            failures.append(relative.as_posix())
        if path.is_file():
            count += 1
            if path.name.lower() in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
                failures.append(relative.as_posix())
            if path.suffix.lower() == '.pem' and b'PRIVATE KEY' in path.read_bytes():
                failures.append(relative.as_posix())
    if failures:
        raise SystemExit('Private/debug content rejected: '+', '.join(sorted(set(failures))))
    return {'files_checked': count, 'private_state_found': False}


if __name__ == '__main__':
    print(json.dumps(check(sys.argv[1]), sort_keys=True))

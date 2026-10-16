"""Per-copy account metadata. Tokens are handled only by the official CLI."""
from __future__ import annotations
import ctypes
import os
import sys
from pathlib import Path
from .core import Problem, atomic_write, dumps


def instance_home() -> Path:
    if os.environ.get('AUTOREVIEW_HOME'):
        return Path(os.environ['AUTOREVIEW_HOME']).resolve()
    root = os.environ.get('AUTOREVIEW_INSTANCE_DIR') or os.environ.get('AUTOREVIEW_SOURCE_ROOT')
    if not root:
        root = str(Path(sys.executable).resolve().parent) if getattr(sys, 'frozen', False) else str(Path(__file__).resolve().parents[4])
    return Path(root).resolve() / '.autoreview-private'


def crypt(data: bytes, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise Problem('SECURE_STORAGE_REQUIRED', '账户持久保存需要 Windows 加密存储，未启用明文回退。', 503)
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, output = Blob(len(data), buffer), Blob()
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    if decrypt:
        ok = library.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        ok = library.CryptProtectData(ctypes.byref(source), 'AutoReview account index', None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise Problem('ACCOUNT_STORAGE_LOCKED', '无法访问本副本的加密账户配置，请使用原 Windows 用户；不会读取其他副本账户。', 503)
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(output.data)


def save_index(home, data):
    fields = {k: data.get(k, [] if k != 'active_profile_id' else None)
              for k in ('profiles', 'active_profile_id', 'pending_profiles')}
    atomic_write(home / 'accounts.dpapi', crypt(dumps(fields).encode('utf-8')))


def load_index(home):
    import json
    path = home / 'accounts.dpapi'
    return json.loads(crypt(path.read_bytes(), True)) if path.exists() else {}


def cli_environment(home):
    # No host app routing, API credentials or account delegation. Keep OS proxy/CA settings.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('CODEX_', 'OPENAI_', 'AUTOREVIEW_CODEX_'))}
    if os.environ.get('CODEX_CA_CERTIFICATE'):
        env['CODEX_CA_CERTIFICATE'] = os.environ['CODEX_CA_CERTIFICATE']
    env['CODEX_HOME'] = str(home)
    return env


def cli_options(home):
    return ['-c', 'forced_login_method="chatgpt"', '-c', 'model_provider="openai"',
            '-c', 'project_doc_max_bytes=0', '-c', 'cli_auth_credentials_store="keyring"',
            '-c', 'auth_keyring_backend="secrets"', '-c', 'features.daemon_auto_start=false',
            '-c', 'log_dir=' + dumps(str(home / 'log'))]


def remove_keyring_keys(home):
    """Remove only this profile's encryption keys; never read credential values.

    Pinned official CLI: secrets::compute_keyring_account and keyring-rs Windows
    username.service target naming. Check both Win32 canonical spellings.
    """
    if os.name != 'nt':
        return
    import hashlib
    api = ctypes.WinDLL('advapi32', use_last_error=True)
    api.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong]
    path = str(home.resolve())
    canonical = path if path.startswith('\\\\?\\') else '\\\\?\\' + path
    for value in {path, canonical}:
        suffix = hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]
        for target in (f'secrets|{suffix}.codex', f'secrets|{suffix}|gateway-oauth.codex', f'cli|{suffix}.Codex Auth'):
            if not api.CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
                raise Problem('ACCOUNT_CLEANUP_FAILED', 'Windows 凭据清理未完成，请关闭相关窗口后重试。', 503)

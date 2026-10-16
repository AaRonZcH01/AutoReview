from __future__ import annotations
import argparse
import json
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
import uvicorn
from .api import create_app
from .core import atomic_write, dumps


def main():
    # This entry is also called synchronously by the uninstaller, before files go away.
    if '--clear-accounts' in sys.argv:
        if '--instance-root' in sys.argv:
            os.environ['AUTOREVIEW_INSTANCE_DIR'] = sys.argv[sys.argv.index('--instance-root') + 1]
            os.environ.pop('AUTOREVIEW_HOME', None)
        import asyncio
        from .core import MachineSettings
        from .provider import Provider
        async def cleanup():
            settings = MachineSettings()
            provider = Provider(settings)
            try:
                for profile in list(settings.data['profiles']):
                    await provider.remove_profile(profile['id'])
                await provider.clean_pending()
            finally:
                await provider.close()
        try:
            asyncio.run(cleanup())
        except Exception:
            sys.exit(2)
        return
    # Windowed frozen executables have no console streams; preserve startup diagnostics.
    if sys.stderr is None:
        from .core import MachineSettings
        log_root = MachineSettings().home / 'startup-logs'
        log_root.mkdir(parents=True, exist_ok=True)
        sys.stderr = (log_root / 'service.log').open('a', encoding='utf-8', buffering=1)
        sys.stdout = sys.stderr
    import faulthandler
    from .core import MachineSettings
    trace_root=MachineSettings().home/'startup-logs'
    trace_root.mkdir(parents=True,exist_ok=True)
    trace=(trace_root/('hang-stacks-'+str(os.getpid())+'.log')).open('a',encoding='utf-8')
    faulthandler.enable(trace)
    parser = argparse.ArgumentParser()
    parser.add_argument('--ready-file')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--keep-alive', action='store_true', help='Developer-only persistent service')
    parser.add_argument('--parent-pid', type=int)
    parser.add_argument('--connection-file', help=argparse.SUPPRESS)
    parser.add_argument('--resume-paused', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--supervise', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.supervise:
        from .lifetime import supervise
        supervise(args.supervise, args.ready_file)
        return
    from .lifetime import contain_children
    job_handle = contain_children()
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[4]))
    static = root / 'dist'
    connection = json.loads(Path(args.connection_file).read_text()) if args.connection_file and Path(args.connection_file).exists() else None
    from urllib.parse import urlsplit, parse_qs
    nonce = parse_qs(urlsplit(connection['url']).fragment)['session'][0] if connection else secrets.token_urlsafe(32)
    if args.resume_paused: os.environ['AUTOREVIEW_RECOVERY_PAUSED']='1'
    app = create_app(nonce=nonce, static_dir=static, manage_lifetime=not args.keep_alive, parent_pid=args.parent_pid)
    app.state.hang_trace = trace
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', connection['port'] if connection else 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    address = f'http://127.0.0.1:{port}/#session={nonce}'
    config = uvicorn.Config(app, use_colors=False, log_level='warning', access_log=False, host='127.0.0.1', port=port, timeout_graceful_shutdown=10)
    server = uvicorn.Server(config)
    app.state.shutdown = lambda: setattr(server, 'should_exit', True)
    def announce():
        while not server.started and not server.should_exit:
            time.sleep(.05)
        if server.started:
            if args.ready_file:
                atomic_write(Path(args.ready_file), dumps({'url': address, 'pid': os.getpid(), 'port': port}).encode())
            if not args.no_browser:
                webbrowser.open(address)
    threading.Thread(target=announce, daemon=True).start()
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()
        if args.ready_file:
            Path(args.ready_file).unlink(missing_ok=True)


if __name__ == '__main__':
    main()

"""Reject a distribution whose bundled catalogue cannot represent product defaults.

No login, credential inspection or inference. A directory listing is not proof
of account entitlement; the release report records real inference separately.
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src/backend'))
from autoreview.v2.provider import Rpc
from autoreview.v2.core import MachineSettings


def verify(models, settings):
    checks = []
    for model_key, effort_key in [('model', 'effort'), ('reference_model', 'reference_effort')]:
        name, effort = settings[model_key], settings[effort_key]
        model = next((m for m in models if (m.get('model') or m.get('id')) == name), None)
        if not model or 'image' not in model.get('inputModalities', []):
            raise RuntimeError(f'Distribution catalogue missing default image model: {name}')
        if effort not in [x.get('reasoningEffort') for x in model.get('supportedReasoningEfforts', [])]:
            raise RuntimeError(f'Distribution catalogue missing default reasoning: {name}/{effort}')
        checks.append({'purpose': model_key, 'model': name, 'effort': effort})
    return checks


async def main(executable):
    os.environ['AUTOREVIEW_CODEX_EXE'] = str(Path(executable).resolve())
    with tempfile.TemporaryDirectory(prefix='autoreview-catalogue-') as directory:
        root = Path(directory)
        defaults = MachineSettings(root/'settings').data
        rpc = Rpc(root/'unsigned-cli')
        try:
            await rpc.start()
            models, cursor, seen = [], None, set()
            while True:
                page = await rpc.request('model/list', {'includeHidden': False, 'limit': 100, 'cursor': cursor}, timeout=20)
                models.extend(page['data'])
                cursor = page.get('nextCursor')
                if not cursor: break
                if cursor in seen or len(seen) >= 32: raise RuntimeError('Incomplete model catalogue')
                seen.add(cursor)
            result = {'server': rpc.server_info.get('userAgent'), 'defaults': verify(models, defaults),
                      'model_count': len(models), 'inference_calls': 0, 'signed_in': False}
            print(json.dumps(result))
        finally:
            await rpc.close()

if __name__ == '__main__':
    asyncio.run(main(sys.argv[1]))

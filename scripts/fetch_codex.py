"""Fetch only hash-pinned public executables from the official release."""
from pathlib import Path
import hashlib
import json
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT/'cli-distribution.lock.json').read_text('utf-8'))
TAG = 'rust-v' + LOCK['cliVersion'].removeprefix('codex-cli ')
HEADERS = {'User-Agent': 'AutoReview-build', 'Accept': 'application/vnd.github+json'}


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=60)


def main():
    folder = ROOT/'resources'
    folder.mkdir(exist_ok=True)
    missing = [entry for entry in LOCK['files'] if not (folder/entry['name']).is_file()
               or hashlib.sha256((folder/entry['name']).read_bytes()).hexdigest() != entry['sha256']]
    if not missing:
        print('Pinned official CLI: all four executable hashes match')
        return
    with get('https://api.github.com/repos/openai/codex/releases/tags/'+TAG) as response:
        release = json.load(response)
    with tempfile.TemporaryDirectory(prefix='autoreview-public-cli-') as temporary:
        for entry in missing:
            archive_name = entry['name'].removesuffix('.exe') + '-x86_64-pc-windows-msvc.exe.tar.gz'
            asset = next(a for a in release['assets'] if a['name'] == archive_name)
            url = f'https://github.com/openai/codex/releases/download/{TAG}/{archive_name}'
            assert asset['browser_download_url'] == url and asset.get('digest', '').startswith('sha256:')
            archive = Path(temporary)/archive_name
            with get(url) as response, archive.open('wb') as output:
                while block := response.read(1024*1024):
                    output.write(block)
            assert hashlib.sha256(archive.read_bytes()).hexdigest() == asset['digest'].removeprefix('sha256:')
            with tarfile.open(archive) as source:
                members = [m for m in source.getmembers() if m.isfile() and Path(m.name).name == entry['name']]
                assert len(members) == 1
                data = source.extractfile(members[0]).read()
            assert len(data) == entry['bytes'] and hashlib.sha256(data).hexdigest() == entry['sha256']
            (folder/entry['name']).write_bytes(data)
            print('Verified public executable: '+entry['name'])


if __name__ == '__main__':
    main()

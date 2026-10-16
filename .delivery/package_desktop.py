"""Package the built Tauri app and runtime without user data; verify every entry."""
import hashlib
import json
import os
import shutil
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / 'release'
RUNTIME = RELEASE / 'runtime/AutoReview-Service'
APP = ROOT / 'src-tauri/target/release/autoreview-desktop.exe'
VERSION = json.loads((ROOT / 'package.json').read_text('utf-8'))['version']
NSIS = ROOT / f'src-tauri/target/release/bundle/nsis/AutoReview_{VERSION}_x64-setup.exe'
TARGET = RELEASE / f'AutoReview-{VERSION}-desktop-win-x64.zip'
INSTALLER = RELEASE / f'AutoReview-{VERSION}-windows-x64-setup.exe'

def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

assert APP.is_file() and NSIS.is_file() and (RUNTIME / 'AutoReview-Service.exe').is_file()
webviews = sorted((Path(os.environ['LOCALAPPDATA']) / 'tauri/x64').rglob('MicrosoftEdgeWebView2RuntimeInstallerX64.exe'), key=lambda p: p.stat().st_mtime)
assert webviews, 'Tauri offline WebView2 installer cache missing; run desktop build first'
webview = webviews[-1]
files = {'AutoReview.exe': APP, 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe': webview}
files.update({'runtime/' + p.relative_to(RUNTIME).as_posix(): p for p in RUNTIME.rglob('*') if p.is_file()})
files.update({'docs/' + name: ROOT/name for name in ('USER_GUIDE.md','USER_GUIDE.docx')})
files.update({name: ROOT/name for name in ('LICENSE','THIRD_PARTY_NOTICES.md','PRIVACY.md')})
assert not any(p.name.lower() in {'auth.json', 'settings.json'} or p.suffix.lower() in {'.sqlite', '.db', '.sqlite3'} for p in files.values())
assert not any('.autoreview-private' in name or 'accounts.dpapi' in name or '/profiles/' in name for name in files)
manifest = [{'path': name, 'bytes': path.stat().st_size, 'sha256': sha(path)} for name, path in sorted(files.items())]
readme = f'''AutoReview {VERSION} 桌面便携版（Windows x64）

完整解压后双击 AutoReview.exe。保持 runtime 文件夹完整，不需要Python、Node、Rust或C++开发工具。
如新机器未安装Microsoft Edge WebView2，请先运行同目录的MicrosoftEdgeWebView2RuntimeInstallerX64.exe（离线组件）。
使用安装版时WebView2检测和安装会自动执行；这里的便携版保留显式运行组件的方式。

端口自动分配，关闭桌面窗口会结束自己的后台及子进程。请勿同时打开使用同一资料库的两个实例。
首次使用账户完全空白，通过官方ChatGPT流程登录。每份程序独立保存配置，Windows加密保护，登录后下次自动复用。
本副本私有目录 .autoreview-private 不得复制或分享；公开分发时请使用原始ZIP。
删除便携目录前，请先在账户面板“清除全部账户并退出”。直接删文件夹不会触发系统凭据清理。
便携仅指程序可复制；资料默认保存在Windows文档/AutoReview/Library，可在设置中选择其他位置。
搬运作业请使用资料库备份/恢复或迁移，勿复制账户令牌。勿在应用运行时直接搬动资料库。

先确认范本图像、结构与答案，再进行学生图片准备及批前确认，最后主动提交云端。
原始图片、机器观察和人工结论保留，校正更新当前处理版本，不自动重跑云端。
详细操作见runtime/PORTABLE-README.txt。

本机安装/运行与离线测试通过不代表真实手写准确率或另一台物理机验收通过。
摄像头支持流式采集、范本与作业替换拍摄。可逐张识别或仅本地保存后统一批处理。
详细操作说明位于 docs/USER_GUIDE.docx 与 docs/USER_GUIDE.md。
程序及归档不包含账户凭据或学生图片。
'''
with ZipFile(TARGET, 'w', ZIP_DEFLATED, compresslevel=6) as archive:
    for name, path in sorted(files.items()):
        archive.write(path, 'AutoReview/' + name)
    archive.writestr('AutoReview/README.txt', readme.encode('utf-8-sig'))
    archive.writestr('AutoReview/desktop-manifest.json', json.dumps(manifest, indent=2))
with ZipFile(TARGET) as archive:
    assert archive.testzip() is None
    for entry in manifest:
        with archive.open('AutoReview/' + entry['path']) as stream:
            assert hashlib.file_digest(stream, 'sha256').hexdigest() == entry['sha256']
shutil.copyfile(NSIS, INSTALLER)
report = {'desktop_archive': TARGET.relative_to(ROOT).as_posix(), 'desktop_sha256': sha(TARGET),
          'desktop_bytes': TARGET.stat().st_size, 'files_verified': len(manifest),
          'installer': INSTALLER.relative_to(ROOT).as_posix(), 'installer_sha256': sha(INSTALLER),
          'installer_bytes': INSTALLER.stat().st_size, 'webview2_offline_included': True,
          'zip_crc_verified': True,
          'real_model_calls': 0, 'new_physical_machine_tested': False}
evidence = ROOT / f'.validation/release-{VERSION}/desktop-distribution.json'
evidence.parent.mkdir(parents=True, exist_ok=True)
evidence.write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report))

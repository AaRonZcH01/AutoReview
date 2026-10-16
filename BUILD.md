# Windows x64 构建

## 依赖

- Python 3.12 x64（包含 pip、venv、Tk）。
- Node.js 24、pnpm 11.19.0。
- 桌面构建还需要 Rust MSVC、Visual Studio C++ Build Tools 和 Windows SDK。
- WebView2 用于桌面运行；Tauri 构建安装器时取得其官方离线组件。

使用各项目的官方分发安装开发工具。终端中应能运行 `python`、`node`、`pnpm` 和 `cargo`。

## 准备与检查

```powershell
.\Prepare-Environment.ps1
pnpm build
$env:PYTHONPATH = 'src/backend'
.\.venv\Scripts\python.exe -m pytest tests/v2 -q
```

准备脚本只安装锁文件中的依赖并执行本地检查，不访问个人认证、不调用云端模型。自定义 Python 路径可使用 `-PythonPath <python.exe>`。锁文件针对上述版本与 Windows x64，不承诺其他系统。

## 源码启动

```powershell
.\Start-Current.ps1 -Browser
# 配置了 Rust 后可直接启动桌面外壳
.\Start-Current.ps1
```

首次使用自己的官方账户登录。源码与已安装或便携程序的账户配置相互隔离；默认业务库位置仍需自行选择，避免两个实例同时使用同一资料库。

## 发行构建

```powershell
.\Build-Portable.ps1
.\Build-Desktop.ps1 -SkipRuntime
```

后台使用 PyInstaller 打包。CLI 只从官方 `openai/codex` 的固定版本发布中获取四个公共可执行文件，按官方归档摘要及 `cli-distribution.lock.json` 的解压后摘要双重校验；不打包 CODEX_HOME、登录文件或个人设置。

构建产物位于 `release/`。安装器与桌面 ZIP 包含前端、后台、CPU 方向模型、固定 CLI、说明与第三方许可证。单独 runtime ZIP 是可选的本地浏览器运行方式。

发布前运行 `scripts/audit_public.py` 检查源码/解压后的运行目录，并使用独立的秘密扫描工具检查待提交 Git 对象。不要对已运行或已登录的目录打包。发行工具拒绝常见账户、资料库及调试产物，但发布者仍需审阅完整待发布清单和新增文件。

# AutoReview

面向小学英语作业的 Windows 教师工作台，支持范本与答案管理、图片导入与相机采集、云端识别、本地判分、教师复核和结果导出。

当前版本：**1.4.2**。软件不提供账户、订阅或额度，使用前需登录自己的 ChatGPT 账户。

## 下载与使用

在 [Releases](https://github.com/AaRonZcH01/AutoReview/releases/latest) 下载 Windows x64 安装包或桌面便携 ZIP，并对照同一版本的 SHA256 清单。

- 安装版：运行 `AutoReview-1.4.2-windows-x64-setup.exe`。
- 便携版：完整解压 `AutoReview-1.4.2-desktop-win-x64.zip`，双击其中的 `AutoReview.exe`，保留整个运行目录。
- 未安装 WebView2 时，安装器会处理；便携版可运行包内的微软 WebView2 安装组件。

具体操作见 [教师使用说明](USER_GUIDE.md) 或 [Word 说明](USER_GUIDE.docx)。

## 主要功能

- 范本图片、本机相机拍摄和手工答案；答案与计分规则版本化。
- 多班、多页纸签编排；本地图片批处理与逐张相机采集。
- 流式确认后立即入队，或先保存本地、之后统一提交。
- 批处理与流式并发独立设置，均支持 1–13，默认 10。
- 默认范本 `gpt-6.1-sol / high`，学生识别 `gpt-6.1-sol / medium`；可用模型以账户返回为准。
- 保守复看可选，默认关闭；任务入队后冻结所用配置。
- 原图定位、逐题人工改判、撤销与重做、失败阶段继续和跨页依赖处理。
- 成绩与易错分析；CSV、XLSX、JSON、批注图片和 PDF 导出。
- 各副本账户隔离、Windows 加密保存、账户删除；Codex CLI 组件更新。

## 数据与账户

资料库默认位于当前 Windows 用户的“文档/AutoReview/Library”，可在设置中更改。账户、设置和浏览器缓存按程序副本保存在 `.autoreview-private`。

相机预览、拍照检查和本地导入在本机完成；提交识别时，必要图片通过官方 Codex CLI 发送，并使用所选账户额度。

删除便携版前，请先执行“清除全部账户并退出”，以清理账户数据和 Windows 凭据库中的对应密钥。详见 [隐私说明](PRIVACY.md) 与 [安全报告方式](SECURITY.md)。

## 开发

源码包含应用代码、离线回归测试、合成测试材料、依赖锁文件和构建脚本。代码结构与构建步骤见 [BUILD.md](BUILD.md) 和 [ARCHITECTURE.md](ARCHITECTURE.md)。

```powershell
.\Prepare-Environment.ps1
.\.venv\Scripts\python.exe scripts/fetch_codex.py
pnpm build
$env:PYTHONPATH = 'src/backend'
.\.venv\Scripts\python.exe -m pytest tests/v2 -q
$env:AUTOREVIEW_CODEX_EXE = Join-Path $PWD 'resources/codex.exe'
.\Start-Current.ps1 -Browser
```

识别结果由教师复核，现有验证范围见 [发布说明](CHANGELOG.md)。

## 许可证

AutoReview 原创代码采用 [MIT](LICENSE)。捆绑组件、模型与依赖保留各自许可证，见 [第三方说明](THIRD_PARTY_NOTICES.md)。

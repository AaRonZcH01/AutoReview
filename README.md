# AutoReview

面向小学英语作业的 Windows 教师工作台。将范本和答案确认、作业导入或相机采集、云端观察、本地判分、人工复核与结果导出连在一起。

当前版本：**1.4.2**。本项目不隶属于 OpenAI。软件不提供账户、订阅或额度，请使用自己的账户并遵守服务提供方的条款。

## 下载与使用

在 [Releases](https://github.com/AaRonZcH01/AutoReview/releases/latest) 下载 Windows x64 安装包或桌面便携 ZIP，并对照同一版本的 SHA256 清单。

- 安装版：运行 `AutoReview-1.4.2-windows-x64-setup.exe`。
- 便携版：完整解压 `AutoReview-1.4.2-desktop-win-x64.zip`，双击其中的 `AutoReview.exe`，保留整个运行目录。
- 未安装 WebView2 时，安装器会处理；便携版可运行包内的微软 WebView2 安装组件。
- 首次使用账户列表为空，通过软件内的官方登录流程连接自己的 ChatGPT 账户。

具体操作见 [教师使用说明](USER_GUIDE.md) 或 [Word 说明](USER_GUIDE.docx)。

## 主要功能

- 范本图片、本机相机拍摄和手工答案；答案与计分规则版本化。
- 多班、多页纸签编排；本地图片批处理与逐张相机采集。
- 流式确认后立即入队，或先保存本地、之后统一提交。
- 批处理与流式并发独立设置，均支持 1–13，默认 10。
- 默认范本 `gpt-6.1-sol / high`，学生识别 `gpt-6.1-sol / medium`；账户实际可用模型以官方返回为准。
- 保守复看可选，默认关闭；任务入队后冻结所用配置。
- 原图定位、逐题人工改判、撤销与重做、失败阶段继续和跨页依赖处理。
- 成绩与易错分析；CSV、XLSX、JSON、批注图片和 PDF 导出。
- 本副本账户隔离、Windows 加密保存、账户删除；兼容 Codex CLI 组件的确认式更新。

## 数据与账户

资料库默认位于当前 Windows 用户的“文档/AutoReview/Library”，可在设置中选择其他位置。账户、设置和浏览器缓存放在本程序副本的 `.autoreview-private`，不包含在发行包中。

仅预览相机、拍照检查和本地导入不会自动上传；主动提交识别会将必要图片交给官方 Codex CLI，并使用所选账户的额度。不要在公开 Issue、日志附件或再分发包中包含自己的私有目录、资料库、试卷或令牌。

便携版删除前应先执行“清除全部账户并退出”。直接删除文件夹不能触发 Windows 凭据库清理。详见 [隐私说明](PRIVACY.md) 与 [安全报告方式](SECURITY.md)。

## 开发

代码结构与构建步骤见 [BUILD.md](BUILD.md) 和 [ARCHITECTURE.md](ARCHITECTURE.md)。源码包含离线回归测试和合成测试材料，不包含开发过程使用过的试卷、账户或资料库。

```powershell
.\Prepare-Environment.ps1
pnpm build
$env:PYTHONPATH = 'src/backend'
.\.venv\Scripts\python.exe -m pytest tests/v2 -q
.\Start-Current.ps1 -Browser
```

云端识别效果受照片、题型、模型和服务状态影响，最终成绩需由教师核实。现有验证范围见 [发布说明](CHANGELOG.md)；不保证全自动准确率或所有相机兼容。

## 许可证

AutoReview 原创代码采用 [MIT](LICENSE)。捆绑组件、模型与依赖保留各自许可证，见 [第三方说明](THIRD_PARTY_NOTICES.md)。

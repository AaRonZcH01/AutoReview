# 项目结构

| 路径 | 用途 |
|---|---|
| src/frontend | React、TypeScript、SVG 工作区与相机交互 |
| src/backend/autoreview/v2 | 当前 FastAPI 服务、SQLite、任务队列、判分、认证和组件更新 |
| src/backend/desktop_entry.py | 冻结后台入口 |
| src-tauri | Windows Tauri 外壳、后台监护、托盘、WebView2 与卸载钩子 |
| tests/v2 | 隔离离线回归测试与合成测试材料 |
| .delivery | 发行构建辅助脚本 |
| scripts | 依赖检查、固定 CLI 下载、发行内容检查 |

旧原型源文件仅作代码演进参考，桌面发行入口使用 v2。端口由系统分配，仅监听本机回环地址。运行服务与桌面窗口关联，退出时结束自己所属的后台。

机器观察、教师修改和最终判定分层保存。配置在任务入队时冻结；跨页依赖、固定纸签页位、版本围栏和幂等控制防止迟到结果覆盖新照片或人工结论。失败重试优先使用匹配的阶段检查点。

测试使用隔离目录与模拟 Provider；模型调用由用户主动提交。

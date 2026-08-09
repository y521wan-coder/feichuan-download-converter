# 无障碍视频转文字

面向 Windows 10 IoT Enterprise LTSC 2021（19044）x64 的纯键盘、读屏友好型视频转 MP3 与腾讯云录音文件识别桌面软件。

当前状态：开发初期，尚未形成可供用户验收的测试候选版。

## 开发环境

- 项目本地 .NET 10 SDK：`tools/dotnet`
- UI：原生 WinForms 标准控件
- 本地媒体处理：FFmpeg/FFprobe 独立进程
- 云端：腾讯云常规录音文件识别 `16k_zh` 与私有 COS
- 私有运行数据：`%LOCALAPPDATA%\AccessibleVideoToText`

完整范围和强制验收条件见 `docs/requirements/最终开发计划.txt`。接手开发前先阅读 `AGENTS.md` 和 `交接说明.txt`。


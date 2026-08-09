# 2026-08-09 初始骨架里程碑

环境：Windows 10 IoT 企业版 LTSC 2021，10.0.19044，x64。

## 工具

- Git 2.53.0.windows.2
- 项目本地 .NET SDK 10.0.302，SHA-512 与微软 release metadata 一致
- 本机 FFmpeg `N-124300-gdba0b078c8-20260502`，GPL，仅开发验证

## 命令与结果

1. `tools\dotnet\dotnet.exe restore AccessibleVideoToText.slnx`：通过，4 个项目已还原。
2. `tools\dotnet\dotnet.exe build AccessibleVideoToText.slnx --no-restore --configuration Debug`：通过，0 警告、0 错误。
3. `tools\dotnet\dotnet.exe test AccessibleVideoToText.slnx --no-build --configuration Debug`：通过，11/11。

覆盖：剪贴板顺序、大小写不敏感去重、100 项整批限制、无效项跳过、未知扩展名待探测、成对编号、`.part` 命名、5% 进度节流、固定任务终态、普通 ASR 参数锁定、凭据 `ToString` 不泄露。

无障碍实测：未执行，全部强制项仍为“待测试”。

许可证异常：`Tencent.QCloud.Cos.Sdk 5.4.51` 包内没有许可证信息或许可证文件，禁止进入候选产物，等待腾讯官方明确依据。


# 飞船下载转换工具

“飞船下载转换工具”1.0 的独立合并开发仓库。仓库位于
`D:\FeichuanDownloadConverterDev`，两个旧项目继续保留在原位置，只作为稳定基线。

当前状态：已完成新目录、开发资料和两套源码的只读导入，正在验证 17 组下载离线
smoke 与 38 项转换测试，并建立 .NET 10 WinForms + Python worker 的版本化 JSON Lines
协议骨架。

## 目录

- `src`：从“无障碍视频转文字”导入的 WinForms、Core、Infrastructure 源码，后续在这里改造成统一外壳。
- `tests`：转换、云端、安全输出和 WinForms 自动测试。
- `worker`：从“飞船下载工具”导入的 Python 下载内核、测试、安装定义、工具和历史资料。
- `docs/requirements`：合并开发计划与旧转换计划。
- `docs/handoff`：两个旧项目的原交接说明副本。
- `logs/development`、`logs/merge-development`：可提交的开发记录；运行时日志不提交。
- `third-party`：依赖边界和许可证。
- `release`：候选安装包目录；最终通过全部必测项后才允许复制到 D 盘根目录。

## 开发边界

- 产品显示版本与安装包版本为 `1.0`，程序集版本为 `1.0.0.0`。
- 软件更新与下载核心更新只允许手动检查；暂停服务器更新发布。
- 不读取、显示、导出或记录腾讯云密钥、Cookie、签名 URL、媒体令牌和识别正文。
- 下载源文件永不删除或覆盖；MP3/TXT 使用临时文件、原子提交和成对编号。
- 未完成安装版真实验收和用户争渡朗读验收前，只能称为测试候选版。

每次继续开发前先完整阅读根目录 `AGENTS.md`、
`docs/requirements/merge-development-plan.md` 与根目录 `交接说明.txt` 的最后一个区块。

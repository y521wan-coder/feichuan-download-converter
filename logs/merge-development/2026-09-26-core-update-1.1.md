# 2026-09-26 下载核心 2026.08.19 更新、版本升级 1.1 与本地安装包

## 起因

- 用户反馈 YouTube 单视频下载失败（`https://youtu.be/HBtRW-aXB8o`）。
- 现场证据：应用日志 `unable to download video data: HTTP Error 403: Forbidden`，并伴随
  `No supported JavaScript runtime could be found` 警告。
- 对照实验：换一个完全不相干的 YouTube 视频同样 403；代理端口、YouTube 首页、格式列表均正常，
  说明不是单个视频、不是保存位置、也不是网络中断。

## 根因

- 内置下载核心为 yt-dlp 2026.07.04，已落后官方稳定版两个多月；YouTube 调整防盗链后，该核心
  取得的媒体地址一律被拒绝（HTTP 403）。
- 官方 2026.08.19 核心使用同一套软件参数立即下载成功，确认根因是核心过期。
- 附带缺陷：`worker/src/feichuan_downloader/core_updater.py` 三处网络请求强制
  `proxies={"http": None, "https": None}`。requests 默认只读取环境变量代理，不读取 Windows
  系统代理，因此本机（系统代理 127.0.0.1:10808）的“检查下载核心更新”永远无法下载 GitHub
  资源，这正是核心长期停留在 2026.07.04 的原因。

## 修改

1. `worker/tools/yt-dlp.exe` 与 `worker/tools/SHA2-256SUMS` 替换为官方 stable 2026.08.19。
2. `core_updater.py` 新增 `system_proxy_candidates()` 与 `CoreUpdater._get()`：按“系统代理 →
   直连”顺序请求，两条路径都失败才报错；`_fetch_release`、`_download_text`、`_download_file`
   全部改为走 `_get()`。
3. 版本升级 1.0 → 1.1：`Directory.Build.props`、`app.manifest`、`installer/FeichuanDownloadConverter.iss`、
   `scripts/build_candidate.ps1`、worker 的 `__init__.py`/`config.py`/`protocol.py`（worker_version）、
   AGENTS.md、README.md、合并计划和使用说明标题。
4. 测试同步：`WinFormsAccessibilityBaselineTests.ProductAssemblyVersions_AreFixedAtOnePointOne`、
   `WorkerProtocolTests`、`worker/tests/smoke_test.py`、`worker/tests/worker_protocol_smoke.py`；
   `worker/tests/core_transaction_smoke.py` 不再硬编码核心版本号，改为从真实核心读取期望值。
5. `worker/tests/smoke_test.py` 新增离线检查：系统代理候选顺序、代理失败回退直连、两条路径
   都失败时报错。全部使用假实现，不联网。
6. `docs/release/使用说明.txt` 补充“更新下载核心会先尝试系统代理、失败再直连”。

## 测试

- .NET Release：72/72 通过，0 失败、0 跳过。
- Python 离线 smoke：20/20 通过（`python worker\scripts\run_smoke.py`）。

## 构建与覆盖安装

- 构建脚本：`scripts/build_candidate.ps1`；候选静态载荷 489 个，`版本与校验.txt` 显示 1.1 / 1.1.0.0。
- 安装包：`release\飞船下载转换工具-Setup-1.1.exe`，164120295 字节，
  SHA-256 `A98E6983B87482A7D48E415C2C11419D9F4AA229F8BAD1C2A6EEFB21EF8433C4`。
- D 盘交付：`D:\飞船下载转换工具-Setup-1.1.exe`，哈希与项目 release 完全一致；D 盘不放 `.sha256`。
- 覆盖安装：`/VERYSILENT` 返回码 0；覆盖前备份
  `artifacts\installer-upgrade-20260926-v1.1\backup-installed`（493 个文件）。
- 安装目录核对 489 项：0 缺失，唯一差异是安装器按设计保留用户自定义的 `任务结束.wav`。

## 真实安装版验收

- 使用安装目录的 `feichuan-worker.exe`（握手 `worker_version=1.1`）真实下载同一 YouTube 视频：
  `kind=download`、`partial_success=false`、`warnings=[]`、`.part=0`、stderr 0 行、worker 退出码 0。
- 媒体：WebM（AV1 1920×1080 + Opus 双声道），251.441 秒，38589330 字节，
  位于 `artifacts\acceptance\2026-09-26-youtube-core-1.1`。
- 验收期间临时切换下载目录，验收后已恢复为用户的 `D:\`。

## 边界

- 未生成、上传或发布服务器更新包；未读取、配置或输出腾讯云凭据；未连接服务器、SSH 或数据库。
- 安装包 Authenticode 仍为 NotSigned，不得称为已签名版本。

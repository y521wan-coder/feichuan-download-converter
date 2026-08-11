# 获取解析直连、安装版验收与本地安装包记录

时间：2026-08-11 23:25（北京时间）

功能提交：`09a0aa0 feat: copy resolved direct links to clipboard`

## 实现结果

- “处理模式”组合框由三项扩为四项，新增“获取解析直连”。原启动焦点和 Tab 顺序不变；输入框或
  组合框按 Enter 均可开始，结束后复位“只下载”。
- 只处理一个视频来源；批量、用户主页、合集、频道、播放列表、直播、图文和图片均在解析前拒绝。
- 优先选择音视频合并直连；仅有分离轨道时只复制最佳音频直连。
- `direct_link.copy` 是协议版本 1 的增量能力。敏感直连不进入协议响应、stdout/stderr、日志或磁盘；
  worker 通过 Win32 `CF_UNICODETEXT` 直接写系统剪贴板，响应仅含 `copied` 和 `media_kind`。
- 直连任务复用统一任务生命周期、协作取消、关闭拦截和完成清理，不下载媒体文件。

## 真实问题与修复

- 首次安装版真实测试未写入直连。只检查非敏感候选特征后确认：页面提供三个候选，服务器将独立
  音频也误标为 `video/mp4`，而原判断只识别特定音乐地址形态。
- 候选分类现同时识别地址中明确的 `audio` 标记；修复后重新构建并仅替换安装目录 worker。
- 用户给定的单条抖音分享文本没有写入源码、测试、日志或交接；解析后的地址也从未输出或落盘。

## 测试和验收

- Python 离线 smoke：19/19。
- .NET Release：50/50，0 失败、0 跳过。
- PyInstaller worker：构建、握手、正常关闭、`direct_link.copy` 能力及安全拒绝测试通过。
- 暂存 UIA：启动焦点为分享文本输入框，一次 Tab 到处理模式，四个选项可读；安全路径任务结束复位
  “只下载”，Cancel 禁用。
- 安装版真实验收：系统剪贴板序列发生变化，剪贴板中只有一个有效 HTTP(S) 直连；仅在内存中做
  Range 探测并返回 HTTP 206。服务器仍将该音频轨标作 `video/mp4`，产品按明确音频标记正确选择。
- 用户随后明确反馈成功，真实验收通过。剪贴板地址未显示、未记录、未保存。
- `git diff --check` 无空白错误；敏感模式和用户测试短链的暂存扫描无命中。

## 当前安装版直接替换

安装目录：`C:\Users\apple007\AppData\Local\Programs\飞船下载工具`

- `飞船下载转换工具.dll`：129024 字节；SHA-256
  `A39A2EF9D5F689CACA53C4018494FB7B9323CA082DCB9252A382CF273B6FEEBA`
- `feichuan-worker.exe`：18747058 字节；SHA-256
  `882BF2C1241E965FEA328608A30A5288DE4505DEBAC0B37D238894C71ABA93C7`
- `使用说明.txt`：7924 字节；SHA-256
  `7B0C064455CD4CC323AB2FBAE7CF77ADCD92FBC66F1E83974F87ED5CE5040377`
- 首轮三文件备份：`artifacts\direct-replacement-20260811-05\backup-installed`
- 音频识别修复前 worker 备份：`artifacts\direct-replacement-20260811-06\backup-installed`

中途断电重启后已复核首次替换文件和备份仍完整；最终三个安装文件哈希如上。设置、凭据、用户数据、
固定工具、快捷方式和卸载信息未改动。

## 本地安装包

用户在真实验收成功后明确授权生成 D 盘本地安装包，版本保持不变。

- 项目安装包：`D:\FeichuanDownloadConverterDev\release\飞船下载转换工具-Setup-1.0.exe`
- 项目审计哈希：同目录 `.sha256`
- 最终交付：`D:\飞船下载转换工具-Setup-1.0.exe`
- 大小：148145443 字节
- SHA-256：`696070FFEF7F86E3E7E64B80E0BB8B9E0CD548819864E4DA8FD75600AE42C4FC`
- 两个 EXE 哈希一致；D 盘根目录未复制 `.sha256`。
- 产品显示版本 1.0、程序集/文件版本 1.0.0.0、固定 AppId 均未变化。
- 候选目录共 301 个文件；封装 worker 协议版本 1、软件版本 1.0，并声明 `direct_link.copy`。
- Authenticode 状态为 `NotSigned`，不得称作已签名版本。

服务器更新发布仍暂停；本次没有构建、复制、上传或发布服务器更新包。

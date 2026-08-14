# 解析直连与系统剪贴板实现调研

日期：2026-08-11

## 采用依据

- yt-dlp 官方 README 说明 `--dump-single-json` 可在模拟、不下载媒体时返回单个 URL 的格式
  信息，输出模板的 `urls` 字段也能表示所选格式的实际地址。项目继续使用仓库固定的 yt-dlp，
  不引入新的下载依赖。
  https://github.com/yt-dlp/yt-dlp/blob/master/README.md
- Microsoft Win32 文档规定写剪贴板时应先取得剪贴板、写入数据并在成功后关闭；剪贴板被其它
  窗口占用时 `OpenClipboard` 会失败。worker 使用带隐藏窗口所有者的 `CF_UNICODETEXT` 写入和
  有限重试，不把直连交给 JSON Lines 或主程序。
  https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-openclipboard
  https://learn.microsoft.com/windows/win32/dataxchg/clipboard-operations

## 选择与拒绝

- 采用：只读解析单视频或单条抖音图文。视频优先最佳音画合一直连，原站只有分轨时复制最佳
  音频直连；图文固定只接受背景音频直连。
- 采用：直连只在 Python 内存和用户明确要求的系统剪贴板中出现，协议只返回复制结果和媒体类型。
- 拒绝：把签名直连放入 JSON、日志、磁盘临时文件或 .NET 进程，因为会破坏现有敏感数据边界。
- 拒绝：新增第三方剪贴板库；Win32 API 已满足 Windows x64 目标并减少供应链与许可证范围。
- 拒绝：主页、合集、频道、播放列表和直播直连导出；本功能只处理一个视频或一条图文的背景
  音频结果。

## 2026-08-14 图文背景音频补充

- 用户指定的真实抖音分享短链解析为单条 `/note/`，下载模式已经能得到独立背景音频；原直连
  协调器却在进入同一音频解析管线前主动拒绝图文，因此属于路由限制而不是键盘或剪贴板故障。
- 沿用 yt-dlp 官方 `--dump-single-json`、`--skip-download` 和 `bestaudio/best`，不增加依赖；
  yt-dlp 未返回独立音频时，继续使用现有抖音浏览器抓流并只选择已识别的音频候选。
- 图文直连不受“图文下载内容”组合框影响，不下载 WebP、音频或 `.part` 文件。

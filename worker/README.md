# 飞船下载工具

当前版本 `0.3.9`。这是原生 Windows 单窗口下载工具，支持普通链接、抖音单作品、
抖音作者主页/官方合集/Playlet 系列子合集、YouTube 单视频、播放列表和频道。所有批量
来源都会先扫描、去重、计数并等待用户确认，确认前不会写入视频文件。

## 目录

- `src`：Python 源码和 wxPython 原生 GUI。
- `tools`：固定版本的 `yt-dlp.exe`、LGPL FFmpeg/ffprobe 和校验文件。
- `licenses`：随安装包分发的第三方许可证全文。
- `dist`：PyInstaller 绿色版输出，运行时工具位于 `dist/tools`。
- `installer`：Inno Setup 6 安装器脚本和说明。
- `release`：安装包输出目录。
- `scripts/build.py`：重建主程序和逐文件 SHA-256 清单。
- `scripts/build_installer.ps1`：预检并生成 Windows 安装包。
- `scripts/run_smoke.py`：统一执行 17 项无公网副作用的离线回归。
- `THIRD_PARTY_NOTICES.txt`：固定版本、来源、许可证和哈希声明。

## 使用

源码运行：

```text
python D:\飞船下载工具开发\src\main.py
```

未选择下载文件夹时，默认保存到当前 Windows 用户的“下载”文件夹。主界面的“选择下载文件夹(&D)”支持 `Alt+D`，选择结果
原子保存到 `%LOCALAPPDATA%\FeichuanDownloader\settings.json`，以后启动继续使用。
合集、主页、频道和分类页会在下载文件夹内自动新建对应任务文件夹。增量数据库位于
`%LOCALAPPDATA%\FeichuanDownloader\state.sqlite3`。更换下载目录后，
旧目录中的有效记录不会导致新目录错误跳过。

0.3.9 保留普通网站分类页、搜索页、合集页、频道页等链接的下载前只读扫描。
检测到多个视频时，确认窗口会显示本次实际数量和标题预览，并让用户选择“下载全部”、
“只下载第一个视频”或“取消”；默认选择取消。普通单视频仍直接下载，预检失败时也不会
自动开始批量下载。抖音和 YouTube 继续使用各自原有的扫描确认流程。

主界面“品质和格式”组合框默认选择“默认最高品质下载”。也可以改为“每次下载前选择
品质或格式”，开始任务时用组合框选择本次使用的简单规则；如果当前资源没有其它可选
画质、音频格式或质量，软件会明确提示不提供其他选择。MP3 不做转换，只有来源直接
提供 MP3 时才会显示 MP3 选项。

抖音 `/share/playlet/detail/<id>` 系列子合集会走抖音批量扫描确认流程，分页发现公开
作品并动态显示本次实际数量，不把集数写死。当前真实验收目标“没爱不行 情绪与价值”
在验收时应发现 16 集；若平台内容随后增删，界面与下载任务以当次扫描结果为准。

抖音图文 `/note/<id>` 单作品会在浏览器抓流兜底中优先保存真实背景音频。若页面只捕获
到短视频预览而没有可校验音频，软件会提示未发现可下载的图文背景音频，不把短 MP4
保存为成功结果。普通抖音视频 `/video/<id>` 仍保持视频优先。

合并界面可通过可选协议字段 `douyin_note_content=images_and_audio` 请求单条图文的作品原图
和背景音频。原图只从作品详情 `image_post_info.images`/`images` 解析并保存为 WebP，不扫描
页面所有 `<img>`，也不生成批量图文说明 TXT；字段缺失或为 `audio_only` 时保持原仅音频行为。

抖音登录使用软件自己的持久 Chrome 资料：

```text
%LOCALAPPDATA%\FeichuanDownloader\DouyinChromeProfile
```

它不读取日常 Chrome profile。用户可以在界面中显式清除这份专用登录资料。

## 测试和构建

```text
python D:\飞船下载工具开发\scripts\run_smoke.py
python D:\飞船下载工具开发\tests\gui_source_smoke.py
python D:\飞船下载工具开发\scripts\build.py
python D:\飞船下载工具开发\tests\gui_smoke.py
```

生成未签名 stable 安装包（当前没有 Authenticode 证书）：

```powershell
powershell -ExecutionPolicy Bypass -File D:\飞船下载工具开发\scripts\build_installer.ps1 -AllowUnsignedStable
```

输出文件名固定为：

```text
release\飞船下载工具-Setup-0.3.9.exe
```

如果需要把安装包放到 `D:\` 根目录便于单独分发，只复制 EXE：

```powershell
powershell -ExecutionPolicy Bypass -File D:\飞船下载工具开发\scripts\copy_installer_to_d_root.ps1
```

`D:\` 根目录不保留 `.sha256` 副本；发布审计用校验文件仍保留在项目 `release` 目录。

安装器首次安装会显示目录选择页；覆盖升级沿用原目录。它使用原生 Inno Setup 控件，
支持键盘导航和屏幕阅读器。卸载不会删除 LocalAppData 中的登录资料、设置、增量状态，
也不会删除用户下载的内容。

没有显式放宽开关的正式构建仍要求主程序和安装包具有有效 Authenticode 签名，
并使用未启用 GPL/nonfree 的 FFmpeg。自签名不等同于
受信任 CA 签名，不能用于声称正式发布者身份。

## 软件更新

固定客户端标识：

```text
product_key=feichuan_download_tool
platform=windows
channel=stable
```

检查接口：

```text
GET https://update.327802521.xyz/api/v1/updates/check
```

客户端发送 `product_key`、`platform`、`channel`、`current_version`，严格读取生产响应
`data.download_url`，不会自行拼接地址。下载安装包时写入 `.part`，核对服务端大小、
流式计算 SHA-256、检查 MZ 文件头；执行前还会再次计算 SHA-256。任何一步不一致都
会删除或拒绝临时文件，并且不会启动安装器。

## 0.3.9 新增与验收范围

- 通用修复抖音图文单作品下载：`/note/` 链接优先捕获 MP3/M4A 等真实背景音频。
- 图文作品没有发现可校验音频时不再把短视频预览保存为成功结果。
- 普通抖音视频、抖音批量扫描、YouTube 和普通网站下载路径保持原有行为。

## 0.3.7 新增与验收范围

- 未选择下载文件夹时默认保存到系统“下载”文件夹；批量任务自动新建任务子文件夹。
- 默认下载来源可提供的最高品质；可选“每次下载前选择品质或格式”，按本次任务统一使用。
- 首次启动显示简短使用说明；关闭后进入正常主界面。
- 移除“回退下载核心”按钮，下载核心更新后用户界面只使用最新版。

## 0.3.6 新增与验收范围

- 重复点击桌面快捷方式时只激活已有主窗口，不再启动第二个软件窗口。
- 任务进度区新增“当前进度摘要”，用于一次读出当前下载项、总数、等待数、百分比和
  预计剩余时间。
- 首页底部新增“喜欢这个作品”按钮，打开随安装包分发的打赏二维码窗口。

## 0.3.5 Playlet 验收范围

- 支持抖音 Playlet `/share/playlet/detail/<id>` 系列子合集，复用现有抖音批量确认、进度、
  增量和重试流程；合集数量来自当次分页扫描，不使用固定常量。
- 本次真实验收目标链接动态扫描为 16 集，并已由覆盖安装后的 0.3.5 完整下载 16 个
  不同作品到 `D:\`；16 个文件均非空、通过 ffprobe 视频轨道检查，SQLite 状态均为
  `valid`，且没有 `.part` 或重复作品 ID。

- 抖音目标作者页“罐头瓶子环球游”：页面报告 336，可靠完整枚举 337 条；真实进入
  第 1/337 条下载后立即取消，未批量耗时下载全部内容。
- YouTube 频道 `UC_IJ-AgdFedHOPuw36b1kdw`：完整枚举 895 条；真实进入第 1/895 条
  下载后立即取消。
- 原有回归、普通网站列表和新增 Playlet 专项测试（合计 17 项）、源码 GUI/UIA 检查和
  PyInstaller 打包版 GUI 冒烟均通过。
- 主窗口不再包含来源页签；三个更新按钮均使用原生确认或结果提示框。
- 普通网站列表链接会先只读扫描实际数量，再由用户选择下载全部、只下载第一个或取消；
  单视频链接保持原来的直接下载体验。
- 新的 FFmpeg 为 BtbN n8.1 LGPL 静态构建，没有 `--enable-gpl` 或 `--enable-nonfree`；
  固定来源、版本和 SHA-256 见 `THIRD_PARTY_NOTICES.txt`。

## 发布限制

- 0.3.9 主程序和安装包当前未使用 Authenticode，Windows 可能显示“未知发布者”。
- 0.3.9 会发布到 stable 更新服务器；发布时不得写入或泄露服务器凭据、Cookie、token、
  媒体签名 URL、私钥或其它敏感信息。
- 不获取私密内容，不破解验证码、DRM 或访问控制；Cookie、媒体签名 URL、token、
  DecodeKey 不得写入日志、SQLite、普通 IPC、安装包或发布文档。

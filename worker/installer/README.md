# Windows 安装器

本目录使用 Inno Setup 6 生成原生 Windows 安装包。安装向导不使用自定义绘制页面，保留标准键盘导航、焦点顺序和屏幕阅读器可读控件。

安装器特性：

- 首次安装会显示安装目录页，默认为当前用户的程序目录，无需 UAC；升级时自动沿用旧路径，并在确认页读出最终目录。
- 固定 `AppId`，新版安装包会识别已安装版本、复用原安装路径并覆盖升级。
- 总是创建开始菜单项，桌面快捷方式在安装任务页可选。
- 卸载不删除 `%LOCALAPPDATA%\FeichuanDownloader`、抖音专用登录状态或已下载文件，因此升级不会丢失登录和增量下载记录。
- `dist\licenses` 中的许可证文件会完整安装到程序目录，第三方声明和许可证随二进制一起交付。
- 正式发布模式会拒绝 GPL FFmpeg 或缺少 Authenticode 签名的载荷。
- 生成的文件名为 `飞船下载工具-Setup-X.Y.Z.exe`，同时生成 UTF-8（无 BOM）的 `.sha256` 文件供发布校验并保留中文文件名。

正式构建前，需在本机 Inno Setup 中预配置签名工具，然后只把其名称放入环境变量；证书密码、Token 和私钥不得写入项目：

```powershell
$env:FEICHUAN_INNO_SIGNTOOL_NAME = "本机已配置的签名工具名"
python D:\飞船下载工具开发\scripts\build.py
powershell -ExecutionPolicy Bypass -File D:\飞船下载工具开发\scripts\build_installer.ps1
```

只有在本机测试安装向导时才可显式使用开发构建开关，该产物不得上传到 stable 更新频道：

```powershell
powershell -ExecutionPolicy Bypass -File D:\飞船下载工具开发\scripts\build_installer.ps1 -AllowDevelopmentBuild
```

如果本次发布明确接受没有代码签名证书，可显式生成“未签名 stable”安装包。该模式仍会强制校验逐文件 SHA-256、许可证文件并拒绝 GPL FFmpeg；安装目录中会写入 `发布限制.txt`，不得把它描述成已签名版本：

```powershell
powershell -ExecutionPolicy Bypass -File D:\飞船下载工具开发\scripts\build_installer.ps1 -AllowUnsignedStable
```

只检查当前 `dist` 是否满足安装包输入条件，不调用编译器：

```powershell
powershell -ExecutionPolicy Bypass -File D:\飞船下载工具开发\scripts\build_installer.ps1 -PreflightOnly
powershell -ExecutionPolicy Bypass -File D:\飞船下载工具开发\scripts\build_installer.ps1 -AllowUnsignedStable -PreflightOnly
powershell -ExecutionPolicy Bypass -File D:\飞船下载工具开发\scripts\build_installer.ps1 -AllowDevelopmentBuild -PreflightOnly
```

预检命令必须使用与实际构建相同的模式；不要用开发开关替代受限 stable 的媒体许可证检查。

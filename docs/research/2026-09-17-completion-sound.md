# 可替换任务结束提示音

按用户要求，选择生成 TXT 即自动继续腾讯云上传识别，取消原上传确认及 10 小时二次确认；仍保留时长探测、凭据校验、对象清理和恢复管线。本项明确用户授权优先于旧计划中的逐批确认规定。

提示音采用自行合成的三音 PCM WAV，无第三方音频版权或依赖；使用现有 .NET 的 SoundPlayer 从内存异步播放，不抢焦点。优先加载程序目录“任务结束.wav”，文件替换后重新启动生效；不可用时回退内置音效。读取后不持有文件句柄。

- Microsoft 官方 API：https://learn.microsoft.com/en-us/dotnet/api/system.media.soundplayer.play?view=windowsdesktop-10.0
- Inno Setup 官方文件规则：https://jrsoftware.org/ishelp/topic_filessection.htm

安装脚本在通配复制中排除该 WAV，使用 onlyifdoesntexist 单独安装；文件位于安装目录根部，不在既有 InstallDelete 目录中，覆盖安装保留用户音效。

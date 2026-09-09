# 微信小鹅通已购课程下载实现评估

日期：2026-09-09

## 用户范围与真实页面

用户明确授权下载自己已经购买、当前由微信内置浏览器打开的小鹅通课程，并要求以后能用软件
下载其他已购课程。本轮真实页面为课程目录，不读取或导出 Cookie。目录显示“已更新 10 期”，
实际为 9 个直播回放和 1 个图文项目；产品标题中的“12讲”是课程计划名称，尚未出现在目录中的
内容不能下载。实现只枚举当前目录的 `content-box`，且只接受类型文字为“直播”的条目。

## 一手资料与开源实现

1. Microsoft UI Automation 官方文档说明 `AutomationElement` 可查找外部界面元素，
   `InvokePattern.Invoke` 用于触发控件的单一动作：
   - https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.automationelement
   - https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.invokepattern.invoke
2. FFmpeg 官方协议文档确认其原生 HTTP/HLS 输入能力；项目已经固定携带 LGPL 版本，继续复用，
   不增加第三方二进制：https://ffmpeg.org/ffmpeg-protocols.html
3. `Abirdcfly/xiaoe_download` 是 MIT 项目，但仓库已于 2021-04-03 归档，要求用户抓包并把 Cookie、
   CID/RID/UID 写入全局变量，不符合当前内存敏感数据边界，拒绝复用：
   https://github.com/Abirdcfly/xiaoe_download
4. `miaoyc666/xiaoetong-video-downloader` 要求把 Cookie、app_id 和 product_id 写入配置文件，README
   同时提示 2025-06-15 后代码改版期间不要使用；仓库没有可识别的标准开源许可证文件，因此拒绝
   复制代码：https://github.com/miaoyc666/xiaoetong-video-downloader
5. `li1055107552/xiaoe-tech-decodeDemo` 与 `nemoTyrant/goose` 包含针对加密视频的自定义解码路径，
   不符合本产品“不破解 DRM/加密保护”的边界，拒绝复用：
   - https://github.com/li1055107552/xiaoe-tech-decodeDemo
   - https://github.com/nemoTyrant/goose

## 采用方案

- .NET 端使用 Windows 自带 UI Automation，仅识别当前微信窗口、当前小鹅通课程目录和其中的
  “直播”条目。开始前显示课程名、已更新数量、视频数量，并以“不开始”为安全默认。
- Python worker 只读微信 Chromium 的 `Cache/Cache_Data/data_0..3`，不打开 Cookies、History、
  Local Storage 或其他账号数据库。每节打开前建立内存快照，只选择之后出现的小鹅通 HTTPS
  HLS 请求。
- 对多个 CDN 候选读取标准 HLS 清单并选择时长最长的完整回放。发现 `EXT-X-KEY` 或不完整清单
  时安全拒绝，不实现解密。
- 签名媒体地址只存在 worker 内存中，以操作 ID 引用；不进入 JSON Lines、日志、源码或磁盘。
- 输出先写随机 `.part.mp4`，通过 ffprobe 的视频轨和音频轨校验后同卷原子提交。已有同名文件也
  必须先校验，通过才跳过；损坏文件不覆盖，另行编号。
- 文件按发布日期从旧到新稳定编号。再次扫描同一课程会跳过已有有效结果，只补目录中新出现的
  视频，图文和推荐内容始终排除。

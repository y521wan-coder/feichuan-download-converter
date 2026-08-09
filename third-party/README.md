# 第三方依赖与发行边界

当前仓库不包含 FFmpeg 二进制。

开发机 PATH 中的 FFmpeg 是 `N-124300-gdba0b078c8-20260502`，配置明确包含 `--enable-gpl --enable-version3`。它只能用于本机开发验证，禁止复制到 `artifacts/portable/current`、候选版本或公开包。

计划固定依赖：

- FFMpegCore 5.4.0，MIT。
- TencentCloudSDK.Asr 3.0.1442，腾讯云官方 SDK，Apache-2.0。
- Tencent.QCloud.Cos.Sdk 5.4.51：腾讯云 COS 官方快速入门指向 `tencentyun/qcloud-sdk-dotnet`，该官方源码仓库声明 MIT；采用并保留 MIT 文本。NuGet 包自身缺少许可证元数据，已在审计记录中保留这一缺口。

公开发行前必须放入固定 LGPL FFmpeg 的许可证全文、源码地址、版本、构建配置，以及所有 NuGet 依赖的许可证和第三方声明。

## 2026-08-09 包内审计

- `FFMpegCore 5.4.0`：`.nuspec` 声明 MIT，并指向 GitHub 提交 `ed8f899d04a1f71c481e02b2123176d5cd097a4a`。
- `TencentCloudSDK.Asr 3.0.1442`：`.nuspec` 指向腾讯官方 GitHub LICENSE；官方仓库为 Apache-2.0。
- `Tencent.QCloud.Cos.Sdk 5.4.51`：`.nuspec` 只有 id、version、authors、description 和目标框架，nupkg 中只有 COSXML.dll；但腾讯云官方快速入门的“SDK 源码下载”明确链接到 `https://github.com/tencentyun/qcloud-sdk-dotnet`，该仓库声明 MIT。允许在测试候选版中引用，公开发布前仍需再次核对包版本与源码标签的对应关系。

当前仓库随附的许可证文本：

- `licenses/FFMpegCore-MIT.txt`
- `licenses/TencentCloudSDK-Apache-2.0.txt`
- `licenses/Tencent-QCloud-COS-MIT.txt`

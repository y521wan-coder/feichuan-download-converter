# 2026-08-10 合并仓库导入与基线验证

时间：2026-08-10 07:24-07:29（北京时间）

## 导入检查

- 新目录：`D:\FeichuanDownloadConverterDev`。
- 旧目录保持原样：`D:\飞船下载工具开发`、`D:\AccessibleVideoToMp3`。
- 转换来源仓库：`main/a5bb97f`，导入前后 `git status` 均为空。
- 导入逐文件 SHA-256 比较：转换源码 36、转换测试/项目 14、飞船源码 23、飞船测试 26，
  不匹配均为 0。
- 暂存文件没有超过 5 MiB 的文件；本地 SDK、NuGet 缓存和 worker EXE 由 `.gitignore` 排除。

## 秘密扫描

对 Git 暂存文本执行以下高风险模式检查：腾讯云 `AKID`、私钥头、带值签名参数、带值
Authorization、直接字面量 SecretId/SecretKey。只命中 C# 对内存凭据对象的变量赋值，没有命中
真实密钥、私钥、签名 URL 或授权值。

## 下载内核基线

命令：

```powershell
Set-Location D:\FeichuanDownloadConverterDev\worker
python .\scripts\run_smoke.py
```

结果：`Offline smoke passed: 17 tests`。17 组全部通过，无失败。

## 转换项目基线

命令（PATH 只对当前进程加入 worker 固定媒体工具）：

```powershell
Set-Location D:\FeichuanDownloadConverterDev
$env:PATH = "D:\FeichuanDownloadConverterDev\worker\tools;$env:PATH"
& .\tools\dotnet\dotnet.exe test .\AccessibleVideoToText.slnx --configuration Release --verbosity minimal
```

结果：还原成功；Core、Infrastructure、App、Tests Release 构建成功；测试 38/38 通过，0 失败，
0 跳过。

## 结论

两套旧能力已在新目录保持基线。可以在独立 Git 历史中开始统一命名、worker 协议和主界面合并；
后续任何基线失败都按新仓库回归处理，不修改旧目录。

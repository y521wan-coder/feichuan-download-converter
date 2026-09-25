# private 目录说明（不包含任何秘密）

本目录用于存放只属于这台电脑的私有材料：GitHub 仓库专用上传密钥、主机密钥文件和
SSH 包装配置。`.gitignore` 默认排除 `private/*`，只跟踪本说明文件，因此这里的真实材料
不会进入 Git，也不会被推送到远端。

- `private/github/`：本仓库专用的 Ed25519 上传密钥、`known_hosts` 和 `ssh_config`。
- 私钥内容不得复制、粘贴、截图或写入聊天、日志、交接说明和其他文档。
- 更换电脑或移动项目目录后，`private/github/ssh_config` 中的绝对路径需要同步更新。
- 需要作废时，直接在 GitHub 仓库设置中删除对应 Deploy Key，然后删除本目录中的密钥文件。

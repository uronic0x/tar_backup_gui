# Contributing | 贡献指南

Thank you for your interest! | 感谢你的关注！

## How to contribute | 如何贡献

1. Fork the repository, create a feature branch | Fork 仓库并创建功能分支
2. Make your changes | 进行修改
3. Run the self-test before submitting | 提交前运行自检：
   ```bash
   python src/tar_backup_gui.py --selftest
   ```
4. Open a pull request | 发起 Pull Request

## Guidelines | 准则

- Keep the GUI text consistent with existing strings (currently Simplified Chinese) | 界面文案保持与现有简体中文一致
- No hardcoded local paths, usernames, or secrets in code or logs | 代码与日志中不得硬编码本地路径、用户名或密钥
- Do not commit backup archives (`.tar*`, `.mf`) — they are gitignored | 不要提交备份归档（`.tar*`、`.mf`），它们已被 gitignore

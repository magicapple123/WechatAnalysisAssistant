# 贡献指南

感谢你愿意帮助改进 WechatAnalysisAssistant。项目会处理高度敏感的本地聊天数据，因此正确性、隐私边界和可复现验证比改动规模更重要。

## 开始之前

- 仅支持 Windows 10/11 与电脑版微信 4.x；涉及密钥提取、Windows 自动化或真实数据的功能需要在 Windows 上验证。
- 功能讨论和缺陷请使用 GitHub Issue；安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。
- 不要提交微信密钥、API Key、聊天数据库、导出文件、日志、诊断包或任何可识别个人身份的数据。测试夹具必须完全匿名且不来自真实用户数据。

## 本地开发

源码开发最低需要 Python 3.10 和 Node.js 20.19；CI 使用 Python 3.10/3.12 与
Node.js 22。以下示例使用 Python 3.12。

```powershell
# Python 后端
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade "pip>=26.2,<27"
.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
.venv\Scripts\python.exe scripts\test-backend.py
.venv\Scripts\python.exe -m ruff check backend scripts --select F,E9

# React 网页端
npm --prefix frontend ci
npm --prefix frontend run check

# Electron 桌面壳
npm --prefix desktop ci
npm --prefix desktop test
npm --prefix desktop run check
```

运行后端测试时请使用 `scripts/test-backend.py`。该入口会把应用数据目录指向临时目录，避免测试读取或改写真实密钥与设置。

## 提交改动

1. 从 `main` 创建主题分支，每个 Pull Request 只解决一个清晰问题。
2. 保持现有公开 API 和本地数据格式兼容；必须破坏兼容性时，在 PR 中写明迁移方式。
3. 新增行为应包含对应测试。修复缺陷时，优先先补充能复现问题的回归测试。
4. 用户可见文字使用清晰中文；代码标识符与技术注释遵循所在文件的既有风格。
5. 提交前运行上面的受影响检查，并检查所有 Git 已跟踪源码：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check-source-safety.ps1
   ```

   该脚本检查当前 Git 已跟踪文件和 `HEAD` 可达历史中的已知敏感路径及高置信度
   凭据特征，并检查当前文件大小边界。它不会扫描未跟踪文件或穷举所有秘密格式，
   也不能证明源码不含任何凭据。仍需人工检查差异；若历史检查失败，不要只删除当前
   文件，应先轮换可能暴露的凭据，再与维护者协调净化待发布历史。

6. 只有在生成 sidecar、`win-unpacked` 或安装器时，才对每个待发布目标额外运行：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check-release.ps1 <待发布目录或文件>
   ```

7. 更新与改动相关的 README、项目指南或 `CHANGELOG.md`。

### 可选：pre-commit 钩子

仓库提供 `.pre-commit-config.yaml`，在本地提交时自动运行与 CI 相同的 ruff 检查：

```powershell
pip install pre-commit
pre-commit install
pre-commit run --all-files   # 手动全量检查
```

## Pull Request 检查清单

- [ ] 没有包含真实账号、密钥、聊天内容、数据库或本地绝对路径
- [ ] `scripts/check-source-safety.ps1` 已通过，并已人工检查待提交文件
- [ ] 后端、网页端和桌面端中受影响的测试均通过
- [ ] 新增的联网行为需要用户明确触发，并在界面与文档中说明发送内容
- [ ] 文件访问、URL、归档解压和子进程参数均经过边界校验
- [ ] 用户文档、版本说明和错误提示已同步更新

维护者会优先关注数据安全、兼容性和可验证性，并可能要求把大型 PR 拆分为更容易审查的改动。

自动测试使用匿名夹具和临时应用目录，不得通过指向真实 `%LOCALAPPDATA%` 来扩大
覆盖范围。涉及真实微信 4.x、Windows 自动化、第三方 API、安装包签名或
SmartScreen 的行为，请仅使用你有权访问的测试数据单独人工验证，并在 PR 中说明
尚未覆盖的环境。

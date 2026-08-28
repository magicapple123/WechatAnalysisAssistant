# 桌面版开发与发布指南

本文面向 Windows 桌面版维护者。正式产物由 Electron 外壳、React
`frontend/dist` 和 PyInstaller `onedir` 后端组成；最终用户不需要安装
Python、Node.js 或 FFmpeg。

## 1. 发布边界与目录

- 后端 sidecar：`dist/backend/WechatAnalysisAssistantBackend/`
- Electron 后端暂存：`desktop/resources/backend/`（由构建脚本生成，不提交
  Git）；前端由 Electron Builder 直接读取 `frontend/dist`
- Electron/NSIS 产物：`desktop/dist-electron/`
- PyAV 自带的 FFmpeg DLL 位于 sidecar 的 `av.libs/`，无需调用系统
  `ffmpeg.exe`
- 用户设置、密钥、数据库、图片/头像/语音缓存只能在运行时写入用户目录，
  绝不能成为安装包资源

以下内容被明确禁止进入产物：`backend/settings.json`、`wechat_key*`、
`wechat_keys*`、任何 `*.db`/`*.sqlite*`、缓存和诊断数据、`.venv`、
`node_modules`、`wechat-decrypt-main` 及完整源码目录。

这里的 `node_modules` 禁令指开发依赖目录不能被原样复制到资源或安装目录。
`app.asar` 内允许 Electron 运行所必需的最小生产依赖（当前为
`electron-updater` 及其传递依赖）；`electron`、`electron-builder` 等开发/构建
包仍会被发布扫描拒绝。

## 2. 构建环境

建议使用干净的 Windows x64 构建机。项目最低要求 Python 3.10 与
Node.js 20.19；当前 CI/发布流水线使用 Python 3.12 和 Node.js 22 LTS。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade "pip>=26.2,<27"
.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
.venv\Scripts\python.exe -m pip install -r packaging\requirements-build.txt
npm ci --prefix frontend
npm ci --prefix desktop
```

构建依赖与实际导入审计见
[`packaging/DEPENDENCY_AUDIT.md`](packaging/DEPENDENCY_AUDIT.md)。不要把
`.venv` 或两个 `node_modules` 复制到发布目录。

Hook 自动提取所用的 `wx_key` 不是必需运行时依赖，本项目目前也没有可据以公开
再分发其二进制的已验证授权，因此公开源码和默认 GitHub 安装包不会捆绑它。构建
脚本只在维护者显式传入 `-IncludeWxKey` 时尝试收集和探测；默认构建即使在本机发现
该模块也会明确排除。运行时缺少组件时，后端会在不关闭微信的情况下使用本机内存
扫描，并保留手动输入密钥。不要把个人机器上的 wheel 复制到仓库或公开产物。

## 3. 开发运行

Web 调试仍可使用根目录 `restart.bat`。Electron 调试使用：

```powershell
npm run dev --prefix desktop
```

桌面外壳应只绑定 `127.0.0.1`，为每次启动生成随机会话令牌，并通过启动参数
或 `WECHAT_ASSISTANT_DESKTOP_TOKEN` 传给 sidecar。不要监听局域网地址，也不要
把固定令牌写入源码、日志或安装包。

## 4. 一键构建

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts\build-desktop.ps1 -Channel beta
```

脚本按以下关键顺序执行：

1. `npm run build --prefix frontend`
2. PyInstaller `onedir` sidecar（检查可选 Hook 组件，缺失时使用安全降级路径）
3. 敏感文件扫描
4. 将完整 sidecar 目录同步至 `desktop/resources/backend/`
5. Electron 单元测试、配置检查和 Electron Builder/NSIS
6. 对 `win-unpacked` 再次进行敏感文件扫描并读取实际 Electron fuses
7. 隐藏启动 `win-unpacked`，用专用最小 `app://` 页面验证 preload 桥、API 代理和
   sidecar 后自动退出（刻意不加载 React）

脚本不会自动安装依赖；缺少 `node_modules` 或 PyInstaller 时会直接失败并给出
安装命令。分阶段排错可以使用 `-SkipFrontend`、`-SkipBackend`、
`-SkipElectron` 或 `-SkipSmokeTest`，但正式发布必须跑一次完整构建且不能跳过
烟雾测试。

也可以单独检查暂存产物：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts\check-release.ps1 `
  desktop\dist-electron\win-unpacked
```

对于 `app.asar`，检查脚本使用本地 Electron/asar CLI 展开文件列表；找不到
本地 CLI 时按失败处理，不会“跳过并通过”。

也可以对现有的未打包目录单独执行整机烟雾测试：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts\smoke-desktop.ps1
```

该脚本为后端和 Electron 用户数据创建独立临时目录，并只加载专用的最小
`app://` 页面，不读取或覆盖用户的正式设置。烟测标志会关闭本地微信账号发现，且
后端只允许桌面 health/self-test/prepare-exit 路由；`/api/status`、
`/api/auto-detect` 等真实数据路由会被拒绝。主进程只有在最小页面、preload 安全桥、
带令牌 API 代理和 sidecar 自检都可用时才返回成功。

## 5. NSIS 安装包检查

Electron Builder 的 Windows 目标应为 NSIS，并在 `desktop/package.json` 中保持：

- `oneClick: false`，允许用户看清安装行为；
- 默认按当前用户安装，除非某个功能确实需要管理员权限；
- 卸载时不删除用户导出的聊天记录、分析报告或微信原始数据；
- `extraResources` 只接收 `../frontend/dist` 和生成的
  `resources/backend/WechatAnalysisAssistantBackend`，构建说明文件不会进入安装包；
- 不使用 `extraFiles: ["../**/*"]` 这类宽泛规则；
- Electron 主程序、安装器和更新元数据使用同一版本；sidecar 在启动握手时必须
  返回完全一致的应用版本和桌面 API 版本，否则主程序拒绝继续。

先验证 `desktop/dist-electron/win-unpacked`，再生成/上传安装器；不要仅扫描
最终 NSIS `.exe`，因为它是压缩容器，普通文件遍历无法证明内部没有敏感数据。

## 6. Windows 代码签名

正式版必须使用受信任的 Authenticode 证书。推荐在隔离的 CI secret 或硬件
令牌中保存证书，不要把 `.pfx`、密码或云签名凭证放进仓库。

Electron Builder 可读取：

```powershell
$env:CSC_LINK = "<CI secret or certificate URL>"
$env:CSC_KEY_PASSWORD = "<CI secret>"
```

若签名流程不由 Electron Builder 托管，顺序应为：先签
`WechatAnalysisAssistantBackend.exe`，再让 Electron Builder 打包并签主程序、
卸载器和 NSIS 安装器。统一使用 SHA-256 和可信 RFC 3161 时间戳服务器。发布前
用 `Get-AuthenticodeSignature` 或 `signtool verify /pa /all` 验证每个 PE 文件。

Beta 可以在内部测试阶段暂时使用独立测试证书，但对外下载页必须清楚标记；正式
渠道不能发布未签名或签名无效的安装包。

## 7. GitHub Releases 在线更新

`desktop/package.json` 使用 Electron Builder 的 GitHub provider，固定指向
`Magicapple-Coder/WechatAnalysisAssistant`。安装时生成的 `app-update.yml` 由桌面
运行时读取并校验 provider、owner 与 repo；无效或不完整的配置会禁用更新，而不是
回退到任意地址。

- SemVer 预发布版本使用 `beta` 频道并读取 `beta.yml`；
- 稳定版本使用 `latest` 频道并读取 `latest.yml`；
- 检查、下载和安装都由用户在界面中主动触发，下载完成后也不会自动重启安装；
- 发布工作流只上传同一构建产生且经 SHA-256 清单复核的安装器、`.blockmap` 和渠道
  元数据，不覆盖已有同名 GitHub Release。

更新元数据中的文件名、版本、大小与 SHA-512 必须和实际文件一致，不得手工编辑。
若撤回版本，应发布更高版本的修复包，不要替换既有标签或资产。GitHub provider
负责传输渠道，不替代 Authenticode 签名和发布页 SHA-256 清单。

## 8. Beta 发布流程

1. 使用 SemVer 预发布号，例如 `1.2.0-beta.1`，同步桌面包版本和“关于”页版本。
2. 使用 `scripts/test-backend.py` 运行隔离的后端测试，再运行前端测试、Electron
   测试和配置检查；测试数据目录不得指向正式 `%LOCALAPPDATA%`。
3. 完整运行 `build-desktop.ps1 -Channel beta`，确认两次发布安全扫描均通过。
4. 已配置签名凭据时，对 sidecar、Electron 主程序和安装器签名并验证；未配置时
   只能发布明确标记为“未签名”的 Beta，并保留 SmartScreen 与 SHA-256 核验提示。
5. 在无 Python/Node 的干净虚拟机执行下节验收。
6. 只上传 Beta 渠道，附变更说明、已知问题和回退方式。
7. 收集崩溃信息时先征得用户同意，并在提交前移除微信号、聊天内容、密钥和本地
   路径；诊断数据永远不随下一版安装包回传。

仓库提供 `.github/workflows/desktop-release.yml`。推送与三个应用版本完全一致的
标签（例如 `v0.1.0-beta.3`）后，GitHub Actions 会在干净 Windows runner 上重跑
测试、完整构建、两次敏感内容扫描、fuse 检查和桌面烟雾测试，然后生成 SHA-256
清单并创建 GitHub prerelease。手动触发只生成有期限的 Actions artifact，不会发布
GitHub Release。没有配置 `CSC_LINK` / `CSC_KEY_PASSWORD` 时仅允许构建未签名
Beta，发布说明和 README 必须继续保留 SmartScreen 提示；正式渠道会拒绝未签名
产物。已有同名 GitHub Release 时工作流应失败，必须提升版本而不是覆盖既有资产。

### 自动验证边界

单元测试、静态配置检查、敏感文件扫描、fuse 读取和桌面烟雾测试只验证其明确覆盖
的条件。烟雾测试使用临时目录、禁止本地微信探测，并刻意不加载生产 React 页面；
因此 CI 不能替代实际界面、微信 4.x 账号与数据库、第三方 API、Authenticode
信任链、SmartScreen 信誉或杀毒软件环境下的人工验收。只有在 CI 真实执行成功并
完成下节干净机清单后，才能在发布说明中声称对应安装包已通过这些检查；本地脚本
或 YAML 语法检查本身不等于安装包已验证。

## 9. 干净机验收清单

至少在一台从未安装开发环境的 Windows 10/11 x64 虚拟机验证：

- 安装、首次启动、单实例、正常退出、卸载和覆盖升级；
- 系统没有 Python、Node.js、FFmpeg，也能启动 sidecar 并通过健康检查；
- Electron 只连接随机回环端口，关闭主窗口后 sidecar 不残留；
- 微信 4.x 账号检测、数据库密钥和 16 位图片 AES 密钥流程；
- 普通图片、WXGF/HEVC、HEIF/AVIF 表情和朋友圈媒体；
- SILK 语音播放/导出、语音转文字 API、图片识别与 AI 分析；
- 联系人、朋友圈置顶、聊天与 HTML/JSON/CSV/TXT 导出；
- 中文用户名、超长路径、非管理员账户、Windows 高 DPI；
- 断网、API 超时、微信未运行、微信数据库只读/占用等失败路径；
- 已签名候选应在 Windows Defender/SmartScreen 下验证签名链；未签名 Beta 应确认
  下载页明确提示 SmartScreen 风险。两者都要检查安装目录没有密钥、数据库、缓存、
  `.venv`、`node_modules` 或源码目录；
- 卸载后用户主动导出的文件保留，应用私有缓存按产品隐私说明处理。

每个版本只能发布完成上述验收的那一份不可变产物，不要在验收后重新构建并沿用原
结论。稳定版使用新的稳定 SemVer，必须重新构建、签名并完成同等验收；未签名 Beta
不能改名或直接提升为正式版。

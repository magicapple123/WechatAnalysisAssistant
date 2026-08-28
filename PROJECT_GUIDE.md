# 微信解析助手 — 项目完全指南

> 版本 v0.1.0-beta.3 | 2026-08-27 | 这份文档帮助你理解整个项目的架构、运作方式以及如何手动修改它。

---

## 目录

1. [项目是什么](#1-项目是什么)
2. [整体架构（一张图看懂）](#2-整体架构)
3. [目录结构总览](#3-目录结构总览)
4. [技术栈速览](#4-技术栈速览)
5. [后端详解](#5-后端详解)
   - [入口与生命周期](#入口与生命周期)
   - [API 路由全表](#api-路由全表)
   - [数据解密管线](#数据解密管线)
   - [密钥提取](#密钥提取)
   - [图片管线](#图片管线)
   - [语音/ASR 管线](#语音asr-管线)
   - [表情包管线](#表情包管线)
   - [朋友圈管线](#朋友圈管线)
   - [AI 分析引擎](#ai-分析引擎)
   - [导出功能](#导出功能)
   - [配置与设置](#配置与设置)
6. [前端详解](#6-前端详解)
   - [应用状态机](#应用状态机)
   - [组件树与职责](#组件树与职责)
   - [API 通信层](#api-通信层)
   - [桌面桥接](#桌面桥接)
   - [样式系统](#样式系统)
7. [Electron 桌面壳详解](#7-electron-桌面壳详解)
   - [主进程启动流程](#主进程启动流程)
   - [IPC 桥接表](#ipc-桥接表)
   - [后端子进程管理](#后端子进程管理)
   - [自定义协议与安全模型](#自定义协议与安全模型)
   - [自动更新](#自动更新)
8. [构建与打包](#8-构建与打包)
   - [完整构建流水线](#完整构建流水线)
   - [PyInstaller 冻结](#pyinstaller-冻结)
   - [DLL 运行时处理](#dll-运行时处理)
   - [安全检查](#安全检查)
   - [安装包生成](#安装包生成)
9. [关键数据流](#9-关键数据流)
10. [常见修改场景指南](#10-常见修改场景指南)
11. [文件索引](#11-文件索引)

---

## 1. 项目是什么

**微信解析助手**是一个 Windows 本地应用，用于浏览、搜索、处理和分析本机微信
聊天记录。它既可作为 Electron 桌面安装包运行，也可从源码启动本机后端并在浏览器
中使用同一套 React 界面；后者不是托管网站。

### 核心功能

| 功能 | 说明 |
|------|------|
| 🔑 密钥提取 | 默认从已运行的微信进程内存获取；授权构建可选择 Hook 增强 |
| 💬 聊天浏览 | 像微信一样查看聊天记录，支持文字/图片/表情/语音/视频/红包/转账 |
| 🖼️ 图片处理 | 解密并显示聊天图片，支持缩略图/高清切换，批量获取高清原图 |
| 👁️ 图片识别 | 对接 16+ AI 视觉模型，批量为聊天图片生成文字描述 |
| 🎤 语音转文字 | 解码 SILK 音频，对接 7+ ASR 服务将语音转为文字 |
| 🤖 AI 分析 | 对接 18+ 大模型，对聊天记录或朋友圈进行情感/主题/风险分析 |
| 📤 多格式导出 | 导出聊天为 HTML/JSON/CSV/TXT，支持嵌入图片和语音文字 |
| 🌐 朋友圈 | 浏览、导出和分析本地朋友圈（SNS）数据 |
| 🔒 隐私优先 | 数据库读取、解密和导出在本机完成；外部模型请求只由用户显式触发 |

### 适用环境

- **操作系统**: Windows 10 / 11 (x64)
- **微信版本**: WeChat 4.x
- **Python**: 3.10+
- **Node.js**: 20.19+（CI 与发布推荐 22 LTS）

---

## 2. 整体架构

桌面模式由**三个独立进程**组成，通过 HTTP + IPC 通信；源码网页版以普通浏览器
替代 Electron 主/渲染进程，并直接访问绑定在回环地址上的本机后端：

```
┌──────────────────────────────────────────────────────────┐
│                    Electron 主进程                         │
│                    (desktop/src/main.mjs)                  │
│                                                           │
│  ┌──────────────┐  ┌───────────────┐  ┌───────────────┐  │
│  │ 窗口管理      │  │ IPC 处理      │  │ 自动更新       │  │
│  │ (连接/主窗口) │  │ (8 个信道)    │  │ (electron-    │  │
│  │              │  │              │  │  updater)     │  │
│  └──────────────┘  └───────────────┘  └───────────────┘  │
│         │                  │                  │           │
│         ▼                  ▼                  │           │
│  ┌──────────────────────────────────────────┐           │
│  │        BackendManager                     │           │
│  │  启动/健康检查/优雅关闭 Python 后端        │           │
│  └──────────────────────────────────────────┘           │
│         │                                                │
│         │  spawn(exe, --desktop --port N)                │
│         ▼                                                │
│  ┌──────────────────────────────────────────┐           │
│  │  自定义 app:// 协议处理器                   │           │
│  │  • /api/* → 代理到 127.0.0.1:N (注入令牌) │           │
│  │  • /*     → 静态文件 (frontend/dist/)     │           │
│  │  • CSP / X-Content-Type-Options 安全头    │           │
│  └──────────────────────────────────────────┘           │
└──────────────────────────────────────────────────────────┘
         │                              │
         │ app://wechat-analysis-       │ 127.0.0.1:random
         │ assistant/                   │ + session-token
         ▼                              ▼
┌─────────────────────┐    ┌──────────────────────────────┐
│   前端 (渲染进程)      │    │     Python 后端 (FastAPI)      │
│   React 18 + Vite    │    │                              │
│                      │    │  /api/status                 │
│  App.jsx (状态机)     │    │  /api/set-key                │
│  ├── KeyInput        │    │  /api/chats                  │
│  ├── ChatList        │    │  /api/chat/{talker}          │
│  ├── ChatView         │   │  /api/chat/{talker}/image/... │
│  ├── ContactsView    │    │  /api/chat/{talker}/voice/...│
│  ├── AccountProfile  │    │  /api/moments/*              │
│  ├── ExportDialog    │    │  /api/analysis/*             │
│  ├── SettingsDialog  │    │  /api/export                  │
│  └── AiAnalysisDialog│    │  /api/settings                │
│                      │    │                              │
│  通信: api.js (axios) │    │  微信数据读取:                 │
│       → /api/*       │    │  parser_v4.py → SQLite       │
│       → desktop.js   │    │  (解密后的 message_N.db 等)    │
│         (IPC桥接)     │    │                              │
└─────────────────────┘    └──────────────────────────────┘
```

### 三个进程如何协作

1. **用户双击 exe** → Electron 主进程启动
2. 主进程生成一个随机端口 + 256-bit 会话令牌
3. 主进程 spawn Python 后端子进程，传入端口和令牌
4. 主进程轮询 `/api/desktop/health` 直到后端就绪
5. 主进程创建 BrowserWindow，加载 `app://wechat-analysis-assistant/`
6. 渲染进程（React）通过 `app://` 协议加载 → 前端代码发起 `/api/*` 请求
7. `app://` 协议处理器拦截 `/api/*` 请求，注入令牌，代理到 `127.0.0.1:port`
8. Python 后端处理请求，读取本地微信数据库，返回 JSON/图片/音频

---

## 3. 目录结构总览

```
WechatAnalysisAssistant/
├── frontend/                    # React 前端 (Vite + Tailwind)
│   ├── src/
│   │   ├── main.jsx             # 入口
│   │   ├── App.jsx              # 根组件 (状态机)
│   │   ├── api.js               # HTTP 客户端 (axios)
│   │   ├── desktop.js           # Electron IPC 桥接
│   │   ├── modelInterfaces.js   # AI 模型预设注册表
│   │   ├── index.css            # 全局样式 + Tailwind
│   │   └── components/
│   │       ├── ChatView.jsx     # 聊天详情与主要交互
│   │       ├── ChatList.jsx     # 会话列表
│   │       ├── KeyInput.jsx     # 密钥输入页 (连接页)
│   │       ├── StatusBar.jsx    # 顶栏
│   │       ├── AppRail.jsx      # 侧边/底部导航
│   │       ├── ContactsView.jsx # 通讯录
│   │       ├── AccountProfile.jsx # 账号资料页
│   │       ├── ExportDialog.jsx # 导出对话框
│   │       ├── MomentsExportDialog.jsx # 朋友圈导出
│   │       ├── SettingsDialog.jsx  # 设置
│   │       ├── AiAnalysisDialog.jsx # AI 分析对话框
│   │       ├── Avatar.jsx       # 头像组件
│   │       └── aiAnalysisDialogUtils.js # Markdown 解析等工具
│   ├── dist/                    # 构建产物 (npm run build)
│   ├── package.json
│   ├── vite.config.js
│   └── tailwind.config.js
│
├── backend/                     # Python 后端 (FastAPI)
│   ├── main.py                  # CLI 入口 (uvicorn 启动)
│   ├── api.py                   # FastAPI 应用 + 全部路由
│   ├── version.py               # APP_VERSION = "0.1.0-beta.3"
│   ├── config.py                # WeChatConfig (微信数据目录发现)
│   ├── settings.py              # 用户设置持久化
│   ├── app_paths.py             # 应用数据目录管理
│   ├── key_extractor.py         # 数据库密钥提取 (内存扫描)
│   ├── decrypt.py               # WCDB 解密 (AES-256-CBC + WAL)
│   ├── wcdb_key_scanner.py      # WCDB 密钥缓存扫描
│   ├── parser_v4.py             # 微信 4.x 消息解析器
│   ├── image_service.py         # 聊天图片解密与服务
│   ├── image_store.py           # AI 图片描述缓存 (SQLite)
│   ├── image_recognition.py     # 图片识别任务管理器
│   ├── image_hd_resource.py     # 高清图片资源检查
│   ├── image_hd_automation.py   # 高清图片键盘自动化
│   ├── image_key_extractor.py   # 图片 V2 AES 密钥提取
│   ├── wechat_viewer_automation.py # SendInput 键盘控制
│   ├── vision.py                # 视觉模型客户端 (OpenAI/Claude/Gemini/...)
│   ├── voice_service.py         # 语音消息解析 (SILK → WAV)
│   ├── voice_store.py           # 语音转文字缓存 (SQLite)
│   ├── voice_transcription.py   # 语音转文字任务管理器
│   ├── transcription.py         # ASR 客户端 (OpenAI/DashScope/Groq/...)
│   ├── sticker_service.py       # 表情包服务 (CDN 下载 + 解密 + 转码)
│   ├── avatar_service.py        # 头像服务
│   ├── moments.py               # 朋友圈解析与导出
│   ├── moments_media.py         # 朋友圈媒体解密与存储
│   ├── sns_media_crypto.py      # ISAAC-64 流密码 (SNS 图片解密)
│   ├── ai_analysis.py           # AI 分析引擎 (Map-Reduce)
│   ├── model_interfaces.py      # AI 接口预设注册表
│   ├── exporter.py              # 聊天导出 (HTML/JSON/CSV/TXT)
│   ├── subprocess_env.py        # 子进程环境清理
│   ├── time_utils.py            # 时间格式化工具
│   └── tests/                   # 后端测试
│
├── desktop/                     # Electron 桌面壳
│   ├── src/
│   │   ├── main.mjs             # Electron 主进程入口
│   │   ├── preload.cjs          # 沙箱预加载 (contextBridge)
│   │   ├── backend-manager.mjs   # Python 后端生命周期管理
│   │   ├── app-protocol.mjs     # app:// 自定义协议
│   │   ├── update-manager.mjs   # 自动更新 (electron-updater)
│   │   ├── runtime-config.mjs   # 配置解析 + URL 构建
│   │   └── protocol-utils.mjs   # 请求/响应代理工具
│   ├── test/                    # Node 测试
│   ├── scripts/
│   │   ├── check-config.mjs     # 打包前配置校验
│   │   ├── check-fuses.mjs      # Electron 安全熔断器校验
│   │   └── smoke-backend.mjs    # 后端集成烟雾测试
│   ├── resources/
│   │   ├── backend/             # PyInstaller 后端副本 (构建时生成，不入库)
│   │   └── frontend/            # Vite 前端副本 (构建时生成，不入库)
│   ├── dist-electron/           # 构建输出 (不入库)
│   │   ├── win-unpacked/        # 未打包的应用 (可直接运行测试)
│   │   └── *.exe                # NSIS 安装包
│   └── package.json
│
├── packaging/                   # PyInstaller 打包配置
│   ├── wechat_backend.spec      # PyInstaller spec (onedir)
│   ├── requirements-build.txt   # PyInstaller + hooks-contrib
│   ├── DEPENDENCY_AUDIT.md      # 依赖审计文档
│   ├── hooks/                   # 自定义 PyInstaller hooks
│   │   ├── hook-av.py           # PyAV FFmpeg DLLs
│   │   ├── hook-pillow_heif.py  # libheif DLLs
│   │   └── hook-pysilk.py       # SILK 双后端
│   └── runtime_hooks/
│       └── windows_dll_paths.py # 运行时 DLL 目录注册
│
├── scripts/                     # 构建与测试脚本
│   ├── build-desktop.ps1        # 完整构建流水线
│   ├── verify-packaging-runtime.py # 冻结前运行时验证
│   ├── check-release.ps1        # 发布安全扫描
│   ├── check-source-safety.ps1  # 源码与可达历史隐私扫描
│   ├── smoke-desktop.ps1        # 打包后烟雾测试
│   ├── test-backend.py          # 后端测试运行器
│   ├── start-web.ps1            # 网页版开发启动器
│   └── generate-desktop-icon.py # 图标生成器
│
├── .github/                     # CI、CodeQL、发布流与协作模板
│   └── workflows/
│       ├── ci.yml
│       ├── codeql.yml
│       └── desktop-release.yml
│
├── dist/                        # PyInstaller 构建产物 (不入库)
│   └── backend/
│       └── WechatAnalysisAssistantBackend/
│           ├── WechatAnalysisAssistantBackend.exe
│           └── _internal/       # Python 运行时 + 所有 .pyd/.dll
│
├── README.md                    # 用户面向文档
├── DESKTOP_RELEASE.md           # 发布流程 (面向维护者)
├── CHANGELOG.md                 # 版本变更记录
├── CONTRIBUTING.md              # 贡献指南
├── SECURITY.md                  # 安全与漏洞报告策略
├── CODE_OF_CONDUCT.md           # 社区行为准则
├── LICENSE                      # 项目源码 MIT 许可证
├── restart.bat                  # 一键开发启动
└── .gitignore
```

---

## 4. 技术栈速览

| 层 | 技术 | 用途 |
|----|------|------|
| **前端** | React 18, Vite 6, Tailwind CSS 3, Axios | SPA 界面 |
| **后端** | Python 3.10+, FastAPI, Uvicorn | HTTP API + 微信数据处理 |
| **桌面壳** | Electron 43, electron-builder, NSIS | Windows 桌面打包 |
| **数据库** | SQLite / WCDB (AES-256-CBC 解密) | 微信本地数据库 |
| **图片** | Pillow, pillow-heif, PyAV (FFmpeg), PyCryptodome | 图片解密/解码/转码 |
| **音频** | pysilk (SILK V3), Python wave | 语音解码 |
| **进程** | pymem, pefile, yara-python | 微信进程内存读取 |
| **原生模块** | 可选 wx_key (.pyd), SendInput API | 授权构建的 Hook 增强、键盘自动化 |
| **AI** | OpenAI/Claude/Gemini/DashScope 等多协议兼容 | 视觉/语音/分析 |
| **打包** | PyInstaller (onedir), electron-builder (NSIS) | 独立 exe + 安装包 |

---

## 5. 后端详解

### 入口与生命周期

**`backend/main.py`** 是命令行入口。启动流程：

```
main()
  ├── parse args (--host, --port, --desktop, --parent-pid, --session-token)
  ├── resolve_runtime_options()
  │     └── 判断桌面模式 vs Web 模式
  ├── run_server()
  │     ├── 创建 uvicorn.Server
  │     ├── 注册 shutdown 回调 (取消后台任务, 关闭数据库)
  │     ├── 如果 --desktop: 绑定 127.0.0.1, 打印端口给 Electron
  │     └── 如果 --parent-pid: 启动父进程监控线程
  └── server.run()
```

### API 路由全表

所有路由定义在 **`backend/api.py`**。以下是 API 功能索引：

#### 状态与设置
| 路由 | 方法 | 用途 |
|------|------|------|
| `/api/status` | GET | 系统状态 (微信账号, 数据库, 密钥) |
| `/api/auto-detect` | GET | 自动检测微信数据 + 密钥 |
| `/api/set-key` | POST | 手动设置 64 位解密密钥 |
| `/api/extract-key` | POST | Hook 注入自动提取密钥 |
| `/api/key` | DELETE | 删除当前账号密钥 |
| `/api/switch-account` | POST | 切换微信账号 |
| `/api/clear-cache` | POST | 清理所有缓存 |
| `/api/settings` | GET/POST | 读/写用户设置 |
| `/api/settings/*/api-key/reveal` | GET | 查看已保存的 API 密钥 |

#### 桌面模式专用
| 路由 | 方法 | 用途 |
|------|------|------|
| `/api/desktop/health` | GET | 握手端点 (Electron 用于验证后端就绪) |
| `/api/desktop/self-test` | POST | 烟雾测试 (验证所有 .pyd 可加载) |
| `/api/desktop/prepare-exit` | POST | 优雅关闭准备 |
| `/api/select-folder` | POST | 原生文件夹选择器 |

#### 聊天与通讯录
| 路由 | 方法 | 用途 |
|------|------|------|
| `/api/chats` | GET | 会话列表 |
| `/api/contacts` | GET | 通讯录 |
| `/api/chat/{talker}` | GET | 分页消息 (支持类型/关键词/时间筛选) |
| `/api/chat/{talker}/timerange` | GET | 会话时间范围 |
| `/api/message-position/{talker}/{msg_id}` | GET | 定位消息所在页 |
| `/api/search` | GET | 全局搜索 |
| `/api/statistics` | GET | 统计数据 |

#### 图片
| 路由 | 方法 | 用途 |
|------|------|------|
| `/api/chat/{talker}/image/{message_id}` | GET | 返回解密后的聊天图片 |
| `/api/chat/{talker}/images/recognize` | POST | 批量图片识别 (AI 视觉) |
| `/api/image-recognition/tasks/{task_id}` | GET | 查询识别任务进度 |
| `/api/image-key/reveal` | GET | 查看图片 V2 AES 密钥 |
| `/api/image-key/extract` | POST | 自动提取图片 AES 密钥 |
| `/api/chat/{talker}/images/hd-automation` | POST | 高清图片键盘自动化 |
| `/api/image-hd-automation/tasks/{task_id}` | GET | 查询 HD 自动化进度 |
| `/api/image-hd-automation/active` | GET | 当前活跃的 HD 任务 |
| `/api/image-hd-automation/tasks/{task_id}/pause` | POST | 暂停 HD 自动化 |
| `/api/image-hd-automation/tasks/{task_id}/navigation-mode` | POST | 切换自动/手动导航 |
| `/api/image-hd-automation/tasks/{task_id}/cancel` | POST | 取消 HD 自动化 |

#### 语音
| 路由 | 方法 | 用途 |
|------|------|------|
| `/api/chat/{talker}/voice/{msg_id}/audio` | GET | 流式返回解码后的 WAV 音频 |
| `/api/chat/{talker}/voice/{msg_id}/export` | GET | 下载 WAV 文件 |
| `/api/chat/{talker}/voices/transcribe` | POST | 批量语音转文字 |
| `/api/voice-transcription/tasks/{task_id}` | GET | 查询转录任务进度 |
| `/api/voice-transcription/tasks/{task_id}/cancel` | POST | 取消转录 |

#### 表情包 / 头像
| 路由 | 方法 | 用途 |
|------|------|------|
| `/api/chat/{talker}/sticker/{message_id}` | GET | 返回自定义表情 (HMAC 签名 URL) |
| `/api/avatar/{username}` | GET | 返回真实头像 (ETag 缓存) |

#### 朋友圈
| 路由 | 方法 | 用途 |
|------|------|------|
| `/api/moments/contacts` | GET | 朋友圈联系人 |
| `/api/moments/preview` | POST | 分页预览 |
| `/api/moments/media/{tid}/{media_index}` | GET | 朋友圈媒体 |
| `/api/moments/media/load` | POST | 加载本地媒体 |
| `/api/moments/export` | POST | 导出朋友圈 |

#### AI 分析
| 路由 | 方法 | 用途 |
|------|------|------|
| `/api/analysis/chat/{talker}` | POST | 分析聊天记录 |
| `/api/analysis/moments` | POST | 分析朋友圈 |

#### 导出
| 路由 | 方法 | 用途 |
|------|------|------|
| `/api/export` | POST | 导出单个会话 |
| `/api/export-all` | POST | 导出所有会话 (ZIP) |

### 数据解密管线

这是整个项目最核心的管线——所有微信数据都经过它：

```
1. 发现微信数据目录
   config.py: detect_all_accounts()
   ├── 扫描 C-G 盘
   ├── 读取 %APPDATA%/Tencent/xwechat/config/*.ini
   └── 匹配 xwechat_files/wxid_*/db_storage/ 模式

2. 获取解密密钥
   key_extractor.py: find_key_auto()
   ├── wechat_keys.json (已保存的密钥)
   ├── pymem 内存扫描 (公开/默认构建)
   └── wx_key Hook DLL 注入 (仅显式包含组件的授权构建)

3. 解密数据库
   decrypt.py: DatabaseDecryptor
   ├── 读取加密文件头 16 字节 salt
   ├── PBKDF2-HMAC-SHA512 (256000 迭代) → 32 字节 AES key
   ├── 逐页 AES-256-CBC 解密 (每页 4096 字节)
   ├── 每页尾部 HMAC-SHA512 完整性校验
   ├── WAL 日志回放 (可选)
   └── 输出临时解密副本 → sqlite3.connect()

4. 解析消息
   parser_v4.py: MessageParserV4
   ├── 读取 message_N.db (多分片)
   ├── Name2Id 映射 (发送者 ID → 微信名)
   ├── XML 子消息解析 (红包, 引用, 合并转发)
   ├── zstd 解压 (微信 4.x 压缩负载)
   └── 输出结构化消息列表

5. 服务层处理
   ├── 图片: image_service.py (解密 .dat 文件 → JPEG/PNG)
   ├── 语音: voice_service.py (提取 SILK → 解码 → WAV)
   ├── 表情: sticker_service.py (CDN 下载 → 解密 → 转码)
   └── 头像: avatar_service.py (本地 db → CDN 下载 → 缓存)
```

### 密钥提取

有两套密钥系统：

#### 数据库解密密钥 (64 hex = 32 bytes)
用于解密 `message_N.db`、`contact.db` 等 WCDB 数据库。提取方式：

1. **已保存** → `wechat_keys.json`
2. **内存扫描** → `pymem` 在 `Weixin.exe` 进程中搜索 64 hex 字符串
3. **可选 Hook 注入** → 只有维护者通过 `-IncludeWxKey` 显式构建、且已自行确认
   组件使用与再分发授权时才可用；公开源码和默认安装包不包含该组件

无论候选来自哪条路径，后端都会先用当前账号数据库验证，再保存到账号隔离的本地
设置中；扫描或验证期间切换账号会丢弃结果。缺少可选 Hook 时不会为了提取密钥关闭
或重启微信，用户也始终可以手动输入 64 位密钥。

#### 图片 V2 AES 密钥 (16 bytes)
用于解密聊天图片 `.dat` 文件。提取方式：

1. **元数据推导** → 从 `%APPDATA%/Tencent/**/kvcomm/` 获取 code，`MD5(code + wxid)[:16]` 暴力尝试
2. **内存扫描** → 搜索 16/32 字符 ASCII 密钥候选

### 图片管线

```
聊天图片请求
  ↓
image_service.py: WeChatImageService.get_image()
  ├── 查 message_resource.db → file_md5
  ├── 搜 msg/attach/*/Img/ 目录 → file_md5*.dat
  ├── 判断加密版本 (V2 / V1 / 单字节 XOR)
  ├── AES-ECB 解密 + XOR 尾部处理
  ├── 去除微信 JPEG 尾部 (24 字节 MD5)
  └── 可选: WXGF/HEVC → JPEG 转换 (PyAV)
  ↓
返回 JPEG/PNG 给前端

AI 图片识别 (批量)
  ↓
image_recognition.py: ImageRecognitionManager
  ├── 遍历目标消息 → 获取图片 bytes
  ├── 调用 vision.py: VisionClient
  │     ├── 转为 base64 data URI
  │     ├── POST 到 OpenAI/Claude/Gemini 等兼容 API
  │     └── 返回文字描述
  ├── 存入 image_store.py: ImageDescriptionStore (SQLite)
  └── 前端轮询 /api/image-recognition/tasks/{id}

高清图片自动化
  ↓
image_hd_automation.py: ImageHDAutomationManager
  ├── 检查 message_resource.db 中高清资源可用性
  ├── wechat_viewer_automation.py: WeChatViewerController
  │     ├── FindWindow 找到微信图片查看器
  │     ├── SendInput → F9 (下载), 左右箭头 (切换图片)
  │     └── 轮询文件系统检查新图片
  └── 支持暂停/恢复/取消/导航模式切换
```

### 语音/ASR 管线

```
语音消息请求
  ↓
voice_service.py: WeChatVoiceService.get_voice()
  ├── 读 media_N.db 分片 → 语音负载
  ├── 验证 #!SILK_V3 魔数 + 去壳 (\x02 头)
  ├── pysilk.decode() → PCM (16-bit mono)
  └── wave 模块 → WAV 封装
  ↓
返回 WAV 音频给前端

批量语音转文字
  ↓
voice_transcription.py: VoiceTranscriptionManager
  ├── 遍历目标消息 → 解码为 WAV
  ├── 调用 transcription.py: TranscriptionClient
  │     ├── multipart/form-data 上传 (OpenAI 兼容)
  │     ├── DashScope ASR (自定义 multipart + Bearer)
  │     └── 自定义 multipart (JSON 配置)
  ├── 存入 voice_store.py: VoiceTranscriptionStore (SQLite)
  │     └── 缓存键 = (account, talker, msg_id, audio_sha256)
  └── 前端轮询 /api/voice-transcription/tasks/{id}
```

### 表情包管线

```
表情请求
  ↓
sticker_service.py: StickerService.resolve()
  ├── 从消息 XML 提取候选 URL (cdn_url, encrypt_url, extern_url, thumb_url)
  ├── 可选: 查 emoticon.db 补充 CDN URL
  ├── SafeStickerDownloader 下载 (DNS 绑定 + 域名白名单)
  ├── 如果加密: AES-CBC 解密 (密钥来自 sticker XML)
  ├── 格式处理:
  │     ├── WXGF/WXAM: 解析 Annex-B HEVC → 双流检测 → PyAV 解码 → Alpha 合并 → WebP
  │     ├── HEVC 静态: PyAV → WebP/PNG
  │     ├── AVIF/HEIC: pillow-heif → WebP/PNG
  │     └── JPEG/PNG/GIF/WebP: 直接透传
  └── 持久化磁盘缓存 + Single-flight 去重
```

### 朋友圈管线

```
朋友圈访问
  ↓
moments.py: MomentsService
  ├── 读 sns.db → SnsTimeLine 表
  ├── zstd/hex/base64 解码 content XML
  ├── 解析: 文字, 媒体, 点赞, 评论, 位置, 链接
  └── 分页返回 (置顶帖特殊处理)

朋友圈媒体
  ↓
moments_media.py: MomentsMediaResolver
  ├── 搜索本地 SNS 缓存 .dat 文件
  ├── 解密 + MD5 校验 (对照帖子元数据)
  └── 存入 SHA-256 内容寻址 blob 存储

CDN 图片下载
  ↓
SafeMomentsMediaDownloader (HTTPS DNS 绑定)
  ├── 如果 x-Enc: 1 → ISAAC-64 流密码解密
  └── 严格格式校验 (文件头/尾/像素数/帧数)
```

### AI 分析引擎

**`ai_analysis.py`** 实现了 Map-Reduce 模式：

```
用户选择范围 (选定消息 / 日期范围 / 全部)
  ↓
engine.analyze_records()
  ├── records_to_analysis_text() → 序列化为结构化文本
  ├── 如果文本长度 ≤ chunk_size:
  │     └── 单次 API 调用, 输出 Markdown 报告
  ├── 如果 > chunk_size (Map-Reduce):
  │     ├── Map: 每个 chunk 独立分析 → 生成子摘要
  │     ├── Reduce: 合并所有子摘要 → 综合报告
  │     └── (支持多轮 reduce, 直到全部聚合成 final)
  └── AnalysisClient.complete() → 保存 Markdown 报告

支持协议: OpenAI / Anthropic / Gemini / 自定义 JSON
分析预设: 综合 / 情感关系 / 话题时间线 / 行动风险
强度: quick / balanced / deep
```

### 导出功能

**`exporter.py`**: `ChatExporter` 支持四种格式：

| 格式 | 特点 |
|------|------|
| HTML | 微信风格气泡, 可选嵌入 base64 图片, 独立离线可用 |
| JSON | 结构化数据, 适合程序处理 |
| CSV | 表格, 公式注入防护 (`'=+@-` 前缀) |
| TXT | 纯文本, 日期/发送者头 |

选项: 图片描述替换, 语音文字替换, 嵌入图片, 图片质量选择

### 配置与设置

配置文件位置: `%LOCALAPPDATA%/WechatAnalysisAssistant/`

| 文件 | 内容 |
|------|------|
| `settings.json` | 导出/UI/视觉/转录/分析/图片密钥配置 |
| `wechat_keys.json` | 多账号数据库密钥 (JSON) |
| `wechat_keys.txt` | 人类可读的密钥副本 |
| `image_descriptions.sqlite3` | AI 图片描述缓存 |
| `voice_transcriptions.sqlite3` | ASR 转录缓存 |
| `moments_media/` | 朋友圈媒体 blob 存储 |
| `stickers/` | 表情包缓存 |
| `avatars/` | 头像缓存 |

---

## 6. 前端详解

### 应用状态机

**`App.jsx`** 定义了应用的顶层状态：

```
STATE.LOADING  →  旋转加载动画
STATE.NEED_KEY →  KeyInput (连接页, 固定窗口)
STATE.READY    →  主界面 (聊天/通讯录/个人资料)
STATE.ERROR    →  错误页 + 重试按钮
```

**工作区切换** (READY 状态下)：

```
WORKSPACE.chats    → ChatList + ChatView
WORKSPACE.contacts → ContactsView
WORKSPACE.profile  → AccountProfile
```

**状态转换触发条件**:

- `loading → need_key`: 未检测到已保存密钥
- `loading → error`: 未检测到微信数据或数据库读取失败
- `need_key → ready`: 密钥验证成功
- `loading → ready`: 已保存密钥, 自动加载
- `any → loading`: 用户点击刷新或切换账号

### 组件树与职责

```
App.jsx (根)
├── StatusBar             # 顶栏: 标题/副标题/计数/连接状态/账号切换
├── AppRail               # 导航栏: 桌面左侧 72px, 移动端底部 Tab
│
├── [STATE.NEED_KEY]
│   └── KeyInput          # 连接页: 检测状态卡片 + 密钥输入 + 自动提取
│
├── [STATE.READY - chats]
│   ├── ChatList          # 会话列表: 搜索/筛选, 头像, 最后消息, 未读数
│   └── ChatView          # 聊天详情 (最复杂):
│       ├── 工具栏: 类型筛选/批量高清/图片识别/语音转写/AI 分析/导出
│       ├── 消息气泡: 自己(绿)/别人(白)/系统(灰)/日期分割线
│       ├── 消息类型: 文字/图片/表情/语音/红包/转账/引用
│       ├── 搜索: 关键词 + 日期 + 发送者
│       ├── 选择模式: 全选/反选/浮动操作栏
│       ├── 图片浏览器: 缩略图/高清切换, 右键导出
│       ├── 语音播放器: 播放/暂停, 波形动画, 逐条转录
│       ├── 进度条: HD 自动化/识别/转录 (完成/失败/总数)
│       └── 范围选择对话框: RecognitionRangeDialog /
│           HdAutomationRangeDialog / TranscriptionRangeDialog /
│           ChatAnalysisRangeDialog
│
├── [STATE.READY - contacts]
│   └── ContactsView      # 通讯录: 联系人/群聊/公众号分类, 详情卡片
│
├── [STATE.READY - profile]
│   └── AccountProfile    # 账号资料: 头像/微信名/数据库信息/版本
│
├── ExportDialog           # 导出对话框: 格式/时间范围/图片嵌入/语音替换
├── MomentsExportDialog    # 朋友圈导出: 两步向导 (浏览→导出设置)
├── SettingsDialog         # 设置: 7 个标签页 (图片/视觉/转录/分析/预设/导出/关于)
└── AiAnalysisDialog       # AI 分析: 预设/强度/详情/Markdown 报告
```

### API 通信层

**`api.js`**: Axios 实例, 所有函数返回 Promise。

- Base URL: `/api`
- 默认超时: 30s (导出 600s, AI 分析 900s)
- 响应拦截器: 自动解包 `response.data`
- 错误规范化: 统一为带 `message`、可选 `status` / `code` 的 `Error`

**请求 → 响应流**:

```
浏览器模式:  React → axios → /api/* → Vite proxy → 127.0.0.1:8520
桌面模式:    React → axios → /api/* → app:// 协议 → 127.0.0.1:random
```

### 桌面桥接

**`desktop.js`**：封装 Electron 能力。浏览器模式下检测函数返回 `false`、查询函数
返回 `null`、订阅函数返回空取消器；只有用户主动调用桌面专属更新操作时才给出明确
错误。

| 函数 | 用途 |
|------|------|
| `isDesktopApp()` | 检测是否在 Electron 中运行 |
| `getDesktopRuntimeInfo()` | 获取版本/平台/频道 |
| `setDesktopWindowMode(mode)` | 切换窗口大小 (`connection` / `main`) |
| `selectDesktopFolder(path)` | 原生文件夹选择器 |
| `getDesktopUpdateStatus()` | 自动更新状态 |
| `checkForDesktopUpdates()` | 检查更新 |
| `downloadDesktopUpdate()` | 下载更新 |
| `installDesktopUpdate()` | 安装更新并重启 |
| `onDesktopUpdateStatus(callback)` | 订阅更新状态并返回取消订阅函数 |

### 样式系统

- **Tailwind CSS 3** — 绝大部分样式
- **自定义颜色** (在 `tailwind.config.js`):
  - `wechat-green` (#07c160), `wechat-green-dark` (#06ad56)
  - `wechat-bg` (#f5f7f6)
  - `wechat-bubble-self` (#95ec69, 自己发的消息)
  - `wechat-bubble-other` (#ffffff, 别人发的消息)
- **自定义 CSS** (`index.css`):
  - `.app-viewport` — `100dvh` 全屏视口
  - `.app-topbar` — 顶栏阴影
  - `.app-icon-button` — 统一图标按钮
  - 自定义滚动条 (Webkit)
  - `message-enter` — 消息淡入动画
- **所有图标均为内联 SVG** (无图标库依赖)

---

## 7. Electron 桌面壳详解

### 主进程启动流程

```
app.whenReady()
  ↓
startDesktop()
  ├── 1. 解析前端目录 (resources/frontend 或 ../frontend/dist)
  ├── 2. 创建 BackendManager (管理 Python 子进程)
  ├── 3. backendManager.start()
  │     ├── 分配随机 loopback 端口
  │     ├── 生成 256-bit 会话令牌
  │     ├── spawn WechatAnalysisAssistantBackend.exe --desktop --port N
  │     ├── 设置环境变量: WECHAT_ASSISTANT_DESKTOP_TOKEN / PORT / MODE / PARENT_PID
  │     ├── 轮询 GET /api/desktop/health (每 250ms, 最多 90s)
  │     └── 验证健康响应 (status=ok, desktop=true, api_version=1)
  ├── 4. 注册 app:// 自定义协议
  ├── 5. 创建 BrowserWindow (980x720, 不可缩放, 无最大化按钮)
  ├── 6. 创建 UpdateManager (如果发布 URL 有效)
  ├── 7. 注册受限 IPC handler 与更新状态事件
  └── 8. 加载 app://wechat-analysis-assistant/
```

### IPC 桥接表

前端通过 `window.wechatDesktop` 调用, 主进程通过 `ipcMain.handle` 响应:

| IPC 信道 | 前端调用 | 主进程处理 |
|----------|----------|-----------|
| `desktop:get-runtime-info` | `getRuntimeInfo()` | 返回版本/频道/平台 |
| `desktop:set-window-mode` | `setWindowMode(mode)` | 切换窗口模式 |
| `desktop:select-folder` | `selectFolder()` | 打开原生文件夹选择器 |
| `desktop:get-update-status` | `getUpdateStatus()` | 返回更新状态 |
| `desktop:check-for-updates` | `checkForUpdates()` | 检查更新 |
| `desktop:download-update` | `downloadUpdate()` | 下载更新 |
| `desktop:install-update` | `installUpdate()` | 安装并重启 |
| `desktop:update-status` | `onUpdateStatus(cb)` | 订阅状态变更事件 |

**安全约束**:
- 所有 IPC 请求必须来自 `app://wechat-analysis-assistant/` 源
- 前端只能调用表中列出的信道，无法访问原始 `ipcRenderer`
- 会话令牌永远不暴露给渲染进程 (只由协议处理器注入)

### 后端子进程管理

**`backend-manager.mjs`**: `BackendManager` 类管理 Python 后端的完整生命周期。

**启动**: spawn → 健康检查轮询 → 握手验证

**关闭** (三阶段优雅退出):
1. `POST /api/desktop/prepare-exit` (12s 超时 — 让后端取消任务, 关闭数据库)
2. `SIGTERM` (2s 超时)
3. Windows: `taskkill /PID /T /F` (强制杀进程树)

**日志**: 写入 `<userData>/logs/backend.log`, 自动轮转 (2MB 上限), 令牌自动脱敏。

### 自定义协议与安全模型

**`app-protocol.mjs`** 实现了三层安全:

1. **路由隔离**: `/api/*` → 代理到后端; 其他 → 静态文件
2. **令牌注入**: 代理时自动添加 `X-Desktop-Session-Token` 请求头
3. **CSP 强制**: 每个响应都带 Content-Security-Policy 头:
   - `default-src 'self'` (只允许同源加载)
   - `script-src 'self'` (禁止 eval 和内联脚本)
   - `img-src 'self' data: blob: https: http:` (图片可跨域)
   - `frame-ancestors 'none'` (禁止被 iframe 嵌入)

### 自动更新

**`update-manager.mjs`** 封装 `electron-updater`:

- 测试版 (version 含 `-`): 使用 `beta` 频道
- 正式版: 使用 `latest` 频道
- `app-update.yml` 固定使用本仓库 GitHub provider；预发布读取 `beta.yml`，稳定版读取 `latest.yml`
- 用户手动触发: 检查 → 下载 → 安装 (从不自动下载)

---

## 8. 构建与打包

### 完整构建流水线

**`scripts/build-desktop.ps1`** 按顺序执行以下关键阶段：

```
阶段 1: Build frontend
  npm run build --prefix frontend
  产物: frontend/dist/

阶段 2: 验证运行时
  scripts/verify-packaging-runtime.py
  检查: 必需模块可导入, PyAV HEVC 解码器, pillow-heif 注册

阶段 3: PyInstaller 冻结
  python -m PyInstaller packaging/wechat_backend.spec
  产物: dist/backend/WechatAnalysisAssistantBackend/

阶段 4: DLL 归一化
  删除旧 CRT DLLs + 注入系统 vcruntime/msvcp

阶段 5: 可选原生模块探测
  仅 -IncludeWxKey 构建执行 --probe-native-module wx_key
  默认公开构建反向确认产物中不存在 wx_key

阶段 6: 安全扫描
  scripts/check-release.ps1
  检查: 无密钥文件, 无数据库, 无源码, 无开发工具

阶段 7: Electron 测试、配置检查与打包
  npm run dist:package --prefix desktop
  产物: desktop/dist-electron/*.exe + win-unpacked/

阶段 8: 打包后检查
  再次扫描 win-unpacked，读取 Electron fuses，并用隔离环境和专用最小 app:// 页面运行烟雾测试
```

### PyInstaller 冻结

**`packaging/wechat_backend.spec`**: onedir 模式 (不打包为单个 exe, 保留 `_internal/` 目录)。

**关键配置**:
- 入口: `backend/main.py`
- 控制台模式 (`console=True`) — 以便 Electron 捕获 stdout
- 必需运行时包按 spec 收集；`wx_key` 仅在
  `WECHAT_ASSISTANT_INCLUDE_WX_KEY_BUILD=1` 时加入，否则明确排除
- 显式收集 Windows 与动态加载模块 (包括 `_cffi_backend`, `aiofiles`, `multipart`, `httptools`, `websockets`, `win32*`)
- 3 个自定义 Hook (av, pillow_heif, pysilk — 处理 DLL 依赖)
- 1 个运行时 Hook (注册 `av.libs/` DLL 目录)
- 排除: tests, faster_whisper, torch, tensorflow, pytest

### DLL 运行时处理

**问题背景**: 构建机上的 JDK 自带了旧版 C++ 运行时 DLL, PyInstaller 可能误收集它们, 导致 `0xC0000005` 访问冲突。

**解决方案** (`Normalize-FrozenNativeRuntime`):
1. 删除 `_internal/` 中的所有 `api-ms-win-crt-*.dll`
2. 删除 `_internal/ucrtbase.dll`
3. 从 `C:\Windows\System32` 复制最新 `msvcp140.dll`, `vcruntime140.dll`, `vcruntime140_1.dll`

### 安全检查

**`scripts/check-release.ps1`** 递归扫描:
- 禁止: `.venv`, `node_modules`, `__pycache__`, 数据库文件, 密钥文件, 日志, 证书
- ASAR 内部检查: 确保 `electron`, `electron-builder` 等构建工具不在 `app.asar` 中
- ZIP 内部检查: 若另行生成 ZIP 发布包，则递归检查其条目；NSIS 的来源目录由 `win-unpacked` 扫描覆盖

### 安装包生成

```powershell
# 方式 1: 从 win-unpacked 生成安装包 (不需要重下载 Electron)
cd desktop
npm run dist:installer:prepackaged
# 产物: dist-electron/WechatAnalysisAssistant-0.1.0-beta.3-x64.exe

# 方式 2: 完整构建安装包 (需要网络)
cd desktop
npm run dist
```

---

## 9. 关键数据流

### 用户打开软件的完整请求流

```
1. 用户双击 微信解析助手.exe
2. Electron 主进程启动, spawn Python 后端
3. 前端加载: app:// 协议 → index.html → React 渲染
4. App.jsx mount → initApp()
5. GET /api/auto-detect → 发现微信数据目录 + 尝试读取已保存密钥
6. 如果有密钥 → GET /api/chats → 显示聊天列表
7. 用户点击某个聊天 → GET /api/chat/{talker} → ChatView 渲染消息
8. 用户点击图片 → GET /api/chat/{talker}/image/{id} → 解密并返回图片
9. 用户播放语音 → GET /api/chat/{talker}/voice/{id}/audio → SILK 解码 → WAV 流
10. 用户点击 AI 分析 → POST /api/analysis/chat/{talker} → SSE 流式响应 → Markdown 报告
```

### 密钥提取的完整流程

```
用户点击"自动提取密钥"
  ↓
POST /api/extract-key
  ↓
api.py: extract_key()
  ├── 1. 捕获当前账号和用于验证的数据库范围
  ├── 2. 默认构建: find_key_auto(persist=False) 读取已运行微信的内存
  ├── 3. 校验 64 位格式并用当前账号数据库 test_key()
  ├── 4. 再次确认扫描期间没有切换账号
  ├── 5. 验证成功后才保存到 wechat_keys.json
  └── 可选授权构建: 隔离探测 wx_key 后执行 Hook 流程；任何候选仍需数据库验证
```

---

## 10. 常见修改场景指南

### 场景 A: 修改前端界面

**涉及目录**: `frontend/src/components/`

1. 编辑 `.jsx` 文件 (修改样式/布局/逻辑)
2. 运行 `npm run build --prefix frontend`
3. 复制 `frontend/dist/` → `desktop/dist-electron/win-unpacked/resources/frontend/`
4. 重新打开 exe 测试

**关键文件定位**:
- 工具栏样式 → `ChatView.jsx` 搜索 `flex-wrap`/`overflow-x`
- 连接页布局 → `KeyInput.jsx`
- 顶栏标题 → `App.jsx` 搜索 `NEED_KEY` / `StatusBar`
- 设置面板 → `SettingsDialog.jsx`

### 场景 B: 修改后端功能

**涉及目录**: `backend/`

1. 编辑 `.py` 文件
2. 运行隔离测试 `.venv\Scripts\python.exe scripts\test-backend.py`
3. 重新冻结: `python -m PyInstaller packaging/wechat_backend.spec --noconfirm --clean --distpath dist/backend --workpath build/pyinstaller`
4. 运行时与打包验证通过 `scripts/build-desktop.ps1` 统一执行；默认构建应确认
   `wx_key` 不在产物中，只有 `-IncludeWxKey` 构建才探测该模块
5. 复制到 `desktop/dist-electron/win-unpacked/resources/backend/`
6. 重新打开 exe 测试

**关键文件定位**:
- 新增 API 路由 → `api.py` 搜索 `@app.get`/`@app.post`
- 消息解析 → `parser_v4.py`
- 图片解密 → `image_service.py`
- 数据导出格式 → `exporter.py`
- 用户设置 → `settings.py`

### 场景 C: 修改 Electron 窗口行为

**涉及目录**: `desktop/src/`

1. 编辑 `main.mjs` (窗口大小/缩放/最大化按钮等)
2. 提取现有 asar: `npx asar extract desktop/dist-electron/win-unpacked/resources/app.asar temp/`
3. 复制修改后的文件覆盖: `cp desktop/src/main.mjs temp/src/`
4. 重新打包: `npx asar pack temp/ desktop/dist-electron/win-unpacked/resources/app.asar`
5. 清理: `rm -rf temp/`
6. 重新打开 exe 测试

**关键文件定位**:
- 窗口创建 → `main.mjs` 搜索 `createMainWindow`
- 窗口模式切换 → `main.mjs` 搜索 `applyWindowMode`
- IPC 信道 → `main.mjs` 搜索 `registerIpc`
- 后端管理 → `backend-manager.mjs`
- 令牌安全 → `app-protocol.mjs` + `protocol-utils.mjs`

### 场景 D: 添加新的 AI 模型支持

**涉及文件**:
1. `frontend/src/modelInterfaces.js` — 添加预设
2. `backend/model_interfaces.py` — 添加解析逻辑
3. 对应的客户端文件 (`vision.py` / `transcription.py` / `ai_analysis.py`)

### 场景 E: 修改版本号

**涉及文件** (必须同步):
1. `backend/version.py` → `APP_VERSION`
2. `desktop/package.json` → `version`
3. `frontend/package.json` → `version`

修改后运行 `npm run check --prefix desktop`；配置检查会同时核对两个
`package-lock.json` 的根版本，避免桌面握手、安装器和网页资源版本漂移。

Beta 版格式: `0.1.0-beta.N` (必须含 `-`)
正式版格式: `0.1.0` (不含 `-`)

### 场景 F: 调试后端

在非桌面模式下运行, 方便用浏览器开发者工具调试:

```powershell
# 终端 1: 启动后端
.venv\Scripts\python.exe -m backend.main --port 8520

# 终端 2: 启动前端 dev server
cd frontend
npm run dev
# 浏览器打开 http://localhost:3000
# Vite 自动代理 /api → 127.0.0.1:8520
```

### 场景 G: 运行测试

```powershell
# 后端隔离测试与静态检查
.venv\Scripts\python.exe scripts\test-backend.py
.venv\Scripts\python.exe -m ruff check backend scripts --select F,E9

# 前端 lint、测试和生产构建
npm run check --prefix frontend

# Electron 测试与配置一致性
npm test --prefix desktop
npm run check --prefix desktop

# Git 已跟踪源码中的敏感/生成文件检查
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check-source-safety.ps1
```

这些自动检查使用匿名夹具或临时目录，不读取真实微信数据库。桌面烟雾测试启用专用
隔离标志、拒绝数据探测路由且不加载生产 React 页面，只覆盖启动、preload 桥、本机
API 代理与进程退出；它不替代实际界面、真实微信 4.x、第三方模型、代码签名、
SmartScreen 和杀毒软件环境下的发布验收。

---

## 11. 文件索引

### 按修改频率排序（从高到低）

#### 🔥 极高频 (几乎每次改动都涉及)

| 文件 | 用途 | 修改场景 |
|------|------|----------|
| `frontend/src/components/ChatView.jsx` | 聊天详情 (所有交互) | 界面调整, 新功能 |
| `backend/api.py` | 全部 API 路由 | 新增端点, 修改逻辑 |
| `frontend/src/App.jsx` | 应用状态机 | 新增状态, 窗口模式 |
| `frontend/src/components/KeyInput.jsx` | 连接页 | 密钥页布局调整 |

#### 🔵 高频

| 文件 | 用途 |
|------|------|
| `frontend/src/components/SettingsDialog.jsx` | 设置面板 |
| `frontend/src/components/MomentsExportDialog.jsx` | 朋友圈导出 |
| `frontend/src/components/AiAnalysisDialog.jsx` | AI 分析对话框 |
| `frontend/src/api.js` | 前端 API 客户端 |
| `backend/parser_v4.py` | 微信消息解析 |
| `backend/sticker_service.py` | 表情包处理 |
| `backend/image_service.py` | 图片解密 |
| `backend/exporter.py` | 聊天导出 |

#### 🟡 中频

| 文件 | 用途 |
|------|------|
| `backend/config.py` | 微信数据目录发现 |
| `backend/decrypt.py` | WCDB 数据库解密 |
| `backend/key_extractor.py` | 密钥提取 |
| `backend/voice_service.py` | 语音解码 |
| `backend/moments.py` | 朋友圈解析 |
| `backend/moments_media.py` | 朋友圈媒体 |
| `backend/ai_analysis.py` | AI 分析引擎 |
| `backend/vision.py` | 视觉模型客户端 |
| `backend/transcription.py` | ASR 客户端 |
| `backend/avatar_service.py` | 头像服务 |
| `backend/settings.py` | 设置持久化 |
| `frontend/src/components/ExportDialog.jsx` | 导出对话框 |
| `frontend/src/components/ContactsView.jsx` | 通讯录 |
| `frontend/src/components/ChatList.jsx` | 会话列表 |
| `frontend/src/components/AccountProfile.jsx` | 账号资料 |
| `frontend/src/components/StatusBar.jsx` | 顶栏 |
| `frontend/src/components/AppRail.jsx` | 导航栏 |
| `frontend/src/modelInterfaces.js` | AI 模型预设 |

#### 🟢 低频

| 文件 | 用途 |
|------|------|
| `backend/main.py` | CLI 入口 |
| `backend/version.py` | 版本常量 |
| `backend/image_recognition.py` | 图片识别管理器 |
| `backend/voice_transcription.py` | 语音转录管理器 |
| `backend/image_hd_automation.py` | 高清图片自动化 |
| `backend/image_key_extractor.py` | 图片密钥提取 |
| `backend/wechat_viewer_automation.py` | SendInput 键盘控制 |
| `backend/wcdb_key_scanner.py` | WCDB 密钥扫描 |
| `backend/sns_media_crypto.py` | ISAAC-64 流密码 |
| `backend/model_interfaces.py` | AI 接口注册 |
| `backend/app_paths.py` | 路径工具 |
| `backend/subprocess_env.py` | 环境清理 |
| `backend/time_utils.py` | 时间格式化 |

#### ⚪ Electron 壳 (改得最少)

| 文件 | 用途 |
|------|------|
| `desktop/src/main.mjs` | 主进程入口 (窗口/IPC/生命周期) |
| `desktop/src/preload.cjs` | 沙箱预加载 (contextBridge) |
| `desktop/src/backend-manager.mjs` | 后端子进程管理 |
| `desktop/src/app-protocol.mjs` | 自定义协议 + 令牌注入 |
| `desktop/src/update-manager.mjs` | 自动更新 |
| `desktop/src/runtime-config.mjs` | 配置解析 |
| `desktop/src/protocol-utils.mjs` | 请求代理工具 |

#### ⚪ 打包 (改得最少)

| 文件 | 用途 |
|------|------|
| `packaging/wechat_backend.spec` | PyInstaller spec |
| `scripts/build-desktop.ps1` | 完整构建流水线 |
| `scripts/check-release.ps1` | 安全扫描 |
| `scripts/verify-packaging-runtime.py` | 运行时验证 |
| `scripts/smoke-desktop.ps1` | 打包烟雾测试 |
| `packaging/hooks/hook-av.py` | PyAV DLL 收集 |
| `packaging/hooks/hook-pillow_heif.py` | libheif DLL 收集 |
| `packaging/hooks/hook-pysilk.py` | SILK 双后端收集 |
| `packaging/runtime_hooks/windows_dll_paths.py` | DLL 目录注册 |

---

## 附录: 项目约束与约定

### 安全约束
- 密钥不得嵌入前端代码、日志或发布产物；普通设置接口只返回掩码状态
- 后端只接受回环客户端与合法本机 Host；浏览器请求还要通过同源/Fetch Metadata 检查
- 桌面模式下所有 `/api` 请求必须有每次启动随机生成的会话令牌
- 远程模型接口要求 HTTPS，本机模型可使用回环 HTTP；朋友圈/表情媒体下载使用
  URL 与地址范围校验，不能把这些措施描述为对所有外部请求的绝对安全保证

### 版本号规则
- `backend/version.py`、桌面和前端的 `package.json` / lock 根版本必须一致
- Beta 版: `X.Y.Z-beta.N`
- 正式版: `X.Y.Z`
- `DESKTOP_API_VERSION` 整数, 前后端协议版本 (当前 = 1)

### 代码风格
- 前端: React 函数组件, 无状态管理库, Props 向下传递, Callback 向上传递
- 后端: FastAPI, dataclass 配置, 服务层独立于路由, 大量 try/except 包裹动态导入
- Electron: ESM (`"type": "module"`), 沙箱渲染进程, contextBridge 暴露 API

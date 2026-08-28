# 前端开发指南

本目录构建 `v0.1.0-beta.3` 的 React 界面，同时供源码启动的本机网页版和 Electron
桌面版使用。项目没有托管网页服务；界面只访问同源 `/api`。微信数据库读取、解密
和导出由本机后端处理，用户明确触发的图片识别、语音转写、AI 分析或媒体下载则由
后端访问相应第三方服务。

## 本地开发

需要 Node.js 20.19 或更高版本，推荐使用 CI/发布流水线采用的 Node.js 22。默认开发
服务使用 `http://127.0.0.1:3000`，并将 `/api` 代理到
`http://127.0.0.1:8520`。

```bash
npm ci
npm run dev
```

提交代码前请运行：

```bash
npm run check
```

`check` 会依次执行 ESLint、Node 单元测试和 Vite 生产构建。也可单独使用 `npm run lint`、`npm test`、`npm run test:watch` 或 `npm run build`。

当前单元测试主要覆盖纯函数、URL 构造和桌面桥接降级，生产构建只验证资源可以生成。
它们不启动真实微信后端，也不替代浏览器交互、Electron 打包、真实微信数据库或第三方
模型端到端测试；相关发布边界见根目录
[DESKTOP_RELEASE.md](../DESKTOP_RELEASE.md)。

## 代码结构

- `src/App.jsx`：账号连接、工作区切换与全局对话框编排。
- `src/api.js`：唯一的 HTTP 客户端，负责路径编码、超时和错误标准化。
- `src/desktop.js`：可选的 Electron preload 桥接；普通浏览器中安全降级。
- `src/components/ChatView.jsx`：聊天阅读、搜索、媒体处理与分析任务。
- `src/components/SettingsDialog.jsx`：导出、图片、转写和 AI 模型设置。
- `src/hooks/useDialogFocus.js`：普通模态对话框共用的焦点限定与恢复逻辑。

聊天详情、设置、导出和朋友圈工具使用动态导入，避免增加连接页的首次下载体积。

## 维护约定

- 组件不要直接创建 Axios 实例；新端点统一添加到 `api.js`。
- 可能被会话切换或输入变更取代的请求，应使用 `AbortController` 或请求代次标识，防止旧响应覆盖新状态。
- 新增对话框需设置 `role="dialog"`、可读取标题、Escape 关闭和焦点恢复；普通对话框优先复用 `useDialogFocus`。
- 不要在前端保存数据库密钥或模型 API Key；密钥明文只能由用户显式点击“显示”后短暂读取。
- 纯数据转换、路径构造和输入校验应优先抽成可单测的函数。

界面视觉和响应式规范参见 [UI_GUIDELINES.md](./UI_GUIDELINES.md)。

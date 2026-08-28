# Electron desktop shell

This directory turns the existing React and Python application into a Windows
desktop application. The renderer is loaded from the packaged `frontend/dist`
directory through the secure `app://wechat-analysis-assistant` protocol. It does
not load the FastAPI HTML page. The current application release is
`v0.1.0-beta.3`.

The main process starts a Python sidecar on a random loopback port and generates
a new 256-bit session token for every launch. Renderer `/api/*` requests are
streamed through the custom protocol; the main process injects the token, so it
is never exposed to renderer JavaScript. GET, POST, DELETE, JSON/multipart
uploads, Range requests and streaming media use the same proxy path.

## Local development

Requirements:

- Node.js 20.19 or newer and npm (CI and release builds use Node.js 22)
- Windows x64, PowerShell, and a Python 3.10+ `.venv` with the backend
  requirements installed (CI and release builds use CPython 3.12)
- the frontend dependencies installed

Run:

```powershell
cd desktop
npm ci
npm test
npm run check
npm run dev
```

`npm run dev` builds the React frontend and then starts Electron. Set
`WECHAT_ASSISTANT_PYTHON` to override the development Python executable.

## Packaging layout

Direct `npm run dist:package` expects the complete PyInstaller onedir result at:

```text
desktop/resources/backend/WechatAnalysisAssistantBackend/
  WechatAnalysisAssistantBackend.exe
```

The installer embeds that directory as `resources/backend` and copies the
current `frontend/dist` build to `resources/frontend`. Generated Vite assets are
not checked into `desktop/resources`; a fresh clone always builds them from
source. Prefer the repository build entrypoint, which freezes the backend,
cleans stale Electron output, runs desktop tests, scans both package stages and
runs a hidden packaged-application smoke test:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File ..\scripts\build-desktop.ps1 -Channel beta
```

Build output is written to `desktop/dist-electron`. The checked-in Windows icon
can be regenerated with:

```powershell
..\.venv\Scripts\python.exe ..\scripts\generate-desktop-icon.py
```

`package.json` uses Electron Builder's GitHub provider for
`Magicapple-Coder/WechatAnalysisAssistant`. Packaged applications validate the
generated `app-update.yml`; prereleases read the `beta.yml` channel and stable
releases read `latest.yml` from that repository's GitHub Releases. Update checks
are user-triggered, downloads do not install automatically, and the renderer
must call `installUpdate()` after explicit user confirmation.

`wx_key` is an optional native Hook enhancement for which this project has no
verified public redistribution permission. Public/default builds do not
download or bundle it. The supported PyMem/manual-key paths remain available; a
maintainer who has independently confirmed authorization for a compatible wheel
may install it into `.venv` and pass `-IncludeWxKey`; the freezer then collects
and probes it. The default build explicitly excludes the module even if it
happens to be installed locally, reducing the risk of accidental redistribution.

For a release build, use `-Channel release`; the script requires a stable
version and a valid enabled GitHub update configuration. Production installers
should additionally be Authenticode-signed and the updater publisher identity
should be pinned. Those credentials intentionally do not belong in this
repository.

A beta build without `CSC_LINK` and `CSC_KEY_PASSWORD` is unsigned. Windows may
show a SmartScreen warning, and the download has no Authenticode publisher
identity; release notes must say so prominently and provide the generated
SHA-256 manifest. The stable release channel rejects missing signing credentials.

## Packaged smoke test

To validate an existing `win-unpacked` tree without showing a window or using
real application data:

```powershell
npm run smoke:desktop
```

The test starts the packaged Python sidecar and loads a dedicated minimal
`app://` page instead of the React application. It verifies the sandboxed
preload bridge and authenticated desktop health/self-test requests through the
custom protocol. A smoke-only backend flag disables local WeChat account
discovery and rejects non-allowlisted routes such as `/api/status` and
`/api/auto-detect`. The harness writes only to a unique system temporary
directory and removes that directory on completion.

This smoke test deliberately does not load the production React UI, open real
WeChat databases, or exercise third-party model services, Authenticode trust,
SmartScreen reputation or antivirus behaviour. Treat it as a bounded
protocol/preload/sidecar check, not proof that a specific installer has passed
clean-machine acceptance or is free of security issues.

## Renderer bridge

The sandboxed preload exposes only `window.wechatDesktop`:

- `isDesktop`
- `getRuntimeInfo()`
- `selectFolder({ defaultPath? })`
- `getUpdateStatus()`
- `checkForUpdates()`
- `downloadUpdate()`
- `installUpdate()`
- `onUpdateStatus(callback)`

Raw Electron IPC is not exposed.

The default session denies camera, microphone, location, notification and
device permissions. New windows and in-app navigation are denied; only validated
HTTP(S) and `mailto:` links can be handed to the operating system. Packaged
builds also disable Electron's Run-as-Node, `NODE_OPTIONS` and inspector fuses,
and require the integrity-checked `app.asar`.

## Maintainer checks

Use the official npm registry for security advisories (some mirrors do not
implement the audit endpoint):

```powershell
npm audit --registry=https://registry.npmjs.org
```

`package-lock.json` and the exact PyInstaller toolchain pins are committed. A
Windows CI job can run the same release entrypoint after `npm ci` for both
frontend and desktop and after creating `.venv`; no build output should be
reused between jobs.

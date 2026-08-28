import path from 'node:path';
import { existsSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  protocol,
  session,
  shell,
} from 'electron';
import { BackendManager } from './backend-manager.mjs';
import {
  APP_ORIGIN,
  APP_SCHEME,
  SMOKE_TEST_PATH,
  inspectUpdateFeed,
  isAllowedExternalUrl,
  isTrustedAppUrl,
  normalizeUpdateChannel,
  projectRootFrom,
  resolveFrontendRoot,
} from './runtime-config.mjs';
import {
  registerApplicationProtocol,
  unregisterApplicationProtocol,
} from './app-protocol.mjs';
import { UpdateManager } from './update-manager.mjs';

protocol.registerSchemesAsPrivileged([{
  scheme: APP_SCHEME,
  privileges: {
    standard: true,
    secure: true,
    supportFetchAPI: true,
    corsEnabled: true,
    stream: true,
    codeCache: true,
  },
}]);

const userDataOverride = String(process.env.WECHAT_ASSISTANT_DESKTOP_USER_DATA_DIR || '').trim();
const desktopUserData = userDataOverride && path.isAbsolute(userDataOverride)
  ? userDataOverride
  : process.platform === 'win32' && process.env.LOCALAPPDATA
    ? path.join(process.env.LOCALAPPDATA, 'WechatAnalysisAssistant', 'desktop')
    : null;
if (desktopUserData) {
  mkdirSync(desktopUserData, { recursive: true });
  app.setPath('userData', desktopUserData);
}

const projectRoot = projectRootFrom(import.meta.url);
const sourceDirectory = path.dirname(fileURLToPath(import.meta.url));
const channel = normalizeUpdateChannel(process.env.WECHAT_ASSISTANT_UPDATE_CHANNEL, app.getVersion());
const updateFeed = inspectUpdateFeed({
  isPackaged: app.isPackaged,
  resourcesPath: process.resourcesPath,
});
const isSmokeTest = process.env.WECHAT_ASSISTANT_SMOKE_TEST === '1';
const entryUrl = isSmokeTest ? `${APP_ORIGIN}${SMOKE_TEST_PATH}` : `${APP_ORIGIN}/`;
const configuredSmokeTimeout = Number(process.env.WECHAT_ASSISTANT_SMOKE_TIMEOUT_MS);
const smokeTimeoutMs = Number.isFinite(configuredSmokeTimeout)
  ? Math.max(10_000, Math.min(300_000, configuredSmokeTimeout))
  : 120_000;
let mainWindow = null;
let backendManager = null;
let updateManager = null;
let shutdownComplete = false;
let shutdownPromise = null;

const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) app.quit();

function isTrustedSender(event) {
  return isTrustedAppUrl(event.senderFrame?.url);
}

function trustedHandler(handler) {
  return async (event, ...args) => {
    if (!isTrustedSender(event)) throw new Error('Untrusted desktop IPC sender');
    return handler(...args);
  };
}

async function selectFolder(options = {}) {
  const defaultPath = typeof options?.defaultPath === 'string' && path.isAbsolute(options.defaultPath)
    ? options.defaultPath
    : undefined;
  const result = await dialog.showOpenDialog(mainWindow, {
    title: '选择文件夹',
    defaultPath,
    properties: ['openDirectory', 'createDirectory'],
  });
  return {
    canceled: result.canceled,
    path: result.canceled ? null : (result.filePaths[0] || null),
  };
}

function applyWindowMode(mode) {
  const window = mainWindow;
  if (!window || window.isDestroyed()) return { mode: 'unavailable' };
  if (mode === 'main') {
    window.setResizable(true);
    window.setMaximizable(true);
    window.setFullScreenable(true);
    window.setMinimumSize(1080, 680);
    const [width, height] = window.getSize();
    if (width < 1080 || height < 680) {
      window.setSize(1400, 860, true);
      window.center();
    }
    return { mode: 'main' };
  }

  if (window.isFullScreen()) window.setFullScreen(false);
  if (window.isMaximized()) window.unmaximize();
  window.setResizable(true);
  window.setMinimumSize(900, 640);
  window.setSize(980, 720, true);
  window.center();
  window.setMaximizable(false);
  window.setFullScreenable(false);
  window.setResizable(false);
  return { mode: 'connection' };
}

function registerIpc() {
  ipcMain.handle('desktop:get-runtime-info', trustedHandler(() => ({
    appVersion: app.getVersion(),
    channel,
    platform: process.platform,
    arch: process.arch,
    isPackaged: app.isPackaged,
  })));
  ipcMain.handle('desktop:set-window-mode', trustedHandler(({ mode } = {}) => (
    applyWindowMode(mode === 'main' ? 'main' : 'connection')
  )));
  ipcMain.handle('desktop:select-folder', trustedHandler(selectFolder));
  ipcMain.handle('desktop:get-update-status', trustedHandler(() => updateManager.currentStatus()));
  ipcMain.handle('desktop:check-for-updates', trustedHandler(() => updateManager.check()));
  ipcMain.handle('desktop:download-update', trustedHandler(() => updateManager.download()));
  ipcMain.handle('desktop:install-update', trustedHandler(() => updateManager.install()));
}

function configureSessionSecurity(electronSession) {
  // The packaged UI has no camera, microphone, location, USB or notification
  // feature. Electron's default permission behavior is therefore narrowed to
  // deny-by-default instead of silently expanding capabilities later.
  electronSession.setPermissionCheckHandler(() => false);
  electronSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
    callback(false);
  });
  electronSession.setDevicePermissionHandler(() => false);
}

function createMainWindow() {
  const window = new BrowserWindow({
    width: 980,
    height: 720,
    minWidth: 900,
    minHeight: 640,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    show: false,
    title: '微信解析助手',
    backgroundColor: '#f4f7f5',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(sourceDirectory, 'preload.cjs'),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      navigateOnDragDrop: false,
      safeDialogs: true,
    },
  });

  window.webContents.setWindowOpenHandler(({ url }) => {
    if (isAllowedExternalUrl(url)) {
      void shell.openExternal(url).catch((error) => {
        console.warn(`Unable to open external URL: ${error?.message || error}`);
      });
    }
    return { action: 'deny' };
  });
  window.webContents.on('will-navigate', (event, url) => {
    if (!isTrustedAppUrl(url)) event.preventDefault();
  });
  window.webContents.on('will-attach-webview', (event) => event.preventDefault());
  window.once('ready-to-show', () => {
    if (!isSmokeTest) window.show();
  });
  window.on('closed', () => {
    if (mainWindow === window) mainWindow = null;
  });
  return window;
}

async function shutdownBackend() {
  if (shutdownComplete) return;
  if (!shutdownPromise) {
    shutdownPromise = (async () => {
      await backendManager?.stop();
      shutdownComplete = true;
    })();
  }
  await shutdownPromise;
}

async function verifyPackagedRenderer() {
  const result = await mainWindow.webContents.executeJavaScript(`(async () => {
    const response = await fetch('/api/desktop/health', { cache: 'no-store' });
    const runtimeResponse = await fetch('/api/desktop/self-test', {
      method: 'POST',
      cache: 'no-store',
    });
    let health = null;
    let runtime = null;
    try { health = await response.json(); } catch { health = null; }
    try { runtime = await runtimeResponse.json(); } catch { runtime = null; }
    return {
      hasDesktopBridge: window.wechatDesktop?.isDesktop === true,
      hasDocument: Boolean(document.body && document.documentElement),
      isDedicatedSmokePage: window.location.pathname === ${JSON.stringify(SMOKE_TEST_PATH)}
        && document.body?.textContent?.trim() === 'Desktop smoke test',
      statusCode: response.status,
      runtimeStatusCode: runtimeResponse.status,
      health,
      runtime,
    };
  })()`, true);
  if (
    result?.hasDesktopBridge !== true
    || result?.hasDocument !== true
    || result?.isDedicatedSmokePage !== true
    || result?.statusCode !== 200
    || result?.runtimeStatusCode !== 200
    || result?.health?.status !== 'ok'
    || result?.health?.desktop !== true
    || result?.runtime?.status !== 'ok'
  ) {
    throw new Error(`Packaged renderer smoke test failed: ${JSON.stringify(result)}`);
  }
  console.log('Packaged Electron smoke test passed: renderer, preload bridge and API proxy are ready.');
}

async function startDesktop() {
  configureSessionSecurity(session.defaultSession);
  const frontendRoot = resolveFrontendRoot({
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    projectRoot,
  });
  if (!existsSync(path.join(frontendRoot, 'index.html'))) {
    throw new Error(`Frontend build is missing: ${path.join(frontendRoot, 'index.html')}`);
  }

  backendManager = new BackendManager({
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    projectRoot,
    userDataPath: app.getPath('userData'),
    expectedAppVersion: app.getVersion(),
    expectedApiVersion: 1,
  });
  const backendStart = await backendManager.start();
  if (isSmokeTest) {
    const marker = Buffer.from(JSON.stringify({
      pid: backendStart.pid,
      executable: backendStart.executable,
    }), 'utf8').toString('base64');
    console.log(`WECHAT_ASSISTANT_SMOKE_BACKEND=${marker}`);
  }
  registerApplicationProtocol({
    frontendRoot,
    backendManager,
    smokeTest: isSmokeTest,
  });

  mainWindow = createMainWindow();
  updateManager = new UpdateManager({
    channel,
    updateFeed,
    getWindow: () => mainWindow,
  });
  registerIpc();
  await mainWindow.loadURL(entryUrl);
  if (isSmokeTest) {
    await verifyPackagedRenderer();
    await shutdownBackend();
    app.quit();
  }
}

if (hasSingleInstanceLock) {
  if (isSmokeTest) {
    const watchdog = setTimeout(() => {
      console.error(`Packaged Electron smoke test timed out after ${smokeTimeoutMs}ms.`);
      void shutdownBackend().finally(() => app.exit(2));
    }, smokeTimeoutMs);
    app.once('will-quit', () => clearTimeout(watchdog));
  }

  app.on('second-instance', () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });

  app.whenReady().then(startDesktop).catch(async (error) => {
    const details = backendManager?.startupFailureSummary();
    const detail = `${error?.message || error}${details && !String(error?.message || '').includes(details) ? `\n\n${details}` : ''}`.slice(0, 12000);
    if (isSmokeTest) {
      console.error(detail);
    } else {
      await dialog.showMessageBox({
        type: 'error',
        title: '微信解析助手启动失败',
        message: '桌面程序未能启动',
        detail,
      });
    }
    await shutdownBackend();
    app.exit(1);
  });

  app.on('activate', () => {
    if (!mainWindow && !shutdownComplete) {
      mainWindow = createMainWindow();
      void mainWindow.loadURL(entryUrl);
    }
  });

  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', (event) => {
    if (shutdownComplete) return;
    event.preventDefault();
    void shutdownBackend().finally(() => app.quit());
  });
  app.on('will-quit', () => {
    updateManager?.dispose();
    unregisterApplicationProtocol();
  });
}

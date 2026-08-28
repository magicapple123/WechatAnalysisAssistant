import updaterModule from 'electron-updater';

function releaseNotesText(value) {
  if (typeof value === 'string') return value.slice(0, 20_000);
  if (Array.isArray(value)) {
    return value
      .map((item) => item?.note || item?.version || '')
      .filter(Boolean)
      .join('\n')
      .slice(0, 20_000);
  }
  return '';
}

export class UpdateManager {
  constructor({ channel, updateFeed, getWindow, updater = null }) {
    this.channel = channel;
    this.updateFeed = updateFeed || { enabled: false, reason: 'missing' };
    this.getWindow = getWindow;
    // Accessing electron-updater's autoUpdater constructs an Electron adapter.
    // Keep that lookup lazy so this controller remains unit-testable in Node.
    this.updater = updater || updaterModule.autoUpdater;
    this.checkPromise = null;
    this.downloadPromise = null;
    this.listeners = [];
    this.status = this.updateFeed.enabled
      ? { state: 'idle', configured: true, message: '尚未检查更新' }
      : {
          state: 'disabled',
          configured: false,
          message: this.updateFeed.reason === 'development'
            ? '开发模式不检查在线更新'
            : '此内测包尚未配置在线更新服务，请从发布者处获取新安装包',
        };

    this.updater.autoDownload = false;
    this.updater.autoInstallOnAppQuit = false;
    this.updater.allowPrerelease = channel === 'beta';
    this.updater.channel = channel;

    this.on('checking-for-update', () => this.publish({ state: 'checking' }));
    this.on('update-available', (info) => this.publish({
      state: 'available',
      version: info.version,
      releaseNotes: releaseNotesText(info.releaseNotes),
    }));
    this.on('update-not-available', (info) => this.publish({
      state: 'not-available',
      version: info?.version,
      message: '当前已经是最新版本',
    }));
    this.on('download-progress', (progress) => this.publish({
      state: 'downloading',
      percent: Math.max(0, Math.min(100, Number(progress.percent) || 0)),
      version: this.status.version,
    }));
    this.on('update-downloaded', (info) => this.publish({
      state: 'downloaded',
      version: info.version,
      releaseNotes: releaseNotesText(info.releaseNotes),
      message: '更新已下载，等待用户确认重启安装',
    }));
    this.on('error', (error) => this.publish({
      state: 'error',
      message: error?.message || '检查更新失败',
    }));
  }

  on(eventName, listener) {
    this.listeners.push([eventName, listener]);
    this.updater.on(eventName, listener);
  }

  publish(next) {
    this.status = { ...this.status, ...next };
    const window = this.getWindow?.();
    if (
      window
      && window.isDestroyed?.() !== true
      && window.webContents?.isDestroyed?.() !== true
    ) {
      window.webContents.send('desktop:update-status', this.status);
    }
    return this.status;
  }

  currentStatus() {
    return { ...this.status };
  }

  async check() {
    if (!this.updateFeed.enabled) {
      return this.publish({ ...this.status, state: 'disabled', configured: false });
    }
    if (this.status.state === 'downloading') {
      throw new Error('更新正在下载，无法同时检查更新');
    }
    if (!this.checkPromise) {
      this.checkPromise = (async () => {
        await this.updater.checkForUpdates();
        return this.currentStatus();
      })();
    }
    try {
      return await this.checkPromise;
    } finally {
      this.checkPromise = null;
    }
  }

  async download() {
    if (!this.updateFeed.enabled) {
      return this.publish({ ...this.status, state: 'disabled', configured: false });
    }
    if (this.status.state !== 'available' && this.status.state !== 'downloading') {
      throw new Error('请先检查并确认有可用更新');
    }
    if (!this.downloadPromise) {
      this.downloadPromise = (async () => {
        await this.updater.downloadUpdate();
        return this.currentStatus();
      })();
    }
    try {
      return await this.downloadPromise;
    } finally {
      this.downloadPromise = null;
    }
  }

  async install() {
    if (this.status.state !== 'downloaded') {
      throw new Error('没有等待安装的更新');
    }
    this.publish({ state: 'installing', message: '正在重启并安装更新' });
    try {
      this.updater.quitAndInstall(false, true);
    } catch (error) {
      this.publish({ state: 'error', message: error?.message || '启动更新安装失败' });
      throw error;
    }
    if (this.status.state === 'error') {
      throw new Error(this.status.message || '启动更新安装失败');
    }
    return { accepted: true };
  }

  dispose() {
    for (const [eventName, listener] of this.listeners.splice(0)) {
      this.updater.removeListener(eventName, listener);
    }
  }
}

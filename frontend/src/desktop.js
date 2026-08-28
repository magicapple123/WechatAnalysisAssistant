/**
 * Narrow, optional bridge exposed by Electron's sandboxed preload script.
 *
 * The browser build deliberately keeps working without this object so the
 * React UI can still be developed and regression-tested with Vite.
 */

function bridge() {
  if (typeof window === 'undefined') return null;
  const candidate = window.wechatDesktop;
  return candidate && candidate.isDesktop === true ? candidate : null;
}

export function isDesktopApp() {
  return Boolean(bridge());
}

export async function getDesktopRuntimeInfo() {
  const desktop = bridge();
  if (!desktop?.getRuntimeInfo) return null;
  return desktop.getRuntimeInfo();
}

export async function setDesktopWindowMode(mode) {
  const desktop = bridge();
  if (!desktop?.setWindowMode) return null;
  return desktop.setWindowMode(mode === 'main' ? 'main' : 'connection');
}

export async function getDesktopUpdateStatus() {
  const desktop = bridge();
  if (!desktop?.getUpdateStatus) return null;
  return desktop.getUpdateStatus();
}

export async function selectDesktopFolder(defaultPath = '') {
  const desktop = bridge();
  if (!desktop?.selectFolder) return null;
  return desktop.selectFolder({ defaultPath: String(defaultPath || '') });
}

export async function checkForDesktopUpdates() {
  const desktop = bridge();
  if (!desktop?.checkForUpdates) {
    throw new Error('自动更新仅在安装后的桌面版中可用');
  }
  return desktop.checkForUpdates();
}

export async function downloadDesktopUpdate() {
  const desktop = bridge();
  if (!desktop?.downloadUpdate) {
    throw new Error('自动更新仅在安装后的桌面版中可用');
  }
  return desktop.downloadUpdate();
}

export async function installDesktopUpdate() {
  const desktop = bridge();
  if (!desktop?.installUpdate) {
    throw new Error('尚未下载可以安装的更新');
  }
  return desktop.installUpdate();
}

export function onDesktopUpdateStatus(callback) {
  const desktop = bridge();
  if (!desktop?.onUpdateStatus || typeof callback !== 'function') {
    return () => {};
  }
  const unsubscribe = desktop.onUpdateStatus(callback);
  return typeof unsubscribe === 'function' ? unsubscribe : () => {};
}

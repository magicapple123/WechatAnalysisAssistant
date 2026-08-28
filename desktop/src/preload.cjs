'use strict';

const { contextBridge, ipcRenderer } = require('electron');

function invoke(channel, payload) {
  return ipcRenderer.invoke(channel, payload);
}

const api = Object.freeze({
  isDesktop: true,
  getRuntimeInfo: () => invoke('desktop:get-runtime-info'),
  setWindowMode: (mode) => invoke('desktop:set-window-mode', {
    mode: mode === 'main' ? 'main' : 'connection',
  }),
  selectFolder: (options = {}) => invoke('desktop:select-folder', {
    defaultPath: typeof options?.defaultPath === 'string' ? options.defaultPath : undefined,
  }),
  getUpdateStatus: () => invoke('desktop:get-update-status'),
  checkForUpdates: () => invoke('desktop:check-for-updates'),
  downloadUpdate: () => invoke('desktop:download-update'),
  installUpdate: () => invoke('desktop:install-update'),
  onUpdateStatus: (callback) => {
    if (typeof callback !== 'function') throw new TypeError('Update listener must be a function');
    const listener = (_event, status) => callback(status);
    ipcRenderer.on('desktop:update-status', listener);
    return () => ipcRenderer.removeListener('desktop:update-status', listener);
  },
});

contextBridge.exposeInMainWorld('wechatDesktop', api);

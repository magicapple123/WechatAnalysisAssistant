import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getDesktopRuntimeInfo,
  getDesktopUpdateStatus,
  isDesktopApp,
  onDesktopUpdateStatus,
  selectDesktopFolder,
  setDesktopWindowMode,
} from './desktop.js';

function withoutWindow(callback) {
  const previous = globalThis.window;
  try {
    delete globalThis.window;
    return callback();
  } finally {
    if (previous !== undefined) globalThis.window = previous;
  }
}

test('browser mode remains available without an Electron preload bridge', async () => {
  await withoutWindow(async () => {
    assert.equal(isDesktopApp(), false);
    assert.equal(await getDesktopRuntimeInfo(), null);
    assert.equal(await getDesktopUpdateStatus(), null);
    assert.equal(await selectDesktopFolder('C:\\Exports'), null);
    assert.equal(await setDesktopWindowMode('main'), null);
  });
});

test('desktop helpers only call the narrow preload contract', async () => {
  const calls = [];
  const previous = globalThis.window;
  globalThis.window = {
    wechatDesktop: {
      isDesktop: true,
      getRuntimeInfo: async () => ({ appVersion: '0.1.0-beta.2' }),
      getUpdateStatus: async () => ({ state: 'disabled', configured: false }),
      setWindowMode: async (mode) => {
        calls.push({ mode });
        return { mode };
      },
      selectFolder: async (options) => {
        calls.push(options);
        return { canceled: false, path: 'D:\\Exports' };
      },
    },
  };
  try {
    assert.equal(isDesktopApp(), true);
    assert.deepEqual(await getDesktopRuntimeInfo(), { appVersion: '0.1.0-beta.2' });
    assert.deepEqual(await getDesktopUpdateStatus(), {
      state: 'disabled',
      configured: false,
    });
    assert.deepEqual(await selectDesktopFolder('C:\\Old'), {
      canceled: false,
      path: 'D:\\Exports',
    });
    assert.deepEqual(await setDesktopWindowMode('main'), { mode: 'main' });
    assert.deepEqual(await setDesktopWindowMode('unexpected'), { mode: 'connection' });
    assert.deepEqual(calls, [
      { defaultPath: 'C:\\Old' },
      { mode: 'main' },
      { mode: 'connection' },
    ]);
  } finally {
    if (previous === undefined) delete globalThis.window;
    else globalThis.window = previous;
  }
});

test('update subscription returns the preload unsubscribe function', () => {
  let unsubscribed = false;
  const previous = globalThis.window;
  globalThis.window = {
    wechatDesktop: {
      isDesktop: true,
      onUpdateStatus: (callback) => {
        callback({ state: 'checking' });
        return () => { unsubscribed = true; };
      },
    },
  };
  try {
    let received = null;
    const unsubscribe = onDesktopUpdateStatus((status) => { received = status; });
    assert.deepEqual(received, { state: 'checking' });
    unsubscribe();
    assert.equal(unsubscribed, true);
  } finally {
    if (previous === undefined) delete globalThis.window;
    else globalThis.window = previous;
  }
});

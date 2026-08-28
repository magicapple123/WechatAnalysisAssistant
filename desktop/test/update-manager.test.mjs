import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import test from 'node:test';

import { UpdateManager } from '../src/update-manager.mjs';

class FakeUpdater extends EventEmitter {
  checks = 0;

  downloads = 0;

  installs = 0;

  async checkForUpdates() {
    this.checks += 1;
    this.emit('checking-for-update');
    this.emit('update-available', { version: '0.1.0-beta.3', releaseNotes: 'Changes' });
  }

  async downloadUpdate() {
    this.downloads += 1;
    this.emit('download-progress', { percent: 42 });
    this.emit('update-downloaded', { version: '0.1.0-beta.3', releaseNotes: 'Changes' });
  }

  quitAndInstall() {
    this.installs += 1;
  }
}

function createManager(updater, sent = []) {
  return new UpdateManager({
    channel: 'beta',
    updateFeed: { enabled: true, url: 'https://updates.example.com/beta/' },
    updater,
    getWindow: () => ({
      isDestroyed: () => false,
      webContents: {
        isDestroyed: () => false,
        send: (channel, status) => sent.push([channel, { ...status }]),
      },
    }),
  });
}

test('update checks and downloads have explicit user-driven state transitions', async () => {
  const updater = new FakeUpdater();
  const sent = [];
  const manager = createManager(updater, sent);

  assert.equal(updater.autoDownload, false);
  assert.equal(updater.autoInstallOnAppQuit, false);
  assert.equal(updater.allowPrerelease, true);
  assert.equal(updater.channel, 'beta');

  await manager.check();
  assert.equal(updater.checks, 1);
  assert.equal(manager.currentStatus().state, 'available');

  await manager.download();
  assert.equal(updater.downloads, 1);
  assert.equal(manager.currentStatus().state, 'downloaded');
  assert.ok(sent.some(([channel, status]) => (
    channel === 'desktop:update-status' && status.state === 'downloading' && status.percent === 42
  )));

  manager.dispose();
  assert.equal(updater.listenerCount('update-available'), 0);
});

test('install validates readiness before asking Electron to quit', async () => {
  const updater = new FakeUpdater();
  const manager = createManager(updater);

  await assert.rejects(
    manager.install(),
    /没有等待安装的更新/,
  );
  assert.equal(updater.installs, 0);

  await manager.check();
  await manager.download();
  const result = await manager.install();
  assert.deepEqual(result, { accepted: true });
  assert.equal(updater.installs, 1);
  assert.equal(manager.currentStatus().state, 'installing');
});

test('disabled update feeds never call the network updater', async () => {
  const updater = new FakeUpdater();
  const manager = new UpdateManager({
    channel: 'beta',
    updateFeed: { enabled: false, reason: 'placeholder' },
    updater,
    getWindow: () => null,
  });

  assert.equal((await manager.check()).state, 'disabled');
  assert.equal((await manager.download()).state, 'disabled');
  assert.equal(updater.checks, 0);
  assert.equal(updater.downloads, 0);
});

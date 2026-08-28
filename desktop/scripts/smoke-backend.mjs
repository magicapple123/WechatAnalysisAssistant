import assert from 'node:assert/strict';
import http from 'node:http';
import { copyFileSync, existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { BackendManager } from '../src/backend-manager.mjs';
import { DESKTOP_TOKEN_HEADER } from '../src/runtime-config.mjs';

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const projectRoot = path.resolve(desktopRoot, '..');
const packageJson = JSON.parse(readFileSync(path.join(desktopRoot, 'package.json'), 'utf8'));
const executableArgument = process.argv[2];
const temporaryDataRoot = mkdtempSync(path.join(tmpdir(), 'wechat-analysis-assistant-smoke-'));
const runLiveDataSmoke = process.env.WECHAT_ASSISTANT_SMOKE_LIVE_DATA === '1';

// The default packaging smoke must never inspect a developer's real WeChat
// installation. Live-data validation is a separate, explicitly named command.
process.env.WECHAT_ASSISTANT_SMOKE_TEST = runLiveDataSmoke ? '0' : '1';

if (runLiveDataSmoke && process.platform === 'win32' && process.env.LOCALAPPDATA) {
  const persistedDataRoot = path.join(
    process.env.LOCALAPPDATA,
    'WechatAnalysisAssistant',
  );
  for (const fileName of ['wechat_keys.json', 'wechat_keys.txt']) {
    const source = path.join(persistedDataRoot, fileName);
    if (existsSync(source)) {
      copyFileSync(source, path.join(temporaryDataRoot, fileName));
    }
  }
}

function request(
  port,
  token = '',
  { method = 'GET', pathname = '/api/desktop/health', timeoutMs = 3000 } = {},
) {
  return new Promise((resolve, reject) => {
    const headers = token ? { [DESKTOP_TOKEN_HEADER]: token } : {};
    const req = http.request({
      host: '127.0.0.1',
      port,
      path: pathname,
      method,
      headers,
      timeout: timeoutMs,
    }, (response) => {
      const chunks = [];
      response.on('data', (chunk) => chunks.push(chunk));
      response.once('end', () => resolve({
        statusCode: response.statusCode,
        body: Buffer.concat(chunks).toString('utf8'),
      }));
    });
    req.once('timeout', () => req.destroy(new Error('Smoke-test request timed out')));
    req.once('error', reject);
    req.end();
  });
}

if (executableArgument) {
  process.env.WECHAT_ASSISTANT_BACKEND_EXECUTABLE = path.resolve(executableArgument);
}
process.env.WECHAT_ASSISTANT_DATA_DIR = temporaryDataRoot;

const backend = new BackendManager({
  isPackaged: false,
  resourcesPath: '',
  projectRoot,
  userDataPath: temporaryDataRoot,
  expectedAppVersion: packageJson.version,
  expectedApiVersion: 1,
  startupTimeoutMs: 90_000,
});

try {
  await backend.start();

  const unauthorized = await request(backend.port);
  assert.equal(unauthorized.statusCode, 401, 'Desktop API must reject requests without its session token');

  const authorized = await request(backend.port, backend.token);
  assert.equal(authorized.statusCode, 200);
  const health = JSON.parse(authorized.body);
  assert.equal(health.status, 'ok');
  assert.equal(health.desktop, true);
  assert.equal(health.app_version, packageJson.version);
  assert.equal(Number(health.api_version), 1);

  const nativeRuntime = await request(backend.port, backend.token, {
    method: 'POST',
    pathname: '/api/desktop/self-test',
    timeoutMs: 20_000,
  });
  assert.equal(nativeRuntime.statusCode, 200);
  const runtimePayload = JSON.parse(nativeRuntime.body);
  assert.equal(runtimePayload.status, 'ok');
  assert.ok(runtimePayload.checks.includes('pyav-hevc'));
  assert.ok(runtimePayload.checks.includes('silk-python'));

  if (runLiveDataSmoke) {
    const liveSteps = [
      { pathname: '/api/status' },
      { pathname: '/api/auto-detect', timeoutMs: 120_000 },
      { pathname: '/api/status' },
      { pathname: '/api/clear-cache', method: 'POST', timeoutMs: 20_000 },
      { pathname: '/api/chats', timeoutMs: 180_000 },
    ];
    for (const step of liveSteps) {
      const result = await request(backend.port, backend.token, step);
      assert.equal(
        result.statusCode,
        200,
        `${step.pathname} returned ${result.statusCode}: ${result.body.slice(0, 500)}`,
      );
      assert.equal(
        backend.child?.exitCode,
        null,
        `Frozen backend exited after ${step.pathname} (code ${backend.child?.exitCode})`,
      );
      console.log(`Live-data smoke step passed: ${step.method || 'GET'} ${step.pathname}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
    assert.equal(
      backend.child?.exitCode,
      null,
      `Frozen backend exited after live-data smoke (code ${backend.child?.exitCode})`,
    );
  }

  if (!runLiveDataSmoke) {
    const backendLogPath = path.join(temporaryDataRoot, 'logs', 'backend.log');
    const backendLog = existsSync(backendLogPath) ? readFileSync(backendLogPath, 'utf8') : '';
    assert.doesNotMatch(
      backendLog,
      /wxid_|Weixin\.exe|Active WeChat account:|Detecting local WeChat data|\/api\/auto-detect/i,
      'Default backend smoke test must not inspect live WeChat data',
    );
  }

  console.log(`Frozen backend smoke test passed on a random loopback port (${backend.port}).`);
} finally {
  await backend.stop();
  rmSync(temporaryDataRoot, { recursive: true, force: true, maxRetries: 3, retryDelay: 150 });
}

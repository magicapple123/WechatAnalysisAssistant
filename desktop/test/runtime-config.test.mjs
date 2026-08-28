import assert from 'node:assert/strict';
import path from 'node:path';
import test from 'node:test';
import {
  backendUrl,
  applicationRouteFor,
  isAllowedExternalUrl,
  isApiPath,
  isTrustedAppUrl,
  normalizeUpdateChannel,
  inspectUpdateFeed,
  resolveBackendLaunchSpec,
  safeStaticPath,
} from '../src/runtime-config.mjs';

test('update channels default from semantic prerelease versions', () => {
  assert.equal(normalizeUpdateChannel('', '0.1.0-beta.2'), 'beta');
  assert.equal(normalizeUpdateChannel('', '1.0.0'), 'latest');
  assert.equal(normalizeUpdateChannel('LATEST', '0.1.0-beta.2'), 'latest');
});

test('online updates require a packaged build with a real HTTPS feed', () => {
  assert.deepEqual(
    inspectUpdateFeed({ isPackaged: false, resourcesPath: 'unused' }),
    { enabled: false, reason: 'development', url: '' },
  );
  assert.equal(inspectUpdateFeed({
    isPackaged: true,
    resourcesPath: 'unused',
    readText: () => 'provider: generic\nurl: https://updates.example.invalid/app\n',
  }).enabled, false);
  const configured = inspectUpdateFeed({
    isPackaged: true,
    resourcesPath: 'unused',
    readText: () => 'provider: generic\nurl: https://updates.example.com/app/beta/\n',
  });
  assert.equal(configured.enabled, true);
  assert.equal(configured.url, 'https://updates.example.com/app/beta/');

  const github = inspectUpdateFeed({
    isPackaged: true,
    resourcesPath: 'unused',
    readText: () => [
      'provider: github',
      'owner: Magicapple-Coder',
      'repo: WechatAnalysisAssistant',
    ].join('\n'),
  });
  assert.deepEqual(github, {
    enabled: true,
    reason: '',
    url: 'https://github.com/Magicapple-Coder/WechatAnalysisAssistant/releases',
  });
  assert.equal(inspectUpdateFeed({
    isPackaged: true,
    resourcesPath: 'unused',
    readText: () => 'provider: github\nowner: Magicapple-Coder\n',
  }).enabled, false);
});

test('backend URL only accepts a valid TCP port', () => {
  assert.equal(backendUrl(54321, '/api/status', '?a=1'), 'http://127.0.0.1:54321/api/status?a=1');
  assert.throws(() => backendUrl(0, '/'), /Invalid backend port/);
});

test('desktop navigation and external links use strict URL allowlists', () => {
  assert.equal(isTrustedAppUrl('app://wechat-analysis-assistant/'), true);
  assert.equal(isTrustedAppUrl('app://wechat-analysis-assistant/settings'), true);
  assert.equal(isTrustedAppUrl('app://wechat-analysis-assistant.example/'), false);
  assert.equal(isTrustedAppUrl('app://wechat-analysis-assistant:443/'), false);
  assert.equal(isTrustedAppUrl('https://wechat-analysis-assistant/'), false);

  assert.equal(isAllowedExternalUrl('https://example.com/docs?a=1'), true);
  assert.equal(isAllowedExternalUrl('http://localhost:3000/docs'), true);
  assert.equal(isAllowedExternalUrl('mailto:maintainer@example.com?subject=Help'), true);
  assert.equal(isAllowedExternalUrl('https://user:password@example.com/'), false);
  assert.equal(isAllowedExternalUrl('file:///C:/Windows/System32/calc.exe'), false);
  assert.equal(isAllowedExternalUrl('javascript:alert(1)'), false);
  assert.equal(isAllowedExternalUrl('mailto:%0A@example.com'), false);
});

test('API path matching does not capture lookalike static paths', () => {
  assert.equal(isApiPath('/api/status'), true);
  assert.equal(isApiPath('/api'), true);
  assert.equal(isApiPath('/api-logo.svg'), false);
});

test('smoke routing exposes only the dedicated page and API proxy', () => {
  assert.equal(applicationRouteFor('/__desktop_smoke__', true), 'smoke');
  assert.equal(applicationRouteFor('/api/desktop/health', true), 'api');
  assert.equal(applicationRouteFor('/api', true), 'api');
  assert.equal(applicationRouteFor('/', true), 'not-found');
  assert.equal(applicationRouteFor('/assets/index.js', true), 'not-found');
  assert.equal(applicationRouteFor('/__desktop_smoke__/nested', true), 'not-found');
  assert.equal(applicationRouteFor('/', false), 'static');
});

test('static paths stay inside the frontend distribution directory', () => {
  const root = path.resolve('frontend-dist');
  assert.equal(safeStaticPath(root, '/assets/app.js'), path.join(root, 'assets', 'app.js'));
  assert.equal(safeStaticPath(root, '/'), path.join(root, 'index.html'));
  assert.equal(safeStaticPath(root, '/..\\secret.txt'), null);
  assert.equal(safeStaticPath(root, '/%2e%2e/secret.txt'), null);
  assert.equal(safeStaticPath(root, '/%00invalid'), null);
});

test('packaged launch spec selects the PyInstaller onedir executable', () => {
  const resourcesPath = path.resolve('resources');
  const expected = path.join(
    resourcesPath,
    'backend',
    'WechatAnalysisAssistantBackend',
    'WechatAnalysisAssistantBackend.exe',
  );
  const result = resolveBackendLaunchSpec({
    isPackaged: true,
    resourcesPath,
    projectRoot: path.resolve('project'),
    platform: 'win32',
    env: {},
    fileExists: (candidate) => candidate === expected,
  });
  assert.equal(result.executable, expected);
  assert.deepEqual(result.baseArgs, []);
});

test('an explicit backend override must exist before it is launched', () => {
  assert.throws(
    () => resolveBackendLaunchSpec({
      isPackaged: false,
      resourcesPath: '',
      projectRoot: path.resolve('project'),
      env: { WECHAT_ASSISTANT_BACKEND_EXECUTABLE: path.resolve('missing-backend.exe') },
      fileExists: () => false,
    }),
    /does not exist/i,
  );
});

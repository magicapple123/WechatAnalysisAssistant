import { existsSync, readFileSync } from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const APP_SCHEME = 'app';
export const APP_HOST = 'wechat-analysis-assistant';
export const APP_ORIGIN = `${APP_SCHEME}://${APP_HOST}`;
export const SMOKE_TEST_PATH = '/__desktop_smoke__';
export const DESKTOP_TOKEN_ENV = 'WECHAT_ASSISTANT_DESKTOP_TOKEN';
export const DESKTOP_PORT_ENV = 'WECHAT_ASSISTANT_DESKTOP_PORT';
export const DESKTOP_TOKEN_HEADER = 'X-Desktop-Session-Token';

function configurationValue(configuration, key) {
  const escapedKey = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = String(configuration).match(new RegExp(
    `^\\s*${escapedKey}\\s*:\\s*(?:"([^"]*)"|'([^']*)'|([^#\\r\\n]+?))\\s*(?:#.*)?$`,
    'im',
  ));
  return String(match?.[1] ?? match?.[2] ?? match?.[3] ?? '').trim();
}

function isGitHubOwner(value) {
  return value.length <= 39
    && /^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$/.test(value)
    && !value.includes('--');
}

function isGitHubRepository(value) {
  return value.length <= 100
    && /^[A-Za-z0-9_.-]+$/.test(value)
    && value !== '.'
    && value !== '..';
}

export function isTrustedAppUrl(value) {
  try {
    const parsed = new URL(String(value || ''));
    return parsed.protocol === `${APP_SCHEME}:`
      && parsed.hostname === APP_HOST
      && parsed.port === ''
      && parsed.username === ''
      && parsed.password === '';
  } catch {
    return false;
  }
}

export function isAllowedExternalUrl(value) {
  const raw = String(value || '').trim();
  if (!raw || raw.length > 8192 || /[\u0000-\u001f\u007f]/.test(raw)) return false;
  try {
    const parsed = new URL(raw);
    if (parsed.protocol === 'mailto:') {
      return parsed.pathname.length > 0 && !/[\r\n]/.test(decodeURIComponent(parsed.pathname));
    }
    return (parsed.protocol === 'https:' || parsed.protocol === 'http:')
      && Boolean(parsed.hostname)
      && parsed.username === ''
      && parsed.password === '';
  } catch {
    return false;
  }
}

export function projectRootFrom(importMetaUrl = import.meta.url) {
  return path.resolve(path.dirname(fileURLToPath(importMetaUrl)), '..', '..');
}

export function normalizeUpdateChannel(requested, appVersion = '') {
  const normalized = String(requested || '').trim().toLowerCase();
  if (normalized === 'beta' || normalized === 'latest') return normalized;
  return String(appVersion).includes('-') ? 'beta' : 'latest';
}

export function inspectUpdateFeed({
  isPackaged,
  resourcesPath,
  readText = (file) => readFileSync(file, 'utf8'),
}) {
  if (!isPackaged) {
    return { enabled: false, reason: 'development', url: '' };
  }
  try {
    const configuration = readText(path.join(resourcesPath, 'app-update.yml'));
    const provider = configurationValue(configuration, 'provider').toLowerCase();
    if (provider === 'github') {
      const owner = configurationValue(configuration, 'owner');
      const repo = configurationValue(configuration, 'repo');
      const enabled = isGitHubOwner(owner) && isGitHubRepository(repo);
      return {
        enabled,
        reason: enabled ? '' : 'invalid',
        url: enabled ? `https://github.com/${owner}/${repo}/releases` : '',
      };
    }

    const rawUrl = configurationValue(configuration, 'url');
    const parsed = new URL(rawUrl);
    const hostname = parsed.hostname.toLowerCase();
    const enabled = (provider === '' || provider === 'generic')
      && parsed.protocol === 'https:'
      && Boolean(hostname)
      && parsed.username === ''
      && parsed.password === ''
      && parsed.hash === ''
      && hostname !== 'example.invalid'
      && !hostname.endsWith('.example.invalid');
    return {
      enabled,
      reason: enabled ? '' : 'placeholder',
      url: enabled ? parsed.toString() : '',
    };
  } catch {
    return { enabled: false, reason: 'missing', url: '' };
  }
}

export function isApiPath(pathname) {
  return pathname === '/api' || pathname.startsWith('/api/');
}

export function applicationRouteFor(pathname, smokeTest = false) {
  if (isApiPath(pathname)) return 'api';
  if (smokeTest) return pathname === SMOKE_TEST_PATH ? 'smoke' : 'not-found';
  return 'static';
}

export function backendUrl(port, pathname = '/', search = '') {
  const numericPort = Number(port);
  if (!Number.isInteger(numericPort) || numericPort < 1 || numericPort > 65535) {
    throw new TypeError('Invalid backend port');
  }
  const safePath = String(pathname || '/').startsWith('/') ? String(pathname || '/') : `/${pathname}`;
  return `http://127.0.0.1:${numericPort}${safePath}${search || ''}`;
}

export async function reserveLoopbackPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once('error', reject);
    server.listen({ host: '127.0.0.1', port: 0, exclusive: true }, () => {
      const address = server.address();
      const port = typeof address === 'object' && address ? address.port : 0;
      server.close((error) => {
        if (error) reject(error);
        else if (!port) reject(new Error('Failed to reserve a loopback port'));
        else resolve(port);
      });
    });
  });
}

export function resolveFrontendRoot({ isPackaged, resourcesPath, projectRoot }) {
  return isPackaged
    ? path.join(resourcesPath, 'frontend')
    : path.join(projectRoot, 'frontend', 'dist');
}

export function resolveBackendLaunchSpec({
  isPackaged,
  resourcesPath,
  projectRoot,
  platform = process.platform,
  env = process.env,
  fileExists = existsSync,
}) {
  const override = String(env.WECHAT_ASSISTANT_BACKEND_EXECUTABLE || '').trim();
  if (override) {
    const executable = path.resolve(override);
    if (!fileExists(executable)) {
      throw new Error(`Configured backend executable does not exist: ${executable}`);
    }
    return { executable, baseArgs: [], cwd: path.dirname(executable) };
  }

  if (isPackaged) {
    const executableName = platform === 'win32'
      ? 'WechatAnalysisAssistantBackend.exe'
      : 'WechatAnalysisAssistantBackend';
    const candidates = [
      path.join(resourcesPath, 'backend', 'WechatAnalysisAssistantBackend', executableName),
      path.join(resourcesPath, 'backend', executableName),
    ];
    const executable = candidates.find(fileExists);
    if (!executable) {
      throw new Error(`Packaged backend sidecar is missing. Checked: ${candidates.join(', ')}`);
    }
    return { executable, baseArgs: [], cwd: path.dirname(executable) };
  }

  const configuredPython = String(env.WECHAT_ASSISTANT_PYTHON || '').trim();
  const venvPython = platform === 'win32'
    ? path.join(projectRoot, '.venv', 'Scripts', 'python.exe')
    : path.join(projectRoot, '.venv', 'bin', 'python');
  const executable = configuredPython || (fileExists(venvPython) ? venvPython : 'python');
  return {
    executable,
    baseArgs: ['-m', 'backend.main'],
    cwd: projectRoot,
  };
}

export function safeStaticPath(frontendRoot, pathname) {
  let decoded;
  try {
    decoded = decodeURIComponent(pathname || '/');
  } catch {
    return null;
  }
  if (decoded.includes('\0') || decoded.includes('\\')) return null;
  const relativeUrlPath = decoded.replace(/^\/+/, '') || 'index.html';
  const candidate = path.resolve(frontendRoot, relativeUrlPath);
  const relative = path.relative(path.resolve(frontendRoot), candidate);
  if (relative.startsWith('..') || path.isAbsolute(relative)) return null;
  return candidate;
}

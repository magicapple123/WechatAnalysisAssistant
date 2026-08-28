import { spawn } from 'node:child_process';
import { appendFileSync, mkdirSync, renameSync, rmSync, statSync } from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { randomBytes } from 'node:crypto';
import {
  DESKTOP_PORT_ENV,
  DESKTOP_TOKEN_ENV,
  DESKTOP_TOKEN_HEADER,
  backendUrl,
  reserveLoopbackPort,
  resolveBackendLaunchSpec,
} from './runtime-config.mjs';

const DEFAULT_START_TIMEOUT_MS = 90_000;
const MAX_LOG_BYTES = 2 * 1024 * 1024;

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function sanitizeLogText(value, token) {
  let text = String(value ?? '').replaceAll(token, '[desktop-session-redacted]');
  text = text.replace(/((?:api[_ -]?key|authorization|bearer|secret|token)\s*[:=]\s*)\S+/gi, '$1[redacted]');
  return text.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, '');
}

class CappedBackendLog {
  constructor(logDirectory, token) {
    mkdirSync(logDirectory, { recursive: true });
    this.file = path.join(logDirectory, 'backend.log');
    this.previousFile = path.join(logDirectory, 'backend.1.log');
    this.token = token;
    this.tail = [];
    this.bytes = 0;
    try {
      this.bytes = statSync(this.file).size;
    } catch {
      this.bytes = 0;
    }
    if (this.bytes >= MAX_LOG_BYTES) this.rotate();
  }

  rotate() {
    try {
      rmSync(this.previousFile, { force: true });
      renameSync(this.file, this.previousFile);
    } catch {
      // No current log or the previous file is locked; a later write can retry.
    }
    this.bytes = 0;
  }

  write(source, chunk) {
    const sanitized = sanitizeLogText(chunk, this.token);
    if (!sanitized) return;
    const line = `[${new Date().toISOString()}] [${source}] ${sanitized}`;
    const bytes = Buffer.byteLength(line, 'utf8');
    if (this.bytes + bytes > MAX_LOG_BYTES) this.rotate();
    try {
      appendFileSync(this.file, line, { encoding: 'utf8' });
      this.bytes += bytes;
    } catch {
      // Logging must never crash the desktop process.
    }
    for (const part of sanitized.split(/\r?\n/).filter(Boolean)) {
      this.tail.push(`[${source}] ${part}`);
    }
    if (this.tail.length > 40) this.tail.splice(0, this.tail.length - 40);
  }

  summary() {
    return this.tail.slice(-20).join('\n').slice(-6000);
  }
}

function requestBackend({ port, token, method, pathname, timeoutMs }) {
  return new Promise((resolve, reject) => {
    const request = http.request({
      host: '127.0.0.1',
      port,
      path: pathname,
      method,
      headers: {
        [DESKTOP_TOKEN_HEADER]: token,
        'Content-Length': '0',
      },
      timeout: timeoutMs,
    }, (response) => {
      const chunks = [];
      let received = 0;
      response.on('data', (chunk) => {
        received += chunk.length;
        if (received <= 64 * 1024) chunks.push(chunk);
      });
      response.once('end', () => {
        if (received > 64 * 1024) {
          reject(new Error('Backend control response was unexpectedly large'));
          return;
        }
        if ((response.statusCode || 500) < 400) {
          resolve({
            statusCode: response.statusCode,
            body: Buffer.concat(chunks).toString('utf8'),
          });
        } else reject(new Error(`Backend returned HTTP ${response.statusCode}`));
      });
    });
    request.once('timeout', () => request.destroy(new Error('Backend request timed out')));
    request.once('error', reject);
    request.end();
  });
}

export function validateHealthPayload(rawBody, expectedAppVersion, expectedApiVersion = 1) {
  let payload;
  try {
    payload = JSON.parse(rawBody);
  } catch {
    throw new Error('Backend health response was not valid JSON');
  }
  if (payload?.status !== 'ok' || payload?.desktop !== true) {
    throw new Error('Backend health response did not identify a desktop sidecar');
  }
  if (Number(payload.api_version) !== Number(expectedApiVersion)) {
    throw new Error(`Backend API version mismatch (expected ${expectedApiVersion}, got ${payload?.api_version ?? 'missing'})`);
  }
  if (String(payload.app_version || '') !== String(expectedAppVersion || '')) {
    throw new Error(`Backend app version mismatch (expected ${expectedAppVersion}, got ${payload?.app_version || 'missing'})`);
  }
  if (!Number.isSafeInteger(payload.pid) || payload.pid <= 0) {
    throw new Error('Backend health response did not include a valid process ID');
  }
  return payload;
}

function hasExited(child) {
  return !child
    || child.exitCode !== null
    || child.signalCode !== null
    || !Number.isInteger(child.pid);
}

async function waitForExit(child, timeoutMs) {
  if (hasExited(child)) return true;
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      child.off('exit', onExit);
      child.off('close', onExit);
      resolve(false);
    }, timeoutMs);
    const onExit = () => {
      clearTimeout(timer);
      child.off('exit', onExit);
      child.off('close', onExit);
      resolve(true);
    };
    child.once('exit', onExit);
    child.once('close', onExit);
  });
}

export class BackendManager {
  constructor({
    isPackaged,
    resourcesPath,
    projectRoot,
    userDataPath,
    expectedAppVersion,
    expectedApiVersion = 1,
    startupTimeoutMs = DEFAULT_START_TIMEOUT_MS,
  }) {
    this.options = { isPackaged, resourcesPath, projectRoot };
    this.userDataPath = userDataPath;
    this.expectedAppVersion = expectedAppVersion;
    this.expectedApiVersion = expectedApiVersion;
    this.startupTimeoutMs = startupTimeoutMs;
    this.child = null;
    this.port = null;
    this.token = null;
    this.isReady = false;
    this.stopping = false;
    this.stopPromise = null;
    this.log = null;
    this.executable = null;
  }

  async start() {
    if (this.child) throw new Error('Desktop backend has already been started');
    this.port = await reserveLoopbackPort();
    this.token = randomBytes(32).toString('hex');
    this.log = new CappedBackendLog(path.join(this.userDataPath, 'logs'), this.token);
    const launch = resolveBackendLaunchSpec(this.options);
    this.executable = path.resolve(launch.executable);
    const args = [
      ...launch.baseArgs,
      '--host', '127.0.0.1',
      '--port', String(this.port),
      '--no-browser',
    ];
    this.log.write('desktop', `Starting backend process: ${path.basename(launch.executable)}\n`);
    this.child = spawn(launch.executable, args, {
      cwd: launch.cwd,
      env: {
        ...process.env,
        [DESKTOP_TOKEN_ENV]: this.token,
        [DESKTOP_PORT_ENV]: String(this.port),
        WECHAT_ASSISTANT_DESKTOP_MODE: '1',
        WECHAT_ASSISTANT_PARENT_PID: String(process.pid),
        WECHAT_ASSISTANT_APP_VERSION: String(this.expectedAppVersion),
        WECHAT_ASSISTANT_DESKTOP_API_VERSION: String(this.expectedApiVersion),
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8',
        PYTHONUNBUFFERED: '1',
      },
      shell: false,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    this.child.stdout?.on('data', (chunk) => this.log.write('stdout', chunk));
    this.child.stderr?.on('data', (chunk) => this.log.write('stderr', chunk));
    this.child.once('error', (error) => this.log.write('process-error', `${error.message}\n`));
    this.child.once('exit', (code, signal) => {
      this.log.write('desktop', `Backend exited (code=${code}, signal=${signal || 'none'})\n`);
      this.isReady = false;
    });

    const deadline = Date.now() + this.startupTimeoutMs;
    let lastError = null;
    while (Date.now() < deadline) {
      if (hasExited(this.child)) {
        const summary = this.log.summary();
        throw new Error(
          `Desktop backend exited during startup (code ${this.child.exitCode}, signal ${this.child.signalCode || 'none'}).`
          + (summary ? `\n${summary}` : ''),
        );
      }
      try {
        const health = await requestBackend({
          port: this.port,
          token: this.token,
          method: 'GET',
          pathname: '/api/desktop/health',
          timeoutMs: 1000,
        });
        const payload = validateHealthPayload(
          health.body,
          this.expectedAppVersion,
          this.expectedApiVersion,
        );
        // A PyInstaller onedir executable can keep a bootloader parent while
        // Uvicorn runs in its child process, so the authenticated health PID is
        // not required to equal ChildProcess.pid. The smoke harness records
        // this server PID and verifies any surviving process by executable path.
        this.isReady = true;
        this.log.write('desktop', `Backend is ready at ${backendUrl(this.port, '/')}\n`);
        return {
          port: this.port,
          pid: payload.pid,
          executable: this.executable,
        };
      } catch (error) {
        lastError = error;
        await delay(250);
      }
    }

    const summary = this.log.summary();
    await this.stop({ prepare: false });
    throw new Error(
      `Desktop backend did not become ready within ${Math.round(this.startupTimeoutMs / 1000)} seconds: ${lastError?.message || 'unknown error'}`
      + (summary ? `\n${summary}` : ''),
    );
  }

  async prepareExit(timeoutMs = 5000) {
    if (!this.child || !this.isReady) return;
    try {
      await requestBackend({
        port: this.port,
        token: this.token,
        method: 'POST',
        pathname: '/api/desktop/prepare-exit',
        timeoutMs,
      });
    } catch (error) {
      this.log?.write('desktop', `Backend prepare-exit was unavailable: ${error.message}\n`);
    }
  }

  async stop({ prepare = true } = {}) {
    if (this.stopPromise) return this.stopPromise;
    this.stopping = true;
    const child = this.child;
    const operation = (async () => {
      if (hasExited(child)) return;
      if (prepare) {
        await this.prepareExit();
        // The prepare route asks Uvicorn to stop accepting work and begin its
        // bounded graceful shutdown. Give it time to close databases and
        // finish/cancel active requests before falling back to termination.
        if (await waitForExit(child, 12_000)) return;
      }

      child.kill('SIGTERM');
      if (await waitForExit(child, 2000)) return;

      if (process.platform === 'win32' && child.pid) {
        const killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], {
          windowsHide: true,
          stdio: 'ignore',
        });
        if (!(await waitForExit(killer, 5000))) killer.kill();
      } else {
        child.kill('SIGKILL');
      }
      if (!(await waitForExit(child, 5000))) {
        throw new Error(`Backend process ${child.pid || '(unknown PID)'} did not exit after forced termination`);
      }
    })();
    this.stopPromise = operation;
    try {
      await operation;
    } finally {
      this.isReady = false;
      if (hasExited(child)) this.child = null;
      this.stopping = false;
      if (this.stopPromise === operation) this.stopPromise = null;
    }
  }

  startupFailureSummary() {
    return this.log?.summary() || '';
  }
}

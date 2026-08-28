import { readFile, stat } from 'node:fs/promises';
import path from 'node:path';
import { net, protocol } from 'electron';
import {
  APP_HOST,
  APP_SCHEME,
  DESKTOP_TOKEN_HEADER,
  SMOKE_TEST_PATH,
  applicationRouteFor,
  safeStaticPath,
} from './runtime-config.mjs';
import {
  proxyHeadersFor,
  proxyTargetFor,
  requestInitForProxy,
} from './protocol-utils.mjs';

const MIME_TYPES = new Map([
  ['.css', 'text/css; charset=utf-8'],
  ['.gif', 'image/gif'],
  ['.html', 'text/html; charset=utf-8'],
  ['.ico', 'image/x-icon'],
  ['.jpeg', 'image/jpeg'],
  ['.jpg', 'image/jpeg'],
  ['.js', 'text/javascript; charset=utf-8'],
  ['.json', 'application/json; charset=utf-8'],
  ['.map', 'application/json; charset=utf-8'],
  ['.png', 'image/png'],
  ['.svg', 'image/svg+xml'],
  ['.webp', 'image/webp'],
  ['.woff', 'font/woff'],
  ['.woff2', 'font/woff2'],
]);

const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https: http:",
  "media-src 'self' data: blob:",
  "font-src 'self' data:",
  "connect-src 'self'",
  "worker-src 'self' blob:",
  "object-src 'none'",
  "frame-src 'none'",
  "base-uri 'none'",
  "frame-ancestors 'none'",
  "form-action 'self'",
].join('; ');

const SMOKE_TEST_HTML = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>Desktop smoke test</title></head><body>Desktop smoke test</body></html>';

function errorResponse(status, message) {
  return new Response(message, {
    status,
    headers: {
      'Content-Type': 'text/plain; charset=utf-8',
      'Cache-Control': 'no-store',
      'Content-Security-Policy': CSP,
      'X-Content-Type-Options': 'nosniff',
    },
  });
}

async function serveStatic(frontendRoot, pathname, method = 'GET') {
  let candidate = safeStaticPath(frontendRoot, pathname);
  if (!candidate) return errorResponse(400, 'Invalid application path');

  let fileStat;
  try {
    fileStat = await stat(candidate);
  } catch {
    fileStat = null;
  }

  if (!fileStat?.isFile() && !path.extname(candidate)) {
    candidate = path.join(frontendRoot, 'index.html');
    try {
      fileStat = await stat(candidate);
    } catch {
      fileStat = null;
    }
  }
  if (!fileStat?.isFile()) return errorResponse(404, 'Application resource not found');

  const body = await readFile(candidate);
  const extension = path.extname(candidate).toLowerCase();
  const isEntry = path.basename(candidate).toLowerCase() === 'index.html';
  return new Response(method === 'HEAD' ? null : body, {
    status: 200,
    headers: {
      'Content-Type': MIME_TYPES.get(extension) || 'application/octet-stream',
      'Content-Length': String(body.byteLength),
      'Cache-Control': isEntry ? 'no-store' : 'public, max-age=31536000, immutable',
      'Content-Security-Policy': CSP,
      'X-Content-Type-Options': 'nosniff',
      'Referrer-Policy': 'no-referrer',
      'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), usb=(), serial=(), hid=(), payment=()',
      'Cross-Origin-Resource-Policy': 'same-origin',
    },
  });
}

async function proxyApi(request, backendManager) {
  if (!backendManager.isReady) return errorResponse(503, 'Desktop backend is not ready');
  const target = proxyTargetFor(request.url, backendManager.port);
  const headers = proxyHeadersFor(request.headers, backendManager.token, DESKTOP_TOKEN_HEADER);
  try {
    // Returning net.fetch's Response preserves the backend status, headers and
    // streaming body. Range requests, audio and large images are not buffered.
    return await net.fetch(target, requestInitForProxy(request, headers));
  } catch (error) {
    return errorResponse(502, `Desktop backend request failed: ${error?.message || 'unknown error'}`);
  }
}

function smokeTestResponse(method) {
  return new Response(method === 'HEAD' ? null : SMOKE_TEST_HTML, {
    status: 200,
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'Content-Length': String(Buffer.byteLength(SMOKE_TEST_HTML)),
      'Cache-Control': 'no-store',
      'Content-Security-Policy': CSP,
      'X-Content-Type-Options': 'nosniff',
      'Referrer-Policy': 'no-referrer',
    },
  });
}

export function registerApplicationProtocol({ frontendRoot, backendManager, smokeTest = false }) {
  protocol.handle(APP_SCHEME, async (request) => {
    const parsed = new URL(request.url);
    if (parsed.hostname !== APP_HOST) return errorResponse(404, 'Unknown application host');
    const route = applicationRouteFor(parsed.pathname, smokeTest);
    if (route === 'api') return proxyApi(request, backendManager);
    // A release smoke run must never fall through to the real React entry or
    // its assets. This keeps future frontend startup effects out of the smoke
    // harness even if a window is recreated during application activation.
    if (route === 'not-found') return errorResponse(404, 'Smoke-test resource not found');
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return errorResponse(405, 'Method not allowed');
    }
    if (route === 'smoke') {
      return smokeTestResponse(request.method);
    }
    return serveStatic(frontendRoot, parsed.pathname, request.method);
  });
}

export function unregisterApplicationProtocol() {
  try {
    protocol.unhandle(APP_SCHEME);
  } catch {
    // The protocol may not have been registered if startup failed early.
  }
}

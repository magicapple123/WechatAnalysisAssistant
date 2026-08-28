import { backendUrl } from './runtime-config.mjs';

const STRIPPED_REQUEST_HEADERS = new Set([
  'connection',
  'host',
  'origin',
  'proxy-authorization',
  'proxy-connection',
  'referer',
  'sec-fetch-dest',
  'sec-fetch-mode',
  'sec-fetch-site',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
]);

export function proxyTargetFor(requestUrl, backendPort) {
  const parsed = new URL(requestUrl);
  return backendUrl(backendPort, parsed.pathname, parsed.search);
}

export function proxyHeadersFor(inputHeaders, token, tokenHeader) {
  const output = new Headers();
  for (const [name, value] of new Headers(inputHeaders).entries()) {
    if (!STRIPPED_REQUEST_HEADERS.has(name.toLowerCase())) output.set(name, value);
  }
  output.set(tokenHeader, token);
  return output;
}

export function requestInitForProxy(request, headers) {
  const method = String(request.method || 'GET').toUpperCase();
  const init = {
    method,
    headers,
    redirect: 'manual',
  };
  if (method !== 'GET' && method !== 'HEAD' && request.body) {
    init.body = request.body;
    // Node/Electron fetch requires duplex for a streaming request body.
    init.duplex = 'half';
  }
  return init;
}

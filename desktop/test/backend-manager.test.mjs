import assert from 'node:assert/strict';
import test from 'node:test';

import { validateHealthPayload } from '../src/backend-manager.mjs';

const healthy = JSON.stringify({
  status: 'ok',
  desktop: true,
  app_version: '0.1.0-beta.3',
  api_version: 1,
  pid: 1234,
});

test('desktop handshake accepts only the matching authenticated sidecar', () => {
  const payload = validateHealthPayload(healthy, '0.1.0-beta.3', 1);
  assert.equal(payload.pid, 1234);
});

test('desktop handshake rejects an unrelated localhost service', () => {
  assert.throws(
    () => validateHealthPayload('<html>not the backend</html>', '0.1.0-beta.3', 1),
    /valid JSON/,
  );
  assert.throws(
    () => validateHealthPayload(JSON.stringify({ status: 'ok' }), '0.1.0-beta.3', 1),
    /desktop sidecar/,
  );
});

test('desktop handshake rejects mismatched application and API versions', () => {
  assert.throws(
    () => validateHealthPayload(healthy, '0.1.0-beta.4', 1),
    /app version mismatch/,
  );
  assert.throws(
    () => validateHealthPayload(healthy, '0.1.0-beta.3', 2),
    /API version mismatch/,
  );
});

test('desktop handshake requires a valid backend process ID', () => {
  const withoutPid = JSON.stringify({
    status: 'ok',
    desktop: true,
    app_version: '0.1.0-beta.3',
    api_version: 1,
  });
  assert.throws(
    () => validateHealthPayload(withoutPid, '0.1.0-beta.3', 1),
    /valid process ID/,
  );
});

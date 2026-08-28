import assert from 'node:assert/strict';
import test from 'node:test';

import api, { normalizeApiError } from './api.js';

test('normalizeApiError keeps FastAPI validation details and request metadata', () => {
  const source = {
    code: 'ERR_BAD_REQUEST',
    response: {
      status: 422,
      data: {
        detail: [
          { loc: ['body', 'key'], msg: '必填字段' },
          { loc: ['body', 'page'], msg: '必须大于 0' },
        ],
      },
    },
  };

  const error = normalizeApiError(source);

  assert.equal(error.name, 'ApiError');
  assert.equal(error.message, 'key：必填字段；page：必须大于 0');
  assert.equal(error.status, 422);
  assert.equal(error.code, 'ERR_BAD_REQUEST');
  assert.equal(error.cause, source);
});

test('normalizeApiError provides actionable network, timeout and cancellation messages', () => {
  assert.equal(
    normalizeApiError({ code: 'ERR_NETWORK', message: 'Network Error' }).message,
    '无法连接本机服务，请确认程序仍在运行',
  );
  assert.equal(
    normalizeApiError({ code: 'ETIMEDOUT' }).message,
    '请求超时，请稍后重试',
  );

  const canceled = normalizeApiError({ code: 'ERR_CANCELED' });
  assert.equal(canceled.name, 'CanceledError');
  assert.equal(canceled.message, '请求已取消');
});

test('voice URLs retain the server id needed to disambiguate media rows', () => {
  assert.equal(
    api.voiceAudioUrl('room/name', 7, 123, 'svr/42'),
    '/api/chat/room%2Fname/voice/7/audio?create_time=123&server_id=svr%2F42',
  );
  assert.equal(
    api.voiceExportUrl('alice', 8, 456, 0),
    '/api/chat/alice/voice/8/export?create_time=456&server_id=0',
  );
});

import assert from 'node:assert/strict';
import test from 'node:test';
import {
  proxyHeadersFor,
  proxyTargetFor,
  requestInitForProxy,
} from '../src/protocol-utils.mjs';

test('API proxy keeps path and query on the random loopback port', () => {
  assert.equal(
    proxyTargetFor('app://wechat-analysis-assistant/api/chat/a/image/1?quality=best', 43123),
    'http://127.0.0.1:43123/api/chat/a/image/1?quality=best',
  );
});

test('proxy headers preserve media Range and content type while hiding app origin', () => {
  const headers = proxyHeadersFor({
    Origin: 'app://wechat-analysis-assistant',
    Range: 'bytes=100-200',
    'Content-Type': 'multipart/form-data; boundary=example',
    Accept: 'image/*',
    'X-Desktop-Session-Token': 'renderer-controlled-value',
  }, 'secret-token', 'X-Desktop-Session-Token');
  assert.equal(headers.get('origin'), null);
  assert.equal(headers.get('range'), 'bytes=100-200');
  assert.equal(headers.get('content-type'), 'multipart/form-data; boundary=example');
  assert.equal(headers.get('accept'), 'image/*');
  assert.equal(headers.get('x-desktop-session-token'), 'secret-token');
});

test('proxy request keeps non-GET streaming bodies', () => {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('{"ok":true}'));
      controller.close();
    },
  });
  const init = requestInitForProxy({ method: 'POST', body }, new Headers());
  assert.equal(init.method, 'POST');
  assert.equal(init.body, body);
  assert.equal(init.duplex, 'half');

  const getInit = requestInitForProxy({ method: 'GET', body }, new Headers());
  assert.equal('body' in getInit, false);
});

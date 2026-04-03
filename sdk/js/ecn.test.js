/**
 * ecn.test.js
 * -----------
 * Unit tests for the ECN JavaScript SDK.
 *
 * Uses Node.js built-in test runner (node:test) and mocks `fetch` via
 * globalThis.fetch so no live server is required.
 */

import { test, describe, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { ECNClient, ECNError, ECNTransactionError, ECNNotFoundError } from './ecn.js';

// ---------------------------------------------------------------------------
// fetch mock helpers
// ---------------------------------------------------------------------------

function mockFetch(statusCode, body, headers = {}) {
  const isNull = body === null;
  const bodyText = isNull ? '' : JSON.stringify(body);
  const respHeaders = new Headers({
    'content-type': isNull ? 'text/plain' : 'application/json',
    'content-length': String(bodyText.length),
    ...headers,
  });
  globalThis.fetch = async () => ({
    status: statusCode,
    ok: statusCode >= 200 && statusCode < 300,
    headers: respHeaders,
    json: async () => body ?? {},
  });
}

function captureFetch(statusCode, body) {
  let captured = null;
  const bodyText = JSON.stringify(body);
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return {
      status: statusCode,
      ok: statusCode >= 200 && statusCode < 300,
      headers: new Headers({
        'content-type': 'application/json',
        'content-length': String(bodyText.length),
      }),
      json: async () => body,
    };
  };
  return () => captured;
}

// ---------------------------------------------------------------------------
// Round response fixture
// ---------------------------------------------------------------------------

const ROUND_RESPONSE = {
  round_id: 1,
  timestamp: '2026-04-03T21:00:00Z',
  transaction: { type: 'ship' },
  consensus_reached: true,
  agreed_hash: 'abc123',
  honest_nodes: ['A', 'B'],
  faulty_nodes: [],
  invalid_sig_nodes: [],
  votes: [],
};

// ---------------------------------------------------------------------------
// ECNClient constructor
// ---------------------------------------------------------------------------

describe('ECNClient constructor', () => {
  test('strips trailing slash from baseUrl', () => {
    const c = new ECNClient('http://localhost:8000/');
    assert.equal(c._baseUrl, 'http://localhost:8000');
  });

  test('stores apiKey option', () => {
    const c = new ECNClient('http://localhost:8000', { apiKey: 'my-secret' });
    assert.equal(c._apiKey, 'my-secret');
  });

  test('defaults to no apiKey', () => {
    const c = new ECNClient('http://localhost:8000');
    assert.equal(c._apiKey, null);
  });

  test('includes X-API-Key header when apiKey provided', () => {
    const c = new ECNClient('http://localhost:8000', { apiKey: 'tok' });
    const headers = c._headers();
    assert.equal(headers['X-API-Key'], 'tok');
  });
});

// ---------------------------------------------------------------------------
// Error handling
// ---------------------------------------------------------------------------

describe('Error handling', () => {
  test('404 throws ECNNotFoundError', async () => {
    const c = new ECNClient('http://localhost:8000');
    mockFetch(404, { detail: 'Not found' });
    await assert.rejects(() => c.auditEvent(99), ECNNotFoundError);
  });

  test('422 throws ECNTransactionError', async () => {
    const c = new ECNClient('http://localhost:8000');
    mockFetch(422, { detail: 'Bad tx' });
    await assert.rejects(() => c.submit('explode'), ECNTransactionError);
  });

  test('500 throws ECNError', async () => {
    const c = new ECNClient('http://localhost:8000');
    mockFetch(500, { detail: 'Server error' });
    await assert.rejects(() => c.networkState(), ECNError);
  });

  test('204 returns null', async () => {
    const c = new ECNClient('http://localhost:8000');
    mockFetch(204, null);
    const result = await c.unsubscribeWebhook('some-id');
    assert.equal(result, null);
  });
});

// ---------------------------------------------------------------------------
// Transaction helpers
// ---------------------------------------------------------------------------

describe('submit', () => {
  test('posts to /transactions', async () => {
    const c = new ECNClient('http://localhost:8000');
    const getCapture = captureFetch(200, ROUND_RESPONSE);
    await c.submit('ship', { product_id: 'P1', destination: 'port', shipper: 'DHL' });
    const req = getCapture();
    assert.ok(req.url.includes('/transactions'));
    assert.equal(req.init.method, 'POST');
    const sent = JSON.parse(req.init.body);
    assert.equal(sent.type, 'ship');
    assert.equal(sent.product_id, 'P1');
  });
});

describe('ship', () => {
  test('sends correct fields', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(200, ROUND_RESPONSE);
    await c.ship('LAPTOP-001', { destination: 'port', shipper: 'DHL' });
    const body = JSON.parse(get().init.body);
    assert.equal(body.type, 'ship');
    assert.equal(body.product_id, 'LAPTOP-001');
    assert.equal(body.destination, 'port');
    assert.equal(body.shipper, 'DHL');
  });
});

describe('receive', () => {
  test('sends correct fields', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(200, ROUND_RESPONSE);
    await c.receive('LAPTOP-001', { receiver: 'customs', atCustoms: true });
    const body = JSON.parse(get().init.body);
    assert.equal(body.type, 'receive');
    assert.equal(body.at_customs, true);
  });

  test('defaults atCustoms to false', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(200, ROUND_RESPONSE);
    await c.receive('LAPTOP-001', { receiver: 'buyer' });
    const body = JSON.parse(get().init.body);
    assert.equal(body.at_customs, false);
  });
});

describe('inspect', () => {
  test('sends check_name and inspector', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(200, ROUND_RESPONSE);
    await c.inspect('LAPTOP-001', { checkName: 'label_check', inspector: 'Lab' });
    const body = JSON.parse(get().init.body);
    assert.equal(body.type, 'inspect');
    assert.equal(body.check_name, 'label_check');
  });
});

describe('quarantine', () => {
  test('sends reason', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(200, ROUND_RESPONSE);
    await c.quarantine('P1', 'suspicious');
    const body = JSON.parse(get().init.body);
    assert.equal(body.type, 'quarantine');
    assert.equal(body.reason, 'suspicious');
  });
});

describe('release', () => {
  test('sends released_by', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(200, ROUND_RESPONSE);
    await c.release('P1', 'Inspector');
    const body = JSON.parse(get().init.body);
    assert.equal(body.type, 'release');
    assert.equal(body.released_by, 'Inspector');
  });
});

// ---------------------------------------------------------------------------
// Network endpoints
// ---------------------------------------------------------------------------

describe('nodes', () => {
  test('calls /network/nodes', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(200, []);
    await c.nodes();
    assert.ok(get().url.includes('/network/nodes'));
  });
});

describe('networkState', () => {
  test('calls /network/state', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(200, { node_count: 5 });
    const result = await c.networkState();
    assert.ok(get().url.includes('/network/state'));
    assert.equal(result.node_count, 5);
  });
});

// ---------------------------------------------------------------------------
// Audit endpoints
// ---------------------------------------------------------------------------

describe('auditEvents', () => {
  test('calls /audit/events with pagination params', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(200, { total: 0, offset: 10, events: [] });
    await c.auditEvents({ limit: 20, offset: 10 });
    assert.ok(get().url.includes('limit=20'));
    assert.ok(get().url.includes('offset=10'));
  });
});

describe('auditEvent', () => {
  test('calls /audit/events/{id}', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(200, ROUND_RESPONSE);
    await c.auditEvent(1);
    assert.ok(get().url.includes('/audit/events/1'));
  });

  test('throws ECNNotFoundError for unknown round', async () => {
    const c = new ECNClient('http://localhost:8000');
    mockFetch(404, { detail: 'not found' });
    await assert.rejects(() => c.auditEvent(99), ECNNotFoundError);
  });
});

describe('auditSummary', () => {
  test('calls /audit/summary', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(200, { fault_rate: 0.1 });
    const result = await c.auditSummary();
    assert.ok(get().url.includes('/audit/summary'));
    assert.equal(result.fault_rate, 0.1);
  });
});

// ---------------------------------------------------------------------------
// Webhook endpoints
// ---------------------------------------------------------------------------

describe('subscribeWebhook', () => {
  test('posts to /webhooks with url and events', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(201, { webhook_id: 'wh-1', url: 'https://x.com', events: ['fault'], message: 'ok' });
    const result = await c.subscribeWebhook('https://x.com', ['fault']);
    const body = JSON.parse(get().init.body);
    assert.equal(body.url, 'https://x.com');
    assert.deepEqual(body.events, ['fault']);
    assert.equal(result.webhook_id, 'wh-1');
  });
});

describe('listWebhooks', () => {
  test('calls GET /webhooks', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(200, []);
    await c.listWebhooks();
    assert.ok(get().url.includes('/webhooks'));
    assert.equal(get().init.method, 'GET');
  });
});

describe('unsubscribeWebhook', () => {
  test('sends DELETE /webhooks/{id}', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(204, null);
    await c.unsubscribeWebhook('wh-1');
    assert.ok(get().url.includes('/webhooks/wh-1'));
    assert.equal(get().init.method, 'DELETE');
  });
});

// ---------------------------------------------------------------------------
// Event streaming
// ---------------------------------------------------------------------------

describe('streamTopics', () => {
  test('calls /stream/topics', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(200, { topics: [] });
    await c.streamTopics();
    assert.ok(get().url.includes('/stream/topics'));
  });
});

describe('pollEvents', () => {
  test('calls /stream/topics/{topic} with offset and limit', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(200, { topic: 'transactions', count: 0, next_offset: 5, events: [] });
    const result = await c.pollEvents('transactions', { offset: 5, limit: 20 });
    assert.ok(get().url.includes('/stream/topics/transactions'));
    assert.ok(get().url.includes('offset=5'));
    assert.ok(get().url.includes('limit=20'));
    assert.equal(result.next_offset, 5);
  });
});

// ---------------------------------------------------------------------------
// Admin routes
// ---------------------------------------------------------------------------

describe('issueApiKey', () => {
  test('posts to /admin/api-keys', async () => {
    const c = new ECNClient('http://localhost:8000');
    const resp = { key_id: 'k1', key: 'secret', role: 'submitter', description: '', message: 'ok' };
    const get = captureFetch(201, resp);
    const result = await c.issueApiKey('submitter', { description: 'ci-bot' });
    const body = JSON.parse(get().init.body);
    assert.equal(body.role, 'submitter');
    assert.equal(body.description, 'ci-bot');
    assert.equal(result.key_id, 'k1');
  });
});

describe('revokeApiKey', () => {
  test('sends DELETE /admin/api-keys/{id}', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(204, null);
    await c.revokeApiKey('k1');
    assert.ok(get().url.includes('/admin/api-keys/k1'));
    assert.equal(get().init.method, 'DELETE');
  });
});

// ---------------------------------------------------------------------------
// Multi-tenant
// ---------------------------------------------------------------------------

describe('createTenant', () => {
  test('posts to /tenants', async () => {
    const c = new ECNClient('http://localhost:8000');
    const resp = { tenant_id: 't1', name: 'Bank-A', node_count: 3 };
    const get = captureFetch(201, resp);
    const result = await c.createTenant('Bank-A', { nodeCount: 3 });
    const body = JSON.parse(get().init.body);
    assert.equal(body.name, 'Bank-A');
    assert.equal(body.node_count, 3);
    assert.equal(result.tenant_id, 't1');
  });
});

describe('tenantShip', () => {
  test('posts to /tenants/{id}/transactions', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(200, ROUND_RESPONSE);
    await c.tenantShip('t1', 'ITEM-001', { destination: 'vault', shipper: 'BankCo' });
    assert.ok(get().url.includes('/tenants/t1/transactions'));
    const body = JSON.parse(get().init.body);
    assert.equal(body.type, 'ship');
    assert.equal(body.product_id, 'ITEM-001');
  });
});

describe('deleteTenant', () => {
  test('sends DELETE /tenants/{id}', async () => {
    const c = new ECNClient('http://localhost:8000');
    const get = captureFetch(204, null);
    await c.deleteTenant('t1');
    assert.ok(get().url.includes('/tenants/t1'));
    assert.equal(get().init.method, 'DELETE');
  });
});

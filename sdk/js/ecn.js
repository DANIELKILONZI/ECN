/**
 * ecn.js
 * ------
 * JavaScript SDK for the ECN — Executable Consensus Network API.
 *
 * Works in Node.js ≥ 18 (uses built-in `fetch`) and modern browsers.
 * No dependencies required.
 *
 * @example
 * import { ECNClient } from './ecn.js';
 *
 * const client = new ECNClient('http://localhost:8000');
 *
 * // Ship a product through the supply chain
 * const result = await client.ship('LAPTOP-001', { destination: 'port', shipper: 'DHL' });
 * console.log(result.consensus_reached); // true
 * console.log(result.faulty_nodes);      // []
 *
 * // Kafka-style event polling
 * const events = await client.pollEvents('transactions', { offset: 0, limit: 10 });
 *
 * // Webhook subscription
 * const sub = await client.subscribeWebhook('https://siem.example.com/hook', ['fault']);
 * console.log(sub.webhook_id);
 *
 * // Multi-tenant
 * const tenant = await client.createTenant('Bank-A', { nodeCount: 3 });
 * await client.tenantShip(tenant.tenant_id, 'ITEM-001', { destination: 'vault', shipper: 'BankCo' });
 */

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

export class ECNError extends Error {
  /**
   * @param {number} statusCode
   * @param {string} detail
   */
  constructor(statusCode, detail) {
    super(`ECN API error ${statusCode}: ${detail}`);
    this.name = 'ECNError';
    this.statusCode = statusCode;
    this.detail = detail;
  }
}

export class ECNTransactionError extends ECNError {
  constructor(statusCode, detail) {
    super(statusCode, detail);
    this.name = 'ECNTransactionError';
  }
}

export class ECNNotFoundError extends ECNError {
  constructor(statusCode, detail) {
    super(statusCode, detail);
    this.name = 'ECNNotFoundError';
  }
}

// ---------------------------------------------------------------------------
// ECNClient
// ---------------------------------------------------------------------------

export class ECNClient {
  /**
   * Create a new ECNClient.
   *
   * @param {string} [baseUrl='http://localhost:8000'] - ECN API base URL.
   * @param {object} [options={}]
   * @param {string} [options.apiKey]    - X-API-Key header for authenticated deployments.
   * @param {number} [options.timeout]   - Request timeout in milliseconds (default: 30 000).
   */
  constructor(baseUrl = 'http://localhost:8000', options = {}) {
    this._baseUrl = baseUrl.replace(/\/$/, '');
    this._apiKey = options.apiKey ?? null;
    this._timeoutMs = options.timeout ?? 30_000;
  }

  // -------------------------------------------------------------------------
  // Internal HTTP helpers
  // -------------------------------------------------------------------------

  /** @private */
  _headers(extra = {}) {
    const h = { 'Content-Type': 'application/json', ...extra };
    if (this._apiKey) h['X-API-Key'] = this._apiKey;
    return h;
  }

  /** @private */
  async _request(method, path, { body, params } = {}) {
    let url = `${this._baseUrl}${path}`;
    if (params) {
      const qs = new URLSearchParams(
        Object.fromEntries(Object.entries(params).filter(([, v]) => v != null))
      ).toString();
      if (qs) url += `?${qs}`;
    }

    const init = {
      method,
      headers: this._headers(),
      signal: AbortSignal.timeout(this._timeoutMs),
    };
    if (body !== undefined) init.body = JSON.stringify(body);

    const resp = await fetch(url, init);
    return this._handle(resp);
  }

  /** @private */
  async _handle(resp) {
    if (resp.status === 204) return null;

    let data;
    const ct = resp.headers.get('content-type') ?? '';
    if (ct.includes('application/json') && resp.headers.get('content-length') !== '0') {
      try { data = await resp.json(); } catch { data = {}; }
    } else {
      data = {};
    }

    if (resp.status === 404) throw new ECNNotFoundError(resp.status, data.detail ?? resp.statusText);
    if (resp.status === 422) throw new ECNTransactionError(resp.status, data.detail ?? resp.statusText);
    if (!resp.ok) throw new ECNError(resp.status, data.detail ?? resp.statusText);

    return data;
  }

  // -------------------------------------------------------------------------
  // Generic transaction submission
  // -------------------------------------------------------------------------

  /**
   * Submit a generic transaction to the ECN network.
   *
   * @param {string} type - Transaction type (e.g. 'ship', 'inspect').
   * @param {object} [fields={}] - Additional transaction fields.
   * @returns {Promise<object>} Consensus round result.
   * @throws {ECNTransactionError} If rejected (422).
   */
  async submit(type, fields = {}) {
    return this._request('POST', '/transactions', { body: { type, ...fields } });
  }

  // -------------------------------------------------------------------------
  // Supply-chain helpers
  // -------------------------------------------------------------------------

  /**
   * Ship a product to a destination.
   * @param {string} productId
   * @param {{destination: string, shipper: string}} opts
   */
  async ship(productId, { destination, shipper } = {}) {
    return this.submit('ship', { product_id: productId, destination, shipper });
  }

  /**
   * Accept delivery of a product.
   * @param {string} productId
   * @param {{receiver: string, atCustoms?: boolean}} opts
   */
  async receive(productId, { receiver, atCustoms = false } = {}) {
    return this.submit('receive', { product_id: productId, receiver, at_customs: atCustoms });
  }

  /**
   * Record a passed quality or compliance check.
   * @param {string} productId
   * @param {{checkName: string, inspector: string}} opts
   */
  async inspect(productId, { checkName, inspector } = {}) {
    return this.submit('inspect', { product_id: productId, check_name: checkName, inspector });
  }

  /**
   * Flag a product as quarantined.
   * @param {string} productId
   * @param {string} reason
   */
  async quarantine(productId, reason) {
    return this.submit('quarantine', { product_id: productId, reason });
  }

  /**
   * Release a quarantined product.
   * @param {string} productId
   * @param {string} releasedBy
   */
  async release(productId, releasedBy) {
    return this.submit('release', { product_id: productId, released_by: releasedBy });
  }

  // -------------------------------------------------------------------------
  // Network
  // -------------------------------------------------------------------------

  /** Return the list of verifier nodes. */
  async nodes() {
    return this._request('GET', '/network/nodes');
  }

  /** Return current consensus health of the network. */
  async networkState() {
    return this._request('GET', '/network/state');
  }

  // -------------------------------------------------------------------------
  // Audit
  // -------------------------------------------------------------------------

  /**
   * Return paginated audit events.
   * @param {{limit?: number, offset?: number}} [opts={}]
   */
  async auditEvents({ limit = 50, offset = 0 } = {}) {
    return this._request('GET', '/audit/events', { params: { limit, offset } });
  }

  /** Return only rounds where faults were detected. */
  async faultEvents() {
    return this._request('GET', '/audit/events/faults');
  }

  /**
   * Return the audit record for a specific round.
   * @param {number} roundId
   */
  async auditEvent(roundId) {
    return this._request('GET', `/audit/events/${roundId}`);
  }

  /** Return aggregate audit statistics. */
  async auditSummary() {
    return this._request('GET', '/audit/summary');
  }

  /** Return the full transaction replay log. */
  async replay() {
    return this._request('GET', '/audit/replay');
  }

  // -------------------------------------------------------------------------
  // Webhooks
  // -------------------------------------------------------------------------

  /**
   * Subscribe a URL to receive webhook notifications.
   * @param {string} url
   * @param {string[]} [events=[]] - 'transaction' and/or 'fault'; empty = all.
   */
  async subscribeWebhook(url, events = []) {
    return this._request('POST', '/webhooks', { body: { url, events } });
  }

  /** List active webhook subscriptions. */
  async listWebhooks() {
    return this._request('GET', '/webhooks');
  }

  /**
   * Remove a webhook subscription.
   * @param {string} webhookId
   */
  async unsubscribeWebhook(webhookId) {
    return this._request('DELETE', `/webhooks/${webhookId}`);
  }

  // -------------------------------------------------------------------------
  // Event Streaming (Kafka-style)
  // -------------------------------------------------------------------------

  /**
   * List available event stream topics and their current offsets.
   */
  async streamTopics() {
    return this._request('GET', '/stream/topics');
  }

  /**
   * Kafka-style offset-based event poll.
   *
   * @param {string} topic - 'transactions' or 'faults'.
   * @param {{offset?: number, limit?: number}} [opts={}]
   * @returns {Promise<{topic: string, count: number, next_offset: number, events: object[]}>}
   *
   * @example
   * let offset = 0;
   * while (true) {
   *   const batch = await client.pollEvents('transactions', { offset, limit: 20 });
   *   process(batch.events);
   *   offset = batch.next_offset;
   *   if (batch.count < 20) await sleep(1000);
   * }
   */
  async pollEvents(topic, { offset = 0, limit = 10 } = {}) {
    return this._request('GET', `/stream/topics/${topic}`, { params: { offset, limit } });
  }

  // -------------------------------------------------------------------------
  // Admin / RBAC
  // -------------------------------------------------------------------------

  /**
   * Issue a new API key.
   * @param {string} role - 'admin' | 'submitter' | 'auditor' | 'readonly'.
   * @param {{description?: string, tenantId?: string}} [opts={}]
   */
  async issueApiKey(role, { description = '', tenantId = null } = {}) {
    return this._request('POST', '/admin/api-keys', {
      body: { role, description, tenant_id: tenantId },
    });
  }

  /** List all issued API keys (secrets redacted). */
  async listApiKeys() {
    return this._request('GET', '/admin/api-keys');
  }

  /**
   * Revoke an API key.
   * @param {string} keyId
   */
  async revokeApiKey(keyId) {
    return this._request('DELETE', `/admin/api-keys/${keyId}`);
  }

  // -------------------------------------------------------------------------
  // Multi-Tenant
  // -------------------------------------------------------------------------

  /**
   * Create a new isolated tenant namespace.
   *
   * @param {string} name
   * @param {{nodeCount?: number, products?: string[]}} [opts={}]
   * @returns {Promise<{tenant_id: string, name: string, node_count: number}>}
   */
  async createTenant(name, { nodeCount = 3, products = [] } = {}) {
    return this._request('POST', '/tenants', {
      body: { name, node_count: nodeCount, products },
    });
  }

  /** List all tenant namespaces. */
  async listTenants() {
    return this._request('GET', '/tenants');
  }

  /**
   * Delete a tenant namespace.
   * @param {string} tenantId
   */
  async deleteTenant(tenantId) {
    return this._request('DELETE', `/tenants/${tenantId}`);
  }

  /**
   * Submit a transaction within a tenant namespace.
   * @param {string} tenantId
   * @param {string} type
   * @param {object} [fields={}]
   */
  async tenantSubmit(tenantId, type, fields = {}) {
    return this._request('POST', `/tenants/${tenantId}/transactions`, {
      body: { type, ...fields },
    });
  }

  /**
   * Ship a product within a tenant namespace.
   * @param {string} tenantId
   * @param {string} productId
   * @param {{destination: string, shipper: string}} opts
   */
  async tenantShip(tenantId, productId, { destination, shipper } = {}) {
    return this.tenantSubmit(tenantId, 'ship', {
      product_id: productId,
      destination,
      shipper,
    });
  }

  /**
   * Return the tenant's network health.
   * @param {string} tenantId
   */
  async tenantNetworkState(tenantId) {
    return this._request('GET', `/tenants/${tenantId}/network/state`);
  }

  /**
   * Return paginated audit events for a tenant.
   * @param {string} tenantId
   * @param {{limit?: number, offset?: number}} [opts={}]
   */
  async tenantAuditEvents(tenantId, { limit = 50, offset = 0 } = {}) {
    return this._request('GET', `/tenants/${tenantId}/audit/events`, {
      params: { limit, offset },
    });
  }

  /**
   * Return aggregate audit statistics for a tenant.
   * @param {string} tenantId
   */
  async tenantAuditSummary(tenantId) {
    return this._request('GET', `/tenants/${tenantId}/audit/summary`);
  }
}

export default ECNClient;

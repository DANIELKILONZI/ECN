package ecnclient;

import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/**
 * ECNClient — Java SDK for the ECN Executable Consensus Network API.
 *
 * <p>Requires Java 11 or later.  No external dependencies.
 *
 * <h2>Quick start</h2>
 * <pre>{@code
 * ECNClient client = new ECNClient("http://localhost:8000");
 *
 * // Ship a product
 * String result = client.ship("LAPTOP-001", "port", "DHL");
 * System.out.println(result); // JSON string
 *
 * // Audit
 * String summary = client.auditSummary();
 *
 * // Kafka-style event polling
 * int offset = 0;
 * while (true) {
 *     String events = client.pollEvents("transactions", offset, 20);
 *     // parse JSON, advance offset from next_offset field...
 *     Thread.sleep(1_000);
 * }
 *
 * // Multi-tenant
 * String tenant = client.createTenant("Bank-A", 3, List.of());
 * String txResult = client.tenantShip(tenantId, "ITEM-001", "vault", "BankCo");
 * }</pre>
 *
 * <p>All methods return the raw JSON string from the API.  Parse with your
 * preferred JSON library (Jackson, Gson, etc.).
 *
 * <h2>Authentication</h2>
 * Set {@code ECN_AUTH_ENABLED=1} on the server and pass your API key:
 * <pre>{@code
 * ECNClient client = ECNClient.builder("http://localhost:8000")
 *     .apiKey("your-secret-key")
 *     .timeout(Duration.ofSeconds(10))
 *     .build();
 * }</pre>
 */
public final class ECNClient {

    // -----------------------------------------------------------------------
    // Errors
    // -----------------------------------------------------------------------

    /** Thrown when the ECN API returns an error response. */
    public static class ECNException extends RuntimeException {
        private final int statusCode;
        private final String detail;

        public ECNException(int statusCode, String detail) {
            super("ECN API error " + statusCode + ": " + detail);
            this.statusCode = statusCode;
            this.detail = detail;
        }

        /** HTTP status code returned by the API. */
        public int getStatusCode() { return statusCode; }
        /** Error detail message from the API response body. */
        public String getDetail() { return detail; }
    }

    /** Thrown when a transaction is rejected (HTTP 422). */
    public static final class TransactionException extends ECNException {
        public TransactionException(String detail) { super(422, detail); }
    }

    /** Thrown when a resource is not found (HTTP 404). */
    public static final class NotFoundException extends ECNException {
        public NotFoundException(String detail) { super(404, detail); }
    }

    // -----------------------------------------------------------------------
    // Builder
    // -----------------------------------------------------------------------

    /** Returns a new {@link Builder} for the given base URL. */
    public static Builder builder(String baseUrl) {
        return new Builder(baseUrl);
    }

    /** Fluent builder for {@link ECNClient}. */
    public static final class Builder {
        private final String baseUrl;
        private String apiKey = null;
        private Duration timeout = Duration.ofSeconds(30);

        private Builder(String baseUrl) {
            this.baseUrl = Objects.requireNonNull(baseUrl).replaceAll("/+$", "");
        }

        public Builder apiKey(String key) { this.apiKey = key; return this; }
        public Builder timeout(Duration d) { this.timeout = d; return this; }

        public ECNClient build() {
            return new ECNClient(baseUrl, apiKey, timeout);
        }
    }

    // -----------------------------------------------------------------------
    // Fields
    // -----------------------------------------------------------------------

    private final String baseUrl;
    private final String apiKey;
    private final HttpClient http;

    /**
     * Create an ECNClient with default settings (30s timeout, no auth).
     *
     * @param baseUrl Base URL of the ECN API server (e.g. {@code "http://localhost:8000"}).
     */
    public ECNClient(String baseUrl) {
        this(baseUrl, null, Duration.ofSeconds(30));
    }

    private ECNClient(String baseUrl, String apiKey, Duration timeout) {
        this.baseUrl = baseUrl.replaceAll("/+$", "");
        this.apiKey = apiKey;
        this.http = HttpClient.newBuilder()
            .connectTimeout(timeout)
            .build();
    }

    // -----------------------------------------------------------------------
    // Internal HTTP helpers
    // -----------------------------------------------------------------------

    private HttpRequest.Builder requestBuilder(String path) {
        var builder = HttpRequest.newBuilder()
            .uri(URI.create(baseUrl + path))
            .header("Content-Type", "application/json")
            .header("Accept", "application/json");
        if (apiKey != null && !apiKey.isEmpty()) {
            builder.header("X-API-Key", apiKey);
        }
        return builder;
    }

    private String get(String path) {
        var req = requestBuilder(path).GET().build();
        return execute(req);
    }

    private String post(String path, String jsonBody) {
        var req = requestBuilder(path)
            .POST(HttpRequest.BodyPublishers.ofString(jsonBody))
            .build();
        return execute(req);
    }

    private String delete(String path) {
        var req = requestBuilder(path).DELETE().build();
        return execute(req);
    }

    private String execute(HttpRequest req) {
        try {
            var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
            if (resp.statusCode() == 204) return null;
            if (resp.statusCode() == 404) throw new NotFoundException(resp.body());
            if (resp.statusCode() == 422) throw new TransactionException(resp.body());
            if (resp.statusCode() < 200 || resp.statusCode() >= 300) {
                throw new ECNException(resp.statusCode(), resp.body());
            }
            return resp.body();
        } catch (ECNException e) {
            throw e;
        } catch (IOException | InterruptedException e) {
            throw new RuntimeException("ECN request failed: " + e.getMessage(), e);
        }
    }

    /** Simple JSON body builder without external libraries. */
    private static String json(Object... keysAndValues) {
        if (keysAndValues.length % 2 != 0) throw new IllegalArgumentException("Must have even number of args");
        var sb = new StringBuilder("{");
        for (int i = 0; i < keysAndValues.length; i += 2) {
            if (i > 0) sb.append(",");
            sb.append("\"").append(keysAndValues[i]).append("\":");
            Object v = keysAndValues[i + 1];
            if (v == null)          sb.append("null");
            else if (v instanceof Boolean) sb.append(v);
            else if (v instanceof Number)  sb.append(v);
            else if (v instanceof List)    sb.append(listToJson((List<?>) v));
            else                    sb.append("\"").append(escape(v.toString())).append("\"");
        }
        sb.append("}");
        return sb.toString();
    }

    private static String listToJson(List<?> list) {
        var sb = new StringBuilder("[");
        for (int i = 0; i < list.size(); i++) {
            if (i > 0) sb.append(",");
            Object v = list.get(i);
            if (v == null) sb.append("null");
            else           sb.append("\"").append(escape(v.toString())).append("\"");
        }
        sb.append("]");
        return sb.toString();
    }

    private static String escape(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    private static String qs(Object... keysAndValues) {
        var sb = new StringBuilder();
        for (int i = 0; i < keysAndValues.length; i += 2) {
            if (keysAndValues[i + 1] == null) continue;
            if (sb.length() > 0) sb.append("&");
            sb.append(URLEncoder.encode(keysAndValues[i].toString(), StandardCharsets.UTF_8))
              .append("=")
              .append(URLEncoder.encode(keysAndValues[i + 1].toString(), StandardCharsets.UTF_8));
        }
        return sb.length() > 0 ? "?" + sb : "";
    }

    // -----------------------------------------------------------------------
    // Generic transaction
    // -----------------------------------------------------------------------

    /**
     * Submit a generic transaction to the ECN network.
     *
     * @param type   Transaction type (e.g. {@code "ship"}).
     * @param fields Additional fields as a JSON string suffix (may be empty {@code ""}).
     * @return JSON response string (consensus round result).
     * @throws TransactionException if the transaction is rejected.
     */
    public String submit(String type, String fieldsJson) {
        String body = fieldsJson == null || fieldsJson.isBlank()
            ? json("type", type)
            : "{\"type\":\"" + escape(type) + "\"," + fieldsJson.substring(1);
        return post("/transactions", body);
    }

    // -----------------------------------------------------------------------
    // Supply-chain helpers
    // -----------------------------------------------------------------------

    /**
     * Ship a product to a destination.
     * @return JSON consensus round result.
     */
    public String ship(String productId, String destination, String shipper) {
        return post("/transactions", json(
            "type", "ship",
            "product_id", productId,
            "destination", destination,
            "shipper", shipper
        ));
    }

    /**
     * Accept delivery of a product.
     * @return JSON consensus round result.
     */
    public String receive(String productId, String receiver, boolean atCustoms) {
        return post("/transactions", json(
            "type", "receive",
            "product_id", productId,
            "receiver", receiver,
            "at_customs", atCustoms
        ));
    }

    /**
     * Record a passed quality or compliance check.
     * @return JSON consensus round result.
     */
    public String inspect(String productId, String checkName, String inspector) {
        return post("/transactions", json(
            "type", "inspect",
            "product_id", productId,
            "check_name", checkName,
            "inspector", inspector
        ));
    }

    /**
     * Flag a product as quarantined.
     * @return JSON consensus round result.
     */
    public String quarantine(String productId, String reason) {
        return post("/transactions", json(
            "type", "quarantine",
            "product_id", productId,
            "reason", reason
        ));
    }

    /**
     * Release a quarantined product.
     * @return JSON consensus round result.
     */
    public String release(String productId, String releasedBy) {
        return post("/transactions", json(
            "type", "release",
            "product_id", productId,
            "released_by", releasedBy
        ));
    }

    // -----------------------------------------------------------------------
    // Network
    // -----------------------------------------------------------------------

    /** Return the list of verifier nodes as a JSON array. */
    public String nodes() { return get("/network/nodes"); }

    /** Return current consensus health as a JSON object. */
    public String networkState() { return get("/network/state"); }

    // -----------------------------------------------------------------------
    // Audit
    // -----------------------------------------------------------------------

    /**
     * Return paginated audit events.
     * @param limit  Max events to return (1–500).
     * @param offset Number of events to skip.
     * @return JSON object with {@code total}, {@code offset}, {@code events}.
     */
    public String auditEvents(int limit, int offset) {
        return get("/audit/events" + qs("limit", limit, "offset", offset));
    }

    /** Return only rounds where faults were detected. */
    public String faultEvents() { return get("/audit/events/faults"); }

    /**
     * Return the audit record for a specific round (1-based).
     * @throws NotFoundException if the round does not exist.
     */
    public String auditEvent(int roundId) {
        return get("/audit/events/" + roundId);
    }

    /** Return aggregate audit statistics. */
    public String auditSummary() { return get("/audit/summary"); }

    /** Return the full transaction replay log. */
    public String replay() { return get("/audit/replay"); }

    // -----------------------------------------------------------------------
    // Webhooks
    // -----------------------------------------------------------------------

    /**
     * Subscribe a URL to receive POST notifications after each consensus round.
     *
     * @param url    Target URL.
     * @param events Filter list ({@code "transaction"} and/or {@code "fault"}).
     *               Pass an empty list to receive all events.
     */
    public String subscribeWebhook(String url, List<String> events) {
        return post("/webhooks", json("url", url, "events", events == null ? List.of() : events));
    }

    /** List active webhook subscriptions. */
    public String listWebhooks() { return get("/webhooks"); }

    /**
     * Remove a webhook subscription.
     * @param webhookId The ID returned by {@link #subscribeWebhook}.
     * @throws NotFoundException if not found.
     */
    public String unsubscribeWebhook(String webhookId) {
        return delete("/webhooks/" + webhookId);
    }

    // -----------------------------------------------------------------------
    // Event streaming (Kafka-style)
    // -----------------------------------------------------------------------

    /** List available event stream topics and their current offsets. */
    public String streamTopics() { return get("/stream/topics"); }

    /**
     * Kafka-style offset-based event poll.
     *
     * <pre>{@code
     * int offset = 0;
     * while (true) {
     *     String resp = client.pollEvents("transactions", offset, 20);
     *     // parse JSON, get next_offset value
     *     offset = parseNextOffset(resp);
     *     if (parseCount(resp) < 20) Thread.sleep(1_000);
     * }
     * }</pre>
     *
     * @param topic  {@code "transactions"} or {@code "faults"}.
     * @param offset Absolute offset to start from (0-based).
     * @param limit  Max events to return.
     */
    public String pollEvents(String topic, int offset, int limit) {
        return get("/stream/topics/" + topic + qs("offset", offset, "limit", limit));
    }

    // -----------------------------------------------------------------------
    // Admin / RBAC
    // -----------------------------------------------------------------------

    /**
     * Issue a new API key.
     *
     * @param role        One of {@code admin}, {@code submitter}, {@code auditor}, {@code readonly}.
     * @param description Human-readable label for this key.
     * @param tenantId    Optional tenant scope ({@code null} for global).
     * @return JSON object containing the secret {@code key} (shown once).
     */
    public String issueApiKey(String role, String description, String tenantId) {
        return post("/admin/api-keys", json(
            "role", role,
            "description", description == null ? "" : description,
            "tenant_id", tenantId
        ));
    }

    /** List all API keys (secrets redacted). */
    public String listApiKeys() { return get("/admin/api-keys"); }

    /**
     * Revoke an API key.
     * @param keyId The {@code key_id} of the key to revoke.
     * @throws NotFoundException if not found.
     */
    public String revokeApiKey(String keyId) {
        return delete("/admin/api-keys/" + keyId);
    }

    // -----------------------------------------------------------------------
    // Multi-tenant
    // -----------------------------------------------------------------------

    /**
     * Create a new isolated tenant namespace.
     *
     * @param name      Human-readable tenant name.
     * @param nodeCount Number of validator nodes (1–10).
     * @param products  Initial product IDs; pass {@code List.of()} for defaults.
     * @return JSON object with {@code tenant_id}, {@code name}, {@code node_count}.
     */
    public String createTenant(String name, int nodeCount, List<String> products) {
        return post("/tenants", json(
            "name", name,
            "node_count", nodeCount,
            "products", products == null ? List.of() : products
        ));
    }

    /** List all tenant namespaces. */
    public String listTenants() { return get("/tenants"); }

    /**
     * Delete a tenant namespace and shut down its nodes.
     * @param tenantId Tenant ID.
     * @throws NotFoundException if not found.
     */
    public String deleteTenant(String tenantId) {
        return delete("/tenants/" + tenantId);
    }

    /**
     * Submit a transaction within a tenant namespace.
     *
     * @param tenantId  Target tenant ID.
     * @param type      Transaction type.
     * @param fieldsJson Additional fields as a JSON string suffix (may be empty).
     */
    public String tenantSubmit(String tenantId, String type, String fieldsJson) {
        String body = fieldsJson == null || fieldsJson.isBlank()
            ? json("type", type)
            : "{\"type\":\"" + escape(type) + "\"," + fieldsJson.substring(1);
        return post("/tenants/" + tenantId + "/transactions", body);
    }

    /**
     * Ship a product within a tenant namespace.
     * @return JSON consensus round result.
     */
    public String tenantShip(String tenantId, String productId, String destination, String shipper) {
        return post("/tenants/" + tenantId + "/transactions", json(
            "type", "ship",
            "product_id", productId,
            "destination", destination,
            "shipper", shipper
        ));
    }

    /** Return the tenant's network health. */
    public String tenantNetworkState(String tenantId) {
        return get("/tenants/" + tenantId + "/network/state");
    }

    /**
     * Return paginated audit events for a tenant.
     * @param limit  Max events (1–500).
     * @param offset Events to skip.
     */
    public String tenantAuditEvents(String tenantId, int limit, int offset) {
        return get("/tenants/" + tenantId + "/audit/events" + qs("limit", limit, "offset", offset));
    }

    /** Return aggregate audit statistics for a tenant. */
    public String tenantAuditSummary(String tenantId) {
        return get("/tenants/" + tenantId + "/audit/summary");
    }
}

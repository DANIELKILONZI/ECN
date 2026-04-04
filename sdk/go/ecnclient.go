// Package ecnclient provides a Go SDK for the ECN — Executable Consensus Network API.
//
// Usage:
//
//	client := ecnclient.New("http://localhost:8000", nil)
//
//	// Supply-chain transactions
//	result, err := client.Ship("LAPTOP-001", "port", "DHL")
//	if err != nil { log.Fatal(err) }
//	fmt.Println(result.ConsensusReached)
//
//	// Audit
//	summary, err := client.AuditSummary()
//	fmt.Printf("Fault rate: %.1f%%\n", summary.FaultRate*100)
//
//	// Kafka-style event polling
//	var offset int
//	for {
//	    batch, _ := client.PollEvents("transactions", offset, 20)
//	    process(batch.Events)
//	    offset = batch.NextOffset
//	    if batch.Count < 20 { time.Sleep(time.Second) }
//	}
//
//	// Multi-tenant
//	tenant, _ := client.CreateTenant("Bank-A", 3, nil)
//	result, _  := client.TenantShip(tenant.TenantID, "ITEM-001", "vault", "BankCo")
package ecnclient

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

// ECNError is returned when the ECN API responds with an error status code.
type ECNError struct {
	StatusCode int
	Detail     string
}

func (e *ECNError) Error() string {
	return fmt.Sprintf("ECN API error %d: %s", e.StatusCode, e.Detail)
}

// ECNTransactionError is returned when a transaction is rejected (HTTP 422).
type ECNTransactionError struct{ ECNError }

// ECNNotFoundError is returned when a resource is not found (HTTP 404).
type ECNNotFoundError struct{ ECNError }

// ---------------------------------------------------------------------------
// Client options
// ---------------------------------------------------------------------------

// Options configures the ECNClient.
type Options struct {
	// APIKey sets the X-API-Key header for authenticated deployments.
	APIKey string
	// Timeout is the HTTP client timeout (default: 30s).
	Timeout time.Duration
	// HTTPClient allows injecting a custom *http.Client (e.g. in tests).
	HTTPClient *http.Client
}

// ---------------------------------------------------------------------------
// Response types
// ---------------------------------------------------------------------------

// NodeVote is a single node's vote in a consensus round.
type NodeVote struct {
	NodeID    string  `json:"node_id"`
	StateHash string  `json:"state_hash"`
	Signature *string `json:"signature"`
	Status    string  `json:"status"`
}

// RoundResult is the consensus outcome for a single transaction.
type RoundResult struct {
	RoundID          int        `json:"round_id"`
	Timestamp        string     `json:"timestamp"`
	Transaction      any        `json:"transaction"`
	ConsensusReached bool       `json:"consensus_reached"`
	AgreedHash       *string    `json:"agreed_hash"`
	HonestNodes      []string   `json:"honest_nodes"`
	FaultyNodes      []string   `json:"faulty_nodes"`
	InvalidSigNodes  []string   `json:"invalid_sig_nodes"`
	Votes            []NodeVote `json:"votes"`
}

// NodeInfo describes a single verifier node.
type NodeInfo struct {
	NodeID       string `json:"node_id"`
	Port         int    `json:"port"`
	PublicKeyHex string `json:"public_key_hex"`
	Malicious    bool   `json:"malicious"`
}

// NetworkState summarises the network's consensus health.
type NetworkState struct {
	NodeCount               int        `json:"node_count"`
	Nodes                   []NodeInfo `json:"nodes"`
	TotalRounds             int        `json:"total_rounds"`
	FaultRounds             int        `json:"fault_rounds"`
	FaultRate               float64    `json:"fault_rate"`
	ConsensusFailureRounds  int        `json:"consensus_failure_rounds"`
}

// AuditEventsResponse wraps a paginated list of audit events.
type AuditEventsResponse struct {
	Total  int           `json:"total"`
	Offset int           `json:"offset"`
	Events []RoundResult `json:"events"`
}

// AuditSummary holds aggregate statistics across all recorded rounds.
type AuditSummary struct {
	TotalRounds            int       `json:"total_rounds"`
	FaultRounds            int       `json:"fault_rounds"`
	ConsensusFailureRounds int       `json:"consensus_failure_rounds"`
	FaultRate              float64   `json:"fault_rate"`
	TopFaultyNodes         [][]any   `json:"top_faulty_nodes"`
}

// TopicInfo holds metadata for one event stream topic.
type TopicInfo struct {
	Topic          string `json:"topic"`
	BufferedEvents int    `json:"buffered_events"`
	NextOffset     int    `json:"next_offset"`
}

// StreamTopicsResponse wraps the list of event stream topics.
type StreamTopicsResponse struct {
	Topics []TopicInfo `json:"topics"`
}

// StreamEvent is a single buffered event returned by a poll request.
type StreamEvent struct {
	Seq     int            `json:"seq"`
	Topic   string         `json:"topic"`
	Payload map[string]any `json:"payload"`
}

// PollEventsResponse is returned by a Kafka-style poll request.
type PollEventsResponse struct {
	Topic      string        `json:"topic"`
	Offset     int           `json:"offset"`
	Count      int           `json:"count"`
	NextOffset int           `json:"next_offset"`
	Events     []StreamEvent `json:"events"`
}

// WebhookInfo describes a webhook subscription.
type WebhookInfo struct {
	WebhookID string   `json:"webhook_id"`
	URL       string   `json:"url"`
	Events    []string `json:"events"`
}

// WebhookSubscribeResponse is returned when a webhook is registered.
type WebhookSubscribeResponse struct {
	WebhookID string   `json:"webhook_id"`
	URL       string   `json:"url"`
	Events    []string `json:"events"`
	Message   string   `json:"message"`
}

// APIKeyInfo describes an issued API key (secret redacted).
type APIKeyInfo struct {
	KeyID       string  `json:"key_id"`
	Role        string  `json:"role"`
	Description string  `json:"description"`
	TenantID    *string `json:"tenant_id"`
}

// APIKeyResponse is returned when a new API key is issued.
type APIKeyResponse struct {
	KeyID       string  `json:"key_id"`
	Key         string  `json:"key"`
	Role        string  `json:"role"`
	Description string  `json:"description"`
	TenantID    *string `json:"tenant_id"`
	Message     string  `json:"message"`
}

// TenantInfo describes a tenant namespace.
type TenantInfo struct {
	TenantID  string `json:"tenant_id"`
	Name      string `json:"name"`
	NodeCount int    `json:"node_count"`
}

// TenantNetworkState describes a tenant's network health.
type TenantNetworkState struct {
	TenantID    string     `json:"tenant_id"`
	NodeCount   int        `json:"node_count"`
	Nodes       []NodeInfo `json:"nodes"`
	TotalRounds int        `json:"total_rounds"`
	FaultRounds int        `json:"fault_rounds"`
	FaultRate   float64    `json:"fault_rate"`
}

// TenantAuditEventsResponse wraps paginated tenant audit events.
type TenantAuditEventsResponse struct {
	TenantID string        `json:"tenant_id"`
	Total    int           `json:"total"`
	Offset   int           `json:"offset"`
	Events   []RoundResult `json:"events"`
}

// TenantAuditSummary holds aggregate audit statistics for a tenant.
type TenantAuditSummary struct {
	TenantID               string    `json:"tenant_id"`
	TotalRounds            int       `json:"total_rounds"`
	FaultRounds            int       `json:"fault_rounds"`
	ConsensusFailureRounds int       `json:"consensus_failure_rounds"`
	FaultRate              float64   `json:"fault_rate"`
	TopFaultyNodes         [][]any   `json:"top_faulty_nodes"`
}

// ---------------------------------------------------------------------------
// ECNClient
// ---------------------------------------------------------------------------

// ECNClient is the Go client for the ECN REST API.
type ECNClient struct {
	baseURL    string
	apiKey     string
	httpClient *http.Client
}

// New creates a new ECNClient.
//
// baseURL is the ECN API server (e.g. "http://localhost:8000").
// opts may be nil to use defaults.
func New(baseURL string, opts *Options) *ECNClient {
	baseURL = strings.TrimRight(baseURL, "/")
	timeout := 30 * time.Second
	var httpClient *http.Client
	var apiKey string

	if opts != nil {
		if opts.Timeout > 0 {
			timeout = opts.Timeout
		}
		if opts.HTTPClient != nil {
			httpClient = opts.HTTPClient
		}
		apiKey = opts.APIKey
	}
	if httpClient == nil {
		httpClient = &http.Client{Timeout: timeout}
	}

	return &ECNClient{
		baseURL:    baseURL,
		apiKey:     apiKey,
		httpClient: httpClient,
	}
}

// ---------------------------------------------------------------------------
// Internal HTTP helpers
// ---------------------------------------------------------------------------

func (c *ECNClient) do(method, path string, body any, params url.Values) (*http.Response, error) {
	u := c.baseURL + path
	if len(params) > 0 {
		u += "?" + params.Encode()
	}

	var bodyReader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		bodyReader = bytes.NewReader(b)
	}

	req, err := http.NewRequest(method, u, bodyReader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.apiKey != "" {
		req.Header.Set("X-API-Key", c.apiKey)
	}

	return c.httpClient.Do(req)
}

func decodeResponse[T any](resp *http.Response) (T, error) {
	var zero T
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNoContent {
		return zero, nil
	}

	var result T
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return zero, err
	}

	if resp.StatusCode == http.StatusNotFound {
		// Attempt to get detail from a map decode
		return zero, &ECNNotFoundError{ECNError{StatusCode: resp.StatusCode, Detail: fmt.Sprintf("%v", result)}}
	}
	if resp.StatusCode == http.StatusUnprocessableEntity {
		return zero, &ECNTransactionError{ECNError{StatusCode: resp.StatusCode, Detail: fmt.Sprintf("%v", result)}}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return zero, &ECNError{StatusCode: resp.StatusCode, Detail: fmt.Sprintf("%v", result)}
	}
	return result, nil
}

func checkStatus(resp *http.Response) error {
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNoContent {
		return nil
	}
	if resp.StatusCode == http.StatusNotFound {
		var e map[string]any
		json.NewDecoder(resp.Body).Decode(&e) //nolint:errcheck
		return &ECNNotFoundError{ECNError{StatusCode: resp.StatusCode, Detail: fmt.Sprintf("%v", e["detail"])}}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		var e map[string]any
		json.NewDecoder(resp.Body).Decode(&e) //nolint:errcheck
		return &ECNError{StatusCode: resp.StatusCode, Detail: fmt.Sprintf("%v", e["detail"])}
	}
	return nil
}

// ---------------------------------------------------------------------------
// Generic transaction
// ---------------------------------------------------------------------------

// Submit broadcasts a generic transaction to the ECN network.
func (c *ECNClient) Submit(txType string, fields map[string]any) (*RoundResult, error) {
	body := map[string]any{"type": txType}
	for k, v := range fields {
		body[k] = v
	}
	resp, err := c.do(http.MethodPost, "/transactions", body, nil)
	if err != nil {
		return nil, err
	}
	return decodeResponse[*RoundResult](resp)
}

// ---------------------------------------------------------------------------
// Supply-chain helpers
// ---------------------------------------------------------------------------

// Ship moves a product to a destination.
func (c *ECNClient) Ship(productID, destination, shipper string) (*RoundResult, error) {
	return c.Submit("ship", map[string]any{
		"product_id":  productID,
		"destination": destination,
		"shipper":     shipper,
	})
}

// Receive accepts delivery of a product.
func (c *ECNClient) Receive(productID, receiver string, atCustoms bool) (*RoundResult, error) {
	return c.Submit("receive", map[string]any{
		"product_id": productID,
		"receiver":   receiver,
		"at_customs": atCustoms,
	})
}

// Inspect records a passed quality or compliance check.
func (c *ECNClient) Inspect(productID, checkName, inspector string) (*RoundResult, error) {
	return c.Submit("inspect", map[string]any{
		"product_id": productID,
		"check_name": checkName,
		"inspector":  inspector,
	})
}

// Quarantine flags a product as quarantined.
func (c *ECNClient) Quarantine(productID, reason string) (*RoundResult, error) {
	return c.Submit("quarantine", map[string]any{
		"product_id": productID,
		"reason":     reason,
	})
}

// Release releases a quarantined product.
func (c *ECNClient) Release(productID, releasedBy string) (*RoundResult, error) {
	return c.Submit("release", map[string]any{
		"product_id":  productID,
		"released_by": releasedBy,
	})
}

// ---------------------------------------------------------------------------
// Network
// ---------------------------------------------------------------------------

// Nodes returns the list of verifier nodes.
func (c *ECNClient) Nodes() ([]NodeInfo, error) {
	resp, err := c.do(http.MethodGet, "/network/nodes", nil, nil)
	if err != nil {
		return nil, err
	}
	return decodeResponse[[]NodeInfo](resp)
}

// NetworkState returns the current consensus health.
func (c *ECNClient) NetworkState() (*NetworkState, error) {
	resp, err := c.do(http.MethodGet, "/network/state", nil, nil)
	if err != nil {
		return nil, err
	}
	return decodeResponse[*NetworkState](resp)
}

// ---------------------------------------------------------------------------
// Audit
// ---------------------------------------------------------------------------

// AuditEvents returns a paginated list of audit events.
func (c *ECNClient) AuditEvents(limit, offset int) (*AuditEventsResponse, error) {
	p := url.Values{}
	p.Set("limit", strconv.Itoa(limit))
	p.Set("offset", strconv.Itoa(offset))
	resp, err := c.do(http.MethodGet, "/audit/events", nil, p)
	if err != nil {
		return nil, err
	}
	return decodeResponse[*AuditEventsResponse](resp)
}

// FaultEvents returns only rounds where faults were detected.
func (c *ECNClient) FaultEvents() (*AuditEventsResponse, error) {
	resp, err := c.do(http.MethodGet, "/audit/events/faults", nil, nil)
	if err != nil {
		return nil, err
	}
	return decodeResponse[*AuditEventsResponse](resp)
}

// AuditEvent returns the audit record for a specific round (1-based).
func (c *ECNClient) AuditEvent(roundID int) (*RoundResult, error) {
	resp, err := c.do(http.MethodGet, fmt.Sprintf("/audit/events/%d", roundID), nil, nil)
	if err != nil {
		return nil, err
	}
	return decodeResponse[*RoundResult](resp)
}

// AuditSummary returns aggregate audit statistics.
func (c *ECNClient) AuditSummary() (*AuditSummary, error) {
	resp, err := c.do(http.MethodGet, "/audit/summary", nil, nil)
	if err != nil {
		return nil, err
	}
	return decodeResponse[*AuditSummary](resp)
}

// ---------------------------------------------------------------------------
// Webhooks
// ---------------------------------------------------------------------------

// SubscribeWebhook registers a URL to receive POST notifications.
// events may be nil or empty to receive all events.
func (c *ECNClient) SubscribeWebhook(webhookURL string, events []string) (*WebhookSubscribeResponse, error) {
	if events == nil {
		events = []string{}
	}
	resp, err := c.do(http.MethodPost, "/webhooks", map[string]any{
		"url": webhookURL, "events": events,
	}, nil)
	if err != nil {
		return nil, err
	}
	return decodeResponse[*WebhookSubscribeResponse](resp)
}

// ListWebhooks returns all active webhook subscriptions.
func (c *ECNClient) ListWebhooks() ([]WebhookInfo, error) {
	resp, err := c.do(http.MethodGet, "/webhooks", nil, nil)
	if err != nil {
		return nil, err
	}
	return decodeResponse[[]WebhookInfo](resp)
}

// UnsubscribeWebhook removes a webhook subscription.
func (c *ECNClient) UnsubscribeWebhook(webhookID string) error {
	resp, err := c.do(http.MethodDelete, "/webhooks/"+webhookID, nil, nil)
	if err != nil {
		return err
	}
	return checkStatus(resp)
}

// ---------------------------------------------------------------------------
// Event streaming (Kafka-style)
// ---------------------------------------------------------------------------

// StreamTopics returns metadata for all event stream topics.
func (c *ECNClient) StreamTopics() (*StreamTopicsResponse, error) {
	resp, err := c.do(http.MethodGet, "/stream/topics", nil, nil)
	if err != nil {
		return nil, err
	}
	return decodeResponse[*StreamTopicsResponse](resp)
}

// PollEvents returns buffered events from topic starting at offset.
// This is the Kafka-style consumer pattern: track NextOffset across calls.
func (c *ECNClient) PollEvents(topic string, offset, limit int) (*PollEventsResponse, error) {
	p := url.Values{}
	p.Set("offset", strconv.Itoa(offset))
	p.Set("limit", strconv.Itoa(limit))
	resp, err := c.do(http.MethodGet, "/stream/topics/"+topic, nil, p)
	if err != nil {
		return nil, err
	}
	return decodeResponse[*PollEventsResponse](resp)
}

// ---------------------------------------------------------------------------
// Admin / RBAC
// ---------------------------------------------------------------------------

// IssueAPIKey creates a new API key with the given role.
func (c *ECNClient) IssueAPIKey(role, description string, tenantID *string) (*APIKeyResponse, error) {
	body := map[string]any{
		"role":        role,
		"description": description,
		"tenant_id":   tenantID,
	}
	resp, err := c.do(http.MethodPost, "/admin/api-keys", body, nil)
	if err != nil {
		return nil, err
	}
	return decodeResponse[*APIKeyResponse](resp)
}

// ListAPIKeys returns all issued API keys (secrets redacted).
func (c *ECNClient) ListAPIKeys() ([]APIKeyInfo, error) {
	resp, err := c.do(http.MethodGet, "/admin/api-keys", nil, nil)
	if err != nil {
		return nil, err
	}
	var result struct {
		Keys []APIKeyInfo `json:"keys"`
	}
	r, err := decodeResponse[*struct {
		Keys []APIKeyInfo `json:"keys"`
	}](resp)
	if err != nil {
		return nil, err
	}
	if r != nil {
		result = *r
	}
	return result.Keys, nil
}

// RevokeAPIKey revokes an API key by its ID.
func (c *ECNClient) RevokeAPIKey(keyID string) error {
	resp, err := c.do(http.MethodDelete, "/admin/api-keys/"+keyID, nil, nil)
	if err != nil {
		return err
	}
	return checkStatus(resp)
}

// ---------------------------------------------------------------------------
// Multi-tenant
// ---------------------------------------------------------------------------

// CreateTenant creates a new isolated tenant namespace.
// products may be nil to use defaults.
func (c *ECNClient) CreateTenant(name string, nodeCount int, products []string) (*TenantInfo, error) {
	if products == nil {
		products = []string{}
	}
	resp, err := c.do(http.MethodPost, "/tenants", map[string]any{
		"name":       name,
		"node_count": nodeCount,
		"products":   products,
	}, nil)
	if err != nil {
		return nil, err
	}
	return decodeResponse[*TenantInfo](resp)
}

// ListTenants returns all active tenant namespaces.
func (c *ECNClient) ListTenants() ([]TenantInfo, error) {
	resp, err := c.do(http.MethodGet, "/tenants", nil, nil)
	if err != nil {
		return nil, err
	}
	return decodeResponse[[]TenantInfo](resp)
}

// DeleteTenant shuts down a tenant's network and removes it.
func (c *ECNClient) DeleteTenant(tenantID string) error {
	resp, err := c.do(http.MethodDelete, "/tenants/"+tenantID, nil, nil)
	if err != nil {
		return err
	}
	return checkStatus(resp)
}

// TenantSubmit submits a transaction within a tenant namespace.
func (c *ECNClient) TenantSubmit(tenantID, txType string, fields map[string]any) (*RoundResult, error) {
	body := map[string]any{"type": txType}
	for k, v := range fields {
		body[k] = v
	}
	resp, err := c.do(http.MethodPost, "/tenants/"+tenantID+"/transactions", body, nil)
	if err != nil {
		return nil, err
	}
	return decodeResponse[*RoundResult](resp)
}

// TenantShip ships a product within a tenant namespace.
func (c *ECNClient) TenantShip(tenantID, productID, destination, shipper string) (*RoundResult, error) {
	return c.TenantSubmit(tenantID, "ship", map[string]any{
		"product_id":  productID,
		"destination": destination,
		"shipper":     shipper,
	})
}

// TenantNetworkState returns the tenant's network health.
func (c *ECNClient) TenantNetworkState(tenantID string) (*TenantNetworkState, error) {
	resp, err := c.do(http.MethodGet, "/tenants/"+tenantID+"/network/state", nil, nil)
	if err != nil {
		return nil, err
	}
	return decodeResponse[*TenantNetworkState](resp)
}

// TenantAuditEvents returns paginated audit events for a tenant.
func (c *ECNClient) TenantAuditEvents(tenantID string, limit, offset int) (*TenantAuditEventsResponse, error) {
	p := url.Values{}
	p.Set("limit", strconv.Itoa(limit))
	p.Set("offset", strconv.Itoa(offset))
	resp, err := c.do(http.MethodGet, "/tenants/"+tenantID+"/audit/events", nil, p)
	if err != nil {
		return nil, err
	}
	return decodeResponse[*TenantAuditEventsResponse](resp)
}

// TenantAuditSummary returns aggregate audit statistics for a tenant.
func (c *ECNClient) TenantAuditSummary(tenantID string) (*TenantAuditSummary, error) {
	resp, err := c.do(http.MethodGet, "/tenants/"+tenantID+"/audit/summary", nil, nil)
	if err != nil {
		return nil, err
	}
	return decodeResponse[*TenantAuditSummary](resp)
}

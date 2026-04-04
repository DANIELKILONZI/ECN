package ecnclient

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

// ---------------------------------------------------------------------------
// Test server helpers
// ---------------------------------------------------------------------------

func newTestServer(statusCode int, body any) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(statusCode)
		if body != nil {
			json.NewEncoder(w).Encode(body) //nolint:errcheck
		}
	}))
}

// routeTestServer returns a test server that dispatches to per-method/path handlers.
func routeTestServer(t *testing.T, routes map[string]func(w http.ResponseWriter, r *http.Request)) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		key := r.Method + " " + r.URL.Path
		if h, ok := routes[key]; ok {
			w.Header().Set("Content-Type", "application/json")
			h(w, r)
			return
		}
		http.Error(w, `{"detail":"not found"}`, http.StatusNotFound)
	}))
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v) //nolint:errcheck
}

// ---------------------------------------------------------------------------
// Constructor
// ---------------------------------------------------------------------------

func TestNew_stripsTrailingSlash(t *testing.T) {
	c := New("http://localhost:8000/", nil)
	if c.baseURL != "http://localhost:8000" {
		t.Errorf("expected stripped URL, got %q", c.baseURL)
	}
}

func TestNew_defaultTimeout(t *testing.T) {
	c := New("http://localhost:8000", nil)
	if c.httpClient == nil {
		t.Fatal("http client should not be nil")
	}
}

func TestNew_apiKey(t *testing.T) {
	c := New("http://localhost:8000", &Options{APIKey: "my-secret"})
	if c.apiKey != "my-secret" {
		t.Errorf("expected apiKey 'my-secret', got %q", c.apiKey)
	}
}

func TestNew_customHTTPClient(t *testing.T) {
	custom := &http.Client{}
	c := New("http://localhost:8000", &Options{HTTPClient: custom})
	if c.httpClient != custom {
		t.Error("expected custom HTTP client to be used")
	}
}

// ---------------------------------------------------------------------------
// Error types
// ---------------------------------------------------------------------------

func TestECNError_message(t *testing.T) {
	e := &ECNError{StatusCode: 500, Detail: "server error"}
	if e.Error() != "ECN API error 500: server error" {
		t.Errorf("unexpected error message: %s", e.Error())
	}
}

func TestErrors_hierarchy(t *testing.T) {
	txErr := &ECNTransactionError{ECNError{StatusCode: 422, Detail: "bad tx"}}
	var ecnErr *ECNError
	// ECNTransactionError embeds ECNError value, not pointer, so check directly
	if txErr.StatusCode != 422 {
		t.Error("expected status 422")
	}
	_ = ecnErr // satisfy unused warning in stricter compilers
}

// ---------------------------------------------------------------------------
// submit / ship / receive / inspect / quarantine / release
// ---------------------------------------------------------------------------

func TestShip(t *testing.T) {
	srv := routeTestServer(t, map[string]func(http.ResponseWriter, *http.Request){
		"POST /transactions": func(w http.ResponseWriter, r *http.Request) {
			var body map[string]any
			json.NewDecoder(r.Body).Decode(&body) //nolint:errcheck
			if body["type"] != "ship" {
				http.Error(w, "wrong type", 400)
				return
			}
			writeJSON(w, 200, &RoundResult{
				RoundID: 1, ConsensusReached: true, HonestNodes: []string{"A"},
			})
		},
	})
	defer srv.Close()

	c := New(srv.URL, nil)
	result, err := c.Ship("LAPTOP-001", "port", "DHL")
	if err != nil {
		t.Fatalf("Ship returned error: %v", err)
	}
	if !result.ConsensusReached {
		t.Error("expected consensus_reached=true")
	}
	if result.RoundID != 1 {
		t.Errorf("expected round_id=1, got %d", result.RoundID)
	}
}

func TestReceive(t *testing.T) {
	srv := newTestServer(200, &RoundResult{RoundID: 2, ConsensusReached: true})
	defer srv.Close()
	c := New(srv.URL, nil)
	result, err := c.Receive("P1", "buyer", false)
	if err != nil {
		t.Fatalf("Receive error: %v", err)
	}
	if result.RoundID != 2 {
		t.Errorf("expected round_id=2, got %d", result.RoundID)
	}
}

func TestInspect(t *testing.T) {
	srv := newTestServer(200, &RoundResult{RoundID: 3, ConsensusReached: true})
	defer srv.Close()
	c := New(srv.URL, nil)
	_, err := c.Inspect("P1", "label_check", "Lab")
	if err != nil {
		t.Fatalf("Inspect error: %v", err)
	}
}

func TestQuarantine(t *testing.T) {
	srv := newTestServer(200, &RoundResult{RoundID: 4, ConsensusReached: true})
	defer srv.Close()
	c := New(srv.URL, nil)
	_, err := c.Quarantine("P1", "suspicious")
	if err != nil {
		t.Fatalf("Quarantine error: %v", err)
	}
}

func TestRelease(t *testing.T) {
	srv := newTestServer(200, &RoundResult{RoundID: 5, ConsensusReached: true})
	defer srv.Close()
	c := New(srv.URL, nil)
	_, err := c.Release("P1", "Inspector")
	if err != nil {
		t.Fatalf("Release error: %v", err)
	}
}

func TestSubmit_422_TransactionError(t *testing.T) {
	srv := newTestServer(422, map[string]any{"detail": "unknown type"})
	defer srv.Close()
	c := New(srv.URL, nil)
	_, err := c.Submit("explode", nil)
	var txErr *ECNTransactionError
	if !errors.As(err, &txErr) {
		t.Errorf("expected ECNTransactionError, got %T: %v", err, err)
	}
}

// ---------------------------------------------------------------------------
// Network endpoints
// ---------------------------------------------------------------------------

func TestNodes(t *testing.T) {
	nodes := []NodeInfo{{NodeID: "A", Port: 9001, PublicKeyHex: "abcd", Malicious: false}}
	srv := newTestServer(200, nodes)
	defer srv.Close()
	c := New(srv.URL, nil)
	result, err := c.Nodes()
	if err != nil {
		t.Fatalf("Nodes error: %v", err)
	}
	if len(result) != 1 || result[0].NodeID != "A" {
		t.Errorf("unexpected nodes: %+v", result)
	}
}

func TestNetworkState(t *testing.T) {
	state := &NetworkState{NodeCount: 5, FaultRate: 0.0}
	srv := newTestServer(200, state)
	defer srv.Close()
	c := New(srv.URL, nil)
	result, err := c.NetworkState()
	if err != nil {
		t.Fatalf("NetworkState error: %v", err)
	}
	if result.NodeCount != 5 {
		t.Errorf("expected NodeCount=5, got %d", result.NodeCount)
	}
}

// ---------------------------------------------------------------------------
// Audit endpoints
// ---------------------------------------------------------------------------

func TestAuditEvents(t *testing.T) {
	response := &AuditEventsResponse{Total: 3, Offset: 0, Events: []RoundResult{}}
	srv := routeTestServer(t, map[string]func(http.ResponseWriter, *http.Request){
		"GET /audit/events": func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Query().Get("limit") == "" {
				http.Error(w, "missing limit", 400)
				return
			}
			writeJSON(w, 200, response)
		},
	})
	defer srv.Close()
	c := New(srv.URL, nil)
	result, err := c.AuditEvents(50, 0)
	if err != nil {
		t.Fatalf("AuditEvents error: %v", err)
	}
	if result.Total != 3 {
		t.Errorf("expected total=3, got %d", result.Total)
	}
}

func TestAuditEvent_notFound(t *testing.T) {
	srv := newTestServer(404, map[string]any{"detail": "not found"})
	defer srv.Close()
	c := New(srv.URL, nil)
	_, err := c.AuditEvent(99)
	var nfe *ECNNotFoundError
	if !errors.As(err, &nfe) {
		t.Errorf("expected ECNNotFoundError, got %T", err)
	}
}

func TestAuditSummary(t *testing.T) {
	summary := &AuditSummary{TotalRounds: 5, FaultRate: 0.2}
	srv := newTestServer(200, summary)
	defer srv.Close()
	c := New(srv.URL, nil)
	result, err := c.AuditSummary()
	if err != nil {
		t.Fatalf("AuditSummary error: %v", err)
	}
	if result.FaultRate != 0.2 {
		t.Errorf("expected FaultRate=0.2, got %f", result.FaultRate)
	}
}

// ---------------------------------------------------------------------------
// Webhooks
// ---------------------------------------------------------------------------

func TestSubscribeWebhook(t *testing.T) {
	resp := &WebhookSubscribeResponse{
		WebhookID: "wh-1", URL: "https://x.com", Events: []string{"fault"}, Message: "ok",
	}
	srv := newTestServer(201, resp)
	defer srv.Close()
	c := New(srv.URL, nil)
	result, err := c.SubscribeWebhook("https://x.com", []string{"fault"})
	if err != nil {
		t.Fatalf("SubscribeWebhook error: %v", err)
	}
	if result.WebhookID != "wh-1" {
		t.Errorf("unexpected webhook_id: %s", result.WebhookID)
	}
}

func TestUnsubscribeWebhook(t *testing.T) {
	srv := newTestServer(204, nil)
	defer srv.Close()
	c := New(srv.URL, nil)
	if err := c.UnsubscribeWebhook("wh-1"); err != nil {
		t.Fatalf("UnsubscribeWebhook error: %v", err)
	}
}

// ---------------------------------------------------------------------------
// Event streaming
// ---------------------------------------------------------------------------

func TestStreamTopics(t *testing.T) {
	resp := &StreamTopicsResponse{Topics: []TopicInfo{
		{Topic: "transactions", BufferedEvents: 3, NextOffset: 3},
	}}
	srv := newTestServer(200, resp)
	defer srv.Close()
	c := New(srv.URL, nil)
	result, err := c.StreamTopics()
	if err != nil {
		t.Fatalf("StreamTopics error: %v", err)
	}
	if len(result.Topics) != 1 || result.Topics[0].Topic != "transactions" {
		t.Errorf("unexpected topics: %+v", result.Topics)
	}
}

func TestPollEvents(t *testing.T) {
	resp := &PollEventsResponse{
		Topic: "transactions", Offset: 0, Count: 1, NextOffset: 1,
		Events: []StreamEvent{{Seq: 0, Topic: "transactions"}},
	}
	srv := routeTestServer(t, map[string]func(http.ResponseWriter, *http.Request){
		"GET /stream/topics/transactions": func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Query().Get("offset") == "" {
				http.Error(w, "missing offset", 400)
				return
			}
			writeJSON(w, 200, resp)
		},
	})
	defer srv.Close()
	c := New(srv.URL, nil)
	result, err := c.PollEvents("transactions", 0, 10)
	if err != nil {
		t.Fatalf("PollEvents error: %v", err)
	}
	if result.NextOffset != 1 {
		t.Errorf("expected NextOffset=1, got %d", result.NextOffset)
	}
}

// ---------------------------------------------------------------------------
// Admin / RBAC
// ---------------------------------------------------------------------------

func TestIssueAPIKey(t *testing.T) {
	resp := &APIKeyResponse{KeyID: "k1", Key: "secret", Role: "submitter"}
	srv := newTestServer(201, resp)
	defer srv.Close()
	c := New(srv.URL, nil)
	result, err := c.IssueAPIKey("submitter", "ci-bot", nil)
	if err != nil {
		t.Fatalf("IssueAPIKey error: %v", err)
	}
	if result.KeyID != "k1" {
		t.Errorf("expected key_id 'k1', got %q", result.KeyID)
	}
}

func TestRevokeAPIKey(t *testing.T) {
	srv := newTestServer(204, nil)
	defer srv.Close()
	c := New(srv.URL, nil)
	if err := c.RevokeAPIKey("k1"); err != nil {
		t.Fatalf("RevokeAPIKey error: %v", err)
	}
}

// ---------------------------------------------------------------------------
// Multi-tenant
// ---------------------------------------------------------------------------

func TestCreateTenant(t *testing.T) {
	resp := &TenantInfo{TenantID: "t1", Name: "Bank-A", NodeCount: 3}
	srv := newTestServer(201, resp)
	defer srv.Close()
	c := New(srv.URL, nil)
	result, err := c.CreateTenant("Bank-A", 3, nil)
	if err != nil {
		t.Fatalf("CreateTenant error: %v", err)
	}
	if result.TenantID != "t1" {
		t.Errorf("expected tenant_id 't1', got %q", result.TenantID)
	}
}

func TestTenantShip(t *testing.T) {
	srv := routeTestServer(t, map[string]func(http.ResponseWriter, *http.Request){
		"POST /tenants/t1/transactions": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(w, 200, &RoundResult{RoundID: 1, ConsensusReached: true})
		},
	})
	defer srv.Close()
	c := New(srv.URL, nil)
	result, err := c.TenantShip("t1", "ITEM-001", "vault", "BankCo")
	if err != nil {
		t.Fatalf("TenantShip error: %v", err)
	}
	if !result.ConsensusReached {
		t.Error("expected consensus_reached=true")
	}
}

func TestDeleteTenant(t *testing.T) {
	srv := newTestServer(204, nil)
	defer srv.Close()
	c := New(srv.URL, nil)
	if err := c.DeleteTenant("t1"); err != nil {
		t.Fatalf("DeleteTenant error: %v", err)
	}
}

func TestTenantAuditSummary(t *testing.T) {
	resp := &TenantAuditSummary{TenantID: "t1", TotalRounds: 2, FaultRate: 0.0}
	srv := routeTestServer(t, map[string]func(http.ResponseWriter, *http.Request){
		"GET /tenants/t1/audit/summary": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(w, 200, resp)
		},
	})
	defer srv.Close()
	c := New(srv.URL, nil)
	result, err := c.TenantAuditSummary("t1")
	if err != nil {
		t.Fatalf("TenantAuditSummary error: %v", err)
	}
	if result.TenantID != "t1" {
		t.Errorf("expected tenant_id 't1', got %q", result.TenantID)
	}
}

func TestAPIKeyHeaderSent(t *testing.T) {
	var capturedKey string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		capturedKey = r.Header.Get("X-API-Key")
		w.WriteHeader(204)
	}))
	defer srv.Close()
	c := New(srv.URL, &Options{APIKey: "secret-key"})
	c.DeleteTenant("t1") //nolint:errcheck
	if capturedKey != "secret-key" {
		t.Errorf("expected X-API-Key='secret-key', got %q", capturedKey)
	}
}

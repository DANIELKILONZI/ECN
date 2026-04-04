"""
test_node_health.py
-------------------
Tests for ecn.node_health — NodeHealthRegistry and per-node health lifecycle.
"""
import pytest
from ecn.node_health import (
    NodeState,
    NodeHealth,
    NodeHealthRegistry,
    init_health_registry,
    get_health_registry,
    _SUSPECT_THRESHOLD,
    _EXCLUDED_THRESHOLD,
)


class TestNodeState:
    def test_all_states_defined(self):
        states = [s.value for s in NodeState]
        assert "HEALTHY" in states
        assert "SUSPECT" in states
        assert "EXCLUDED" in states
        assert "REHABILITATING" in states

    def test_state_is_string_enum(self):
        assert NodeState.HEALTHY == "HEALTHY"


class TestNodeHealthRegistry:
    def setup_method(self):
        self.registry = NodeHealthRegistry()

    def test_new_node_is_healthy(self):
        h = self.registry.get("Node-1")
        assert h.state == NodeState.HEALTHY
        assert h.consecutive_failures == 0

    def test_record_success_stays_healthy(self):
        h = self.registry.record_success("Node-1", latency_ms=10)
        assert h.state == NodeState.HEALTHY
        assert h.consecutive_failures == 0

    def test_record_success_sets_last_success_at(self):
        h = self.registry.record_success("Node-1")
        assert h.last_success_at is not None
        assert h.last_success_at.endswith("Z")

    def test_failures_below_suspect_threshold_stay_healthy(self):
        for _ in range(_SUSPECT_THRESHOLD - 1):
            h = self.registry.record_failure("Node-1")
        assert h.state == NodeState.HEALTHY
        assert h.consecutive_failures == _SUSPECT_THRESHOLD - 1

    def test_suspect_threshold_transitions_to_suspect(self):
        for _ in range(_SUSPECT_THRESHOLD):
            h = self.registry.record_failure("Node-1")
        assert h.state == NodeState.SUSPECT

    def test_excluded_threshold_transitions_to_excluded(self):
        for _ in range(_EXCLUDED_THRESHOLD):
            h = self.registry.record_failure("Node-1")
        assert h.state == NodeState.EXCLUDED

    def test_success_resets_failures_for_healthy_node(self):
        self.registry.record_failure("Node-1")
        self.registry.record_failure("Node-1")
        h = self.registry.record_success("Node-1")
        assert h.consecutive_failures == 0
        assert h.state == NodeState.HEALTHY

    def test_success_resets_failures_for_suspect_node(self):
        for _ in range(_SUSPECT_THRESHOLD):
            self.registry.record_failure("Node-1")
        h = self.registry.record_success("Node-1")
        assert h.consecutive_failures == 0
        assert h.state == NodeState.HEALTHY

    def test_excluded_node_needs_rehab_window(self):
        for _ in range(_EXCLUDED_THRESHOLD):
            self.registry.record_failure("Node-1")
        # Success immediately after exclusion — still excluded (window not elapsed)
        h = self.registry.record_success("Node-1")
        assert h.state == NodeState.EXCLUDED

    def test_excluded_node_marked_excluded_since(self):
        for _ in range(_EXCLUDED_THRESHOLD):
            h = self.registry.record_failure("Node-1")
        assert h.excluded_since is not None

    def test_active_nodes_excludes_excluded(self):
        self.registry.get("Node-1")
        self.registry.get("Node-2")
        for _ in range(_EXCLUDED_THRESHOLD):
            self.registry.record_failure("Node-2")
        active = self.registry.active_nodes()
        assert "Node-1" in active
        assert "Node-2" not in active

    def test_excluded_nodes_list(self):
        for _ in range(_EXCLUDED_THRESHOLD):
            self.registry.record_failure("Node-1")
        excluded = self.registry.excluded_nodes()
        assert "Node-1" in excluded

    def test_all_nodes_returns_all(self):
        self.registry.get("Node-1")
        self.registry.get("Node-2")
        all_nodes = self.registry.all_nodes()
        assert len(all_nodes) == 2
        assert "Node-1" in all_nodes
        assert "Node-2" in all_nodes

    def test_node_isolation(self):
        for _ in range(_EXCLUDED_THRESHOLD):
            self.registry.record_failure("Node-1")
        h2 = self.registry.get("Node-2")
        assert h2.state == NodeState.HEALTHY

    def test_p95_latency_single_sample(self):
        h = self.registry.record_success("Node-1", latency_ms=50.0)
        assert h.p95_latency_ms == 50.0

    def test_p95_latency_multiple_samples(self):
        for ms in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
            h = self.registry.record_success("Node-1", latency_ms=float(ms))
        # p95 of 10 samples sorted = [10,20,...,100], idx = int(10*0.95)=9 → 100
        assert h.p95_latency_ms == 100.0

    def test_p95_latency_none_when_no_data(self):
        h = self.registry.get("Node-1")
        assert h.p95_latency_ms is None

    def test_to_dict_has_required_keys(self):
        h = self.registry.get("Node-1")
        d = h.to_dict()
        assert "node_id" in d
        assert "state" in d
        assert "consecutive_failures" in d
        assert "last_success_at" in d
        assert "excluded_since" in d
        assert "p95_latency_ms" in d


class TestHealthRegistrySingleton:
    def test_init_and_get(self, monkeypatch):
        import ecn.node_health as nh
        monkeypatch.setattr(nh, "_registry", None)
        registry = init_health_registry()
        assert registry is get_health_registry()

    def test_get_raises_before_init(self, monkeypatch):
        import ecn.node_health as nh
        monkeypatch.setattr(nh, "_registry", None)
        with pytest.raises(RuntimeError):
            get_health_registry()

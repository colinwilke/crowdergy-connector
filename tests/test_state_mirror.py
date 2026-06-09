"""Tests für DeviceStateMirror (FEAT-5 Phase A, 2026-06-09).

Phase A ist reine Storage-Extraktion — wir testen hier nur die
strukturellen Invarianten der Dataclass, weil Verhalten unverändert
beim Coordinator bleibt.
"""
from __future__ import annotations

from custom_components.theothergas.state_mirror import DeviceStateMirror


def test_default_state_is_empty():
    s = DeviceStateMirror()
    assert s.active_state == {}
    assert s.on_state == {}
    assert s.cool_state == {}
    assert s.hold_tasks == {}
    assert s.charge_mode_hold_tasks == {}
    assert s.held_charge_mode == {}
    assert s.active_state_bootstrapped is False
    assert s.last_sse_event_at == 0.0


def test_independent_instances_dont_share_dicts():
    """Sanity: jede DeviceStateMirror-Instance kriegt eigene Dicts —
    sonst würden zwei parallele Coordinator-Setups (Tests, Multi-
    Entry) sich gegenseitig die Caches verstellen."""
    a = DeviceStateMirror()
    b = DeviceStateMirror()
    a.active_state["dev-1"] = True
    assert "dev-1" not in b.active_state


def test_state_mirror_is_mutable():
    """Zugriff übers Coordinator-Property-Shim funktioniert auf
    mutierende Dict-Operationen (set/pop/clear). Hier direkt am
    DataClass-Attribut verifiziert."""
    s = DeviceStateMirror()
    s.active_state["d1"] = True
    s.on_state["d1"] = False
    s.cool_state["d1"] = True
    s.held_charge_mode["d1"] = "solar"
    s.active_state_bootstrapped = True
    s.last_sse_event_at = 1234.5
    assert s.active_state == {"d1": True}
    assert s.on_state == {"d1": False}
    assert s.cool_state == {"d1": True}
    assert s.held_charge_mode == {"d1": "solar"}
    assert s.active_state_bootstrapped is True
    assert s.last_sse_event_at == 1234.5

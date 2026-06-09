"""Smoke-Test: pure-Logic-Helper aus dem Connector ohne HA-Harness.

Pendant zum Backend `test_daily_summary` — wenn wir hier mal eine
zeit-/sleep-Math im Connector haben (z.B. SSE-Backoff), kommt der
Test hierhin.

Aktuell als Placeholder + Smoke-Test damit pytest-collect nicht
leer ist.
"""
from __future__ import annotations


def test_constants_loadable():
    """Sanity: die SSE-Stale-Threshold-Konstante (Cluster B-Fix
    `_hold_loop` SSE-Bail) ist definiert und positiv."""
    from custom_components.theothergas.const import SSE_STALE_THRESHOLD_S
    assert isinstance(SSE_STALE_THRESHOLD_S, (int, float))
    assert SSE_STALE_THRESHOLD_S > 0


def test_solver_extra_fields_registry_non_empty():
    """`_SOLVER_EXTRA_FIELDS` ist die SSOT-Registry für solver-only-
    Felder (vorlauf_setpoint_c etc.). Wenn die je leer ist, bricht
    Phase-2-Continuous-Vorlauf still."""
    from custom_components.theothergas.coordinator import _SOLVER_EXTRA_FIELDS
    assert isinstance(_SOLVER_EXTRA_FIELDS, (dict, list, tuple))
    assert len(_SOLVER_EXTRA_FIELDS) > 0

# crowdergy-connector — Projekt-Memory

> **Standing Rule (User-Vorgabe 2026-06-10): `CONTEXT.md` (Detail-Stand)
> und diese Datei am Ende JEDER Arbeitssitzung automatisch aktualisieren.**

Detaillierter Stand: `CONTEXT.md`. Multi-Repo-Index: `crowdergy-ios/CLAUDE.md`.
Box-Integration (provision_box, box_services, Consent-Gates) gemerged
(PR #1, 2026-06-11); Gegenstück im Repo `crowdergy-box`.

Tests laufen NUR mit Python ≥3.12 (`requirements-test.txt`-Kopf);
`tests/test_sse_client.py::test_start_is_idempotent` hat einen bekannten
umgebungsabhängigen Error (aiodns/aiohttp-Drift in frischen venvs) — auf
sauberem Stand gegenprüfen, bevor man eigene Änderungen verdächtigt.

### Harter Code-Review 2026-06-11 — umgesetzt (v3.22.0 auf diesem Branch)

CN-1…CN-14 (ohne CN-10) + P3 gefixt; 71 Tests grün (2 deselektiert:
`test_start_is_idempotent` + `test_stop_cancels_running_task`, beides der
bekannte aiodns/aiohttp-Drift, per Baseline verifiziert). Kern:
Remote-Control-Consent-Gate jetzt ZENTRAL in allen `_apply_*`
(Resync/Self-Heal/Hold eingeschlossen); Re-Pairing merged Consent-Options;
Self-Heal respektiert SSE-Stale; Climate-Guard liest das
`temperature`-Attribut; Mirror hat eigenen `_last_mirror_at` und ist auf
Telemetrie-Consent gegated; Box-Services nur noch mit `theothergas:`-
YAML-Key (bewusst Breaking für Self-Hosted ohne Key); SSE-401 mit Backoff
+ Reauth-Flow nach 5 Zyklen; Options-Flow persistiert sofort. Verträge zu
Backend/Box waren feldgenau OK. Offen (User-Entscheidung): E-2
Wallbox-Snapshot/Restore (`charge_mode_value_crowdergy`-Pfad noch drin),
E-4 Liveness-Traffic bei consent_telemetry=False, E-6 Slot-Allowlist (CN-10).

### Agent-Ownership (Interferenz-Schutz, User-Vorgabe 2026-06-10)

- **Schreib-Ownership dieses Repos: die Remote-/Web-Session** (zusammen mit
  connector + backend). Der lokale Mac-/Xcode-Agent besitzt `crowdergy-ios`.
- Fremde Agents: dieses Repo LESEN ja (API-Verträge, dev-up.sh), schreiben
  nein — Ausnahme: CLAUDE.md/CONTEXT.md-Memory-Updates.
- Nie auf fremden `claude/...`-Arbeitsbranches committen; eigene Branches,
  Sync-Punkt ist main (Merges macht der User bzw. der lokale Agent).

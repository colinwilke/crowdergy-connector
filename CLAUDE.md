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

### Harter Code-Review 2026-06-11 — Befunde offen

Multi-Agent-Review über alle 5 Repos; konsolidierte Fix-Prompts beim User.
Top hier: (1) Remote-Control-Consent-Gate nur in `_handle_ws_message` —
`state_resync_loop` + Hold-Self-Heal umgehen es (Gate in die `_apply_*`
ziehen); (2) Re-Pairing via provision_box verwirft Consent-Flags
(`already_configured` updated nur Tokens, nie Options); (3) Hold-Self-Heal
ignoriert den SSE-Stale-Bail (überschreibt User-Eingriffe im Outage);
(4) Climate-Idempotenz-Guard wirkungslos (`float("heat")` wirft);
(5) `charge_mode_value_crowdergy`-Restore tot. Verträge zu Backend/Box
feldgenau OK (14 REST-Pfade, SSE-Frames, device_field_spec).

### Agent-Ownership (Interferenz-Schutz, User-Vorgabe 2026-06-10)

- **Schreib-Ownership dieses Repos: die Remote-/Web-Session** (zusammen mit
  connector + backend). Der lokale Mac-/Xcode-Agent besitzt `crowdergy-ios`.
- Fremde Agents: dieses Repo LESEN ja (API-Verträge, dev-up.sh), schreiben
  nein — Ausnahme: CLAUDE.md/CONTEXT.md-Memory-Updates.
- Nie auf fremden `claude/...`-Arbeitsbranches committen; eigene Branches,
  Sync-Punkt ist main (Merges macht der User bzw. der lokale Agent).

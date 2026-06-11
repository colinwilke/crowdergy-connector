# crowdergy-connector — Projekt-Memory

> **Standing Rule (User-Vorgabe 2026-06-10): `CONTEXT.md` (Detail-Stand)
> und diese Datei am Ende JEDER Arbeitssitzung automatisch aktualisieren.**

Detaillierter Stand: `CONTEXT.md`. Multi-Repo-Index: `crowdergy-ios/CLAUDE.md`.
Box-Integration (provision_box, box_services, Consent-Gates) auf Branch
`claude/trusting-planck-4f9txj`; Gegenstück im Repo `crowdergy-box`.

Tests laufen NUR mit Python ≥3.12 (`requirements-test.txt`-Kopf);
`tests/test_sse_client.py::test_start_is_idempotent` hat einen bekannten
umgebungsabhängigen Error (aiodns/aiohttp-Drift in frischen venvs) — auf
sauberem Stand gegenprüfen, bevor man eigene Änderungen verdächtigt.

### Agent-Ownership (Interferenz-Schutz, User-Vorgabe 2026-06-10)

- **Schreib-Ownership dieses Repos: die Remote-/Web-Session** (zusammen mit
  connector + backend). Der lokale Mac-/Xcode-Agent besitzt `crowdergy-ios`.
- Fremde Agents: dieses Repo LESEN ja (API-Verträge, dev-up.sh), schreiben
  nein — Ausnahme: CLAUDE.md/CONTEXT.md-Memory-Updates.
- Nie auf fremden `claude/...`-Arbeitsbranches committen; eigene Branches,
  Sync-Punkt ist main (Merges macht der User bzw. der lokale Agent).

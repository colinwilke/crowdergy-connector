# crowdergy-connector — Projekt-Memory

> **Standing Rule (User-Vorgabe 2026-06-10): `CONTEXT.md` (Detail-Stand)
> und diese Datei am Ende JEDER Arbeitssitzung automatisch aktualisieren.**

Detaillierter Stand: `CONTEXT.md`. Multi-Repo-Index, Vereinbarungen und der
**projektweite Backlog (SSOT)**: `crowdergy-ios/CLAUDE.md`. Connector-Items
dort: Cluster C (#16–#22).

## Repo-Regeln & getroffene Entscheidungen

- **SSOT-Regeln (immer einhalten):**
  - Neue Backend-Device-Felder NUR in `device_field_spec.py` (Roundtrip
    create/update), nie direkt in `_register_device`/`_update_device_backend`.
  - Neue Mapping-Slots NUR in der Allowlist `MAPPABLE_ENTITY_DOMAINS`
    (Default-DENY; Read-Slots nur sensor/binary_sensor, Control-Slots nur
    schreibbare Domains).
  - `CONTROLLABLE_TYPES` in `const.py` ist SSOT für steuerbare Typen.
  - Preset-Slots (was in einen Crowd-Preset-Beitrag gehört) NUR in
    `preset_spec.PRESET_SLOT_SPEC`; Entity-Slots dort müssen in
    `MAPPABLE_ENTITY_DOMAINS` stehen (Test-gesichert). Vertrag:
    `docs/crowd-preset-store.md`.
  - **`api_client.py` ist DER eine Auth-Pfad** (ab v3.27.0): NIE eine
    zweite Refresh-Implementierung anlegen — neue Backend-Calls über
    `CrowdergyAuthSession.async_request` bzw. `authenticated_request`
    (re-used die Coordinator-Session; Single-Flight-Lock + CAS gegen das
    Single-Use-Refresh-Token). httpx-Clients IMMER im Executor bauen.
- **Onboarding = Pairing-Code (User-Entscheidung 2026-06-12, gelandet v3.27.0):**
  Email/Passwort ist aus dem Config-Flow KOMPLETT raus (auch Reauth) —
  HA sieht nie Credentials, funktioniert auch für Apple-only-Accounts,
  Backend kann die Session revozieren. Claim kanonisch
  `POST /api/v1/connector/claim` mit `client_id` = HA-Instance-ID;
  Fallback auf `/api/v1/box/claim`+`box_id` NUR bei 404/405 (altes
  Backend). Bestands-Entries aus der Login-Ära laufen unverändert
  weiter (kein Migrationszwang). Reauth lehnt Codes fremder Accounts ab
  (`reauth_account_mismatch`) — Reauth tauscht Tokens, nie den Account.
- **Consent-Semantik (entschieden):** Telemetrie-Consent gated NUR
  Energiedaten. Liveness-Traffic (Heartbeat/Version/Device-Polling) ist
  bewusst NICHT gegated — dokumentiert in `services.yaml` +
  `telemetry_composer`. Remote-Control-Consent wird ZENTRAL in allen
  `_apply_*` geprüft (inkl. Resync/Self-Heal/Hold).
- **Box-Services nur mit `theothergas:`-YAML-Key** (bewusst Breaking für
  Self-Hosted ohne Key). Normale HACS-Installationen sind von allen
  Box-Pfaden unberührt.
- **Wallbox:** Pre-AI-Lademodus wird bei AI-OFF NICHT restauriert
  (Restore-Pfad entfernt, Backend-Spalte existiert nicht mehr).
- **Config-Flow Edit-Felder:** `vol.Optional(..., description=
  {"suggested_value": ...})`, NIE `default=` (HA re-injected, Felder
  werden unlöschbar).
- **Entry-Schema-Regel (Box-Update-Stabilität, 2026-06-11):**
  Änderungen an Entry-data/-options nur ADDITIV oder mit
  Load-Time-Migration („Feld fehlt = Altverhalten") — ein
  Vendor-Pin-Bump auf der Box darf NIE Re-Provisionierung brauchen.
- **Release-Prozess:** Manifest-Bump + Tag; GitHub-Release via
  `tag-release.yml` bzw. User. Die Remote-Session pusht nur auf den
  Arbeitsbranch. Vor dem Taggen prüfen, ob der Tag auf origin schon
  belegt ist (parallele Sessions) — im Zweifel weiter bumpen statt
  Konflikt.
- **Public-Repo-Disziplin:** Dieses Repo ist public (HACS =
  Contribute-Kanal für den Crowd-Preset-Store). Hier liegt nur das
  Slot-SCHEMA (`preset_spec.py`); Store-Daten/Kuration bleiben im Backend
  hinter Auth, Box-Know-how bleibt im privaten Box-Repo.

## Tests

Nur mit **Python ≥ 3.12** (`requirements-test.txt`-Kopf).

**Remote-Session (Claude Code on the web):** der SessionStart-Hook
(`.claude/hooks/session-start.sh`) baut `.venv` (Python 3.12,
`requirements-test.txt`). Tests dann via `.venv/bin/pytest`.
`tests/test_sse_client.py::test_start_is_idempotent` und
`test_stop_cancels_running_task` haben einen bekannten
umgebungsabhängigen Error (aiodns/aiohttp-Drift in frischen venvs) — auf
sauberem Stand gegenprüfen, bevor man eigene Änderungen verdächtigt.

## Agent-Ownership (Interferenz-Schutz, User-Vorgabe 2026-06-10)

- **Schreib-Ownership dieses Repos: die Remote-/Web-Session** (zusammen mit
  backend + box). Der lokale Mac-/Xcode-Agent besitzt `crowdergy-ios`.
- Fremde Agents: dieses Repo LESEN ja (API-Verträge), schreiben nein —
  Ausnahme: CLAUDE.md/CONTEXT.md-Memory-Updates.
- **Trunk-based (User-Entscheidung 2026-06-11):** Owner-Agent pusht direkt
  auf `main`; Bedingung: Tests vorher grün. `claude/...`-Branches nur für
  Riskantes/Experimentelles; nie auf fremden Arbeitsbranches committen.
  Releases bleiben davon getrennt (nur via Tag).

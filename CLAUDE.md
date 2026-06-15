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
- **Entry-Schema-Regel — [Alpha: gelockert, 2026-06-15]:** In der Alpha
  dürfen Entry-data/-options breaking ändern (Test-User
  re-provisionieren); KEINE „Feld fehlt = Altverhalten"-Load-Time-
  Migration mehr nötig. Die alte Stabilitätsgarantie (additiv/migrierbar,
  Pin-Bump ohne Re-Provisionierung) greift erst mit echten Prod-Usern
  wieder.
- **Release-Prozess:** Manifest-Bump + Tag passieren **nur beim Release
  auf `main`**, durch den, der das Tag schneidet — NIE auf
  Feature-Branches (sonst Versions-Kollision wie beim v3.26.0-Chaos: drei
  Branches, dieselbe Nummer, verschiedene Arbeit). GitHub-Release via
  `tag-release.yml` bzw. User; vor dem Taggen prüfen, ob der Tag auf
  origin schon belegt ist. **Stand: v3.28.0 released** (2026-06-15, Tag
  via `tag-release.yml`-MCP-Trigger: #42 Hausverbrauchs-Flow-Sensoren
  (HC-Triade) für den Hausverbrauchs-Chart; davor v3.27.1 `5a86843` =
  #18 Telemetry-Retry/Backoff + #19 proaktives Token-Refresh, v3.27.0
  `2f2ea76` = #39 `/connector/*`-Pairing-Alias + #40 device_update-
  Preset-Apply). Box-Pin noch auf v3.27.1 — Re-Vendor auf v3.28.0
  ausstehend.
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
- **Trunk-based (User 2026-06-11; geschärft 2026-06-15):** Owner-Agent
  pusht klein + grün direkt auf `main` (Tests grün). `claude/...`-Branches
  nur für Riskantes/Experimentelles und **kurzlebig** (same-session
  mergen/löschen). Handoff-Fenster: bei fremder Release-Übernahme komplett
  stillstehen. Voller Workflow + Parallel-Agent-/Alpha-Regeln: SSOT
  `crowdergy-ios/CLAUDE.md` (Abschnitte „Agent-Ownership & Branches" +
  „Alpha-Phase").

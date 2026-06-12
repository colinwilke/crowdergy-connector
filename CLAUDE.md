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

### Harter Code-Review 2026-06-11 — umgesetzt (v3.22.0 released, Tag auf main)

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
Backend/Box waren feldgenau OK.

### v3.26.0 vorbereitet 2026-06-12 (Branch `claude/device-hierarchy-power-snys2h`)

`included_in_haushalt` end-to-end entfernt — ersetzt durch den
Backend-Topologie-Baum `parent_device_id` (App-konfiguriert,
„Übergeordnetes Gerät"). Bestands-Entries behalten schlafende Werte;
Backend toleriert den Key von ≤3.25 als No-Op. Release-Notes in
`docs/releases/v3.26.0.md`; **Tag erst nach Backend-Deploy von Mig
`20260612_0001`**. 100 Tests grün (2 bekannte aiodns-Deselects).

### User-Entscheidungen 2026-06-11 umgesetzt → v3.23.0

E-2: toter Wallbox-Restore-Pfad (`charge_mode_value_crowdergy`,
`_snapshot_/_restore_charge_mode`) entfernt — Pre-AI-Lademodus wird bei
AI-OFF NICHT mehr restauriert (Backend-Spalte seit 2026-06-03 weg).
E-6 (CN-10): Blocklist → **Allowlist** `MAPPABLE_ENTITY_DOMAINS` pro Slot
(Default-DENY; Read-Slots nur sensor/binary_sensor, Control-Slots nur
schreibbare Domains; KOSTAL per Regressionstest grün). NEUE Slots künftig
dort eintragen (analog `device_field_spec.SPEC`). E-4: Liveness
(Heartbeat/Version/Device-Polling) bewusst NICHT gegated — dokumentiert in
services.yaml + telemetry_composer (Telemetrie-Consent = nur Energiedaten).
76 Tests grün (2 deselektiert: bekannter aiodns-Drift).

### Agent-Ownership (Interferenz-Schutz, User-Vorgabe 2026-06-10)

- **Schreib-Ownership dieses Repos: die Remote-/Web-Session** (zusammen mit
  connector + backend). Der lokale Mac-/Xcode-Agent besitzt `crowdergy-ios`.
- Fremde Agents: dieses Repo LESEN ja (API-Verträge, dev-up.sh), schreiben
  nein — Ausnahme: CLAUDE.md/CONTEXT.md-Memory-Updates.
- Nie auf fremden `claude/...`-Arbeitsbranches committen; eigene Branches,
  Sync-Punkt ist main (Merges macht der User bzw. der lokale Agent).

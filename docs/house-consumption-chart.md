# House-consumption chart — cross-lane contract (#41 / #42 / #43)

The Hausverbrauchs-Diagramm (stacked-area "where did the home's power
come from, where did the surplus go") spans three repos. This file is
the single source of truth for the data contract; the per-repo code is
the implementation.

- **Connector (#42)** — maps the vendor sensors, ships their values.
- **Backend (#41)** — buckets + decomposes, serves the chart endpoint.
- **iOS (#43)** — renders `HouseConsumptionChartView`.

## Connector → Backend: the HC-triade (`telemetry.extra`)

Four optional read-only power sensors (`device_field_spec` / config-flow
read step, Default-DENY `sensor`-only in `MAPPABLE_ENTITY_DOMAINS`). They
do **not** become typed device columns; they ride the existing
`telemetry.extra` pipeline (like `vorlauf_temp_c`), normalised to **kW**
by `_read_power_kw`. The Connector registry is
`coordinator._SOLVER_EXTRA_FIELDS`; the Backend mirror is
`app/mpc/solver_fields.SOLVER_FIELDS` (so `validate_extra` keeps them).

| Config slot (`const.py`) | On device | `extra` key | Kostal register |
|---|---|---|---|
| `entity_home_consumption_pv_power` | solar | `hc_pv_power_kw` | Home Consumption from PV |
| `entity_home_consumption_battery_power` | battery | `hc_battery_power_kw` | Home Consumption from Battery |
| `entity_home_consumption_grid_power` | grid | `hc_grid_power_kw` | Home Consumption from Grid |
| `entity_pv_to_battery_power` *(optional)* | battery | `pv_to_battery_power_kw` | PV→battery charge power |

The three HC sensors are the **vendor-truth trigger**: they say directly
how much of the home's current consumption was covered by PV / battery /
grid (`Σ ≈ total home power`). The 4th sensor is a nice-to-have that
removes a derivation (see below). All are unsigned (≥0); the W→kW
conversion is unit-attribute driven.

**Keys must stay in lock-step** across `_SOLVER_EXTRA_FIELDS` (Connector)
and `SOLVER_FIELDS` (Backend) — a mismatch makes `validate_extra` drop
the value silently. `tests/test_house_consumption_extras.py` (Connector)
and `tests/test_house_energy.py` (Backend) pin both ends.

## Backend endpoint

`GET /api/v1/users/me/energy/today?slots=hourly|15min&day=yyyy-MM-dd`

`day` defaults to today (Europe/Berlin); buckets anchor to local
midnight via `date_bin`. Values are **slot-average power in kW** (not
kWh). Response = `HouseEnergyDay`:

```jsonc
{
  "date": "2026-06-15",
  "granularity": "hourly",          // echoes the request
  "source": "vendor",               // "vendor" | "allocator" | "none"
  "slots": [
    {
      "bucket_start": "2026-06-15T13:00:00+02:00",
      "pv_to_home": 3.0, "battery_to_home": 0.0, "grid_to_home": 0.0,
      "pv_to_battery": 1.0, "grid_to_battery": 0.0, "pv_to_grid": 1.0,
      "home_total": 3.0, "wallbox_total": 0.0,
      "degraded": false
    }
  ]
}
```

- **Under-the-line sources** sum to `Σ HC` (the raw vendor home
  consumption): `pv_to_home` / `battery_to_home` / `grid_to_home` are the
  HC-triade verbatim.
- **Over-the-line surplus targets**: `pv_to_battery` / `grid_to_battery`
  (battery charge split) + `pv_to_grid` (export). They stack above the
  mint ceiling.
- **`home_total`** = `Σ HC` **+ a separately-metered wallbox** (#48, see
  *Wallbox topology* below) — the chart's mint ceiling (= total site
  consumption incl. the EV).
- **`wallbox_total`** = the full wallbox draw; the gap between the black
  and mint lines.
- **Lines** (iOS): black = `home_total − wallbox_total` (the base
  household line — equals `Σ HC` when the wallbox is metered separately),
  mint = `home_total` (the ceiling incl. the EV).

### Decomposition (Phase 1, `app/house_energy.compute_house_flows`)

Home-centric signs (positive = into home): `battery_kw > 0` discharge,
`< 0` charge; `grid_kw > 0` import, `< 0` export; `solar_kw` = production.

```
pv_to_home   = HC_from_pv
battery_to_home = HC_from_battery
grid_to_home = HC_from_grid

if battery charging (signed ≤ 0):
    charge_total = -battery_signed
    if grid importing (signed > 0):
        grid_to_battery = clamp(grid_signed - HC_from_grid, 0, charge_total)
        pv_to_battery   = charge_total - grid_to_battery
    else:                           # exporting → no grid charge
        grid_to_battery = 0
        pv_to_battery   = charge_total
pv_to_grid = max(0, solar - HC_from_pv - pv_to_battery)
```

The optional `pv_to_battery_power_kw` sensor, when present, **overrides**
the derived `pv_to_battery` (grid charge = remainder); a >10 % disagreement
with the derivation is logged as a warning (the sensor still wins).

### Decomposition (Phase 2, `app/house_energy.compute_house_flows_from_net`)

For setups **without** the HC-triade. Uses only the signed net-flow
meters (`solar_kw`, `battery_kw`, `grid_kw`) — same sign convention as
Phase 1. The total home load is the energy balance at the home bus, then
split by a deterministic **self-consumption priority** (PV → battery
discharge → grid for the load; PV-surplus → battery → export for the
surplus):

```
home_total      = max(0, solar + grid_signed + battery_signed)

pv_to_home      = min(solar, home_total)
battery_to_home = min(max(battery_signed, 0), home_total - pv_to_home)
grid_to_home    = home_total - pv_to_home - battery_to_home

pv_surplus      = max(0, solar - pv_to_home)
charge          = max(0, -battery_signed)
pv_to_battery   = min(pv_surplus, charge)
grid_to_battery = charge - pv_to_battery
pv_to_grid      = pv_surplus - pv_to_battery        # == grid export
```

Under-the-line sources sum to `home_total` (no separate `Σ HC`). This is
the standard self-consumption model and resolves the night-grid-charge
case correctly (the charge falls on the grid, not on absent PV).

**Deliberate choices** (delegated to the implementing session, 2026-06-17):

- **No wallbox add-back** (unlike the vendor path below): a wallbox sits
  behind the grid meter, so its draw is already inside `grid_signed` →
  `home_total` is the plain net total and the wallbox renders *within*
  the stack (like the cascade case). iOS' `baseHome = home_total −
  wallbox_total` stays consistent. (A truly separately-metered wallbox
  not behind the grid meter would under-count — the v1.6.23 iOS clamp
  keeps that from going negative; rare, accepted.)
- **`degraded` always `false`** — the reconstruction is self-consistent
  by construction; a `haushalt` meter may exclude battery/wallbox, so a
  closure check would raise false flags.
- **Battery→grid export is not a series** (parity with the vendor path);
  in a rare discharge-while-exporting slot that fraction is not drawn
  above the line (home + under-line stay correct).

### Wallbox topology (#48)

The HC-triade measures *home* consumption — and on real vendor setups
(e.g. Kostal with the wallbox on a separate phase **after** the house
meter) that triade does **not** include the wallbox. If iOS naively drew
`black = home_total − wallbox_total` with `home_total = Σ HC`, the black
line and the grid area would dip negative whenever the car charged.

The backend resolves this from the `parent_device_id` tree
(`app/topology.has_ancestor_of_type`):

- **Separate** (default — the wallbox has **no** `haushalt` ancestor): its
  draw is *not* in `Σ HC`, so the backend folds it back in:
  `home_total = Σ HC + wallbox`. The under-line stack still tops out at
  `Σ HC`, so the wallbox lands as a sliver **above** the household stack
  (black at `Σ HC`, mint at `Σ HC + wallbox`).
- **Cascade** (the user modelled `wallbox.parent_device_id` → a `haushalt`
  device): the wallbox is already inside `Σ HC`, so nothing is added
  (`home_total = Σ HC`) and it renders as the top slice **within** the
  stack (black at `Σ HC − wallbox`).

iOS needs **no** layout branch: its existing `baseHome =
home_total − wallbox_total` math is correct in both cases by construction.
The closure check (`degraded`) stays on the raw `Σ HC`, so the folded-in
wallbox never skews it. Mixed multi-wallbox setups split per device (only
the separately-metered wallboxes are added).

### `source` selection + `degraded`

- `vendor` — the HC-triade actually reported (PV + grid always, battery
  too when a battery device exists). Phase-1 decomposition.
- `allocator` — triade absent but solar+grid devices reported net power:
  Phase-2 net-flow reconstruction (below).
- `none` — no relevant telemetry at all for the day (or no solar/grid
  device) → empty `slots`; iOS shows the "needs solar + grid metering"
  empty state.
- `degraded` (per slot) — vendor path only: when an independent
  whole-home meter (`haushalt` device power) exists and `Σ HC` deviates
  from it by > 5 %. No haushalt meter ⇒ no closure check ⇒ never
  degraded. The allocator path is self-consistent by construction →
  always `false`.

## Known Phase-1 limitations

- Flows are computed on slot-**averages**, so a slot where the battery
  both charged and discharged can mis-split (smaller with `15min`).
- The full HC-triade must report for the vendor path; a partial setup
  falls back to the Phase-2 allocator (net-flow reconstruction) rather
  than showing wrong vendor zeros, and only to the empty state when not
  even solar+grid net power is available.
- Kostal-Vendor-Template auto-prefill of these slots is backlog #22
  (parked) — for now the user maps the four sensors by hand.

# Intraday grid-cost curve — cross-lane contract (#71)

The `PriceSavingsSheet` (opened from the price tap on the iOS HomeView)
gets an intraday chart next to its existing 7-day savings aggregates: a
**cumulative EUR curve of the grid-import cost incurred so far today**,
analogous to `HouseConsumptionChartView` (96 × 15-min buckets,
cumulative). This file is the single source of truth for the data
contract.

- **Backend (#71)** — buckets grid-import telemetry, prices each slot,
  serves the endpoint. *(this lane)*
- **iOS (#71)** — `CostsTodayChartView` (cumulative EUR area) above the
  Stromkosten card in `PriceSavingsSheet`; waits on this field.
- **Connector** — no code. The cost is computed entirely from existing
  grid telemetry (`energy_kwh_delta`) + the user's tariff config; this
  doc records the contract.

## v1 scope: actual line only

v1 ships **only the actual cost line**. A baseline / savings-difference
overlay is deliberately deferred (v2, below): synthesising it on the
client would duplicate the solver's cost model (Tibber dynamic / flat /
cascade sub-tariff from the #67 world) and re-run the #64 drift episode.
The backend is the single source of truth for what a kWh cost.

## Backend endpoint

`GET /api/v1/users/me/costs/today?slots=hourly|15min&day=yyyy-MM-dd`

- `slots` — bucket width, default **`15min`** (`hourly` also supported).
- `day` — local day (Europe/Berlin), default today. Buckets anchor to
  local midnight via `date_bin`, so the x-axis lines up with the wall
  clock. **Tibber realized prices are only available for *today*** (the
  Tibber API serves today + tomorrow, no history) → for a past `day`
  every grid falls back to its fixed tariff.

Response = `CostsTodayDay`:

```jsonc
{
  "date": "2026-06-21",
  "granularity": "15min",            // echoes the request
  "source": "tibber_realized",       // day summary, see below
  "slots": [
    {
      "ts": "2026-06-21T06:00:00+02:00",
      "grid_in_kwh": 1.0,            // Σ grid import this slot (≥0)
      "price_eur_per_kwh": 0.55,     // volume-weighted price paid
      "cost_eur": 0.55,             // Σ_grid import_kWh × slot-tariff
      "cost_cum_eur": 0.55,         // running total since midnight
      "source": "tibber_realized"    // per-slot tariff source
    },
    {
      "ts": "2026-06-21T06:15:00+02:00",
      "grid_in_kwh": 0.5, "price_eur_per_kwh": 0.55,
      "cost_eur": 0.275, "cost_cum_eur": 0.825,
      "source": "tibber_realized",
      "grids": [
        {
          "grid_id": "…", "label": "Netz", "kwh": 0.5,
          "price_eur_per_kwh": 0.55, "cost_eur": 0.275,
          "cost_cum_eur": 0.825, "source": "tibber_realized"
        }
      ]
    }
  ]
}
```

### Pricing

Per slot, per grid device:

```
cost_eur = Σ_grid (import_kWh × slot_tariff(grid))
```

- **import_kWh** = the grid's positive `energy_kwh_delta` summed over the
  bucket. **Export is not credited** in v1 — a pure-export slot costs 0
  (it still renders, with the grid's tariff as a continuous price line).
- **slot_tariff(grid)** reuses the same sources the solver bills against:
  - `tibber_realized` — the grid is in Tibber mode → the realized hourly
    Tibber price for that slot (`get_tibber_today_prices`, the elapsed
    half of today's curve, not the forward solver curve).
  - `flat` — the grid's `import_tariff_fixed_eur_per_kwh` (or the BDEW
    default 0.30 €/kWh when unset).
  - `cascade_sub` — the grid sits behind another grid (#67 sub-meter); it
    is billed at its own (sub-)tariff and flagged so iOS can label it.
- **price_eur_per_kwh** is the volume-weighted price actually paid that
  slot (`cost_eur / grid_in_kwh`); when nothing was imported it falls back
  to the representative (root) grid's tariff so the price line stays
  continuous.

`cost_cum_eur` is the cumulative `cost_eur` since local midnight, in
chronological order — the curve iOS draws.

### Per-grid breakdown (`grids`, #72)

Each slot also carries a `grids` array — the same numbers split per grid
device, so iOS can stack the cumulative curve per tariff (a Tibber sub-grid
behind a flat root grid, the #67 cascade world) and label which grid cost
what:

```
grids: [{ grid_id, label, kwh, price_eur_per_kwh, cost_eur,
          cost_cum_eur, source }]
```

- **Invariant:** `Σ_grids kwh == grid_in_kwh`, `Σ_grids cost_eur ==
  cost_eur`, `Σ_grids cost_cum_eur == cost_cum_eur` — exactly, every slot.
  A **single-grid user gets a one-element `grids`** whose fields equal the
  top-level totals (the top-level fields are byte-identical to before #72;
  `grids` is purely additive).
- **Membership + continuity:** one entry per grid that reported telemetry
  today, present in *every* slot (its `cost_cum_eur` carries forward in
  slots where it imported nothing) so each per-grid cumulative series is
  continuous and stacks cleanly.
- `label` is the grid device's user-facing name; `source` is *that grid's*
  tariff source (so a flat root + a Tibber/cascade sub keep distinct labels
  while the slot-level `source` stays `mixed`).

### `source` (per-slot + day summary)

Each slot's `source` is the tariff source of the grid that imported the
most that slot. The day-level `source` is:

- one of `tibber_realized` / `flat` / `cascade_sub` when every priced
  slot shared it,
- `mixed` when slots span more than one source (e.g. a flat root grid
  plus a Tibber sub-grid),
- `none` when no grid telemetry landed → empty `slots`. iOS hides the
  chart in that case (no-op until data arrives — same fail-soft pattern
  as `/me/energy/today` #41 and `/me/energy/live` #63).

## iOS lane (waits on this field)

- New model `CostsTodayDay` / `CostsTodaySlot`, endpoint `.costsToday` in
  `APIClient` / `APIClientProtocol`.
- `CostsTodayChartView` in `Features/Power/` — cumulative EUR area, EUR
  y-axis, hours x-axis — mounted above the existing Stromkosten card in
  `PriceSavingsSheet`.
- Until the backend ships: render nothing on `source == "none"`
  (fail-soft, no-op).

## v2 (deferred)

Baseline line + savings difference. Requires extending the contract with
`cost_baseline_cum_eur` and aggregating the solver's `cost_baseline_eur`
path per slot. Not in v1.
```

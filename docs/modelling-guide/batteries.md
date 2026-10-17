# Batteries and charging

An electric fleet can carry a battery, so a fleet-sizing study counts the time
vehicles lose to charging. A fleet without `battery` behaves exactly as before.

## The battery

Add a `battery` to a fleet, as the fleet's `battery` key:

| Field | Meaning |
|---|---|
| `capacity_kwh` | kWh, > 0 |
| `initial` | state of charge (SoC) at the start, 0-1, default 1.0 |
| `low` | SoC below which a freed or idle vehicle goes to charge |
| `target` | SoC charged to, > `low`, default 1.0 |
| `strategy` | `threshold` (default), `opportunity`, `swap` or `scheduled` |
| `opportunity_below` | opportunity only: an idle vehicle below it charges when a point is free |
| `schedule` | scheduled only: a record of the `schedules` section; a value > 0 is a charging window |
| `chargers` | optional list of charger names; default every charger of the matching kind |

The vehicle drains at the fleet's `power` kW: `busy` while driving (loaded, empty
or to a charger) and handling, `idle` while idle, nothing while charging. A
battery fleet's own resource has no energy row: the grid sees the chargers.

## Chargers

Declare chargers in the model's `chargers`:

| Field | Meaning |
|---|---|
| `name`, `node` | unique name; a node of the path network |
| `kind` | `charge` (default) or `swap` |
| `points` | vehicles served at once, >= 1 |
| `power_kw` | kW per point; for `swap`, the rate removed packs recharge at |
| `knee`, `taper_floor` | default 0.8 and 0.1: above `knee` the power falls linearly to `taper_floor x power_kw` at 100 % |
| `efficiency` | grid kWh = battery kWh / efficiency, default 1.0 |
| `swap_time` | swap only: a number or distribution, in `swap_time_unit` (default the model unit) |
| `spare_packs` | swap only, optional: a swap waits for a charged pack; without it packs are unlimited |

## Strategies

- **Threshold.** A vehicle released below `low` drives to a charger (empty travel),
  queues for a point, charges to `target`, then is free again. It keeps its fleet
  slot the whole time, so it counts as busy and cannot be dispatched.
- **Opportunity.** A vehicle going idle below `opportunity_below` drives to a
  charger only if a point is free there now. It stays dispatchable: a request
  ends the charge at that instant with the SoC reached. Below `low` the threshold
  rule applies.
- **Swap.** As threshold, at a `swap` station: the stay is `swap_time` and the SoC
  becomes 1.0. The removed pack recharges at the station's `power_kw`.
- **Scheduled.** During a window of the schedule, each vehicle released below
  `target` charges until `target` or the window's end, whichever comes first;
  idle vehicles start an interruptible charge when a window opens. Outside
  windows the threshold rule still applies.

A vehicle standing idle charges too, not only one just released. At the exact
instant its `idle` draw takes its SoC below `low`, or below the lower of
`opportunity_below` and `target` under opportunity, it plans its charge as if it
had just been released: the same strategy, the same charger choice, and a held
charge keeps its fleet slot. A vehicle that starts the run, comes back from a
failure or a shift, or is put back by a state already below one of them plans at
once; one that finds no charger waits for the next one down, and a held charge
that finds every slot taken tries again when one frees. A run with an idle draw
and a time limit runs to that limit; a run without one plans no idle charges,
since they would keep it running for ever. With a time limit and a reachable
charger, a fleet with an idle draw so never runs flat just by waiting for work.

A vehicle picks the charger with the least drive time plus expected wait (its place
in line behind the vehicles on a point, waiting or on their way, times the mean
stay), ties by name. It never stops mid-job: the SoC may go below zero, which counts
as one **depleted** event per crossing and is logged once per fleet. Size the
battery or `low` so the count stays at 0. A battery fleet with no charger it can
drive to gets `W_NO_CHARGER` at load, naming the node to join; its vehicles then
simply never charge.

## Results

- The `charging_summary` frame, per charger: sessions, point
  utilization, mean wait and queue, grid kWh, peak kW and energy cost. Each
  charger also has a row in the energy and cost summaries. Grid kWh is what was
  drawn in the observed window: a session still running at the end counts what
  it has drawn so far, and one spanning the warm-up counts only from the reset.
- The `battery_summary` frame, per fleet: share of time charging,
  charge trips, minimum and mean final SoC, depleted count and drain kWh.
- Scenario and optimizer keys: `charger/<name>/Grid kWh`,
  `battery/<fleet>/Charging %` and the other columns.
- Playback shows a charger at its node with its occupied points, and a SoC bar on
  each vehicle.

## Workers and gantry cranes

Every fleet kind takes the same `battery`: vehicles, workers (`kind: "worker"`) and
gantry cranes (`motion: "gantry"`). A worker drains walking to a station, working
there and standing idle, and goes to charge once its work item is released or while it
stands idle, never in the middle of one; all four strategies apply. A gantry crane
reaches a charger by a direct hop to the charger node's coordinates, so the charger
needs no path. Set `capacity_kwh` to 0 to switch the battery off again: the fleet then
runs as it did without one.

A charger inside a submodel definition is a template (node `$base` or `$bay`): each instance
gets its own copy, `<instance>/<name>`, which is what the results show.

## Not modelled

A site power cap or load management, battery ageing, charging while moving and
per-vehicle differences within a fleet. A warm-up never resets the SoC.

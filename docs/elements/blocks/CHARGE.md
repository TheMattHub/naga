Use CHARGE for electric vehicles or battery packs that take energy at a limited power while they stay at a site. Several vehicles can share one site power cap. It fits a depot, a car park or a charging bay. For a vehicle that must go to a charger as part of a fleet trip, use the fleet's battery and charger sections instead.

## Behaviour and rules

Every entity at the block is a charging session with its own need in kWh, its own power limit in kW and an optional deadline. The block re-divides the site cap whenever a session starts, fills up or reaches its deadline. Power is constant in between, so the energy is exact.

With `sharing` set to `equal`, each session gets an equal share, and what a slow session cannot use goes to the others. With `first_come`, sessions are served in order of arrival, each taking as much as it can. A `site_limit` of 0 means no cap. The `efficiency` is the share of grid energy that reaches the battery. The cap and `power_variable` are measured on the grid side.

The entity leaves when it is full, or at its time limit if one is set, whichever comes first. With `leave_when_full` off, a full entity stays until the time limit and draws nothing. On leaving, the entity carries `charged_kwh`, `charge_full` (1 or 0) and `charge_time`.

An entity with no need leaves at once as full. An entity with no power and no time limit leaves at once with nothing charged, and a warning is logged. A deadline already past sends the entity on, charged 0. There is no taper, and the cap is constant.

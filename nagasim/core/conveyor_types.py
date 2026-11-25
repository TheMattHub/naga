"""Conveyor types: default dimensions of a belt, roller, chain, pallet or mdr conveyor
(conveyor system A).

Data only. On a typed CONVEYOR (`type` set) an item with no size and no `part_length` is
`zone_length` long (D1355; the belt's cells are its own `cell` param); speed, width, height
and roller pitch are the type's defaults for the layout and the 3D view.

Sources, all read 2026-11-21 (one value picked inside each vendor range, a common mid value):
- belt: Hytrol Model TA slider bed, https://www.cisco-eagle.com/uploads/Hytrol-Spec-Belt/Model-TA.pdf
  "BELT SPEED - Constant 65 FPM." (0.33 m/s); 24 in belt (0.61 m); slider bed, no rollers (pitch 0).
- roller: Hytrol 190-NSP, https://www.cisco-eagle.com/uploads/Hytrol-Spec-LiveRoller/190-NSP-Specs.pdf
  "Constant 65 FPM." (0.33 m/s); 21 in between rails (0.533 m); "spaced every 3 in." (0.0762 m).
- chain: Interroll PM 9720, https://www.interroll.com/fileadmin/products/product_data/PM-9720/PM9720_EN.pdf
  "Conveyor speed 0.1 to 0.5 m/s" (pick 0.3); "Module width (TW) 1012, 1137 mm" (1.012);
  "Conveying height (TOC) 350 to 1200 mm" (mid 0.775); "Module length (ML) 1000 to 5600 mm".
- pallet: Interroll PM 9711, https://www.interroll.com/fileadmin/products/product_data/PM-9711/PM9711_EN.pdf
  "Conveyor speed 0.22 m/s"; "Module conveying width (CW) 860, 1060, 1295 mm" (1.06);
  "Conveying height (TOR) 95" (0.095); "Roller pitch (P) 190.5 and 222.25 mm" (0.1905);
  "Module length (ML) 300 to 2980 mm".
- mdr: Interroll RM 8310, https://www.interroll.com/fileadmin/products/product_data/RM-8310/RM8310_EN.pdf
  "Conveyor speed 0.1 to 1.0 m/s" (pick 0.5); "BF Rated width 420, 620, 840 mm" (0.62);
  "P Roller pitch 60/90/120/150 mm" (0.09). Zone length from Hytrol 190-E24EZ,
  https://www.cisco-eagle.com/uploads/Hytrol-Spec-Accumulation/190-E24EZ-Specs.pdf
  "18 in., 24 in., 30 in., and 36 in. zones" (24 in, 0.61 m).

Fields no vendor page states, each marked in the table:
- gap, every type: no vendor value: 0 (the pallet sheet says only "installed very close to each other").
- belt zone_length: no vendor value: the Hytrol 190-E24EZ 24 in zone, 0.61 m.
- roller zone_length (not zoned): no vendor value: the same Hytrol 24 in zone, 0.61 m.
- belt, roller and mdr height: not a vendor value but an owner choice (D1434): 0.762 m (30 in), the
  knuckle height for loading cartons by hand. The vendors' minimum elevation (6 in, 0.152 m) drew these
  lines 15 cm off the floor. Sources: Cisco-Eagle, "How to Use Conveyors to Increase Warehouse
  Ergonomics", https://www.cisco-eagle.com/blog/2017/12/07/conveyors-and-ergonomics (about 30 in as the
  average knuckle-height target); CCOHS, "Conveyors - Ergonomics",
  https://www.ccohs.ca/oshanswers/ergonomics/conveyor_ergonomics.html (65-120 cm suits most of the workforce).
- chain and pallet zone_length: no vendor value: the module length, its lower end, 1.0 m.
"""
from __future__ import annotations

from typing import NamedTuple


class ConveyorType(NamedTuple):
    speed: float            # m/s
    width: float            # m
    height: float           # m
    zone_length: float      # m; the length of an unsized item on a typed belt
    roller_pitch: float     # m; 0 for a belt or chain (no rollers)
    gap: float              # m; free belt kept between two items


TYPES: dict[str, ConveyorType] = {
    "belt": ConveyorType(speed=0.33, width=0.61, height=0.762, zone_length=0.61,
                         roller_pitch=0.0, gap=0.0),
    "roller": ConveyorType(speed=0.33, width=0.533, height=0.762, zone_length=0.61,
                           roller_pitch=0.0762, gap=0.0),
    "chain": ConveyorType(speed=0.3, width=1.012, height=0.775, zone_length=1.0,
                          roller_pitch=0.0, gap=0.0),
    "pallet": ConveyorType(speed=0.22, width=1.06, height=0.095, zone_length=1.0,
                           roller_pitch=0.1905, gap=0.0),
    "mdr": ConveyorType(speed=0.5, width=0.62, height=0.762, zone_length=0.61,
                        roller_pitch=0.09, gap=0.0),
}

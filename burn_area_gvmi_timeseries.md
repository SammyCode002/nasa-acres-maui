# Burn-area GVMI time series

**File:** `burn_area_gvmi_timeseries.py`
**Project:** NASA ACRES Maui (github.com/SammyCode002/nasa-acres-maui)
**Author:** Samuel Dameg (SammyCode002)

## What it builds

A chart of mean GVMI over time (2022 to present), two lines:
- inside the 2023 Kula burn perimeter, and
- in an unburned control ring 100 m to 1500 m just outside the burn.

It reads the burn polygon from `data/burn_perimeter/kula_2023_perimeter.geojson` and the corridor AOI from `aoi/corridor.geojson`, computes the monthly means straight from Earth Engine, and saves `data/figures/burn_area_gvmi_timeseries.pdf` (and a `.png`).

## What the chart actually shows (read this before using it)

I built this expecting a clean GVMI dip at the fire. It is not there, and that is a real result worth being straight about.

- The burn area sits consistently **above** the control the whole record, before and after the fire. That is geography: the burn is upcountry where it is greener, so it is moister than land just downslope.
- Both lines swing hard with the **dry season** (low late summer, high winter and spring).
- At Aug to Sep 2023 the burn-above-control gap narrows, but it narrows just as much at other dry-season troughs, so the fire is not separable from seasonal noise in this metric.

Why: grassland burns and regrows within weeks to a couple of months, and GVMI is a moisture index, not a burn index. By the time of the monthly composites the grass had largely recovered, so the moisture signal looks normal again.

## So what is the fire validation

The fire is validated by **dNBR**, the purpose-built burn ratio (`dnbr_burnscar.py`). It caught the burn at the time of the fire and gave a 978-acre perimeter that matches the known footprint. That is the strong fire result.

This GVMI chart validates a **different** thing, which is still useful for Ana:
- GVMI tracks the real seasonal moisture cycle (clear annual sawtooth), so the maps respond to actual surface conditions.
- The burned grassland recovered fast; there is no lasting moisture scar, which is consistent with Hawaiian grass regrowth.

Pairing the two is the honest story: the burn shows up in the burn-sensitive index (dNBR), and the moisture index (GVMI) shows the seasonal cycle and a quick recovery.

## How it works

1. Read the AOI and the burn polygon, build the control ring (`burn.buffer(1500)` minus `burn.buffer(100)`).
2. For each month, build a median GVMI composite (SCL cloud mask, GVMI from B8 and B11) and reduce the mean over the burn and over the ring, all server-side in one round trip.
3. Plot both lines, mark the fire date, save PDF and PNG.

## Key decisions (and why)

- **Ring control, not the whole corridor.** The burn is upcountry and moister; the lower corridor is much drier. Comparing to the whole corridor is apples-to-oranges. A ring hugs the same elevation and vegetation.
- **Start in 2022.** A pre-fire baseline shows the normal burn-above-control offset, so any fire effect would show as that gap collapsing. It does not, which is the finding.
- **Compute from Earth Engine, not the Drive tiles.** Same Sentinel-2 source, identical numbers, no 35-file download.

## Notes / caveats

- Do not present this as a fire-detection chart. Present it as seasonal-moisture validation plus evidence of fast post-fire recovery, and use dNBR for the fire itself.
- A fully clouded month would show as a gap (None becomes NaN).
- Tunable knobs: the control ring distances and the date range.

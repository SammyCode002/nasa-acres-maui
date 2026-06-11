# AOI: Kula-to-Kihei corridor (single source of truth)

**File:** `aoi/corridor.geojson`
**Project:** NASA ACRES Maui (github.com/SammyCode002/nasa-acres-maui)
**Author:** Samuel Dameg (SammyCode002)

## What this is

One GeoJSON file that defines the study-area extent for the whole project. Every script reads the AOI from here instead of carrying its own bounding box, so the corridor lives in exactly one place. If the extent ever changes, it changes here and every downstream script follows.

Consumers:
- `clip_climate_normals.py` (clips the climate covariate grids to this AOI)
- `gvmi_baseline.py` (the Sentinel-2 baseline; point its AOI at this file)
- future LFMC inference and evaluation code

## The AOI

A single rectangle covering the mauka-to-makai corridor from upper Kula on the Haleakala slopes down to Waiohuli Kai and the South Kihei coast.

- CRS: EPSG:4326 (WGS84 lon/lat, decimal degrees)
- Bounds [west, south, east, north]: `[-156.470, 20.670, -156.290, 20.830]`
- Roughly 18.8 km east-to-west by 17.8 km north-to-south (about 335 km2)

It is stored as the feature whose `properties.role == "aoi"`. Scripts select that feature, which keeps the AOI separate from the cross-validation blocks below (also in the file).

### Does it need adjusting against the ArcGIS Pro extent?

The rectangle is a sensible starting corridor and it covers the features that matter:
- The 2023 Upcountry/Kula fire area sits inside the northeast (upper Kula) part.
- South Kihei Road and the Kulanihakoi and Waipuilani gulch outlets sit inside the southwest (coastal) part.
- Waiohuli Kai on the coast is near the southwest corner.

Check it against your ArcGIS Pro corridor map and nudge if any of these fall on or outside an edge:
- If the full 2023 fire perimeter runs past the north (20.830) or east (-156.290) edge, extend those.
- If you want more of the upper watershed above Kula, raise the north edge.
- If the coastal restoration site or a gulch outlet sits west of -156.470, extend the west edge.

To change the AOI, edit the four corner coordinates of the `role: "aoi"` feature in `corridor.geojson`. Nothing else needs to change; the scripts re-read it.

## Block split for leakage-safe spatial cross-validation

When we later evaluate the LFMC model, a plain random train/test split leaks: nearby pixels are correlated, so a random split lets the model "see" the test area through its neighbors and the score comes out too high. The Johnson et al. (2025) LFMC paper hit exactly this (a small but significant spatial autocorrelation in their residuals) and recommended spatial partitioning. So we evaluate by spatial blocks instead.

The corridor is divided into three blocks along the mauka-to-makai (elevation) gradient. Elevation runs roughly east (upcountry, high) to west (coast, low), so the split is by longitude:

| Block | Position | Longitude range | CV fold |
|---|---|---|---|
| `upper_kula` | mauka (east, high) | -156.343333 to -156.290000 | 3 |
| `mid_slope` | mid-slope (center) | -156.406667 to -156.353333 | 2 |
| `coastal_kihei` | makai (west, low) | -156.470000 to -156.416667 | 1 |

All three span the full corridor latitude (20.670 to 20.830).

Between each pair of adjacent blocks there is a **0.010 degree (~1 km) buffer gap** that belongs to no block. That gap is wider than a single 250 m cell, so no cell in one fold sits next to a cell in another fold. That is what makes the split leakage-safe: train on two blocks, test on the held-out block, and the buffer keeps the folds from touching.

The blocks are stored as the features with `properties.role == "cv_block"`. They are not used by the clip script (it clips to the full AOI); they are there for the evaluation step later.

## Notes

- The buffer strips are intentionally unused ground, not a fourth fold. Their only job is to separate the folds.
- If you change the AOI bounds, recompute the block boundaries so they still tile the new width with the same buffer. The arithmetic: usable width = total width minus (2 x buffer); each block = usable width / 3.

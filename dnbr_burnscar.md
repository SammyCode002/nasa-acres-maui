# dNBR burn scar and perimeter (2023 Kula fire)

**File:** `dnbr_burnscar.py`
**Project:** NASA ACRES Maui (github.com/SammyCode002/nasa-acres-maui)
**Author:** Samuel Dameg (SammyCode002)

## Why this exists

No public agency published a polygon for the 2023 Upcountry (Kula) fire. It was a state and county incident and never made it into the federal interagency perimeter datasets, and the one community dashboard that has a perimeter covers only Lahaina. So instead of borrowing a polygon, we derive the burn extent straight from satellite data.

## What it builds

1. A dNBR GeoTIFF over the corridor, exported to your Drive folder `nasa_acres_maui_gvmi` as `dNBR_kula_2023.tif` (bands: dNBR, NBR_pre, NBR_post).
2. A clean burn-perimeter polygon, saved locally to `data/burn_perimeter/kula_2023_perimeter.geojson`.

## How it works

1. Read the corridor boundary from `aoi/corridor.geojson` (same boundary as everything else).
2. Build a pre-fire median composite (2023-07-10 to 2023-08-08) and a tight post-fire one (2023-08-13 to 2023-08-28), masking clouds with the SCL band.
3. Compute NBR on each: `NBR = (B8 - B12) / (B8 + B12)`.
4. dNBR = NBR_pre minus NBR_post. Fire removes vegetation and chars the ground, which lowers NBR, so the difference is positive and bright where it burned.
5. Threshold dNBR at 0.27 (moderate-low and up), drop patches smaller than 50 pixels (the seasonal-browning specks), keep the single largest contiguous patch, and vectorize it to one polygon.

## Key decisions (and why)

- **Tight post-fire window (Aug 13 to 28).** This corridor is dry grass that browns through the dry season, and that browning also lowers NBR. A short window right after the fire keeps the seasonal signal small so the burn stands out. This run used 8 clear pre-fire and 6 clear post-fire scenes.
- **B8 and B12 for NBR.** Near-infrared paired with the longer SWIR (about 2.2 microns) is the standard, most sensitive pair for char and lost vegetation.
- **Keep the largest contiguous patch.** Seasonal browning leaves scattered small specks above the threshold; the fire is one big connected blob. Filtering small patches and taking the largest isolates the fire.
- **Threshold 0.27.** Moderate-low and up. It excludes the faint diffuse signal (mostly seasonal) while keeping the real burn. It is a config value, easy to tune.

## Result and cross-check

The derived perimeter came out to about **978 acres**. The reported 2023 Kula and Olinda Upcountry fires burned on the order of 1,000 acres, so the extent checks out. Always eyeball the polygon against the imagery and the known footprint before trusting it.

## Symbolize the dNBR raster in ArcGIS

Display the dNBR band (band 1) as a stretch and read severity with the USGS breakpoints:

| dNBR | Severity |
|---|---|
| below 0.10 | unburned |
| 0.10 to 0.27 | low |
| 0.27 to 0.44 | moderate-low |
| 0.44 to 0.66 | moderate-high |
| above 0.66 | high |

## Success criteria

- `dNBR_kula_2023.tif` lands in Drive with 3 float bands
- `data/burn_perimeter/kula_2023_perimeter.geojson` holds one polygon
- the polygon area is in the right ballpark for the known fire (a few hundred to ~1,000 acres)

## Notes / caveats

- dNBR is a derived index, not an official perimeter. Treat it as a best-available estimate and note the method (Sentinel-2 dNBR, threshold 0.27, largest patch) when you show it.
- The threshold and the post-fire window are the two knobs. A lower threshold grows the polygon and pulls in more seasonal signal; a tighter window cuts seasonal drift but risks cloud gaps.
- The perimeter feeds `burn_area_gvmi_timeseries.py` (mean GVMI inside the burn over time) and the ArcGIS overlay figure.
- Reads the same `aoi/corridor.geojson` as the other scripts, so everything lines up.

# GVMI Baseline Map Generator

**Project:** NASA ACRES Maui (github.com/SammyCode002/nasa-acres-maui)
**File:** `gvmi_baseline.py`
**Purpose:** Generate the first baseline maps, the simple spectral-index layer the OlmoEarth LFMC model has to beat.

---

## What this builds

Monthly vegetation-moisture maps over the Kula-to-Kihei corridor, **Aug 2023 to present**, from Sentinel-2. Each month becomes one GeoTIFF in your Google Drive with three bands:

- **GVMI** (primary) - vegetation water content. Matches the index NASA ACRES already used on Maui, so you're extending known work, not inventing a new yardstick.
- **NDMI** (secondary) - a second moisture view for comparison.
- **NDVI** (context) - greenness, helps separate "dry" from "no vegetation."

Aug 2023 is the start on purpose: it captures the Upcountry fire month, so those burn-area pixels give you a built-in sanity check (they should read dry).

---

## How it works (the pipeline)

1. **Initialize** Earth Engine (auto-runs interactive auth if your token is missing or expired).
2. **Load the AOI** from `aoi/corridor.geojson`, the single shared boundary file. Falls back to a bounding box if that file isn't there yet.
3. **Load Sentinel-2** surface reflectance, filtered to the AOI and date range, dropping scenes over 60% cloud.
4. **Mask clouds** per pixel using the SCL band (removes cloud, shadow, cirrus, defective, snow).
5. **Add indices** (GVMI, NDMI, NDVI) to every image.
6. **Monthly median composite** per month, clipped to the AOI. Median fills cloud gaps and ignores stray bad pixels.
7. **Export** each month to Drive as a float GeoTIFF.

---

## Key decisions (and why)

- **One AOI file, read by everything.** The boundary lives in `aoi/corridor.geojson`. This script and the climate-grid clip both read it, so the GVMI maps, climate covariates, and field sites all line up on the same boundary. A drifting box between tools is a quiet form of leakage. The loader accepts a FeatureCollection, a Feature, or a bare geometry, so a rectangle now and the digitized watershed later both just work. When the file is a FeatureCollection it picks the feature tagged `role: "aoi"`, so the cross-validation blocks in the same file are never mistaken for the boundary.
- **GVMI rescales B8/B11 by 10000 first.** GVMI's constants (0.1, 0.02) assume reflectance in 0-1. Sentinel-2 stores it as integers x10000, so we rescale or the math is meaningless. NDMI/NDVI are pure ratios, so scaling cancels and they use raw bands.
- **Median, not mean or single-date.** Single dates are cloud-gapped; mean gets dragged by outliers. Per-pixel median across the month is the robust choice.
- **SCL-based masking.** Simpler and good enough for a baseline. (The s2cloudless probability collection is more aggressive if we need it later.)
- **20 m export scale.** Matches the SWIR band's native resolution, so we're not faking 10 m detail the moisture signal doesn't have.
- **Empty months are skipped and logged**, not exported as blank tiles.

---

## How to run

```bash
pip install earthengine-api
earthengine authenticate          # one-time, opens a browser
```

Then edit the **Config block** at the top of `gvmi_baseline.py`:

- `EE_PROJECT` - your Earth Engine / Google Cloud project id (required).
- `AOI_PATH` - path to the shared boundary file. Default `aoi/corridor.geojson`. Run the script from the repo root so this relative path resolves.
- `AOI_FALLBACK_BBOX` - used only if `AOI_PATH` is missing, so a first pass still runs before the GeoJSON exists. Format `[west, south, east, north]`.
- `DRIVE_FOLDER` - where the GeoTIFFs land in your Drive.

Run it:

```bash
python gvmi_baseline.py
```

Watch progress in the **Tasks tab** at code.earthengine.google.com, or set `MONITOR_TASKS = True` to poll from the script.

---

## Debug logging (your 4x4 standard)

Every major step is wrapped by the `debug_log` decorator, which logs **inputs, outputs, timing, and status** on each call. If a run dies, the log already shows which function failed, with what inputs, and after how long. The per-pixel server-side functions (`mask_s2_clouds`, `add_indices`) are intentionally left undecorated because they run inside GEE's `.map()` and never execute locally.

---

## Success criteria

- One GeoTIFF per month with clear data in `DRIVE_FOLDER`
- Each file has 3 float bands: GVMI, NDMI, NDVI
- GVMI values fall within roughly [-1, 1]
- Burn-area pixels in/after Aug 2023 trend dry (sanity check)

---

## Next steps

1. **Build `aoi/corridor.geojson`** (the shared boundary + documented block-split scheme), then run this script so it reads from it instead of the fallback box.
2. **Pull the GeoTIFFs into ArcGIS Pro**, symbolize GVMI (dry = red, wet = green), and eyeball the 2023 Upcountry burn area.
3. **Hand the data prep to Noah** (Sentinel-2 stack + fire perimeters) so the inputs are ready when OlmoEarth access lands.
4. **When OlmoEarth is live**, run its LFMC inference over the same corridor and months, then compare against this baseline using the metric plan (RMSE/R² plus signed bias, dry-class recall, Spearman, disaggregated by vegetation/elevation/season).

---

## Notes / caveats

- GVMI is a **proxy**, not calibrated LFMC. It tracks moisture but isn't a moisture percentage. That's exactly why it's the baseline and not the deliverable.
- This is a starting baseline. Swapping in cloud-probability masking or harmonizing across Sentinel-2 processing-baseline changes are easy follow-ups if the composites look noisy.

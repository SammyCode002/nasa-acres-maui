# LFMC Model Input Specification

What the Ai2 `lfmc` pipeline (Galileo encoder) actually consumes per labelled
point, traced from source. This is the contract our own GEE export
(`export_lfmc_tifs.py`) has to satisfy so we can regenerate the per-point image
stacks ourselves instead of pulling them from the private `gs://presto-tifs`
bucket.

Everything below is cited to a file and line in the local clones:

- `lfmc-ai2/` (the Ai2 LFMC repo)
- `lfmc-ai2/submodules/galileo/` (the Galileo encoder + the Earth Engine export code), referred to below as `galileo/`

Read this top to bottom once. The short version: each point becomes **one
multi-band GeoTIFF** whose channels are `T` monthly timesteps of 18 dynamic
bands interleaved, followed by 16 static-space bands, followed by 1 static
band. The dataset loader turns that into four arrays (`s_t_x`, `sp_x`, `t_x`,
`st_x`) plus a month vector, normalizes them, and feeds them to the encoder.

---

## 1. The four arrays the model sees

The encoder consumes four tensors plus a month vector. Channel counts are fixed
and **must stay distinct from each other**, because the normalizer figures out
which array is which purely from its last-axis size
(`galileo/src/data/dataset.py:110`, the
`assert len(SPACE_TIME_BANDS) != len(SPACE_BANDS) != len(TIME_BANDS) != len(STATIC_BANDS)`,
and `galileo/src/data/dataset.py:176`, `self.shift_div_dict[x.shape[-1]]`).

| Array    | Shape after load   | Channels | What it is |
|----------|--------------------|----------|------------|
| `s_t_x`  | `[H, W, T, 13]`    | 13 | Space-time: S1 (2) + S2 (10) + NDVI (1) |
| `sp_x`   | `[H, W, 16]`       | 16 | Static space: SRTM (2) + Dynamic World (9) + WorldCereal (5) |
| `t_x`    | `[T, 6]`           | 6  | Time series: ERA5 (2) + TerraClimate (3) + VIIRS (1) |
| `st_x`   | `[18]`             | 18 | Static: LandScan (1) + location x,y,z (3) + DW static (9) + WC static (5) |
| `months` | `[T]`              | -  | Integer month index 0-11 per timestep |

After the dataset's crop, `H = W = output_hw = 32` and `T = output_timesteps =
12` by default (`lfmc/main/finetune_model.py:67-76`). The GeoTIFF is exported
**larger** than this and the loader center-crops H/W and tail-slices T (see
section 6).

The channel-count math, all from `galileo/src/data/earthengine/`:

- `s_t_x` 13 = S1 `["VV","VH"]` (`s1.py:9`) + S2 `["B2","B3","B4","B5","B6","B7","B8","B8A","B11","B12"]` (`s2.py:49-60`) + `"NDVI"` appended at read time (`galileo/src/data/dataset.py:61`, `SPACE_TIME_BANDS = EO_SPACE_TIME_BANDS + ["NDVI"]`).
- `t_x` 6 = ERA5 `["temperature_2m","total_precipitation_sum"]` (`era5.py:8`) + TerraClimate `["def","soil","aet"]` (`terraclimate.py:8`) + VIIRS `["avg_rad"]` (`viirs.py:12`).
- `sp_x` 16 = SRTM `["elevation","slope"]` (`srtm.py:6`) + DW 9 bands (`dynamic_world.py:5-17`) + WC 5 bands (`worldcereal.py:5-13`).
- `st_x` 18 = LandScan `["b1"]` (`landscan.py:6`) + location `["x","y","z"]` (`eo.py:89`) + DW-static 9 + WC-static 5 (`galileo/src/data/dataset.py:93-95`).

---

## 2. Sensors and bands per sensor

All band lists are defined in `galileo/src/data/earthengine/` and assembled in
`eo.py:72-92`.

### Sentinel-1 (`s1.py`)
- Collection `COPERNICUS/S1_GRD`, IW mode.
- Bands: `VV`, `VH` (`s1.py:9`).
- One orbit pass only (ascending or descending, whichever the first scene has) so coverage stays consistent (`s1.py:32-34`).
- Per-timestep value is the **median** of scenes closest to the timestep mid-date, with a 31-day search pad because S1 is sparse (`s1.py:51-59`, `eo.py:191-193`).

### Sentinel-2 (`s2.py`)
- Collection `COPERNICUS/S2_HARMONIZED` (`s2.py:13`).
- Bands kept: `B2,B3,B4,B5,B6,B7,B8,B8A,B11,B12` (10 bands). `B1,B9,B10` are dropped (`s2.py:49-61`).
- Custom cloud and shadow scoring, then a `qualityMosaic` composite per timestep (`s2.py:66-89`). Not a plain median.

### ERA5-Land monthly (`era5.py`)
- Collection `ECMWF/ERA5_LAND/MONTHLY_AGGR` (`era5.py:7`).
- Bands: `temperature_2m`, `total_precipitation_sum` (`era5.py:8`).
- Pulled by calendar month of the timestep start date (`utils.py:40-76`, `get_monthly_data`).

### TerraClimate (`terraclimate.py`)
- Collection `IDAHO_EPSCOR/TERRACLIMATE` (`terraclimate.py:7`).
- Bands: `def` (climate water deficit), `soil` (soil moisture), `aet` (actual ET) (`terraclimate.py:8`).
- Closest image to timestep mid-date, ocean unmasked to 0 (`terraclimate.py:24-36`).

### VIIRS nighttime lights (`viirs.py`)
- Collection `NOAA/VIIRS/DNB/MONTHLY_V1/VCMCFG` (`viirs.py:11`).
- Band: `avg_rad` (`viirs.py:12`).
- Monthly, with hardcoded fallbacks for missing months (Oct 2023 -> Nov 2023; anything past 2024-05 clamps) (`viirs.py:22-34`).

### SRTM (`srtm.py`) - static
- Collection `USGS/SRTMGL1_003` (`srtm.py:5`).
- Bands: `elevation`, and `slope` computed from it via `ee.Terrain.slope` (`srtm.py:6,13-17`).

### Dynamic World (`dynamic_world.py`) - static here
- Collection `GOOGLE/DYNAMICWORLD/V1` (`dynamic_world.py:23`).
- 9 class-probability bands renamed `DW_water, DW_trees, DW_grass, DW_flooded_vegetation, DW_crops, DW_shrub_and_scrub, DW_built, DW_bare, DW_snow_and_ice` (`dynamic_world.py:5-17`).
- **Mean** over the export window (`dynamic_world.py:28`). Used twice: once as a spatial map in `sp_x`, once spatially averaged into `st_x` (`galileo/src/data/dataset.py:536-543`).

### WorldCereal (`worldcereal.py`) - static
- Collection `ESA/WorldCereal/2021/MODELS/v100` (`worldcereal.py:20`).
- 5 bands renamed `WC_temporarycrops, WC_maize, WC_wintercereals, WC_springcereals, WC_irrigation` (`worldcereal.py:5-13`).
- Mosaic per product, ocean unmasked to 0 (`worldcereal.py:21-34`). Same double use as DW (map in `sp_x`, averaged into `st_x`).

### LandScan (`landscan.py`) - static
- Collection `projects/sat-io/open-datasets/ORNL/LANDSCAN_GLOBAL` (`landscan.py:10`).
- Band: `b1` (population) (`landscan.py:6`).
- Clamps to 2022 if the label date is later (`landscan.py:16-21`).

### Location (computed, not a sensor)
- `x, y, z` Cartesian unit vector from the tile's center lat/lon (`eo.py:89`, computed in `galileo/src/data/dataset.py:541`, `to_cartesian`). Never exported in the TIF, added during TIF-to-array conversion.

---

## 3. Temporal window relative to the label date

This is the part with the most reconstruction risk, so read carefully.

**The window is one year of monthly timesteps ending at (label month + ~1
month).** It is derived per point from the label's `sampling_date` by
`pad_dates` (`lfmc/core/padding.py:7-12`):

```
new_end   = sampling_date + 30 days, snapped to the last day of that month
start     = first day of the same month, one year earlier
```

Worked example, label `2019-07-17`:
- `+30d` -> `2019-08-16` -> snap to month end -> `2019-08-31` (window end)
- one year before that month, day 1 -> `2018-08-01` (window start)
- window = `2018-08-01 .. 2019-08-31`

The dynamic stack is then built by stepping `DAYS_PER_TIMESTEP = 30` days from
start to end (`galileo/src/data/config.py:3`, loop in `eo.py:196-227`,
`while cur_end_date <= end_date`). A ~13-month span at 30-day steps yields
**13 raw timesteps**; the loader then keeps only the last
`output_timesteps = 12` (section 6). The model's `NUM_TIMESTEPS` is 12
(`galileo/src/data/config.py:4`).

The month vector is built from the window start month, not the band data
(`lfmc/core/dataset.py:150-154`, the LFMC override of `month_array_from_file`):

```python
start_month = pad_dates(sampling_date).start.month   # 8 for the example
months = np.fmod(np.arange(start_month - 1, start_month - 1 + T), 12)
```

So for the example with T=13: `[7,8,9,10,11,0,1,2,3,4,5,6,7]` (Aug..Aug), and
after tail-slicing to 12: months `[8,9,10,11,0,1,2,3,4,5,6,7]`.

**Cadence quirk to reproduce, not fix:** ERA5, VIIRS, and TerraClimate are
fetched by the *calendar month of the 30-day step's start date*
(`utils.py:52-56`). Because 30-day steps drift off calendar months over a year
(Aug 1, Aug 31, Sep 30, ...), consecutive timesteps can land in the same
calendar month or skip one. The Ai2 code comments acknowledge this
(`utils.py:43-51`). We replicate it by reusing their stacking code, so our
tiles drift the same way theirs do.

> **Open question for Ana (Q1).** `pad_dates` is used by the loader to set
> `start_month`, but the *actual* export window that produced the real h5pys is
> not in this repo (no exporter calls `pad_dates`; `EarthEngineExporter` uses a
> fixed 2022-2023 range, `galileo/src/data/config.py:10-11`, `eo.py:63-64`).
> We are *inferring* that the per-point TIFs were exported over the `pad_dates`
> window at 30-day cadence. Need confirmation of (a) the exact start/end rule
> and (b) the raw timestep count `T` in the real tiles. If they used a
> different window our regenerated tiles would still load (the loader
> tail-slices to 12) but would not be band-for-band identical to theirs.

---

## 4. Spatial patch size and resolution

- **Tile footprint:** 1000 m x 1000 m, from `EXPORTED_HEIGHT_WIDTH_METRES = 1000` (`galileo/src/data/config.py:12`), built as a center box of `±500 m` via `EEBoundingBox.from_centre(lat, lon, 500)` (`eo.py:301,415-419`, `ee_bbox.py:109-126`).
- **Export scale:** 10 m (`eo.py:367`, `scale=10`). So the raw tile is ~`100 x 100` pixels.
- **CRS:** the export does not pin a CRS in batch mode (`eo.py:361-370`); url mode uses GEO_TIFF. For our own export we pin `EPSG:4326` to match the rest of the Maui pipeline.
- **Model crop:** the loader center-crops to `output_hw = 32` pixels (`lfmc/core/dataset.py:209-224`). So only the central 320 m x 320 m actually reaches the encoder by default. Native resolutions (S1 10 m, S2 10-20 m, SRTM 30 m, DW/WC 10 m, ERA5 ~11 km, TerraClimate ~4.6 km, VIIRS ~500 m, LandScan ~1 km) are all resampled onto the 10 m export grid; the coarse climate bands are effectively constant across the small tile.
- **Patch size** for the encoder is 16 (`lfmc/main/finetune_model.py:77-80`), so a 32 px tile is a 2x2 grid of patches.

---

## 5. Normalization: what, where, from what

- **Stats file:** `galileo/config/normalization.json` (2.3 KB), loaded by `Dataset.load_normalization_values` (`galileo/src/data/dataset.py:697-710`) and wrapped in a `Normalizer(std=True, ...)` (`lfmc/main/create_h5pys.py:13-15`).
- **Keys are channel counts.** The JSON top-level keys are `"13"`, `"16"`, `"6"`, `"18"` plus `"total_n"`/`"sampled_n"`. Each holds a per-band `mean` and `std`. This is why the four arrays must have distinct channel counts: that count is the lookup key (`galileo/src/data/dataset.py:113-177`).
- **How values were computed:** mean/std over a 10,000-tile sample of the Ai2 training set (`total_n: 127155, sampled_n: 10000`), via `compute_normalization_values` (`galileo/src/data/dataset.py:712-762`). We do **not** recompute these; we reuse the file so our tiles land in the same normalized space the pretrained head expects.
- **Two normalization styles, mixed per band** (`galileo/src/data/dataset.py:113-168`):
  - Most bands: fixed shift/divide constants defined alongside each sensor (for example S1 `shift 25 / div 25` in `s1.py:11-12`, S2 `div 1e4` in `s2.py:62-63`, ERA5 in `era5.py:13-14`).
  - The `std_bands` set (all S2/S1 reflectance bands, SRTM, all time bands, LandScan): replaced with a `mean ± 2*std` min-max derived from the JSON stats (`std_multiplier = 2`, `dataset.py:123-168`). NDVI and the DW/WC probability bands keep their fixed constants.
- **Applied at read time**, after slicing, casting to float16: `DatasetOutput.normalize` (`galileo/src/data/dataset.py:196-205`), dispatched by `Normalizer.__call__` on `x.shape[-1]` (`dataset.py:175-177`).
- **Label normalization:** the LFMC target is divided by `MAX_LFMC_VALUE = 302` and clipped to `[0,1]` (`lfmc/core/const.py:60`, `lfmc/core/dataset.py:202`).

---

## 6. The h5py cache schema

The h5py files are **not** a proprietary artifact. They are a cached
TIF-to-array conversion. `save_h5py` writes exactly four datasets, no
attributes or metadata keys (`galileo/src/data/dataset.py:623-629`):

```python
with h5py.File(folder / f"{tif_stem}.h5", "w") as hf:
    hf.create_dataset("s_t_x", data=s_t_x)   # [H, W, T, 13]  float16
    hf.create_dataset("sp_x",  data=sp_x)    # [H, W, 16]     float16
    hf.create_dataset("t_x",   data=t_x)     # [T, 6]         float16
    hf.create_dataset("st_x",  data=st_x)    # [18]           float16
```

| Key     | Shape         | dtype   | Notes |
|---------|---------------|---------|-------|
| `s_t_x` | `[H, W, T, 13]` | float16 | NDVI already appended as channel 13 |
| `sp_x`  | `[H, W, 16]`    | float16 | |
| `t_x`   | `[T, 6]`        | float16 | already spatially averaged from the TIF |
| `st_x`  | `[18]`          | float16 | location x,y,z already injected |

- dtype float16 comes from the `.astype(np.half)` at the end of every array in `_tif_to_array` (`galileo/src/data/dataset.py:560-564`).
- `H, W, T` here are the **raw exported** sizes (~100, ~100, ~13), before cropping.
- **months, lat, lon, and the LFMC label are not stored.** They are recovered at read time from the label CSV via `stem_to_sample` keyed on the filename stem (`lfmc/core/dataset.py:140-154,201-202`).
- **Filename = `<sorting_id>.h5`** (and `<sorting_id>.tif` for the TIFs). The LFMC dataset looks files up by sorting_id, not by a `dates=` string (`lfmc/core/dataset.py:130-133`). Note this differs from the Galileo base class, which parses `dates=` out of the filename; the LFMC subclass overrides that (`lfmc/core/dataset.py:150-154`).

### Read-time slicing (`read_and_slice_h5py_file`, `galileo/src/data/dataset.py:657-678`)
- `start_t = total_t - output_timesteps` -> **tail-slice**, keeps the most recent 12 timesteps (LFMC override `return_subset_indices`, `lfmc/core/dataset.py:209-224`).
- `start_h, start_w = (total - 32) / 2` -> **center-crop** H and W.
- months are computed for the full `T` then sliced the same way.

### TIF channel layout (what our export must produce)
From `_tif_to_array` (`galileo/src/data/dataset.py:492-548`), a TIF band axis is:

```
[ T * 18 dynamic bands ]  ++  [ 16 space bands ]  ++  [ 1 static band ]
```

- The `T*18` block is `(timestep, channel)` interleaved, reshaped `(t c) h w -> h w t c` with `c = 18` (`dataset.py:511-516`). Per-timestep order is exactly `ALL_DYNAMIC_IN_TIME_BANDS = S1 + S2 + ERA5 + TC + VIIRS` (`eo.py:80`), which is how `create_ee_image` stacks them (`eo.py:196-232`).
- The 18 splits into space-time (first 12: S1+S2) and time (last 6: ERA5+TC+VIIRS) (`dataset.py:518-526`). NDVI is computed from B8/B4 and concatenated (`dataset.py:521-523,631-655`), making s_t_x 13 channels.
- The trailing 16 = `SPACE_BANDS` (SRTM+DW+WC) (`eo.py:82`, `dataset.py:528-532`).
- The trailing 1 = LandScan (`static_bands_in_tif = len(EO_STATIC_BANDS) - len(LOCATION_BANDS) = 4 - 3 = 1`, `dataset.py:505,534`). location x,y,z and the DW/WC static averages are computed during conversion, not exported (`dataset.py:536-546`).

So **total TIF bands = `T*18 + 17`.** That single equation is the export's
correctness check.

### Band naming caveat (verified against live GEE)

The loader matches bands by **position and count only**; it never reads band
names out of the TIF. This matters because the exported band *names* are not all
the canonical names the loader uses internally:

- Dynamic World renames its bands with a `DW_` prefix (`dynamic_world.py:27`, `.select(ORIGINAL_BANDS, DW_BANDS)`), so they export as `DW_water..DW_snow_and_ice`.
- WorldCereal renames to the **bare** product name (`worldcereal.py:28`, `.rename(product)`), so they export as `temporarycrops, maize, wintercereals, springcereals, irrigation`, **not** `WC_*`.

A live 5-point dry-run confirmed the trailing 17 bands come out as
`[elevation, slope, DW_water..DW_snow_and_ice (9), temporarycrops..irrigation (5), b1]`.
This is correct and matches the real tiles (same `create_ee_image`), because the
loader keys off the channel count (16 space + 1 static) and position, not the
names. Our export verification checks against the **emitted** names, not the
loader's internal `WC_*` names.

---

## 7. Open questions for Ana

1. **Export window (Q1 above).** Confirm the per-point TIFs were exported over the `pad_dates` window (one year ending ~1 month after the label date) at 30-day cadence, and the raw timestep count `T`. The exporter that made the real tiles is not in this repo.
2. **Batch-mode CRS.** The Ai2 batch export pins no CRS (`eo.py:361-370`). What CRS / projection are the real tiles in? We are defaulting to `EPSG:4326`; if theirs differ, spatial alignment of the central crop could shift slightly.
3. **`fileDimensions` / tiling.** Batch export passes `file_dimensions=None` (`eo.py:369`). Were the real CONUS tiles ever split into multiple files per point, or always one TIF per sorting_id?
4. **S2 composite reproducibility.** The S2 `qualityMosaic` (`s2.py:66-89`) is sensitive to the exact scene set, which depends on when it was run. Our regenerated S2 bands will be close but not bit-identical. Confirm that is acceptable for reproducing the paper metrics, or whether they expect us to match their exact composites (which would require their cached scenes).
5. **Hawaii applicability.** WorldCereal v100 and the CONUS-tuned normalization stats were built for CONUS. For the eventual Maui application, do we keep these bands/stats or expect coverage gaps over Hawaii (WorldCereal and DW coverage, LandScan resolution)?

---

## 8. One-line summary for the export

For each labelled point: take a 1000 m center box, build a 10 m GeoTIFF whose
bands are `T` monthly timesteps of `[VV, VH, B2..B12, temperature_2m,
total_precipitation_sum, def, soil, aet, avg_rad]` (18 each, interleaved),
then `[elevation, slope, DW x9, WC x5]` (16), then `[LandScan b1]` (1), name it
`<sorting_id>.tif`, and the loader does the rest. Total bands = `T*18 + 17`.

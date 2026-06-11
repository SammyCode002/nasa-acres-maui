# NASA ACRES Maui: Live Fuel Moisture Content Mapping

Mapping wildfire risk in Maui County, Hawaiʻi using live fuel moisture content (LFMC) from satellite remote sensing and Earth-observation foundation models.

Status: active development. Baseline maps (GVMI) and burn-scar detection (dNBR) are running over the corridor. The Galileo LFMC pipeline is set up and the data-export fallback is verified; full CONUS reproduction is paused pending one confirmation from Ana (the per-point export window). See Next steps.

## Project goal (from the internship description)

Develop capabilities for creating monthly LFMC maps over Maui County (for example, 2023 to 2026) as a wildfire-risk indicator. The approach follows Johnson et al. (2025): fine-tune an Earth-observation foundation model to predict LFMC from satellite inputs, trained on the Globe-LFMC dataset.

Deliverables: a GitHub repository with end-to-end mapping code, and monthly LFMC maps over Maui County for multiple years.

## Team

| Person | Role |
|---|---|
| Sam Dameg | Lead Fellow |
| Noah Munz | Intern |
| Ana Tárano, PhD | Advisor (ASU SCAI) |
| Hannah Kerner, PhD | Faculty Lead (ASU SCAI) |
| Thomas Blamey | Faculty Mentor (UH Maui College) |
| Nicolette van der Lee, EdD | Program Manager (UH Community Colleges) |

Meetings: Mondays 8:00 AM HST, weekly to start, then bi-weekly.

## Questions to work through (from Ana)

These framed the early reading. Where the work has since landed:

1. What would be a good approach to extend the LFMC methodology to Hawaiʻi? (Open. The blocker is the Hawaiʻi LFMC label gap: Globe-LFMC 2.0 and FEMS NFMD have zero Hawaiʻi sites.)
2. Should we use OlmoEarth, Galileo, or both? Which training strategy should we first attempt (full fine-tuning, transfer learning, embeddings)? (Starting with Galileo, frozen encoder plus a fine-tuned head. OlmoEarth access requested through Ana.)
3. What data do we need to collect for Hawaiʻi? (In progress. Climate covariates clipped to the corridor; HCDP and FEMS RAWS being pulled. Ground-truth LFMC for Hawaiʻi remains the hard gap.)
4. How should we divide tasks between Sam and Noah? (Settled. Sam: model, pipeline, baseline and validation maps. Noah: data collection, GIS, HCDP.)

## Reading list (assigned by Ana)

| Type | Item |
|---|---|
| Read | Johnson et al. (2025), LFMC mapping: https://arxiv.org/pdf/2506.20132v2 |
| Read | Globe-LFMC 2.0 dataset: https://www.nature.com/articles/s41597-024-03159-6 |
| Read | OlmoEarth: https://arxiv.org/pdf/2511.13655 |
| Review | Project scope doc (Google Doc from Ana) |
| Explore | FEMS, Fire Environment Mapping System: https://fems.fs2c.usda.gov |

## Next steps

Done:

- [x] Read the papers, explore FEMS, meet with Ana, and plan the project
- [x] GVMI/NDMI/NDVI baseline maps over the corridor (`gvmi_baseline.py`)
- [x] dNBR burn-scar detection and a 2023 Kula perimeter (`dnbr_burnscar.py`)
- [x] Climate covariates clipped to the corridor (`clip_climate_normals.py`)
- [x] Galileo LFMC input contract documented (`docs/lfmc_input_spec.md`)
- [x] GEE export fallback for the per-point Galileo tiles, verified end to end (`export_lfmc_tifs.py`)

In progress / blocked:

- [ ] Confirm with Ana the per-point export window so the full Galileo tile set can run (the one open blocker; see `docs/lfmc_input_spec.md`, open question Q1)
- [ ] Reproduce CONUS LFMC metrics with Galileo, then extend toward Maui
- [ ] Source ground-truth LFMC for Hawaiʻi (no existing sites in Globe-LFMC 2.0 or FEMS NFMD)
- [ ] Evaluate OlmoEarth once access lands

## Repository layout

Every script has a companion `.md` explaining what it builds, how it works, and the key decisions.

| Path | What it is |
|---|---|
| `aoi/corridor.geojson` | Single source of truth for the study-area AOI (and the spatial CV blocks). Every script reads its extent from here. See `aoi/corridor.md`. |
| `clip_climate_normals.py` | Clips the 11 statewide climate covariate grids to the corridor. See `clip_climate_normals.md`. |
| `gvmi_baseline.py` | Sentinel-2 GVMI/NDMI/NDVI baseline maps (the simple index the LFMC model must beat). See `gvmi_baseline.md`. |
| `dnbr_burnscar.py` | dNBR burn-scar detection; derives the 2023 Kula burn perimeter from Sentinel-2. See `dnbr_burnscar.md`. |
| `burn_area_gvmi_timeseries.py` | Validation chart: monthly mean GVMI inside the burn vs an unburned control ring. See `burn_area_gvmi_timeseries.md`. |
| `export_lfmc_tifs.py` | Regenerates the per-point Galileo input tiles from GEE, the fallback for the private data bucket. See `export_lfmc_tifs.md`. |
| `docs/lfmc_input_spec.md` | The exact input contract the Galileo LFMC pipeline consumes (bands, window, normalization, h5py schema), with open questions for Ana. |
| `data/` | Local geodata, git-ignored. Raw and clipped grids stay on disk; only code is committed. |

Geospatial scripts need rasterio, which has no Python 3.14 wheels yet, so run them on 3.12:

```bash
uv run --python 3.12 --with rasterio python clip_climate_normals.py
```

## Data

### Climate covariates (climate normals)

Eleven statewide climate covariate variables at 250 m, WGS84, in ESRI grid format. Ten are monthly (13 grids each: Jan-Dec plus Annual); `land_cover` is a single static grid. Variables: `soil_moisture`, `vpd`, `aet_mm`, `pet_penman_mm`, `land_cover`, `veg_cover_fraction`, `lai`, `veg_height`, `solar_radiation`, `rh` (relative humidity), `tair` (air temperature).

- Source: Evapotranspiration of Hawai‘i atlas, https://evapotranspiration.geography.hawaii.edu (the "Climate of Hawai‘i" gridded data products).
- Place the extracted grids under `data/climate_normals/<variable>/` (one folder per variable, each holding its 13 monthly ESRI grid folders), then run `clip_climate_normals.py` to clip them to the corridor. The clipped output lands in `data/climate_normals_clipped/`. Both `data/` subfolders are git-ignored.

Citation:

> Giambelluca, T.W., X. Shuai, M.L. Barnes, R.J. Alliss, R.J. Longman, T. Miura, Q. Chen, A.G. Frazier, R.G. Mudd, L. Cuo, and A.D. Businger, 2014. *Evapotranspiration of Hawai‘i.* Final report submitted to the U.S. Army Corps of Engineers, Honolulu District, and the Commission on Water Resource Management, State of Hawai‘i.

## Acknowledgments

Supported by NASA ACRES, with mentorship from Dr. Ana Tárano and Dr. Hannah Kerner (ASU SCAI). Based at the University of Hawaiʻi Maui College.

"""
export_lfmc_tifs.py

NASA ACRES Maui - Regenerate the per-point LFMC image stacks from GEE.

WHY THIS EXISTS
---------------
The Ai2 `lfmc` pipeline needs one multi-band GeoTIFF per labelled point. The
real tiles live in a private bucket (gs://presto-tifs) we cannot read. We have
GEE access and the CONUS label CSV, so we regenerate the tiles ourselves.

This script is the SMALL-SCALE proof. It exports the first 5 points only and,
before it queues anything, checks that the image it built matches the documented
input contract band-for-band (see docs/lfmc_input_spec.md). It does NOT run the
full ~90k export. The point is to prove the export is correctly shaped and
correctly banded, not to produce the dataset.

HOW IT GUARANTEES A MATCH
-------------------------
It does not re-implement the sensor stacking. It imports Galileo's own
`create_ee_image` from the lfmc-ai2 clone, the exact function that produced the
real tiles, so the band set and order are identical by construction. The only
things reimplemented here are the two tiny label-side helpers (`pad_dates` and
the grouped CSV read), copied so this script does not need the whole `lfmc`
package on the path. Each carries a citation to its source.

Per point the GeoTIFF band axis is:
    [ T * 18 dynamic ]  ++  [ 16 space ]  ++  [ 1 static ]   = T*18 + 17 bands
Dynamic per timestep (order fixed by ALL_DYNAMIC_IN_TIME_BANDS):
    VV, VH, B2..B12, temperature_2m, total_precipitation_sum, def, soil, aet, avg_rad
Space:  elevation, slope, DW x9, WC x5
Static: LandScan b1   (location x,y,z and DW/WC static averages are computed
                       later during TIF-to-array, not exported here)
Filename: <sorting_id>.tif

Run requirements:
    pip install earthengine-api pandas
    earthengine authenticate          # one-time, opens a browser
    # the lfmc-ai2 clone must sit next to this repo (see GALILEO_SRC below)
"""

import argparse
import calendar
import functools
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------- #
# Config  (the block you may need to edit)
# --------------------------------------------------------------------------- #

# Earth Engine / Google Cloud project id. Same one the GVMI baseline uses.
EE_PROJECT = "ace-shine-392702"

# The CONUS label CSV. Lives in the lfmc-ai2 clone, not in this repo.
LABELS_CSV = Path(
    r"C:\Users\Admin\Documents\lfmc-ai2\data\labels\lfmc_data_conus.csv"
)

# Root of the Galileo submodule inside the lfmc-ai2 clone. It carries a
# `galileo` symlink to its `src`, so adding this dir to sys.path lets us import
# `galileo.data...` exactly like the smoke test does. We import their export
# code so our tiles match theirs band-for-band. WHY a hard path: this repo and
# lfmc-ai2 are separate repos that live side by side under Documents\; we point
# at the sibling rather than vendoring a copy that could drift from upstream.
GALILEO_ROOT = Path(
    r"C:\Users\Admin\Documents\lfmc-ai2\submodules\galileo"
)

# Export target. We use Drive (not the private bucket). The folder is created
# in your Drive on first export.
DRIVE_FOLDER = "nasa_acres_lfmc_tifs"

# Small-scale guard. How many points to export in this proof run.
DEFAULT_SAMPLE_SIZE = 5

# Export geometry, straight from the contract (docs/lfmc_input_spec.md sec 4).
EXPORTED_HEIGHT_WIDTH_METRES = 1000  # galileo config.py:12
SURROUNDING_METRES = EXPORTED_HEIGHT_WIDTH_METRES / 2  # eo.py:301
EXPORT_SCALE = 10  # meters, eo.py:367
EXPORT_CRS = "EPSG:4326"  # we pin this; batch export upstream leaves it unset
DAYS_PER_TIMESTEP = 30  # galileo config.py:3

# The static (non-timestep) trailing band count the contract requires:
#   16 space bands (SRTM 2 + DW 9 + WC 5) + 1 static (LandScan b1) = 17
EXPECTED_TRAILING_BANDS = 17
DYNAMIC_BANDS_PER_TIMESTEP = 18  # ALL_DYNAMIC_IN_TIME_BANDS

# --------------------------------------------------------------------------- #
# 4x4 debug logging  (inputs, outputs, timing, status) via a decorator
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("export_lfmc_tifs")


def _short(obj, limit=160):
    """Truncate a repr so server-side ee objects do not flood the log."""
    text = repr(obj)
    return text if len(text) <= limit else text[:limit] + "..."


def debug_log(func):
    """Log the 4 things we care about every call: inputs, outputs, timing, status.

    WHY a decorator: it wraps any function without rewriting its body, so every
    step self-reports the same way. When an export misbehaves the log already
    says which function, with what inputs, and how long it ran before it failed.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        logger.info("[INPUT ] %s args=%s kwargs=%s", func.__name__, _short(args), _short(kwargs))
        try:
            result = func(*args, **kwargs)
            elapsed = time.perf_counter() - start
            logger.info("[OUTPUT] %s -> %s", func.__name__, _short(result))
            logger.info("[TIME  ] %s took %.3fs", func.__name__, elapsed)
            logger.info("[STATUS] %s OK", func.__name__)
            return result
        except Exception as exc:  # noqa: BLE001  (re-raise after logging)
            elapsed = time.perf_counter() - start
            logger.exception("[STATUS] %s FAILED after %.3fs: %s", func.__name__, elapsed, exc)
            raise

    return wrapper


# --------------------------------------------------------------------------- #
# Bootstrap the Galileo import (their export code is the source of truth)
# --------------------------------------------------------------------------- #


@debug_log
def import_galileo():
    """Make Galileo's export code importable and return what we need from it.

    WHY import instead of reimplement: `create_ee_image` is the exact function
    that built the real tiles. Reusing it means our band set and order cannot
    drift from the contract. We only add GALILEO_SRC to sys.path; we do not
    install or modify the upstream repo.

    Returns a small bundle: the image builder, the bounding-box helper, and the
    canonical band lists we assert against.
    """
    if not GALILEO_ROOT.exists():
        raise FileNotFoundError(
            f"Galileo root not found at {GALILEO_ROOT}. "
            "Clone lfmc-ai2 next to this repo, or edit GALILEO_ROOT."
        )
    sys.path.insert(0, str(GALILEO_ROOT))

    # Imported here, after the path insert, on purpose. The `galileo` symlink
    # under GALILEO_ROOT points at `src`, so this resolves to src/data/...
    from galileo.data.earthengine.eo import (  # noqa: E402
        ALL_DYNAMIC_IN_TIME_BANDS,
        create_ee_image,
    )
    from galileo.data.earthengine.ee_bbox import EEBoundingBox  # noqa: E402

    # The trailing (non-timestep) bands, named the way GEE actually emits them,
    # not the way the loader renames them internally. This distinction bit us
    # once: dynamic_world renames its bands with a `DW_` prefix
    # (dynamic_world.py:27, `.select(ORIGINAL_BANDS, DW_BANDS)`), but worldcereal
    # renames to the BARE product name (worldcereal.py:28, `.rename(product)`),
    # so the exported WC bands are `temporarycrops..irrigation`, NOT `WC_*`. The
    # loader does not care (it reads by position and count, never by TIF band
    # name), but our band-name check has to compare against what GEE emits.
    from galileo.data.earthengine.srtm import SRTM_BANDS  # noqa: E402
    from galileo.data.earthengine.dynamic_world import DW_BANDS  # noqa: E402
    from galileo.data.earthengine.worldcereal import (  # noqa: E402
        ORIGINAL_BANDS as WC_EMITTED_BANDS,
    )
    from galileo.data.earthengine.landscan import LANDSCAN_BANDS  # noqa: E402

    # Order matches create_ee_image's append order: SRTM, DW, WC, then LandScan.
    expected_trailing = list(SRTM_BANDS) + list(DW_BANDS) + list(WC_EMITTED_BANDS) + list(LANDSCAN_BANDS)

    return {
        "create_ee_image": create_ee_image,
        "EEBoundingBox": EEBoundingBox,
        "ALL_DYNAMIC_IN_TIME_BANDS": ALL_DYNAMIC_IN_TIME_BANDS,
        "expected_trailing_bands": expected_trailing,
    }


# --------------------------------------------------------------------------- #
# Label-side helpers (small, copied from lfmc-ai2 with citations)
# --------------------------------------------------------------------------- #

# Column names, from lfmc/core/const.py:63-77
COL_SORTING_ID = "sorting_id"
COL_LATITUDE = "latitude"
COL_LONGITUDE = "longitude"
COL_SAMPLING_DATE = "sampling_date"
COL_LFMC_VALUE = "lfmc_value"
COL_SITE_NAME = "site_name"
COL_STATE_REGION = "state_region"
COL_COUNTRY = "country"
COL_LANDCOVER = "landcover"
COL_ELEVATION = "elevation"

DEFAULT_PADDING = timedelta(days=30)  # lfmc/core/padding.py:4


def pad_dates(end_date: date, padding: timedelta = DEFAULT_PADDING) -> tuple[date, date]:
    """Return the (start, end) export window for a label date.

    Copied verbatim from lfmc/core/padding.py:7-12. The window is one year of
    monthly steps ending ~1 month after the label date. See
    docs/lfmc_input_spec.md section 3 for why this defines the temporal window.
    """
    new_end_date = end_date + padding
    last_day_of_month = calendar.monthrange(new_end_date.year, new_end_date.month)[1]
    new_end_date = date(new_end_date.year, new_end_date.month, last_day_of_month)
    start_date = date(new_end_date.year - 1, new_end_date.month, 1)
    return start_date, new_end_date


@debug_log
def read_labels(path: Path) -> pd.DataFrame:
    """Read and group the LFMC labels exactly like the pipeline does.

    Copied from lfmc/core/labels.py:8-39. The grouping by (lat, lon, date) and
    keeping the first sorting_id per group matters: the dataset finds files by
    iterating these grouped rows, so we must pick sorting_ids from the grouped
    frame or we would name a tile the loader never looks for.
    """
    data = pd.read_csv(path)
    grouped = data.groupby(
        [COL_LATITUDE, COL_LONGITUDE, COL_SAMPLING_DATE],
        as_index=False,
    ).agg(
        {
            COL_SITE_NAME: "first",
            COL_SORTING_ID: "first",
            COL_LFMC_VALUE: "mean",
            COL_STATE_REGION: "first",
            COL_COUNTRY: "first",
            COL_LANDCOVER: "first",
            COL_ELEVATION: "first",
        }
    )
    grouped[COL_SAMPLING_DATE] = pd.to_datetime(grouped[COL_SAMPLING_DATE])
    return grouped


def expected_timesteps(start_date: date, end_date: date, days_per_timestep: int) -> int:
    """Count the timesteps Galileo's loop will emit for a window.

    Mirrors the `while cur_end_date <= end_date` loop in eo.py:196-227 so we can
    predict the raw T and assert the exported band count against it.
    """
    cur_date = start_date
    cur_end_date = cur_date + timedelta(days=days_per_timestep)
    steps = 0
    while cur_end_date <= end_date:
        steps += 1
        cur_date += timedelta(days=days_per_timestep)
        cur_end_date += timedelta(days=days_per_timestep)
    return steps


# --------------------------------------------------------------------------- #
# Core export + contract check
# --------------------------------------------------------------------------- #


@debug_log
def initialize_ee(project: str):
    """Authenticate (if needed) and initialize Earth Engine."""
    import ee

    try:
        ee.Initialize(project=project)
    except Exception:  # noqa: BLE001
        logger.warning("EE init failed, launching interactive authentication...")
        ee.Authenticate()
        ee.Initialize(project=project)
    return ee.Number(1).getInfo()  # tiny round-trip to confirm the link is live


@debug_log
def build_image_for_point(galileo, lat: float, lon: float, start_date: date, end_date: date):
    """Build the multi-band ee.Image for one point over its label window.

    Delegates the entire band stack to Galileo's create_ee_image, so the result
    is identical to what produced the real tiles. Returns (image, polygon).
    """
    bbox = galileo["EEBoundingBox"].from_centre(
        mid_lat=float(lat),
        mid_lon=float(lon),
        surrounding_metres=int(SURROUNDING_METRES),
    )
    polygon = bbox.to_ee_polygon()
    image = galileo["create_ee_image"](
        polygon, start_date, end_date, days_per_timestep=DAYS_PER_TIMESTEP
    )
    return image, polygon


@debug_log
def verify_contract(galileo, image, start_date: date, end_date: date) -> dict:
    """Prove the built image matches docs/lfmc_input_spec.md before exporting.

    This is the heart of the proof. It pulls the server-side band names ONCE and
    checks:
      1. total bands == T*18 + 17
      2. the first 18 bands are ALL_DYNAMIC_IN_TIME_BANDS in order
      3. the trailing 17 are SRTM (2) + DW (9) + WC (5) + LandScan (1), named the
         way GEE emits them (see import_galileo for the WC naming caveat)
    Returns a small report dict. Raises AssertionError on any mismatch.
    """
    band_names = image.bandNames().getInfo()  # one network call
    total = len(band_names)

    t_expected = expected_timesteps(start_date, end_date, DAYS_PER_TIMESTEP)
    bands_expected = t_expected * DYNAMIC_BANDS_PER_TIMESTEP + EXPECTED_TRAILING_BANDS

    # 1. total band count and the divisibility the loader relies on
    dynamic_total = total - EXPECTED_TRAILING_BANDS
    if dynamic_total <= 0 or dynamic_total % DYNAMIC_BANDS_PER_TIMESTEP != 0:
        raise AssertionError(
            f"Band count {total} is not T*18 + 17 for any T "
            f"(dynamic block {dynamic_total} not divisible by 18)."
        )
    t_actual = dynamic_total // DYNAMIC_BANDS_PER_TIMESTEP
    if total != bands_expected:
        raise AssertionError(
            f"Expected {bands_expected} bands (T={t_expected}), got {total} "
            f"(implied T={t_actual}). Window {start_date}..{end_date} drifted."
        )

    # 2. first timestep's 18 bands match ALL_DYNAMIC_IN_TIME_BANDS, in order.
    # GEE suffixes repeats across timesteps (VV, VV_1, ...), so the first
    # occurrence block is the clean, unsuffixed set.
    expected_dynamic = list(galileo["ALL_DYNAMIC_IN_TIME_BANDS"])
    first_block = band_names[:DYNAMIC_BANDS_PER_TIMESTEP]
    if first_block != expected_dynamic:
        raise AssertionError(
            "First-timestep band order does not match ALL_DYNAMIC_IN_TIME_BANDS.\n"
            f"  expected: {expected_dynamic}\n  got:      {first_block}"
        )

    # 3. trailing 17 = SRTM (2) + DW (9) + WC (5) + LandScan (1), in the names
    # GEE emits (WC bands are unprefixed; see import_galileo).
    expected_trailing = list(galileo["expected_trailing_bands"])
    trailing = band_names[-EXPECTED_TRAILING_BANDS:]
    if trailing != expected_trailing:
        raise AssertionError(
            "Trailing bands do not match the emitted SRTM+DW+WC+LandScan set.\n"
            f"  expected: {expected_trailing}\n  got:      {trailing}"
        )

    report = {
        "total_bands": total,
        "timesteps": t_actual,
        "dynamic_per_timestep": DYNAMIC_BANDS_PER_TIMESTEP,
        "trailing_static": EXPECTED_TRAILING_BANDS,
        "first_timestep_bands": first_block,
        "trailing_bands": trailing,
    }
    logger.info(
        "[CONTRACT] OK  total=%d  T=%d  (T*18 + 17)  first18=%s",
        total,
        t_actual,
        first_block,
    )
    return report


@debug_log
def export_point_to_drive(image, polygon, sorting_id: int, folder: str):
    """Queue a Drive export for one point's tile, named <sorting_id>.tif.

    WHY toFloat: the stack mixes integer and float bands; casting to float keeps
    the index/probability bands from truncating. WHY high maxPixels: a 100x100
    tile times many bands can exceed the default cap.
    """
    import ee

    task = ee.batch.Export.image.toDrive(
        image=image.toFloat(),
        description=f"lfmc_{sorting_id}",
        folder=folder,
        fileNamePrefix=str(sorting_id),
        region=polygon,
        scale=EXPORT_SCALE,
        crs=EXPORT_CRS,
        maxPixels=int(1e13),
    )
    task.start()
    return task


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def main():
    """Verify and (optionally) export the first N label points.

    Success criteria for this proof:
      - every sampled point builds an image whose band count is T*18 + 17
      - the first 18 bands and trailing 17 bands match the contract exactly
      - with --dry-run (default) nothing is queued; we only prove the shape
      - with --export, one <sorting_id>.tif per point lands in DRIVE_FOLDER
    """
    parser = argparse.ArgumentParser("Regenerate LFMC TIF stacks from GEE (small-scale proof)")
    parser.add_argument(
        "--sample-size",
        type=int,
        default=DEFAULT_SAMPLE_SIZE,
        help="How many label points to process (proof run, keep this small).",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Actually queue Drive exports. Default is dry-run: verify only.",
    )
    parser.add_argument(
        "--labels-csv",
        type=Path,
        default=LABELS_CSV,
        help="Path to lfmc_data_conus.csv.",
    )
    args = parser.parse_args()

    if args.sample_size > 50:
        # This script is the proof, not the production export. Refuse to fan out.
        raise SystemExit(
            f"--sample-size {args.sample_size} is too large for the proof script. "
            "Keep it <= 50; the full ~90k export is a separate, deliberate job."
        )

    galileo = import_galileo()
    initialize_ee(EE_PROJECT)

    labels = read_labels(args.labels_csv)
    sample = labels.head(args.sample_size)
    logger.info(
        "[PLAN  ] %d points, mode=%s, drive=%s",
        len(sample),
        "EXPORT" if args.export else "DRY-RUN (verify only)",
        DRIVE_FOLDER,
    )

    verified, exported, failures = 0, 0, []
    for _, row in sample.iterrows():
        sorting_id = int(row[COL_SORTING_ID])
        label_date = row[COL_SAMPLING_DATE].date()
        start_date, end_date = pad_dates(label_date)
        logger.info(
            "[POINT ] id=%d  label=%s  window=%s..%s  lat=%.5f lon=%.5f",
            sorting_id,
            label_date,
            start_date,
            end_date,
            row[COL_LATITUDE],
            row[COL_LONGITUDE],
        )
        try:
            image, polygon = build_image_for_point(
                galileo, row[COL_LATITUDE], row[COL_LONGITUDE], start_date, end_date
            )
            verify_contract(galileo, image, start_date, end_date)
            verified += 1
            if args.export:
                task = export_point_to_drive(image, polygon, sorting_id, DRIVE_FOLDER)
                logger.info("[QUEUED] id=%d task=%s", sorting_id, task.id)
                exported += 1
        except Exception as exc:  # noqa: BLE001
            failures.append((sorting_id, str(exc)))
            logger.error("[POINT ] id=%d FAILED: %s", sorting_id, exc)

    logger.info(
        "[SUMMARY] verified %d/%d, exported %d, failed %d",
        verified,
        len(sample),
        exported,
        len(failures),
    )
    for sid, msg in failures:
        logger.info("[SUMMARY]   FAIL id=%d: %s", sid, msg)
    if not args.export:
        logger.info("[SUMMARY] dry-run: nothing queued. Re-run with --export to send to Drive.")

    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

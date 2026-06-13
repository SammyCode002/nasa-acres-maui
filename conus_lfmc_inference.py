"""conus_lfmc_inference.py  (NASA ACRES Maui, Sam's baseline task).

STAGED end-to-end runner for the Galileo LFMC model. It loads a pretrained
Galileo encoder, attaches the LFMC regression head, runs inference on the
paper's CONUS test points (or a bounding box), renders the predicted LFMC map
and a predicted-vs-actual error plot, and compares the metrics to the paper
targets in lfmc-ai2/results/tables.md.

WHY this is staged and not run yet: the LFMC head is *trained*, not shipped.
`lfmc-ai2/lfmc/core/eval.py` builds a fresh `nn.Linear` head and fine-tunes it
every run, and only Galileo's foundation encoder weights exist locally. So this
script intentionally does nothing meaningful until EITHER a trained checkpoint
is dropped in (`--checkpoint finetuned_model.pth`) OR we fine-tune the head
ourselves on Globe-LFMC (`--finetune`). Every other piece (encoder load,
dataset, forward pass, metric math, plots) is wired to the repo's own classes
and proven green by the two smoke tests, so the day a trained head lands, Ana's
items 2 and 3 are one command away.

Author: Samuel Dameg (SammyCode002).
Run inside the lfmc-ai2 `.venv-smoke` (torch, galileo, lfmc on the right paths).
"""

import argparse
import functools
import logging
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# Locate the sibling lfmc-ai2 checkout and put its packages on the path.
# WHY: this script lives in the public nasa-acres-maui repo but drives the
# private lfmc-ai2 model code. lfmc-ai2 is a sibling checkout, never vendored
# in here (it is git-ignored). This mirrors smoke_load_encoder.py's bootstrap.
# --------------------------------------------------------------------------- #
LFMC_AI2_ROOT = Path(r"C:\Users\Admin\Documents\lfmc-ai2")
# galileo package = submodules/galileo (holds the `galileo` junction -> src).
sys.path.insert(0, str(LFMC_AI2_ROOT / "submodules" / "galileo"))
# lfmc package = lfmc-ai2 repo root.
sys.path.insert(0, str(LFMC_AI2_ROOT))

# Real upstream config + weights. The repo's data/config and data/models are
# pointer files, so we point at the genuine junction targets (per the smoke
# test notes).
WEIGHTS_ROOT = LFMC_AI2_ROOT / "submodules" / "galileo-data" / "models"
CONFIG_DIR = LFMC_AI2_ROOT / "submodules" / "galileo" / "config"

# Paper targets to beat, from lfmc-ai2/results/tables.md (Pretrained model).
PAPER_TARGETS = {
    "random": {"rmse": 19.14, "mae": 12.80, "r2_score": 0.71},
    "spatial": {"rmse": 24.90, "mae": 17.68, "r2_score": 0.48},
}
# The weak monthly-mean baseline the learned model must beat (random split).
# This is conceptually where the GVMI/monthly index work lands.
PAPER_BASELINE = {"rmse": 33.66, "mae": 25.38, "r2_score": 0.12}

# Mirrors lfmc.core.const.MAX_LFMC_VALUE. The dataset normalizes the label to
# [0, 1] by dividing by this, so LFMC% = normalized_value * MAX_LFMC_VALUE.
MAX_LFMC_VALUE = 302

logger = logging.getLogger("conus_lfmc_inference")


def debug_log(func):
    """4x4 logging: inputs, outputs, timing, status. Standing rule."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        arg_summary = ", ".join(
            [repr(a) for a in args][:4]
            + [f"{k}={v!r}" for k, v in list(kwargs.items())[:4]]
        )
        logger.info("CALL %s | inputs: %s", func.__name__, arg_summary or "(none)")
        try:
            result = func(*args, **kwargs)
            logger.info(
                "STATUS %s OK | TIMING %.1f ms",
                func.__name__,
                (time.perf_counter() - start) * 1000.0,
            )
            return result
        except Exception as exc:
            logger.exception(
                "STATUS %s FAIL | TIMING %.1f ms | %s",
                func.__name__,
                (time.perf_counter() - start) * 1000.0,
                exc,
            )
            raise

    return wrapper


# --------------------------------------------------------------------------- #
# Heavy imports, after the path bootstrap. Wrapped so the file still imports
# (for --help and a byte-compile syntax check) outside the smoke venv.
# --------------------------------------------------------------------------- #
try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import torch
    import torch.nn as nn
    from matplotlib.backends.backend_pdf import PdfPages

    from galileo.utils import device
    from lfmc.core.encoder_loader import load_from_folder
    from lfmc.core.eval import LFMCEval, finetune_and_evaluate
    from lfmc.core.finetuning import FineTuningModel
    from lfmc.core.splits import (
        DEFAULT_TEST_FOLDS,
        DEFAULT_VALIDATION_FOLDS,
        SplitType,
    )
    from lfmc.main.finetune_model import load_normalizer

    _IMPORTS_OK = True
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - only fires outside the smoke venv
    _IMPORTS_OK = False
    _IMPORT_ERROR = exc


@debug_log
def build_model(encoder_name: str, checkpoint_path):
    """Load the Galileo encoder, attach the LFMC head, optionally load a
    trained checkpoint.

    A saved `finetuned_model.pth` is exactly `FineTuningModel.state_dict()`
    (see lfmc/core/eval.py `finetune`), so it loads straight into the same
    architecture we build here. Returns (model_in_eval_mode, is_trained).
    """
    encoder = load_from_folder(WEIGHTS_ROOT / encoder_name, load_weights=True)
    head = nn.Linear(encoder.embedding_size, 1)
    model = FineTuningModel(encoder, head).to(device)

    is_trained = False
    if checkpoint_path is not None:
        state_dict = torch.load(checkpoint_path, map_location=device)
        model.load_state_dict(state_dict)
        is_trained = True
        logger.info("Loaded trained LFMC head from %s", checkpoint_path)
    else:
        # WHY a loud warning: a random head emits near-constant noise, which
        # can look plausible on a colorbar. We never want to present that.
        logger.warning(
            "NO checkpoint: the head is randomly initialized, so predictions "
            "are MEANINGLESS (plumbing only)."
        )
    model.eval()
    return model, is_trained


@debug_log
def get_evaluator(split_type, data_folder, h5py_folder, config_dir, h5pys_only):
    """Build the repo's own LFMCEval over the paper's test split.

    Reusing LFMCEval (not a hand-rolled loop) keeps the dataset construction,
    normalization, and metric math identical to the paper pipeline.
    """
    if not data_folder.exists():
        logger.warning("data_folder does not exist yet: %s", data_folder)
    normalizer = load_normalizer(config_dir)
    return LFMCEval(
        normalizer=normalizer,
        data_folder=data_folder,
        h5py_folder=h5py_folder,
        h5pys_only=h5pys_only,
        output_hw=32,
        output_timesteps=12,
        patch_size=16,
        split_type=split_type,
        validation_folds=DEFAULT_VALIDATION_FOLDS,
        test_folds=DEFAULT_TEST_FOLDS,
        validation_state_regions=None,
        test_state_regions=None,
        excluded_bands=frozenset(),
    )


@debug_log
def run_finetune(args, split_type):
    """Fine-tune the head on Globe-LFMC ourselves, returning the checkpoint
    path.

    WHY delegate to finetune_and_evaluate: running the repo's exact training
    loop guarantees the checkpoint it writes matches FineTuningModel, so the
    inference path below loads it without key surgery.
    """
    normalizer = load_normalizer(Path(args.config_dir))
    pretrained = load_from_folder(WEIGHTS_ROOT / args.encoder, load_weights=True)
    output_folder = Path(args.output_dir) / f"finetuned_{args.encoder}_{args.split}"
    output_folder.mkdir(parents=True, exist_ok=True)
    finetune_and_evaluate(
        normalizer=normalizer,
        pretrained_model=pretrained,
        data_folder=Path(args.data_folder),
        h5py_folder=Path(args.h5py_folder),
        output_folder=output_folder,
        h5pys_only=args.h5pys_only,
        output_hw=32,
        output_timesteps=12,
        patch_size=16,
        split_type=split_type,
        validation_folds=DEFAULT_VALIDATION_FOLDS,
        test_folds=DEFAULT_TEST_FOLDS,
        excluded_bands=frozenset(),
    )
    return output_folder / "finetuned_model.pth"


def log_comparison(name, computed, target):
    """Print computed metrics next to the paper targets with deltas."""
    logger.info("=== %s vs paper target ===", name)
    for key in ("rmse", "mae", "r2_score"):
        got = computed.get(key)
        want = target.get(key)
        if got is None or want is None:
            continue
        # For RMSE/MAE lower is better; for R2 higher is better.
        better = (got <= want) if key != "r2_score" else (got >= want)
        verdict = "meets/beats" if better else "worse than"
        logger.info(
            "  %-9s got %7.3f | target %7.3f | %s target",
            key,
            got,
            want,
            verdict,
        )


@debug_log
def render_outputs(preds, labels, lats, lons, out_pdf, metrics):
    """Two-page PDF: the predicted LFMC map, then the error plot if labels
    exist."""
    preds_pct = preds * MAX_LFMC_VALUE
    have_labels = labels is not None and getattr(labels, "size", 0) > 0
    labels_pct = labels * MAX_LFMC_VALUE if have_labels else None

    with PdfPages(out_pdf) as pdf:
        # Page 1: predicted LFMC over the CONUS test points (a real map).
        fig, ax = plt.subplots(figsize=(10, 6))
        sc = ax.scatter(
            lons, lats, c=preds_pct, cmap="RdYlGn", s=14,
            vmin=0, vmax=MAX_LFMC_VALUE, edgecolors="none",
        )
        ax.set_title("Predicted LFMC, CONUS test points")
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        cb = fig.colorbar(sc, ax=ax)
        cb.set_label("Predicted LFMC (%)")
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)

        # Page 2: predicted vs actual, plus a residual histogram.
        if have_labels:
            fig, (axs, axh) = plt.subplots(1, 2, figsize=(12, 5))
            axs.scatter(labels_pct, preds_pct, s=10, alpha=0.5)
            lim = [0, MAX_LFMC_VALUE]
            axs.plot(lim, lim, "k--", lw=1, label="1:1")
            axs.set_xlabel("Actual LFMC (%)")
            axs.set_ylabel("Predicted LFMC (%)")
            axs.set_title("Predicted vs actual")
            axs.legend(loc="lower right")
            stat = (
                f"RMSE {metrics.get('rmse', float('nan')):.2f}\n"
                f"MAE  {metrics.get('mae', float('nan')):.2f}\n"
                f"R2   {metrics.get('r2_score', float('nan')):.2f}"
            )
            axs.text(
                0.05, 0.95, stat, transform=axs.transAxes, va="top",
                family="monospace",
            )

            resid = preds_pct - labels_pct
            axh.hist(resid, bins=40)
            axh.axvline(0, color="k", lw=1)
            axh.set_xlabel("Residual (pred - actual, %)")
            axh.set_ylabel("Count")
            axh.set_title("Residuals")
            fig.tight_layout()
            pdf.savefig(fig)
            plt.close(fig)

    logger.info("Wrote %s", out_pdf)


@debug_log
def infer_bbox(args):
    """Bounding-box gridded inference. STAGED on purpose."""
    raise NotImplementedError(
        "Bounding-box gridded inference is staged. It needs one model-input "
        "tile per grid cell, which is exactly what export_lfmc_tifs.py already "
        "produces for label points. To finish it: grid the bbox, reuse "
        "create_ee_image to export a tile per cell, cache each to h5py, then "
        "run build_model plus the same forward pass per cell and assemble the "
        "raster. The paper evaluates on test POINTS, so use the default "
        "(point) path first."
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Staged CONUS LFMC inference + visualization."
    )
    parser.add_argument(
        "--encoder", choices=["nano", "tiny", "base"], default="tiny",
        help="Which pretrained Galileo encoder to load.",
    )
    parser.add_argument(
        "--checkpoint", type=Path, default=None,
        help="Path to a trained finetuned_model.pth (the LFMC head). Drops in.",
    )
    parser.add_argument(
        "--finetune", action="store_true",
        help="Fine-tune the head on Globe-LFMC ourselves, then evaluate.",
    )
    parser.add_argument(
        "--split", choices=["random", "spatial"], default="random",
        help="Which paper split to evaluate against.",
    )
    parser.add_argument(
        "--bbox", type=float, nargs=4, default=None,
        metavar=("W", "S", "E", "N"),
        help="CONUS bounding box for gridded inference (staged).",
    )
    parser.add_argument("--data-folder", type=Path, default=LFMC_AI2_ROOT / "data")
    parser.add_argument(
        "--h5py-folder", type=Path, default=LFMC_AI2_ROOT / "data" / "h5py"
    )
    parser.add_argument("--config-dir", type=Path, default=CONFIG_DIR)
    parser.add_argument("--h5pys-only", action="store_true")
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path(__file__).resolve().parent / "data" / "figures",
        help="Where to write the PDF (git-ignored data/ by default).",
    )
    parser.add_argument(
        "--baseline", action="store_true",
        help="Also compute the monthly-mean baseline (the index to beat).",
    )
    return parser.parse_args()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    if not _IMPORTS_OK:
        logger.error(
            "Model dependencies are not importable. Run this in the lfmc-ai2 "
            ".venv-smoke. Underlying error: %s",
            _IMPORT_ERROR,
        )
        sys.exit(2)

    args = parse_args()
    split_type = SplitType(args.split)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    checkpoint = args.checkpoint
    if args.finetune:
        # Train the head ourselves; this writes finetuned_model.pth.
        checkpoint = run_finetune(args, split_type)

    # The honest guard: refuse to produce a meaningless map.
    if checkpoint is None:
        logger.error(
            "STAGED: no trained head available. Pass --checkpoint "
            "<finetuned_model.pth> or --finetune. The encoder, dataset, forward "
            "pass, metrics, and plots are all wired and smoke-tested; the only "
            "missing piece is the trained head. Exiting without a fake map."
        )
        sys.exit(1)

    if args.bbox is not None:
        infer_bbox(args)
        return

    model, _ = build_model(args.encoder, checkpoint)
    evaluator = get_evaluator(
        split_type,
        Path(args.data_folder),
        Path(args.h5py_folder),
        Path(args.config_dir),
        args.h5pys_only,
    )

    # Inference on the paper's CONUS test points.
    labels, preds, lats, lons = evaluator.test("conus", model)
    metrics = evaluator.compute_metrics("conus", preds, labels).get("conus", {})
    log_comparison("Pretrained LFMC (ours)", metrics, PAPER_TARGETS[args.split])

    if args.baseline:
        b_preds, b_labels = evaluator.baseline()
        b_metrics = evaluator.compute_metrics("baseline", b_preds, b_labels).get(
            "baseline", {}
        )
        log_comparison("Monthly-mean baseline", b_metrics, PAPER_BASELINE)

    out_pdf = args.output_dir / f"conus_lfmc_inference_{args.encoder}_{args.split}.pdf"
    render_outputs(preds, labels, lats, lons, out_pdf, metrics)


if __name__ == "__main__":
    main()

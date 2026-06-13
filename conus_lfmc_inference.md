# CONUS LFMC inference and visualization (staged)

**File:** `conus_lfmc_inference.py`
**Project:** NASA ACRES Maui (github.com/SammyCode002/nasa-acres-maui)
**Author:** Samuel Dameg (SammyCode002)

## Status: STAGED, waiting on a trained head

This script is built and wired but intentionally not run yet. It produces
nothing meaningful until there is a trained LFMC head, because the head is
**trained, not shipped**. `lfmc-ai2/lfmc/core/eval.py` builds a fresh
`nn.Linear` head and fine-tunes it on every run, and only Galileo's foundation
encoder weights exist locally. So the script needs either:

- `--checkpoint <finetuned_model.pth>`: a trained head to drop in, or
- `--finetune`: fine-tune the head ourselves on Globe-LFMC first.

Everything else (encoder load, dataset, forward pass, metric math, plots) is
already wired to the repo's own classes and proven green by the two smoke
tests. The moment a trained head lands, Ana's items 2 (inference) and 3
(visualize) are one command away.

## What it builds

An end-to-end runner that:

1. Loads a pretrained Galileo encoder (`tiny` by default) and attaches the LFMC
   regression head (`nn.Linear(embedding_size, 1)`), exactly as the paper
   pipeline does.
2. Either loads a trained checkpoint or fine-tunes the head on Globe-LFMC.
3. Runs inference on the paper's CONUS test points (the default) or, later, a
   bounding box.
4. Renders a two-page PDF: the predicted LFMC map (test points colored by
   predicted LFMC %) and, when labels exist, a predicted-vs-actual plot with a
   residual histogram.
5. Compares the metrics (RMSE, MAE, R2) to the paper targets already in
   `lfmc-ai2/results/tables.md`.

## How it works (and why it drops in)

The script reuses the lfmc-ai2 classes rather than re-implementing them, so its
numbers match the paper pipeline by construction:

- **Model.** `build_model` loads the encoder with `load_from_folder`, wraps it
  in `FineTuningModel(encoder, head)`. A saved `finetuned_model.pth` is exactly
  `FineTuningModel.state_dict()` (see `eval.py` `finetune`), so
  `model.load_state_dict(torch.load(checkpoint))` loads it with no key surgery.
  This is the whole reason it will "just drop in."
- **Data and inference.** It builds the repo's `LFMCEval` over the chosen split
  (`random` or `spatial`) and calls `evaluator.test(...)`, the same path the
  paper uses to produce `(labels, preds, lats, lons)`.
- **Metrics.** It calls `evaluator.compute_metrics(...)`, which scales by
  `MAX_LFMC_VALUE = 302` (LFMC%) and uses the same sklearn RMSE/MAE/R2 as the
  paper. `log_comparison` prints each metric next to its target with a verdict.
- **Fine-tune path.** `--finetune` delegates to `finetune_and_evaluate`, the
  repo's real training loop, so the checkpoint it writes is guaranteed
  compatible.

## What is proven vs not

- **Proven now:** the module imports clean in the smoke venv (every `galileo`
  and `lfmc` symbol resolves), and the two smoke tests pass green (encoder load,
  and a real forward pass on the exact code path).
- **Not proven:** that real Earth-observation inputs produce correct LFMC
  values. That needs a trained head and the real data, which is the staged part.

## Usage (once a head exists)

```bash
# in the lfmc-ai2 smoke venv
.venv-smoke/Scripts/python.exe conus_lfmc_inference.py \
    --encoder tiny --checkpoint path/to/finetuned_model.pth \
    --split random --baseline

# or fine-tune the head ourselves first, then evaluate
.venv-smoke/Scripts/python.exe conus_lfmc_inference.py \
    --encoder tiny --finetune --split random \
    --data-folder <tiles> --h5py-folder <h5pys>
```

Without `--checkpoint` or `--finetune`, the script exits with a clear STAGED
message and does not draw a fake map.

## Key decisions (and why)

- **Reuse the repo's classes, do not re-implement.** Guarantees identical
  normalization, dataset construction, forward pass, and metrics. The only way
  the comparison to the paper targets is honest.
- **Honest guard.** A random head emits near-constant noise that can look
  plausible on a colorbar, so the script refuses to render without a trained
  head rather than produce a convincing but meaningless map.
- **Bounding-box inference is staged.** A gridded CONUS map needs one
  model-input tile per grid cell, which is exactly what `export_lfmc_tifs.py`
  generates for label points. `infer_bbox` raises with the recipe to finish it
  (grid the bbox, export a tile per cell, cache to h5py, run the forward pass
  per cell, assemble the raster). The paper evaluates on test points, so that
  path is the default and the one to use first.
- **Paper targets are pinned in code** from `results/tables.md` (Pretrained
  random RMSE 19.14 / R2 0.71; spatial RMSE 24.90), plus the weak monthly-mean
  baseline (RMSE 33.66), which is conceptually where the GVMI/monthly work
  lands.

## Environment

Runs in the lfmc-ai2 `.venv-smoke` (Python 3.12, torch 2.2.1+cpu). It adds the
sibling `lfmc-ai2` checkout to the path (galileo at `submodules/galileo`, lfmc
at the repo root); `LFMC_AI2_ROOT` at the top of the file points at it. The
lfmc-ai2 checkout is never vendored into this repo (it is git-ignored).

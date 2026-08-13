# Vendored ProPainter

- Source: https://github.com/sczhou/ProPainter
- Pinned commit: e870e79321c31b733e2031af5aa2fb1fe3ac7eec (clone 2026-08-12)
- License: NTU S-Lab License 1.0 (see LICENSE) — non-commercial redistribution
  terms apply to the code; weights downloaded separately from the official
  v0.1.0 release on first use into `backend/data/models/propainter/`.
- Trimmed for inference-only use: removed `.git/`, `assets/`, `inputs/`,
  `web-demos/`, `datasets/` (~115 MB → ~0.5 MB). Training entry points are
  kept but unused.
- Consumed exclusively through
  `backend/app/services/propainter_adapter.py`, which appends this directory
  to `sys.path` and imports `model.propainter`, `model.recurrent_flow_completion`
  and `model.modules.flow_comp_raft` directly (NOT `core.utils`, which pulls
  matplotlib at import time).
- Inference-time external deps: torch, torchvision, einops, numpy, cv2, PIL —
  all in `pixi.toml` (`einops` added for this module).

#!/usr/bin/env python3
"""Upsert owner review ROUND 6 verdicts into backend/data/eval_waivers.json.

Convention (owner, 2026-07-11): exhaustive on the review6 pages —
unmentioned review entries are PASS (recorded with generated intervals for
the stale guard). Named FAILs stay machine failures. SKIPs are permanent
owner-approved ignores (new evaluator verdict, no stale guard):
411f #7/#8 (GT region buggy: evidence-hole slow-mo burst) and 5e85 #45
(non-anime content appended at the edit end contaminates matching there;
truth timings validated present among primary/secondary candidates).

Usage:
  pixi run python backend/scripts/diagnostics/upsert_round6_waivers.py \
      --json-prefix ~/.cache/atr-eval/vXXX_fresh   (per-project suffix)
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluate_matching_against_ground_truth import (
    _load_generated,
    _validate_strict,
)

DATE = "2026-07-11"
FAIL_SCENES = {
    ("dcd74148c7ec", 6),
    ("85de83ca6323", 10),
    ("85de83ca6323", 20),
    ("411f73d26c1d", 51),
    ("5e85164d9ff8", 11),
}
SKIP_SCENES = {
    ("411f73d26c1d", 7): "owner round-6 SKIP: GT region buggy "
    "(evidence-hole 0.59x slow-mo burst) - permanent ignore",
    ("411f73d26c1d", 8): "owner round-6 SKIP: GT region buggy "
    "(evidence-hole 0.59x slow-mo burst) - permanent ignore",
    ("5e85164d9ff8", 45): "owner round-6 SKIP: non-anime scene appended at "
    "edit end contaminates matching; out-of-scope (truth timings verified "
    "present among primary/secondary candidates)",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-map", nargs=4, required=True,
                    help="four pid=path pairs")
    args = ap.parse_args()
    ledger_path = Path("backend/data/eval_waivers.json")
    ledger = json.loads(ledger_path.read_text())
    by_key = {
        (w["project_id"], w["gt_scene_index"], w["axis"]): w for w in ledger
    }

    def upsert(pid: str, idx: int, axis: str, verdict: str,
               generated, note: str) -> None:
        key = (pid, idx, axis)
        entry = by_key.get(key)
        if entry is None:
            entry = {"project_id": pid, "gt_scene_index": idx, "axis": axis}
            ledger.append(entry)
            by_key[key] = entry
        entry["verdict"] = verdict
        entry["generated"] = list(generated)
        entry["note"] = note
        entry["date"] = DATE

    for pair in args.json_map:
        pid, path = pair.split("=", 1)
        generated = _load_generated(Path(path).expanduser())
        result = _validate_strict(pid, generated)
        for e in result.review_entries:
            idx, axis = e.gt_scene_index, e.axis
            if (pid, idx) in SKIP_SCENES:
                continue  # handled below for both axes
            if (pid, idx) in FAIL_SCENES:
                upsert(pid, idx, axis, "fail", e.generated_interval,
                       "owner round-6: FAIL (machine failure stands)")
            else:
                upsert(pid, idx, axis, "pass", e.generated_interval,
                       "owner round-6: pass (unmentioned = pass)")
        print(f"{pid}: {len(result.review_entries)} review entries processed")

    for (pid, idx), note in SKIP_SCENES.items():
        for axis in ("scene", "source"):
            upsert(pid, idx, axis, "skip", (0.0, 0.0), note)

    ledger_path.write_text(json.dumps(ledger, indent=1))
    print(f"ledger: {len(ledger)} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

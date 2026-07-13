#!/usr/bin/env python3
"""Upsert owner review ROUND 8 verdicts into backend/data/eval_waivers.json.

Owner verdict (2026-07-13, on the review8 pages generated from the final
v160-v162 outputs): ALL PASS — including the five machine-fixed round-7
targets at their new intervals (dcd#18, 411f#28, 85de#10, 85de#20,
5e85#11) and the 411f#19 toward-GT stale. The single remaining failure is
411f#51, for which the owner supplied NEW intelligence: the tail is a
manually added fadeout-to-black (the edit's final transition) and the
machine's 587.03 start is confirmed good — a fadeout-tail instrument is
the prescribed fix, so its FAIL entry stays until fixed.

Usage:
  pixi run python backend/scripts/diagnostics/upsert_round8_waivers.py \
      --json-map pid=path pid=path pid=path pid=path
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

DATE = "2026-07-13"
FAIL_SCENES = {
    ("411f73d26c1d", 51): (
        "owner round-8: FAIL — manually added fadeout-to-black tail (edit's "
        "final transition) breaks the fold; 587.03 start confirmed good"
    ),
}
SKIP_SCENES = {("411f73d26c1d", 7), ("411f73d26c1d", 8), ("5e85164d9ff8", 45)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-map", nargs=4, required=True)
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
        n = 0
        for e in result.review_entries:
            idx, axis = e.gt_scene_index, e.axis
            if (pid, idx) in SKIP_SCENES:
                continue
            if (pid, idx) in FAIL_SCENES:
                upsert(pid, idx, axis, "fail", e.generated_interval,
                       FAIL_SCENES[(pid, idx)])
            else:
                upsert(pid, idx, axis, "pass", e.generated_interval,
                       "owner round-8: pass (exhaustive review8, "
                       "unmentioned = pass)")
            n += 1
        print(f"{pid}: {n} review entries upserted")

    ledger_path.write_text(json.dumps(ledger, indent=1))
    from collections import Counter

    print(f"ledger: {len(ledger)} entries",
          Counter(e["verdict"] for e in ledger))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

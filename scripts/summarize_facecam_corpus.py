"""Export an auditable inventory and descriptive statistics, never retention estimates."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import statistics

from facecam_corpus import DEFAULT_OUTPUT, save_json


def summarize(root: Path):
    summary = {"creators": {}, "complete": True,
               "limitations": ["ASR transcripts and model labels need editorial review.",
                               "Public views/likes do not measure retention or causal effectiveness.",
                               "Title placement means spoken/ASR text, not on-screen title visibility."]}
    rows = []
    for manifest_path in sorted(root.glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text())
        creator = manifest["creator"]
        stats = {"requested": manifest["requested_count"], "downloaded": 0, "transcribed": 0,
                 "annotated": 0, "intro_excluded": 0, "duration_seconds": 0,
                 "speech_gaps": [], "missing": [], "oldest": None, "newest": None}
        words, durations, story_words, story_durations, annotations, story_annotations = [], [], [], [], [], []
        transitions = Counter()
        for rank, entry in enumerate(manifest["videos"], 1):
            folder = manifest_path.parent / "videos" / entry["id"]
            date = datetime.fromtimestamp(entry["timestamp"], timezone.utc).isoformat()
            stats["newest"] = stats["newest"] or date
            stats["oldest"] = date
            if (folder / "video.mp4").exists():
                stats["downloaded"] += 1
            if not (folder / "transcript.json").exists():
                stats["missing"].append(entry["id"])
                continue
            transcript = json.loads((folder / "transcript.json").read_text())
            stats["transcribed"] += 1
            stats["intro_excluded"] += bool(transcript["excluded_branded_intro"])
            duration = transcript["duration"]
            stats["duration_seconds"] += duration
            count = len(transcript["analysis_text"].split())
            words.append(count)
            durations.append(duration)
            if transcript["uncovered_speech"]:
                stats["speech_gaps"].append({"id": entry["id"], "spans": transcript["uncovered_speech"]})
            annotation_path = folder / "analysis.json"
            annotation = json.loads(annotation_path.read_text()) if annotation_path.exists() else {}
            if annotation:
                stats["annotated"] += 1
                annotations.append(annotation)
                if annotation["format"] == "presentation":
                    story_annotations.append(annotation)
                    story_words.append(count)
                    story_durations.append(duration)
                    text = transcript["analysis_text"].casefold()
                    for pattern in ["mais", "sauf que", "alors", "tout va basculer", "on a là"]:
                        if re.search(r"\b" + re.escape(pattern) + r"\b", text):
                            transitions[pattern] += 1
            ending = annotation.get("ending", {})
            ending_text = ending.get("description", "") if isinstance(ending, dict) else ending
            rows.append({"creator": creator, "rank": rank, "id": entry["id"], "date": date,
                "url": entry["url"], "duration": round(duration, 2), "words_without_brand": count,
                "views_at_collection": entry.get("view_count"), "format": annotation.get("format", "unannotated"),
                "hook_type": annotation.get("hook_type", ""), "hook": annotation.get("hook_summary", ""),
                "progression": " → ".join(annotation.get("progression", [])),
                "devices": " | ".join(annotation.get("retention_devices", [])),
                "ending": ending_text, "title_placement": annotation.get("title_placement", ""),
                "transfer": annotation.get("transfer", ""),
                "evidence": " | ".join(annotation.get("evidence", []))})
        stats["formats"] = dict(Counter(a["format"] for a in annotations))
        stats["presentation_hook_types"] = dict(Counter(a["hook_type"] for a in story_annotations))
        stats["presentation_title_placement"] = dict(Counter(a["title_placement"] for a in story_annotations))
        stats["presentation_transition_presence"] = dict(transitions)
        stats["presentation_count"] = len(story_annotations)
        for name, values in [("words", words), ("duration", durations),
                             ("presentation_words", story_words), ("presentation_duration", story_durations)]:
            stats[name] = {"median": round(statistics.median(values), 2), "min": min(values),
                           "max": max(values)} if values else None
        stats["duration_seconds"] = round(stats["duration_seconds"], 2)
        summary["creators"][creator] = stats
        if any(stats[key] != stats["requested"] for key in ["downloaded", "transcribed", "annotated"]):
            summary["complete"] = False
    if not summary["creators"]:
        summary["complete"] = False
    save_json(root / "summary.json", summary)
    if rows:
        with (root / "analysis-index.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(summarize(args.output), ensure_ascii=False, indent=2))

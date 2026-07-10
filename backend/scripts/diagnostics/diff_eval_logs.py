#!/usr/bin/env python3
"""Diff two evaluator logs per GT scene: bucket transitions between runs.

Usage: diff_eval_logs.py old.log new.log [axis]  (axis: scene|source, default source)
"""
import re
import sys
from pathlib import Path


def parse(path: str) -> dict[str, dict[tuple[str, int], str]]:
    projects: dict[str, dict[tuple[str, int], str]] = {}
    current = None
    for line in Path(path).read_text().splitlines():
        m = re.match(r"^Project (\S+)", line)
        if m:
            current = projects.setdefault(m.group(1), {})
            continue
        if current is None:
            continue
        m = re.match(r"\s+(scene|source)#(\d+) (.*)", line)
        if m:
            axis, idx, rest = m.group(1), int(m.group(2)), m.group(3)
            kind = rest.split(":")[0]
            key = (axis, idx)
            # keep the worst/most specific mention (first wins is fine)
            current.setdefault(key, f"{kind} | {rest}")
    return projects


def main() -> None:
    old, new = parse(sys.argv[1]), parse(sys.argv[2])
    axis_filter = sys.argv[3] if len(sys.argv) > 3 else None
    for pid in sorted(set(old) | set(new)):
        o, n = old.get(pid, {}), new.get(pid, {})
        keys = sorted(set(o) | set(n))
        changes = []
        for key in keys:
            if axis_filter and key[0] != axis_filter:
                continue
            ov = o.get(key, "exact")
            nv = n.get(key, "exact")
            if ov.split(" | ")[0] != nv.split(" | ")[0] or ov != nv:
                changes.append((key, ov, nv))
        print(f"== {pid}: {len(changes)} changed rows")
        for (axis, idx), ov, nv in changes:
            print(f"  {axis}#{idx}:")
            print(f"    old: {ov}")
            print(f"    new: {nv}")


if __name__ == "__main__":
    main()

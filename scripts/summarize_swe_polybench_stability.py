#!/usr/bin/env python3
"""Create a non-destructive derived classification for a stability run."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from run_swe_polybench_stability import _atomic_json, _classify, _sha256


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.run.resolve(strict=True)
    manifest_path = root / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = []
    for path in sorted((root / "instances").glob("*/result.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        derived = _classify(raw)
        records.append(
            {
                "instance_id": raw["instance_id"],
                "original_classification": raw.get("classification"),
                "derived_classification": derived,
                "error_code": raw.get("error_code"),
                "cleanup_ok": raw.get("cleanup_ok"),
                "timing_seconds": raw.get("timing_seconds", {}),
            }
        )
    result = {
        "schema": "jolink.swe-polybench-stability-derived.v1",
        "source_manifest_sha256": _sha256(manifest_path),
        "source_manifest": manifest,
        "completed": len(records),
        "original_classification_counts": dict(
            Counter(item["original_classification"] for item in records)
        ),
        "derived_classification_counts": dict(
            Counter(item["derived_classification"] for item in records)
        ),
        "reclassified": [
            item
            for item in records
            if item["original_classification"] != item["derived_classification"]
        ],
        "records": records,
    }
    destination = args.output or (root / "derived-summary.json")
    _atomic_json(destination.resolve(strict=False), result)
    print(json.dumps(result["derived_classification_counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

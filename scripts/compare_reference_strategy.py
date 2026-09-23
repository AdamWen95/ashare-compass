"""Compare frozen technical observations offline; create one new JSON artifact.

python scripts/compare_reference_strategy.py --selection selection.json
  --inputs inputs.json --observation observation.json --benchmark benchmark.json
  --output new-study.json

The raw benchmark response must be next to its frozen benchmark packet. No
production databases, external services, model clients, or credentials are used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PureWindowsPath
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ashare_daily.reference_study import compare_reference_strategies, normalize_study_benchmark


def read_json(path):
    def reject_constant(value):
        raise ValueError("nonfinite_json_value:" + value)
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject_constant)


def read_verified_benchmark(path):
    path = Path(path).resolve(strict=True)
    packet = read_json(path)
    reference = packet.get("source_reference", {})
    raw_path = reference.get("path")
    if not isinstance(raw_path, str) or not raw_path or "://" in raw_path:
        raise ValueError("benchmark_source_path_invalid")
    # An absolute path from a different machine is relocated only by basename;
    # the adjacent file hash and complete reconstructed packet must still match.
    basename = PureWindowsPath(raw_path).name if "\\" in raw_path else Path(raw_path).name
    source_path = (path.parent / basename).resolve(strict=True)
    if source_path.parent != path.parent or not source_path.is_file():
        raise ValueError("benchmark_source_must_be_adjacent_regular_file")
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if source_hash != packet.get("source_hash") or source_hash != reference.get("sha256"):
        raise ValueError("benchmark_source_file_hash_mismatch")
    rebuilt = normalize_study_benchmark(read_json(source_path), packet.get("calendar"),
        source_hash=source_hash, source_reference=reference)
    if rebuilt != packet:
        raise ValueError("benchmark_frozen_packet_differs_from_original_source")
    return packet


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("selection", "inputs", "observation", "benchmark", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.absolute()
    if output.exists():
        raise ValueError("study_output_must_be_new")
    result = compare_reference_strategies(read_json(args.selection), read_json(args.inputs),
        read_json(args.observation), read_verified_benchmark(args.benchmark))
    body = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(body)
    print(json.dumps({"status": result["status"], "output": str(output), "anchor_count": len(result["anchors"]),
        "conclusion": result["conclusion"], "ranking_change_allowed": False,
        "network_requests": 0, "model_calls": 0}, ensure_ascii=False))
    return 0 if result["status"] == "available" else 2


if __name__ == "__main__":
    raise SystemExit(main())

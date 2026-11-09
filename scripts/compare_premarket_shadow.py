"""Read validated frozen archives; write a new 3/5/10-session comparison offline."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from ashare_daily.premarket.archive import _read
from ashare_daily.premarket.shadow_followup import read_followup_evidence, compare_shadow_reports


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', type=Path, required=True, help='Existing outputs root')
    parser.add_argument('--report-directory', type=Path, action='append', required=True)
    parser.add_argument('--evidence-directory', type=Path, action='append', default=[])
    parser.add_argument('--as-of', required=True, help='Asia/Shanghai YYYY-MM-DD; observation date, not a trade assumption')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise ValueError('shadow_comparison_output_must_be_new')
    reports = [_read(args.output_root.absolute(), p.absolute())['report'] for p in args.report_directory]
    evidence = read_followup_evidence(args.evidence_directory)
    result = compare_shadow_reports(reports, evidence, as_of=args.as_of)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({k: result[k] for k in ('status', 'conclusion', 'ranking_change_allowed', 'network_requests', 'model_calls')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

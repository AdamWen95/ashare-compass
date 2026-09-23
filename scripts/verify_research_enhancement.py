"""Explicit bounded LIVE verification. Does not print environment or credentials."""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path

from ashare_daily.market_schemas import SHANGHAI
from ashare_daily.qualification_sources import collect_eligibility
from ashare_daily.research.sources import collect_materials
from ashare_daily.research.runner import archive_materials, run_research
from ashare_daily.research.model import ChatCompletionsModel
from ashare_daily.research.model_settings import load_model_settings
from ashare_daily.storage.market import MarketStore
from ashare_daily.sample_data import load_sample_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', action='store_true', help='One separately observable real model call, no retry')
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--cutoff', default='2026-09-10T21:00:00+08:00')
    args = parser.parse_args()
    root = Path('outputs/verification/enhancement')
    now = datetime.now(SHANGHAI)
    cutoff = datetime.fromisoformat(args.cutoff)
    start = (cutoff - timedelta(days=3)).isoformat()
    if args.model:
        if not args.bundle or not args.snapshot:
            parser.error('--model requires --bundle and --snapshot')
        settings = load_model_settings(Path('.env'), include_environment=False).model_copy(
            update={'max_calls': 1, 'max_retries': 0, 'timeout_seconds': 90, 'max_output_tokens': 4096})
        report = run_research(market_snapshot=args.snapshot, evidence_bundle=args.bundle,
            start=start, cutoff=args.cutoff, config_path=Path('config/m3_daily.json'),
            output_dir=root / 'real-model', client=ChatCompletionsModel(settings))
        print(json.dumps(report, ensure_ascii=False))
        return
    config = json.loads(Path('config/m21_100.json').read_text(encoding='utf-8-sig'))
    sample = load_sample_config(config['sample_file'])
    store = MarketStore(Path('data/research/market.sqlite3'))
    identities = {}
    for symbol in sample['symbol_types']:
        instrument = store.get_instrument(symbol)
        if instrument and instrument.security_type == 'stock':
            identities[symbol] = instrument.name
    qualification = collect_eligibility(config_path=Path('config/eligibility_sources.json'), target=now.date(), output_dir=root / 'live-eligibility')
    print(json.dumps({'stage': 'eligibility', 'result': qualification}, ensure_ascii=False), flush=True)
    collection = collect_materials(registry_path=Path('config/m3_sources.json'), start=start, cutoff=args.cutoff,
        sample_symbols=identities, output_dir=root / 'live-news', include_background=True)
    archive = archive_materials(collection=collection, database=Path('data/research/market.sqlite3'),
        output_dir=root / 'live-materials', start=start, cutoff=args.cutoff)
    summary = {'stage': 'news', 'request_count': collection['request_count'], 'evidence_count': collection['evidence_count'],
        'source_health': collection['source_health'], 'bundle_file': archive['bundle_file'],
        'articles': [{'title': e['title'], 'published_at': e['published_at'], 'associations': e['security_associations']}
                     for e in collection['evidence']]}
    (root / 'live-source-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()

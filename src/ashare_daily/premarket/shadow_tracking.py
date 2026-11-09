"""Bounded local follow-up attached to the existing job, after publication."""
from pathlib import Path

from ..operations.daily import atomic_json, local_path
from .contracts import SHADOW_SCHEMAS
from .shadow_followup import read_followup_evidence, compare_shadow_reports

MAX_REPORTS = 20


def track_shadow_history(project, archives, prepared, directory, *, as_of):
    reports = [a['report'] for a in archives if a['report']['schema_version'] in SHADOW_SCHEMAS][:MAX_REPORTS]
    source = prepared.get('source_paths', {}).get('observation')
    windows = []
    issue = None
    if source and reports:
        path = Path(source)
        if not path.is_absolute():
            path = local_path(project, source)
        if not path.resolve().is_relative_to(Path(project).resolve()):
            raise ValueError('shadow_tracking_source_outside_project')
        try:
            windows = read_followup_evidence([path])
        except (ValueError, KeyError, TypeError, OSError) as exc:
            issue = '冻结后续历史不可用：' + type(exc).__name__
    result = compare_shadow_reports(reports, windows, as_of=as_of)
    result.update(history_limit=MAX_REPORTS, evidence_scope='current_run_frozen_history_only',
                  evidence_issue=issue, collection_expanded=False)
    atomic_json(Path(directory)/'shadow_followup.json', result)
    return {'status': result['status'], 'reports': len(reports), 'evidence_issue': issue,
            'artifact': 'shadow_followup.json', 'ranking_change_allowed': False}

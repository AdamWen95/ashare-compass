from datetime import datetime

import pytest

from ashare_daily.premarket.announcement_review import publication_scope


@pytest.mark.parametrize('day,begin,end,expected', [
    ('2026-11-05','2026-11-04T08:55:00+08:00','2026-11-07T08:55:00+08:00','inside'),
    ('2026-11-07','2026-11-06T08:55:00+08:00','2026-11-07T08:55:00+08:00','observed_by_cutoff'),
    ('2026-11-06','2026-11-06T08:55:00+08:00','2026-11-07T08:55:00+08:00','start_boundary'),
    ('2026-11-08','2026-11-06T08:55:00+08:00','2026-11-07T08:55:00+08:00','future'),
    ('2026-11-05','2026-11-06T08:55:00+08:00','2026-11-07T08:55:00+08:00','outside'),
])
def test_date_interval_preserves_precision_without_rejecting_entire_days(day,begin,end,expected):
    item={'published_at':day,'publication_precision':'date'}
    assert publication_scope(item,begin,end,'2026-11-07T08:34:00+08:00')==expected
    assert item=={'published_at':day,'publication_precision':'date'}


def test_receipt_after_cutoff_does_not_prove_early_availability():
    with pytest.raises(ValueError,match='cutoff'):
        publication_scope({'published_at':'2026-11-07','publication_precision':'date'},
            '2026-11-06T08:55:00+08:00','2026-11-07T08:55:00+08:00','2026-11-07T09:01:00+08:00')

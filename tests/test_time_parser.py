import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from time_parser import parse_duration


def test_parse_duration_uses_minute_for_short_m():
    assert parse_duration("1m") == datetime.timedelta(minutes=1)
    assert parse_duration("2m 30s") == datetime.timedelta(minutes=2, seconds=30)


def test_parse_duration_supports_month_aliases():
    assert parse_duration("1mo") == datetime.timedelta(days=30)
    assert parse_duration("1month") == datetime.timedelta(days=30)


def test_parse_duration_rejects_values_outside_timedelta_range():
    with pytest.raises(ValueError, match="too large"):
        parse_duration("100000000years")

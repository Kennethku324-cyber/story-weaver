"""Timer 格式鎖定 — spec §8「timer 鎖定」。

timer.py 嘅星期／日期字串係簡繁同形（「星期一」…「星期日」、
%Y年%m月%d日），spec §5.10 明確唔改，呢度加測試鎖定格式唔俾人郁。

行法：/Users/kenneth/Projects/story-weaver/.venv/bin/python -m pytest tests/localization/test_timer.py
"""

import datetime
import re

import pytest

from modules.utils.timer import Timer
from modules.model.text_normalize import contains_simplified

DATE_CN_RE = re.compile(r"^\d{4}年\d{2}月\d{2}日（星期[一二三四五六日]）$")
TIME_CN_RE = re.compile(r"^\d{4}年\d{2}月\d{2}日（星期[一二三四五六日]）\d{2}:\d{2}$")

# 2026-07-27 係星期一，連續七日涵蓋成個星期
EXPECTED_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


@pytest.mark.parametrize("offset,expected", list(enumerate(EXPECTED_WEEKDAYS)))
def test_get_weekday_full_week(offset, expected):
    day = datetime.date(2026, 7, 27) + datetime.timedelta(days=offset)
    t = Timer(start=day.strftime("%Y%m%d-06:00"))
    assert t.get_weekday(t.get_date()) == expected


def test_daily_format_cn_format_lock():
    """daily_format_cn() 輸出含「星期X」＋「年…月…日」格式（spec §8 通過條件）。"""
    t = Timer(start="20260727-06:00")
    out = t.daily_format_cn()
    assert DATE_CN_RE.match(out), f"日期格式唔啱: {out!r}"
    assert out == "2026年07月27日（星期一）"


def test_daily_format_cn_covers_all_weekdays():
    """連續七日輸出齊「星期一」…「星期日」，全部簡繁同形、零簡體。"""
    seen = set()
    for offset in range(7):
        day = datetime.date(2026, 7, 27) + datetime.timedelta(days=offset)
        t = Timer(start=day.strftime("%Y%m%d-06:00"))
        out = t.daily_format_cn()
        assert DATE_CN_RE.match(out), f"日期格式唔啱: {out!r}"
        assert not contains_simplified(out)
        seen.add(out[out.index("（") + 1:out.index("）")])
    assert seen == set(EXPECTED_WEEKDAYS)


def test_time_format_cn_format_lock():
    t = Timer(start="20260728-06:00")
    out = t.time_format_cn(datetime.datetime(2026, 7, 28, 14, 30))
    assert TIME_CN_RE.match(out), f"時間格式唔啱: {out!r}"
    assert out == "2026年07月28日（星期二）14:30"

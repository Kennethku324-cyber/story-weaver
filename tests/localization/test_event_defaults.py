"""Event 預設值回歸 — spec §8／§4.3。

Event 預設 predicate／object 必須係 keywords 常量（「此時」「空閒」），
唔准 hardcode 字面量漂移；agent.py 多處靠 `event.fit(None, KW_AT_THIS_TIME,
KW_IDLE)` 同 `event.object == KW_IDLE` 做邏輯判斷，預設值一漂移全鏈路斷。

行法：/Users/kenneth/Projects/story-weaver/.venv/bin/python -m pytest tests/localization/test_event_defaults.py
"""

from modules.memory.event import Event
from modules.prompt.keywords import KW_AT_THIS_TIME, KW_IDLE, KW_CHAT
from modules.model.text_normalize import contains_simplified


def test_constructor_defaults_are_keyword_constants():
    e = Event("伊莎貝拉")
    assert e.predicate is KW_AT_THIS_TIME or e.predicate == KW_AT_THIS_TIME
    assert e.object == KW_IDLE
    assert e.predicate == "此時"
    assert e.object == "空閒"


def test_constructor_none_args_fall_back_to_constants():
    e = Event("甲", None, None)
    assert e.predicate == KW_AT_THIS_TIME
    assert e.object == KW_IDLE


def test_update_defaults_are_keyword_constants():
    e = Event("甲", KW_CHAT, "乙")
    e.update()  # 唔俾 predicate/object → 重置為常量
    assert e.predicate == KW_AT_THIS_TIME
    assert e.object == KW_IDLE


def test_default_event_fit_keyword_constants():
    """agent.py:642 `event.fit(None, KW_AT_THIS_TIME, KW_IDLE)` 命中預設 Event。"""
    e = Event("甲")
    assert e.fit(None, KW_AT_THIS_TIME, KW_IDLE)


def test_explicit_values_not_overridden():
    e = Event("甲", KW_CHAT, "乙", describe="同乙傾偈")
    assert e.predicate == KW_CHAT
    assert e.object == "乙"


def test_to_dict_from_dict_round_trip_preserves_defaults():
    e = Event("甲")
    e2 = Event.from_dict(e.to_dict())
    assert e2.predicate == KW_AT_THIS_TIME
    assert e2.object == KW_IDLE
    assert e2.subject == "甲"


def test_defaults_contain_no_simplified():
    e = Event("甲")
    assert not contains_simplified(e.predicate)
    assert not contains_simplified(e.object)
    assert not contains_simplified(str(e))

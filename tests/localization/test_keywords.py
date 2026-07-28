"""關鍵字命中回歸 — spec §8「關鍵字回歸」。

鎖定 modules/prompt/keywords.py 常量值同命中行為：
- LLM 風格輸入「睡覺」→ KW_SLEEP 子串命中（簡繁同形字唔准改字形）
- Event.fit 用 KW_CHAT / KW_PENDING 等常量命中
- Boolean 解析 KW_TRUE_TOKENS 含「是」「係」容錯
- 所有常量本身唔准含簡體字（contains_simplified 把關）

另含 Review Blocker 1 回歸：Action.abstract() 狀態文案唔准出簡體
（「进行中」事件 — 2026-07-28 review 發現 action.py:25 簡體殘留）。

行法：/Users/kenneth/Projects/story-weaver/.venv/bin/python -m pytest tests/localization/test_keywords.py
"""

import datetime

from modules.model.text_normalize import contains_simplified
from modules.prompt import keywords as kw
from modules.memory.event import Event


# --- spec §3.1 常量值鎖定（逐項對照）---

def test_keyword_values_match_spec():
    assert kw.KW_AT_THIS_TIME == "此時"
    assert kw.KW_IDLE == "空閒"
    assert kw.KW_ONGOING == "正在"
    assert kw.KW_PENDING == "待開始"
    assert kw.KW_CHAT == "對話"
    assert kw.KW_OCCUPIED == "被佔用"


def test_sleep_keywords_same_form_chars_preserved():
    """簡繁同形字唔准改字形（spec §2.3）：「睡」「床」字面值鎖定。"""
    assert kw.KW_SLEEP == "睡"
    assert kw.KW_SLEEPING == "睡覺"
    assert kw.KW_BED == "床"
    assert kw.KW_SLEEPING_EN == "sleeping"
    assert kw.KW_LIVING_AREA == "living_area"


def test_true_tokens_include_hk_tolerance():
    """PRD Done When：保留「是」，新增「係」容錯。"""
    assert "是" in kw.KW_TRUE_TOKENS
    assert "係" in kw.KW_TRUE_TOKENS
    assert "true" in kw.KW_TRUE_TOKENS
    assert "yes" in kw.KW_TRUE_TOKENS
    assert "1" in kw.KW_TRUE_TOKENS


def test_traditional_chinese_directive():
    assert kw.TRADITIONAL_CHINESE_DIRECTIVE == "一律使用繁體中文（香港書面語）回答。"


# --- 命中行為回歸（spec §8 通過條件）---

def test_sleep_substring_hits_traditional_llm_output():
    """LLM 輸出繁體「睡覺」時 KW_SLEEP in describe 仍然命中。"""
    assert kw.KW_SLEEP in "睡覺"
    assert kw.KW_SLEEP in "正在睡覺"
    assert kw.KW_SLEEP in "上床睡覺"


def test_bed_substring_hits_sleep_address():
    """睡覺地址尾段「床」子串判斷命中（schedule.py 搵床邏輯）。"""
    address_tail = "床"
    assert kw.KW_BED in address_tail
    assert kw.KW_BED in "雙人床"


def test_event_fit_with_keyword_constants():
    """Event(...).fit(predicate=KW_CHAT) 命中。"""
    e = Event("伊莎貝拉", kw.KW_CHAT, "克勞斯")
    assert e.fit(predicate=kw.KW_CHAT)
    assert e.fit("伊莎貝拉", kw.KW_CHAT, "克勞斯")
    assert not e.fit(predicate=kw.KW_IDLE)


def test_pending_keyword_equality():
    assert "待開始" == kw.KW_PENDING
    e = Event("甲", "待開始", "x")
    assert e.predicate == kw.KW_PENDING


def test_keywords_contain_no_simplified():
    """所有邏輯常量本身唔准含簡體字。"""
    values = [
        kw.KW_AT_THIS_TIME, kw.KW_IDLE, kw.KW_ONGOING, kw.KW_PENDING,
        kw.KW_CHAT, kw.KW_OCCUPIED, kw.KW_SLEEP, kw.KW_SLEEPING, kw.KW_BED,
        kw.TRADITIONAL_CHINESE_DIRECTIVE,
    ]
    for v in values:
        assert not contains_simplified(v), f"常量含簡體字: {v!r}"


# --- Review Blocker 1 回歸：Action 狀態文案唔准簡體 ---

def test_action_abstract_status_is_traditional():
    """action.py「进行中」事件回歸：未完成 Action 嘅狀態必須係繁體「進行中」。

    呢個字串經 Action.abstract() 入 prompt 上下文，簡體殘留會令
    scan_simplified.py CI gate FAIL。
    """
    from modules.memory.action import Action

    event = Event("甲", kw.KW_ONGOING, kw.KW_SLEEPING,
                  address=["the Ville", "伊莎貝拉的公寓", "主人房"])
    start = datetime.datetime.now() - datetime.timedelta(minutes=10)
    action = Action(event, start=start, duration=60)  # 未完：end 喺未來
    assert not action.finished()
    status = action.abstract()["status"]
    assert status.startswith("進行中"), f"狀態文案唔係繁體「進行中」: {status!r}"
    assert not contains_simplified(status), f"狀態文案含簡體字: {status!r}"

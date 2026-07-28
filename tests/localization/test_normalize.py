"""Normalize 單測 — spec §8「Normalize 單測」。

覆蓋 spec §3.2 契約：
- 「对话记录」→「對話記錄」、「睡觉」→「睡覺」
- dict key 唔郁、value normalize
- 空串／純英文 fast path 原樣返回
- OpenCC 缺裝時 fallback dict 仍工作、永不拋異常

另含三條 Review 回歸：
- Blocker 3：床→牀 分歧。normalize 主路徑缺 VARIANT_FIX 後處理，
  LLM echo 地址尾段「床」被轉成「牀」，determine_object exact-match
  唔命中 → 靜默落 random failsafe。測試鎖定 normalize 輸出同
  build-time（scripts/localization/_convert.py VARIANT_FIX 11 組）對齊。
- 建議 4：glossary vocabulary 主路徑唔生效（「小睡一会儿」應轉「小睡片刻」）。
- 建議 5：build_fallback_dict 預設路徑 cwd-dependent，由 repo root
  啟動會靜默退回內置字對表，glossary 1522 條（含詞級替換）全部唔生效。

行法：/Users/kenneth/Projects/story-weaver/.venv/bin/python -m pytest tests/localization/test_normalize.py
"""

import modules.model.text_normalize as tn
from modules.model.text_normalize import (
    build_fallback_dict,
    contains_simplified,
    normalize_llm_output,
    normalize_text,
)


# --- 基本轉換（spec §8 通過條件）---

def test_basic_simplified_to_traditional():
    assert normalize_text("对话记录") == "對話記錄"
    assert normalize_text("睡觉") == "睡覺"


def test_already_traditional_unchanged():
    assert normalize_text("對話記錄") == "對話記錄"
    assert normalize_text("睡覺") == "睡覺"


def test_empty_and_non_cjk_fast_path():
    assert normalize_text("") == ""
    assert normalize_text("hello world 123") == "hello world 123"
    assert normalize_text("6:00") == "6:00"
    assert normalize_text("living_area") == "living_area"
    assert normalize_text("sleeping") == "sleeping"


def test_non_string_input_passthrough():
    assert normalize_text(None) is None
    assert normalize_text(123) == 123


# --- normalize_llm_output 遞歸契約 ---

def test_dict_keys_untouched_values_normalized():
    """dict `{"6:00": "睡觉"}` → key 不郁、value「睡覺」。"""
    out = normalize_llm_output({"6:00": "睡觉", "chat": "对话记录"})
    assert out == {"6:00": "睡覺", "chat": "對話記錄"}
    assert "6:00" in out  # JSON schema key 唔係自然語言，唔准郁


def test_list_tuple_container_types_preserved():
    assert normalize_llm_output(["睡觉", "对话"]) == ["睡覺", "對話"]
    assert normalize_llm_output(("睡觉",)) == ("睡覺",)
    assert isinstance(normalize_llm_output(("睡觉",)), tuple)


def test_non_string_scalars_passthrough():
    assert normalize_llm_output(1) == 1
    assert normalize_llm_output(True) is True
    assert normalize_llm_output(None) is None


def test_nested_structure():
    out = normalize_llm_output({"schedule": [{"6:00": "睡觉"}, 7, None]})
    assert out == {"schedule": [{"6:00": "睡覺"}, 7, None]}


# --- fallback 路徑（spec §8：OpenCC 缺裝時 fallback dict 仍工作）---

def test_fallback_path_when_opencc_unavailable(monkeypatch):
    """模擬 OpenCC 缺裝：normalize 降級 fallback，唔拋異常、基本轉換仍工作。"""
    monkeypatch.setattr(tn, "_get_opencc", lambda: None)
    assert normalize_text("对话记录") == "對話記錄"
    assert normalize_text("睡觉") == "睡覺"


def test_fallback_convert_direct():
    assert tn._fallback_convert("对话记录") == "對話記錄"
    assert tn._fallback_convert("睡觉") == "睡覺"


def test_normalize_never_raises(monkeypatch):
    """內部任何錯誤都 log warning 後返回原文（熱路徑唔可以斷）。"""
    def boom():
        raise RuntimeError("opencc explode")

    monkeypatch.setattr(tn, "_get_opencc", boom)
    assert normalize_text("对话") == "对话"
    # normalize_llm_output 同樣唔准拋
    assert normalize_llm_output({"k": "对话"}) == {"k": "对话"}


# --- contains_simplified 黑名單掃描 ---

def test_contains_simplified():
    assert contains_simplified("对话记录")
    assert contains_simplified("睡觉")
    assert not contains_simplified("對話記錄")
    assert not contains_simplified("睡覺")
    assert not contains_simplified("")
    assert not contains_simplified("hello")


# --- Review Blocker 3 回歸：床→牀 VARIANT_FIX 分歧 ---

def test_bed_char_preserved_through_normalize():
    """LLM 正確 echo 地址尾段「床」→ normalize 後必須仍然係「床」。

    determine_object 係 exact match（response if response in objects else
    failsafe）；maze／KW_BED／睡覺地址全部用「床」（glossary identity
    override）。「床」被轉成「牀」即靜默落 random failsafe 揀錯物件。
    """
    assert normalize_text("床") == "床"
    assert normalize_text("睡在床上") == "睡在床上"


def test_normalize_output_aligns_with_build_time_variant_fix():
    """主路徑輸出唔准含 build-time VARIANT_FIX 嘅異體字（11 組全對齊）。

    scripts/localization/_convert.py VARIANT_FIX：
    牀→床 衞→衛 啓→啟 説→說 閲→閱 悦→悅 脱→脫 税→稅 兑→兌 鋭→銳 藴→蘊
    build-time 同 runtime 轉換規則必須一致，否則記憶流文字同模板/maze
    用字長期異體字不一。
    """
    cases = {
        "床": "床",
        "卫生": "衛生",
        "启动": "啟動",
        "说话": "說話",
        "阅读": "閱讀",
        "悦": "悅",
        "脱": "脫",
        "税": "稅",
        "兑": "兌",
        "锐": "銳",
        "蕴": "蘊",
    }
    for src, expected in cases.items():
        assert normalize_text(src) == expected, (
            f"normalize_text({src!r}) = {normalize_text(src)!r}，"
            f"同 build-time VARIANT_FIX 輸出 {expected!r} 唔一致"
        )


def test_determine_object_echo_scenario_hits_exact_match():
    """模擬 prompt_determine_object._callback 嘅 exact-match 行為。

    LLM 正確 echo 候選地址「the Ville:伊莎貝拉的公寓:主人房:床」，
    normalize 之後必須仍然命中候選列表，唔准落 failsafe。
    """
    objects = ["the Ville:伊莎貝拉的公寓:主人房:床",
               "the Ville:伊莎貝拉的公寓:主人房:書桌"]
    llm_echo = "the Ville:伊莎貝拉的公寓:主人房:床"
    response = normalize_text(llm_echo)
    assert response in objects, (
        f"LLM echo 經 normalize 後唔命中候選地址（{response!r}），"
        "determine_object 會靜默落 random failsafe"
    )


# --- Review 建議 4 回歸：glossary vocabulary 主路徑 ---

def test_glossary_vocabulary_applies_in_main_path():
    """glossary vocabulary 段（書面用詞統一）喺 OpenCC 主路徑都要生效。

    PRD 意圖：「小睡一会儿」統一書面語「小睡片刻」（failsafe 日程、
    模板都用呢個寫法）；而家只喺 fallback 路徑生效，主路徑輸出
    「小睡一會兒」同 build-time 轉換唔對齊。
    """
    assert normalize_text("小睡一会儿") == "小睡片刻"


# --- Review 建議 5 回歸：fallback dict 載入唔准 cwd-dependent ---

def test_build_fallback_dict_loads_glossary_regardless_of_cwd(tmp_path, monkeypatch):
    """由任意 cwd 呼叫都應該載入到機讀 glossary（詞級替換入表）。

    而家預設路徑係相對 "data/glossary_s2hk.json"，由 repo root 啟動
    server 會靜默退回內置字對表，glossary 嘅詞級替換（如「小睡一会儿」）
    全部唔生效。應改用 __file__ 相對定位。
    """
    monkeypatch.chdir(tmp_path)  # 模擬由 repo root 以外嘅 cwd 啟動
    table = build_fallback_dict()
    assert "小睡一会儿" in table, (
        "fallback dict 未載入 glossary vocabulary 段（cwd-dependent 路徑問題）"
    )
    assert table.get("小睡一会儿") == "小睡片刻"

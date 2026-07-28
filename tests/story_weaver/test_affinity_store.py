"""AffinityStore / validate_setup 測試（spec §7）。"""

import pytest

from story_weaver.affinity import (
    AffinityStore,
    RelationInput,
    SetupAffinityPayload,
    SetupValidationError,
    UnknownAgentError,
    validate_setup,
)

NAMES = ["阿珍", "阿強", "小美", "阿明"]


def make_store(data=None):
    return AffinityStore(data if data is not None else {}, list(NAMES))


# ---------------------------------------------------------------- clamp


def test_set_affinity_clamps_to_bounds():
    s = make_store()
    s.set_affinity("阿珍", "阿強", 150)
    assert s.get("阿珍", "阿強").value == 100
    s.set_affinity("阿珍", "阿強", -999)
    assert s.get("阿珍", "阿強").value == -100


def test_set_affinity_label_truncated():
    s = make_store()
    s.set_affinity("阿珍", "阿強", 10, "x" * 200)
    assert len(s.get("阿珍", "阿強").label) == 100


def test_set_affinity_unknown_agent_raises():
    s = make_store()
    with pytest.raises(UnknownAgentError):
        s.set_affinity("阿珍", "阿強強", 10)


def test_adjust_delta_clamped_to_25():
    s = make_store()
    s.set_affinity("阿珍", "阿強", -65)
    change = s.adjust("阿珍", "阿強", 80, "幫忙解圍")
    assert change.delta == 25
    assert change.new == -40


def test_adjust_result_clamped_to_bounds():
    s = make_store()
    s.set_affinity("阿珍", "阿強", 95)
    change = s.adjust("阿珍", "阿強", 25, "好事")
    assert change.new == 100
    assert change.delta == 5


def test_adjust_zero_delta_returns_none():
    s = make_store()
    assert s.adjust("阿珍", "阿強", 0, "無事") is None


def test_adjust_absolute_bypasses_delta_clamp():
    s = make_store()
    s.set_affinity("阿珍", "阿強", -65)
    change = s.adjust("阿珍", "阿強", 0, "和好", absolute=True, absolute_value=60)
    assert change.new == 60
    assert change.delta == 125
    assert change.absolute is True


def test_adjust_unknown_agent_raises():
    s = make_store()
    with pytest.raises(UnknownAgentError):
        s.adjust("阿珍", "鬼", 5, "x")


# ---------------------------------------------------------------- band


@pytest.mark.parametrize(
    "value,expected",
    [
        (100, "摯愛/至交"), (61, "摯愛/至交"),
        (60, "友好"), (21, "友好"),
        (20, "略有好感"), (1, "略有好感"),
        (0, "陌生/中立"),
        (-1, "略有反感"), (-20, "略有反感"),
        (-21, "敵對"), (-60, "敵對"),
        (-61, "死敵/痛恨"), (-100, "死敵/痛恨"),
    ],
)
def test_band_of(value, expected):
    assert AffinityStore.band_of(value) == expected


# ---------------------------------------------------------------- ensure_pairs / legacy


def test_ensure_pairs_fills_full_matrix():
    data = {}
    s = make_store(data)
    for a in NAMES:
        assert set(data[a].keys()) == {b for b in NAMES if b != a}
        assert all(v == {"value": 0, "label": ""} for v in data[a].values())
    assert s.get("阿珍", "阿強").value == 0


def test_legacy_checkpoint_empty_dict_no_crash():
    """舊 checkpoint 無 affinity key → setdefault 俾 {} → 補 0，唔 crash。"""
    config = {}
    s = AffinityStore(config.setdefault("affinity", {}), list(NAMES))
    assert s.relation_line("阿珍", "阿強") == "「阿珍與阿強並不相識（陌生/中立）。」"


def test_ensure_pairs_removes_unknown_agents():
    data = {"鬼": {"阿珍": {"value": 50, "label": ""}}, "阿珍": {"鬼": {"value": 1, "label": ""}}}
    make_store(data)
    assert "鬼" not in data
    assert "鬼" not in data["阿珍"]


def test_get_unknown_pair_returns_default():
    s = make_store()
    entry = s.get("不存在", "都唔存在")
    assert entry.value == 0 and entry.label == ""


def test_to_dict_shares_reference():
    data = {}
    s = make_store(data)
    assert s.to_dict() is data
    s.set_affinity("阿珍", "阿強", 30)
    assert data["阿珍"]["阿強"]["value"] == 30


# ---------------------------------------------------------------- relation_line


def test_relation_line_with_label():
    s = make_store()
    s.set_affinity("阿珍", "阿強", -65, "舊情人，分手時鬧得好僵")
    assert s.relation_line("阿珍", "阿強") == (
        "「阿珍對阿強的好感度為-65（死敵/痛恨）：舊情人，分手時鬧得好僵。」"
    )


def test_relation_line_without_label():
    s = make_store()
    s.set_affinity("阿珍", "阿強", 40)
    assert s.relation_line("阿珍", "阿強") == "「阿珍對阿強的好感度為40（友好）。」"


def test_relation_line_zero():
    s = make_store()
    assert s.relation_line("小美", "阿明") == "「小美與阿明並不相識（陌生/中立）。」"


# ---------------------------------------------------------------- validate_setup


def payload(relations):
    return SetupAffinityPayload(
        agents=list(NAMES),
        relations=[RelationInput(**r) for r in relations],
    )


def test_validate_setup_ok_and_fills_zeros():
    result = validate_setup(payload([
        {"from": "阿珍", "to": "阿強", "affinity": -65, "label": "舊情人"},
    ]))
    matrix = result.model_dump()["affinity"]
    assert matrix["阿珍"]["阿強"] == {"value": -65, "label": "舊情人"}
    assert matrix["阿強"]["阿珍"] == {"value": 0, "label": ""}
    for a in NAMES:
        assert a not in matrix[a]  # 對角線停用


def test_validate_setup_typo_rejected():
    with pytest.raises(SetupValidationError) as exc:
        validate_setup(payload([
            {"from": "阿珍", "to": "阿強強", "affinity": 10},
        ]))
    err = exc.value.errors[0].model_dump(by_alias=True)
    assert err["to"] == "阿強強"
    assert "阿強強" in err["message"]


def test_validate_setup_diagonal_rejected():
    with pytest.raises(SetupValidationError) as exc:
        validate_setup(payload([
            {"from": "阿珍", "to": "阿珍", "affinity": 10},
        ]))
    assert "自己" in exc.value.errors[0].message


def test_validate_setup_duplicate_later_wins():
    result = validate_setup(payload([
        {"from": "阿珍", "to": "阿強", "affinity": 10},
        {"from": "阿珍", "to": "阿強", "affinity": -50, "label": "改咗主意"},
    ]))
    matrix = result.model_dump()["affinity"]
    assert matrix["阿珍"]["阿強"] == {"value": -50, "label": "改咗主意"}


def test_validate_setup_out_of_range_rejected_by_pydantic():
    with pytest.raises(Exception):
        payload([{"from": "阿珍", "to": "阿強", "affinity": 200}])

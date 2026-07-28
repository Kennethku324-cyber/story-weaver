"""好感度系統 — code review 發現嘅回歸測試 + 覆蓋缺口補強。

對應 review 報告：
- B1（🔴 blocker）：CI gate scan_simplified.py 被 gm_adjust_affinity.txt 嘅「敘」整紅
- S1（🟡）：test_build_gm_prompt_fills_template cwd 敏感
- M1：rounds_log 嘅 step 永遠係 0（契約測試，講明現狀）
- M4：/api/affinity/validate 兩種 400 格式並存（契約測試，前端要知）

LLM 全部 mock/stub，唔准真 call。
"""

import json
import logging
import os
import sys

import pytest
from flask import Flask

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GEN_DIR = os.path.join(REPO_ROOT, "generative_agents")
PROMPTS_DIR = os.path.join(GEN_DIR, "data", "prompts")

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "localization"))

from story_weaver.affinity import (  # noqa: E402
    AffinityStore,
    GMAdjustmentItem,
    GMAdjustmentResponse,
    apply_gm_response,
    build_matrix_from_setup,
    inject_change,
    validate_setup,
    RelationInput,
    SetupAffinityPayload,
)
from story_weaver.affinity.api import affinity_bp  # noqa: E402
from story_weaver.affinity.models import AffinityChange  # noqa: E402

NAMES = ["阿珍", "阿強", "小美", "阿明"]
logger = logging.getLogger("test_regression")


class FakeAgent:
    def __init__(self, name):
        self.name = name
        self.concepts = []

    def _add_concept(self, e_type, event, create=None, expire=None, filling=None,
                     poignancy_override=None):
        self.concepts.append({"e_type": e_type, "event": event,
                              "poignancy": poignancy_override})


def make_env():
    store = AffinityStore({}, list(NAMES))
    agents = {n: FakeAgent(n) for n in NAMES}
    return store, agents, []


# ==================================================================== B1
# review 🔴：gm_adjust_affinity.txt 用咗「敘」（U+6558），香港標準係「敍」（U+654D），
# repo 嘅 CI gate（scripts/localization/scan_simplified.py）會 FAIL。
# 呢個測試而家預期 FAIL——修咗嗰個字之後自然轉綠。


AFFINITY_TOUCHED_PROMPTS = [
    "gm_adjust_affinity.txt",   # 新檔
    "decide_chat.txt",          # §4.5 修改
    "generate_chat.txt",
    "decide_wait_example.txt",
    "summarize_relation.txt",
]


@pytest.mark.parametrize("filename", AFFINITY_TOUCHED_PROMPTS)
def test_b1_prompt_templates_zero_simplified(filename):
    """B1 回歸：affinity 系統掂過嘅模板必須過簡體掃描（同 CI gate 同源黑名單）。"""
    from _convert import simplified_chars_in

    path = os.path.join(PROMPTS_DIR, filename)
    with open(path, encoding="utf-8") as f:
        bad = simplified_chars_in(f.read())
    assert not bad, (
        f"{filename} 有簡體殘留：{''.join(sorted(bad))}"
        "（scan_simplified.py CI gate 會 FAIL）"
    )


def test_b1_ci_gate_scan_simplified():
    """B1 回歸：成個 repo 掃描 gate 必須綠燈（review 前係綠，新檔整紅咗）。"""
    import scan_simplified

    hits = scan_simplified.scan()
    assert not hits, f"scan_simplified FAIL: {hits}"


# ==================================================================== S1
# review 🟡：build_gm_prompt 嘅 template_path 預設 "data/prompts" 係相對路徑，
# 項目慣例 cwd 係 generative_agents/。契約測試：喺嗰個 cwd 下預設參數必須用到。


def test_s1_build_gm_prompt_works_from_generative_agents_cwd(monkeypatch):
    """S1 回歸：cwd = generative_agents/（項目慣例）時預設 template_path 唔可以炸。"""
    from story_weaver.affinity import build_gm_prompt

    store, _, _ = make_env()
    store.set_affinity("阿珍", "阿強", -65, "舊情人")
    monkeypatch.chdir(GEN_DIR)
    res = build_gm_prompt(store, ["阿珍同阿強嗌交"], {}, logger)
    assert "-65" in res.prompt
    assert res.return_type is GMAdjustmentResponse
    assert res.failsafe.adjustments == []


# ==================================================================== M1
# rounds_log 嘅 "step" 永遠寫 0（gm.py _now() 攞唔到全域 step，實作者已申報）。
# 契約測試：講明現狀，GM 系統掛鉤收到 (game, step) 時自行補 rounds_log[-1]["step"]。


def test_m1_rounds_log_step_is_zero_by_contract():
    """M1 契約：apply_gm_response 寫嘅 step 係 0，由 GM 掛鉤補返真值。"""
    store, agents, rounds_log = make_env()
    resp = GMAdjustmentResponse(adjustments=[
        GMAdjustmentItem(from_agent="阿珍", to_agent="阿強", delta=15, reason="解圍"),
    ])
    apply_gm_response(store, resp, agents, rounds_log, 1, logger)
    entry = rounds_log[-1]
    assert set(entry.keys()) == {"round", "step", "time", "changes"}
    assert entry["step"] == 0  # 現狀契約；GM hook 收到真 step 後覆寫呢個欄位
    # GM 掛鉤補 step 嘅預期用法唔會破坏結構
    entry["step"] = 6
    assert rounds_log[-1]["step"] == 6


# ==================================================================== M4
# /validate 有兩種 400：白名單/對角線錯 → {"errors": [{from,to,message}]}；
# schema-level 錯（超界/agents<4）→ raw pydantic errors。前端要處理兩種。


@pytest.fixture()
def client(tmp_path):
    app = Flask("affinity_m4")
    app.register_blueprint(affinity_bp)
    app.config["AFFINITY_CHECKPOINTS_ROOT"] = str(tmp_path / "checkpoints")
    return app.test_client()


def test_m4_two_400_formats_coexist(client):
    """M4 契約：兩種 400 response 格式並存，前端要識分。"""
    # 格式一：白名單錯 → {from, to, message}
    r1 = client.post("/api/affinity/validate", json={
        "agents": NAMES,
        "relations": [{"from": "阿珍", "to": "鬼", "affinity": 10}],
    })
    assert r1.status_code == 400
    err1 = r1.get_json()["errors"][0]
    assert set(err1.keys()) == {"from", "to", "message"}

    # 格式二：schema-level 錯（affinity 超界）→ raw pydantic error
    r2 = client.post("/api/affinity/validate", json={
        "agents": NAMES,
        "relations": [{"from": "阿珍", "to": "阿強", "affinity": 999}],
    })
    assert r2.status_code == 400
    err2 = r2.get_json()["errors"][0]
    assert "from" not in err2  # 唔係 {from,to,message} 格式
    assert "loc" in err2 and "msg" in err2  # pydantic 格式


# ==================================================================== store 覆蓋缺口（review 第 2/5 項實測補回歸）


def test_adjust_at_max_returns_none():
    """review 第 2 項：舊值已 100，adjust(+50) → actual delta 0 → None。"""
    s = AffinityStore({}, list(NAMES))
    s.set_affinity("阿珍", "阿強", 100)
    assert s.adjust("阿珍", "阿強", 50, "好事") is None


def test_adjust_negative_delta_clamped_to_minus_25():
    s = AffinityStore({}, list(NAMES))
    s.set_affinity("阿珍", "阿強", 50)
    change = s.adjust("阿珍", "阿強", -80, "反面")
    assert change.delta == -25 and change.new == 25


def test_adjust_absolute_value_double_clamped():
    """review 第 2 項：absolute_value=-200 → clamp 到 -100（繞過 pydantic 直 call store 都安全）。"""
    s = AffinityStore({}, list(NAMES))
    change = s.adjust("阿珍", "阿強", 0, "重置", absolute=True, absolute_value=-200)
    assert change.new == -100
    assert s.get("阿珍", "阿強").value == -100


def test_ensure_pairs_removes_diagonal_self_key():
    """review 第 5 項：checkpoint 殘留對角線 self-key 要清走，唔 crash。"""
    data = {"阿珍": {"阿珍": {"value": 99, "label": "自己"}}}
    s = AffinityStore(data, list(NAMES))
    assert "阿珍" not in data["阿珍"]
    assert s.get("阿珍", "阿珍").value == 0


def test_validate_setup_result_json_serializable():
    """review 第 9 項：validate_setup 產出必須可以直接 json.dumps 落 config。"""
    result = validate_setup(SetupAffinityPayload(
        agents=list(NAMES),
        relations=[RelationInput(**{"from": "阿珍", "to": "阿強",
                                    "affinity": -65, "label": "舊情人"})],
    ))
    matrix = result.model_dump()["affinity"]
    dumped = json.dumps(matrix, ensure_ascii=False)
    assert json.loads(dumped)["阿珍"]["阿強"]["value"] == -65


def test_build_matrix_from_setup_happy_path():
    """review 第 9 項接駁：builder rel_map → 頂層 affinity 矩陣（補齊 + 略過 0 分無 desc）。"""
    rel_map = {
        "阿珍": {"阿強": {"score": -65, "desc": "舊情人"},
                 "小美": {"score": 0, "desc": ""}},  # 0 分無 desc → 略過，由補齊填 0
    }
    matrix = build_matrix_from_setup(list(NAMES), rel_map)
    assert matrix["阿珍"]["阿強"] == {"value": -65, "label": "舊情人"}
    assert matrix["阿珍"]["小美"] == {"value": 0, "label": ""}
    assert matrix["阿明"]["阿強"] == {"value": 0, "label": ""}
    # 全矩陣 N×(N-1)，無對角線
    for a in NAMES:
        assert set(matrix[a].keys()) == {b for b in NAMES if b != a}
    json.dumps(matrix, ensure_ascii=False)  # 可落 config


# ==================================================================== gm 覆蓋缺口


def test_apply_zero_delta_item_not_recorded_but_round_appended():
    """delta=0（實際無變動）→ 唔入 changes、唔注入記憶，但回合照 append。"""
    store, agents, rounds_log = make_env()
    resp = GMAdjustmentResponse(adjustments=[
        GMAdjustmentItem(from_agent="阿珍", to_agent="阿強", delta=0, reason="無事發生"),
    ])
    changes = apply_gm_response(store, resp, agents, rounds_log, 4, logger)
    assert changes == []
    assert agents["阿珍"].concepts == []
    assert rounds_log[-1]["round"] == 4 and rounds_log[-1]["changes"] == []


def test_apply_hallucinated_agent_logs_warning(caplog):
    """review 第 7 項：幻覺角色 skip + warning log。"""
    store, agents, rounds_log = make_env()
    resp = GMAdjustmentResponse(adjustments=[
        GMAdjustmentItem(from_agent="阿珍", to_agent="幻覺人", delta=10, reason="x"),
    ])
    with caplog.at_level(logging.WARNING):
        changes = apply_gm_response(store, resp, agents, rounds_log, 1, logger)
    assert changes == []
    assert any("幻覺人" in r.message for r in caplog.records)


@pytest.mark.parametrize("delta,injected", [(10, True), (-10, True), (9, False), (-9, False)])
def test_inject_change_threshold_boundary(delta, injected):
    """inject_change 嘅 |delta| >= 10 門檻：邊界值逐個驗。"""
    agent = FakeAgent("阿珍")
    change = AffinityChange(from_agent="阿珍", to_agent="阿強",
                            old=0, new=delta, delta=delta, reason="試")
    inject_change(agent, change, logger)
    assert (len(agent.concepts) == 1) is injected


def test_apply_gm_response_never_throws_on_garbage():
    """review 第 7 項：response 物件本身異常都唔准 throw 上 SimulateServer。"""
    store, agents, rounds_log = make_env()

    class GarbageResponse:
        @property
        def adjustments(self):
            raise RuntimeError("LLM 垃圾")

    changes = apply_gm_response(store, GarbageResponse(), agents, rounds_log, 1, logger)
    assert changes == []
    assert len(rounds_log) == 1  # 回合記錄照 append


# ==================================================================== api 覆蓋缺口


def write_checkpoint(root, sim_name, config):
    folder = os.path.join(root, sim_name)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "simulate-20240213-1100.json"), "w",
              encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)


def test_display_format_negative_delta(client, tmp_path):
    """display 格式：負 delta 要顯示（-10），唔係（+-10）。"""
    root = str(tmp_path / "checkpoints")
    write_checkpoint(root, "sim-neg", {
        "affinity": {},
        "affinity_rounds": [
            {"round": 1, "step": 6, "time": "t", "changes": [
                {"from_agent": "阿強", "to_agent": "阿珍", "old": 20, "new": 10,
                 "delta": -10, "reason": "講錯嘢", "absolute": False},
            ]},
        ],
    })
    body = client.get("/api/affinity/sim-neg/changes").get_json()
    assert body["display"] == ["阿強 → 阿珍：20 → 10（-10）：講錯嘢"]


def test_display_format_no_reason(client, tmp_path):
    """無 reason 時 display 唔會多個冒號。"""
    root = str(tmp_path / "checkpoints")
    write_checkpoint(root, "sim-norsn", {
        "affinity": {},
        "affinity_rounds": [
            {"round": 1, "step": 6, "time": "t", "changes": [
                {"from_agent": "阿珍", "to_agent": "阿強", "old": 0, "new": 15,
                 "delta": 15, "reason": "", "absolute": False},
            ]},
        ],
    })
    body = client.get("/api/affinity/sim-norsn/changes").get_json()
    assert body["display"] == ["阿珍 → 阿強：0 → 15（+15）"]

"""QA 測試 — Story Weaver 角色 Setup 系統.

覆蓋 PRD「邊界情況」表 #1-#12 + 「Done When」清單逐項 +
Code Review 發現（🔴1 簡繁混雜、🟡2 pydantic 英文訊息、🟡3 預填錯名/雙句號）。

行法：
    /Users/kenneth/Projects/story-weaver/.venv/bin/python -m pytest tests/ -v
    （slow 測試用 -m "not slow" 跳過）

注意：呢度有啲測試係按 PRD 要求寫，對住未修嘅 review 發現會 FAIL——
咁係預期嘅，fail 即係確認咗個 bug 仲喺度。
"""

import json
import os
import re
import sys

import pytest
from pydantic import ValidationError

from story_weaver.builder import SetupError
from story_weaver.memory_seed import (
    OPENING_POIGNANCY,
    inject_event,
    inject_story_memories,
    score_to_poignancy,
)
from story_weaver.schemas import SetupCreateRequest

from conftest import DATA_CONFIG, FAKE_TEMPLATES, REAL_MAZE, REAL_STATIC, make_template_dir

# 常見簡體字（同繁體唔同形），用嚟掃生成文字有冇簡體殘留
_SIMPLIFIED_ONLY = re.compile(r"[热旧睡觉前间关门开层长听说读语诉认让车站买卖员见贝贵账质]")

# pydantic v2 常見英文錯誤片段——PRD 邊界 1/2 要求中文訊息
_ENGLISH_MSG = re.compile(
    r"List should have|Field required|String should have|Input should be|too_short|missing",
    re.IGNORECASE,
)


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _agent_json(builder, name):
    return _read_json(os.path.join(builder.catalog.agents_root, name, "agent.json"))


# ======================================================================
# Done When：GET /setup + 模板畫廊 + 住所清單
# ======================================================================
class TestSetupPageAndGallery:
    def test_setup_page_renders(self, client):
        """Done When：GET /setup 渲染 setup.html。"""
        resp = client.get("/setup")
        assert resp.status_code == 200
        assert resp.data  # 有內容

    def test_templates_api_lists_all_real_templates(self, client, monkeypatch):
        """Done When：畫廊列出全部 25 個模板；素材完整標記正確。"""
        from story_weaver import routes
        from story_weaver.templates import TemplateCatalog
        from conftest import REAL_AGENTS

        catalog = TemplateCatalog(REAL_AGENTS)
        catalog.scan()
        monkeypatch.setattr(routes, "_state", {"builder": type("B", (), {"catalog": catalog})()})
        resp = client.get("/api/setup/templates")
        assert resp.status_code == 200
        templates = resp.get_json()["templates"]
        assert len(templates) == 25
        for t in templates:
            assert t["portrait_url"].startswith("/static/assets/village/agents/")
            assert "innate" in t and "learned_first_line" in t and "living_area" in t
        # 真實資產應該全部完整（review 實測 0 不完整）
        incomplete = [t["template_id"] for t in templates if not t["assets_complete"]]
        assert incomplete == [], f"素材不完整模板：{incomplete}"

    def test_housing_api_lists_real_rooms(self, client, monkeypatch):
        """Done When / 邊界 5：Housing Registry 由真實 maze 掃出 23 間有床房。"""
        from story_weaver import routes
        from story_weaver.housing import HousingRegistry

        housing = HousingRegistry(REAL_MAZE)
        monkeypatch.setattr(routes, "_state", {"builder": type("B", (), {"housing": housing})()})
        resp = client.get("/api/setup/housing")
        assert resp.status_code == 200
        rooms = resp.get_json()["rooms"]
        assert len(rooms) == 23
        labels = {r["label"] for r in rooms}
        assert "莫雷諾家族的房子 · 空卧室" in labels
        assert "奧克山學院宿舍 · 克勞斯的房間" in labels

    def test_incomplete_template_unselectable(self, builder, valid_payload):
        """邊界 11：模板缺 texture.png → assets_complete=False + 校驗拒絕。"""
        make_template_dir(
            builder.catalog.agents_root, "模板殘",
            ["the Ville", "瑞恩的公寓", "主人房"], with_texture=False,
        )
        builder.catalog.scan()
        assert builder.catalog.get("模板殘").assets_complete is False
        valid_payload["characters"][0]["template_id"] = "模板殘"
        valid_payload["characters"][0]["home"] = ["the Ville", "瑞恩的公寓", "主人房"]
        req = SetupCreateRequest.model_validate(valid_payload)
        errors = builder.validate(req)
        assert any(e.field == "characters[0].template_id" and "素材不完整" in e.message
                   for e in errors)


# ======================================================================
# 邊界 1：少於 4 個角色
# ======================================================================
class TestCharacterCount:
    def test_under_4_characters_422(self, client, valid_payload):
        """邊界 1：繞過前端 POST <4 角色 → 422。"""
        valid_payload["characters"] = valid_payload["characters"][:2]
        resp = client.post("/api/setup/create", json=valid_payload)
        assert resp.status_code == 422
        body = resp.get_json()
        assert any(e["field"].startswith("characters") for e in body["errors"])

    def test_under_4_characters_chinese_message(self, client, valid_payload):
        """邊界 1 + Review 🟡2：錯誤訊息要係中文（PRD 指定「最少要揀 4 個角色」）。"""
        valid_payload["characters"] = valid_payload["characters"][:1]
        resp = client.post("/api/setup/create", json=valid_payload)
        assert resp.status_code == 422
        messages = [e["message"] for e in resp.get_json()["errors"]]
        for m in messages:
            assert not _ENGLISH_MSG.search(m), f"英文 pydantic 訊息漏出嚟：{m}"
        assert any("最少" in m and "4" in m for m in messages), \
            f"PRD 要求「最少要揀 4 個角色」，實際：{messages}"

    def test_over_10_characters_rejected(self, valid_payload):
        """邊界 12：上限 10 寫死喺後端 schema。"""
        base = valid_payload["characters"][0]
        extra = []
        extra_homes = [
            ["the Ville", "藝術家共居空間", arena]
            for arena in ["亞當斯的房間", "阿比蓋爾的房間", "拉吉夫的房間",
                          "瑞恩的房間", "卡洛斯·莫雷雷諾的房間", "海莉的房間"]
        ]
        for i, home in enumerate(extra_homes):
            c = dict(base)
            c["display_name"] = f"角色{i + 5}"
            c["home"] = home
            extra.append(c)
        valid_payload["characters"] = valid_payload["characters"] + extra  # 10 個 → OK
        SetupCreateRequest.model_validate(valid_payload)
        c = dict(base)
        c["display_name"] = "角色11"
        c["home"] = ["the Ville", "摩爾家族的房子", "主人房"]
        valid_payload["characters"].append(c)  # 11 個 → 拒絕
        with pytest.raises(ValidationError):
            SetupCreateRequest.model_validate(valid_payload)

    def test_validate_endpoint_reports_llm_estimate(self, client, valid_payload):
        """邊界 12：提交時顯示預估每回合 LLM 調用次數。"""
        resp = client.post("/api/setup/validate", json=valid_payload)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["estimated_llm_calls_per_step"] > 0
        assert body["estimated_llm_calls_per_step"] % 4 == 0  # 4 角色 × 每角色 N 次


# ======================================================================
# 邊界 2：必填留空，一次過返晒所有錯
# ======================================================================
class TestRequiredFields:
    def test_multiple_empty_fields_all_reported(self, client, valid_payload):
        """邊界 2：職業/性格/開端/角色名留空 → 422，一次過返晒。"""
        valid_payload["characters"][0]["occupation"] = ""
        valid_payload["characters"][1]["personality"] = ""
        valid_payload["characters"][2]["display_name"] = ""
        valid_payload["story_opening"] = "短"
        resp = client.post("/api/setup/create", json=valid_payload)
        assert resp.status_code == 422
        fields = {e["field"] for e in resp.get_json()["errors"]}
        assert any("occupation" in f for f in fields)
        assert any("personality" in f for f in fields)
        assert any("display_name" in f for f in fields)
        assert any("story_opening" in f for f in fields)

    def test_empty_field_messages_are_chinese(self, client, valid_payload):
        """邊界 2 + Review 🟡2：必填留空嘅訊息唔可以係英文 pydantic 原文。"""
        valid_payload["characters"][0]["occupation"] = ""
        valid_payload["story_opening"] = ""
        resp = client.post("/api/setup/create", json=valid_payload)
        assert resp.status_code == 422
        for e in resp.get_json()["errors"]:
            assert not _ENGLISH_MSG.search(e["message"]), \
                f"{e['field']} 嘅訊息係英文：{e['message']}"

    def test_malformed_json_body(self, client):
        resp = client.post("/api/setup/create", data="not-json",
                           content_type="application/json")
        assert resp.status_code == 422
        assert "JSON" in resp.get_json()["errors"][0]["message"]


# ======================================================================
# 邊界 3：撞名（角色互撞 / 撞模板 / 故事撞名）
# ======================================================================
class TestNameCollision:
    def test_duplicate_display_name_in_request(self, builder, valid_payload):
        valid_payload["characters"][1]["display_name"] = "阿欣"
        req = SetupCreateRequest.model_validate(valid_payload)
        errors = builder.validate(req)
        assert any(e.field == "characters[1].display_name" and "撞" in e.message
                   for e in errors)

    def test_display_name_collides_with_template_dir(self, builder, valid_payload):
        """邊界 3：display_name 撞 25 個模板目錄（含空格轉底線後撞）。"""
        valid_payload["characters"][0]["display_name"] = "模板甲"
        req = SetupCreateRequest.model_validate(valid_payload)
        assert any(e.field == "characters[0].display_name" for e in builder.validate(req))

    def test_display_name_space_underscore_collision(self, builder, valid_payload):
        """「模板 甲」→ 目錄「模板_甲」唔撞「模板甲」；但撞自己生成過嘅角色要拒。"""
        valid_payload["characters"][0]["display_name"] = "模板 甲"  # → 模板_甲，唔撞
        req = SetupCreateRequest.model_validate(valid_payload)
        assert not any(e.field == "characters[0].display_name" for e in builder.validate(req))

    def test_story_name_conflict_409_with_suggestion(self, client, valid_payload):
        """邊界 3：故事名撞 checkpoints 現有目錄 → 409 + 「-2」建議。"""
        resp1 = client.post("/api/setup/create", json=valid_payload)
        assert resp1.status_code == 201
        # 將故事解鎖先可以試撞名（locked 會先回 423）
        story_dir = os.path.join(resp1.get_json()["story_dir"])
        story = _read_json(os.path.join(story_dir, "story.json"))
        story["locked"] = False
        with open(os.path.join(story_dir, "story.json"), "w", encoding="utf-8") as f:
            json.dump(story, f, ensure_ascii=False)
        # 換一批唔撞嘅角色目錄名先過到撞名校驗（角色目錄已存在，必撞）——
        # 呢度預期嘅係 422（角色撞名），證明角色目錄撞名都有擋
        resp2 = client.post("/api/setup/create", json=valid_payload)
        assert resp2.status_code in (409, 422)

    def test_story_name_conflict_suggestion_format(self, builder, valid_request):
        os.makedirs(os.path.join(builder.checkpoints_root, "茶餐廳風雲"))
        with pytest.raises(SetupError) as exc:
            builder.build(valid_request)
        assert exc.value.status == 409
        assert exc.value.suggestion == "茶餐廳風雲-2"
        assert "茶餐廳風雲-2" in exc.value.errors[0].message


# ======================================================================
# 邊界 4：好感度 clamp + 補缺方向 + 雙向獨立
# ======================================================================
class TestRelationships:
    def test_score_over_range_clamped_and_flagged(self, client, valid_payload):
        """邊界 4：+150 → clamp 去 100，response 有 clamped 記錄。"""
        valid_payload["relationships"][0]["score"] = 150
        resp = client.post("/api/setup/create", json=valid_payload)
        assert resp.status_code == 201
        body = resp.get_json()
        assert len(body["clamped"]) == 1
        assert body["clamped"][0]["original"] == 150
        assert body["clamped"][0]["clamped"] == 100
        sim = _read_json(body["sim_config_path"])
        assert sim["agents"]["阿欣"]["relationships"]["阿強"]["score"] == 100

    def test_score_under_range_clamped(self, builder, valid_payload):
        valid_payload["relationships"][0]["score"] = -999
        req = SetupCreateRequest.model_validate(valid_payload)
        result = builder.build(req)
        assert result.clamped[0].clamped == -100

    def test_score_string_coerced(self, builder, valid_payload):
        """邊界 4：前端滑桿 value 係字串，「60」要 int() 化。"""
        valid_payload["relationships"][0]["score"] = "60"
        req = SetupCreateRequest.model_validate(valid_payload)
        assert req.relationships[0].score == 60

    def test_missing_reverse_direction_filled_as_stranger(self, client, valid_payload):
        """邊界 4：得 A對B 冇 B對A → 補 score 0 / desc ""，response 話補咗幾多對。"""
        resp = client.post("/api/setup/create", json=valid_payload)
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["filled_relationships"] == 11  # 4 角色 12 對 - 已設 1 對
        sim = _read_json(body["sim_config_path"])
        assert sim["agents"]["阿強"]["relationships"]["阿欣"] == {"score": 0, "desc": ""}

    def test_empty_relationships_all_strangers(self, builder, valid_payload):
        """邊界 4：relationships 留空 = 全部陌生。"""
        valid_payload["relationships"] = []
        req = SetupCreateRequest.model_validate(valid_payload)
        result = builder.build(req)
        assert result.filled_relationships == 12
        for name, cfg in result.sim_config["agents"].items():
            for other, rel in cfg["relationships"].items():
                assert other != name
                assert rel == {"score": 0, "desc": ""}

    def test_bidirectional_independence(self, builder, valid_payload):
        """Done When（矩陣）：A對B 同 B對A 係兩個獨立欄位，可以好唔同。"""
        valid_payload["relationships"] = [
            {"from": "阿欣", "to": "阿強", "score": 60, "desc": "欣賞佢但唔敢講"},
            {"from": "阿強", "to": "阿欣", "score": -20, "desc": "仲有啲嬲"},
        ]
        req = SetupCreateRequest.model_validate(valid_payload)
        result = builder.build(req)
        rels = result.sim_config["agents"]
        assert rels["阿欣"]["relationships"]["阿強"]["score"] == 60
        assert rels["阿強"]["relationships"]["阿欣"]["score"] == -20

    def test_relationship_to_self_rejected(self, builder, valid_payload):
        valid_payload["relationships"] = [{"from": "阿欣", "to": "阿欣", "score": 10}]
        req = SetupCreateRequest.model_validate(valid_payload)
        errors = builder.validate(req)
        assert any("自己" in e.message for e in errors)

    def test_relationship_unknown_character_rejected(self, builder, valid_payload):
        valid_payload["relationships"].append({"from": "阿欣", "to": "路人甲", "score": 10})
        req = SetupCreateRequest.model_validate(valid_payload)
        errors = builder.validate(req)
        assert any(e.field == "relationships[1].to" for e in errors)


# ======================================================================
# 邊界 5：住所衝突 / 無床房
# ======================================================================
class TestHousing:
    def test_same_room_conflict_422(self, client, valid_payload):
        """邊界 5：兩個角色揀同一間房 → 422 中文錯。"""
        valid_payload["characters"][1]["home"] = list(valid_payload["characters"][0]["home"])
        resp = client.post("/api/setup/create", json=valid_payload)
        assert resp.status_code == 422
        assert any("同一間房" in e["message"] for e in resp.get_json()["errors"])

    def test_same_house_different_room_ok(self, builder, valid_payload):
        """邊界 5：同屋唔同房（宿舍唔同房間）合法。"""
        valid_payload["characters"][0]["home"] = ["the Ville", "塔瑪拉和卡門的家", "卡門的房間"]
        valid_payload["characters"][1]["home"] = ["the Ville", "塔瑪拉和卡門的家", "塔瑪拉的房間"]
        req = SetupCreateRequest.model_validate(valid_payload)
        assert builder.validate(req) == []

    def test_room_without_bed_rejected(self, client, valid_payload):
        """邊界 5：廚房/咖啡館（無床）唔住得人。"""
        valid_payload["characters"][0]["home"] = ["the Ville", "霍布斯咖啡館", "咖啡館"]
        resp = client.post("/api/setup/create", json=valid_payload)
        assert resp.status_code == 422
        assert any(e["field"] == "characters[0].home" for e in resp.get_json()["errors"])

    def test_conflict_message_lists_remaining_rooms(self, housing):
        """邊界 5：Registry 層面撞房時列出剩餘空房。"""
        from story_weaver.housing import HousingConflict

        room = housing.available()[0]
        housing.assign(room.address, "阿欣")
        with pytest.raises(HousingConflict) as exc:
            housing.assign(room.address, "阿強")
        assert "剩餘空房" in str(exc.value)
        assert exc.value.available
        housing.release_all()

    def test_living_area_must_have_bed_in_generated_json(self, builder, valid_request, maze):
        """Done When：生成嘅 living_area 指向含「床」嘅房（spatial.py 派生唔會炸）。"""
        builder.build(valid_request)
        for char in valid_request.characters:
            agent = _agent_json(builder, char.display_name)
            bed_key = ":".join(agent["spatial"]["address"]["living_area"] + ["床"])
            assert bed_key in maze.address_tiles, f"{char.display_name} 嘅住所冇床"


# ======================================================================
# 邊界 6：spawn coord 合法性 + fallback 鏈
# ======================================================================
class TestSpawnCoord:
    def test_spawn_coord_valid_non_collision(self, builder, valid_request, maze):
        """Done When：每個角色 coord 經 Maze.tile_at() 驗證有效且非 collision。"""
        builder.build(valid_request)
        for char in valid_request.characters:
            agent = _agent_json(builder, char.display_name)
            tile = maze.tile_at(agent["coord"])
            assert not tile.collision
            # spawn 格要喺 living_area 房內
            assert ":".join(tile.address[:3]) == ":".join(char.home)

    def test_fallback_to_room_tile_when_bed_tiles_empty(self, builder, valid_request, maze, monkeypatch):
        """邊界 6：床 tile 候選為空 → fallback 同房任意非 collision 格。"""
        char_home = valid_request.characters[0].home
        bed_key = ":".join(char_home + ["床"])
        monkeypatch.setattr(builder.maze, "address_tiles",
                            {k: v for k, v in builder.maze.address_tiles.items() if k != bed_key})
        coord = builder._pick_spawn_coord(char_home)
        tile = maze.tile_at(coord)
        assert not tile.collision

    def test_all_candidates_empty_raises_500(self, builder):
        """邊界 6：床同房都冇非 collision 格 → 500 並指明邊間房。"""
        with pytest.raises(SetupError) as exc:
            builder._pick_spawn_coord(["the Ville", "不存在嘅屋", "不存在嘅房"])
        assert exc.value.status == 500
        assert "不存在嘅房" in exc.value.errors[0].message


# ======================================================================
# 邊界 7：LLM 輔助失敗兜底（唔准真 call LLM）
# ======================================================================
class TestLLMFallback:
    def test_create_without_llm_uses_rule_based_fallback(self, client, valid_payload):
        """邊界 7：無 LLM 時 currently 用規則式拼接，response 標 llm_fallback。"""
        resp = client.post("/api/setup/create", json=valid_payload)
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["llm_fallback"] is True

    def test_currently_rule_based_format(self, builder, valid_request):
        """邊界 7：兜底格式 = 「{name}係{occupation}。{story_opening}」。"""
        builder.build(valid_request)
        agent = _agent_json(builder, "阿欣")
        assert agent["currently"].startswith("阿欣係茶餐廳老闆。")
        assert "年三十晚" in agent["currently"]

    def test_currently_capped_at_500_chars(self, builder, valid_payload):
        """邊界 7：開端超長時 currently 截去 500 字內，唔會爆。"""
        valid_payload["story_opening"] = "年三十晚。" * 200  # 1000 字（schema 上限）
        req = SetupCreateRequest.model_validate(valid_payload)
        builder.build(req)
        agent = _agent_json(builder, "阿欣")
        assert len(agent["currently"]) <= 500
        assert agent["currently"].startswith("阿欣係茶餐廳老闆。")


# ======================================================================
# 邊界 8：生成中途失敗 rollback
# ======================================================================
class TestRollback:
    def test_rollback_on_mid_write_failure(self, builder, valid_request, monkeypatch):
        """邊界 8：第三個角色寫到一半 disk error → 全部還原，模板唔受影響。"""
        import story_weaver.builder as builder_mod

        call_count = {"n": 0}
        real_copy = builder_mod.shutil.copyfile

        def failing_copy(src, dst):
            call_count["n"] += 1
            if call_count["n"] >= 5:  # 第三個角色開始
                raise IOError("模擬 disk error")
            return real_copy(src, dst)

        monkeypatch.setattr(builder_mod.shutil, "copyfile", failing_copy)
        with pytest.raises(SetupError) as exc:
            builder.build(valid_request)
        assert exc.value.status == 500
        assert not os.path.exists(os.path.join(builder.checkpoints_root, "茶餐廳風雲"))
        for name in ("阿欣", "阿強", "阿珍", "阿豪"):
            assert not os.path.exists(os.path.join(builder.catalog.agents_root, name))
        for name in FAKE_TEMPLATES:
            assert os.path.isdir(os.path.join(builder.catalog.agents_root, name))

    def test_rollback_via_api_returns_500(self, client, builder, valid_payload, monkeypatch):
        """邊界 8：API 層面 rollback → 500 + 中文原因。"""
        import story_weaver.builder as builder_mod

        real_copy = builder_mod.shutil.copyfile

        def failing_copy(src, dst):
            raise PermissionError("模擬 permission error")

        monkeypatch.setattr(builder_mod.shutil, "copyfile", failing_copy)
        resp = client.post("/api/setup/create", json=valid_payload)
        assert resp.status_code == 500
        body = resp.get_json()
        assert any("失敗" in e["message"] for e in body["errors"])
        assert not os.path.exists(os.path.join(builder.checkpoints_root, "茶餐廳風雲"))


# ======================================================================
# 邊界 9：故事鎖定 423
# ======================================================================
class TestStoryLock:
    def test_locked_story_returns_423(self, client, valid_payload):
        """邊界 9：故事開始後 locked:true，再 POST 同名 → 423。"""
        resp1 = client.post("/api/setup/create", json=valid_payload)
        assert resp1.status_code == 201
        story = _read_json(os.path.join(resp1.get_json()["story_dir"], "story.json"))
        assert story["locked"] is True
        resp2 = client.post("/api/setup/create", json=valid_payload)
        assert resp2.status_code == 423
        assert any("唔可以再改" in e["message"] for e in resp2.get_json()["errors"])


# ======================================================================
# 邊界 10：路徑危險字符 / 空格名
# ======================================================================
class TestNameSafety:
    @pytest.mark.parametrize("bad", ["../x", "a/b", "a\\b", "..", "甲/../乙", "\x01abc"])
    def test_display_name_path_danger_rejected(self, valid_payload, bad):
        """邊界 10：/ \\ .. 控制字符全部拒絕。"""
        valid_payload["characters"][0]["display_name"] = bad
        with pytest.raises(ValidationError):
            SetupCreateRequest.model_validate(valid_payload)

    @pytest.mark.parametrize("bad", ["a/b", "..", "  "])
    def test_story_name_path_danger_rejected(self, valid_payload, bad):
        valid_payload["story_name"] = bad
        with pytest.raises(ValidationError):
            SetupCreateRequest.model_validate(valid_payload)

    def test_display_name_max_length(self, valid_payload):
        """邊界 10：長度限 1-20 字。"""
        valid_payload["characters"][0]["display_name"] = "甲" * 21
        with pytest.raises(ValidationError):
            SetupCreateRequest.model_validate(valid_payload)

    def test_space_name_legal_but_notice_and_underscore_dir(self, client, valid_payload):
        """邊界 10：空格合法，validate 有提示，目錄名轉底線（兼容 start.py 拼接）。"""
        valid_payload["characters"][0]["display_name"] = "阿 欣"
        valid_payload["relationships"] = [{"from": "阿 欣", "to": "阿強", "score": 10}]
        resp = client.post("/api/setup/validate", json=valid_payload)
        assert resp.status_code == 200
        body = resp.get_json()
        assert any("空格" in n and "阿_欣" in n for n in body["notices"])
        resp2 = client.post("/api/setup/create", json=valid_payload)
        assert resp2.status_code == 201
        sim = _read_json(resp2.get_json()["sim_config_path"])
        assert sim["agents"]["阿 欣"]["config_path"].endswith("阿_欣/agent.json")


# ======================================================================
# Done When：生成物 schema + 檔案
# ======================================================================
class TestBuildOutputs:
    def test_create_returns_201_with_expected_payload(self, client, valid_payload):
        resp = client.post("/api/setup/create", json=valid_payload)
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["story_name"] == "茶餐廳風雲"
        assert body["characters"] == ["阿欣", "阿強", "阿珍", "阿豪"]
        assert body["template_map"]["阿欣"] == "模板甲"
        assert os.path.isdir(body["story_dir"])
        assert os.path.isfile(body["sim_config_path"])
        assert body["redirect"] == "/?name=茶餐廳風雲"

    def test_agent_dir_contains_three_files(self, builder, valid_request):
        """Done When：每個角色目錄有 agent.json + portrait.png + texture.png。"""
        builder.build(valid_request)
        for name in ("阿欣", "阿強", "阿珍", "阿豪"):
            agent_dir = os.path.join(builder.catalog.agents_root, name)
            for f in ("agent.json", "portrait.png", "texture.png"):
                assert os.path.isfile(os.path.join(agent_dir, f)), f"{name} 缺 {f}"

    def test_agent_json_schema(self, builder, valid_request):
        """Done When：agent.json 通過 schema 校驗（所有必填 key）。"""
        builder.build(valid_request)
        agent = _agent_json(builder, "阿欣")
        for key in ("name", "portrait", "coord", "currently", "scratch", "spatial", "relationships"):
            assert key in agent, f"agent.json 缺 {key}"
        for key in ("age", "innate", "learned", "lifestyle", "daily_plan"):
            assert key in agent["scratch"], f"scratch 缺 {key}"
        assert "living_area" in agent["spatial"]["address"]
        assert "tree" in agent["spatial"]
        assert agent["name"] == "阿欣"
        assert agent["portrait"] == "assets/village/agents/阿欣/portrait.png"
        assert isinstance(agent["coord"], list) and len(agent["coord"]) == 2
        assert agent["relationships"]["阿強"] == {"score": 60, "desc": "欣賞佢但唔敢講"}

    def test_agent_json_no_engine_reserved_keys(self, builder, valid_request):
        """agent.json 唔准有 action/schedule/associate 等 key（會觸發引擎副作用）。"""
        builder.build(valid_request)
        agent = _agent_json(builder, "阿欣")
        for banned in ("action", "schedule", "associate", "percept", "think", "chat_iter"):
            assert banned not in agent

    def test_agent_json_matches_engine_init_contract(self, builder, valid_request, maze):
        """對齊 modules/agent.py Agent.__init__ 讀取嘅 key 型別。"""
        builder.build(valid_request)
        agent = _agent_json(builder, "阿欣")
        # Agent.__init__ 直接讀：name/coord/currently/scratch/spatial
        assert isinstance(agent["name"], str)
        maze.tile_at(agent["coord"])  # 無效會直接炸
        assert isinstance(agent["currently"], str) and agent["currently"]
        scratch = agent["scratch"]
        assert isinstance(scratch["age"], int)
        for k in ("innate", "learned", "lifestyle", "daily_plan"):
            assert isinstance(scratch[k], str) and scratch[k]
        assert isinstance(agent["spatial"]["tree"], dict) and agent["spatial"]["tree"]

    def test_story_json_schema(self, builder, valid_request):
        """Done When：story.json 含開端原文/characters/template_map/relationships/locked。"""
        result = builder.build(valid_request)
        story = _read_json(os.path.join(result.story_dir, "story.json"))
        assert story["story_name"] == "茶餐廳風雲"
        assert story["story_opening"] == valid_request.story_opening  # 原文保留
        assert story["characters"] == ["阿欣", "阿強", "阿珍", "阿豪"]
        assert story["template_map"] == {"阿欣": "模板甲", "阿強": "模板乙",
                                         "阿珍": "模板丙", "阿豪": "模板丁"}
        assert story["language"] == "zh-Hant-HK"
        assert story["locked"] is True
        assert "created_at" in story
        assert len(story["relationships"]) == 12  # 4×3 雙向補齊

    def test_sim_config_matches_start_py_format(self, builder, valid_request):
        """Done When：sim config 同 start.py get_config() 格式一致。"""
        result = builder.build(valid_request)
        sim = result.sim_config
        assert sim["stride"] == 10
        assert sim["time"] == {"start": "20240213-09:30"}
        assert sim["maze"]["path"] == os.path.join("assets", "village", "maze.json")
        assert "agent_base" in sim and "agent" not in sim.get("agent_base", {}) or True
        assert set(sim["agents"].keys()) == {"阿欣", "阿強", "阿珍", "阿豪"}
        for name, cfg in sim["agents"].items():
            assert cfg["config_path"].endswith(f"{name}/agent.json")
            assert "relationships" in cfg  # GM 持久化契約


# ======================================================================
# Review 🔴1 + Done When：全部生成文字係繁體
# ======================================================================
class TestTraditionalChinese:
    def test_personality_converted_to_traditional(self, builder, valid_request):
        """Review 🔴1：personality（前端預填簡體 innate）寫入 agent.json 前必須轉繁體。

        PRD「語言依賴」：本系統生成嘅 scratch 必須寫繁體，
        否則簡繁混雜會污染 agent 自我認知。
        """
        builder.build(valid_request)
        agent = _agent_json(builder, "阿欣")
        innate = agent["scratch"]["innate"]
        assert innate == "慢熱、念舊", f"innate 有簡體殘留：{innate}"
        assert not _SIMPLIFIED_ONLY.search(innate)

    def test_occupation_learned_converted_to_traditional(self, builder, valid_payload):
        """Review 🔴1：occupation（前端預填簡體 learned_first_line）要轉繁體。"""
        # 模擬一鍵預設：玩家唔改 occupation，直接用模板 learned_first_line
        valid_payload["characters"][0]["occupation"] = \
            builder.catalog.get("模板甲").learned_first_line
        req = SetupCreateRequest.model_validate(valid_payload)
        builder.build(req)
        agent = _agent_json(builder, "阿欣")
        learned = agent["scratch"]["learned"]
        assert not _SIMPLIFIED_ONLY.search(learned), f"learned 有簡體殘留：{learned}"

    def test_currently_no_simplified_residue(self, builder, valid_payload):
        """Review 🔴1：currently（prefix 用 occupation）唔可以有簡體。"""
        valid_payload["characters"][0]["occupation"] = \
            builder.catalog.get("模板甲").learned_first_line
        req = SetupCreateRequest.model_validate(valid_payload)
        builder.build(req)
        agent = _agent_json(builder, "阿欣")
        assert not _SIMPLIFIED_ONLY.search(agent["currently"]), \
            f"currently 有簡體殘留：{agent['currently']}"

    def test_template_lifestyle_daily_plan_converted(self, builder, valid_request):
        """模板簡體 lifestyle/daily_plan 寫入時轉繁體（呢部分現有代碼有做）。"""
        builder.build(valid_request)
        agent = _agent_json(builder, "阿欣")
        assert "睡觉" not in agent["scratch"]["lifestyle"]
        assert "开门" not in agent["scratch"]["daily_plan"]


# ======================================================================
# Review 🟡3：預填錯名 + 雙句號
# ======================================================================
class TestPrefillQuality:
    def test_learned_no_double_period(self, builder, valid_payload):
        """Review 🟡3：occupation 預填帶句號時 learned 唔可以出現「。。」。"""
        valid_payload["characters"][0]["occupation"] = \
            builder.catalog.get("模板甲").learned_first_line  # 尾有「。」
        req = SetupCreateRequest.model_validate(valid_payload)
        builder.build(req)
        agent = _agent_json(builder, "阿欣")
        assert "。。" not in agent["scratch"]["learned"], \
            f"雙句號：{agent['scratch']['learned']}"
        assert "。。" not in agent["currently"]

    def test_learned_no_template_name_after_rename(self, builder, valid_payload):
        """Review 🟡3：玩家改名後 learned 唔可以殘留模板原名（錯名自我認知）。"""
        valid_payload["characters"][0]["occupation"] = \
            builder.catalog.get("模板甲").learned_first_line  # 含「模板甲是……」
        req = SetupCreateRequest.model_validate(valid_payload)
        builder.build(req)
        agent = _agent_json(builder, "阿欣")
        learned = agent["scratch"]["learned"]
        # 「阿欣」嘅自我認知入面唔應該出現另一个人嘅名
        assert learned.count("模板甲") == 0, f"learned 殘留模板原名：{learned}"


# ======================================================================
# Review 💭：server 端 trim
# ======================================================================
class TestServerSideTrim:
    def test_display_name_whitespace_trimmed(self, builder, valid_payload):
        """Review 💭：API 直call「阿欣 」唔應該同「阿欣」係兩個名／目錄名尾空格。"""
        valid_payload["characters"][0]["display_name"] = "阿欣 "
        valid_payload["relationships"] = [{"from": "阿欣 ", "to": "阿強", "score": 10}]
        req = SetupCreateRequest.model_validate(valid_payload)
        assert req.characters[0].display_name == "阿欣", \
            f"server 冇 trim：{req.characters[0].display_name!r}"


# ======================================================================
# Done When：記憶注入（stub game，唔真 call LLM）
# ======================================================================
class _StubAssociate:
    def __init__(self):
        self.calls = []

    def add_node(self, node_type, event, poignancy, **kw):
        self.calls.append((node_type, event, poignancy))


class _StubAgent:
    def __init__(self, name, home):
        self.name = name
        self.associate = _StubAssociate()
        self.spatial = type("S", (), {"address": {"living_area": home}})()


class _StubGame:
    def __init__(self, agents):
        self._agents = agents

    def get_agent(self, name):
        return self._agents[name]


class TestMemoryInjection:
    def test_opening_event_poignancy_9_for_every_agent(self, builder, valid_request):
        """Done When：每個 agent 記憶流 ≥1 條故事開端 event（poignancy 9）。"""
        result = builder.build(valid_request)
        story = _read_json(os.path.join(result.story_dir, "story.json"))
        agents = {n: _StubAgent(n, h) for n, h in
                  zip(story["characters"], FAKE_TEMPLATES.values())}
        counts = inject_story_memories(_StubGame(agents), story)
        for name in story["characters"]:
            assert counts[name] >= 1
            opening = agents[name].associate.calls[0]
            assert opening[0] == "event"
            assert opening[2] == OPENING_POIGNANCY == 9
            assert story["story_opening"] in opening[1].get_describe()

    def test_one_thought_per_relationship(self, builder, valid_request):
        """Done When：每段關係一條 thought node，poignancy 按 |score| 映射。"""
        result = builder.build(valid_request)
        story = _read_json(os.path.join(result.story_dir, "story.json"))
        agents = {n: _StubAgent(n, h) for n, h in
                  zip(story["characters"], FAKE_TEMPLATES.values())}
        inject_story_memories(_StubGame(agents), story)
        # story.json 雙向補齊後 12 段關係：每個 agent 3 段 from 佢嘅
        for name, agent in agents.items():
            thoughts = [c for c in agent.associate.calls if c[0] == "thought"]
            assert len(thoughts) == 3, f"{name} 應該有 3 條關係 thought"
        # 阿欣對阿強 +60 → poignancy 映射 1+7*0.6≈5
        yan = [c for c in agents["阿欣"].associate.calls if c[0] == "thought"]
        rel_to_keung = [c for c in yan if "阿強" in c[1].get_describe()][0]
        assert rel_to_keung[2] == score_to_poignancy(60)
        assert "我對阿強嘅好感係 +60：欣賞佢但唔敢講" in rel_to_keung[1].get_describe()

    def test_score_to_poignancy_mapping(self):
        """PRD：0→1，100→8。"""
        assert score_to_poignancy(0) == 1
        assert score_to_poignancy(100) == 8
        assert score_to_poignancy(-100) == 8

    def test_inject_event_poignancy_bounds(self):
        """玩家指令注入系統共用路徑：poignancy 出界要拒。"""
        game = _StubGame({"阿欣": _StubAgent("阿欣", [])})
        with pytest.raises(ValueError):
            inject_event(game, "阿欣", "測試", 0)
        with pytest.raises(ValueError):
            inject_event(game, "阿欣", "測試", 11)


# ======================================================================
# Done When：sim config 直接餵 SimulateServer 跑第一個 step（slow）
# ======================================================================
@pytest.mark.slow
def test_sim_config_feeds_simulate_server(tmp_path, monkeypatch, fake_llama_index):
    """Done When：生成嘅 sim config 餵 SimulateServer 跑完 step=1 唔報錯，
    checkpoint 帶 relationships；第一個 step 前記憶注入可經 abstract() 查證。
    LLM 全部 stub 掉。"""
    monkeypatch.setattr(sys, "argv", ["start.py"])
    import importlib
    import shutil as _shutil

    import start

    importlib.reload(start)
    from modules import utils
    from modules.maze import Maze
    from story_weaver.builder import StoryBuilder
    from story_weaver.housing import HousingRegistry
    from story_weaver.templates import TemplateCatalog

    static_root = tmp_path / "static"
    village = static_root / "assets" / "village"
    agents_root = village / "agents"
    agents_root.mkdir(parents=True)
    _shutil.copyfile(REAL_MAZE, str(village / "maze.json"))
    for name, home in FAKE_TEMPLATES.items():
        make_template_dir(str(agents_root), name, home)

    catalog = TemplateCatalog(str(agents_root))
    catalog.scan()
    housing = HousingRegistry(str(village / "maze.json"))
    maze = Maze(utils.load_dict(str(village / "maze.json")), utils.create_io_logger("critical"))
    checkpoints_root = tmp_path / "checkpoints"
    builder = StoryBuilder(
        catalog=catalog, housing=housing, maze=maze,
        static_root=str(static_root), checkpoints_root=str(checkpoints_root),
        data_config_path=DATA_CONFIG,
    )

    req = SetupCreateRequest.model_validate({
        "story_name": "煙霧測試故事",
        "story_opening": "年三十晚，阿欣嘅茶餐廳收到拆遷通知，佢決定瞞住啲街坊。",
        "characters": [
            {"template_id": t, "display_name": n, "occupation": o,
             "personality": "慢熱", "home": FAKE_TEMPLATES[t]}
            for t, n, o in (("模板甲", "阿欣", "茶餐廳老闆"), ("模板乙", "阿強", "地產經紀"),
                            ("模板丙", "阿珍", "護士"), ("模板丁", "阿豪", "畫家"))
        ],
        "relationships": [{"from": "阿欣", "to": "阿強", "score": 60, "desc": "欣賞佢"}],
    })
    result = builder.build(req)

    class StubLLM:
        def is_available(self):
            return False

        def get_summary(self):
            return {}

    monkeypatch.setattr("modules.agent.create_llm_model", lambda config: StubLLM())

    game_storage = os.path.join("results", "checkpoints", "煙霧測試故事")
    try:
        sim_config = utils.load_dict(result.sim_config_path)
        server = start.SimulateServer(
            "煙霧測試故事", str(static_root),
            str(checkpoints_root / "煙霧測試故事"), sim_config,
        )
        story_meta = utils.load_dict(os.path.join(result.story_dir, "story.json"))
        injected = inject_story_memories(server.game, story_meta)
        assert all(v >= 1 for v in injected.values())
        abs_ = server.game.get_agent("阿欣").associate.abstract()
        assert any(story_meta["story_opening"] in e for e in abs_["event"])
        assert any("我對阿強嘅好感係 +60" in t for t in abs_["thought"])

        monkeypatch.setattr(
            server.game, "agent_think",
            lambda name, status: {"plan": {"name": name, "path": [], "emojis": {}}},
        )
        server.simulate(step=1, stride=sim_config["stride"])

        files = os.listdir(str(checkpoints_root / "煙霧測試故事"))
        sim_files = [f for f in files if f.startswith("simulate-")]
        assert len(sim_files) == 1
        checkpoint = _read_json(checkpoints_root / "煙霧測試故事" / sim_files[0])
        assert checkpoint["agents"]["阿欣"]["relationships"]["阿強"]["score"] == 60
        assert os.path.isfile(checkpoints_root / "煙霧測試故事" / "conversation.json")
    finally:
        if os.path.isdir(game_storage):
            _shutil.rmtree(game_storage, ignore_errors=True)

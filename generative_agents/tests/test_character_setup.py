"""test_character_setup — 角色 Setup 系統測試.

行法：cd generative_agents && ../.venv/bin/pytest tests/test_character_setup.py
"""

import json
import os
import sys

import pytest
from pydantic import ValidationError

from story_weaver.builder import SetupError, StoryBuilder
from story_weaver.housing import HousingRegistry
from story_weaver.memory_seed import (
    OPENING_POIGNANCY,
    inject_story_memories,
    relationship_thought,
    score_to_poignancy,
)
from story_weaver.schemas import SetupCreateRequest

from .conftest import (
    DATA_CONFIG,
    FAKE_TEMPLATES,
    REAL_MAZE,
    REAL_STATIC,
    _make_template_dir,
)


# ----------------------------------------------------------------------
# Schema 校驗
# ----------------------------------------------------------------------
class TestSchema:
    def test_schema_valid_request(self, valid_request):
        assert len(valid_request.characters) == 4
        assert valid_request.relationships[0].from_name == "阿欣"

    def test_schema_too_few_characters(self, valid_payload):
        valid_payload["characters"] = valid_payload["characters"][:3]
        with pytest.raises(ValidationError):
            SetupCreateRequest.model_validate(valid_payload)

    def test_required_fields_empty(self, valid_payload):
        valid_payload["characters"][0]["occupation"] = ""
        valid_payload["characters"][1]["personality"] = ""
        valid_payload["story_opening"] = "太短"
        with pytest.raises(ValidationError) as exc:
            SetupCreateRequest.model_validate(valid_payload)
        fields = {".".join(str(p) for p in e["loc"]) for e in exc.value.errors()}
        assert any("occupation" in f for f in fields)
        assert any("personality" in f for f in fields)
        assert any("story_opening" in f for f in fields)

    def test_display_name_path_traversal(self, valid_payload):
        for bad in ("../x", "a/b", "a\\b", "..", "   "):
            valid_payload["characters"][0]["display_name"] = bad
            with pytest.raises(ValidationError):
                SetupCreateRequest.model_validate(valid_payload)
        # 空格合法
        valid_payload["characters"][0]["display_name"] = "阿 欣"
        req = SetupCreateRequest.model_validate(valid_payload)
        assert req.characters[0].display_name == "阿 欣"


# ----------------------------------------------------------------------
# 業務校驗
# ----------------------------------------------------------------------
class TestValidate:
    def test_validate_ok(self, builder, valid_request):
        assert builder.validate(valid_request) == []

    def test_display_name_duplicate_in_request(self, builder, valid_payload):
        valid_payload["characters"][1]["display_name"] = "阿欣"
        req = SetupCreateRequest.model_validate(valid_payload)
        errors = builder.validate(req)
        assert any("撞" in e.message and "characters[1].display_name" == e.field for e in errors)

    def test_display_name_collides_with_template(self, builder, valid_payload):
        valid_payload["characters"][0]["display_name"] = "模板甲"
        req = SetupCreateRequest.model_validate(valid_payload)
        errors = builder.validate(req)
        assert any(e.field == "characters[0].display_name" for e in errors)

    def test_housing_conflict_same_room(self, builder, valid_payload):
        valid_payload["characters"][1]["home"] = list(valid_payload["characters"][0]["home"])
        req = SetupCreateRequest.model_validate(valid_payload)
        errors = builder.validate(req)
        assert any(e.field == "characters[1].home" and "同一間房" in e.message for e in errors)

    def test_housing_same_house_different_room_ok(self, builder, valid_payload):
        valid_payload["characters"][0]["home"] = ["the Ville", "塔瑪拉和卡門的家", "卡門的房間"]
        valid_payload["characters"][1]["home"] = ["the Ville", "塔瑪拉和卡門的家", "塔瑪拉的房間"]
        req = SetupCreateRequest.model_validate(valid_payload)
        assert builder.validate(req) == []

    def test_home_without_bed_rejected(self, builder, valid_payload):
        valid_payload["characters"][0]["home"] = ["the Ville", "霍布斯咖啡館", "咖啡館"]
        req = SetupCreateRequest.model_validate(valid_payload)
        errors = builder.validate(req)
        assert any(e.field == "characters[0].home" for e in errors)

    def test_relationship_unknown_names(self, builder, valid_payload):
        valid_payload["relationships"].append({"from": "阿欣", "to": "路人甲", "score": 10})
        req = SetupCreateRequest.model_validate(valid_payload)
        errors = builder.validate(req)
        assert any(e.field == "relationships[1].to" for e in errors)

    def test_incomplete_assets_template_unselectable(self, builder, valid_payload):
        _make_template_dir(
            builder.catalog.agents_root, "模板殘", ["the Ville", "瑞恩的公寓", "主人房"],
            with_texture=False,
        )
        builder.catalog.scan()
        valid_payload["characters"][0]["template_id"] = "模板殘"
        valid_payload["characters"][0]["home"] = ["the Ville", "瑞恩的公寓", "主人房"]
        req = SetupCreateRequest.model_validate(valid_payload)
        template = builder.catalog.get("模板殘")
        assert template.assets_complete is False
        errors = builder.validate(req)
        assert any(e.field == "characters[0].template_id" for e in errors)


# ----------------------------------------------------------------------
# 生成
# ----------------------------------------------------------------------
class TestBuild:
    def test_build_success_outputs(self, builder, valid_request, tmp_path):
        result = builder.build(valid_request)
        # story.json
        story_json_path = os.path.join(result.story_dir, "story.json")
        assert os.path.isfile(story_json_path)
        with open(story_json_path, encoding="utf-8") as f:
            story = json.load(f)
        assert story["locked"] is True
        assert story["story_opening"] == valid_request.story_opening
        assert story["characters"] == ["阿欣", "阿強", "阿珍", "阿豪"]
        assert story["template_map"]["阿欣"] == "模板甲"
        assert story["language"] == "zh-Hant-HK"
        # sim_config.json
        assert os.path.isfile(result.sim_config_path)
        sim = result.sim_config
        assert sim["stride"] == 10
        assert sim["time"] == {"start": "20240213-09:30"}
        assert sim["maze"]["path"] == os.path.join("assets", "village", "maze.json")
        assert "agent_base" in sim
        assert set(sim["agents"].keys()) == {"阿欣", "阿強", "阿珍", "阿豪"}
        assert sim["agents"]["阿欣"]["relationships"]["阿強"]["score"] == 60
        # 角色目錄
        for name in story["characters"]:
            agent_dir = os.path.join(builder.catalog.agents_root, name)
            assert os.path.isfile(os.path.join(agent_dir, "agent.json"))
            assert os.path.isfile(os.path.join(agent_dir, "portrait.png"))
            assert os.path.isfile(os.path.join(agent_dir, "texture.png"))

    def test_agent_json_schema(self, builder, valid_request):
        result = builder.build(valid_request)
        with open(os.path.join(builder.catalog.agents_root, "阿欣", "agent.json"), encoding="utf-8") as f:
            agent = json.load(f)
        for key in ("name", "portrait", "coord", "currently", "scratch", "spatial", "relationships"):
            assert key in agent, f"agent.json 缺 {key}"
        for key in ("age", "innate", "learned", "lifestyle", "daily_plan"):
            assert key in agent["scratch"]
        assert "living_area" in agent["spatial"]["address"]
        assert "tree" in agent["spatial"]
        assert agent["scratch"]["innate"] == "慢熱、念舊"
        assert agent["scratch"]["learned"] == "阿欣係茶餐廳老闆。"
        assert agent["currently"].startswith("阿欣係茶餐廳老闆。")
        assert len(agent["currently"]) <= 500
        assert agent["relationships"]["阿強"] == {"score": 60, "desc": "欣賞佢但唔敢講"}

    def test_agent_json_no_engine_keys(self, builder, valid_request):
        builder.build(valid_request)
        with open(os.path.join(builder.catalog.agents_root, "阿欣", "agent.json"), encoding="utf-8") as f:
            agent = json.load(f)
        # 「action」會觸發 Agent.__init__ 重抽 coord；其餘會覆蓋 agent_base
        for banned in ("action", "schedule", "associate", "percept", "think", "chat_iter"):
            assert banned not in agent

    def test_spawn_coord_non_collision(self, builder, valid_request, maze):
        builder.build(valid_request)
        for name, char in zip(("阿欣", "阿強", "阿珍", "阿豪"), valid_request.characters):
            with open(os.path.join(builder.catalog.agents_root, name, "agent.json"), encoding="utf-8") as f:
                agent = json.load(f)
            tile = maze.tile_at(agent["coord"])
            assert not tile.collision
            # spawn 格地址要喺 living_area 房間範圍內
            room_key = ":".join(char.home)
            assert ":".join(tile.address[:3]) == room_key

    def test_score_clamped(self, builder, valid_payload):
        valid_payload["relationships"][0]["score"] = 150
        req = SetupCreateRequest.model_validate(valid_payload)
        result = builder.build(req)
        assert len(result.clamped) == 1
        assert result.clamped[0].original == 150
        assert result.clamped[0].clamped == 100
        assert result.sim_config["agents"]["阿欣"]["relationships"]["阿強"]["score"] == 100

    def test_missing_reverse_relationship_filled(self, builder, valid_request):
        result = builder.build(valid_request)
        # 4 角色 12 對，設定咗 1 對，補 11 對
        assert result.filled_relationships == 11
        rel = result.sim_config["agents"]["阿強"]["relationships"]["阿欣"]
        assert rel == {"score": 0, "desc": ""}

    def test_story_name_conflict(self, builder, valid_request):
        os.makedirs(os.path.join(builder.checkpoints_root, "茶餐廳風雲"))
        with pytest.raises(SetupError) as exc:
            builder.build(valid_request)
        assert exc.value.status == 409
        assert exc.value.suggestion == "茶餐廳風雲-2"

    def test_locked_story_returns_423(self, builder, valid_request):
        builder.build(valid_request)
        with pytest.raises(SetupError) as exc:
            builder.build(valid_request)
        assert exc.value.status == 423

    def test_rollback_on_mid_write_failure(self, builder, valid_request, monkeypatch):
        import story_weaver.builder as builder_mod

        real_makedirs = os.makedirs
        made_agent_dirs = []

        def tracking_makedirs(path, *a, **kw):
            made_agent_dirs.append(path)
            return real_makedirs(path, *a, **kw)

        call_count = {"n": 0}
        real_copy = builder_mod.shutil.copyfile

        def failing_copy(src, dst):
            call_count["n"] += 1
            # 第三個角色（第 5-6 次 copy）開始失敗
            if call_count["n"] >= 5:
                raise IOError("模擬 disk error")
            return real_copy(src, dst)

        monkeypatch.setattr(builder_mod.shutil, "copyfile", failing_copy)
        with pytest.raises(SetupError) as exc:
            builder.build(valid_request)
        assert exc.value.status == 500
        # story 目錄同已建角色目錄全部還原
        assert not os.path.exists(os.path.join(builder.checkpoints_root, "茶餐廳風雲"))
        for name in ("阿欣", "阿強", "阿珍", "阿豪"):
            assert not os.path.exists(os.path.join(builder.catalog.agents_root, name))
        # 模板目錄唔受影響
        for name in FAKE_TEMPLATES:
            assert os.path.isdir(os.path.join(builder.catalog.agents_root, name))

    def test_generated_text_is_traditional(self, builder, valid_request):
        builder.build(valid_request)
        with open(os.path.join(builder.catalog.agents_root, "阿欣", "agent.json"), encoding="utf-8") as f:
            agent = json.load(f)
        # 模板原文係簡體（睡觉/咖啡館/门），生成後必須轉繁體
        assert "睡覺" in agent["scratch"]["lifestyle"]
        assert "睡觉" not in agent["scratch"]["lifestyle"]
        assert "開門" in agent["scratch"]["daily_plan"] or "开门" not in agent["scratch"]["daily_plan"]

    def test_space_name_underscore_dir(self, builder, valid_payload):
        valid_payload["characters"][0]["display_name"] = "阿 欣"
        valid_payload["relationships"] = [{"from": "阿 欣", "to": "阿強", "score": 10, "desc": ""}]
        req = SetupCreateRequest.model_validate(valid_payload)
        result = builder.build(req)
        assert os.path.isdir(os.path.join(builder.catalog.agents_root, "阿_欣"))
        assert result.sim_config["agents"]["阿 欣"]["config_path"].endswith("阿_欣/agent.json")
        assert "空格" in builder.name_notices(req)[0]


# ----------------------------------------------------------------------
# 記憶注入
# ----------------------------------------------------------------------
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


class TestMemorySeed:
    def test_score_to_poignancy(self):
        assert score_to_poignancy(0) == 1
        assert score_to_poignancy(100) == 8
        assert score_to_poignancy(-100) == 8
        assert score_to_poignancy(-60) == 5

    def test_relationship_thought_describe(self):
        e = relationship_thought("阿欣", "阿強", 60, "欣賞佢但唔敢講")
        assert "我對阿強嘅好感係 +60：欣賞佢但唔敢講" in e.get_describe()
        e2 = relationship_thought("阿強", "阿欣", 0, "")
        assert "我對阿欣嘅好感係 +0：陌生" in e2.get_describe()

    def test_inject_story_memories(self):
        homes = {n: h for n, h in zip(("阿欣", "阿強", "阿珍", "阿豪"), FAKE_TEMPLATES.values())}
        agents = {n: _StubAgent(n, h) for n, h in homes.items()}
        game = _StubGame(agents)
        story = {
            "characters": list(agents.keys()),
            "story_opening": "年三十晚，茶餐廳收到拆遷通知。",
            "relationships": [
                {"from": "阿欣", "to": "阿強", "score": 60, "desc": "欣賞佢但唔敢講"},
                {"from": "阿強", "to": "阿欣", "score": -20, "desc": "仲有啲嬲"},
            ],
        }
        counts = inject_story_memories(game, story)
        # 阿欣：1 開端 + 1 關係；阿強：1+1；其餘：1 開端
        assert counts == {"阿欣": 2, "阿強": 2, "阿珍": 1, "阿豪": 1}
        opening_call = agents["阿欣"].associate.calls[0]
        assert opening_call[0] == "event"
        assert opening_call[2] == OPENING_POIGNANCY
        assert "年三十晚，茶餐廳收到拆遷通知。" in opening_call[1].get_describe()
        rel_call = agents["阿欣"].associate.calls[1]
        assert rel_call[0] == "thought"
        assert rel_call[2] == score_to_poignancy(60)
        neg_call = agents["阿強"].associate.calls[1]
        assert neg_call[2] == score_to_poignancy(-20)

    def test_inject_event_validates_poignancy(self):
        from story_weaver.memory_seed import inject_event

        game = _StubGame({"阿欣": _StubAgent("阿欣", [])})
        with pytest.raises(ValueError):
            inject_event(game, "阿欣", "測試", 99)


# ----------------------------------------------------------------------
# Housing Registry
# ----------------------------------------------------------------------
class TestHousing:
    def test_registry_scans_real_maze(self, housing):
        rooms = housing.rooms()
        assert len(rooms) == 23
        labels = {r.label for r in rooms}
        assert "莫雷諾家族的房子 · 空卧室" in labels

    def test_assign_conflict_lists_available(self, housing):
        room = housing.available()[0]
        housing.assign(room.address, "阿欣")
        from story_weaver.housing import HousingConflict

        with pytest.raises(HousingConflict) as exc:
            housing.assign(room.address, "阿強")
        assert exc.value.available
        housing.release_all()
        assert len(housing.available()) == 23


# ----------------------------------------------------------------------
# start.py checkpoint 過濾修正（story.json / sim_config.json 唔會被誤載）
# ----------------------------------------------------------------------
class TestResumeFilter:
    def test_get_config_from_log_ignores_story_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["start.py"])
        import importlib

        import start

        importlib.reload(start)
        folder = tmp_path / "checkpoints" / "demo"
        folder.mkdir(parents=True)
        # 干擾檔：字母序排最尾
        (folder / "story.json").write_text(json.dumps({"story_name": "demo"}), encoding="utf-8")
        (folder / "sim_config.json").write_text(json.dumps({}), encoding="utf-8")
        (folder / "conversation.json").write_text("{}", encoding="utf-8")
        sim_checkpoint = {
            "time": "20240213-09:40",
            "stride": 10,
            "agents": {"阿欣": {"coord": [72, 14]}},
        }
        (folder / "simulate-20240213-0940.json").write_text(
            json.dumps(sim_checkpoint), encoding="utf-8"
        )
        config = start.get_config_from_log(str(folder))
        assert config is not None
        assert config["agents"]["阿欣"]["config_path"].endswith("阿欣/agent.json")
        # 空目錄（只有 story.json）→ None
        folder2 = tmp_path / "checkpoints" / "demo2"
        folder2.mkdir()
        (folder2 / "story.json").write_text("{}", encoding="utf-8")
        assert start.get_config_from_log(str(folder2)) is None


# ----------------------------------------------------------------------
# 端到端：sim config 直接餵 SimulateServer 跑第一個 step
# ----------------------------------------------------------------------
@pytest.mark.slow
def test_sim_config_feeds_simulate_server(tmp_path, monkeypatch, fake_llama_index):
    # start.py 喺 import 時 parse_args，要先清 argv
    monkeypatch.setattr(sys, "argv", ["start.py"])
    import importlib

    import start

    importlib.reload(start)
    from modules import utils
    from modules.maze import Maze
    from story_weaver.builder import StoryBuilder
    from story_weaver.housing import HousingRegistry
    from story_weaver.memory_seed import inject_story_memories
    from story_weaver.templates import TemplateCatalog

    # 砌獨立 static_root：maze.json 副本 + 4 個假模板
    static_root = tmp_path / "static"
    village = static_root / "assets" / "village"
    agents_root = village / "agents"
    agents_root.mkdir(parents=True)
    import shutil as _shutil

    _shutil.copyfile(REAL_MAZE, str(village / "maze.json"))
    for name, home in FAKE_TEMPLATES.items():
        _make_template_dir(str(agents_root), name, home)

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
            {"template_id": "模板甲", "display_name": "阿欣", "occupation": "茶餐廳老闆",
             "personality": "慢熱", "home": FAKE_TEMPLATES["模板甲"]},
            {"template_id": "模板乙", "display_name": "阿強", "occupation": "地產經紀",
             "personality": "圓滑", "home": FAKE_TEMPLATES["模板乙"]},
            {"template_id": "模板丙", "display_name": "阿珍", "occupation": "護士",
             "personality": "細心", "home": FAKE_TEMPLATES["模板丙"]},
            {"template_id": "模板丁", "display_name": "阿豪", "occupation": "畫家",
             "personality": "孤僻", "home": FAKE_TEMPLATES["模板丁"]},
        ],
        "relationships": [{"from": "阿欣", "to": "阿強", "score": 60, "desc": "欣賞佢"}],
    })
    result = builder.build(req)

    # LLM stub：is_available False → completion 會行 failsafe 分支（如有）；
    # 但我哋連 think 都 stub 埋，避免真 LLM 調用
    class StubLLM:
        def is_available(self):
            return False

        def get_summary(self):
            return {}

    monkeypatch.setattr("modules.agent.create_llm_model", lambda config: StubLLM())

    story_name = "煙霧測試故事"
    game_storage = os.path.join("results", "checkpoints", story_name)
    try:
        sim_config = utils.load_dict(result.sim_config_path)
        server = start.SimulateServer(
            story_name, str(static_root), str(checkpoints_root / story_name), sim_config,
        )
        story_meta = utils.load_dict(os.path.join(result.story_dir, "story.json"))

        # 記憶注入：init 後、simulate 前
        injected = inject_story_memories(server.game, story_meta)
        assert all(v >= 1 for v in injected.values())
        # 每個 agent 有開端 event（P.9）+ 關係 thought
        abs_ = server.game.get_agent("阿欣").associate.abstract()
        assert any(story_meta["story_opening"] in e for e in abs_["event"])
        assert any("我對阿強嘅好感係 +60" in t for t in abs_["thought"])

        # stub think（避免真 LLM），其餘行真實 simulate 流程
        def fake_agent_think(name, status):
            return {"plan": {"name": name, "path": [], "emojis": {}}}

        monkeypatch.setattr(server.game, "agent_think", fake_agent_think)
        server.simulate(step=1, stride=sim_config["stride"])

        # checkpoint 落盤，且帶住 relationships（GM 契約）
        files = os.listdir(str(checkpoints_root / story_name))
        sim_files = [f for f in files if f.startswith("simulate-")]
        assert len(sim_files) == 1
        with open(checkpoints_root / story_name / sim_files[0], encoding="utf-8") as f:
            checkpoint = json.load(f)
        assert checkpoint["agents"]["阿欣"]["relationships"]["阿強"]["score"] == 60
        # conversation.json 都有寫
        assert os.path.isfile(checkpoints_root / story_name / "conversation.json")
    finally:
        # Game.__init__ 會喺 cwd 建立 results/checkpoints/<name>/storage，清走佢
        if os.path.isdir(game_storage):
            _shutil.rmtree(game_storage, ignore_errors=True)

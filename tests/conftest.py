"""QA conftest — 角色 Setup 系統測試 fixtures（repo-root tests/）。

自給自足：唔依賴 generative_agents/tests/，直接將 generative_agents/
加入 sys.path 後用真實 maze.json + 假模板目錄做測試。

行法：/Users/kenneth/Projects/story-weaver/.venv/bin/python -m pytest tests/
"""

import json
import os
import sys
from pathlib import Path

import pytest

GEN_DIR = str(Path(__file__).resolve().parents[1] / "generative_agents")
if GEN_DIR not in sys.path:
    sys.path.insert(0, GEN_DIR)

REAL_STATIC = os.path.join(GEN_DIR, "frontend", "static")
REAL_MAZE = os.path.join(REAL_STATIC, "assets", "village", "maze.json")
REAL_AGENTS = os.path.join(REAL_STATIC, "assets", "village", "agents")
REAL_TEMPLATES_DIR = os.path.join(GEN_DIR, "frontend", "templates")
DATA_CONFIG = os.path.join(GEN_DIR, "data", "config.json")

# 1x1 透明 PNG
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)

# 四個測試模板：唔同住所，避免衝突；scratch 用簡體原文（模擬真實模板）
FAKE_TEMPLATES = {
    "模板甲": ["the Ville", "伊莎貝拉的公寓", "主人房"],
    "模板乙": ["the Ville", "莫雷諾家族的房子", "空卧室"],
    "模板丙": ["the Ville", "奧克山學院宿舍", "克勞斯的房間"],
    "模板丁": ["the Ville", "藝術家共居空間", "海莉的房間"],
}


def make_template_dir(root: str, name: str, living_area: list, with_texture: bool = True):
    agent_dir = os.path.join(root, name)
    os.makedirs(agent_dir, exist_ok=True)
    agent_json = {
        "name": name,
        "portrait": f"assets/village/agents/{name}/portrait.png",
        "coord": [72, 14],
        "currently": f"{name}而家得閒。",
        "scratch": {
            "age": 30,
            "innate": "慢热、念旧",
            "learned": f"{name}是咖啡館的老板，她总是想办法让客人放松。",
            "lifestyle": f"{name}晚上11点左右上床睡觉，早上6点左右醒来。",
            "daily_plan": f"{name}每天早上8点开门。",
        },
        "spatial": {
            "address": {"living_area": living_area},
            "tree": {"the Ville": {living_area[1]: {living_area[2]: ["床"]}}},
        },
    }
    with open(os.path.join(agent_dir, "agent.json"), "w", encoding="utf-8") as f:
        json.dump(agent_json, f, ensure_ascii=False)
    for asset in (["portrait.png", "texture.png"] if with_texture else ["portrait.png"]):
        with open(os.path.join(agent_dir, asset), "wb") as f:
            f.write(TINY_PNG)


@pytest.fixture()
def agents_root(tmp_path):
    root = tmp_path / "agents"
    root.mkdir()
    for name, home in FAKE_TEMPLATES.items():
        make_template_dir(str(root), name, home)
    return str(root)


@pytest.fixture()
def catalog(agents_root):
    from story_weaver.templates import TemplateCatalog

    c = TemplateCatalog(agents_root)
    c.scan()
    return c


@pytest.fixture()
def housing():
    from story_weaver.housing import HousingRegistry

    return HousingRegistry(REAL_MAZE)


@pytest.fixture()
def maze():
    from modules import utils
    from modules.maze import Maze

    return Maze(utils.load_dict(REAL_MAZE), utils.create_io_logger("critical"))


@pytest.fixture()
def builder(catalog, housing, maze, tmp_path):
    from story_weaver.builder import StoryBuilder

    return StoryBuilder(
        catalog=catalog,
        housing=housing,
        maze=maze,
        static_root=REAL_STATIC,
        checkpoints_root=str(tmp_path / "checkpoints"),
        data_config_path=DATA_CONFIG,
    )


@pytest.fixture()
def valid_payload():
    """合法 payload；personality 用簡體，模擬前端一鍵預填模板 innate 嘅預設路徑。"""
    return {
        "story_name": "茶餐廳風雲",
        "story_opening": "年三十晚，阿欣嘅茶餐廳收到拆遷通知，佢決定瞞住啲街坊。",
        "characters": [
            {
                "template_id": "模板甲",
                "display_name": "阿欣",
                "occupation": "茶餐廳老闆",
                "personality": "慢热、念旧",  # ← 前端預填模板 innate（簡體）
                "home": ["the Ville", "伊莎貝拉的公寓", "主人房"],
            },
            {
                "template_id": "模板乙",
                "display_name": "阿強",
                "occupation": "地產經紀",
                "personality": "口甜舌滑",
                "home": ["the Ville", "莫雷諾家族的房子", "空卧室"],
            },
            {
                "template_id": "模板丙",
                "display_name": "阿珍",
                "occupation": "護士",
                "personality": "細心、八卦",
                "home": ["the Ville", "奧克山學院宿舍", "克勞斯的房間"],
            },
            {
                "template_id": "模板丁",
                "display_name": "阿豪",
                "occupation": "畫家",
                "personality": "孤僻",
                "home": ["the Ville", "藝術家共居空間", "海莉的房間"],
            },
        ],
        "relationships": [
            {"from": "阿欣", "to": "阿強", "score": 60, "desc": "欣賞佢但唔敢講"},
        ],
    }


@pytest.fixture()
def valid_request(valid_payload):
    from story_weaver.schemas import SetupCreateRequest

    return SetupCreateRequest.model_validate(valid_payload)


@pytest.fixture()
def client(builder):
    """Flask test client：真實 templates/static，builder 注入測試用 tmp 目錄。"""
    from flask import Flask

    from story_weaver import routes

    app = Flask(
        "qa",
        template_folder=REAL_TEMPLATES_DIR,
        static_folder=REAL_STATIC,
    )
    app.register_blueprint(routes.setup_bp)
    routes.configure(
        catalog=builder.catalog,
        housing=builder.housing,
        maze=builder.maze,
        static_root=REAL_STATIC,
        checkpoints_root=builder.checkpoints_root,
        data_config_path=DATA_CONFIG,
    )
    return app.test_client()


@pytest.fixture()
def real_catalog():
    """真實 25 個模板（唯讀，唔會寫檔）。"""
    from story_weaver.templates import TemplateCatalog

    c = TemplateCatalog(REAL_AGENTS)
    c.scan()
    return c


@pytest.fixture()
def fake_llama_index(monkeypatch):
    """將 Associate 入面嘅 LlamaIndex 換做純內存 stub（唔需要 embedding 服務）。"""
    import modules.memory.associate as associate_mod

    class FakeNode:
        def __init__(self, id_, text, metadata):
            self.id_ = id_
            self.text = text
            self.metadata = metadata or {}

    class FakeIndex:
        def __init__(self, embedding, path=None):
            self._nodes = {}
            self._counter = 0

        def add_node(self, text, metadata=None, exclude_llm_keys=None,
                     exclude_embedding_keys=None, id=None):
            id = id or f"node_{self._counter}"
            self._counter += 1
            node = FakeNode(id, text, metadata)
            self._nodes[id] = node
            return node

        def cleanup(self):
            return []

        def remove_nodes(self, node_ids, delete_from_docstore=True):
            for nid in node_ids:
                self._nodes.pop(nid, None)

        def find_node(self, node_id):
            return self._nodes[node_id]

        @property
        def nodes_num(self):
            return len(self._nodes)

        def save(self):
            pass

    monkeypatch.setattr(associate_mod, "LlamaIndex", FakeIndex)
    return FakeIndex

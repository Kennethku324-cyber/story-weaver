"""story_weaver.templates — 角色模板目錄（Template Catalog）.

啟動時掃描 `frontend/static/assets/village/agents/*/agent.json`，
緩存每個模板嘅基本資料俾畫廊顯示同生成用。模板目錄係靜態資產，
運行期唔會變，掃一次就夠。
"""

import json
import logging
import os
from dataclasses import dataclass, field

from .textnorm import to_traditional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CharacterTemplate:
    template_id: str  # 目錄名，例如「伊莎贝拉」
    name: str  # agent.json["name"]
    age: int
    innate: str  # 繁體（scan 時已轉），表單預填用
    learned: str
    learned_first_line: str  # learned 第一句（繁體、剝離模板名同尾句號），表單預填
    lifestyle: str
    daily_plan: str
    living_area: list[str]  # [world, sector, arena]
    spatial_tree: dict = field(default_factory=dict)  # 完整 spatial.tree
    portrait_path: str = ""  # 相對 static_root
    texture_path: str = ""
    assets_complete: bool = True  # portrait.png + texture.png 都存在先係 True


def _first_line(text: str, name: str = "") -> str:
    """攞 learned 第一個分句做職業預填：喺最早出現嘅句讀處截斷，
    唔保留尾句號（builder prefix 會自己加），並剝離句首嘅模板名同
    「是/係」，避免玩家改名後自我認知出現另一个人嘅名。"""
    cut = len(text)
    for sep in ("。", "，", "；", "；", ".", ",", ";"):
        idx = text.find(sep)
        if 0 <= idx < cut:
            cut = idx
    head = text[:cut].strip().rstrip("。.")
    if name and head.startswith(name):
        head = head[len(name):].lstrip("是係").strip()
    return head or text.strip().rstrip("。.")


class TemplateCatalog:
    def __init__(self, agents_root: str) -> None:
        self.agents_root = agents_root
        self._templates: dict[str, CharacterTemplate] = {}

    def scan(self) -> None:
        self._templates = {}
        if not os.path.isdir(self.agents_root):
            logger.warning("模板目錄唔存在：%s", self.agents_root)
            return
        for entry in sorted(os.listdir(self.agents_root)):
            agent_dir = os.path.join(self.agents_root, entry)
            agent_json_path = os.path.join(agent_dir, "agent.json")
            if not os.path.isdir(agent_dir) or not os.path.isfile(agent_json_path):
                continue
            try:
                with open(agent_json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                scratch = data.get("scratch", {})
                spatial = data.get("spatial", {})
                portrait = os.path.join(agent_dir, "portrait.png")
                texture = os.path.join(agent_dir, "texture.png")
                complete = os.path.isfile(portrait) and os.path.isfile(texture)
                if not complete:
                    logger.warning("模板「%s」素材不完整（portrait/texture 缺失），唔俾揀", entry)
                learned = scratch.get("learned", "")
                name = data.get("name", entry)
                self._templates[entry] = CharacterTemplate(
                    template_id=entry,
                    name=name,
                    age=scratch.get("age", 30),
                    innate=to_traditional(scratch.get("innate", "")),
                    learned=learned,
                    learned_first_line=to_traditional(_first_line(learned, name)),
                    lifestyle=scratch.get("lifestyle", ""),
                    daily_plan=scratch.get("daily_plan", ""),
                    living_area=list(spatial.get("address", {}).get("living_area", [])),
                    spatial_tree=spatial.get("tree", {}),
                    portrait_path=f"assets/village/agents/{entry}/portrait.png",
                    texture_path=f"assets/village/agents/{entry}/texture.png",
                    assets_complete=complete,
                )
            except Exception as e:
                logger.warning("掃描模板「%s」失敗：%s", entry, e)

    def list(self) -> list[CharacterTemplate]:
        return list(self._templates.values())

    def get(self, template_id: str) -> CharacterTemplate:
        if template_id not in self._templates:
            raise KeyError(template_id)
        return self._templates[template_id]

    def existing_dir_names(self) -> set[str]:
        """agents_root 入面現有嘅全部角色目錄名（含模板同已生成角色）。"""
        if not os.path.isdir(self.agents_root):
            return set()
        return {
            e
            for e in os.listdir(self.agents_root)
            if os.path.isdir(os.path.join(self.agents_root, e))
        }

"""story_weaver.builder — StoryBuilder：校驗 → 生成 → rollback.

生成順序（PRD）：先寫 story 目錄（story.json + sim_config.json），
再逐角色寫 agent 目錄。任何一步失敗 rollback 今次新建嘅全部路徑。
"""

import copy
import json
import logging
import os
import random
import shutil
import threading
from dataclasses import dataclass, field

from .housing import HousingConflict, HousingRegistry
from .schemas import (
    CharacterIn,
    ClampNotice,
    FieldError,
    SetupCreateRequest,
)
from .templates import CharacterTemplate, TemplateCatalog
from .textnorm import to_traditional

logger = logging.getLogger(__name__)

CURRENTLY_MAX_LEN = 500
DEFAULT_TIME_START = "20240213-09:30"
DEFAULT_STRIDE = 10
# 每 agent 每 step 嘅粗略 LLM 調用估算（plan + schedule + percept 相關）
ESTIMATED_LLM_CALLS_PER_AGENT = 7


class SetupError(Exception):
    """業務校驗/生成失敗。routes 層按 status 映射 HTTP code。"""

    def __init__(self, status: int, errors: list[FieldError], suggestion: str = "") -> None:
        super().__init__("; ".join(e.message for e in errors))
        self.status = status
        self.errors = errors
        self.suggestion = suggestion


@dataclass
class BuildResult:
    story_name: str
    story_dir: str
    sim_config_path: str
    sim_config: dict
    agent_dirs: list[str]
    template_map: dict[str, str]
    clamped: list[ClampNotice] = field(default_factory=list)
    filled_relationships: int = 0
    llm_fallback: bool = True


class StoryBuilder:
    def __init__(
        self,
        catalog: TemplateCatalog,
        housing: HousingRegistry,
        maze,
        static_root: str,
        checkpoints_root: str,
        data_config_path: str = "data/config.json",
    ) -> None:
        self.catalog = catalog
        self.housing = housing
        self.maze = maze
        self.static_root = static_root
        self.checkpoints_root = checkpoints_root
        self.data_config_path = data_config_path
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 校驗
    # ------------------------------------------------------------------
    def validate(self, req: SetupCreateRequest) -> list[FieldError]:
        """純校驗，唔寫檔。返空 list = 合法。全部錯一次過收集。"""
        errors: list[FieldError] = []

        display_names = [c.display_name for c in req.characters]

        # 1) request 內撞名
        seen: dict[str, int] = {}
        for idx, name in enumerate(display_names):
            if name in seen:
                errors.append(FieldError(
                    field=f"characters[{idx}].display_name",
                    message=f"角色名「{name}」同第 {seen[name] + 1} 個角色撞咗，唔該改過",
                ))
            else:
                seen[name] = idx

        # 2) 同現有角色目錄撞名（含 25 個模板同已生成角色；空格會轉底線）
        existing_dirs = self.catalog.existing_dir_names()
        for idx, name in enumerate(display_names):
            if name.replace(" ", "_") in existing_dirs:
                errors.append(FieldError(
                    field=f"characters[{idx}].display_name",
                    message=f"角色名「{name}」同現有角色撞咗，唔該改過",
                ))

        # 3) 逐角色：模板存在、素材完整、住所合法
        for idx, char in enumerate(req.characters):
            prefix = f"characters[{idx}]"
            try:
                template = self.catalog.get(char.template_id)
                if not template.assets_complete:
                    errors.append(FieldError(
                        field=f"{prefix}.template_id",
                        message=f"模板「{char.template_id}」素材不完整（portrait/texture 缺失），唔揀得",
                    ))
            except KeyError:
                errors.append(FieldError(
                    field=f"{prefix}.template_id",
                    message=f"搵唔到模板「{char.template_id}」",
                ))
            if not self.housing.is_valid_home(char.home):
                errors.append(FieldError(
                    field=f"{prefix}.home",
                    message=f"住所「{' · '.join(char.home[1:])}」唔存在或者冇床，唔住得人",
                ))

        # 4) 住所衝突（dry-run，唔 mutate registry）
        taken: dict[tuple, str] = {}
        for idx, char in enumerate(req.characters):
            key = tuple(char.home)
            if key in taken:
                errors.append(FieldError(
                    field=f"characters[{idx}].home",
                    message=(
                        f"「{' · '.join(char.home[1:])}」已經俾「{taken[key]}」住咗，"
                        "兩個角色唔可以住同一間房"
                    ),
                ))
            else:
                taken[key] = char.display_name

        # 5) 關係：from/to 必須係已揀角色，from ≠ to
        name_set = set(display_names)
        for ridx, rel in enumerate(req.relationships):
            if rel.from_name not in name_set:
                errors.append(FieldError(
                    field=f"relationships[{ridx}].from",
                    message=f"關係嘅「{rel.from_name}」唔係已揀角色",
                ))
            if rel.to not in name_set:
                errors.append(FieldError(
                    field=f"relationships[{ridx}].to",
                    message=f"關係嘅「{rel.to}」唔係已揀角色",
                ))
            if rel.from_name == rel.to:
                errors.append(FieldError(
                    field=f"relationships[{ridx}].to",
                    message="唔可以設定角色對自己嘅關係",
                ))

        return errors

    def name_notices(self, req: SetupCreateRequest) -> list[str]:
        """非阻塞提示：例如角色名含空格會存做底線目錄名。"""
        notices = []
        for char in req.characters:
            if " " in char.display_name:
                notices.append(
                    f"角色名「{char.display_name}」含空格，檔案目錄會存做"
                    f"「{char.display_name.replace(' ', '_')}」"
                )
        if " " in req.story_name:
            notices.append(f"故事名「{req.story_name}」含空格，會原樣存做目錄名")
        return notices

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------
    def build(self, req: SetupCreateRequest) -> BuildResult:
        with self._lock:
            return self._build_locked(req)

    def _build_locked(self, req: SetupCreateRequest) -> BuildResult:
        story_dir = os.path.join(self.checkpoints_root, req.story_name)

        # 故事名撞名 / 鎖定檢查
        if os.path.isdir(story_dir):
            story_json_path = os.path.join(story_dir, "story.json")
            if os.path.isfile(story_json_path):
                try:
                    with open(story_json_path, "r", encoding="utf-8") as f:
                        locked = json.load(f).get("locked", False)
                except Exception:
                    locked = False
                if locked:
                    raise SetupError(423, [FieldError(
                        field="story_name",
                        message=f"故事「{req.story_name}」已經開始推演，唔可以再改設定",
                    )])
            suggestion = self._suggest_story_name(req.story_name)
            raise SetupError(409, [FieldError(
                field="story_name",
                message=f"故事名「{req.story_name}」已經用咗，建議改用「{suggestion}」",
            )], suggestion=suggestion)

        errors = self.validate(req)
        if errors:
            raise SetupError(422, errors)

        # 住所登記（今次 build 重新計）
        self.housing.release_all()
        for char in req.characters:
            try:
                self.housing.assign(char.home, char.display_name)
            except (ValueError, HousingConflict) as e:
                raise SetupError(422, [FieldError(field="characters", message=str(e))])

        rel_map, clamped, filled = self._normalize_relationships(req)

        created: list[str] = []
        agent_dirs: list[str] = []
        template_map: dict[str, str] = {}
        try:
            # 1) story 目錄 + story.json + sim_config.json
            os.makedirs(story_dir)
            created.append(story_dir)
            story_meta = self._render_story_json(req, rel_map)
            self._write_json(os.path.join(story_dir, "story.json"), story_meta)
            sim_config = self._build_sim_config(req, rel_map)
            sim_config_path = os.path.join(story_dir, "sim_config.json")
            self._write_json(sim_config_path, sim_config)

            # 2) 逐角色寫 agent 目錄
            for char in req.characters:
                template = self.catalog.get(char.template_id)
                dir_name = char.display_name.replace(" ", "_")
                agent_dir = os.path.join(self.catalog.agents_root, dir_name)
                os.makedirs(agent_dir)
                created.append(agent_dir)
                agent_dirs.append(agent_dir)
                template_map[char.display_name] = char.template_id

                template_dir = os.path.join(self.catalog.agents_root, char.template_id)
                for asset in ("portrait.png", "texture.png"):
                    shutil.copyfile(
                        os.path.join(template_dir, asset),
                        os.path.join(agent_dir, asset),
                    )
                agent_json = self._render_agent_json(char, template, rel_map, req.story_opening)
                self._write_json(os.path.join(agent_dir, "agent.json"), agent_json)

            return BuildResult(
                story_name=req.story_name,
                story_dir=story_dir,
                sim_config_path=sim_config_path,
                sim_config=sim_config,
                agent_dirs=agent_dirs,
                template_map=template_map,
                clamped=clamped,
                filled_relationships=filled,
                llm_fallback=True,
            )
        except SetupError:
            self._rollback(created)
            raise
        except Exception as e:
            self._rollback(created)
            logger.exception("生成故事「%s」失敗", req.story_name)
            raise SetupError(500, [FieldError(
                field="_",
                message=f"生成故事中途失敗：{e}。已經還原晒，唔該再試一次",
            )])

    # ------------------------------------------------------------------
    # 內部步驟
    # ------------------------------------------------------------------
    def _normalize_relationships(
        self, req: SetupCreateRequest
    ) -> tuple[dict[str, dict[str, dict]], list[ClampNotice], int]:
        """返 {from_name: {to: {score, desc}}}（雙向補齊）+ clamp 記錄 + 補咗幾多對。"""
        rel_map: dict[str, dict[str, dict]] = {}
        clamped: list[ClampNotice] = []
        for ridx, rel in enumerate(req.relationships):
            score = rel.score
            if score < -100 or score > 100:
                clamped_score = max(-100, min(100, score))
                clamped.append(ClampNotice(
                    field=f"relationships[{ridx}].score",
                    original=score,
                    clamped=clamped_score,
                ))
                score = clamped_score
            rel_map.setdefault(rel.from_name, {})[rel.to] = {
                "score": score,
                "desc": rel.desc,
            }

        # 補齊缺方向嘅關係（陌生：0 / ""）
        names = [c.display_name for c in req.characters]
        filled = 0
        for a in names:
            for b in names:
                if a == b:
                    continue
                if b not in rel_map.setdefault(a, {}):
                    rel_map[a][b] = {"score": 0, "desc": ""}
                    filled += 1
        return rel_map, clamped, filled

    def _pick_spawn_coord(self, home: list[str]) -> list[int]:
        """由住所嘅「床」tile 揀一格非 collision spawn；候選為空 fallback 同房任意格。"""
        bed_key = ":".join(home + ["床"])
        bed_tiles = self.maze.address_tiles.get(bed_key, set())
        candidates = [c for c in bed_tiles if not self.maze.tile_at(list(c)).collision]
        if not candidates:
            room_tiles = self.maze.address_tiles.get(":".join(home), set())
            candidates = [c for c in room_tiles if not self.maze.tile_at(list(c)).collision]
        if not candidates:
            raise SetupError(500, [FieldError(
                field="_",
                message=f"住所「{' · '.join(home[1:])}」搵唔到可以企嘅格，數據異常",
            )])
        return list(random.choice(candidates))

    def _render_agent_json(
        self,
        char: CharacterIn,
        template: CharacterTemplate,
        rel_map: dict[str, dict[str, dict]],
        story_opening: str,
    ) -> dict:
        # occupation/personality 可能係模板預填嘅簡體，必須過 to_traditional
        # （s2hk 對已係繁體嘅玩家輸入係 no-op，唔會誤傷）
        prefix = f"{char.display_name}係{to_traditional(char.occupation)}。"
        # currently 上限 500 字：保留前綴，截故事開端
        opening = story_opening
        if len(prefix) + len(opening) > CURRENTLY_MAX_LEN:
            opening = opening[: CURRENTLY_MAX_LEN - len(prefix)]
        dir_name = char.display_name.replace(" ", "_")
        return {
            "name": char.display_name,
            "portrait": f"assets/village/agents/{dir_name}/portrait.png",
            "coord": self._pick_spawn_coord(char.home),
            "currently": prefix + opening,
            "scratch": {
                "age": template.age,
                "innate": to_traditional(char.personality),
                "learned": prefix,
                "lifestyle": to_traditional(template.lifestyle),
                "daily_plan": to_traditional(template.daily_plan),
            },
            "spatial": {
                "address": {"living_area": list(char.home)},
                "tree": copy.deepcopy(template.spatial_tree),
            },
            # 擴展欄位：源檔記錄；checkpoint 持久化靠 sim config 嘅 relationships
            "relationships": copy.deepcopy(rel_map.get(char.display_name, {})),
        }

    def _render_story_json(
        self, req: SetupCreateRequest, rel_map: dict[str, dict[str, dict]]
    ) -> dict:
        from datetime import datetime

        relationships = [
            {"from": a, "to": b, "score": rel["score"], "desc": rel["desc"]}
            for a, targets in rel_map.items()
            for b, rel in targets.items()
        ]
        return {
            "story_name": req.story_name,
            "story_opening": req.story_opening,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "language": "zh-Hant-HK",
            "locked": True,
            "characters": [c.display_name for c in req.characters],
            "template_map": {c.display_name: c.template_id for c in req.characters},
            "relationships": relationships,
        }

    def _build_sim_config(
        self, req: SetupCreateRequest, rel_map: dict[str, dict[str, dict]]
    ) -> dict:
        with open(self.data_config_path, "r", encoding="utf-8") as f:
            agent_base = json.load(f)["agent"]
        agents: dict[str, dict] = {}
        for char in req.characters:
            dir_name = char.display_name.replace(" ", "_")
            agents[char.display_name] = {
                "config_path": os.path.join(
                    "assets", "village", "agents", dir_name, "agent.json"
                ),
                # checkpoint 持久化：SimulateServer 每 step update(to_dict())
                # 唔會覆蓋呢個 block，GM 改咗會隨下一步落盤
                "relationships": copy.deepcopy(rel_map.get(char.display_name, {})),
            }
        return {
            "stride": DEFAULT_STRIDE,
            "time": {"start": DEFAULT_TIME_START},
            "maze": {"path": os.path.join("assets", "village", "maze.json")},
            "agent_base": agent_base,
            "agents": agents,
        }

    def _suggest_story_name(self, story_name: str) -> str:
        n = 2
        while os.path.isdir(os.path.join(self.checkpoints_root, f"{story_name}-{n}")):
            n += 1
        return f"{story_name}-{n}"

    @staticmethod
    def _write_json(path: str, data: dict) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False))

    @staticmethod
    def _rollback(created_paths: list[str]) -> None:
        """逆序刪走今次新建嘅路徑，best-effort，唔遮蔽原異常。"""
        for path in reversed(created_paths):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                elif os.path.exists(path):
                    os.unlink(path)
            except Exception:
                logger.exception("rollback 刪除「%s」失敗", path)

    def estimated_llm_calls(self, req: SetupCreateRequest) -> int:
        return ESTIMATED_LLM_CALLS_PER_AGENT * len(req.characters)

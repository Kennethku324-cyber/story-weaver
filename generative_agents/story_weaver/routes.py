"""story_weaver.routes — Flask Blueprint：/setup + /api/setup/*.

app startup 建立 catalog / housing / maze singleton（見 configure）。
預設路徑相對 generative_agents/ 工作目錄。
"""

import logging
import os

from flask import Blueprint, jsonify, render_template, request
from pydantic import ValidationError

from modules import utils
from modules.maze import Maze

from .builder import StoryBuilder, SetupError
from .housing import HousingRegistry
from .schemas import FieldError, SetupCreateRequest, SetupCreateResponse
from .templates import TemplateCatalog

logger = logging.getLogger(__name__)

setup_bp = Blueprint("setup", __name__)

_state: dict = {}


def configure(
    catalog: TemplateCatalog,
    housing: HousingRegistry,
    maze,
    static_root: str,
    checkpoints_root: str,
    data_config_path: str = "data/config.json",
) -> StoryBuilder:
    """注入依賴（測試或自訂部署用）。返 StoryBuilder 實例。"""
    builder = StoryBuilder(
        catalog=catalog,
        housing=housing,
        maze=maze,
        static_root=static_root,
        checkpoints_root=checkpoints_root,
        data_config_path=data_config_path,
    )
    _state["builder"] = builder
    return builder


def _builder() -> StoryBuilder:
    if "builder" not in _state:
        static_root = "frontend/static"
        maze_path = os.path.join(static_root, "assets", "village", "maze.json")
        catalog = TemplateCatalog(os.path.join(static_root, "assets", "village", "agents"))
        catalog.scan()
        housing = HousingRegistry(maze_path)
        maze = Maze(utils.load_dict(maze_path), utils.create_io_logger("error"))
        configure(
            catalog=catalog,
            housing=housing,
            maze=maze,
            static_root=static_root,
            checkpoints_root="results/checkpoints",
        )
    return _state["builder"]


def _format_loc(loc) -> str:
    """pydantic loc tuple → "characters[2].display_name" 格式。"""
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out or "_"


def _translate_msg(e) -> str:
    """pydantic 內建英文訊息 → 繁體中文（業務 validator 嘅訊息本身係中文，原樣返回）。"""
    msg = e["msg"]
    if msg.startswith("Value error, "):
        return msg[len("Value error, "):]
    t = e["type"]
    ctx = e.get("ctx") or {}
    n = ctx.get("limit_value") or ctx.get("min_length") or ctx.get("max_length")
    loc_last = str(e["loc"][-1]) if e["loc"] else ""
    if t == "missing":
        return "呢個欄位必填"
    if t in ("too_short", "string_too_short"):
        if loc_last == "characters":
            return f"最少要揀 {n} 個角色" if n else "角色數量唔夠"
        return f"最少要 {n} 個字" if n else "內容太短"
    if t in ("too_long", "string_too_long"):
        if loc_last == "characters":
            return f"最多可以揀 {n} 個角色" if n else "角色數量太多"
        return f"最多 {n} 個字" if n else "內容太長"
    return msg


def _pydantic_errors(err: ValidationError) -> list[dict]:
    errors = []
    for e in err.errors():
        errors.append(
            FieldError(field=_format_loc(e["loc"]), message=_translate_msg(e)).model_dump()
        )
    return errors


def _parse_request():
    """解析 JSON body 做 SetupCreateRequest；失敗返 (None, errors)。"""
    data = request.get_json(silent=True)
    if data is None:
        return None, [FieldError(field="_", message="請求內容唔係合法 JSON").model_dump()]
    try:
        return SetupCreateRequest.model_validate(data), None
    except ValidationError as e:
        return None, _pydantic_errors(e)


@setup_bp.route("/setup", methods=["GET"])
def setup_page():
    return render_template("setup.html")


@setup_bp.route("/api/setup/templates", methods=["GET"])
def list_templates():
    catalog = _builder().catalog
    return jsonify({
        "templates": [
            {
                "template_id": t.template_id,
                "portrait_url": "/static/" + t.portrait_path,
                "innate": t.innate,
                "learned_first_line": t.learned_first_line,
                "living_area": t.living_area,
                "assets_complete": t.assets_complete,
            }
            for t in catalog.list()
        ]
    })


@setup_bp.route("/api/setup/housing", methods=["GET"])
def list_housing():
    housing = _builder().housing
    return jsonify({
        "rooms": [
            {"address": r.address, "label": r.label}
            for r in sorted(housing.rooms(), key=lambda r: (r.sector, r.arena))
        ]
    })


@setup_bp.route("/api/setup/validate", methods=["POST"])
def validate_setup():
    req, errors = _parse_request()
    if errors:
        return jsonify({"ok": False, "errors": errors, "clamped": [],
                        "filled_relationships": 0, "notices": []})
    builder = _builder()
    errors = [e.model_dump() for e in builder.validate(req)]
    _, clamped, filled = builder._normalize_relationships(req)
    return jsonify({
        "ok": not errors,
        "errors": errors,
        "clamped": [c.model_dump() for c in clamped],
        "filled_relationships": filled,
        "notices": builder.name_notices(req),
        "estimated_llm_calls_per_step": builder.estimated_llm_calls(req),
    })


@setup_bp.route("/api/setup/create", methods=["POST"])
def create_setup():
    req, errors = _parse_request()
    if errors:
        return jsonify({"errors": errors}), 422
    builder = _builder()
    try:
        result = builder.build(req)
    except SetupError as e:
        body = {"errors": [err.model_dump() for err in e.errors]}
        if e.suggestion:
            body["suggestion"] = e.suggestion
        return jsonify(body), e.status
    response = SetupCreateResponse(
        story_name=result.story_name,
        story_dir=result.story_dir,
        sim_config_path=result.sim_config_path,
        characters=[c.display_name for c in req.characters],
        template_map=result.template_map,
        clamped=result.clamped,
        filled_relationships=result.filled_relationships,
        llm_fallback=result.llm_fallback,
        redirect=f"/?name={result.story_name}",
    )
    return jsonify(response.model_dump()), 201

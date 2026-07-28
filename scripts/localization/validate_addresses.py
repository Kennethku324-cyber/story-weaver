"""地址引用完整性 CI 檢查（spec §3.3）。

usage: python scripts/localization/validate_addresses.py
exit 0 = 全部通過；非 0 = 有錯（逐條列出）。

檢查：每個 agent.json 嘅 spatial.tree 葉路徑、spatial.address["living_area"]、
spatial.py 推導出嘅「睡覺」地址（living_area + ["床"]），全部喺 maze.json
tiles[].address 搵到。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VILLAGE = REPO_ROOT / "generative_agents" / "frontend" / "static" / "assets" / "village"
MAZE_PATH = VILLAGE / "maze.json"
AGENTS_ROOT = VILLAGE / "agents"

KW_SLEEPING = "睡覺"
KW_BED = "床"


def _maze_prefixes(maze: dict) -> set[tuple]:
    # maze tiles[].address 係 [sector, arena, game_object]，唔含 world；
    # agent tree／living_area 路徑以 world 開頭，所以 prefix 要補返 world 段。
    world = maze.get("world", "the Ville")
    prefixes = set()
    for tile in maze["tiles"]:
        addr = tile.get("address") or []
        for i in range(1, len(addr) + 1):
            prefixes.add((world, *addr[:i]))
    return prefixes


def _walk_tree(node, path, out):
    if isinstance(node, dict):
        for k, v in node.items():
            _walk_tree(v, path + [k], out)
    elif isinstance(node, list):
        for leaf in node:
            out.append(tuple(path + [leaf]))


def validate() -> list[str]:
    """返回錯誤列表，空 = 通過。"""
    errors: list[str] = []
    maze = json.loads(MAZE_PATH.read_text(encoding="utf-8"))
    prefixes = _maze_prefixes(maze)

    agent_dirs = sorted(p for p in AGENTS_ROOT.iterdir() if p.is_dir() and (p / "agent.json").exists())
    if len(agent_dirs) != 25:
        errors.append(f"角色目錄數量唔係 25：{len(agent_dirs)}")

    for d in agent_dirs:
        agent = json.loads((d / "agent.json").read_text(encoding="utf-8"))
        name = agent.get("name", d.name)
        if name != d.name:
            errors.append(f"{d.name}: agent.json name（{name}）≠ 目錄名")
        spatial = agent.get("spatial", {})
        living = spatial.get("address", {}).get("living_area")
        if not living:
            errors.append(f"{name}: 缺 spatial.address.living_area")
        else:
            if tuple(living) not in prefixes:
                errors.append(f"{name}: living_area 喺 maze 搵唔到：{living}")
            sleeping = tuple(living) + (KW_BED,)
            if sleeping not in prefixes:
                errors.append(f"{name}: 「{KW_SLEEPING}」推導地址喺 maze 搵唔到：{list(sleeping)}")
        # spatial.address 其餘鍵（如 睡覺）都要喺 maze 搵到
        for key, addr in spatial.get("address", {}).items():
            if key == "living_area":
                continue
            if tuple(addr) not in prefixes:
                errors.append(f"{name}: address[{key}] 喺 maze 搵唔到：{addr}")
        paths: list[tuple] = []
        _walk_tree(spatial.get("tree", {}), [], paths)
        for p in paths:
            if p not in prefixes:
                errors.append(f"{name}: tree 路徑喺 maze 搵唔到：{list(p)}")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        print(f"FAIL: {len(errors)} 個地址錯誤")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("OK: 全部 agent 地址引用喺 maze 搵到")
    return 0


if __name__ == "__main__":
    sys.exit(main())

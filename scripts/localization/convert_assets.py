"""maze.json + 25 個 agent.json + 角色目錄名 + start.py personas 原子轉換。

usage:
  python scripts/localization/convert_assets.py --dry-run   # 只輸出報告，唔寫檔
  python scripts/localization/convert_assets.py             # 真跑（temp-dir + 驗證 + atomic + rollback）

流程（spec §3.3）：
  load glossary → 建 mapping → 轉 maze.json tiles[].address →
  轉 25 個 agent.json（spatial.address / spatial.tree / name / currently / scratch.* / portrait）→
  驗證（每個 agent tree 地名喺新 maze 搵到）→ temp dir 寫晒 → atomic apply →
  rename 角色目錄 → 更新 start.py personas 列表。
任何一步失敗：全部 rollback，退出碼非 0。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _convert import TextConverter, convert_address_token, load_glossary  # noqa: E402
from _report import ConvertReport  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN = REPO_ROOT / "generative_agents"
VILLAGE = GEN / "frontend" / "static" / "assets" / "village"
MAZE_PATH = VILLAGE / "maze.json"
AGENTS_ROOT = VILLAGE / "agents"
START_PY = GEN / "start.py"


def convert_maze(maze: dict, glossary: dict, report: ConvertReport) -> dict:
    out = json.loads(json.dumps(maze))  # deep copy
    for tile in out["tiles"]:
        addr = tile.get("address")
        if not addr:
            continue
        new_addr = []
        for tok in addr:
            new_tok = convert_address_token(tok, glossary)
            if new_tok != tok:
                report.replacements += 1
            new_addr.append(new_tok)
        tile["address"] = new_addr
    return out


def _convert_tree(node, glossary, report):
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            new_k = convert_address_token(k, glossary)
            if new_k != k:
                report.replacements += 1
            out[new_k] = _convert_tree(v, glossary, report)
        return out
    if isinstance(node, list):
        out = []
        for leaf in node:
            new_leaf = convert_address_token(leaf, glossary)
            if new_leaf != leaf:
                report.replacements += 1
            out.append(new_leaf)
        return out
    raise TypeError(f"spatial.tree 節點型別唔啱：{type(node)}")


def convert_agent(agent: dict, old_dir_name: str, new_dir_name: str,
                  glossary: dict, conv: TextConverter, report: ConvertReport) -> dict:
    out = json.loads(json.dumps(agent))
    names = glossary["agent_names"]

    old_name = out["name"]
    if old_name not in names:
        raise KeyError(f"agent name 未喺 glossary agent_names 登記：{old_name!r}")
    out["name"] = names[old_name]
    if out["name"] != old_name:
        report.replacements += 1

    portrait = out.get("portrait", "")
    if f"/agents/{old_dir_name}/" in portrait:
        out["portrait"] = portrait.replace(f"/agents/{old_dir_name}/", f"/agents/{new_dir_name}/")
        report.replacements += 1

    for field in ("currently",):
        if field in out and isinstance(out[field], str):
            new_val = conv.convert_text(out[field])
            if new_val != out[field]:
                report.replacements += 1
            out[field] = new_val

    scratch = out.get("scratch", {})
    for field in ("innate", "learned", "lifestyle", "daily_plan"):
        if field in scratch and isinstance(scratch[field], str):
            new_val = conv.convert_text(scratch[field])
            if new_val != scratch[field]:
                report.replacements += 1
            scratch[field] = new_val

    spatial = out.get("spatial", {})
    addr = spatial.get("address", {})
    new_addr = {}
    for k, v in addr.items():  # key（living_area / sleeping）係英文/受保護，唔郁
        new_addr[k] = [convert_address_token(tok, glossary) for tok in v]
    spatial["address"] = new_addr
    report.replacements += sum(
        1 for k in addr for a, b in zip(addr[k], new_addr[k]) if a != b
    )
    spatial["tree"] = _convert_tree(spatial.get("tree", {}), glossary, report)
    return out


# ----------------------------------------------------------------------
# 驗證：新 maze 嘅地址前綴集合 vs 每個 agent 嘅引用
# ----------------------------------------------------------------------
def _maze_prefixes(maze: dict) -> set[tuple]:
    # maze tiles[].address 係 [sector, arena, game_object]，唔含 world；
    # agent tree 路徑以 world 開頭，所以 prefix 要補返 world 段。
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
    else:
        for leaf in node:
            out.append(tuple(path + [leaf]))


def validate_all(maze: dict, agents: dict[str, dict]) -> list[str]:
    errors: list[str] = []
    prefixes = _maze_prefixes(maze)
    for name, agent in agents.items():
        spatial = agent.get("spatial", {})
        living = spatial.get("address", {}).get("living_area")
        if not living:
            errors.append(f"{name}: 缺 spatial.address.living_area")
        else:
            if tuple(living) not in prefixes:
                errors.append(f"{name}: living_area 喺 maze 搵唔到：{living}")
            sleeping = tuple(living) + ("床",)
            if sleeping not in prefixes:
                errors.append(f"{name}: 睡覺地址喺 maze 搵唔到：{list(sleeping)}")
        paths: list[tuple] = []
        _walk_tree(spatial.get("tree", {}), [], paths)
        for p in paths:
            if p not in prefixes:
                errors.append(f"{name}: tree 路徑喺 maze 搵唔到：{list(p)}")
    return errors


# ----------------------------------------------------------------------
# start.py personas 列表更新（腳本改，唔准手改）
# ----------------------------------------------------------------------
def update_start_py(names: dict[str, str], report: ConvertReport, dry_run: bool) -> str:
    src = START_PY.read_text(encoding="utf-8")
    out = src
    for old, new in sorted(names.items(), key=lambda kv: len(kv[0]), reverse=True):
        if old == new:
            continue
        quoted_old = f'"{old}"'
        if quoted_old in out:
            report.replacements += out.count(quoted_old)
            out = out.replace(quoted_old, f'"{new}"')
    if not dry_run and out != src:
        START_PY.write_text(out, encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glossary", default=str(GEN / "data" / "glossary_s2hk.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    report = ConvertReport(dry_run=args.dry_run)
    glossary = load_glossary(args.glossary)
    conv = TextConverter(glossary)
    names = glossary["agent_names"]

    # ---- 掃描 + 轉換（全部喺記憶體做）----
    maze_old = json.loads(MAZE_PATH.read_text(encoding="utf-8"))
    report.files_scanned += 1
    maze_new = convert_maze(maze_old, glossary, report)

    agent_dirs = sorted(p for p in AGENTS_ROOT.iterdir() if p.is_dir() and (p / "agent.json").exists())
    if len(agent_dirs) != 25:
        report.errors.append(f"角色目錄數量唔係 25：{len(agent_dirs)}")
        print(report.summary())
        return 1

    agents_new: dict[str, dict] = {}
    dir_renames: list[tuple[Path, Path]] = []
    for d in agent_dirs:
        report.files_scanned += 1
        agent = json.loads((d / "agent.json").read_text(encoding="utf-8"))
        old_dir = d.name
        if old_dir not in names:
            report.errors.append(f"角色目錄名未喺 agent_names 登記：{old_dir}")
            continue
        new_dir = names[old_dir]
        new_agent = convert_agent(agent, old_dir, new_dir, glossary, conv, report)
        if new_agent["name"] != new_dir:
            report.errors.append(f"{old_dir}: agent.json name（{new_agent['name']}）≠ 目錄名（{new_dir}）")
        agents_new[old_dir] = new_agent
        if new_dir != old_dir:
            dir_renames.append((d, d.with_name(new_dir)))

    targets = [t for _, t in dir_renames]
    if len(set(targets)) != len(targets):
        report.errors.append("目錄改名後有撞名")
    for t in targets:
        if t.exists():
            report.errors.append(f"目標目錄已存在：{t.name}")
    if report.errors:
        print(report.summary())
        return 1

    # ---- 驗證（寫檔前）----
    errors = validate_all(maze_new, agents_new)
    report.errors.extend(errors)
    if report.errors:
        print(report.summary())
        return 1

    changed_files = sum(
        1 for old_dir, new in agents_new.items()
        if new != json.loads((AGENTS_ROOT / old_dir / "agent.json").read_text(encoding="utf-8"))
    )
    report.files_changed = (1 if maze_new != maze_old else 0) + changed_files

    if args.dry_run:
        print(f"[dry-run] dir renames: {len(dir_renames)}")
        for old, new in dir_renames:
            print(f"  {old.name} -> {new.name}")
        print(report.summary())
        return 0

    # ---- temp dir 寫晒 → atomic apply → rollback 保護 ----
    with tempfile.TemporaryDirectory(prefix="loc_convert_") as tmp:
        tmp_path = Path(tmp)
        staged_maze = tmp_path / "maze.json"
        staged_maze.write_text(json.dumps(maze_new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staged_agents = {}
        for old_dir, new_agent in agents_new.items():
            p = tmp_path / f"agent__{old_dir}.json"
            p.write_text(json.dumps(new_agent, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            staged_agents[old_dir] = p

        # 備份原檔
        backup = tmp_path / "backup"
        backup.mkdir()
        shutil.copy2(MAZE_PATH, backup / "maze.json")
        (backup / "agents").mkdir()
        for old_dir in agents_new:
            shutil.copy2(AGENTS_ROOT / old_dir / "agent.json", backup / "agents" / f"{old_dir}.json")
        start_backup = START_PY.read_text(encoding="utf-8")

        applied_dirs: list[tuple[Path, Path]] = []
        try:
            os.replace(staged_maze, MAZE_PATH)
            for old_dir, staged in staged_agents.items():
                os.replace(staged, AGENTS_ROOT / old_dir / "agent.json")
            # 目錄改名（經臨時名避免同名撞車）
            for old, new in dir_renames:
                tmp_name = old.with_name(f".__renaming__{old.name}")
                old.rename(tmp_name)
                tmp_name.rename(new)
                applied_dirs.append((old, new))
            update_start_py(names, report, dry_run=False)
        except Exception as exc:  # rollback
            report.errors.append(f"apply 中途失敗，已 rollback：{exc}")
            shutil.copy2(backup / "maze.json", MAZE_PATH)
            for old_dir in agents_new:
                shutil.copy2(backup / "agents" / f"{old_dir}.json", AGENTS_ROOT / old_dir / "agent.json")
            for old, new in reversed(applied_dirs):
                if new.exists() and not old.exists():
                    new.rename(old)
            START_PY.write_text(start_backup, encoding="utf-8")
            print(report.summary())
            return 1

    # ---- apply 後再驗證一次（從磁碟讀）----
    maze_disk = json.loads(MAZE_PATH.read_text(encoding="utf-8"))
    agents_disk = {}
    for d in sorted(p for p in AGENTS_ROOT.iterdir() if p.is_dir() and (p / "agent.json").exists()):
        agents_disk[d.name] = json.loads((d / "agent.json").read_text(encoding="utf-8"))
    post_errors = validate_all(maze_disk, agents_disk)
    report.errors.extend(post_errors)
    print(report.summary())
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())

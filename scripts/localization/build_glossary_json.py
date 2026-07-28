"""由 docs/prd/localization-glossary.md 單向生成機讀 glossary_s2hk.json。

usage: python scripts/localization/build_glossary_json.py [--check]

MD 係人讀 SSoT；JSON 係腳本同 normalize fallback 嘅數據源。
--check：只驗證現有 JSON 同 MD 同步，唔寫檔（CI 用）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MD_PATH = REPO_ROOT / "docs" / "prd" / "localization-glossary.md"
JSON_PATH = REPO_ROOT / "generative_agents" / "data" / "glossary_s2hk.json"

SECTIONS = ("keywords", "place_names", "agent_names", "vocabulary")


def parse_glossary_md(md_path: Path = MD_PATH) -> dict:
    data: dict = {s: {} for s in SECTIONS}
    data["protected_tokens"] = []
    section = None
    for raw in md_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            name = line[3:].strip()
            section = name if name in SECTIONS or name == "protected_tokens" else None
            continue
        if section in SECTIONS and line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 2 or cells[0] in ("簡體", "") or set(cells[0]) <= {"-", ":"}:
                continue
            data[section][cells[0]] = cells[1]
        elif section == "protected_tokens" and line.startswith("- "):
            data["protected_tokens"].append(line[2:].strip().strip("`"))
    return data


def build_json(data: dict) -> dict:
    return {
        "version": "1.0",
        "generated_from": "docs/prd/localization-glossary.md",
        "keywords": data["keywords"],
        "place_names": data["place_names"],
        "agent_names": data["agent_names"],
        "vocabulary": data["vocabulary"],
        "protected_tokens": data["protected_tokens"],
    }


def cross_check_maze(data: dict) -> list[str]:
    """驗證 place_names 全量覆蓋 maze.json tiles[].address 嘅 CJK token。"""
    maze_path = REPO_ROOT / "generative_agents" / "frontend" / "static" / "assets" / "village" / "maze.json"
    maze = json.loads(maze_path.read_text(encoding="utf-8"))
    tokens = set()
    for tile in maze["tiles"]:
        for tok in tile.get("address") or []:
            tokens.add(tok)
    tokens.discard("the Ville")
    place = data["place_names"]
    missing = sorted(t for t in tokens if t not in place and t not in place.values())
    return [f"maze token 未覆蓋：{t}" for t in missing]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只驗證 JSON 同 MD 同步")
    args = ap.parse_args()

    data = parse_glossary_md()
    errors = cross_check_maze(data)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    out = build_json(data)
    if args.check:
        current = json.loads(JSON_PATH.read_text(encoding="utf-8")) if JSON_PATH.exists() else None
        if current != out:
            print("ERROR: glossary_s2hk.json 同 MD 唔同步，請重跑 build_glossary_json.py", file=sys.stderr)
            return 1
        print("OK: JSON 同 MD 同步")
        return 0

    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts = {s: len(out[s]) for s in SECTIONS}
    print(f"written {JSON_PATH} counts={counts} protected={len(out['protected_tokens'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

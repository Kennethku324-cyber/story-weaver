"""29 個 prompt 模板繁體化：機轉底稿生成 + --check CI 驗證。

usage:
  python scripts/localization/convert_prompts.py           # 轉換（in-place，idempotent）
  python scripts/localization/convert_prompts.py --dry-run # 只報告邊啲檔會變
  python scripts/localization/convert_prompts.py --check   # CI 驗證，唔寫檔

轉換規則（spec §5.11）：
- 中文內容 OpenCC s2hk + glossary 詞彙（vocabulary 措辭覆蓋優先）
- ${} 佔位符名零改動；protected_tokens 唔郁
- 內嵌 JSON 示例 key 唔郁，只轉 value／自然語言
- 尾部統一追加 TRADITIONAL_CHINESE_DIRECTIVE

--check 驗證每個模板：
(a) 無簡體黑名單字 (b) ${} 佔位符良好（名稱 ASCII、配對）
(c) 尾部含 TRADITIONAL_CHINESE_DIRECTIVE (d) 內嵌 JSON 示例段 json.loads 可解析
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _convert import TextConverter, load_glossary, simplified_chars_in  # noqa: E402
from _report import ConvertReport  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "generative_agents" / "data" / "prompts"

try:
    sys.path.insert(0, str(REPO_ROOT / "generative_agents"))
    from modules.prompt.keywords import TRADITIONAL_CHINESE_DIRECTIVE  # type: ignore
except Exception:  # keywords.py 未就位時用字面值（同 spec §3.1 一致）
    TRADITIONAL_CHINESE_DIRECTIVE = "一律使用繁體中文（香港書面語）回答。"

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]*)\}")


def extract_json_blocks(text: str) -> list[str]:
    """抽出獨立成行嘅 JSON 示例段（{...} 或 [...]），用行級配對。"""
    blocks = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped in ("{", "["):
            opener = stripped
            closer = "}" if opener == "{" else "]"
            depth = 0
            buf = []
            j = i
            while j < len(lines):
                buf.append(lines[j])
                depth += lines[j].count(opener) - lines[j].count(closer)
                if depth == 0:
                    break
                j += 1
            if depth == 0:
                blocks.append("\n".join(buf))
                i = j + 1
                continue
        i += 1
    return blocks


def convert_template(text: str, conv: TextConverter) -> str:
    out = conv.convert_text(text)
    if TRADITIONAL_CHINESE_DIRECTIVE not in out:
        out = out.rstrip("\n") + "\n\n" + TRADITIONAL_CHINESE_DIRECTIVE + "\n"
    return out


def check_template(path: Path) -> list[str]:
    errors = []
    text = path.read_text(encoding="utf-8")
    bad = simplified_chars_in(text)
    if bad:
        errors.append(f"含簡體字：{''.join(sorted(bad))}")
    # 佔位符良好性：名稱只准 ASCII identifier
    for m in _PLACEHOLDER_RE.finditer(text):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", m.group(1)):
            errors.append(f"佔位符異常：${{{m.group(1)}}}")
    if text.count("${") != len(_PLACEHOLDER_RE.findall(text)):
        errors.append("存在未配對嘅 ${ 佔位符")
    if TRADITIONAL_CHINESE_DIRECTIVE not in text:
        errors.append("尾部缺 TRADITIONAL_CHINESE_DIRECTIVE")
    for block in extract_json_blocks(text):
        try:
            json.loads(block)
        except json.JSONDecodeError as exc:
            if "..." in block or "(" in block:
                continue  # pseudo-schema 插圖（tuple／省略號），唔係 JSON 示例
            errors.append(f"JSON 示例段解析失敗：{exc}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glossary", default=str(REPO_ROOT / "generative_agents" / "data" / "glossary_s2hk.json"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    templates = sorted(PROMPTS_DIR.glob("*.txt"))
    if len(templates) != 29:
        print(f"ERROR: 模板數量唔係 29：{len(templates)}", file=sys.stderr)
        return 1

    if args.check:
        failed = 0
        for p in templates:
            errors = check_template(p)
            if errors:
                failed += 1
                print(f"FAIL {p.name}")
                for e in errors:
                    print(f"  - {e}")
        if failed:
            print(f"{failed}/{len(templates)} 個模板未過 check")
            return 1
        print(f"OK: {len(templates)} 個模板全部通過 check")
        return 0

    glossary = load_glossary(args.glossary)
    conv = TextConverter(glossary)
    report = ConvertReport(dry_run=args.dry_run)
    for p in templates:
        report.files_scanned += 1
        src = p.read_text(encoding="utf-8")
        out = convert_template(src, conv)
        if out != src:
            report.files_changed += 1
            report.replacements += sum(1 for a, b in zip(src, out) if a != b) + abs(len(out) - len(src))
            if not args.dry_run:
                p.write_text(out, encoding="utf-8")
    print(report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())

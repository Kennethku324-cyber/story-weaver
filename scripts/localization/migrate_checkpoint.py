"""舊 checkpoint 簡→繁遷移（spec §3.3）。

usage:
  python scripts/localization/migrate_checkpoint.py results/checkpoints/<name>
  python scripts/localization/migrate_checkpoint.py results/checkpoints/<name> --in-place

對 simulate-*.json + conversation.json 內所有 describe/address/scratch/event 文字
跑 s2hk 轉換（glossary 詞彙優先）；dict key 唔郁（JSON schema key 如 "6:00" 唔係自然語言）。
預設寫去 <name>-zhHK/；--in-place 先覆寫（每個檔先備份 .bak）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _convert import TextConverter, load_glossary  # noqa: E402
from _report import MigrateReport  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


def migrate(checkpoint_dir: str, in_place: bool = False,
            glossary_path: str | None = None) -> MigrateReport:
    src = Path(checkpoint_dir)
    if not src.is_dir():
        raise FileNotFoundError(f"checkpoint 目錄唔存在：{src}")
    glossary = load_glossary(glossary_path or REPO_ROOT / "generative_agents" / "data" / "glossary_s2hk.json")
    conv = TextConverter(glossary)

    report = MigrateReport(checkpoint_dir=str(src))
    if in_place:
        out_dir = src
    else:
        out_dir = src.with_name(src.name + "-zhHK")
        out_dir.mkdir(parents=True, exist_ok=True)
    report.output_dir = str(out_dir)

    targets = sorted(src.glob("simulate-*.json")) + sorted(src.glob("conversation.json"))
    if not targets:
        report.warnings.append("搵唔到 simulate-*.json / conversation.json")
        return report

    for f in targets:
        data = json.loads(f.read_text(encoding="utf-8"))
        converted = conv.convert_json_values(data)
        if converted != data:
            report.concepts_normalized += _count_diff_strings(data, converted)
        out_path = out_dir / f.name
        if in_place:
            bak = f.with_suffix(f.suffix + ".bak")
            shutil.copy2(f, bak)
        out_path.write_text(json.dumps(converted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report.files_migrated += 1
    return report


def _count_diff_strings(a, b) -> int:
    if isinstance(a, str) and isinstance(b, str):
        return 1 if a != b else 0
    if isinstance(a, list) and isinstance(b, list):
        return sum(_count_diff_strings(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return sum(_count_diff_strings(a[k], b[k]) for k in a if k in b)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("checkpoint_dir")
    ap.add_argument("--in-place", action="store_true", help="覆寫原檔（先備份 .bak）")
    ap.add_argument("--glossary", default=None)
    args = ap.parse_args()
    try:
        report = migrate(args.checkpoint_dir, args.in_place, args.glossary)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())

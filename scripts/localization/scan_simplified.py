"""全 repo 簡體掃描（CI gate，spec §3.3）。

usage: python scripts/localization/scan_simplified.py
exit 0 = 零命中；非 0 = 有簡體殘留（逐檔逐字列出）。

掃描範圍：
- generative_agents/data/prompts/*.txt
- generative_agents/modules/**/*.py（豁免 keywords.py、timer.py；.py 只掃非註釋部分）
- generative_agents/frontend/templates/*.html
- generative_agents/frontend/static/assets/village/**/*.json

黑名單：OpenCC s2hk 會改動嘅 CJK 字（同轉換器同源；OpenCC 缺裝時用內置高頻表）。
注意：HK 標準字形（如「卧」「台」）喺 s2hk 映像內，唔會誤傷。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN = REPO_ROOT / "generative_agents"

# keywords.py：繁體常量 SSoT；timer.py：簡繁同形白名單（spec §5.10）；
# text_normalize.py：內嵌簡體黑名單字表做偵測數據，簡體字係故意嘅
EXEMPT_PY = {"keywords.py", "timer.py", "text_normalize.py"}

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _convert import simplified_chars_in  # noqa: E402


def _strip_py_comments(text: str) -> str:
    """.naive 註釋剝離：逐行切 # 之後嘅嘢（掃描工具，容許 f-string 內 # 誤切）。"""
    out = []
    for line in text.splitlines():
        pos = line.find("#")
        out.append(line if pos < 0 else line[:pos])
    return "\n".join(out)


def scan() -> dict[str, set[str]]:
    hits: dict[str, set[str]] = {}

    def _check(path: Path, text: str):
        bad = simplified_chars_in(text)
        if bad:
            hits[str(path.relative_to(REPO_ROOT))] = bad

    for p in sorted((GEN / "data" / "prompts").glob("*.txt")):
        _check(p, p.read_text(encoding="utf-8"))
    for p in sorted((GEN / "modules").rglob("*.py")):
        if p.name in EXEMPT_PY:
            continue
        _check(p, _strip_py_comments(p.read_text(encoding="utf-8")))
    for p in sorted((GEN / "frontend" / "templates").glob("*.html")):
        _check(p, p.read_text(encoding="utf-8"))
    for p in sorted((GEN / "frontend" / "static" / "assets" / "village").rglob("*.json")):
        _check(p, p.read_text(encoding="utf-8"))
    return hits


def main() -> int:
    hits = scan()
    if hits:
        print(f"FAIL: {len(hits)} 個檔案有簡體殘留")
        for path, chars in hits.items():
            print(f"  {path}: {''.join(sorted(chars))}")
        return 1
    print("OK: 掃描範圍零簡體命中")
    return 0


if __name__ == "__main__":
    sys.exit(main())

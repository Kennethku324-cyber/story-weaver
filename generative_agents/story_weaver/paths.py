"""Project paths resolved from this package, never from the current directory."""

from pathlib import Path


GEN_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = GEN_ROOT / "data"
GM_PROMPTS_ROOT = DATA_ROOT / "prompts_gm"
RECAP_PROMPTS_ROOT = DATA_ROOT / "prompts"
GM_CONFIG_PATH = DATA_ROOT / "gm_config.json"
DATA_CONFIG_PATH = DATA_ROOT / "config.json"

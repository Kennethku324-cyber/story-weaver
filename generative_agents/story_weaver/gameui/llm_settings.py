"""story_weaver.gameui.llm_settings — LLM 設定讀寫（/settings 頁後盾）。

三組配置：
- agent_llm：data/config.json → agent.think.llm（角色思考用）
- embedding：data/config.json → agent.associate.embedding（記憶檢索用）
- gm_llm：data/gm_config.json → llm（GM / 故事回顧用）

api_key 保安：GET 唔會返真 key，只返 api_key_set + 頭 4 位提示；
POST 收到空 api_key → 保留舊值。兩個檔都係原子寫（tmp + os.replace）。
"""

from __future__ import annotations

import copy
import json
import logging
import os

logger = logging.getLogger(__name__)

GEN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(GEN_ROOT, "data", "config.json")
GM_CONFIG_PATH = os.path.join(GEN_ROOT, "data", "gm_config.json")

LLM_KEYS = ("provider", "model", "base_url", "api_key")
PROVIDERS = ("ollama", "openai", "hugging_face")  # hugging_face：本機 embedding，唔使 key/base_url


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.warning("llm_settings: 讀唔到 %s", path, exc_info=True)
        return {}


def _write_json_atomic(path: str, data: dict) -> None:
    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _mask(cfg: dict) -> dict:
    """對外輸出版本：api_key 只返設定狀態 + 頭幾位提示。"""
    out = {k: cfg.get(k, "") for k in LLM_KEYS if k != "api_key"}
    key = cfg.get("api_key") or ""
    out["api_key"] = ""
    out["api_key_set"] = bool(key)
    out["api_key_hint"] = (key[:4] + "…") if key else ""
    return out


def load_settings() -> dict:
    config = _read_json(CONFIG_PATH)
    gm_config = _read_json(GM_CONFIG_PATH)
    return {
        "agent_llm": _mask(((config.get("agent") or {}).get("think") or {}).get("llm") or {}),
        "embedding": _mask(((config.get("agent") or {}).get("associate") or {}).get("embedding") or {}),
        "gm_llm": _mask(gm_config.get("llm") or {}),
        "pace": {
            "steps_per_round": int(gm_config.get("steps_per_round", 6)),
            "chat_iter": int(((config.get("agent") or {}).get("chat_iter")) or 4),
            "max_rounds": int(gm_config.get("max_rounds", 10)),
        },
    }


def _validate_section(name: str, section: dict) -> str | None:
    """返錯誤訊息；None = 通過。"""
    provider = section.get("provider", "")
    if provider not in PROVIDERS:
        return f"{name}：provider 必須係 ollama、openai 或 hugging_face"
    if not (section.get("model") or "").strip():
        return f"{name}：model 必填"
    if provider != "hugging_face" and not (section.get("base_url") or "").strip():
        return f"{name}：base_url 必填"
    return None


def save_settings(payload: dict) -> list[str]:
    """寫入三組配置 + pace（每回合步數）。返錯誤列表（空 = 成功）。空 api_key 保留舊值。"""
    errors = []
    for name in ("agent_llm", "embedding", "gm_llm"):
        if name in payload:
            err = _validate_section(name, payload[name])
            if err:
                errors.append(err)
    pace = payload.get("pace")
    if pace is not None:
        try:
            steps = int(pace.get("steps_per_round", 0))
            if not 1 <= steps <= 20:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("每回合步數必須係 1-20 嘅整數")
        try:
            chat_iter = int(pace.get("chat_iter", 0))
            if not 1 <= chat_iter <= 8:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("對話長度必須係 1-8 嘅整數")
        try:
            max_rounds = int(pace.get("max_rounds", 0))
            if not 2 <= max_rounds <= 50:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("總回合數必須係 2-50 嘅整數")
    if errors:
        return errors

    if pace is not None:
        gm_pace = _read_json(GM_CONFIG_PATH)
        gm_pace["steps_per_round"] = int(pace["steps_per_round"])
        gm_pace["max_rounds"] = int(pace["max_rounds"])
        _write_json_atomic(GM_CONFIG_PATH, gm_pace)
        cfg_pace = _read_json(CONFIG_PATH)
        cfg_pace.setdefault("agent", {})["chat_iter"] = int(pace["chat_iter"])
        _write_json_atomic(CONFIG_PATH, cfg_pace)

    config = _read_json(CONFIG_PATH)
    gm_config = _read_json(GM_CONFIG_PATH)
    targets = {
        "agent_llm": (config, ("agent", "think", "llm"), CONFIG_PATH),
        "embedding": (config, ("agent", "associate", "embedding"), CONFIG_PATH),
        "gm_llm": (gm_config, ("llm",), GM_CONFIG_PATH),
    }
    touched: dict[str, dict] = {}
    for name, section in payload.items():
        if name not in targets:
            continue
        root, path_keys, path = targets[name]
        node = root
        for k in path_keys[:-1]:
            node = node.setdefault(k, {})
        old = node.get(path_keys[-1]) or {}
        new = {k: section.get(k, old.get(k, "")) for k in LLM_KEYS}
        if not (section.get("api_key") or "").strip():
            new["api_key"] = old.get("api_key", "")  # 空 key → 保留舊值
        node[path_keys[-1]] = new
        touched[path] = root

    for path, data in touched.items():
        _write_json_atomic(path, data)
    return []


def test_llm(cfg: dict) -> tuple[bool, str]:
    """試連線：LLM 叫佢講一句嘢；hugging_face embedding 就 embed 一句嘢。返 (ok, 訊息)。"""
    err = _validate_section("LLM", cfg)
    if err:
        return False, err
    if cfg.get("provider") == "hugging_face":
        try:
            from llama_index.embeddings.huggingface import HuggingFaceEmbedding

            embed = HuggingFaceEmbedding(model_name=cfg["model"])
            vec = embed.get_text_embedding("連線測試。")
            return True, f"本機模型載入成功（向量維度 {len(vec)}），唔使 API key。"
        except Exception as e:
            return False, f"模型載入失敗：{e}"
    try:
        from pydantic import BaseModel

        from modules.model.llm_model import create_llm_model

        class _Ping(BaseModel):
            res: str

        llm = create_llm_model(cfg)
        result = llm.completion(
            "用一句繁體中文話「連線成功」。",
            retry=1,
            failsafe=None,
            return_type=_Ping,
            caller="settings_test",
        )
        if result is None:
            return False, "連唔上或者冇回應——檢查 base_url、API key 同 model 名。"
        return True, f"連線成功，模型回應：{str(result)[:50]}"
    except Exception as e:
        return False, f"連線失敗：{e}"

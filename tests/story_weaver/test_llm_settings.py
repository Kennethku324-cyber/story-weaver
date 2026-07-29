"""llm_settings 測試：讀寫、api_key 保留、校驗、mask。"""

import json

import pytest

from story_weaver.gameui import llm_settings
from story_weaver.gameui.llm_settings import load_settings, save_settings


@pytest.fixture()
def cfg_files(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    gm_path = tmp_path / "gm_config.json"
    config_path.write_text(json.dumps({
        "agent": {
            "think": {"llm": {"provider": "ollama", "model": "qwen3", "base_url": "http://x", "api_key": "sk-old-secret"}},
            "associate": {"embedding": {"provider": "ollama", "model": "emb", "base_url": "http://y", "api_key": ""}},
        }
    }), encoding="utf-8")
    gm_path.write_text(json.dumps({
        "llm": {"provider": "openai", "model": "gpt", "base_url": "http://z", "api_key": ""},
        "max_rounds": 10,
    }), encoding="utf-8")
    monkeypatch.setattr(llm_settings, "CONFIG_PATH", str(config_path))
    monkeypatch.setattr(llm_settings, "GM_CONFIG_PATH", str(gm_path))
    return config_path, gm_path


def test_load_masks_api_key(cfg_files):
    s = load_settings()
    assert s["agent_llm"]["api_key"] == ""
    assert s["agent_llm"]["api_key_set"] is True
    assert s["agent_llm"]["api_key_hint"].startswith("sk-o")
    assert s["embedding"]["api_key_set"] is False
    assert s["gm_llm"]["provider"] == "openai"


def test_save_preserves_key_when_empty(cfg_files):
    errors = save_settings({
        "agent_llm": {"provider": "openai", "model": "kimi-k2", "base_url": "https://api.example.com/v1", "api_key": ""},
    })
    assert errors == []
    s = load_settings()
    assert s["agent_llm"]["model"] == "kimi-k2"
    assert s["agent_llm"]["api_key_set"] is True  # 舊 key 留低
    assert s["agent_llm"]["api_key_hint"].startswith("sk-o")


def test_save_new_key_overwrites(cfg_files):
    save_settings({
        "agent_llm": {"provider": "openai", "model": "m", "base_url": "http://b", "api_key": "sk-new-key"},
    })
    s = load_settings()
    assert s["agent_llm"]["api_key_hint"].startswith("sk-n")


def test_save_validation_errors(cfg_files):
    errors = save_settings({
        "agent_llm": {"provider": "bad", "model": "", "base_url": "", "api_key": ""},
    })
    assert len(errors) == 1
    assert "provider" in errors[0]
    # 驗證失敗唔會寫檔
    assert load_settings()["agent_llm"]["model"] == "qwen3"


def test_save_gm_keeps_other_fields(cfg_files):
    save_settings({
        "gm_llm": {"provider": "openai", "model": "deepseek", "base_url": "http://g", "api_key": "k"},
    })
    _config, gm_path = cfg_files
    raw = json.loads(gm_path.read_text(encoding="utf-8"))
    assert raw["max_rounds"] == 10  # 其他欄位唔郁
    assert raw["llm"]["model"] == "deepseek"


def test_atomic_write_no_tmp(cfg_files, tmp_path):
    save_settings({
        "agent_llm": {"provider": "ollama", "model": "q", "base_url": "http://x", "api_key": ""},
    })
    assert not [f for f in tmp_path.iterdir() if ".tmp." in f.name]

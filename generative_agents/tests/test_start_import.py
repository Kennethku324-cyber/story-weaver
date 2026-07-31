import importlib
import sys


def test_importing_start_ignores_gunicorn_arguments(monkeypatch):
    """The web server imports SimulateServer, so its argv must not be parsed."""
    monkeypatch.setattr(sys, "argv", [
        "gunicorn",
        "story_weaver.gameui.game_server:app",
        "--bind",
        "0.0.0.0:5001",
    ])
    sys.modules.pop("start", None)

    start = importlib.import_module("start")

    assert start.args.name == ""

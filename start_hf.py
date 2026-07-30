#!/usr/bin/env python3
"""HuggingFace Spaces entry point."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "generative_agents"))

from story_weaver.gameui.game_server import app

port = int(os.environ.get("PORT", 7860))
app.run(host="0.0.0.0", port=port, threaded=True)

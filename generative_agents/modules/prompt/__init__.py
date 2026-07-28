"""generative_agents.prompt"""

from .keywords import *


def __getattr__(name):
    # 惰性載入 Scratch：避免 modules.memory.event 引用 keywords 時
    # 經 prompt/__init__ → scratch → modules.memory 造成循環 import
    if name == "Scratch":
        from .scratch import Scratch
        return Scratch
    raise AttributeError("module {!r} has no attribute {!r}".format(__name__, name))

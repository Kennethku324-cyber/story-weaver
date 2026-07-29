"""story_weaver.recap.store — StoryRecapStore：story_recap.json 原子讀寫（spec §1.3）。

tmp + os.replace()：kill -9 最多留一個 .tmp 殘檔，主檔永遠係合法 JSON。
寫入上 threading.Lock；讀取唔上鎖（最多讀到上一個原子版本）。
"""

from __future__ import annotations

import glob
import json
import logging
import os
import threading

from .models import StoryRecap

logger = logging.getLogger(__name__)


class StoryRecapStore:
    """每個 sim 一個 story_recap.json。本系統獨家讀寫。"""

    def __init__(self, checkpoints_root: str = "results/checkpoints") -> None:
        self._root = checkpoints_root
        self._lock = threading.Lock()
        self._cleanup_stale_tmps()

    def _path(self, sim_name: str) -> str:
        return os.path.join(self._root, sim_name, "story_recap.json")

    def _cleanup_stale_tmps(self) -> None:
        for tmp in glob.glob(os.path.join(self._root, "*", "story_recap.json.tmp.*")):
            try:
                os.remove(tmp)
                logger.info("recap store: 清理殘留 tmp 檔 %s", tmp)
            except OSError:
                pass

    def exists(self, sim_name: str) -> bool:
        return os.path.exists(self._path(sim_name))

    def load(self, sim_name: str) -> StoryRecap | None:
        """檔案唔存在 → None；損毀 → None + warning（主檔永遠合法，損毀即係外部干預）。"""
        path = self._path(sim_name)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return StoryRecap.from_dict(json.load(f))
        except Exception:
            logger.warning("recap store: story_recap.json 損毀（%s）", path, exc_info=True)
            return None

    def save(self, recap: StoryRecap) -> None:
        """原子寫入：tmp.<pid> → os.replace。"""
        path = self._path(recap.sim_name)
        tmp_path = f"{path}.tmp.{os.getpid()}"
        with self._lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(recap.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)

    def update(self, sim_name: str, mutator) -> StoryRecap | None:
        """load → mutator(recap) → save，成個過程上鎖，避免 read-modify-write 競態。"""
        with self._lock:
            recap = self.load(sim_name)
            if recap is None:
                return None
            mutator(recap)
            # save 內部會再攞鎖（threading.Lock 唔可重入），所以呢度直接內聯寫入
            path = self._path(sim_name)
            tmp_path = f"{path}.tmp.{os.getpid()}"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(recap.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            return recap

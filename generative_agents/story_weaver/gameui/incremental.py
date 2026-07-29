"""story_weaver.gameui.incremental — FrameBuffer：checkpoint → 增量 frame + feed（spec §2.3）。

邏輯複寫自 compress.py generate_movement（增量版）：
- 跳過已處理 step；last_location 跨 scan 保持；
- 對話逐句結構化（唔係 compress.py 嘅一舊 string）；
- 唔寫檔（memory-only；server 重啟由 checkpoint 全量重建）。
"""

from __future__ import annotations

import json
import logging
import os
import re

from modules.prompt.keywords import KW_SLEEPING

from .models import DialogueLine, FeedItem, FeedKind

logger = logging.getLogger(__name__)

SIMULATE_FILE_RE = re.compile(r"^simulate-\d{12}\.json$")

# 同 compress.py:12 保持一致（唔 import compress——佢會連帶 import start.py，
# 而 start.py 喺 import 時 parse_args，非 CLI 環境會炸）
FRAMES_PER_STEP = 60


def _get_location(address: list) -> str | None:
    """同 compress.py get_location：唔顯示第一級（小鎮名）；空地址 → None。"""
    if not address:
        return None
    return "，".join(address[1:]) if len(address) > 1 else "，".join(address)


def load_maze(static_root: str = "frontend/static"):
    """同 compress.py:97-103：讀 assets/village/maze.json 建 Maze。"""
    from modules import utils
    from modules.maze import Maze

    maze_path = os.path.join(static_root, "assets", "village", "maze.json")
    with open(maze_path, "r", encoding="utf-8") as f:
        return Maze(json.load(f), utils.create_io_logger("error"))


class FrameBuffer:
    """每個 session 一個。frame 結構同 compress.py all_movement 完全一致。"""

    def __init__(self, checkpoints_folder: str, maze) -> None:
        self._folder = checkpoints_folder
        self._maze = maze
        self._frames: dict[str, dict] = {}  # step_key -> {agent: {movement, location, action}}
        self._feed: list[FeedItem] = []
        self._feed_seq = 0
        self._last_location: dict[str, dict] = {}  # agent -> {"movement": [x,y], "location": str}
        self._conversation_seen: set[str] = set()
        self._last_step = 0

    # ---------------------------------------------------------------- 查詢

    def frames_since(self, frame_cursor: int) -> dict[str, dict]:
        return {k: v for k, v in self._frames.items() if k.isdigit() and int(k) > frame_cursor}

    def latest_frame_key(self) -> int:
        keys = [int(k) for k in self._frames.keys() if k.isdigit()]
        return max(keys) if keys else 0

    def feed_since(self, feed_cursor: int) -> list[FeedItem]:
        return [f for f in self._feed if f.seq > feed_cursor]

    @property
    def feed_latest(self) -> int:
        return self._feed_seq

    @property
    def last_step(self) -> int:
        return self._last_step

    # ---------------------------------------------------------------- 掃描

    def scan(self, processed_steps: list[int] | None = None) -> dict:
        """掃新 checkpoint，更新內部狀態。返回 {"new_frames", "new_feed", "last_step", "skipped"}。"""
        processed = set(processed_steps or [])
        try:
            files = sorted(
                f for f in os.listdir(self._folder) if SIMULATE_FILE_RE.match(f)
            )
        except OSError:
            return {"new_frames": {}, "new_feed": [], "last_step": self._last_step, "skipped": []}

        conversation = self._load_conversation()
        new_frames: dict[str, dict] = {}
        new_feed: list[FeedItem] = []
        skipped: list[str] = []

        for file_name in files:
            path = os.path.join(self._folder, file_name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                step = data["step"]
            except Exception:
                skipped.append(file_name)  # 半截檔（推演中被殺）→ 跳過
                continue
            if step in processed or step <= self._last_step:
                continue
            self._process_checkpoint(data, conversation, new_frames, new_feed)
            self._last_step = max(self._last_step, step)

        self._frames.update(new_frames)
        self._feed.extend(new_feed)
        return {
            "new_frames": new_frames,
            "new_feed": [f.model_dump() for f in new_feed],
            "last_step": self._last_step,
            "skipped": skipped,
        }

    def _load_conversation(self) -> dict:
        path = os.path.join(self._folder, "conversation.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.warning("framebuffer: conversation.json 損毀，對話 feed 暫缺")
            return {}

    def _next_seq(self) -> int:
        self._feed_seq += 1
        return self._feed_seq

    def _process_checkpoint(self, data: dict, conversation: dict,
                            new_frames: dict, new_feed: list[FeedItem]) -> None:
        step = data["step"]
        step_time = data.get("time", "")
        agents = data.get("agents") or {}

        # 對話（逐句結構化，一個 key 一條 FeedItem）
        persons_in_conversation: list[list[str]] = []
        if step_time in conversation and step_time not in self._conversation_seen:
            for chats in conversation[step_time]:
                for header, chat in chats.items():
                    speakers, _, location = header.partition(" @ ")
                    persons_in_conversation.append(speakers.split(" -> "))
                    lines = [
                        DialogueLine(speaker=str(c[0]), line=str(c[1]))
                        for c in chat if isinstance(c, (list, tuple)) and len(c) >= 2
                    ]
                    new_feed.append(FeedItem(
                        seq=self._next_seq(),
                        sim_time=step_time,
                        kind=FeedKind.CHAT,
                        location=location,
                        dialogue=lines,
                    ))
            self._conversation_seen.add(step_time)
        elif step_time in conversation:
            # 已見過嘅 key（同一 scan 循環內唔會重複；cross-scan 由 _conversation_seen 擋）
            for chats in conversation[step_time]:
                for header in chats:
                    persons_in_conversation.append(header.partition(" @ ")[0].split(" -> "))

        for agent_name, agent_data in agents.items():
            event = ((agent_data or {}).get("action") or {}).get("event") or {}
            coord = agent_data.get("coord")
            address = event.get("address") or []
            location = _get_location(address) if address else None

            last = self._last_location.get(agent_name)
            if last is None:
                # 首次見到呢個 agent：以當前 coord 做起點（resume 情境）或第 0 帧
                last = {"movement": list(coord) if coord else [0, 0], "location": location or ""}
                self._last_location[agent_name] = last
                if step == 1:
                    # 第 0 帧初始位置（同 compress.py insert_frame0）
                    new_frames.setdefault("0", {})[agent_name] = {
                        "location": location or "",
                        "movement": list(coord) if coord else [0, 0],
                    }

            source_coord = last["movement"]
            if location is None:
                location = last["location"]
                path = [source_coord]
            elif coord:
                try:
                    path = self._maze.find_path(source_coord, coord)
                except Exception:
                    logger.warning("framebuffer: find_path 失敗（%s: %s → %s），直達處理",
                                   agent_name, source_coord, coord)
                    path = [source_coord, coord]
            else:
                path = [source_coord]

            describe = event.get("describe") or ""
            if not describe:
                describe = f"{event.get('predicate', '')}{event.get('object', '')}"

            had_conversation = any(agent_name in p for p in persons_in_conversation)

            for i in range(FRAMES_PER_STEP):
                moving = len(path) > 1
                if len(path) > 0:
                    movement = list(path[0])
                    path = path[1:]
                    self._last_location[agent_name]["movement"] = movement
                    self._last_location[agent_name]["location"] = location
                else:
                    movement = None

                action = ""
                if moving:
                    action = f"前往 {location}"
                elif movement is not None:
                    action = describe
                    if KW_SLEEPING in action:
                        action = "😴 " + action
                    elif had_conversation:
                        action = "💬 " + action

                step_key = "%d" % ((step - 1) * FRAMES_PER_STEP + 1 + i)
                if movement is not None:
                    new_frames.setdefault(step_key, {})[agent_name] = {
                        "location": location,
                        "movement": movement,
                        "action": action,
                    }

            # 事件 feed（每 checkpoint 每 agent 一條）
            if describe:
                new_feed.append(FeedItem(
                    seq=self._next_seq(),
                    sim_time=step_time,
                    kind=FeedKind.EVENT,
                    actor=agent_name,
                    location=location,
                    text=describe,
                ))

    # ---------------------------------------------------------------- 系統訊息

    def add_system_feed(self, text: str, sim_time: str = "") -> FeedItem:
        """「命令已送達」「GM 調整好感」等系統訊息（繁體，由後端出）。"""
        item = FeedItem(
            seq=self._next_seq(),
            sim_time=sim_time,
            kind=FeedKind.SYSTEM,
            text=text,
        )
        self._feed.append(item)
        return item

    def add_narrative_feed(self, text: str, sim_time: str = "") -> FeedItem:
        """即時劇情旁白（StepNarrator 產出）。"""
        item = FeedItem(
            seq=self._next_seq(),
            sim_time=sim_time,
            kind=FeedKind.NARRATIVE,
            text=text,
        )
        self._feed.append(item)
        return item

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
import random
import re

from modules.prompt.keywords import KW_SLEEPING

from .models import DialogueLine, FeedItem, FeedKind

logger = logging.getLogger(__name__)

SIMULATE_FILE_RE = re.compile(r"^simulate-\d{8}-\d{4}\.json$")  # simulate-YYYYMMDD-HHMM.json（start.py 寫檔格式）

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
        self._files_processed: set[str] = set()  # [story-weaver:dedup] 用檔名去重（唔用 step number，因為 checkpoint step 會跨回合重置）
        self._last_step = 0
        self._cumulative_step = 0  # [story-weaver:dedup] 累積 step counter，避免 frame key 碰撞
        import threading
        self._lock = threading.Lock()  # [story-weaver:streaming] 保護 scan/frames_since 並發

    # ---------------------------------------------------------------- 查詢

    def frames_since(self, frame_cursor: int) -> dict[str, dict]:
        with self._lock:
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
        with self._lock:
            return self._scan_unlocked(processed_steps)

    def _scan_unlocked(self, processed_steps: list[int] | None = None) -> dict:
        processed = set(processed_steps or [])
        try:
            files = sorted(
                f for f in os.listdir(self._folder) if SIMULATE_FILE_RE.match(f)
            )
        except OSError:
            return {"new_frames": {}, "new_feed": [], "last_step": self._last_step, "skipped": []}

        new_count = sum(1 for f in files if f not in self._files_processed)
        if new_count > 0:
            logger.info(
                "framebuffer: scan — %d total files, %d new, _cumulative_step=%d",
                len(files), new_count, self._cumulative_step
            )

        conversation = self._load_conversation()
        new_frames: dict[str, dict] = {}
        new_feed: list[FeedItem] = []
        skipped: list[str] = []

        for file_name in files:
            # [story-weaver:dedup] 用檔名去重（唔用 step number——checkpoint step 會跨回合重置）
            if file_name in self._files_processed:
                continue
            path = os.path.join(self._folder, file_name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                step = data["step"]
            except Exception:
                skipped.append(file_name)  # 半截檔（推演中被殺）→ 跳過
                continue
            self._cumulative_step += 1
            self._process_checkpoint(data, conversation, new_frames, new_feed)
            self._files_processed.add(file_name)
            self._last_step = max(self._last_step, step)

        self._frames.update(new_frames)
        self._feed.extend(new_feed)
        return {
            "new_frames": new_frames,
            "new_feed": [f.model_dump() for f in new_feed],
            "last_step": self._last_step,
            "skipped": skipped,
        }

    def seed_idle_frames(self, agent_positions: dict[str, list], num_frames: int = 120) -> None:
        """[story-weaver:idle] 未有 checkpoint 之前，用 spawn 位置生成初始踱步幀，
        等角色未推演都有嘢睇，唔會死企。"""
        if self._frames:
            return  # 已有幀就唔覆蓋
        for i in range(num_frames):
            step_key = str(i + 1)
            frame_data: dict[str, dict] = {}
            for agent_name, coord in agent_positions.items():
                # 生成簡單來回踱步（模擬 _build_wandering_path 效果）
                import math
                offset_x = math.sin(i * 0.3 + hash(agent_name) % 100 * 0.1) * 1.5
                offset_y = math.cos(i * 0.3 + hash(agent_name) % 100 * 0.1) * 1.5
                movement = [
                    float(coord[0]) + offset_x,
                    float(coord[1]) + offset_y,
                ]
                frame_data[agent_name] = {
                    "location": "",
                    "movement": movement,
                    "action": "等待故事開始…",
                }
            self._frames[step_key] = frame_data
        # 加 frame 0
        frame_0: dict[str, dict] = {}
        for agent_name, coord in agent_positions.items():
            frame_0[agent_name] = {
                "location": "",
                "movement": [float(coord[0]), float(coord[1])],
                "action": "",
            }
        self._frames["0"] = frame_0
        # 初始化 _last_location
        for agent_name, coord in agent_positions.items():
            self._last_location[agent_name] = {
                "movement": list(coord),
                "location": "",
            }

    def _build_wandering_path(self, coord: list, steps: int = 8) -> list:
        """[story-weaver:wandering] 為原地活動嘅角色生成室內踱步路徑。
        喺當前 coord 附近搵 non-collision tile 行一個來回 loop。"""
        path = [list(coord)]
        current = list(coord)
        for _ in range(steps):
            try:
                neighbors = self._maze.get_around(current, no_collision=True)
            except Exception:
                neighbors = []
            if not neighbors:
                break
            current = random.choice(neighbors)
            path.append(list(current))
        # 返原位（形成來回踱步感）
        if len(path) > 1:
            path.append(list(coord))
        return path

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
        # [story-weaver:dedup] 用累積 step 計 frame key，避免 checkpoint step 跨回合重置導致碰撞
        cstep = self._cumulative_step
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
                # [story-weaver:pre-move] GM teleport 前位置 → frame 0 用原位，
                # 令 FrameBuffer 可以生成由原位行去見面地點嘅動畫
                pre_move = (data.get("_pre_move_positions") or {}).get(agent_name)
                start_pos = pre_move if pre_move else (list(coord) if coord else [0, 0])
                last = {"movement": start_pos, "location": location or ""}
                self._last_location[agent_name] = last
                if cstep == 1:
                    new_frames.setdefault("0", {})[agent_name] = {
                        "location": location or "",
                        "movement": list(start_pos),
                        "action": "前往見面地點…" if pre_move else "",
                        "currently": "",
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

            # [story-weaver:movement-interp] 將 path 插值均勻分佈到全部幀，
            # 避免角色只喺頭幾幀閃現、之後全程靜止。
            # 原地活動（path_len <= 1）生成室內踱步路徑，令角色持續可見。
            path_points = list(path)  # shallow copy，每個 element 係 [x, y]
            path_len = len(path_points)
            is_wandering = False
            if path_len <= 1:
                # [story-weaver:wandering] 原地活動 → 生成室內踱步
                wandering = self._build_wandering_path(source_coord, steps=20)
                if len(wandering) > 2:
                    path_points = wandering
                    path_len = len(path_points)
                    is_wandering = True
            for i in range(FRAMES_PER_STEP):
                if path_len > 1:
                    # [story-weaver:linear-interp] 線性插值：每幀位置都唔同，
                    # 角色持續平滑移動，唔會因為重複位置俾 adaptive pacing skip 咗
                    progress = i / max(FRAMES_PER_STEP - 1, 1)
                    total_segments = path_len - 1
                    float_idx = progress * total_segments
                    seg_idx = int(float_idx)
                    frac = float_idx - seg_idx
                    if seg_idx >= path_len - 1:
                        movement = [float(path_points[-1][0]), float(path_points[-1][1])]
                        moving = False
                    else:
                        p0 = path_points[seg_idx]
                        p1 = path_points[seg_idx + 1]
                        movement = [
                            p0[0] + frac * (p1[0] - p0[0]),
                            p0[1] + frac * (p1[1] - p0[1]),
                        ]
                        moving = True
                elif path_len == 1:
                    movement = [float(path_points[0][0]), float(path_points[0][1])]
                    moving = False
                else:
                    movement = [float(source_coord[0]), float(source_coord[1])]
                    moving = False

                action = ""
                if moving and not is_wandering:
                    action = f"前往 {location}"
                else:
                    action = describe
                    if KW_SLEEPING in action:
                        action = "😴 " + action
                    elif had_conversation:
                        action = "💬 " + action

                step_key = "%d" % ((cstep - 1) * FRAMES_PER_STEP + 1 + i)
                # [story-weaver:currently-label] 將 currently（內心/戲劇狀態）
                # 傳去 frontend，取代原本淨係 show 物理行動
                currently = (agent_data.get("currently") or "").strip()
                new_frames.setdefault(step_key, {})[agent_name] = {
                    "location": location,
                    "movement": movement,
                    "action": action,
                    "currently": currently,
                }

            # 更新最後已知位置（用 path 終點）
            if path_len > 0:
                self._last_location[agent_name]["movement"] = list(path_points[-1])
                self._last_location[agent_name]["location"] = location

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

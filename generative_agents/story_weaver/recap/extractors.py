"""story_weaver.recap.extractors — 事件/對話/記憶後備提取器（spec §4）。

純消費者：只讀 checkpoints + conversation.json + storage docstore，唔改任何嘢。
對白 lines 位元級複製（唔 strip、唔轉換、唔修標點）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from modules.prompt.keywords import (
    KW_AT_THIS_TIME,
    KW_IDLE,
    KW_PENDING,
    KW_SLEEPING,
)

from .models import DialogueBlock, DialogueLine, TimelineEvent

logger = logging.getLogger(__name__)

SIMULATE_FILE_RE = re.compile(r"^simulate-\d{12}\.json$")

# 瑣碎事件嘅 predicate/object 組合（英文係原版容錯）
_TRIVIAL_PREDICATES = {"is", KW_AT_THIS_TIME}
_TRIVIAL_OBJECTS = {"idle", KW_IDLE, KW_PENDING, "waiting to start"}

POIGNANCY_MIN = 3  # 低於呢個數嘅瑣碎事件唔入時間線


@dataclass
class ExtractionResult:
    events: list[TimelineEvent] = field(default_factory=list)
    sim_time_start: str = ""
    sim_time_end: str = ""
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False  # 有冇因損毀而提前終止


@dataclass
class DialogueResult:
    blocks: list[DialogueBlock] = field(default_factory=list)
    health: str = "ok"  # "ok" | "degraded" | "missing"
    warnings: list[str] = field(default_factory=list)


def _location(address: list) -> str:
    """對齊 compress.py get_location：唔顯示第一級（小鎮名）。"""
    if not address:
        return ""
    return "，".join(address[1:]) if len(address) > 1 else "，".join(address)


def _scan_docstore_poignancy(sim_dir: str) -> dict[str, int]:
    """輕量掃各 agent associate docstore，建「事件文本 → poignancy」map。

    best-effort：檔案唔存在 / 格式唔啱 → 返空 map（上層行 heuristic）。
    唔實例化 LlamaIndex，直接讀 JSON，避免 embedding 模型依賴。
    """
    poignancy_map: dict[str, int] = {}
    storage_root = os.path.join(sim_dir, "storage")
    if not os.path.isdir(storage_root):
        return poignancy_map
    for agent_dir in os.listdir(storage_root):
        docstore_path = os.path.join(storage_root, agent_dir, "associate", "docstore.json")
        if not os.path.exists(docstore_path):
            continue
        try:
            with open(docstore_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            logger.warning("recap extractor: docstore 損毀（%s）", docstore_path)
            continue
        for node in _walk_docstore_nodes(data):
            metadata = node.get("metadata") or {}
            text = node.get("text") or ""
            poignancy = metadata.get("poignancy")
            if text and isinstance(poignancy, int):
                poignancy_map[text] = poignancy
    return poignancy_map


def _walk_docstore_nodes(data):
    """逐層搵出 docstore JSON 入面嘅 node dict（要有 text + metadata）。"""
    if isinstance(data, dict):
        if "text" in data and "metadata" in data:
            yield data
            return
        # LlamaIndex 格式：{"__data__": {...}} 包一層
        inner = data.get("__data__")
        if isinstance(inner, dict) and "text" in inner:
            yield inner
            return
        for v in data.values():
            yield from _walk_docstore_nodes(v)
    elif isinstance(data, list):
        for item in data:
            yield from _walk_docstore_nodes(item)


class EventExtractor:
    """逐個讀本回合 simulate-*.json，輸出去重後嘅事件流（spec §4.1）。"""

    def extract(self, sim_dir: str, step_range: tuple[int, int]) -> ExtractionResult:
        result = ExtractionResult()
        start_step, end_step = step_range
        try:
            files = sorted(
                f for f in os.listdir(sim_dir) if SIMULATE_FILE_RE.match(f)
            )
        except OSError:
            result.warnings.append(f"checkpoints 目錄唔存在：{sim_dir}")
            return result

        poignancy_map = _scan_docstore_poignancy(sim_dir)
        last_state: dict[str, tuple[str, str]] = {}  # agent -> (location, describe)
        corrupt = 0

        for file_name in files:
            path = os.path.join(sim_dir, file_name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not all(k in data for k in ("agents", "time", "step")):
                    raise ValueError("缺頂層鍵")
            except Exception:
                corrupt += 1
                result.warnings.append(f"checkpoint 損毀已剔除：{file_name}")
                continue

            step = data.get("step", 0)
            if step < start_step or step > end_step:
                continue
            sim_time = str(data.get("time", ""))
            if not result.sim_time_start:
                result.sim_time_start = sim_time
            result.sim_time_end = sim_time  # 如實反映最後一個合法 checkpoint

            for agent_name, agent_data in (data.get("agents") or {}).items():
                event = ((agent_data or {}).get("action") or {}).get("event") or {}
                if not event:
                    continue
                describe = event.get("describe") or ""
                predicate = event.get("predicate") or ""
                obj = event.get("object") or ""
                if not describe:
                    describe = f"{predicate} {obj}".strip()
                location = _location(event.get("address") or [])

                # 瑣碎過濾
                if predicate in _TRIVIAL_PREDICATES and obj in _TRIVIAL_OBJECTS:
                    continue
                # heuristic（spec §4.1）：瑣碎類 = 1（會被過濾），其餘 = 5
                if describe in poignancy_map:
                    poignancy = poignancy_map[describe]
                elif KW_SLEEPING in describe or KW_IDLE in describe or obj == KW_SLEEPING:
                    poignancy = 1
                else:
                    poignancy = 5
                if poignancy < POIGNANCY_MIN:
                    continue

                # 去重：相鄰 checkpoint 同 agent 同 (location, describe) → 合併
                if last_state.get(agent_name) == (location, describe):
                    continue
                last_state[agent_name] = (location, describe)

                result.events.append(
                    TimelineEvent(
                        sim_time=sim_time,
                        agent=agent_name,
                        type="action",
                        location=location,
                        describe=describe,
                        poignancy=poignancy,
                        step=step,
                    )
                )

        if corrupt:
            result.truncated = True
        result.events.sort(key=lambda e: (e.sim_time, e.step))
        return result


class DialogueExtractor:
    """解析 conversation.json 三層結構，逐句對白位元級保留（spec §4.2）。"""

    def extract(self, sim_dir: str, sim_time_start: str, sim_time_end: str) -> DialogueResult:
        result = DialogueResult()
        path = os.path.join(sim_dir, "conversation.json")
        conversation = None
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    conversation = json.load(f)
            except Exception:
                logger.warning("recap extractor: conversation.json 損毀（%s）", path)

        if conversation is None:
            # 降級：由記憶摘要重建
            result.warnings.append("conversation.json 損毀或缺失，對話改由角色回憶重建")
            agents = _list_storage_agents(sim_dir)
            blocks = MemoryFallbackExtractor().scan_chat_concepts(
                sim_dir, agents, sim_time_start, sim_time_end
            )
            if blocks:
                result.blocks = blocks
                result.health = "degraded"
            else:
                result.health = "missing"
            return result

        for time_key in sorted(conversation.keys()):
            if sim_time_start and time_key < sim_time_start:
                continue
            if sim_time_end and time_key > sim_time_end:
                continue
            groups = conversation[time_key]
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                for header, chats in group.items():
                    participants, location = _parse_header(header)
                    lines = [
                        DialogueLine(speaker=str(item[0]), text=str(item[1]))
                        for item in chats
                        if isinstance(item, (list, tuple)) and len(item) >= 2
                    ]
                    result.blocks.append(
                        DialogueBlock(
                            sim_time=time_key,
                            participants=participants,
                            location=location,
                            lines=lines,
                        )
                    )
        result.health = "ok"
        return result


def _parse_header(header: str) -> tuple[list[str], str]:
    """"阿珍 -> 阿強 @ 地址1，地址2" → (["阿珍", "阿強"], "地址1，地址2")。"""
    speakers, _, location = header.partition(" @ ")
    participants = [s.strip() for s in speakers.split(" -> ") if s.strip()]
    return participants, location


def _list_storage_agents(sim_dir: str) -> list[str]:
    storage_root = os.path.join(sim_dir, "storage")
    if not os.path.isdir(storage_root):
        return []
    return [d for d in os.listdir(storage_root) if not d.startswith(".")]


class MemoryFallbackExtractor:
    """conversation.json 損毀時，由 associate docstore 嘅 chat concept 重建（spec §4.3）。

    輸出全部 degraded=True —— 嗰啲係 LLM 摘要，唔係原文。
    """

    def scan_chat_concepts(
        self, sim_dir: str, agents: list[str], sim_time_start: str, sim_time_end: str
    ) -> list[DialogueBlock]:
        blocks: list[DialogueBlock] = []
        for agent in agents:
            docstore_path = os.path.join(sim_dir, "storage", agent, "associate", "docstore.json")
            if not os.path.exists(docstore_path):
                continue
            try:
                with open(docstore_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            for node in _walk_docstore_nodes(data):
                metadata = node.get("metadata") or {}
                if metadata.get("node_type") != "chat":
                    continue
                create = str(metadata.get("create", ""))[:13]  # "%Y%m%d-%H:%M"
                if sim_time_start and create < sim_time_start:
                    continue
                if sim_time_end and create > sim_time_end:
                    continue
                subject = metadata.get("subject", agent)
                obj = metadata.get("object", "")
                address = str(metadata.get("address", "")).replace(":", "，")
                blocks.append(
                    DialogueBlock(
                        sim_time=create,
                        participants=[p for p in (subject, obj) if p],
                        location=address,
                        lines=[DialogueLine(speaker="（回憶）", text=node.get("text") or "")],
                        degraded=True,
                    )
                )
        blocks.sort(key=lambda b: b.sim_time)
        return blocks

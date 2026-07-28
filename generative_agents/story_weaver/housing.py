"""story_weaver.housing — 住所登記表（Housing Registry）.

由 maze.json 掃出全部含「床」game_object 嘅房間（sector + arena）。
只有入冊嘅房間先俾玩家揀做住所，保證 spatial.py 嘅「睡覺」地址
派生（living_area + ["床"]）一定搵到 tile。

Registry 係進程內狀態，每次 build 開始時 release_all() 重置；
落盤嘅事實係各 agent.json 嘅 spatial.address.living_area。
"""

import json
from dataclasses import dataclass


@dataclass
class Room:
    world: str
    sector: str
    arena: str
    bed_tiles: int  # 該房「床」tile 數（≥1 先入冊）
    occupied_by: str | None = None  # display_name

    @property
    def address(self) -> list[str]:
        return [self.world, self.sector, self.arena]

    @property
    def label(self) -> str:
        return f"{self.sector} · {self.arena}"


class HousingConflict(Exception):
    def __init__(self, message: str, available: list[Room]) -> None:
        super().__init__(message)
        self.available = available


class HousingRegistry:
    def __init__(self, maze_path: str) -> None:
        self.maze_path = maze_path
        self._rooms: dict[tuple[str, str, str], Room] = {}
        self._scan()

    def _scan(self) -> None:
        with open(self.maze_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        world = data.get("world", "the Ville")
        for tile in data.get("tiles", []):
            address = tile.get("address") or []
            # tile address 格式：[sector, arena, game_object]（無 world）
            if len(address) == 3 and address[-1] == "床":
                key = (world, address[0], address[1])
                if key not in self._rooms:
                    self._rooms[key] = Room(world, address[0], address[1], 0)
                self._rooms[key].bed_tiles += 1

    def rooms(self) -> list[Room]:
        return list(self._rooms.values())

    def available(self) -> list[Room]:
        return [r for r in self._rooms.values() if r.occupied_by is None]

    def is_valid_home(self, address: list[str]) -> bool:
        return tuple(address) in self._rooms

    def assign(self, address: list[str], display_name: str) -> None:
        key = tuple(address)
        if key not in self._rooms:
            raise ValueError(f"住所「{' · '.join(address[1:])}」唔存在或者冇床，唔住得人")
        room = self._rooms[key]
        if room.occupied_by is not None:
            remaining = "、".join(r.label for r in self.available()) or "（冇剩餘空房）"
            raise HousingConflict(
                f"「{room.label}」已經俾「{room.occupied_by}」住咗。剩餘空房：{remaining}",
                self.available(),
            )
        room.occupied_by = display_name

    def release_all(self) -> None:
        for room in self._rooms.values():
            room.occupied_by = None

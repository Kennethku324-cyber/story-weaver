"""story_weaver.housing — 住所登記表（Housing Registry）.

由 maze.json 掃出全部含「床」game_object 嘅房間（sector + arena）。
只有入冊嘅房間先俾玩家揀做住所，保證 spatial.py 嘅「睡覺」地址
派生（living_area + ["床"]）一定搵到 tile。

Registry 係進程內狀態，每次 build 開始時 release_all() 重置；
落盤嘅事實係各 agent.json 嘅 spatial.address.living_area。
"""

import json
from dataclasses import dataclass, field


@dataclass
class Room:
    world: str
    sector: str
    arena: str
    bed_tiles: int  # 該房「床」tile 數（≥1 先入冊）
    occupants: list[str] = field(default_factory=list)  # display_name 列表

    @property
    def address(self) -> list[str]:
        return [self.world, self.sector, self.arena]

    @property
    def label(self) -> str:
        return f"{self.sector} · {self.arena}"

    @property
    def capacity(self) -> int:
        """可住人數。原版遊戲入面夫婦係同房嘅（梅+約翰、湯姆+簡、山姆+詹妮弗），
        所以下限 2；床多嘅房可以再住多啲。"""
        return max(2, self.bed_tiles)

    @property
    def is_full(self) -> bool:
        return len(self.occupants) >= self.capacity


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
        return [r for r in self._rooms.values() if not r.is_full]

    def is_valid_home(self, address: list[str]) -> bool:
        return tuple(address) in self._rooms

    def capacity_of(self, address: list[str]) -> int:
        room = self._rooms.get(tuple(address))
        return room.capacity if room else 0

    def assign(self, address: list[str], display_name: str) -> None:
        key = tuple(address)
        if key not in self._rooms:
            raise ValueError(f"住所「{' · '.join(address[1:])}」唔存在或者冇床，唔住得人")
        room = self._rooms[key]
        if display_name in room.occupants:
            return
        if room.is_full:
            remaining = "、".join(r.label for r in self.available()) or "（冇剩餘空房）"
            raise HousingConflict(
                f"「{room.label}」已經住滿（{room.capacity} 人：{'、'.join(room.occupants)}）。"
                f"剩餘空房：{remaining}",
                self.available(),
            )
        room.occupants.append(display_name)

    def release_all(self) -> None:
        for room in self._rooms.values():
            room.occupants = []

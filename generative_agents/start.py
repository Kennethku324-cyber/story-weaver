import os
import copy
import json
import time
import logging
import argparse
import datetime
from concurrent.futures import ThreadPoolExecutor

sim_timing_logger = logging.getLogger("sim_timing")

try:
    from dotenv import load_dotenv, find_dotenv
except ImportError:
    load_dotenv = None  # type: ignore
    find_dotenv = None  # type: ignore

from modules.game import create_game, get_game
from modules import utils
from modules.model.text_normalize import contains_simplified


def _contains_simplified_text(obj):
    """遞歸掃描 JSON 結構（dict/list/str），命中簡體黑名單字即返回 True。"""
    if isinstance(obj, str):
        return contains_simplified(obj)
    if isinstance(obj, list):
        return any(_contains_simplified_text(i) for i in obj)
    if isinstance(obj, dict):
        return any(
            _contains_simplified_text(k) or _contains_simplified_text(v)
            for k, v in obj.items()
        )
    return False

personas = [
    "阿伊莎", "克勞斯", "瑪麗亞", "沃爾夫岡",  # 学生
    "梅", "約翰", "埃迪",  # 家庭：教授、药店主人、学生
    "簡", "湯姆",  # 家庭：家庭主妇、市场主人
    "卡門", "塔瑪拉",  # 室友：供应店主人、儿童读物作家
    "亞瑟", "伊莎貝拉",  # 酒吧老板、咖啡馆老板
    "山姆", "詹妮弗",  # 家庭：退役军官、水彩画家
    "弗朗西斯科", "海莉", "拉吉夫", "拉託亞",  # 共居空间：喜剧演员、作家、画家、摄影师
    "阿比蓋爾", "卡洛斯", "喬治", "瑞恩", "山本百合子", "亞當",  # 动画师、诗人、数学家、软件工程师、税务律师、哲学家
]


class SimulateServer:
    def __init__(self, name, static_root, checkpoints_folder, config, start_step=0, verbose="info", log_file=""):
        self.name = name
        self.static_root = static_root
        self.checkpoints_folder = checkpoints_folder

        # 历史存档数据（用于断点恢复）
        self.config = config

        os.makedirs(checkpoints_folder, exist_ok=True)

        # 载入历史对话数据（用于断点恢复）
        self.conversation_log = f"{checkpoints_folder}/conversation.json"
        if os.path.exists(self.conversation_log):
            try:
                with open(self.conversation_log, "r", encoding="utf-8") as f:
                    conversation = json.load(f)
            except Exception:
                print(f"WARNING: conversation.json 損毀（{checkpoints_folder}），以空對話開始")
                conversation = {}
        else:
            conversation = {}

        if len(log_file) > 0:
            self.logger = utils.create_file_logger(f"{checkpoints_folder}/{log_file}", verbose)
        else:
            self.logger = utils.create_io_logger(verbose)

        # checkpoint 簡體偵測：舊簡體 checkpoint 唔做靜默轉換（PRD 邊界 3），只 warning
        if _contains_simplified_text(conversation) or _contains_simplified_text(config):
            self.logger.warning(
                "偵測到簡體 checkpoint，建議運行 python scripts/localization/migrate_checkpoint.py "
                + str(checkpoints_folder)
            )

        # 创建游戏
        game = create_game(name, static_root, config, conversation, logger=self.logger)
        game.reset_game()

        self.game = get_game()
        self.tile_size = self.game.maze.tile_size
        self.agent_status = {}
        if "agent_base" in config:
            agent_base = config["agent_base"]
        else:
            agent_base = {}
        for agent_name, agent in config["agents"].items():
            agent_config = copy.deepcopy(agent_base)
            agent_config.update(self.load_static(agent["config_path"]))
            self.agent_status[agent_name] = {
                "coord": agent_config["coord"],
                "path": [],
            }
        self.think_interval = max(
            a.think_config["interval"] for a in self.game.agents.values()
        )
        self.start_step = start_step
        # [story-weaver:affinity] GM 回合尾掛鉤（由 GM 系統注入）；預設 6 step 一回合
        self.gm_hook = None  # [story-weaver:affinity]
        self.steps_per_round = max(1, config.get("steps_per_round", 6))  # [story-weaver:affinity]

    def set_agent_destination(self, name: str, destination: list) -> bool:
        """[story-weaver:gm-walk] 設定 agent 要行去嘅目的地（唔 teleport）。
        會用 maze pathfinding 搵路徑，等 simulate() 時 agent 真係行過去。
        返 True 如果成功 set 咗路徑。"""
        agent = self.game.agents.get(name)
        if agent is None or name not in self.agent_status:
            return False
        current = list(agent.coord) if agent.coord else self.agent_status[name]["coord"]
        path = self.game.maze.find_path(current, destination)
        if path and len(path) > 1:
            self.agent_status[name]["coord"] = list(destination)
            self.agent_status[name]["path"] = path[1:]  # 唔包 starting position
            if name in self.config.get("agents", {}):
                self.config["agents"][name]["coord"] = list(destination)
            self.logger.info(
                "gm-walk: %s 行去 [%s,%s] — %d steps, from [%s,%s]",
                name, destination[0], destination[1], len(path),
                current[0], current[1],
            )
            return True
        # 冇路徑（可能同一位置）→ fallback
        self.agent_status[name]["coord"] = list(destination)
        self.agent_status[name]["path"] = []
        if name in self.config.get("agents", {}):
            self.config["agents"][name]["coord"] = list(destination)
        self.logger.info(
            "gm-walk: %s → [%s,%s] (no path, %d steps)",
            name, destination[0], destination[1], len(path) if path else 0,
        )
        return False

    def sync_agent_positions(self) -> None:
        """[story-weaver:gm-move] 將 game agents 嘅當前位置同步到 agent_status，
        GM teleport 之後 call 呢個，確保 simulate() 用新位置而唔係 config 預設。"""
        for name, agent in self.game.agents.items():
            if name in self.agent_status:
                self.agent_status[name]["coord"] = list(agent.coord)
                self.agent_status[name]["path"] = []
                if name in self.config.get("agents", {}):
                    self.config["agents"][name]["coord"] = list(agent.coord)

    def simulate(self, step, stride=0):
        timer = utils.get_timer()
        for i in range(self.start_step, self.start_step + step):
            step_start = time.perf_counter()
            title = "Simulate Step[{}/{}, time: {}]".format(i+1, self.start_step + step, timer.get_date())
            self.logger.info("\n" + utils.split_line(title, "="))

            # [story-weaver:parallel] 並行 think + 串行 apply：
            # think 階段係 LLM I/O bound，用 thread 並行（對話由 _CHAT_LOCK 互斥）；
            # apply 階段（config / status / checkpoint）維持串行，順序同原本一致。
            # 語義差異：agent 唔再即時見到同一步較早 agent 嘅新事件，要下一步先見到。
            def _think(name, status):
                think_start = time.perf_counter()
                plan = self.game.agent_think(name, status)["plan"]
                sim_timing_logger.info(
                    "agent_think step=%d agent=%s duration=%.2fs",
                    i + 1, name, time.perf_counter() - think_start,
                )
                return plan

            with ThreadPoolExecutor(max_workers=max(1, len(self.agent_status))) as pool:
                futures = {
                    name: pool.submit(_think, name, status)
                    for name, status in self.agent_status.items()
                }
                for name, status in self.agent_status.items():
                    plan = futures[name].result()
                    agent = self.game.get_agent(name)
                    if name not in self.config["agents"]:
                        self.config["agents"][name] = {}
                    self.config["agents"][name].update(agent.to_dict())
                    if plan.get("path"):
                        status["coord"], status["path"] = plan["path"][-1], []
                    self.config["agents"][name].update(
                        # {"coord": status["coord"], "path": plan["path"]}
                        {"coord": status["coord"]}
                    )

            sim_time = timer.get_date("%Y%m%d-%H:%M")
            self.config.update(
                {
                    "time": sim_time,
                    "step": i + 1,
                }
            )
            # 保存Agent活动数据
            with open(f"{self.checkpoints_folder}/simulate-{sim_time.replace(':', '')}.json", "w", encoding="utf-8") as f:
                f.write(json.dumps(self.config, indent=2, ensure_ascii=False))
            # 保存对话数据
            with open(f"{self.checkpoints_folder}/conversation.json", "w", encoding="utf-8") as f:
                f.write(json.dumps(self.game.conversation, indent=2, ensure_ascii=False))

            # [story-weaver:affinity] 回合尾掛鉤：GM 系統經 apply_gm_response 調整好感度；
            # 本系統保證 apply_gm_response 任何異常都唔會 throw 上嚟（內部 try/except + failsafe）
            if self.gm_hook is not None and (i + 1) % self.steps_per_round == 0:  # [story-weaver:affinity]
                self.gm_hook(self.game, i + 1)  # [story-weaver:affinity]

            if stride > 0:
                timer.forward(stride)
            sim_timing_logger.info(
                "step_done step=%d duration=%.2fs",
                i + 1, time.perf_counter() - step_start,
            )

    def load_static(self, path):
        return utils.load_dict(os.path.join(self.static_root, path))


# 从存档数据中载入配置，用于断点恢复
def get_config_from_log(checkpoints_folder):
    files = sorted(os.listdir(checkpoints_folder))

    json_files = list()
    for file_name in files:
        if file_name.startswith("simulate-") and file_name.endswith(".json"):
            json_files.append(os.path.join(checkpoints_folder, file_name))

    if len(json_files) < 1:
        return None

    with open(json_files[-1], "r", encoding="utf-8") as f:
        config = json.load(f)

    assets_root = os.path.join("assets", "village")

    start_time = datetime.datetime.strptime(config["time"], "%Y%m%d-%H:%M")
    start_time += datetime.timedelta(minutes=config["stride"])
    config["time"] = {"start": start_time.strftime("%Y%m%d-%H:%M")}
    agents = config["agents"]
    for a in agents:
        config["agents"][a]["config_path"] = os.path.join(assets_root, "agents", a.replace(" ", "_"), "agent.json")

    return config


# 为新游戏创建配置
def get_config(start_time="20240213-09:30", stride=15, agents=None,
               affinity=None):  # [story-weaver:affinity]
    with open("data/config.json", "r", encoding="utf-8") as f:
        json_data = json.load(f)
        agent_config = json_data["agent"]

    assets_root = os.path.join("assets", "village")
    config = {
        "stride": stride,
        "time": {"start": start_time},
        "maze": {"path": os.path.join(assets_root, "maze.json")},
        "agent_base": agent_config,
        "agents": {},
        "affinity": affinity or {},  # [story-weaver:affinity] Setup 校驗後嘅矩陣
        "affinity_rounds": [],  # [story-weaver:affinity] 變動歷史
        "affinity_meta": {},  # [story-weaver:affinity] 冧等標記等
    }
    for a in agents:
        config["agents"][a] = {
            "config_path": os.path.join(
                assets_root, "agents", a.replace(" ", "_"), "agent.json"
            ),
        }
    return config


if load_dotenv and find_dotenv:
    load_dotenv(find_dotenv())

parser = argparse.ArgumentParser(description="console for village")
parser.add_argument("--name", type=str, default="", help="The simulation name")
parser.add_argument("--start", type=str, default="20240213-09:30", help="The starting time of the simulated ville")
parser.add_argument("--resume", action="store_true", help="Resume running the simulation")
parser.add_argument("--step", type=int, default=10, help="The simulate step")
parser.add_argument("--stride", type=int, default=10, help="The step stride in minute")
parser.add_argument("--verbose", type=str, default="debug", help="The verbose level")
parser.add_argument("--log", type=str, default="", help="Name of the log file")
args = parser.parse_args() if __name__ == "__main__" else parser.parse_args([])


if __name__ == "__main__":
    checkpoints_path = "results/checkpoints"

    name = args.name
    if len(name) < 1:
        name = input("Please enter a simulation name (e.g. sim-test): ")

    resume = args.resume
    if resume:
        while not os.path.exists(f"{checkpoints_path}/{name}"):
            name = input(f"'{name}' doesn't exists, please re-enter the simulation name: ")
    else:
        while os.path.exists(f"{checkpoints_path}/{name}"):
            name = input(f"The name '{name}' already exists, please enter a new name: ")

    checkpoints_folder = f"{checkpoints_path}/{name}"

    start_time = args.start
    if resume:
        sim_config = get_config_from_log(checkpoints_folder)
        if sim_config is None:
            print("No checkpoint file found to resume running.")
            exit(0)
        start_step = sim_config["step"]
    else:
        sim_config = get_config(start_time, args.stride, personas)
        start_step = 0

    static_root = "frontend/static"

    server = SimulateServer(name, static_root, checkpoints_folder, sim_config, start_step, args.verbose, args.log)
    server.simulate(args.step, args.stride)

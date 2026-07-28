import random
import datetime
import re
from string import Template
from pydantic import BaseModel, Field
from collections import namedtuple
from typing import List, Tuple
from modules import utils
from modules.memory import Event
from .keywords import KW_AT_THIS_TIME, KW_IDLE, KW_SLEEPING, KW_TRUE_TOKENS


Result = namedtuple("Result", ["prompt", "callback", "failsafe", "return_type"])

class Scratch:
    def __init__(self, name, currently, config):
        self.name = name
        self.currently = currently
        self.config = config
        self.template_path = "data/prompts"

    def build_prompt(self, template, data):
        with open(f"{self.template_path}/{template}.txt", "r", encoding="utf-8") as file:
            file_content = file.read()

        template = Template(file_content)
        filled_content = template.substitute(data)

        return filled_content

    def _base_desc(self):
        return self.build_prompt(
            "base_desc",
            {
                "name": self.name,
                "age": self.config["age"],
                "innate": self.config["innate"],
                "learned": self.config["learned"],
                "lifestyle": self.config["lifestyle"],
                "daily_plan": self.config["daily_plan"],
                "date": utils.get_timer().daily_format_cn(),
                "currently": self.currently,
            }
        )

    # [story-weaver:affinity] 結構化好感度一句描述；Game 未建立或任何異常返回 ""
    def _relation_line(self, a_name: str, b_name: str) -> str:  # [story-weaver:affinity]
        try:  # [story-weaver:affinity]
            from modules.game import get_game  # [story-weaver:affinity] lazy import，避免 circular import
            game = get_game()  # [story-weaver:affinity]
            if game is not None and getattr(game, "affinity", None) is not None:  # [story-weaver:affinity]
                return game.affinity.relation_line(a_name, b_name)  # [story-weaver:affinity]
        except Exception:  # [story-weaver:affinity]
            pass  # [story-weaver:affinity]
        return ""  # [story-weaver:affinity]

    def prompt_poignancy_event(self, event):
        prompt = self.build_prompt(
            "poignancy_event",
            {
                "base_desc": self._base_desc(),
                "agent": self.name,
                "event": event.get_describe(),
            }
        )

        class PoignancyEventResponse(BaseModel):
            res: int = Field(description="事件的情感強度評分，整數，範圍1到10")

        return Result(prompt, None, random.choice(list(range(10))) + 1, PoignancyEventResponse)

    def prompt_poignancy_chat(self, event):
        prompt = self.build_prompt(
            "poignancy_chat",
            {
                "base_desc": self._base_desc(),
                "agent": self.name,
                "event": event.get_describe(),
            }
        )

        class PoignancyChatResponse(BaseModel):
            res: int = Field(description="對話的情感強度評分，整數，範圍1到10")

        return Result(prompt, None, random.choice(list(range(10))) + 1, PoignancyChatResponse)

    def prompt_wake_up(self):
        prompt = self.build_prompt(
            "wake_up",
            {
                "base_desc": self._base_desc(),
                "lifestyle": self.config["lifestyle"],
                "agent": self.name,
            }
        )

        class wakeupResponse(BaseModel):
            res: int = Field(description="起床時間，24小時制的小時數，整數，範圍0到11")

        def _callback(response):
            value = response
            if value > 11:
                value = 11
            return value

        return Result(prompt, _callback, 8, wakeupResponse)

    def prompt_schedule_init(self, wake_up):
        prompt = self.build_prompt(
            "schedule_init",
            {
                "base_desc": self._base_desc(),
                "lifestyle": self.config["lifestyle"],
                "agent": self.name,
                "wake_up": wake_up,
            }
        )

        class schedule_initResponse(BaseModel):
            res: list[str] = Field(description="按時間順序排列的日程活動列表，每項為簡短的活動描述")

        def _callback(response):
            assert len(response) >= 3, "schedule_init: too few items"
            return response

        failsafe = [
            "早上6點起床並完成早餐的例行工作",
            "早上7點吃早餐",
            "早上8點看書",
            "中午12點吃午飯",
            "下午1點小睡片刻",
            "晚上7點放鬆一下，看電視",
            "晚上11點睡覺",
        ]
        return Result(prompt, _callback, failsafe, schedule_initResponse)

    def prompt_schedule_daily(self, wake_up, daily_schedule):
        hourly_schedule = ""
        for i in range(wake_up):
            hourly_schedule += f"[{i}:00] {KW_SLEEPING}\n"
        for i in range(wake_up, 24):
            hourly_schedule += f"[{i}:00] <活動>\n"

        prompt = self.build_prompt(
            "schedule_daily",
            {
                "base_desc": self._base_desc(),
                "agent": self.name,
                "daily_schedule": "；".join(daily_schedule),
                "hourly_schedule": hourly_schedule,
            }
        )

        class schedule_dailyResponse(BaseModel):
            res: dict[str, str] = Field(description="24小時日程表，鍵為時間字符串如'8:00'，值為該時段的活動描述")

        failsafe = {
            "6:00": "起床並完成早晨的例行工作",
            "7:00": "吃早餐",
            "8:00": "讀書",
            "9:00": "讀書",
            "10:00": "讀書",
            "11:00": "讀書",
            "12:00": "吃午飯",
            "13:00": "小睡片刻",
            "14:00": "小睡片刻",
            "15:00": "小睡片刻",
            "16:00": "繼續工作",
            "17:00": "繼續工作",
            "18:00": "回家",
            "19:00": "放鬆，看電視",
            "20:00": "放鬆，看電視",
            "21:00": "睡前看書",
            "22:00": "準備睡覺",
            "23:00": "睡覺",
        }

        def _callback(response):
            assert len(response) >= 5, "less than 5 schedules"
            return response

        return Result(prompt, _callback, failsafe, schedule_dailyResponse)

    def prompt_schedule_decompose(self, plan, schedule):
        def _plan_des(plan):
            start, end = schedule.plan_stamps(plan, time_format="%H:%M")
            return f'{start} 至 {end}，{self.name} 計劃 {plan["describe"]}'

        indices = range(
            max(plan["idx"] - 1, 0), min(plan["idx"] + 2, len(schedule.daily_schedule))
        )

        start, end = schedule.plan_stamps(plan, time_format="%H:%M")
        increment = max(int(plan["duration"] / 100) * 5, 5)

        prompt = self.build_prompt(
            "schedule_decompose",
            {
                "base_desc": self._base_desc(),
                "agent": self.name,
                "plan": "；".join([_plan_des(schedule.daily_schedule[i]) for i in indices]),
                "increment": increment,
                "start": start,
                "end": end,
            }
        )

        class schedule_decomposeResponse(BaseModel):
            res: List[Tuple[str, int]] = Field(description="子任務列表，每項為 [活動描述, 時長分鐘數] 的元組")

        def _callback(response):
            left = plan["duration"] - sum([s[1] for s in response])
            if left > 0:
                response.append((plan["describe"], left))
            return response

        failsafe = [(plan["describe"], 10) for _ in range(int(plan["duration"] / 10))]
        return Result(prompt, _callback, failsafe, schedule_decomposeResponse)

    def prompt_schedule_revise(self, action, schedule):
        plan, _ = schedule.current_plan()
        start, end = schedule.plan_stamps(plan, time_format="%H:%M")
        act_start_minutes = utils.daily_duration(action.start)
        original_plan, new_plan = [], []

        def _plan_des(start, end, describe):
            if not isinstance(start, str):
                start = start.strftime("%H:%M")
            if not isinstance(end, str):
                end = end.strftime("%H:%M")
            return "[{} 至 {}] {}".format(start, end, describe)

        for de_plan in plan["decompose"]:
            de_start, de_end = schedule.plan_stamps(de_plan, time_format="%H:%M")
            original_plan.append(_plan_des(de_start, de_end, de_plan["describe"]))
            if de_plan["start"] + de_plan["duration"] <= act_start_minutes:
                new_plan.append(_plan_des(de_start, de_end, de_plan["describe"]))
            elif de_plan["start"] <= act_start_minutes:
                new_plan.extend(
                    [
                        _plan_des(de_start, action.start, de_plan["describe"]),
                        _plan_des(
                            action.start, action.end, action.event.get_describe(False)
                        ),
                    ]
                )

        original_plan, new_plan = "\n".join(original_plan), "\n".join(new_plan)

        prompt = self.build_prompt(
            "schedule_revise",
            {
                "agent": self.name,
                "start": start,
                "end": end,
                "original_plan": original_plan,
                "duration": action.duration,
                "event": action.event.get_describe(),
                "new_plan": new_plan,
            }
        )

        class schedule_reviseResponse(BaseModel):
            res: List[Tuple[str, str, str]] = Field(description="調整後的完整日程列表，每項為 [開始時間, 結束時間, 活動描述] 的元組，時間格式為 HH:MM")

        def _callback(response):  
            # response已经是List[Tuple[str, str, str]]类型  
            # 格式: [(开始时间, 结束时间, 描述), ...]  
            decompose = []  
            for start, end, describe in response:  
                m_start = utils.daily_duration(utils.to_date(start, "%H:%M"))  
                m_end = utils.daily_duration(utils.to_date(end, "%H:%M"))  
                decompose.append(  
                    {  
                        "idx": len(decompose),  
                        "describe": describe,  
                        "start": m_start,  
                        "duration": m_end - m_start,  
                    }  
                )  
            return decompose

        return Result(prompt, _callback, plan["decompose"], schedule_reviseResponse)

    def prompt_determine_sector(self, describes, spatial, address, tile):
        live_address = spatial.find_address("living_area", as_list=True)[:-1]
        curr_address = tile.get_address("sector", as_list=True)

        prompt = self.build_prompt(
            "determine_sector",
            {
                "agent": self.name,
                "live_sector": live_address[-1],
                "live_arenas": ", ".join(i for i in spatial.get_leaves(live_address)),
                "current_sector": curr_address[-1],
                "current_arenas": ", ".join(i for i in spatial.get_leaves(curr_address)),
                "daily_plan": self.config["daily_plan"],
                "areas": ", ".join(i for i in spatial.get_leaves(address)),
                "complete_plan": describes[0],
                "decomposed_plan": describes[1],
            }
        )

        sectors = spatial.get_leaves(address)
        arenas = {}
        for sec in sectors:
            arenas.update(
                {a: sec for a in spatial.get_leaves(address + [sec]) if a not in arenas}
            )
        failsafe = random.choice(sectors)

        class determine_sectorResponse(BaseModel):
            res: str = Field(description="從給定列表中選出的目標區域名稱，必須與列表中的某項完全一致")

        def _callback(response):  
            # response已经是str类型  
            # 验证sector是否在有效列表中，或进行映射  
            if response in sectors:  
                return response  
            if response in arenas:  
                return arenas[response]  
            for s in sectors:  
                if response.startswith(s):  
                    return s  
            return failsafe
        return Result(prompt, _callback, failsafe, determine_sectorResponse)

    def prompt_determine_arena(self, describes, spatial, address):
        prompt = self.build_prompt(
            "determine_arena",
            {
                "agent": self.name,
                "target_sector": address[-1],
                "target_arenas": ", ".join(i for i in spatial.get_leaves(address)),
                "daily_plan": self.config["daily_plan"],
                "complete_plan": describes[0],
                "decomposed_plan": describes[1],
            }
        )

        arenas = spatial.get_leaves(address)
        failsafe = random.choice(arenas)

        class determine_arenaResponse(BaseModel):
            res: str = Field(description="從給定列表中選出的目標場所名稱，必須與列表中的某項完全一致")

        def _callback(response):
            return response if response in arenas else failsafe

        return Result(prompt, _callback, failsafe, determine_arenaResponse)

    def prompt_determine_object(self, describes, spatial, address):
        objects = spatial.get_leaves(address)

        prompt = self.build_prompt(
            "determine_object",
            {
                "activity": describes[1],
                "objects": ", ".join(objects),
            }
        )

        failsafe = random.choice(objects)

        class determine_objectResponse(BaseModel):
            res: str = Field(description="從給定列表中選出的最相關對象名稱，必須與列表中的某項完全一致")
        def _callback(response):
            # pattern = ["The most relevant object from the Objects is: <(.+?)>", "<(.+?)>"]
            return response if response in objects else failsafe

        return Result(prompt, _callback, failsafe, determine_objectResponse)

    def prompt_describe_event(self, subject, describe, address, emoji=None):
        prompt = self.build_prompt(
            "describe_event",
            {
                "action": describe,
            }
        )

        e_describe = describe.replace("(", "").replace(")", "").replace("<", "").replace(">", "")
        if e_describe.startswith(subject + KW_AT_THIS_TIME):
            e_describe = e_describe.replace(subject + KW_AT_THIS_TIME, "")
        failsafe = Event(
            subject, KW_AT_THIS_TIME, e_describe, describe=describe, address=address, emoji=emoji
        )
        class describe_eventResponse(BaseModel):
            res: List[Tuple[str, str, str]] = Field(description="動作的三元組列表，每項為 [主語, 謂語, 賓語]")

        def _callback(response):  
            # response已经是List[Tuple[str, str, str]]类型  
            # 格式: [(主语, 谓语, 宾语), ...]  
            for subject, predicate, obj in response:  
                # 验证三元组不为空  
                if subject and predicate and obj:  
                    return Event(subject, predicate, obj, describe=describe, address=address, emoji=emoji)  
            return None
        return Result(prompt, _callback, failsafe, describe_eventResponse)

    def prompt_describe_object(self, obj, describe):
        prompt = self.build_prompt(
            "describe_object",
            {
                "object": obj,
                "agent": self.name,
                "action": describe,
            }
        )

        class DescribeObjectResponse(BaseModel):
            res: str = Field(description="物品的狀態描述，不超過10個字的短句，不要包含物品名稱")

        def _callback(response):
            response = response.strip()
            # 移除 "物品名" 前缀
            if response.startswith(obj):
                response = response[len(obj):].strip()
            return response or failsafe

        failsafe = KW_IDLE
        return Result(prompt, _callback, failsafe, DescribeObjectResponse)

    def prompt_decide_chat(self, agent, other, focus, chats):
        def _status_des(a):
            event = a.get_event()
            if a.path:
                return f"{a.name} 正去往 {event.get_describe(False)}"
            return event.get_describe()

        context = "。".join(
            [c.describe for c in focus["events"]]
        )
        context += "\n" + "。".join([c.describe for c in focus["thoughts"]]) 
        date_str = utils.get_timer().get_date("%Y-%m-%d %H:%M:%S")
        chat_history = ""
        if chats:
            chat_history = f" {agent.name} 和 {other.name} 上次在 {chats[0].create} 聊過關於 {chats[0].describe} 的話題"
        a_des, o_des = _status_des(agent), _status_des(other)

        prompt = self.build_prompt(
            "decide_chat",
            {
                "context": context,
                "date": date_str,
                "chat_history": chat_history,
                "agent_status": a_des,
                "another_status": o_des,
                "agent": agent.name,
                "another": other.name,
                "relation_line": self._relation_line(agent.name, other.name),  # [story-weaver:affinity]
            }
        )

        class decide_chatResponse(BaseModel):
            res: bool = Field(description="是否主動發起對話，true 表示會主動對話，false 表示不會")

        def _callback(response):
            if isinstance(response, bool):
                return response
            return str(response).strip().lower() in KW_TRUE_TOKENS

        failsafe = False
        return Result(prompt, _callback, failsafe, decide_chatResponse)

    def prompt_decide_chat_terminate(self, agent, other, chats):
        conversation = "\n".join(["{}: {}".format(n, u) for n, u in chats])
        conversation = (
            conversation or "[對話尚未開始]"
        )

        prompt = self.build_prompt(
            "decide_chat_terminate",
            {
                "conversation": conversation,
                "agent": agent.name,
                "another": other.name,
            }
        )

        class decide_chat_terminateResponse(BaseModel):
            res: bool = Field(description="對話是否已告一段落，true 表示對話結束，false 表示對話仍在繼續")

        def _callback(response):
            if isinstance(response, bool):
                return response
            return str(response).strip().lower() in KW_TRUE_TOKENS

        failsafe = False
        return Result(prompt, _callback, failsafe, decide_chat_terminateResponse)

    def prompt_decide_wait(self, agent, other, focus):
        example1 = self.build_prompt(
            "decide_wait_example",
            {
                "context": "簡是麗茲的室友。2022-10-25 07:05，簡和麗茲互相問候了早上好。",
                "date": "2022-10-25 07:09",
                "agent": "簡",
                "another": "麗茲",
                "status": "簡 正要去浴室",
                "another_status": "麗茲 已經在 使用浴室",
                "action": "使用浴室",
                "another_action": "使用浴室",
                "reason": "推理：簡和麗茲都想用浴室。簡和麗茲同時使用浴室會很奇怪。所以，既然麗茲已經在用浴室了，對簡來說最好的選擇就是等著用浴室。\n",
                "answer": "答案：<選項A>",
                "relation_line": "「簡對麗茲的好感度為50（友好）：室友，相處融洽。」",  # [story-weaver:affinity]
            }
        )
        example2 = self.build_prompt(
            "decide_wait_example",
            {
                "context": "山姆是莎拉的朋友。2022-10-24 23:00，山姆和莎拉就最喜歡的電影進行了交談。",
                "date": "2022-10-25 12:40",
                "agent": "山姆",
                "another": "莎拉",
                "status": "山姆 正要去吃午飯",
                "another_status": "莎拉 已經在 洗衣服",
                "action": "吃午飯",
                "another_action": "洗衣服",
                "reason": "推理：山姆可能會在餐廳吃午飯。莎拉可能會去洗衣房洗衣服。由於山姆和莎拉需要使用不同的區域，他們的行為並不衝突。所以，由於山姆和莎拉將在不同的區域，山姆現在繼續吃午飯。\n",
                "answer": "答案：<選項B>",
                "relation_line": "「山姆與莎拉並不相識（陌生/中立）。」",  # [story-weaver:affinity]
            }
        )

        def _status_des(a):
            event, loc = a.get_event(), ""
            if event.address:
                loc = " 在 {} 的 {}".format(event.address[-2], event.address[-1])
            if not a.path:
                return f"{a.name} 已經在 {event.get_describe(False)}{loc}"
            return f"{a.name} 正要去 {event.get_describe(False)}{loc}"

        context = ". ".join(
            [c.describe for c in focus["events"]]
        )
        context += "\n" + ". ".join([c.describe for c in focus["thoughts"]])

        task = self.build_prompt(
            "decide_wait_example",
            {
                "context": context,
                "date": utils.get_timer().get_date("%Y-%m-%d %H:%M"),
                "agent": agent.name,
                "another": other.name,
                "status": _status_des(agent),
                "another_status": _status_des(other),
                "action": agent.get_event().get_describe(False),
                "another_action": other.get_event().get_describe(False),
                "reason": "",
                "answer": "",
                "relation_line": self._relation_line(agent.name, other.name),  # [story-weaver:affinity]
            }
        )

        prompt = self.build_prompt(
            "decide_wait",
            {
                "examples_1": example1,
                "examples_2": example2,
                "task": task,
            }
        )

        class decide_waitResponse(BaseModel):
            res: str = Field(description="選擇的選項，'A' 表示等待，'B' 表示繼續當前行動")

        def _callback(response):
            return "A" in response

        failsafe = False
        return Result(prompt, _callback, failsafe, decide_waitResponse)

    def prompt_summarize_relation(self, agent, other_name):
        nodes = agent.associate.retrieve_focus([other_name], 50)

        prompt = self.build_prompt(
            "summarize_relation",
            {
                "context": "\n".join(["{}. {}".format(idx, n.describe) for idx, n in enumerate(nodes)]),
                "agent": agent.name,
                "another": other_name,
                "relation_line": self._relation_line(agent.name, other_name),  # [story-weaver:affinity]
            }
        )
        failsafe = agent.name + " 正在看著 " + other_name
        class summarize_relationResponse(BaseModel):
            res: str = Field(description="一句話描述兩人之間的關係，以第三人稱表述")

        def _callback(response):
            return response.strip() or failsafe

        return Result(prompt, _callback, failsafe, summarize_relationResponse)

    def prompt_generate_chat(self, agent, other, relation, chats):
        focus = [relation, other.get_event().get_describe()]
        if len(chats) > 4:
            focus.append("; ".join("{}: {}".format(n, t) for n, t in chats[-4:]))
        nodes = agent.associate.retrieve_focus(focus, 15)
        memory = "\n- " + "\n- ".join([n.describe for n in nodes])
        chat_nodes = agent.associate.retrieve_chats(other.name)
        pass_context = ""
        for n in chat_nodes:
            delta = utils.get_timer().get_delta(n.create)
            if delta > 480:
                continue
            pass_context += f"{delta} 分鐘前，{agent.name} 和 {other.name} 進行過對話。{n.describe}\n"

        address = agent.get_tile().get_address()
        if len(pass_context) > 0:
            prev_context = f'\n背景：\n"""\n{pass_context}"""\n\n'
        else:
            prev_context = ""
        curr_context = (
            f"{agent.name} {agent.get_event().get_describe(False)} 時，看到 {other.name} {other.get_event().get_describe(False)}。"
        )

        conversation = "\n".join(["{}: {}".format(n, u) for n, u in chats])
        conversation = (
            conversation or "[對話尚未開始]"
        )

        prompt = self.build_prompt(
            "generate_chat",
            {
                "agent": agent.name,
                "base_desc": self._base_desc(),
                "memory": memory,
                "address": f"{address[-2]}，{address[-1]}",
                "current_time": utils.get_timer().get_date("%H:%M"),
                "previous_context": prev_context,
                "current_context": curr_context,
                "another": other.name,
                "conversation": conversation,
                "relation_line": self._relation_line(agent.name, other.name),  # [story-weaver:affinity]
            }
        )

        class generate_chat(BaseModel):
            res: str = Field(description="角色說出的對話內容，1到3句話")

        def _callback(response):
            response = response.strip()
            # 移除 "名字：" 前缀
            if response.startswith(agent.name + "：") or response.startswith(agent.name + ":"):
                response = response[len(agent.name) + 1:].strip()
            return response or failsafe

        failsafe = "嗯"
        return Result(prompt, _callback, failsafe, generate_chat)

    def prompt_generate_chat_check_repeat(self, agent, chats, content):
        conversation = "\n".join(["{}: {}".format(n, u) for n, u in chats])
        conversation = (
                conversation or "[對話尚未開始]"
        )

        class generate_chat_check_repeatResponse(BaseModel):
            res: bool = Field(description="新對話內容是否與歷史記錄重複，true 表示重複，false 表示不重複")

        prompt = self.build_prompt(
            "generate_chat_check_repeat",
            {
                "conversation": conversation,
                "content": f"{agent.name}: {content}",
                "agent": agent.name,
            }
        )

        def _callback(response):
            if isinstance(response, bool):
                return response
            return str(response).strip().lower() in KW_TRUE_TOKENS

        failsafe = False
        return Result(prompt, _callback, failsafe, generate_chat_check_repeatResponse)

    def prompt_summarize_chats(self, chats):
        conversation = "\n".join(["{}: {}".format(n, u) for n, u in chats])

        prompt = self.build_prompt(
            "summarize_chats",
            {
                "conversation": conversation,
            }
        )

        class summarize_chatsResponse(BaseModel):
            res: str = Field(description="對話內容的簡短摘要，一句話概括對話主題")

        def _callback(response):
            return response.strip()

        if len(chats) > 1:
            failsafe = "{} 和 {} 之間的普通對話".format(chats[0][0], chats[1][0])
        else:
            failsafe = "{} 說的話沒有得到回應".format(chats[0][0])

        return Result(prompt, _callback, failsafe, summarize_chatsResponse)

    def prompt_reflect_focus(self, nodes, topk):
        prompt = self.build_prompt(
            "reflect_focus",
            {
                "reference": "\n".join(["{}. {}".format(idx, n.describe) for idx, n in enumerate(nodes)]),
                "number": topk,
            }
        )

        class reflect_focusResponse(BaseModel):
            res: List[str] = Field(description="需要深入思考的問題列表，每項為一個問題")

        def _callback(response):
            assert len(response) >= 1, "reflect_focus: empty list"
            return response

        failsafe = [
                "{} 是誰？".format(self.name),
                "{} 住在哪裡？".format(self.name),
                "{} 今天要做什麼？".format(self.name),
            ]
        return Result(prompt, _callback, failsafe, reflect_focusResponse)

    def prompt_reflect_insights(self, nodes, topk):
        prompt = self.build_prompt(
            "reflect_insights",
            {
                "reference": "\n".join(["{}. {}".format(idx, n.describe) for idx, n in enumerate(nodes)]),
                "number": topk,
            }
        )

        class reflect_insightsResponse(BaseModel):
            res: List[Tuple[str, str]] = Field(description="洞察列表，每項為 [洞察內容, 相關節點索引的逗號分隔字符串如'1,2,3'] 的元組")

        def _callback(response):  
            insights = []  
            for insight, node_ids_str in response:  
                # 将字符串"1,2,3"转换为节点ID列表  
                indices = [int(i.strip()) for i in node_ids_str.split(",")]  
                node_ids = [nodes[i].node_id for i in indices if i < len(nodes)]  
                insights.append([insight.strip(), node_ids])  
            return insights

        failsafe = [
                [
                    "{} 在考慮下一步該做什麼".format(self.name),
                    [nodes[0].node_id],
                ]
            ]
        return Result(prompt, _callback, failsafe, reflect_insightsResponse)

    def prompt_reflect_chat_planing(self, chats):
        all_chats = "\n".join(["{}: {}".format(n, c) for n, c in chats])

        prompt = self.build_prompt(
            "reflect_chat_planing",
            {
                "conversation": all_chats,
                "agent": self.name,
            }
        )

        class reflect_chat_planingResponse(BaseModel):
            res: str = Field(description="從對話中提取的對角色計劃的影響或啟發，一句話描述")

        def _callback(response):
            return response.strip() or failsafe

        failsafe = f"{self.name} 進行了一次對話"
        return Result(prompt, _callback, failsafe, reflect_chat_planingResponse)

    def prompt_reflect_chat_memory(self, chats):
        all_chats = "\n".join(["{}: {}".format(n, c) for n, c in chats])

        prompt = self.build_prompt(
            "reflect_chat_memory",
            {
                "conversation": all_chats,
                "agent": self.name,
            }
        )
        class reflect_chat_memoryResponse(BaseModel):
            res: str = Field(description="從對話中提取的值得記憶的內容，一句話描述")

        def _callback(response):
            return response.strip() or failsafe

        failsafe = f"{self.name} 進行了一次對話"
        return Result(prompt, _callback, failsafe, reflect_chat_memoryResponse)

    def prompt_retrieve_plan(self, nodes):
        statements = [
            n.create.strftime("%Y-%m-%d %H:%M") + ": " + n.describe for n in nodes
        ]

        prompt = self.build_prompt(
            "retrieve_plan",
            {
                "description": "\n".join(statements),
                "agent": self.name,
                "date": utils.get_timer().get_date("%Y-%m-%d"),
            }
        )

        class retrieve_planResponse(BaseModel):
            res: List[str] = Field(description="從記憶中檢索出的相關計劃列表，每項為一條計劃描述")

        def _callback(response):
            assert len(response) >= 1, "retrieve_plan: empty list"
            return response

        failsafe = [r.describe for r in random.choices(nodes, k=5)]
        return Result(prompt, _callback, failsafe, retrieve_planResponse)

    def prompt_retrieve_thought(self, nodes):
        statements = [
            n.create.strftime("%Y-%m-%d %H:%M") + "：" + n.describe for n in nodes
        ]

        prompt = self.build_prompt(
            "retrieve_thought",
            {
                "description": "\n".join(statements),
                "agent": self.name,
            }
        )

        class retrieve_thoughtResponse(BaseModel):
            res: str = Field(description="從記憶中檢索出的相關思考內容，一句話總結")

        def _callback(response):
            return response.strip() or failsafe

        failsafe = "{} 應該遵循昨天的日程".format(self.name)
        return Result(prompt, _callback, failsafe, retrieve_thoughtResponse)

    def prompt_retrieve_currently(self, plan_note, thought_note):
        time_stamp = (
            utils.get_timer().get_date() - datetime.timedelta(days=1)
        ).strftime("%Y-%m-%d")

        prompt = self.build_prompt(
            "retrieve_currently",
            {
                "agent": self.name,
                "time": time_stamp,
                "currently": self.currently,
                "plan": ". ".join(plan_note),
                "thought": thought_note,
                "current_time": utils.get_timer().get_date("%Y-%m-%d"),
            }
        )

        class retrieve_currentlyResponse(BaseModel):
            res: str = Field(description="角色當前狀態的更新描述，基於過去的計劃和思考")

        def _callback(response):
            return response.strip() or failsafe

        failsafe = self.currently

        return Result(prompt, _callback, failsafe, retrieve_currentlyResponse)

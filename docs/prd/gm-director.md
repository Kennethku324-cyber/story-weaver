# PRD: GM/敘事總監系統

> Story Weaver 系統級 mini-PRD。負責：每回合結束讀取該回合所有事件與對話、偵測劇情分支點、生成 2-3 個選項、處理玩家自訂命令、將玩家選擇轉化為高 poignancy 記憶注入相關 agents、調整好感度。
>
> 本文所有接口均對照已驗證代碼：`generative_agents/start.py`、`modules/game.py`、`modules/agent.py`、`modules/memory/associate.py`、`modules/memory/event.py`、`modules/model/llm_model.py`、`data/config.json`。

---

## 核心目的

**唯一必須做到的事：在 agents 自主推演與玩家敘事意志之間建立一個可靠、可恢復的「干預通道」。**

GenerativeAgentsCN 的 think loop（`Agent.think()`：percept → make_plan → reflect）本來是全封閉的——agents 只對世界裡其他 agents 的事件作反應，玩家是旁觀者。本系統要做的，就是把玩家的意圖**翻譯成 agents 記憶流裡的一等公民**（`Concept`，node_type 為 `event`/`thought`，高 poignancy），令 agents 在下一回合的 percept / retrieve / reflect / chat 中自然地把玩家意志「當成自己經歷過的事」來回應，而不是硬改 agents 的行動表。

服務的玩家需求：

1. **共寫而非旁觀**——玩家每回合都能在關鍵分支點落子，故事走向由玩家與 agents 共同決定。
2. **理解才落子**——玩家做決定前必須看到由開端到目前的完整劇情時間線（故事回顧），保留角色對白原文，而不是只看 GM 摘要。
3. **不干預都成立**——玩家不輸入任何東西，故事依然自主推演且連貫；GM 的干預是「錦上添花」，不是劇情成立的必要條件。
4. **世界有記憶**——好感度、關係、角色設定隨劇情演變，玩家感受到自己的選擇在角色身上留下持久痕跡。

明確不做的事：無輸贏判定、無任務/目標系統、GM 不直接改寫 agent 的 `action` 或 `schedule`（只用記憶注入與好感度兩個槓桿，保持 agents 自主性）。

---

## 玩家體驗

### 流程逐步描述（以一個回合為單位）

1. **進入**：Setup 完成後進入遊戲主畫面（2D 小鎮地圖，沿用 `frontend/` 原版視覺）。玩家按「開始下一回合」（或第一回合自動開始）。前端呼叫後端 `POST /api/round/start`。
2. **推演中**：後端執行 `SimulateServer.simulate(step=N)`（一回合 = N 個模擬 step，預設 N 對應遊戲內約 2-3 小時）。前端以 replay 風格播 agent 移動與 emoji；對話發生時地圖上角色頭頂出現 💬。畫面有「推演中…」狀態，玩家不能中途下指令。
3. **回合結束 → GM 介入**：後端跑完 N step 後，GM agent 被觸發：
   - 讀取該回合 `conversation.json` 的增量對話（`game.conversation` 結構：`{時間key: [{"A -> B @ 地址": [(名, 對白), ...]}]}`）與每個 agent 該回合新增的 `concepts`。
   - LLM 生成本回合「回合摘要」、偵測 1 個主要「劇情分支點」、生成 2-3 個分支選項，每個選項附一句「預期走向」。
4. **決策 modal 彈出**，包含四個區塊：
   - **故事回顧**：由「故事開端」（Setup 填的）到目前為止的完整劇情時間線，逐回合列出，保留角色對白原文（直接從 conversation log 取出，不經 LLM 改寫）。可摺疊，預設展開最新一回合。
   - **本回合摘要**：GM 寫的 3-5 句敘事摘要。
   - **分支選項**：2-3 張選項卡，每張一句標題 + 一句預期走向。
   - **自訂命令框**：純文字輸入，placeholder 例：「命令梅主動約約翰去玫瑰酒吧傾偈」。
5. **玩家輸入**：揀一個選項，或（亦可同時）填自訂命令；亦可按「任由發展」跳過。好感度變動如 GM 有建議會一併顯示（例：「梅 → 約翰：+15」），玩家可接受或手動改數值（slider，-100 ~ +100）。
6. **反饋**：確認後畫面顯示「你的意志已注入小鎮」過場，列出受影響角色與注入內容（一句話）。隨即開始下一回合推演。
7. **感覺**：玩家應感到自己像一個「落一句提示就改變全鎮命運」的編劇——不是微操角色，而是看著角色把自己的提示消化、演繹、有時甚至陽奉陰違（因為注入的只是記憶，agents 仍有自主性）。這種「不完全受控」正是設計意圖。
8. **結局**：第 10 回合結束後（或玩家 2 回合後隨時可提早完結），GM 生成「故事終章」：完整時間線 + 結尾敘事 + 各角色最終狀態與好感度變化總表。無輸贏，只有「你們共同寫成的故事」。

---

## 數據模型（Inputs / Internal State / Outputs）

### Inputs（每回合 GM 收到的數據）

| 欄位 | 來源 | 說明 |
|---|---|---|
| `round_no: int` | 回合系統 | 2-10 |
| `conversations_delta: dict` | `game.conversation`（`start.py` 每 step 寫入 `results/checkpoints/<name>/conversation.json`） | 本回合新增對話，結構 `{時間key: [{"A -> B @ 地址": [[名, 對白], ...]}]}` |
| `events_delta: list[dict]` | 各 agent `agent.concepts`（`Agent.percept()` 產生的本 step Concept 列表）+ `agent.chats` | 本回合各 agent 感知到的事件（subject/predicate/object/describe/address/poignancy） |
| `agent_states: dict` | checkpoint `simulate-<時間>.json` 的 `config["agents"][名]` | 每個 agent 的 `currently`、`status.poignancy`、`action.abstract()`、`schedule.abstract()`、coord |
| `affinity_matrix: dict` | Setup 系統建立的關係存儲（見 Internal State） | `{"A": {"B": -100..+100}}`，雙向獨立 |
| `story_seed: str` | Setup 系統 | 玩家填的故事開端原文 |
| `player_choice: object \| null` | 前端 modal | 上一回合玩家選擇（首回合為 null）：`{type: "option"\|"custom"\|"skip", option_id?, text?, affinity_overrides?}` |

### Internal State（GM 自身持有，存檔於 checkpoint）

新增檔案 `results/checkpoints/<name>/gm_state.json`，隨每回合 checkpoint 一起寫入：

```json
{
  "story_seed": "玩家填的開端原文",
  "timeline": [
    {
      "round": 1,
      "summary": "GM 回合摘要（3-5 句）",
      "key_events": ["事件描述原文..."],
      "dialogues": [{"speakers": "梅 -> 約翰", "address": "...", "lines": [["梅", "對白原文"], ...]}],
      "branch_point": "偵測到的分支點描述",
      "options_offered": [{"id": "A", "title": "...", "predicted": "..."}],
      "player_choice": {"type": "option", "option_id": "A"},
      "affinity_changes": [{"from": "梅", "to": "約翰", "delta": 15, "new_value": 65, "reason": "..."}]
    }
  ],
  "pending_injections": [],
  "injection_log": [
    {"round": 1, "targets": ["梅", "約翰"], "content": "...", "node_ids": {"梅": "node-xxx"}, "poignancy": 9}
  ],
  "branch_point_history": ["..."]
}
```

好感度存儲（Setup 系統建立、GM 系統修改）：`results/checkpoints/<name>/affinity.json`

```json
{"梅": {"約翰": 50, "埃迪": 80}, "約翰": {"梅": 45}}
```

另：好感度對 agents 可見的方式是**在每回合開始時把當前好感度快照注入每個 agent 的 prompt 上下文**（見「依賴」第 4 點），而非改寫 `agent.json` 的 `scratch`（scratch 保留 Setup 初始設定，作為「角色原點」）。

### Outputs（其他系統需要的數據）

| 輸出 | 接收方 | 欄位 |
|---|---|---|
| `GMDecision`（API 回應給前端） | 前端 modal | `round_no, summary, branch_point, options[2-3]{id,title,predicted}, suggested_affinity_changes[]{from,to,delta,reason}, story_timeline[]（故事回顧完整內容）` |
| 記憶注入 | Agent 記憶流 | 對每個 target agent 呼叫 `agent.associate.add_node(node_type="event", event=Event(...), poignancy=8~10)`；同時 `agent.status["poignancy"] += 20`，推動該 agent 盡快觸發 `reflect()`（`config.json` 的 `think.poignancy_max=150` 為閾值） |
| 好感度更新 | 關係存儲 + Setup 系統讀取 | 寫入 `affinity.json`；clamp 至 [-100, +100] |
| 回合歷史 | 結局系統 / 故事回顧 | `gm_state.json` 的 `timeline` |
| 自訂命令解析結果 | 前端確認 UI | `{targets: [角色名...], command_event_describe: "改寫為第三人稱事件描述", feasible: bool, refuse_reason?}` |

---

## 依賴

### 對 GenerativeAgentsCN 現有模塊的依賴（具體到文件與函數）

1. **回合掛鉤點 — `generative_agents/start.py`**
   - `SimulateServer.simulate(step, stride)` 的 for-loop 每 iteration 為一 step。GM 系統在 `simulate()` 跑完一回合的 N step **之後**被呼叫（新增 `GMDirector.on_round_end(server)`，由 Flask 回合路由驅動，**不改動** `simulate()` 內部邏輯）。
   - 讀取 `self.game.conversation`（`Game.__init__` 第 21 行注入、`_chat_with()` 第 576-579 行寫入）取得對話增量。
   - 讀取 `self.config["agents"][名]`（每 step 第 81-87 行以 `agent.to_dict()` 更新）取得 agent 狀態。

2. **記憶注入 — `modules/memory/associate.py` + `modules/memory/event.py` + `modules/agent.py`**
   - 注入用 `Associate.add_node(node_type, event, poignancy, create, expire, filling)`（第 166-194 行已驗證簽名），**繞過** `Agent._add_concept()` 的 LLM poignancy 評分（第 632-656 行會呼叫 `completion("poignancy_event")`），直接指定 poignancy=8~10，保證玩家意志是「極重要記憶」。
   - `Event(subject, predicate, object, describe, address, emoji)`（`modules/memory/event.py`）：GM 選項注入用 `predicate="得知"`、自訂命令用 `predicate="被命運驅使"`（或其他繁中謂語，見下）；`describe` 為完整事件描述（LlamaIndex 以 `event.get_describe()` 做 embedding，`add_node` 第 188 行）。
   - **語言注意**：代碼中 `Agent.think()`、`is_awake()`、`_skip_react()` 等處以中文字串硬編碼判斷（如 `"睡" in plan["describe"]`、`predicate == "待开始"`、`"睡觉"`、`"空闲"`/`"空闲"`）。GM 注入的 event **subject 必須是 agent 名、describe 用繁體書面語**，但不得使用會誤觸硬編碼判斷的詞（「睡觉」「空闲」「对话」等簡體謂語只用於系統事件，玩家注入記憶用「得知/聽聞/下定決心」等中性謂語，避免 `percept()` 的 `node_type` 分類（`event.fit(self.name, "对话")`，agent.py 第 301 行）錯誤歸類為 chat）。

3. **LLM 呼叫 — `modules/model/llm_model.py`**
   - GM agent 是**獨立 LLM 客戶端**，不是一個 `Agent` 實例（不進 maze、無 spatial/schedule）。直接 `create_llm_model({"provider": "openai", "model": ..., "base_url": ..., "api_key": ...})`，沿用 `completion(prompt, retry=10, failsafe=...)` 的 retry + failsafe 機制（第 24-55 行）。
   - GM 的 structured output 定義 pydantic model（`GMDecision`），沿用 `OllamaLLMModel._completion` 的 json_schema + pydantic 驗證路徑；OpenAI provider 走 magentic `-> return_type`。
   - GM 的 failsafe：若 10 次 retry 全敗，回傳預設安全值（「任由發展」+ 無好感度變動），回合流程**絕不因 GM 失敗而卡死**。

4. **好感度可見化 — `modules/prompt/scratch.py`**
   - `_base_desc()`（第 28-42 行）組裝 base_desc 模板。新增繁體模板欄位 `relations_block`：每回合開始前由回合系統把該 agent 的 affinity 快照渲染成文字（例：「你對約翰的好感度為 65（信任的丈夫）；約翰對你的好感度為 45」），注入 `Scratch.currently` 的前綴或 base_desc 新變量。這是好感度影響 agents 行為的唯一通道——配合 `summarize_relation.txt`（現有模板，`_chat_with` 第 529-531 行呼叫）讓 LLM 在生成對話時感知關係變化。

5. **Checkpoint / 恢復 — `start.py::get_config_from_log`（第 111-134 行）**
   - 現有恢復只還原 config + conversation + LlamaIndex storage（`results/checkpoints/<name>/storage`，`Associate.to_dict()` → `_index.save()`）。GM 系統新增：`gm_state.json` 與 `affinity.json` 放同一 checkpoints 目錄，resume 時一併載入；`start_step` 仍由最新 `simulate-*.json` 的 `config["step"]` 決定。

6. **Prompt 模板 — `generative_agents/data/prompts/`（29 個簡體 .txt）**
   - 本系統新增 3 個繁體模板（不依賴 Scratch，GM 用自己的模板目錄 `data/prompts_gm/`）：
     - `gm_round_summary.txt`（回合摘要 + 分支點偵測 + 2-3 選項，一次 LLM call 完成）
     - `gm_custom_command.txt`（解析玩家自訂命令 → targets + 事件描述 + 可行性判斷）
     - `gm_finale.txt`（終章敘事）
   - 現有 29 個模板轉繁體屬「Prompt 本地化系統」的範疇，但 GM 注入記憶的 describe 文字會進入這些模板的檢索結果（`retrieve_focus`），故**轉繁體必須先於或同步於本系統上線**，否則簡繁混雜會污染 embedding 檢索與對話生成。

### 對其他 5 個 Story Weaver 系統的關係

| 系統 | 關係 |
|---|---|
| Setup/角色配置系統 | **輸入方**：提供 `story_seed`、初始 `affinity.json`、角色定義（職業/性格寫入 `agent.json` 的 `scratch.learned`）。GM 不修改 scratch，只讀。 |
| 回合/推演引擎系統 | **宿主**：負責調 `simulate(N)` 並在回合邊界呼叫 `GMDirector.on_round_end()`；回合數上限（10）由它判斷並通知 GM 生成終章。GM 注入記憶後，由它觸發下一回合。 |
| 前端 UI 系統 | **呈現方**：渲染決策 modal（故事回顧/摘要/選項/自訂框/好感度 slider），提交 `player_choice` 到 `POST /api/round/decide`。GM 只出 JSON，不碰 HTML。 |
| 存檔/恢復系統 | **持久化夥伴**：GM 把 `gm_state.json`、`affinity.json` 的讀寫委託給它；resume 時保證「未完成的決策 modal」可重現（見邊界情況 3）。 |
| Prompt 本地化系統 | **前置依賴**：29 個模板簡轉繁；GM 的 3 個新模板直接以繁體撰寫，遵循同一書面語規範。 |

---

## 邊界情況

| 場景 | 預期行為 |
|---|---|
| 1. 玩家唔揀選項、唔填命令，直接撳「任由發展」（或 modal 超時） | 視為 `player_choice={type:"skip"}`，**不注入任何記憶**、好感度不變，直接開始下一回合。故事必須依然連貫（agents 自主推演本來就成立）。 |
| 2. GM 的 LLM 返回垃圾（JSON parse 失敗 / 選項少於 2 個 / 欄位缺失），10 次 retry 全敗 | 走 `failsafe`：modal 顯示「命運之線暫時模糊…」+ 只有「任由發展」一個按鈕 + 自訂命令框仍可用（自訂命令有獨立解析與 failsafe）。回合流程永不阻塞；失敗記入 `gm_state.json` 的 `injection_log` 附 `error` 欄位。 |
| 3. 玩家喺決策 modal 未確認時關閉瀏覽器 / 伺服器重啟 | 決策狀態已寫入 `gm_state.json`（`pending_decision` 欄位）。Resume 時（`--resume` 路徑）前端重開 modal，顯示同一組選項（選項在生成時已持久化，不重跑 LLM，避免每次 reload 選項都變）。 |
| 4. Checkpoint 恢復中途斷咗（`simulate-*.json` 寫到一半 / LlamaIndex storage 損毀） | `get_config_from_log` 取**最新完整** JSON（按文件名排序，讀取失敗則退到前一個）；storage 損毀時 `LlamaIndex` 重建空索引，agents 記憶歸零但 `gm_state.json` 的 timeline 仍在，故事回顧不受影響；GM 在下一回合摘要中提示「小鎮的記憶出現了裂縫」。 |
| 5. 好感度爆界：GM 建議 delta 使數值超出 [-100, +100]，或玩家手動拉爆 | 寫入前 clamp（`max(-100, min(100, v))`）；modal 顯示的 `new_value` 已是 clamp 後值。GM prompt 中明示當前值與上下限，減少 LLM 產出超界 delta 的機會。 |
| 6. 玩家自訂命令包含唔存在嘅角色名 / 語義不明 / 內容不當 | `gm_custom_command.txt` 解析時以「當前存活角色名單」做約束；`feasible=false` 時 modal 回傳 `refuse_reason`（例：「小鎮裡沒有這個人」），玩家可修改重交，**不消耗回合**。LLM 拒絕解析時 failsafe 為 `feasible=false`。 |
| 7. 玩家自訂命令同所揀選項互相矛盾（例：揀「梅原諒約翰」+ 命令「梅同約翰反面」） | 兩者都注入，但自訂命令的 poignancy（10）高於選項（8），且 GM 在命令事件 describe 中註明「這是後來發生的事」。矛盾由 agents 的 reflect 自行消化——這是特性不是 bug（人本就會反覆）。 |
| 8. 注入記憶嘅 agent 正在瞓覺（`is_awake()==False`） | 記憶照樣注入 `associate`（記憶流與睡眠狀態無耦合，percept 只加新概念、retrieve 隨時可用）；該 agent 醒來後首次 `make_schedule`/`reflect` 自然檢索到。不喚醒 agent。 |
| 9. 該回合完全無對話、無非 idle 事件（全鎮瞓覺/各自做工） | GM 偵測到 `conversations_delta` 為空且 `events_delta` 全是 poignancy=1 的 idle：跳過分支選項生成，modal 只顯示「平靜的一日」+ 自訂命令框，不浪費 LLM call 生成硬砌的「分支」。 |
| 10. 玩家喺第 2 回合就想提早完結 | modal 永遠有「完結故事」按鈕（第 2 回合起可用）；GM 用現有 timeline 跑 `gm_finale.txt` 生成終章。 |

---

## Done When

- [ ] **回合掛鉤**：`POST /api/round/start` 跑完 N step 後觸發 `GMDirector.on_round_end()`，回傳含 `summary / branch_point / options(2-3) / story_timeline / suggested_affinity_changes` 的 JSON；整個過程對 `SimulateServer.simulate()` 零侵入（只讀 `game.conversation` 與 `config`）。
- [ ] **故事回顧正確**：`story_timeline` 由 `story_seed` + `gm_state.json` 累積而成，對白為 `conversation.json` 原文（未經 LLM 改寫），跨 10 回合無遺漏、無重複。
- [ ] **記憶注入生效**：對目標 agent 調 `associate.add_node(..., poignancy≥8)` 後，該 agent 下一回合的 `retrieve_focus` 結果包含注入 concept（可用 `agent.associate.retrieve_events("注入關鍵字")` 驗證），且 `status["poignancy"]` 有相應增加。
- [ ] **注入可觀察**：注入後 3 個 step 內，目標 agent 的 `currently` 或對話內容與注入事件相關（人工抽查 5 個測試回合，命中率 ≥ 4/5）。
- [ ] **自訂命令**：`gm_custom_command` 能從自由文字中正確抽取 targets（對照當前角色名單，F1 ≥ 0.9 於 20 條測試命令）；`feasible=false` 時前端可原框重交且不推進回合。
- [ ] **好感度**：`affinity.json` 讀寫正確、clamp [-100, +100]；修改後下一回合 `base_desc`（或 `currently` 前綴）出現更新後的關係描述，可在 agent 的 `summarize_relation` prompt 輸入中觀察到。
- [ ] **失敗安全**：模擬 GM LLM 全掛（mock 拋異常）時，回合流程照舊完成、modal 顯示 failsafe 文案、錯誤入 log；模擬 `simulate-*.json` 損毀時 resume 成功退回前一 checkpoint。
- [ ] **決策持久化**：生成選項後 kill 伺服器再 resume，重開 modal 顯示**同一組**選項（逐字相同）。
- [ ] **語言**：GM 全部輸出（摘要、選項、終章、拒絕理由）為繁體香港書面語；GM 注入的記憶 describe 不含會誤觸 agent.py 硬編碼判斷的簡體謂語（「睡觉」「对话」「空闲」「待开始」）。
- [ ] **終章**：第 10 回合或提早完結時，`gm_finale.txt` 輸出完整終章（時間線 + 結尾敘事 + 好感度變化總表），寫入 `gm_state.json` 並可於前端展示。
- [ ] **全程無輸贏語義**：代碼與文案中無 win/lose/score 相關邏輯。

# PRD: 故事回顧系統

> 系統負責範圍：記錄由故事開端到目前回合嘅完整劇情時間線（事件 + 角色對白原文），生成敘事化回顧文本，供 GM 決策 modal 展示。
> 依據代碼庫：`/Users/kenneth/Projects/story-weaver`（Python 3.12 + Flask，GenerativeAgentsCN 重構版）。

---

## 核心目的

**唯一必須做到嘅嘢**：喺玩家做每一個劇情決策之前，畀佢一份「由開端到而家」嘅可靠、完整、可快速消化嘅劇情記錄 —— 包括事件時間線同**角色對白原文（一字不改）**，等佢有足夠上下文揀分支或輸入自訂命令。

服務嘅玩家需求：
- 呢個遊戲每回合 agents 自主推演 2-10 回合，玩家唔會逐 step 睇住小鎮直播；到決策時佢已經「斷片」，必須靠回顧重拾劇情。
- 玩家嘅決策（揀選項 / 自訂命令）會以高 poignancy 事件注入 agents 記憶流，直接改寫故事走向 —— 決策質素完全取決於回顧質素。回顧唔準 = 玩家亂揀 = 故事斷裂。
- 「對白原文保留」係硬性要求：玩家共寫故事嘅情感投入來自角色親口講嘅說話，摘要唔可以取代原文。

非目標（Out of Scope）：
- 唔做即時直播回放（已有 `replay.py` 負責 2D 小鎮回放）。
- 唔做分支預測或選項生成（GM agent 系統負責）。
- 唔修改 agents 記憶（玩家指令注入系統負責，本系統只記錄注入咗咩）。

---

## 玩家體驗

逐步互動流程：

1. **進入**：每回合結束，GM agent 讀取該回合所有事件同對話後，前端彈出決策 modal。modal 內置三個區域：「故事回顧」（本系統）、「2-3 個劇情選項」（GM 系統）、「自訂命令輸入框」。玩家唔需要主動搵回顧 —— 佢就喺決策嘅必经之路上。
2. **第一眼**：玩家見到嘅係**敘事化回顧**（cumulative recap）：一段 300-600 字嘅繁體書面語故事文，由開端講到而家，以「講故事」口吻串連關鍵事件同轉折，重要對白以引號嵌入原文。頂部固定顯示玩家當初填嘅「故事開端」原文，提醒佢初心。
3. **深入**：玩家可以展開「完整時間線」，按回合分節（第 1 回合、第 2 回合……），每節列出：
   - 模擬時間範圍（如 `2024-02-13 09:30 – 12:00`）
   - 事件條目：`[時間] 角色 @ 地點：做咗咩`
   - 對話區塊：標題 `阿伊莎 -> 瑪麗亞 @ 奧克山學院，圖書館`，下面逐句 `阿伊莎：「……」` 保留原文（沿用 `compress.py` 嘅 `> 引用` 排版風格）
   - 玩家干預標記：上一回合佢揀咗嘅選項或自訂命令，以醒目樣式標示「✦ 你嘅決定：……」
4. **輸入**：本系統係**唯讀**，玩家唔輸入任何嘢。互動只有：展開/收起回合、捲動、（可選）按角色篩選時間線。
5. **反饋與感覺**：
   - 正常情況：modal 一打開回顧已經喺度（上一回合結束時背景預先生成好），零等待。
   - 生成中：顯示「GM 正在整理故事……」loading，最多等 30 秒；超時自動降級。
   - 降級情況：LLM 生成失敗時，直接顯示模板拼接嘅原始時間線（冇敘事文，但事件同對白原文齊全），頂部一行小字「故事摘要暫時不可用，以下為原始記錄」。玩家永遠唔會見到空白 modal。
   - 感覺目標：玩家覺得「有個說書人幫我記住晒所有嘢」，而唔係「睇緊 log」。

---

## 數據模型（Inputs / Internal State / Outputs）

### Inputs（本系統消費嘅現成數據，全部已喺代碼庫存在）

| 來源 | 路徑 / 函數 | 關鍵欄位 |
|---|---|---|
| 每步 checkpoint | `results/checkpoints/<sim_name>/simulate-<YYYYmmdd-HHMM>.json`（由 `start.py` `SimulateServer.simulate()` 第 97-98 行寫出） | `time`、`step`、`stride`、`agents[<名>].action.event{subject, predicate, object, describe, address, emoji}`、`agents[<名>].currently`、`agents[<名>].status` |
| 對話原文 | `results/checkpoints/<sim_name>/conversation.json`（`start.py` 第 100-101 行寫出；`agent.py` `Agent._chat_with()` 第 576-579 行填入） | 結構：`{"YYYYmmdd-HH:MM": [{"阿伊莎 -> 瑪麗亞 @ 地址1，地址2": [[說話者, 對白原文], ...]}]}` |
| Agent 記憶概念 | `results/checkpoints/<sim_name>/storage/<agent>/associate`（LlamaIndex，`associate.py` `Concept`） | `node_type`(event/chat/thought)、`poignancy`、`create`、`event.describe` |
| 故事開端 + 角色設定 | Setup 系統輸出（開端文本、職業、性格、關係、雙向好感度） | `opening`、per-agent `profile` |
| 玩家決策記錄 | 決策 modal 系統提交嘅結果 | `round`、`type`(option/custom)、`text`、時間戳 |
| 角色靜態資料 | `frontend/static/assets/village/agents/<名>/agent.json` | `scratch{age, innate, learned, lifestyle}`、`currently` |

### Internal State

新增一個 append-only 檔案 **`results/checkpoints/<sim_name>/story_recap.json`**，由本系統獨家讀寫（唔改動現有 checkpoint 格式，resume 邏輯 `get_config_from_log()` 唔受影響）：

```json
{
  "sim_name": "my-story",
  "opening": "玩家填嘅故事開端原文",
  "created_at": "2026-07-28T15:00:00",
  "rounds": [
    {
      "round": 1,
      "sim_time_start": "20240213-09:30",
      "sim_time_end": "20240213-12:00",
      "step_range": [1, 12],
      "events": [
        {
          "sim_time": "20240213-09:45",
          "agent": "阿伊莎",
          "type": "action",
          "location": "奧克山學院，圖書館",
          "describe": "阿伊莎正在圖書館查閱莎士比亞嘅資料",
          "poignancy": 4
        }
      ],
      "dialogues": [
        {
          "sim_time": "20240213-10:15",
          "participants": ["阿伊莎", "瑪麗亞"],
          "location": "奧克山學院，圖書館",
          "lines": [
            {"speaker": "阿伊莎", "text": "對白原文，一字不改"}
          ]
        }
      ],
      "player_decision": {
        "type": "option",
        "text": "讓阿伊莎主動約瑪麗亞去咖啡館",
        "chosen_at": "2026-07-28T15:20:00"
      },
      "round_recap": "本回合敘事摘要（LLM 生成）",
      "recap_status": "ok"
    }
  ],
  "cumulative_recap": {
    "text": "由開端到最新回合嘅完整敘事回顧",
    "generated_at_round": 3,
    "status": "ok",
    "model": "gpt-4o-mini"
  }
}
```

欄位約束：
- `recap_status` / `cumulative_recap.status` 三值：`ok`（LLM 生成成功）、`fallback`（降級用模板拼接）、`pending`（生成中）。
- `dialogues[].lines[].text` **永不經 LLM 改寫**，直接從 `conversation.json` 複製。
- `events[].poignancy` 來自 `Agent._add_concept()`（`agent.py` 第 632-656 行）評分，1-10；低於 3 嘅瑣碎事件（如「空闲」「睡觉」）預設唔入時間線，但保留喺原始 checkpoint。
- 事件去重：相鄰 checkpoint 同一 agent 嘅 `action.event.describe` + 地點相同則合併（沿用 `compress.py` `generate_report()` 嘅 `last_state` 去重模式，第 217-245 行）。

### Outputs（其他系統消費）

| 消費者 | 接口 | 內容 |
|---|---|---|
| 決策 modal UI | `GET /api/story/<sim_name>/recap` | `{opening, cumulative_recap, rounds[], status}`（即上面 JSON 嘅讀取視圖） |
| GM agent（敘事總監） | Python 函數 `build_gm_context(sim_name) -> dict` | 壓縮版時間線：每回合 `round_recap` + 最新回合完整事件/對話，供 GM 寫入佢嘅 prompt 偵測分支 |
| 玩家指令注入系統 | 讀取 `player_decision` 欄位 | 確認上一回合注入咗咩，避免重複注入 |
| 回放/導出 | `GET /api/story/<sim_name>/recap?format=markdown` | 完整故事 markdown（可視為 `compress.py` `generate_report()` 嘅敘事化升級版），供故事結束後導出 |

---

## 依賴

### 對 GenerativeAgentsCN 現有模塊（具體到文件同函數）

| 依賴 | 文件 / 函數 | 關係 |
|---|---|---|
| 模擬主迴圈 | `generative_agents/start.py` `SimulateServer.simulate()` | 每 step 寫 `simulate-*.json` 同 `conversation.json`；本系統喺**回合結束時**（最後一個 step 寫完後）被觸發，讀取本回合嘅 checkpoints。**唔改動**寫入邏輯，只做消費者。 |
| 對話產生 | `generative_agents/modules/agent.py` `Agent._chat_with()`（第 501-594 行） | 對白原文嘅唯一來源。注意第 576 行 key 係分鐘級 `"%Y%m%d-%H:%M"`，同一分鐘多組對話會 append 入同一 list，解析時要處理一個 key 多組 chat。 |
| 記憶流 | `generative_agents/modules/memory/associate.py` `Concept` / `Associate.add_node()` | `poignancy` 評分用嚟篩選重要事件；`node_type=chat` 嘅 concept 係 `conversation.json` 損毀時嘅對話後備來源（concept 存嘅係 `summarize_chats` 摘要而唔係逐句原文，所以只能救返「發生過對話」呢個事實）。 |
| 事件模型 | `generative_agents/modules/memory/event.py` `Event.to_dict()` / `get_describe()` | 事件條目嘅序列化格式直接對齊 `to_dict()` 六欄位。 |
| 現成提取模式 | `generative_agents/compress.py` `generate_report()` | 已驗證嘅「checkpoints + conversation.json → 時間線 markdown」模式；本系統嘅事件提取器重用其去重同排版邏輯，但輸出結構化 JSON 而唔係純 markdown。 |
| LLM 調用 | `generative_agents/modules/model/llm_model.py` `LLMModel.completion(retry=10, failsafe=...)` | 敘事回顧生成必須經呢層，用盡內建 retry=10 同 `failsafe` 參數：failsafe 返回 `None` 時觸發模板降級。config 用 `data/config.json` 嘅 openai 兼容 provider。 |
| Prompt 模板 | `generative_agents/data/prompts/` + `modules/prompt/scratch.py` | 新增第 30 個模板 `story_recap_round.txt` 同 `story_recap_cumulative.txt`，**直接用繁體香港書面語撰寫**（唔等 29 個簡體模板嘅轉換工程，本系統係新嘢，冇歷史包袱）。模板唔入 `Scratch` 類（嗰個係 agent 專用），改用獨立嘅 `RecapPrompt` loader。 |
| Flask 服務 | `generative_agents/replay.py` | 參考其 Flask app 結構；recap API endpoint 掛喺遊戲主 server（決策 modal 系統同一個 app），唔係掛喺 replay server。 |

### 對其他五個系統

| 系統 | 關係 |
|---|---|
| Setup 系統（角色選擇 + 開端設定） | **上游**：提供 `opening` 原文、角色 profile、初始好感度；故事開始時觸發本系統初始化 `story_recap.json`。若 Setup 未填開端，本系統拒絕初始化並報錯返 Setup。 |
| 回合管理 / 模擬循環系統 | **上游觸發源**：每回合最後一個 step 完成後 call `recap.on_round_end(sim_name, round_no)`；提供 round ↔ step range 映射。 |
| GM agent 系統 | **下游 + 並行**：GM 用 `build_gm_context()` 攞壓縮時間線做分支偵測；GM 生成嘅選項會寫返入本系統嘅 `player_decision` 候選池記錄。GM 唔可以直接改寫時間線。 |
| 決策 modal UI 系統 | **主要消費者**：渲染 `GET /api/story/<sim>/recap`；玩家提交決策後回寫 `player_decision` 欄位。 |
| 玩家指令注入系統 | **並行**：佢負責將決策經 `Agent._add_concept()` 以高 poignancy 注入記憶流；本系統只記錄「注入咗咩」做時間線上嘅 ✦ 標記，唔參與注入邏輯。 |

---

## 邊界情況

| 場景 | 預期行為 |
|---|---|
| 第 1 回合決策（冇任何事件，得開端） | 回顧只顯示故事開端 + 角色設定卡 + 一句「故事即將展開」。`rounds` 為空陣列，`cumulative_recap.status = "ok"`，唔調 LLM。 |
| LLM 返回垃圾（空字串、JSON 截斷、簡體混英文亂碼、內容與時間線明顯不符） | `LLMModel.completion` retry=10 用盡後 failsafe 觸發 → `recap_status = "fallback"`，改為模板拼接（按回合列出事件 + 對話原文），UI 顯示降級提示。校驗規則：非空、≥50 字、不含未渲染嘅 `{placeholder}`、提及至少一個角色名。 |
| 某回合零對話零重要事件（全部 agents 瞓覺 / idle） | 時間線該節顯示「本回合風平浪靜：（地點）嘅眾人各自作息」，`round_recap` 唔調 LLM，直接用模板；`recap_status = "ok"`。 |
| 模擬中途 crash，checkpoint 寫到一半（最後一個 `simulate-*.json` 缺 agent 或 JSON 截斷） | 回合結束觸發時先驗證本回合所有 checkpoint 可解析；損毀嘅 step 剔除並記 warning log，時間線只涵蓋到最後一個完整 step；`sim_time_end` 如實反映截斷位置。 |
| `conversation.json` 損毀或缺失 | 唔阻斷流程：事件時間線照舊生成；對話區塊改從各 agent 嘅 associate 記憶（`node_type=chat` concepts）救返「邊個同邊個講過嘢 + 摘要」，標註「對話原文已散佚，以下為角色回憶」；完全救唔返就顯示「本回合對話記錄缺失」。 |
| `conversation.json` 同一分鐘 key 有多組對話（`agent.py` 第 576-579 行 append 行為） | 解析時逐組拆開成獨立 `dialogues[]` 條目，按 `participants` 排序，唔合併唔覆蓋。 |
| 玩家自訂命令注入後 agents 冇明顯反應 | 時間線照樣記錄 ✦ 干預條目；下一回合嘅敘事回顧 prompt 必須包含該干預原文，等 LLM 如實寫「你嘗試咗……但眾人似乎冇為意」而唔係隱瞞。 |
| 故事去到第 10 回合，累積文本爆 LLM context | 分層摘要：cumulative recap 嘅 input = 開端 + 各回合 `round_recap`（每段 ≤200 字）+ 最新一回合完整事件/對話原文；絕對唔將 10 回合原文一次過塞入 prompt。完整時間線 API 支援 `?round=N` 分頁。 |
| 對白原文係簡體（29 個現存模板未轉換期間） | 「原文保留」優先：對白一字不改照出（即使係簡體）；但敘事回顧、UI 標籤、事件描述一律繁體。呢個係已知過渡狀態，唔視為 bug。 |
| 兩個玩家決策連續提交（modal 重複 POST / 網絡重試） | `player_decision` 按 `round` 做 upsert（同一回合只保留最新一次），寫入用臨時檔 + rename 保證原子性，唔會寫出半截 JSON。 |
| Checkpoint resume（`--resume`）後繼續推演 | `story_recap.json` 與 checkpoint 同目錄，resume 時按 `step` 對齊：新回合嘅 step_range 由 `sim_config["step"]` 起計，唔會重覆收錄已記錄嘅 step。 |
| 好感度/角色設定隨劇情被 GM 調整 | 每次 GM 調整寫一條 `type: "gm_note"` 事件入時間線（如「阿伊莎對瑪麗亞嘅好感降至 -40」），等玩家理解角色關係點解變咗。 |

---

## Done When

- [ ] `story_recap.json` schema 實作完成，Setup 系統提交開端後自動初始化，`opening` 為空時拒絕初始化並返回明確錯誤。
- [ ] 事件提取器：逐個讀取本回合 `simulate-*.json`，輸出去重後嘅事件流（驗證：同一 agent 連續 3 個 step 同一 `describe` + 地點 → 時間線只出現 1 條）。
- [ ] 對話提取器：正確解析 `conversation.json` 嘅 `時間 → [組 → [逐句]]` 三層結構，逐句對白**位元級等同**原始記錄（自動測試比對 hash）。
- [ ] `story_recap_round.txt` 同 `story_recap_cumulative.txt` 兩個繁體模板完成，經 `LLMModel.completion` 調用，失敗時 `recap_status` 正確落 `fallback` 且模板降級文本可读。
- [ ] 分層摘要生效：第 10 回合時 cumulative recap 嘅 prompt token 數唔超過模型 context 嘅 60%（用 `LLMModel.get_summary()` 嘅計數驗證）。
- [ ] `GET /api/story/<sim_name>/recap` 返回完整 JSON，`?format=markdown` 返回可導出嘅完整故事文檔；`?round=N` 分頁可用。
- [ ] `build_gm_context(sim_name)` 輸出被 GM agent prompt 成功引用（GM 生成嘅選項內容與時間線事件相關，人工抽查 3 個回合）。
- [ ] 降級路徑全部可觸發：mock LLM 全失敗 → modal 顯示原始時間線 + 降級提示，無空白、無 500。
- [ ] 原子寫入：模擬中途 kill -9，重啟後 `story_recap.json` 永遠係合法 JSON（臨時檔 + rename 驗證）。
- [ ] Resume 對齊：`--resume` 後新回合由正確 step 起計，時間線無重複條目。
- [ ] 玩家決策（選項 / 自訂命令）正確寫入 `player_decision` 並喺下一回合時間線顯示 ✦ 標記。
- [ ] 全程繁體香港書面語：UI 文案、敘事回顧、錯誤提示；對白原文除外（保留 LLM 實際輸出）。
- [ ] 效能：回顧生成喺回合結束後背景執行，唔阻塞下一回合模擬啟動；modal 打開時 recap 已就緒率 ≥95%（本地測試 20 個回合）。

# PRD: 關係/好感度系統

> 系統負責：結構化雙向好感度（-100 ~ +100，A對B 與 B對A 獨立），初始值由玩家喺 Setup 頁設定，隨劇情由 GM agent 調整，並注入 agents 嘅 prompt（decide_chat / generate_chat / decide_wait / reflect）影響自主行為。
>
> 代碼基礎：`/Users/kenneth/Projects/story-weaver`（GenerativeAgentsCN，Python 3.12 + Flask）。

---

## 核心目的

**唯一必須做到嘅嘢：令「角色之間嘅關係」成為一個玩家可設定、GM 可演化、agents 可感知嘅一等數據，並且實際改變 agents 嘅對話決策同對白內容。**

而家嘅代碼入面，「關係」只係隱性存在——`summarize_relation`（`scratch.py:557`）喺每次開對話前即場用記憶流 summarize 一句關係描述，`decide_chat`（`agent.py:523`）純粹靠 LLM 睇 context 判斷「會唔會主動傾偈」。呢個有兩個問題：

1. 玩家喺 Setup 填嘅「A 係 B 嘅死敵」只係一段文字，第一回合之後就會被記憶流嘅向量檢索沖淡；
2. agents 之間嘅恩怨無法累積——今日嗌交、聽日若無其事，因為無任何結構化狀態記住「佢哋而家係 -60」。

**服務嘅玩家需求**：玩家係「共同作者」，佢設定嘅人物關係網係成個故事嘅戲劇引擎。如果玩家設咗「兄妹鬩牆 -80」但 agents 照樣客氣閒聊，玩家會即刻覺得「AI 唔聽話」，成個產品嘅核心幻想（我設計人物、佢哋演繹恩怨）就會崩潰。所以系統嘅成功標準係：**玩家設定嘅極端關係（±60 以上）必須喺第一回合嘅對話入面已經聽得出**。

---

## 玩家體驗

### Setup 階段（遊戲開始前）

1. 玩家喺 Setup 頁揀咗 4+ 個角色之後，進入「關係設定」區塊。
2. 介面顯示一個 N×N 關係矩陣（N = 已選角色數）：每個格仔係「行角色 → 列角色」嘅單向好感度滑桿（-100 ~ +100，預設 0），對角線（自己對自己）停用。
3. 玩家每揀一對角色，可以另填一句「關係描述」（自由文字，例：「舊情人，分手時鬧得好僵」）。呢句嘢係必答嘅戲劇上下文，數值係佢嘅量化。
4. 填寫時嘅即時反饋：滑桿數值旁邊即時顯示等級標籤（見「數據模型」嘅 band 表），例如拉到 -75 會即時顯示「敵對」，等玩家知道自己設咗幾強嘅恨意。
5. 玩家唔想逐格填，可以揀「全部陌生人」快捷鍵（全設 0 + 描述留空）。
6. 提交前驗證：任何一格缺數值 → 預設 0；數值超界 → 前端 clamp。提交後寫入模擬 config。

### 遊戲進行中

1. 玩家主要係「觀察」：佢會喺對話回放（`conversation.json`）見到 agents 嘅對白體現關係——例如 -80 嘅兩個角色撞見，應該見到 `decide_chat` 傾向唔傾、或者傾咗都係帶刺嘅對白。
2. 每回合結束，GM 嘅決策 modal（另一個系統負責）會顯示**本回合好感度變動摘要**：「阿珍 → 阿強：+15（阿強幫阿珍解圍）」，等玩家見到關係網喺度郁。
3. 玩家可以透過自訂命令直接干預（例：「阿珍原諒咗阿強」），GM 會將佢轉譯成好感度調整 + 高 poignancy 記憶注入。
4. 故事回顧時間線入面，好感度重大變動（|delta| ≥ 20）會作為事件節點出現。

### 感覺

玩家應該覺得：「我設定嘅恩怨真係上演緊」同「角色嘅態度轉變有跡可尋、我話到事」。

---

## 數據模型（Inputs / Internal State / Outputs）

### Inputs（Setup 頁提交）

```json
{
  "relations": [
    {
      "from": "阿珍",            // string，必須係已選角色名
      "to": "阿強",              // string，from != to
      "affinity": -65,           // int，-100 ~ +100
      "label": "舊情人，分手時鬧得好僵"  // string，可空，≤ 100 字
    }
  ]
}
```

- 雙向獨立：`阿珍→阿強` 同 `阿強→阿珍` 係兩條獨立記錄，唔會自動對稱。
- 缺省補全：任何「已選角色有序對」無出現喺 `relations` → 視為 `{affinity: 0, label: ""}`。

### Internal State

**存儲位置：集中式矩陣，唔放單個 agent。** 喺模擬 config 加一個頂層 key：

```json
"affinity": {
  "阿珍": {"阿強": {"value": -65, "label": "舊情人，分手時鬧得好僵"}},
  "阿強": {"阿珍": {"value": -40, "label": "想挽回"}}
}
```

理由（基於代碼事實）：
- `start.py:84` 每 step 做 `self.config["agents"][name].update(agent.to_dict())` 然後成個 `self.config` dump 落 `simulate-*.json` checkpoint——頂層 `affinity` key 會**自動跟住 checkpoint 持久化**，`get_config_from_log()`（`start.py:111`）resume 時自動讀返，唔使改序列化邏輯。
- 如果放喺個別 agent 嘅 `status` 或 `agent.json` 入面，就要改 `Agent.__init__`（`agent.py:38`）、`to_dict()`（`agent.py:678`）同 checkpoint 合併邏輯，仲會出現「A 記住 +50、B 個檔案無呢條數」嘅同步問題。集中存儲由 Game 層持有，避免雙寫。

**記憶流投影（讓 agents「感知」關係）：**
- Setup 完成時，對每條 `affinity != 0` 嘅關係，向 `from` 角色嘅記憶流注入一條 thought concept（經 `Agent._add_concept`，`agent.py:632`），describe 例：「阿珍 同 阿強 係舊情人，分手時鬧得好僵（阿珍對阿強嘅好感度：敵對）」，poignancy 直接設 8（極端值設 9-10），繞過 LLM 評分。
- GM 每次調整好感度 ≥ 10 分時，同樣注入一條 thought（例：「阿強 今日幫 阿珍 解圍，阿珍 對佢改觀咗」），令向量檢索自然帶出關係演變。

**數值 → 描述 band（注入 prompt 用）：**

| 範圍 | 標籤 |
|---|---|
| 61 ~ 100 | 摯愛/至交 |
| 21 ~ 60 | 友好 |
| 1 ~ 20 | 略有好感 |
| 0 | 陌生/中立 |
| -20 ~ -1 | 略有反感 |
| -60 ~ -21 | 敵對 |
| -100 ~ -61 | 死敵/痛恨 |

### Outputs（其他系統需要嘅嘢）

| 消費者 | 輸出 |
|---|---|
| GM agent | `affinity` 矩陣全文（每回合結束時讀取，用嚟決定調整） |
| Prompt 注入層 | `relation_line(from, to)` → 一句繁中描述，例：「阿珍 對 阿強 嘅好感度係 -65（敵對）：舊情人，分手時鬧得好僵。」 |
| 決策 modal / 故事回顧系統 | 本回合變動列表 `[{from, to, old, new, delta, reason}]` |
| Setup 系統 | 寫入接口 `set_affinity(from, to, value, label)`（clamp + 校驗） |
| Checkpoint/resume | 頂層 `affinity` key 自動入 `simulate-*.json`，無額外接口 |

---

## 依賴

### 依賴 GenerativeAgentsCN 現有模塊（本系統要改/用嘅位）

| 文件 | 位置 | 改動 |
|---|---|---|
| `generative_agents/start.py` | `get_config()`（L138）| 加 `config["affinity"] = setup_data["affinity"]`；新 key 隨 `simulate()`（L97）自動入 checkpoint |
| `generative_agents/modules/game.py` | `Game.__init__`（L15）| Game 持有 `self.affinity = config.get("affinity", {})`，提供 `get_relation_line(a, b)` 同 `adjust(a, b, delta, reason)`（clamp 到 [-100, 100]）|
| `generative_agents/modules/prompt/scratch.py` | `prompt_decide_chat`（L411）、`prompt_generate_chat`（L577）、`prompt_summarize_relation`（L557）、`prompt_decide_wait`（L478）| 各加一個模板變數 `relation_line`，由 Game 層傳入（Scratch 而家唔識 Game，需喺 `completion()` 呼叫鏈注入，參考 `agent.py:92`）|
| `generative_agents/data/prompts/decide_chat.txt` | 模板 | 背景段加一行「${relation_line}」——好感度低時 `decide_chat` 更易返「否」|
| `generative_agents/data/prompts/generate_chat.txt` | 模板 | `<對話原則>` 加一條「說話態度必須符合 ${relation_line} 所描述嘅關係同好感度」|
| `generative_agents/modules/agent.py` | `_chat_with`（L501）| `relations = [...]`（L528）嘅 `summarize_relation` 結果前面 prepend `relation_line`，令 LLM 總結關係時有結構化錨點 |
| `generative_agents/modules/agent.py` | `_add_concept`（L632）| 新增可選參數 `poignancy_override`，畀關係注入跳過 LLM poignancy 評分 |
| `generative_agents/data/prompts/` | 新檔 `gm_adjust_affinity.txt` | GM 用：輸入本回合事件+對話+現有矩陣，structured output 返回 `[{from, to, delta, reason}]`，delta clamp ±25/回合 |

### 依賴其他 5 個新系統

| 系統 | 關係 |
|---|---|
| Setup/角色建立系統 | **上游**。提供 `relations` 輸入；本系統定義佢嘅提交 schema。Setup 必須保證 `from`/`to` 都係已選角色。 |
| GM/敘事總監系統 | **雙向**。GM 每回合讀 `affinity` 矩陣 + `conversation.json` 決定調整（經 `gm_adjust_affinity.txt`）；玩家自訂命令涉及關係時由 GM 翻譯成 `adjust()` 呼叫。本系統提供 clamp 同記憶注入，GM 唔直接寫數值。 |
| 決策 modal / 故事回顧系統 | **下游消費者**。讀本回合 `affinity_changes` 列表顯示變動摘要；重大變動（≥20）入時間線。 |
| 玩家命令注入系統 | **經 GM 間接依賴**。玩家命令如「佢哋和好咗」→ GM 決定 delta → `adjust()`。本系統唔直接 parse 玩家文字。 |
| 記憶注入系統（玩家選項注入） | **共用機制**。玩家選項注入高 poignancy event 用同一條 `_add_concept(poignancy_override=...)` 路徑；本系統嘅關係變動 thought 應同佢對齊 poignancy 量級（8-10），避免關係記憶被玩家命令記憶完全淹沒。 |

---

## 邊界情況

| 場景 | 預期行為 |
|---|---|
| 玩家唔填任何好感度（全留空） | Setup 補全全部有序對為 `{value: 0, label: ""}`；遊戲照開，`relation_line` 輸出「陌生/中立」，`decide_chat` 行為同原版一致（無退化） |
| 玩家填咗 label 但無校驗角色名（打錯字「阿強」→「阿強強」） | Setup 提交時後端對 `from`/`to` 做白名單校驗（已選角色集合），唔 match 即拒絕並指出邊一格；絕唔靜默丟棄 |
| GM 嘅 LLM 返回垃圾（delta 係字串/超界/無 `reason`） | `gm_adjust_affinity` 用 pydantic structured output + `llm_model.py` 自帶 retry=10；最終 failsafe = 空列表（本回合唔調整），並 log warning。clamp：`adjust()` 內部永遠 `max(-100, min(100, v))`，delta 永遠 `max(-25, min(25, d))` |
| Resume 中途斷咗（checkpoint 係舊版、無 `affinity` key） | `Game.__init__` 用 `config.get("affinity", {})` + 對缺漏角色對補 0；resume 後 GM 見到全 0 矩陣，遊戲繼續，關係功能退化為原版行為，唔 crash |
| 好感度爆界（玩家設 150 / GM 連續 +25 推到過 100） | 寫入層（Setup `set_affinity`）同調整層（Game `adjust()`）雙重 clamp；永遠唔會有大過 ±100 嘅值進入 prompt 或 checkpoint |
| 好感度極端（-100）但 agents 被日程迫住同處一室 | `decide_chat` prompt 有 `relation_line`，LLM 大概率返「否」；即使返「是」，`generate_chat` 嘅態度約束會令對白冷淡/火藥味。**唔做硬性禁止對話**——戲劇上死敵對質係好事，系統只偏置唔鎖死 |
| 兩角色全遊戲零互動，GM 無理由調整 | 正常情況，`gm_adjust_affinity` 輸出空 delta；矩陣保持初始值，`affinity_changes` 為空，modal 顯示「本回合無關係變動」 |
| 玩家自訂命令同現有數值矛盾（「佢哋係最好朋友」但而家 -90） | GM 判斷後可以一次過調 ±25 上限以外嘅「敘事重置」：由 GM 明確輸出 `set_absolute` flag，`adjust()` 記錄為重大變動事件（入時間線），並向雙方記憶流注入 poignancy 10 嘅和解/翻轉事件，令之後嘅檢索支持新人設 |
| 角色名喺 checkpoint 同前端之間嘅編碼（中文名、空格） | 沿用現有慣例（`start.py:132` `a.replace(" ", "_")` 只用喺檔案路徑）；affinity key 永遠用角色原名（同 `config["agents"]` 嘅 key 一致），唔做路徑轉換 |

---

## Done When

- [ ] Setup 提交嘅 `relations` 經校驗同 clamp 後，寫入新遊戲 config 嘅頂層 `affinity` key，並出現喺第一個 `simulate-*.json` checkpoint 入面（開一個 4 角色新遊戲，step=1，打開 checkpoint 確認）。
- [ ] 每條非零關係喺遊戲開始時注入一條 poignancy ≥ 8 嘅 thought concept 入 `from` 角色嘅記憶流（檢查 associate index 或 log）。
- [ ] `decide_chat.txt` 同 `generate_chat.txt` 模板包含 `relation_line` 變數，且 log 入面嘅 `<PROMPT>`（`agent.py:102` debug log）可以見到完整關係描述句子。
- [ ] 對照測試：設 A→B = -80（死敵 label）同一個無設定嘅 C→D = 0，各跑 3 回合，A/B 之間嘅對話次數 ≤ C/D，且 A 對 B 嘅對白喺人工抽查下明顯帶敵意。
- [ ] GM 每回合結束呼叫 `gm_adjust_affinity`，輸出經 pydantic 校驗、delta clamp ±25、數值 clamp ±100；變動寫入 `affinity_changes` 供 modal 消費。
- [ ] LLM 連續返回垃圾 10 次時，該回合關係調整靜默降級為空操作 + warning log，模擬唔中斷。
- [ ] 用 `--resume` 由任何一個 checkpoint 恢復，`affinity` 矩陣同斷點前完全一致；由無 `affinity` key 嘅舊 checkpoint 恢復時補 0 唔 crash。
- [ ] 所有新增 prompt 模板（含 `gm_adjust_affinity.txt`）同修改嘅模板係繁體香港書面語，同項目語言決策一致。
- [ ] 決策 modal 可以讀到本回合 `affinity_changes` 並顯示「阿珍 → 阿強：-65 → -50（+15）」格式嘅摘要。

# cc-session-index

SQLite FTS5 全文索引 over Claude Code、Codex 與 Pi 的 session jsonl，取代 grep 全量掃。概念借鑑 deepseek-harness 的 session-query-sqlite（2026-08-14 競合分析抽件 #1）。2026-09-13 起支援 Codex，2026-10-19 借鑑 Obelisk 補上 Pi 支援、工具檔案參數抽取、控制面雜訊過濾、append-only 位元組游標、`message_refs` 定位表與前後文展開。

| vendor | 根目錄 | 格式 |
|---|---|---|
| cc | `~/.claude/projects/**/*.jsonl`（自動排除 `codex/` 備份鏡像） | 一行一則 `type=user/assistant`＋`message` |
| codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`、`~/.codex/archived_sessions/rollout-*.jsonl` | 一行一則 `session_meta／turn_context／response_item／event_msg`＋`payload` |
| pi | `~/.pi/agent/sessions/**/*.jsonl` | 一行一則 `session／model_change／message` |

## 用法

```sh
ccsi index                       # 建立或增量更新索引（三個 vendor 都掃）
ccsi index --vendor codex        # 只掃指定 vendor（cc / codex / pi）
ccsi search "查詢字串" -k 10
ccsi search "查詢字串" --vendor pi
ccsi search "查詢字串" -C 2      # 印出命中列同檔案前後各 2 筆已索引紀錄
ccsi search "查詢字串" --exclude-session 94c245bb

ccsi events --vendor codex --since 2026-09-13 --kind tool_call --tool spawn_agent   # 一行一個正規化事件（JSONL）
ccsi events --session 97ac80ca --kind text
```

`events` 每行欄位：`vendor、session、path、ts、cwd、role、kind（text｜tool_call｜tool_result｜session_meta｜turn_context）、text`，加上 `tool`、`model`；CC 的 Agent 呼叫多帶 `agent_type`／`agent_model`、Skill 呼叫帶 `skill`；Codex 多帶 `effort`、子 thread 的 `agent_role`（來自 session_meta.source.subagent）、`spawn_agent` 的 `agent_type`。Codex 子 agent 的真實模型只信 `turn_context`（會跟在同 session 的後續事件 `model` 欄），不信它自報的文字。

治理問題兩端一起問的寫法：先 `ccsi events --since <日期> --kind tool_call` 落成一個 JSONL，再用 jq 依 `vendor` 分組；不要各寫一套解析。

- 索引落點：`~/Library/Caches/cc-session-index/index.sqlite`
- 索引粒度：訊息級（user / assistant 文字＋tool_use 的指令與關鍵參數摘要），單則截 8000 字
- tokenizer：FTS5 trigram——中文子字串直接可搜，至少需包含一個 3 字以上詞或雙引號片語（如 `"測試 假綠"`）；純 `<3` 字詞查詢會回傳 exit 2 提示，普通空白分隔的長短詞混合查詢會對 `<3` 字詞附加 `LIKE` 過濾
- 首次全量建索引會跳過不存在舊資料的新檔刪除；15,000 檔回歸測試由 14.4 秒降至 2.7 秒。2026-10-19 v2 實測完整重建 52,992 檔耗時 371.5 秒，後續暖快取 7 檔增量耗時 0.6 秒
- `~/.local/bin/ccsi` 已連到本專案腳本
- LaunchAgent `com.gggodlin.cc-session-index` 約每 15 分鐘（900 秒）低優先度背景增量更新，log 在同一 cache 目錄（剛發生的對話若尚未入庫可先跑 `ccsi index`）

## 已知限制

- FTS5 MATCH 語法字元（`"` `*` `-` 等）會被當運算子，字面搜尋請加雙引號包裹
- 不索引 tool result 內文（只索引指令與對話文字），要挖 tool 輸出仍回 grep
- jsonl 被 CC 清掉後索引殘留舊列，`index` 不做刪除偵測（重建：刪 sqlite 重跑）
- 大量既有檔案同時改變 mtime 時，增量更新仍會逐檔掃描 FTS 舊列；這種搬機情境應備份並移除 cache 後做乾淨重建

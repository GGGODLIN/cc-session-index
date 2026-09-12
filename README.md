# cc-session-index

SQLite FTS5 全文索引 over Claude Code 與 Codex 的 session jsonl，取代 grep 全量掃。概念借鑑 deepseek-harness 的 session-query-sqlite（2026-08-14 競合分析抽件 #1）。2026-09-13 起雙軌：三個根目錄一起掃，搜尋結果帶 `[cc]`／`[codex]` 標籤，另有 `events` 子命令把兩種格式攤成同一種事件流給治理分析用。

| vendor | 根目錄 | 格式 |
|---|---|---|
| cc | `~/.claude/projects/**/*.jsonl` | 一行一則 `type=user/assistant`＋`message` |
| codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`、`~/.codex/archived_sessions/rollout-*.jsonl` | 一行一則 `session_meta／turn_context／response_item／event_msg`＋`payload` |

## 用法

```sh
ccsi index                       # 建立或增量更新索引（比對 mtime，只重讀變過的檔；兩個 vendor 都掃）
ccsi index --vendor codex        # 只掃一邊
ccsi search "查詢字串" -k 10
ccsi search "查詢字串" --vendor codex   # 只看 Codex 的命中

ccsi events --vendor codex --since 2026-09-13 --kind tool_call --tool spawn_agent   # 一行一個正規化事件（JSONL）
ccsi events --session 97ac80ca --kind text
```

`events` 每行欄位：`vendor、session、path、ts、cwd、role、kind（text｜tool_call｜tool_result｜session_meta｜turn_context）、text`，加上 `tool`、`model`；CC 的 Agent 呼叫多帶 `agent_type`／`agent_model`、Skill 呼叫帶 `skill`；Codex 多帶 `effort`、子 thread 的 `agent_role`（來自 session_meta.source.subagent）、`spawn_agent` 的 `agent_type`。Codex 子 agent 的真實模型只信 `turn_context`（會跟在同 session 的後續事件 `model` 欄），不信它自報的文字。

治理問題兩端一起問的寫法：先 `ccsi events --since <日期> --kind tool_call` 落成一個 JSONL，再用 jq 依 `vendor` 分組；不要各寫一套解析。

- 索引落點：`~/Library/Caches/cc-session-index/index.sqlite`
- 索引粒度：訊息級（user / assistant 文字＋tool_use 的指令字串），單則截 8000 字
- tokenizer：FTS5 trigram——中文子字串直接可搜，query 給 3 字以上
- 首次全量建索引會跳過不存在舊資料的新檔刪除；15,000 檔回歸測試由 14.4 秒降至 2.7 秒。2026-09-02 實測完整重建 39,465 檔耗時 184.3 秒，後續 7 檔增量耗時 8.7 秒
- `~/.local/bin/ccsi` 已連到本專案腳本
- LaunchAgent `com.gggodlin.cc-session-index` 每 6 小時低優先度增量更新，log 在同一 cache 目錄

## 已知限制

- FTS5 MATCH 語法字元（`"` `*` `-` 等）會被當運算子，字面搜尋請加雙引號包裹
- 不索引 tool result 內文（只索引指令與對話文字），要挖 tool 輸出仍回 grep
- jsonl 被 CC 清掉後索引殘留舊列，`index` 不做刪除偵測（重建：刪 sqlite 重跑）
- 大量既有檔案同時改變 mtime 時，增量更新仍會逐檔掃描 FTS 舊列；這種搬機情境應備份並移除 cache 後做乾淨重建

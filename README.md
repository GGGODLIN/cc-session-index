# cc-session-index

SQLite FTS5 全文索引 over Claude Code session jsonl（`~/.claude/projects/**/*.jsonl`），取代 grep 全量掃。概念借鑑 deepseek-harness 的 session-query-sqlite（2026-08-14 競合分析抽件 #1）。

## 用法

```sh
ccsi index            # 建立或增量更新索引（比對 mtime，只重讀變過的檔）
ccsi search "查詢字串" -k 10
```

- 索引落點：`~/Library/Caches/cc-session-index/index.sqlite`
- 索引粒度：訊息級（user / assistant 文字＋tool_use 的指令字串），單則截 8000 字
- tokenizer：FTS5 trigram——中文子字串直接可搜，query 給 3 字以上
- 首次全量建索引慢（本機 33,744 檔耗時 43 分鐘）；之後增量目前約 20 秒
- `~/.local/bin/ccsi` 已連到本專案腳本
- LaunchAgent `com.gggodlin.cc-session-index` 每 6 小時低優先度增量更新，log 在同一 cache 目錄

## 已知限制

- FTS5 MATCH 語法字元（`"` `*` `-` 等）會被當運算子，字面搜尋請加雙引號包裹
- 不索引 tool result 內文（只索引指令與對話文字），要挖 tool 輸出仍回 grep
- jsonl 被 CC 清掉後索引殘留舊列，`index` 不做刪除偵測（重建：刪 sqlite 重跑）

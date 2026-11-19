---
status: proposed
---

# Contentless FTS index; hit text is re-derived from the source .jsonl

The index stored every normalized message twice: once as trigram postings and once as a full-text copy inside the FTS table, together with the file path repeated on every row. On 2026-11-19 `index.sqlite` was 17.2 GB — 8.86 GB trigram postings, 5.21 GB text copy, 2.32 GB path strings in `message_refs` — for 6.33M rows over 202K files, and rows had grown about 5x in two months. The text copy only duplicates what the transcripts already hold, so the `messages` FTS table becomes contentless (`content=''`, `contentless_delete=1`, trigram tokenizer unchanged); each path is stored once in a `paths` table, and `message_refs` keeps `path_id`, the row's ordinal within its file, `ts` and `role`. A search hit's text and its `-C` context are rebuilt by re-parsing the source `.jsonl` with the same normalization used at index time and taking the row at that ordinal.

## Considered Options

- **Keep the content column (status quo).** Simplest, no savings.
- **External-content table over a view.** Not possible: the indexed text exists only after Python normalization of the `.jsonl` (command envelopes collapsed, task notifications reduced to their summary, tool calls summarized and truncated), not as anything SQL can select.
- **Byte offset per row instead of an ordinal.** Would make `-C` read only the needed lines, at the cost of more columns. Not taken for now: on a 3,000-file sample, re-deriving 10 hits by fully re-parsing their files took 0.20 s.

## Consequences

- Measured on the 3,000-file sample: 231.0 MB current schema vs 133.9 MB contentless with path ids (-42%); top-10 rankings were identical on 6 of 6 queries, and re-derived texts matched the stored copy byte for byte.
- Correctness now depends on the extractor that built the index being the one that re-derives the text. An extractor version is recorded in the database, and a different version forces a full rebuild; otherwise ordinals would point at the wrong rows.
- A hit needs its source file. Files deleted from disk are pruned from the index on the next `ccsi index`, so deleted sessions are no longer searchable (decided 2026-11-19). Files rewritten in place were already re-indexed whole.
- SQL `snippet()` and `highlight()` return nothing on a contentless table; the `>>>`/`<<<` snippet is built in Python. The short-token `LIKE` filter and the vendor / exclude-session path filters can no longer read FTS columns: path filters join `paths`, and short tokens over-fetch candidates and filter after re-derivation.
- A schema or extractor change means a full rebuild, built beside the live database and swapped in so searches keep working meanwhile. SQLite 3.43+ is required for `contentless_delete`.

---
status: accepted
---

# Contentless FTS index with hit text re-derived from the source .jsonl

The v2 index stored trigram postings and a normalized copy of each message. It also repeated the file path on every row. On 2026-11-19, the author's `index.sqlite` occupied 17.2 GB for 6,327,719 indexed rows and 207,509 paths in `files`. Trigram postings took 8.86 GB, the text copy took 5.21 GB, and the two structures associated with `message_refs` (the table with path strings and its index) took 2.32 GB together. The `messages` FTS table is now contentless (`content=''`, `contentless_delete=1`) and still uses the trigram tokenizer. `paths` stores each path once. `message_refs` stores `path_id`, the row's ordinal within its file, `ts`, and `role`. Search re-parses the source `.jsonl` with the same normalization used at index time to recover the hit at that ordinal and its `-C` context.

## Considered Options

- **Keep the content column (status quo).** Simplest, no savings.
- **External-content table over a view.** The indexed text exists only after Python normalizes the `.jsonl`. The extractor condenses command envelopes, keeps task notification summaries and results, and summarizes and truncates tool calls. SQL has no table or view to select that text from.
- **Byte offset per row instead of an ordinal.** Would make `-C` read only the needed lines, at the cost of more columns. On a 3,000-file sample, re-deriving 10 hits by fully re-parsing their files took 0.20 s, so the index keeps ordinals.

## Consequences

- On the 3,000-file sample, the v2 schema used 231.0 MB and the contentless schema with path IDs used 133.9 MB (42% less). All 6 queries had identical top-10 rankings, and re-derived texts matched the stored copy byte for byte.
- On the author's machine (2026-11-19), `index.sqlite` shrank from 17.2 GB to 3.09 GB. Most of the drop came from removing duplicate entries and rows for deleted files. Dropping the stored text copy accounts for only part of the savings.
- The database records its extractor version so search can recover the text at the indexed ordinal while a replacement index is being built. A change to the schema or extractor version triggers a full rebuild. `ccsi index` builds the replacement beside the live database and swaps it in atomically while holding the index lock. Search can keep using the old index during the rebuild. SQLite 3.43+ is required for `contentless_delete`.
- Search skips a hit if its source file is gone, unreadable, smaller than when indexed, or has a different inode. It continues to the next ranked candidate. An incremental `ccsi index` prunes confirmed deletions within the scan scope and retains entries if their source root is missing. Appended rows can appear in `-C` context before they become searchable.
- To produce the `>>>`/`<<<` excerpt, Python inserts the re-derived text into an in-memory FTS5 table and calls SQLite's `snippet()` with the same trigram tokenizer and snippet settings as v2. Search joins `paths` for `--vendor` and `--exclude-session`. For queries that mix long and short tokens, it takes candidates in rank order and filters for the short tokens in Python after re-deriving the text.

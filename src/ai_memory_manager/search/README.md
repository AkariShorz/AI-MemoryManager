# Search

Search provides offline full-text search over files in `archive/Session_Lib`. It builds a rebuildable SQLite FTS5 index and leaves archived source files untouched. JSON/JSONL strings and plain text are indexed; metadata comes from `session.meta.json` when available, with the archive path as a fallback.

## Use

From the repository root:

```bash
PYTHONPATH=src python -m ai_memory_manager.search.search "关键词"
PYTHONPATH=src python -m ai_memory_manager.search.search "关键词" --provider ChatGPT --persona-id persona-1 --date-from 2026-01-01 --date-to 2026-12-31
```

The index defaults to `src/ai_memory_manager/search/search-index.sqlite3`. Override it with `--index`; select a different archive root with `--archive-dir`. Each search incrementally scans the archive, adds or updates changed files, and removes entries for files that have been deleted. A full rebuild can be done by deleting the index file and running a search again.

The index contains searchable copies of text and metadata, so it has the same privacy classification as the archive. Keep the generated database local and out of version control. FTS5 must be enabled in the Python SQLite build.

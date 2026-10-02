# JSON history performance

Session and analysis JSON schemas are unchanged. No cache fields are written
into sessions; caches are disposable and rebuilt after restart. Original events,
unknown extensions, skill snapshots and reversible loot exclusions remain saved.

## Rendering

- Previous Sessions, loot receipts, item drop histories, skill message histories
  and analysis session details show 200 rows per page. First/Prev/Next/Last and
  a page-number entry expose the entire history, including sessions older than
  the former 200-session display limit. Selection applies to visible rows.
- Sorting covers the entire underlying dataset before paging. Nonpaged trees
  use one `set_children` call instead of moving each row separately.
- Session Details inserts only the selected event page in one Text operation.
- Hidden session/loot/analysis views render on activation. Files and analysis
  copies are still updated immediately when an edit is saved.
- Unchanged loot views reuse tables and chart payloads. MU changes reuse parsed
  quantities/TT and graphs; item graph filters reuse timestamps and base graphs.

## Cache and mutation rules

`SessionDerivedCache` retains compact per-session item quantity/TT totals, kill
counts and exclusion counts. Normalized receipt lists use an LRU capped at six
sessions and 25,000 receipts; one larger selected session may exceed that budget.
Removing sessions or reloading analysis prunes obsolete entries.

All application mutations of loot must call `invalidate`. List identity/length
guards detect replaced/imported histories; they intentionally do not hash every
raw message during tab changes. Use `refresh_loot_tab(force=True)` after an
external in-place mutation; the Refresh button does this. Cached normalized
events and summaries are read-only to callers.

| Change | Work |
| --- | --- |
| New live loot or exclusion/restore | Invalidate the affected session's receipts and totals |
| Setup or manual PED correction | Reprice receipt costs and turnover; retain quantity/TT totals |
| Mob/maturity | Update HP-dependent metrics and analysis grouping; retain turnover and receipts |
| Notes | Update metadata without reparsing loot |
| MU | Apply markup to cached quantities/TT, including fixed PED-per-item MU |
| Efficiency/looter | Apply projection formulas to cached per-session item totals |

For older unpriced sessions (`count_hunting` false), assigning a target still
performs the original initial turnover calculation. Equipment repricing scans
the raw attack log only if saved attack/per-receipt shot data is insufficient.
Legacy loot reconstruction still checks TT consistency before using a full log;
same-second kills and cost carried across excluded receipts retain their rules.

Archive synchronization preserves archive-only extensions and independent
analysis sessions. Edits copy only changed linked sessions; startup skips copies
whose source fields are already current. Primary/analysis write rollback and
JSON backup behavior remain in place. Cache changes are applied only after a
successful save. Archive summaries inherit canonical source totals, including
when a previously loaded analysis copy was stale.

## Validation and measurement

Run `python -m unittest discover -s tests` with an available Tk display.
`python scripts/benchmark_history.py` uses temporary synthetic files and never
opens user histories. `--module /path/to/older/skill_tracker_ui.py` compares an
older source with identical inputs. Benchmark timings exclude startup and disk
saves; cached timings are explicitly warmed. No timing thresholds are used in CI.

Example on Python 3.12/Linux/Tk: 10,000 receipts (70,000 raw events), a separate
30,000-row sort, and analysis of 100 sessions with 200 receipts each:

| Operation | Before | After |
| --- | ---: | ---: |
| Explicit full Loot Refresh | 0.70 s | 0.33 s |
| Unchanged Loot refresh | 0.73 s | 0.00013 s |
| Reprice calculation with exact saved shots | 0.047 s | 0.006 s |
| Sort 30,000 rows | 3.21 s | 0.083 s |
| Repeated analysis with changed character inputs | 0.294 s | 0.001 s |
| Repeated Previous Sessions rendering | 0.151 s | 0.003 s |

These are illustrative measurements, not Windows performance guarantees. The
first analysis still processes uncached histories. Startup still parses complete
JSON archives, and saves still rewrite the JSON files; a future storage change
can address those remaining costs independently.

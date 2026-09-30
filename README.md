# Flex Appeal FFL — site data

JSON feeds for the Flex Appeal FFL website, rebuilt from the public
[Sleeper API](https://docs.sleeper.com/) plus local history under
`/workspace/flex-appeal/sleeper/history`.

## Quick start

```bash
cd /workspace/flex-appeal-data
python3 build_site_data.py && python3 apply_history_overrides.py
```

`apply_history_overrides.py` merges manual title rulings from
`data/sources/champions_manual.json` into `history.json` (build_site_data.py only
knows the Sleeper seasons). Right now that's 2022: Deion & Jared,
co-champions (the Week 17 final was cut short when the Bills-Bengals game was
suspended). It also applies `data/sources/score_overrides.json` to the all-time
extremes lists and career H2H (2024 Week 7 official totals). Run it after every
`build_site_data.py` run or those fixes drop out.

No third-party packages required (stdlib only). Run weekly after scores
finalize, or anytime during the week to refresh standings / upcoming.

## Output (`data/`)

| File | Contents |
|------|----------|
| `meta.json` | Season, current week, `last_updated` (America/Phoenix ISO), league name |
| `flex_rankings.json` | Power rankings for the board week (heading into `current_week`) |
| `standings.json` | Dual-record standings (H2H + vs weekly median) |
| `matchups.json` | Completed weeks only (per-team rows) |
| `upcoming.json` | Next unscored week’s pairings + records |
| `season_records.json` | 2026 high/low scores, closest games, blowouts |
| `history.json` | Champions (2022 co-champions via `apply_history_overrides.py`; entries may carry `co_champions` / `note`), career (through 2026 to date), all-time extremes |
| `teams.json` | Owner, short name, team name, Sleeper handle |
| `team_stats.json` | Starter TDs/yards per team, completed weeks (`build_team_stats.py`) |
| `all_time_stats.json` | All-time stats 2022-present by manager: career, season and single-week records, streaks, manager seasons, best/worst seasons (`build_all_time_stats.py`) |
| `h2h_all_time.json` | All-time head-to-head grid 2022-present (2022 ESPN + Sleeper), Legacy Owners, season formats, title games, game log (`build_h2h.py`) |
| `rankings_history.json` | Power/Flex Rankings history 2022-present: confirmed boards (season, week, date, ranks by display name), all-time and per-season tally, #1 runs, coverage. No formula data. Built by `/workspace/flex-rankings-history/scripts_v2/publish_v3.py` |

## Head-to-head (all-time)

```bash
python3 build_h2h.py   # writes data/h2h_all_time.json (stdlib only)
```

Real H2H games only (same `matchup_id`, no median games); regular season plus all
winners-bracket games (3rd/5th-place games typed `placement`); two-week playoff
rounds count once on combined score; toilet bowl excluded; completed weeks only.
Keyed by person (Sleeper user / ESPN owner), so team renames don't split records.
2022 (ESPN) comes from the processed file `data/sources/espn_2022_games.json`
(raw ESPN dumps are not committed). Past managers are labeled **Legacy Owners**
(`meta.labels.former`; JSON keys stay `former_*`): Deion, Jake F,
Jared, Mike, Anup and Matt F (`managers[].name`; ids
stay stable). Everyone shows by first name; shared first names get a last initial (Matt Z, Matt A,
Matt F, Jake F; current Jake is just "Jake"). All display names live in `data/sources/display_names.json` (its `site_short_names` also sets the `short_name` in teams.json / team_stats.json via `build_site_data.py` and `build_team_stats.py`; `owner` stays the full name)
(also applied to history.json by `apply_history_overrides.py`; current managers keep full
names there because the site's team pages match history.json to teams.json owners). Deion's 2023 roster had no linked Sleeper account and is mapped
to him; his 2022 ESPN team also appears as "Team Hulse". Mid-season owner changes
live in `data/sources/owner_overrides.json`: 2024 roster 2 is credited to Mike
for Weeks 1-6 (he left at 11-1) and to Matt Z from Week 7 (`managers[].partial_seasons`).
Weekly scores are Sleeper matchup points except the official standings totals in
`data/sources/score_overrides.json` (2024 Week 7: trades reversed after the Sunday games
left the returned starters at 0 in Sleeper's matchup data; `meta.score_overrides_applied`).
`build_h2h.py`, `build_all_time_stats.py` and `apply_history_overrides.py` all apply it. PDF snapshot:
`assets/Flex_Appeal_H2H_All_Time.pdf` (through 2026 Week 3).

## All-time stats

```bash
python3 build_h2h.py && python3 build_all_time_stats.py   # writes data/all_time_stats.json
```

Reuses the manager registry, names, Legacy Owners label, title games and owner
overrides from `h2h_all_time.json`, so run it after `build_h2h.py`. Top-level keys:
`meta` (labels, rules, caveats, scoring_by_season, checks), `career`, `season_records`,
`week_records`, `streaks`, `manager_seasons`, `best_worst`, `notable`. Regular-season
PF/PA/PPG/all-play; record = H2H + median where Sleeper's standings count it (2024+).
2022 starter TDs / yards / player starts come from `data/sources/espn_2022_starters.json`
(processed ESPN box scores; starter points reconcile with every 2022 weekly score; raw
dumps are not committed). 2024 Week 7 uses the official standings totals from
`score_overrides.json`, so every 2024 record and PF matches Sleeper's standings. Completed-season Sleeper
responses are cached in `/workspace/flex-alltime-cache` (not committed). PDF snapshot:
`assets/Flex_Appeal_All_Time_Stats.pdf`.

## Flex Rankings

Flex Rankings are calculated via Commissioner Jake's secret formula, discretion, and vibes.

Board convention: **Week N** = ranking heading into week N, using results
through week N−1. **Week 3 board is locked** (posted movements).

## League IDs

| Season | League ID |
|--------|-----------|
| 2026 (current) | `1311998258079371264` |
| 2025 | `1180242334892032000` |
| 2024 | `1048417672926547968` |
| 2023 (Nothing Catchy FFL) | `996133920917856256` |

Champions come from Sleeper `winners_bracket` (`p=1`). 2023 roster 3
(owner_id null) is labeled **Deion**. Jake’s blank Sleeper team name is
shown as **Drought Ends Here**.

## Fallback cache

If the API is unreachable, the script falls back to:

- `/workspace/flex-numbers-2026/api/` (2026 snapshots)
- `/workspace/flex-appeal/sleeper/history/` (past seasons + career CSVs)

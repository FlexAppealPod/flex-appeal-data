# Flex Appeal FFL — site data

JSON feeds for the Flex Appeal FFL website, rebuilt from the public
[Sleeper API](https://docs.sleeper.com/) plus local history under
`/workspace/flex-appeal/sleeper/history`.

## Quick start

```bash
cd /workspace/flex-appeal-data
python3 build_site_data.py && python3 apply_history_overrides.py
```

`apply_history_overrides.py` also applies the owner fixes in
`data/sources/owner_overrides.json` to history.json: `season_owners` (2024 roster 11 = Aaron)
and the mid-season split (2024 roster 2: Mike gets Weeks 1-6 H2H W/L and PF plus a 2024 season;
Matt Z keeps Week 7 on, the playoff appearance and the finish). It merges manual title rulings from
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
| `h2h_all_time.json` | All-time head-to-head grid 2022-present (2022 ESPN + Sleeper), Legacy Managers, season formats, title games, game log (`build_h2h.py`) |
| `transactions.json` | Roster moves for the current season (`build_transactions.py`): every completed trade / waiver / free-agent move newest first (managers + team names, adds/drops with position and NFL team, FAAB bids, trade picks and FAAB), per-manager FAAB remaining/spent, summary (totals, most active, biggest bid, most added), traded-pick ownership, and clearly labeled `failed_claims` (losing waiver bids; omit with `--no-failed-claims`) |
| `rankings_history.json` | Power/Flex Rankings history 2022-present: confirmed boards (season, week, date, ranks by display name), all-time and per-season tally, #1 runs, coverage. No formula data. Built by `/workspace/flex-rankings-history/scripts_v2/publish_v3.py` |

## Roster moves

```bash
python3 build_transactions.py   # writes data/transactions.json (stdlib only)
```

Pulls Sleeper transaction legs 0-18 plus rosters (`waiver_budget_used`), users, traded
picks and the players file (cached at `/workspace/sleeper_players.json`, refreshed weekly).
Reuses the owner map, team-name fallback and fetch helpers from `build_team_stats.py`
(so display names follow `data/sources/display_names.json`). Sleeper files every pre-season
move under leg 1; the build relabels moves before the startup draft as `phase: offseason`
and draft-to-kickoff as `phase: preseason` (both `week: 0`; `sleeper_leg` keeps the raw value).

## Records: no consolation games

Every record list counts regular-season games and real playoff-bracket games only:
single-week highs/lows, blowouts, closest games and best individual starts
(`all_time_stats.json` `week_records`), career highs/lows, manager-season `high_week` /
`low_week`, `season_records.json` and the `history.json` extremes lists. "Playoff bracket" =
every Sleeper winners-bracket game, including the placement games inside it (3rd-place game,
5th-place game, 5th-place semifinal); each leg of a two-week round is its own week. Consolation
games (losers bracket / toilet bowl, and any other playoff-week pairing of teams outside the
winners bracket; ESPN 2022 `LOSERS_CONSOLATION_LADDER`) are excluded. The rule lives in
`record_phases.py`, used by `build_site_data.py`, `apply_history_overrides.py` and
`build_all_time_stats.py`, so the Tuesday rebuild keeps it. `all_time_stats.json`
`meta.consolation_excluded_from_records` counts what was left out. W-L records, PF and streaks
were already regular season / H2H only and are unaffected.

## Head-to-head (all-time)

```bash
python3 build_h2h.py   # writes data/h2h_all_time.json (stdlib only)
```

Real H2H games only (same `matchup_id`, no median games); regular season plus all
winners-bracket games (3rd/5th-place games typed `placement`); two-week playoff
rounds count once on combined score; toilet bowl excluded; completed weeks only.
Keyed by person (Sleeper user / ESPN owner), so team renames don't split records.
2022 (ESPN) comes from the processed file `data/sources/espn_2022_games.json`
(raw ESPN dumps are not committed). Past managers are labeled **Legacy Managers**
(`meta.labels.former`; JSON keys stay `former_*`): Deion, Jake F,
Jared, Mike, Anup, Matt F and Aaron (`managers[].name`; ids
stay stable). Everyone shows by first name; shared first names get a last initial (Matt Z, Matt A,
Matt F, Jake F; current Jake is just "Jake"). All display names live in `data/sources/display_names.json` (its `site_short_names` also sets the `short_name` in teams.json / team_stats.json via `build_site_data.py` and `build_team_stats.py`; `owner` stays the full name)
(also applied to history.json by `apply_history_overrides.py`; current managers keep full
names there because the site's team pages match history.json to teams.json owners). Deion's 2023 roster had no linked Sleeper account and is mapped
to him; his 2022 ESPN team also appears as "Team Hulse". Mid-season owner changes
live in `data/sources/owner_overrides.json`: 2024 roster 2 is credited to Mike
for Weeks 1-6 (he left at 11-1) and to Matt Z from Week 7 (`managers[].partial_seasons`).
Whole-season owner fixes live in the same file under `season_owners`: Sleeper stores only a
roster's *current* owner, so a roster handed to a new manager after the season shows him for the
old season too. 2024 roster 11 ("Team AMartinez" / "Comrade Kamara", 6-20) was Aaron Martinez's
(Sleeper `AMartinez528`, Legacy Manager "Aaron") all season; Matt A (`matkinson94`) only took it over
on 2024-12-31, and his first season is 2025. `build_h2h.apply_season_owners` swaps the owner before
anything is counted (h2h, all-time stats), and `apply_history_overrides.py` moves that season's
career line and extremes names in history.json (`season_owner_fixes_applied`). To check a roster's
real manager, look at who created its Sleeper transactions (`creator`), not `owner_id` or draft
`picked_by` (both are rewritten to the current owner).
Weekly scores are Sleeper matchup points except the official standings totals in
`data/sources/score_overrides.json` (2024 Week 7: trades reversed after the Sunday games
left the returned starters at 0 in Sleeper's matchup data; `meta.score_overrides_applied`).
`build_h2h.py`, `build_all_time_stats.py` and `apply_history_overrides.py` all apply it. PDF snapshot:
`assets/Flex_Appeal_H2H_All_Time.pdf` (through 2026 Week 3).

## All-time stats

```bash
python3 build_h2h.py && python3 build_all_time_stats.py   # writes data/all_time_stats.json
```

Reuses the manager registry, names, Legacy Managers label, title games and owner
overrides from `h2h_all_time.json`, so run it after `build_h2h.py`. Top-level keys:
`meta` (labels, rules, caveats, scoring_by_season, checks), `career`, `season_records`,
`week_records`, `streaks`, `manager_seasons`, `best_worst`, `notable`. Regular-season
PF/PA/PPG/all-play; record = H2H + median where Sleeper's standings count it (2024+).
`career[].games_played` (alias `games`) = regular-season head-to-head games played; the weekly
median result is not a game (Doug: 53 games, record 45-35 = 29-24 H2H + 16-11 median).
`career[].playoff_games` = winners-bracket games (a two-week round is one game).
Each career row also has a structured split (top-level fields stay for backward compatibility):
`career[].regular` (seasons, games, record / h2h / median with _str and _pct, pf, pa, ppg, pa_pg,
all_play, high_week / low_week from regular-season weeks, starter TDs/yards) and `career[].playoffs`
(appearances, seasons, games, weeks, record_str / record_pct, pf, pa, ppg / pa_pg **per playoff week
played**, finals, titles, title_seasons, high_week / low_week, best_game / worst_game by margin,
finishes by season, game_log). Playoffs = winners bracket incl. 3rd/5th-place games, no consolation;
a two-week round is one game but its PF/PA are two-week totals (see `meta.career_split`).
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
(owner_id null) is labeled **Deion**. 2024 roster 11 (Sleeper owner now matkinson94) is
**Aaron** (see `season_owners` in `data/sources/owner_overrides.json`). Jake’s blank Sleeper team name is
shown as **Drought Ends Here**.

## Fallback cache

If the API is unreachable, the script falls back to:

- `/workspace/flex-numbers-2026/api/` (2026 snapshots)
- `/workspace/flex-appeal/sleeper/history/` (past seasons + career CSVs)

## X score cards

```bash
python3 scripts/make_score_card.py --label TNF --out /workspace/score-cards/week4-tnf.png
python3 scripts/make_score_card.py --week 4 --label "MNF final" --out /workspace/score-cards/week4-final.png
python3 scripts/make_score_card.py --label SNF --format square --out /workspace/score-cards/week4-snf-sq.png
```

1600x900 PNG (or `--format square`, 1080x1350) for @FlexAppealFFL in-game posts, built from
Sleeper's public API: all six matchups (leader in red; W/L/T badges and median result when the
label contains "final" or `--final`), the weekly median and how many teams are above it, and the
top starting-lineup scorers. Live cards only show matchups where a starter on either team has
played (ESPN game state; falls back to nonzero points) and note the rest as "yet to kick off";
final cards show all six. The grid re-flows for 1-6 matchups. Week defaults to Sleeper `state/nfl`. "To play" counts come from
ESPN's public scoreboard (skip with `--no-schedule`). Needs Pillow; uses the site's fonts
(Barlow Condensed, Outfit), downloaded from google/fonts into `/workspace/cache/fonts` if they
aren't installed. The Sleeper players file is cached at `/workspace/cache/sleeper/players_nfl.json`
and refreshed at most daily. Generated images and caches stay out of the repo.

# Flex Appeal FFL — site data

JSON feeds for the Flex Appeal FFL website, rebuilt from the public
[Sleeper API](https://docs.sleeper.com/) plus local history under
`/workspace/flex-appeal/sleeper/history`.

## Quick start

```bash
cd /workspace/flex-appeal-data
python3 build_site_data.py
```

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
| `history.json` | Champions, career (through 2026 to date), all-time extremes |
| `teams.json` | Owner, short name, team name, Sleeper handle |

## Flex formula

Lower score is better:

`0.35×standings rank + 0.25×PF rank + 0.10×PD rank + 0.15×prev-week points rank + 0.15×streak rank`

Input ties use average ranks. Final whole-number ranks break ties by higher PF, then higher PD.

Sheet / board convention matches the Numbers workbook: **Week N** = ranking
heading into week N, using results through week N−1.

**Week 3 board is locked** (posted movements). Later weeks compute movement
vs the previous computed board.

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

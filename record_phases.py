"""Shared helpers: which weekly games count for the record books.

Records (single-week highs/lows, blowouts, closest games, career / season high and low
weeks, best individual starts) count regular-season games and real playoff-bracket games
only: every Sleeper winners-bracket game, including the placement games inside it
(3rd-place game, 5th-place game, 5th-place semifinal). Consolation games (losers bracket /
toilet bowl, and any playoff-week pairing of teams outside the winners bracket) are
excluded. ESPN 2022 uses its tier labels the same way (WINNERS_BRACKET and the
WINNERS_CONSOLATION_LADDER 3rd-place game count; LOSERS_CONSOLATION_LADDER does not).

Used by build_site_data.py, apply_history_overrides.py and build_all_time_stats.py so
the Tuesday rebuild keeps the same rule everywhere. Stdlib only.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

RECORD_PHASES = ("regular", "playoff")  # "consolation" never counts for records
ESPN_TIER_PHASE = {"NONE": "regular", "WINNERS_BRACKET": "playoff", "WINNERS_CONSOLATION_LADDER": "playoff"}


def counts_for_records(phase: str | None) -> bool:
    return phase in RECORD_PHASES


def playoff_rounds(settings: dict, n_rounds: int) -> dict[int, list[int]]:
    """Winners-bracket round -> NFL weeks (same rule as build_h2h.playoff_rounds)."""
    start = int(settings.get("playoff_week_start") or 15)
    rtype = int(settings.get("playoff_round_type") or 0)  # 0: 1 wk/round, 1: 2-wk final, 2: 2 wks/round
    rounds, wk = {}, start
    for r in range(1, n_rounds + 1):
        length = 2 if rtype == 2 or (rtype == 1 and r == n_rounds) else 1
        rounds[r] = list(range(wk, wk + length))
        wk += length
    return rounds


def winners_bracket_by_week(settings: dict, winners_bracket: list) -> dict[int, set]:
    """Week -> roster ids playing a winners-bracket game that week."""
    wb = winners_bracket or []
    rounds = playoff_rounds(settings, max([g["r"] for g in wb] or [0]))
    out: dict[int, set] = defaultdict(set)
    for g in wb:
        for w in rounds.get(g["r"], []):
            out[w] |= {g.get("t1"), g.get("t2")} - {None}
    return out


def sleeper_phase(week: int, roster_id, settings: dict, in_wb: dict[int, set]) -> str:
    """'regular' | 'playoff' (winners bracket incl. placement games) | 'consolation'."""
    if week < int(settings.get("playoff_week_start") or 15):
        return "regular"
    return "playoff" if roster_id in in_wb.get(week, set()) else "consolation"


def history_consolation_games(history_dir: Path) -> set[tuple[int, int, str]]:
    """(season, week, matchup_id) of every consolation game in the local Sleeper history
    snapshots (league-*.json, winners_bracket-*.json, matchups-*-w*.json)."""
    out: set[tuple[int, int, str]] = set()
    for lf in sorted(history_dir.glob("league-*.json")):
        lid = lf.stem.split("-", 1)[1]
        wbf = history_dir / f"winners_bracket-{lid}.json"
        if not wbf.exists():
            continue
        lg = json.loads(lf.read_text())
        s, season = lg.get("settings") or {}, int(lg.get("season"))
        in_wb = winners_bracket_by_week(s, json.loads(wbf.read_text()))
        for mf in history_dir.glob(f"matchups-{lid}-w*.json"):
            w = int(re.search(r"-w(\d+)\.json$", mf.name).group(1))
            for m in json.loads(mf.read_text()) or []:
                if m.get("matchup_id") is not None and sleeper_phase(w, m["roster_id"], s, in_wb) == "consolation":
                    out.add((season, w, str(m["matchup_id"])))
    return out

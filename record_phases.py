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


# history.json owner names for 2022 ESPN manager ids still in the league (full names).
# Legacy Managers (Jared, Deion, Jake F, Mike, …) come from display_names.json legacy_owners.
ESPN_CURRENT_HISTORY_NAMES = {
    "jake": "Jake Prosser",
    "vlad": "Vlad Barber",
    "lij": "Elijah Bruette",
    "doug": "Douglas Sullivan",
}
ESPN_2022_GAMES = Path(__file__).resolve().parent / "data" / "sources" / "espn_2022_games.json"
DISPLAY_NAMES_FILE = Path(__file__).resolve().parent / "data" / "sources" / "display_names.json"
RECORDS_NOTE = (
    "Record lists cover 2022–2026 and count regular-season and playoff-bracket games only "
    "(winners bracket incl. 3rd/5th-place games); consolation / toilet-bowl games are excluded. "
    "* marks a playoff game."
)
PLAYOFF_NOTE = "Playoff game"


def espn_history_names() -> dict[str, str]:
    """ESPN manager id -> history.json owner name (full name now, first name if Legacy)."""
    names = dict(ESPN_CURRENT_HISTORY_NAMES)
    if DISPLAY_NAMES_FILE.exists():
        names.update(json.loads(DISPLAY_NAMES_FILE.read_text()).get("legacy_owners", {}))
    return names


def annotate_playoff(entry: dict, playoff: bool | None = None) -> dict:
    """Copy a record-list entry with playoff true/false and note='Playoff game' when true."""
    if playoff is None:
        playoff = bool(entry.get("playoff"))
    playoff = bool(playoff)
    skip = {"playoff", "note"}
    out = {k: v for k, v in entry.items() if k not in skip}
    out["playoff"] = playoff
    if playoff:
        out["note"] = PLAYOFF_NOTE
    return out


def record_score_entry(season, week, owner, points, opponent, playoff) -> dict:
    return annotate_playoff(
        {"season": int(season), "week": int(week), "owner": owner, "points": points, "opponent": opponent},
        playoff,
    )


def record_game_entry(season, week, owner, points, opponent, opponent_points, margin, playoff) -> dict:
    return annotate_playoff(
        {
            "season": int(season),
            "week": int(week),
            "owner": owner,
            "points": points,
            "opponent": opponent,
            "opponent_points": opponent_points,
            "margin": margin,
        },
        playoff,
    )


def _espn_2022_record_pairs() -> list[dict]:
    """Record-eligible 2022 weekly pairings (regular season + winners bracket, no consolation)."""
    if not ESPN_2022_GAMES.exists():
        return []
    sd = json.loads(ESPN_2022_GAMES.read_text())
    names = espn_history_names()
    out = []
    for p in sd.get("weekly_pairs") or []:
        phase = ESPN_TIER_PHASE.get(p["tier"], "consolation")
        if not counts_for_records(phase):
            continue
        try:
            a, b = names[p["a"]], names[p["b"]]
        except KeyError as exc:
            raise KeyError(f"No history.json display name for 2022 ESPN manager {exc}") from exc
        out.append({
            "season": 2022,
            "week": int(p["week"]),
            "a": a,
            "b": b,
            "a_pts": p["a_pts"],
            "b_pts": p["b_pts"],
            "playoff": phase == "playoff",
        })
    return out


def espn_2022_record_scores() -> list[dict]:
    """2022 team-weeks for all_time_high_scores / all_time_low_scores."""
    rows = []
    for p in _espn_2022_record_pairs():
        rows.append(record_score_entry(p["season"], p["week"], p["a"], p["a_pts"], p["b"], p["playoff"]))
        rows.append(record_score_entry(p["season"], p["week"], p["b"], p["b_pts"], p["a"], p["playoff"]))
    return rows


def espn_2022_record_games() -> list[dict]:
    """2022 winner-centric games for biggest_blowouts / closest_games. 2022 had no ties."""
    rows = []
    for p in _espn_2022_record_pairs():
        if p["a_pts"] == p["b_pts"]:
            continue
        if p["a_pts"] > p["b_pts"]:
            owner, pts, opp, opp_pts = p["a"], p["a_pts"], p["b"], p["b_pts"]
        else:
            owner, pts, opp, opp_pts = p["b"], p["b_pts"], p["a"], p["a_pts"]
        rows.append(record_game_entry(
            p["season"], p["week"], owner, pts, opp, opp_pts, round(abs(pts - opp_pts), 2), p["playoff"],
        ))
    return rows

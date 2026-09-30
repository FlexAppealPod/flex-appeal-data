#!/usr/bin/env python3
"""Merge manual rulings into data/history.json.

build_site_data.py only knows Sleeper seasons (2023+), so run this right after it:

    python3 build_site_data.py && python3 apply_history_overrides.py

1. data/sources/champions_manual.json: keeps the champions schema (season / champion /
   runner_up) and adds co_champions, note and platform.
2. data/sources/score_overrides.json: official standings totals for weeks where Sleeper's
   matchup data under-counts (2024 Week 7). Rebuilds all_time_high_scores,
   all_time_low_scores, biggest_blowouts and closest_games from the Sleeper history
   team_games.csv with those totals (2026 entries are kept as built), and moves the
   career H2H W/L for any game whose winner flips. build_site_data.py rewrites
   history.json from scratch every run; the "score_overrides_applied" key marks a file
   that is already patched, so this stays idempotent.
Stdlib only.
"""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "data" / "history.json"
MANUAL = ROOT / "data" / "sources" / "champions_manual.json"
SCORE_OVERRIDES = ROOT / "data" / "sources" / "score_overrides.json"
SLEEPER_HISTORY = Path("/workspace/flex-appeal/sleeper/history")  # same source build_site_data.py reads
LIST_LEN = {"all_time_high_scores": 15, "all_time_low_scores": 10, "biggest_blowouts": 15, "closest_games": 15}


def apply_score_overrides(hist: dict) -> None:
    tg_path = SLEEPER_HISTORY / "team_games.csv"
    if not SCORE_OVERRIDES.exists() or not tg_path.exists():
        return
    if hist.get("score_overrides_applied"):
        print("history.json score overrides already applied")
        return
    ovs = json.loads(SCORE_OVERRIDES.read_text())["overrides"]
    rows = list(csv.DictReader(tg_path.open(newline="", encoding="utf-8")))
    games = []
    for r in rows:
        g = {"season": int(r["season"]), "week": int(r["week"]), "playoff": r["playoff_week"] != "0", "mid": r["matchup_id"],
             "owner": r["manager"], "points": float(r["points"]), "opponent": r["opponent"], "opponent_points": float(r["opp_points"])}
        g["orig_win"] = g["points"] > g["opponent_points"]
        games.append(g)
    applied = []
    for o in ovs:
        hit = False
        for g in games:
            if (g["season"], g["week"], g["mid"]) != (int(o["season"]), int(o["week"]), str(o["matchup_id"])):
                continue
            if abs(g["points"] - o["matchup_points"]) < 0.005:
                g["points"], hit = o["official_points"], True
            elif abs(g["opponent_points"] - o["matchup_points"]) < 0.005:
                g["opponent_points"] = o["official_points"]
        if hit:
            applied.append(f"{o['season']} wk{o['week']} matchup {o['matchup_id']}: {o['matchup_points']} -> {o['official_points']}")
    if not applied:
        return
    for g in games:
        g["margin"] = round(abs(g["points"] - g["opponent_points"]), 2)
    keep26 = {k: [e for e in hist.get(k, []) if int(e["season"]) > max(g["season"] for g in games)] for k in LIST_LEN}
    team = lambda g: {"season": g["season"], "week": g["week"], "owner": g["owner"], "points": g["points"], "opponent": g["opponent"]}  # noqa: E731
    pair = lambda g: {**team(g), "opponent_points": g["opponent_points"], "margin": g["margin"]}  # noqa: E731
    winners = [g for g in games if g["points"] > g["opponent_points"]]
    new = {
        "all_time_high_scores": sorted([team(g) for g in games] + keep26["all_time_high_scores"], key=lambda e: e["points"], reverse=True),
        "all_time_low_scores": sorted([team(g) for g in games] + keep26["all_time_low_scores"], key=lambda e: e["points"]),
        "biggest_blowouts": sorted([pair(g) for g in winners] + keep26["biggest_blowouts"], key=lambda e: e["margin"], reverse=True),
        "closest_games": sorted([pair(g) for g in winners] + keep26["closest_games"], key=lambda e: e["margin"]),
    }
    for k, n in LIST_LEN.items():
        hist[k] = new[k][:n]
    # career H2H (regular season) for flipped results
    career = {c["owner"]: c for c in hist.get("career", [])}
    for g in games:
        if g["playoff"] or (g["points"] > g["opponent_points"]) == g["orig_win"]:
            continue
        c = career.get(g["owner"])
        if c:
            d = 1 if g["points"] > g["opponent_points"] else -1
            c["wins"] += d
            c["losses"] -= d
            c["win_pct"] = round(c["wins"] / (c["wins"] + c["losses"]), 2) if c["wins"] + c["losses"] else 0.0
            applied.append(f"career {g['owner']}: {'+1 W' if d > 0 else '+1 L'} ({g['season']} wk{g['week']})")
    hist["score_overrides_applied"] = applied
    print("history.json score overrides:", applied)


def main() -> None:
    hist = json.loads(HISTORY.read_text())
    manual = json.loads(MANUAL.read_text())["seasons"]
    champs = [c for c in hist.get("champions", []) if str(c.get("season")) not in manual]
    for season, m in manual.items():
        entry = {"season": int(season), "champion": m["champion"], "runner_up": m["runner_up"]}
        for k in ("co_champions", "note", "platform"):
            if k in m:
                entry[k] = m[k]
        champs.append(entry)
    hist["champions"] = sorted(champs, key=lambda c: int(c["season"]))
    apply_score_overrides(hist)
    HISTORY.write_text(json.dumps(hist, indent=2, ensure_ascii=False) + "\n")
    print("history.json champions:", [(c["season"], c["champion"]) for c in hist["champions"]])


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Merge manual rulings into data/history.json.

build_site_data.py only knows Sleeper seasons (2023+), so run this right after it:

    python3 build_site_data.py && python3 apply_history_overrides.py

1. data/sources/champions_manual.json: keeps the champions schema (season / champion /
   runner_up) and adds co_champions, note and platform.
3. data/sources/display_names.json: Legacy Owner display names (Deion, Jake F, Jared,
   Mike, Anup, Matt F) for every owner/opponent/champion field, and manual co-championships
   counted in career championships. Current managers keep their full names here (the site's
   team pages match history.json to teams.json owners by full name).
2. data/sources/score_overrides.json: official standings totals for weeks where Sleeper's
   matchup data under-counts (2024 Week 7). Rebuilds all_time_high_scores,
   all_time_low_scores, biggest_blowouts and closest_games from the Sleeper history
   team_games.csv with those totals (2026 entries are kept as built), and moves the
   career H2H W/L for any game whose winner flips. build_site_data.py rewrites
   history.json from scratch every run; the "score_overrides_applied" key marks a file
   that is already patched, so this stays idempotent.
4. data/sources/owner_overrides.json "season_owners": whole-season roster owner fixes. Sleeper
   (and so the Sleeper history CSVs) credits a roster to its CURRENT owner, so a roster handed
   to a new manager after the season shows him for the old season too (2024 roster 11 = Aaron,
   not Matt Atkinson). Moves that season's career line (seasons, H2H W/L, PF, playoffs, titles,
   from manager_seasons.csv) to the true owner and renames that season's owner/opponent in the
   extremes lists. The "season_owner_fixes_applied" key keeps the career move idempotent.
Stdlib only.
"""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "data" / "history.json"
MANUAL = ROOT / "data" / "sources" / "champions_manual.json"
SCORE_OVERRIDES = ROOT / "data" / "sources" / "score_overrides.json"
DISPLAY_NAMES = ROOT / "data" / "sources" / "display_names.json"
OWNER_OVERRIDES = ROOT / "data" / "sources" / "owner_overrides.json"
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


def apply_season_owner_fixes(hist: dict) -> None:
    """Whole-season owner fixes (owner_overrides.json season_owners) for history.json."""
    ms_path = SLEEPER_HISTORY / "manager_seasons.csv"
    if not OWNER_OVERRIDES.exists() or not ms_path.exists():
        return
    fixes = json.loads(OWNER_OVERRIDES.read_text()).get("season_owners", [])
    if not fixes:
        return
    legacy = json.loads(DISPLAY_NAMES.read_text()).get("legacy_owners", {}) if DISPLAY_NAMES.exists() else {}
    ms_rows = list(csv.DictReader(ms_path.open(newline="", encoding="utf-8")))
    already = bool(hist.get("season_owner_fixes_applied"))
    applied = []
    for o in fixes:
        season = int(o["season"])
        row = next((r for r in ms_rows if int(r["season"]) == season and int(r["roster_id"]) == int(o["roster_id"])), None)
        if row is None:
            continue
        old = row["manager"]  # name the Sleeper history credits (the roster's current owner)
        new = o.get("display_name") or legacy.get(o["manager"]) or o.get("full_name") or o["manager"]
        if old == new or row.get("user_id") == o["sleeper_user_id"]:
            continue
        for k in LIST_LEN:
            for e in hist.get(k, []):
                if int(e.get("season", 0)) == season:
                    for f in ("owner", "opponent"):
                        if e.get(f) == old:
                            e[f] = new
        for c in hist.get("champions", []):
            if int(c.get("season", 0)) == season:
                for f in ("champion", "runner_up"):
                    if c.get(f) == old:
                        c[f] = new
        if already:
            continue
        w, l_ = (int(x) for x in row["h2h_reg_record"].split("-")[:2])
        pf, playoff, champ = float(row["pf_settings"]), int(row["playoff"] or 0), int(row["champion"] or 0)
        career = {c["owner"]: c for c in hist.get("career", [])}
        if new not in career:
            career[new] = {"owner": new, "seasons": 0, "wins": 0, "losses": 0, "win_pct": 0.0, "total_pf": 0.0,
                           "championships": 0, "playoff_appearances": 0}
            hist.setdefault("career", []).append(career[new])
        for name_, sign in ((old, -1), (new, 1)):
            c = career.get(name_)
            if not c:
                continue
            c["seasons"] += sign
            c["wins"] += sign * w
            c["losses"] += sign * l_
            c["total_pf"] = round(c["total_pf"] + sign * pf, 2)
            c["playoff_appearances"] += sign * playoff
            c["championships"] += sign * champ
            c["win_pct"] = round(c["wins"] / (c["wins"] + c["losses"]), 2) if c["wins"] + c["losses"] else 0.0
        applied.append(f"{season} roster {o['roster_id']}: {old} -> {new} ({row['h2h_reg_record']} H2H, PF {pf:.2f}, playoffs {playoff})")
    if applied:
        hist["career"].sort(key=lambda r: (-r["total_pf"], -r["wins"], r["owner"]))
        hist["season_owner_fixes_applied"] = applied
        print("history.json season owner fixes:", applied)
    elif already:
        print("history.json season owner fixes already applied")


def apply_display_names(hist: dict) -> None:
    """Legacy Owner display names (data/sources/display_names.json -> history_json_aliases).

    Renames owner / opponent / champion / runner_up / co_champions everywhere in history.json and
    makes sure career championships count manual co-championships (2022). Idempotent.
    """
    if not DISPLAY_NAMES.exists():
        return
    alias = json.loads(DISPLAY_NAMES.read_text()).get("history_json_aliases", {}).get("names", {})
    ren = lambda n: alias.get(n, n) if isinstance(n, str) else n  # noqa: E731
    for k in ("career", "all_time_high_scores", "all_time_low_scores", "biggest_blowouts", "closest_games"):
        for e in hist.get(k, []):
            for f in ("owner", "opponent"):
                if f in e:
                    e[f] = ren(e[f])
    for c in hist.get("champions", []):
        c["champion"], c["runner_up"] = ren(c.get("champion")), ren(c.get("runner_up"))
        if c.get("co_champions"):
            c["co_champions"] = [ren(n) for n in c["co_champions"]]
    if hist.get("score_overrides_applied"):
        out = []
        for line in hist["score_overrides_applied"]:
            for old, new in alias.items():
                line = line.replace(f"career {old}:", f"career {new}:")
            out.append(line)
        hist["score_overrides_applied"] = out
    for row in hist.get("career", []):
        titles = sum(1 for c in hist.get("champions", []) if row["owner"] == c.get("champion") or row["owner"] in (c.get("co_champions") or []))
        if titles > row.get("championships", 0):
            row["championships"] = titles


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
    apply_season_owner_fixes(hist)
    apply_display_names(hist)
    HISTORY.write_text(json.dumps(hist, indent=2, ensure_ascii=False) + "\n")
    print("history.json champions:", [(c["season"], c["champion"]) for c in hist["champions"]])


if __name__ == "__main__":
    main()

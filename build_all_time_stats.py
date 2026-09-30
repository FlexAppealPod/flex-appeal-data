#!/usr/bin/env python3
"""Build data/all_time_stats.json: all-time Flex Appeal FFL stats, 2022 (ESPN) to date (Sleeper).

Run from /workspace/flex-appeal-data AFTER build_h2h.py (it reuses the manager registry,
ids, display names and title games from data/h2h_all_time.json):

    python3 build_h2h.py && python3 build_all_time_stats.py

Rules
- Keyed by manager (same ids / names / Legacy Owners label as build_h2h.py), incl. the
  mid-season owner overrides in data/sources/owner_overrides.json (2024 roster 2:
  Mike Dewey Weeks 1-7, Matt Z from Week 8).
- 2022 comes from data/sources/espn_2022_games.json (processed ESPN data; raw dumps are
  never committed). ESPN 2022 had no median game and no player-level data here, so
  2022 TDs / yards / player starts are null (N/A).
- Regular-season record = H2H + weekly median where the league used it (2023+ on Sleeper:
  league_average_match = 1). A median win = beating the median of all scores that week.
- PF / PA / PPG / all-play = regular season only. Playoff results come from the H2H game
  list (a two-week round is one game on combined score).
- Single-week records use every real weekly pairing (regular season, playoff legs and
  consolation legs), the same convention as history.json.
- Starter TDs / yards reuse build_team_stats.py (starters only, weeks started, regular
  season only). Scoring differs by season (see meta.scoring_by_season), so compare
  per-game numbers and read cross-season records with that in mind.
- Partial seasons (in-progress season, or a mid-season owner change) are shown but are
  not eligible for season records or best/worst seasons.

Stdlib only. Standalone (does not import build_site_data.py).
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import build_h2h as H
from build_team_stats import td_breakdown, yard_breakdown

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "all_time_stats.json"
H2H_FILE = ROOT / "data" / "h2h_all_time.json"
TEAMS_FILE = ROOT / "data" / "teams.json"
CACHE = Path("/workspace/flex-alltime-cache")  # Sleeper responses for completed seasons; not committed
PLAYERS_CACHE = Path("/workspace/players_nfl.json")  # shared with build_team_stats.py; not committed
TOP = 10

SCORING_NOTES = {
    "2022": {"platform": "ESPN", "ppr": 0.5, "idp": False, "median": False,
             "lineup": "QB, 2 RB, 2 WR, TE, FLEX, D/ST, K"},
}


def fetch(path: str, cacheable: bool):
    """Sleeper GET with a local cache. Completed seasons read the cache first."""
    local = CACHE / (path.replace("/", "_") + ".json")
    if cacheable and local.exists():
        return json.loads(local.read_text())
    data = H.get_json(path)
    if data is not None:
        CACHE.mkdir(parents=True, exist_ok=True)
        local.write_text(json.dumps(data))
        return data
    return json.loads(local.read_text()) if local.exists() else None


def rec():
    return {"w": 0, "l": 0, "t": 0}


def add(r, res):
    r[res] += 1


def pct(r):
    g = r["w"] + r["l"] + r["t"]
    return round((r["w"] + 0.5 * r["t"]) / g, 3) if g else None


def fmt(r):
    return f"{r['w']}-{r['l']}" + (f"-{r['t']}" if r["t"] else "")


def res_of(a, b):
    return "w" if a > b else ("l" if a < b else "t")


def r2(x):
    return round(x + 0.0, 2)


def main() -> None:
    h2h = json.loads(H2H_FILE.read_text())
    reg = {m["id"]: m for m in h2h["managers"]}
    uid2id = {m["sleeper_user_id"]: m["id"] for m in h2h["managers"] if m.get("sleeper_user_id")}
    handle_full = {t["sleeper_handle"].lower(): t["owner"] for t in json.loads(TEAMS_FILE.read_text())} if TEAMS_FILE.exists() else {}
    overrides = json.loads(H.OVERRIDES_FILE.read_text())["overrides"] if H.OVERRIDES_FILE.exists() else []
    name = lambda i: reg[i]["name"] if i in reg else i  # noqa: E731
    state = H.get_json("state/nfl") or {}

    rows: dict[tuple, dict] = {}      # (season, manager) -> season row
    week_scores: list[dict] = []      # every real weekly score
    week_games: list[dict] = []       # every real weekly pairing
    player_starts: list[dict] = []
    scoring_by_season: list[dict] = []
    checks: list[str] = []
    problems: list[str] = []
    through = None

    def row(season, mid, platform):
        k = (season, mid)
        if k not in rows:
            rows[k] = {"season": season, "manager": mid, "name": name(mid), "platform": platform,
                       "weeks_managed": None, "partial": False, "in_progress": False,
                       "games": 0, "h2h": rec(), "median": None, "record": rec(),
                       "pf": 0.0, "pa": 0.0, "all_play": rec(),
                       "high_week": None, "low_week": None,
                       "starter_tds": None, "starter_yards": None, "td_breakdown": None,
                       "playoffs": False, "finish": None, "playoff_record": rec(), "sleeper_standings": None,
                       "record_note": None}
        return rows[k]

    def note_week(r, season, week, pts, opp_id, opp_pts, phase):
        e = {"season": season, "week": week, "manager": r["manager"], "name": r["name"], "points": r2(pts),
             "opponent": opp_id, "opponent_name": name(opp_id), "opponent_points": r2(opp_pts), "phase": phase}
        week_scores.append(e)
        if r["high_week"] is None or pts > r["high_week"]["points"]:
            r["high_week"] = {"week": week, "points": r2(pts), "opponent": name(opp_id), "phase": phase}
        if r["low_week"] is None or pts < r["low_week"]["points"]:
            r["low_week"] = {"week": week, "points": r2(pts), "opponent": name(opp_id), "phase": phase}

    # ---------------- 2022 (ESPN, processed source) ----------------
    for src in sorted(H.SOURCES.glob("espn_*_games.json")):
        sd = json.loads(src.read_text())
        season = sd["season_format"]["season"]
        n = SCORING_NOTES.get(season, {})
        scoring_by_season.append({"season": season, "platform": "ESPN", "ppr": n.get("ppr"), "idp": n.get("idp", False),
                                  "median_game": False, "lineup": sd.get("lineup") or n.get("lineup"),
                                  "teams": sd["season_format"]["teams"], "regular_weeks": sd["season_format"]["regular_weeks"],
                                  "scoring": sd["season_format"].get("scoring")})
        reg_last = int(sd["season_format"]["regular_weeks"].split("-")[1])
        tier_phase = {"NONE": "regular", "WINNERS_BRACKET": "playoff", "WINNERS_CONSOLATION_LADDER": "playoff"}
        by_week = defaultdict(list)
        for p in sd["weekly_pairs"]:
            phase = tier_phase.get(p["tier"], "consolation")
            ra, rb = row(season, p["a"], "ESPN"), row(season, p["b"], "ESPN")
            note_week(ra, season, p["week"], p["a_pts"], p["b"], p["b_pts"], phase)
            note_week(rb, season, p["week"], p["b_pts"], p["a"], p["a_pts"], phase)
            week_games.append({"season": season, "week": p["week"], "phase": phase, "a": p["a"], "b": p["b"],
                               "a_pts": p["a_pts"], "b_pts": p["b_pts"]})
            if p["week"] <= reg_last:
                for r, me, op in ((ra, p["a_pts"], p["b_pts"]), (rb, p["b_pts"], p["a_pts"])):
                    r["games"] += 1; r["pf"] += me; r["pa"] += op
                    add(r["h2h"], res_of(me, op)); add(r["record"], res_of(me, op))
                by_week[p["week"]] += [(p["a"], p["a_pts"]), (p["b"], p["b_pts"])]
        for w, sc in by_week.items():
            for mid, pts in sc:
                for mid2, pts2 in sc:
                    if mid2 != mid:
                        add(rows[(season, mid)]["all_play"], res_of(pts, pts2))
        for t in sd["teams"]:  # sanity vs ESPN standings
            r = rows[(season, t["manager"])]
            ok = (r["h2h"]["w"], r["h2h"]["l"]) == (t["wins"], t["losses"]) and abs(r["pf"] - t["points_for"]) < 0.01 and abs(r["pa"] - t["points_against"]) < 0.01
            (checks if ok else problems).append(f"{season} {t['manager']}: {fmt(r['h2h'])} PF {r2(r['pf'])} PA {r2(r['pa'])} vs ESPN {t['wins']}-{t['losses']} PF {t['points_for']} PA {t['points_against']}")

    # ---------------- Sleeper seasons ----------------
    leagues = H.discover_leagues()
    players = json.loads(PLAYERS_CACHE.read_text()) if PLAYERS_CACHE.exists() else (H.get_json("players/nfl") or {})
    for season, lid in leagues.items():
        lg = fetch(f"league/{lid}", False)
        complete = lg.get("status") == "complete"
        s = lg["settings"]
        users = fetch(f"league/{lid}/users", complete) or []
        rosters = fetch(f"league/{lid}/rosters", complete) or []
        wb = fetch(f"league/{lid}/winners_bracket", complete) or []
        rid2uid = {r["roster_id"]: r.get("owner_id") for r in rosters}
        last_leg = int(s.get("last_scored_leg") or 0)
        if not complete and str(state.get("season")) == season:
            last_leg = min(last_leg, max(int(state.get("week") or 1) - 1, 0))
        pstart = int(s.get("playoff_week_start") or 15)
        n_rounds = max([g["r"] for g in wb] or [0])
        rounds = H.playoff_rounds(s, n_rounds)
        # Median game: on only if Sleeper's standings actually count it (2023 has the flag set
        # but its standings are H2H only: 13 results for 13 weeks).
        done_reg = len([w for w in range(1, pstart) if w <= last_leg])
        st_games = max(((r.get("settings") or {}).get("wins", 0) + (r.get("settings") or {}).get("losses", 0)
                        + (r.get("settings") or {}).get("ties", 0)) for r in rosters) if rosters else 0
        median_on = int(s.get("league_average_match") or 0) == 1 and st_games > done_reg
        pos = lg.get("roster_positions") or []
        sc = lg.get("scoring_settings") or {}
        scoring_by_season.append({"season": season, "platform": "Sleeper", "ppr": sc.get("rec"),
                                  "idp": any(p.startswith("IDP") or p in ("DL", "LB", "DB") for p in pos),
                                  "median_game": median_on,
                                  "lineup": ", ".join(p for p in pos if p != "BN"),
                                  "teams": int(s.get("num_teams") or len(rosters)),
                                  "regular_weeks": f"1-{pstart - 1}", "status": lg.get("status"),
                                  "completed_through_week": last_leg})
        through = {"season": season, "week": last_leg}
        max_week = min(last_leg, 17)
        in_wb = defaultdict(set)  # week -> roster ids in a winners-bracket game that week
        for g in wb:
            for w in rounds.get(g["r"], []):
                in_wb[w] |= {g.get("t1"), g.get("t2")}

        def mgr(rid, week):
            for o in overrides:
                if o["season"] == season and o["roster_id"] == rid and o["weeks"][0] <= week <= o["weeks"][1]:
                    return o["manager"]
            uid = rid2uid.get(rid)
            if uid in uid2id:
                return uid2id[uid]
            return H.slug(H.UNOWNED_LABELS.get((season, rid), f"roster {season}-{rid}"))

        roster_pf = defaultdict(float)
        roster_rec = defaultdict(rec)
        for w in range(1, max_week + 1):
            ms = fetch(f"league/{lid}/matchups/{w}", complete) or []
            regular = w < pstart
            stats = fetch(f"stats/nfl/regular/{season}/{w}", complete) if regular else None
            pairs = defaultdict(list)
            for m in ms:
                if m.get("matchup_id") is not None:
                    pairs[m["matchup_id"]].append(m)
            wk_scores = [m["points"] or 0 for pr in pairs.values() for m in pr]
            med = statistics.median(wk_scores) if wk_scores else 0
            for pr in pairs.values():
                if len(pr) != 2:
                    problems.append(f"{season} wk{w}: matchup with {len(pr)} rosters skipped")
                    continue
                a, b = pr
                phase = "regular" if regular else ("playoff" if a["roster_id"] in in_wb[w] else "consolation")
                ida, idb = mgr(a["roster_id"], w), mgr(b["roster_id"], w)
                week_games.append({"season": season, "week": w, "phase": phase, "a": ida, "b": idb,
                                   "a_pts": r2(a["points"] or 0), "b_pts": r2(b["points"] or 0)})
                for me, op, mid, oid in ((a, b, ida, idb), (b, a, idb, ida)):
                    r = row(season, mid, "Sleeper")
                    mp, opp = me["points"] or 0, op["points"] or 0
                    note_week(r, season, w, mp, oid, opp, phase)
                    for pid, pp in zip(me.get("starters") or [], me.get("starters_points") or []):
                        if pid and pid != "0":
                            pl = players.get(pid) or {}
                            player_starts.append({"season": season, "week": w, "manager": mid, "name": r["name"],
                                                  "player": pl.get("full_name") or (f"{pid} DEF" if not pid.isdigit() else pid),
                                                  "position": pl.get("position") or ("DEF" if not pid.isdigit() else None),
                                                  "points": r2(pp), "phase": phase})
                    if not regular:
                        continue
                    r["games"] += 1; r["pf"] += mp; r["pa"] += opp
                    roster_pf[me["roster_id"]] += mp
                    hres = res_of(mp, opp)
                    add(r["h2h"], hres); add(r["record"], hres); add(roster_rec[me["roster_id"]], hres)
                    if median_on:
                        if r["median"] is None:
                            r["median"] = rec()
                        mres = res_of(mp, med)
                        add(r["median"], mres); add(r["record"], mres); add(roster_rec[me["roster_id"]], mres)
                    ap = Counter(res_of(mp, x) for x in wk_scores)
                    ap["t"] -= 1  # self
                    for k2 in ("w", "l", "t"):
                        r["all_play"][k2] += ap[k2]
                    if stats is not None:
                        if r["starter_tds"] is None:
                            r["starter_tds"], r["starter_yards"] = 0, 0
                            r["td_breakdown"] = Counter({"pass": 0, "rush": 0, "rec": 0, "idp": 0, "return": 0})
                        for pid in me.get("starters") or []:
                            if pid and pid != "0":
                                st = stats.get(pid) or {}
                                t, y = td_breakdown(st), yard_breakdown(st)
                                r["td_breakdown"].update(t)
                                r["starter_tds"] += sum(t.values())
                                r["starter_yards"] += sum(y.values())
        # sanity vs Sleeper standings (roster settings include median wins when used)
        split_rids = {o["roster_id"] for o in overrides if o["season"] == season}
        for rr in rosters:
            st = rr.get("settings") or {}
            fpts = r2((st.get("fpts") or 0) + (st.get("fpts_decimal") or 0) / 100)
            fpa = r2((st.get("fpts_against") or 0) + (st.get("fpts_against_decimal") or 0) / 100)
            mine = roster_rec[rr["roster_id"]]
            ok_pf = abs(roster_pf[rr["roster_id"]] - fpts) < 0.02
            ok_rec = (mine["w"], mine["l"], mine["t"]) == (st.get("wins", 0), st.get("losses", 0), st.get("ties", 0))
            off = {"record": f"{st.get('wins', 0)}-{st.get('losses', 0)}" + (f"-{st['ties']}" if st.get("ties") else ""), "pf": fpts, "pa": fpa}
            if rr["roster_id"] in split_rids:
                off["note"] = "whole-roster figure (roster changed hands mid-season)"
            owners_here = {mid for (se, mid) in rows if se == season} & {mgr(rr["roster_id"], w) for w in range(1, max_week + 1)}
            for mid in owners_here:
                rows[(season, mid)]["sleeper_standings"] = off
            msg = (f"{season} roster {rr['roster_id']} ({'/'.join(sorted(name(m) for m in owners_here))}): computed {fmt(mine)}, PF {r2(roster_pf[rr['roster_id']])}"
                   f" | Sleeper standings {off['record']}, PF {fpts}")
            if not ok_rec and rr["roster_id"] not in split_rids and len(owners_here) == 1:
                # Official record = Sleeper standings. H2H stays as recomputed; the median part absorbs the gap.
                r = rows[(season, next(iter(owners_here)))]
                r["record"] = {"w": st.get("wins", 0), "l": st.get("losses", 0), "t": st.get("ties", 0)}
                if r["median"] is not None:
                    r["median"] = {k: r["record"][k] - r["h2h"][k] for k in ("w", "l", "t")}
                r["record_note"] = (f"Official Sleeper standings ({off['record']}) used; recomputing from current matchup scores gives {fmt(mine)}. "
                                    "The gap is in median results only.")
                msg += " -> official standings record used"
                ok_rec = ok_pf = False  # keep it listed for transparency
            (checks if ok_pf and ok_rec else problems).append(msg)
        # flags
        for o in overrides:
            if o["season"] == season:
                r = rows.get((season, o["manager"]))
                if r:
                    r["partial"], r["weeks_managed"] = True, f"Weeks {o['weeks'][0]}-{o['weeks'][1]}"
                if o.get("handover_to") and (season, o["handover_to"]) in rows:
                    r2_ = rows[(season, o["handover_to"])]
                    r2_["partial"], r2_["weeks_managed"] = True, f"Weeks {o['weeks'][1] + 1}-17"
        if not complete:
            for (se, _), r in rows.items():
                if se == season:
                    r["in_progress"] = True

    # ---------------- playoffs, finishes, titles (from the H2H game list) ----------------
    titles_by_season = {t["season"]: t for t in h2h["title_games"]}
    name2id = {m["name"]: m["id"] for m in h2h["managers"]}
    for g in h2h["games"]:
        if g["type"] == "regular":
            continue
        for mid in (g["a"], g["b"]):
            if (g["season"], mid) in rows:
                rows[(g["season"], mid)]["playoffs"] = True
        win, lose = (g["a"], g["b"]) if g["a_pts"] > g["b_pts"] else (g["b"], g["a"])
        add(rows[(g["season"], win)]["playoff_record"], "w"); add(rows[(g["season"], lose)]["playoff_record"], "l")
        fin = {"Championship": ("Champion", "Runner-up"), "3rd-place game": ("3rd", "4th"), "5th-place game": ("5th", "6th")}.get(g["label"])
        if fin:
            rows[(g["season"], win)]["finish"], rows[(g["season"], lose)]["finish"] = fin
    for season, t in titles_by_season.items():
        if t.get("co_champions"):
            for nm in t["co_champions"]:
                rows[(season, name2id[nm])]["finish"] = "Co-champion"
    for r in rows.values():
        if r["finish"] is None:
            r["finish"] = "In progress" if r["in_progress"] else ("Playoffs" if r["playoffs"] else "Missed playoffs")

    # ---------------- finalize season rows ----------------
    season_rows = []
    for r in rows.values():
        g = r["games"]
        r["pf"], r["pa"] = r2(r["pf"]), r2(r["pa"])
        r["ppg"] = r2(r["pf"] / g) if g else None
        r["pa_pg"] = r2(r["pa"] / g) if g else None
        r["record_str"] = fmt(r["record"]); r["h2h_str"] = fmt(r["h2h"])
        r["median_str"] = fmt(r["median"]) if r["median"] else None
        r["record_pct"], r["h2h_pct"], r["all_play_pct"] = pct(r["record"]), pct(r["h2h"]), pct(r["all_play"])
        r["all_play_str"] = fmt(r["all_play"])
        r["playoff_record_str"] = fmt(r["playoff_record"]) if sum(r["playoff_record"].values()) else None
        if r["td_breakdown"] is not None:
            r["td_breakdown"] = {k: int(v) for k, v in r["td_breakdown"].items()}
            r["starter_tds"], r["starter_yards"] = int(r["starter_tds"]), int(r["starter_yards"])
            r["tds_per_game"] = r2(r["starter_tds"] / g) if g else None
        else:
            r["tds_per_game"] = None
        r["eligible_for_records"] = not (r["partial"] or r["in_progress"])
        r["current"] = bool(reg.get(r["manager"], {}).get("current"))
        season_rows.append(r)
    season_rows.sort(key=lambda r: (r["season"], r["name"].lower()))

    # ---------------- career ----------------
    career = []
    by_mgr = defaultdict(list)
    for r in season_rows:
        by_mgr[r["manager"]].append(r)
    best_worst = {}
    for mid, rs in by_mgr.items():
        m = reg.get(mid, {})
        c = {"manager": mid, "name": name(mid), "group": "current" if m.get("current") else "former",
             "full_name": handle_full.get((m.get("sleeper_handle") or "").lower()) or name(mid),
             "seasons": [r["season"] for r in rs], "games": 0, "h2h": rec(), "median": rec(), "record": rec(),
             "pf": 0.0, "pa": 0.0, "all_play": rec(), "playoff_record": rec(),
             "playoff_appearances": sum(r["playoffs"] for r in rs),
             "finals": sum(r["finish"] in ("Champion", "Runner-up", "Co-champion") for r in rs),
             "titles": sum(r["finish"] in ("Champion", "Co-champion") for r in rs),
             "title_seasons": [r["season"] + (" (co)" if r["finish"] == "Co-champion" else "") for r in rs if r["finish"] in ("Champion", "Co-champion")],
             "starter_tds": 0, "starter_yards": 0, "td_games": 0}
        for r in rs:
            c["games"] += r["games"]; c["pf"] += r["pf"]; c["pa"] += r["pa"]
            for k in ("w", "l", "t"):
                c["h2h"][k] += r["h2h"][k]; c["record"][k] += r["record"][k]; c["all_play"][k] += r["all_play"][k]
                c["playoff_record"][k] += r["playoff_record"][k]
                if r["median"]:
                    c["median"][k] += r["median"][k]
            if r["starter_tds"] is not None:
                c["starter_tds"] += r["starter_tds"]; c["starter_yards"] += r["starter_yards"]; c["td_games"] += r["games"]
        hi = max((dict(season=r["season"], **r["high_week"]) for r in rs if r["high_week"]), key=lambda x: x["points"])
        lo = min((dict(season=r["season"], **r["low_week"]) for r in rs if r["low_week"]), key=lambda x: x["points"])
        c.update(pf=r2(c["pf"]), pa=r2(c["pa"]), ppg=r2(c["pf"] / c["games"]) if c["games"] else None,
                 pa_pg=r2(c["pa"] / c["games"]) if c["games"] else None,
                 record_str=fmt(c["record"]), record_pct=pct(c["record"]), h2h_str=fmt(c["h2h"]), h2h_pct=pct(c["h2h"]),
                 median_str=fmt(c["median"]) if sum(c["median"].values()) else None,
                 all_play_str=fmt(c["all_play"]), all_play_pct=pct(c["all_play"]),
                 playoff_record_str=fmt(c["playoff_record"]) if sum(c["playoff_record"].values()) else None,
                 tds_per_game=r2(c["starter_tds"] / c["td_games"]) if c["td_games"] else None,
                 career_high=hi, career_low=lo,
                 full_seasons=sum(r["eligible_for_records"] for r in rs),
                 partial_seasons=[f"{r['season']} ({r['weeks_managed'] or 'in progress'})" for r in rs if not r["eligible_for_records"]])
        if not c["td_games"]:
            c["starter_tds"] = c["starter_yards"] = None
        del c["td_games"]
        career.append(c)
        full = [r for r in rs if r["eligible_for_records"]]
        if full:
            key = lambda r: (r["record_pct"], r["pf"])  # noqa: E731
            b, w_ = max(full, key=key), min(full, key=key)
            slim = lambda r: {k: r[k] for k in ("season", "record_str", "record_pct", "h2h_str", "median_str", "pf", "ppg", "all_play_str", "finish")}  # noqa: E731
            best_worst[mid] = {"name": name(mid), "best": slim(b), "worst": slim(w_), "only_one_full_season": len(full) == 1}
    career.sort(key=lambda c: (c["group"] != "current", -c["pf"]))

    # ---------------- season / week records ----------------
    elig = [r for r in season_rows if r["eligible_for_records"]]
    slim_s = lambda r, *extra: {"season": r["season"], "manager": r["manager"], "name": r["name"], "platform": r["platform"],  # noqa: E731
                                "record": r["record_str"], "pf": r["pf"], "ppg": r["ppg"], "games": r["games"],
                                **{k: r[k] for k in extra}}
    top = lambda seq, key, rev=True, n=5: sorted(seq, key=key, reverse=rev)[:n]  # noqa: E731
    tdrows = [r for r in elig if r["starter_tds"] is not None]
    season_records = {
        "most_pf": [slim_s(r) for r in top(elig, lambda r: r["pf"])],
        "fewest_pf": [slim_s(r) for r in top(elig, lambda r: r["pf"], False)],
        "highest_ppg": [slim_s(r) for r in top(elig, lambda r: r["ppg"])],
        "lowest_ppg": [slim_s(r) for r in top(elig, lambda r: r["ppg"], False)],
        "best_record": [slim_s(r, "record_pct", "h2h_str", "median_str") for r in top(elig, lambda r: (r["record_pct"], r["pf"]))],
        "worst_record": [slim_s(r, "record_pct", "h2h_str", "median_str") for r in top(elig, lambda r: (r["record_pct"], r["pf"]), False)],
        "most_starter_tds": [slim_s(r, "starter_tds", "tds_per_game", "td_breakdown") for r in top(tdrows, lambda r: r["starter_tds"])],
        "most_tds_per_game": [slim_s(r, "starter_tds", "tds_per_game") for r in top(tdrows, lambda r: r["tds_per_game"])],
        "most_starter_yards": [slim_s(r, "starter_yards") for r in top(tdrows, lambda r: r["starter_yards"])],
        "best_all_play": [slim_s(r, "all_play_str", "all_play_pct") for r in top(elig, lambda r: r["all_play_pct"])],
    }
    ws_sorted = sorted(week_scores, key=lambda e: e["points"], reverse=True)
    gm = [dict(g, margin=r2(abs(g["a_pts"] - g["b_pts"]))) for g in week_games]

    def game_view(g):
        w, l_ = (("a", "b") if g["a_pts"] >= g["b_pts"] else ("b", "a"))
        return {"season": g["season"], "week": g["week"], "phase": g["phase"], "winner": name(g[w]), "winner_id": g[w],
                "winner_points": g[w + "_pts"], "loser": name(g[l_]), "loser_id": g[l_], "loser_points": g[l_ + "_pts"], "margin": g["margin"]}
    week_records = {
        "highest_scores": ws_sorted[:TOP],
        "lowest_scores": ws_sorted[::-1][:TOP],
        "biggest_blowouts": [game_view(g) for g in sorted(gm, key=lambda g: g["margin"], reverse=True)[:TOP]],
        "closest_games": [game_view(g) for g in sorted(gm, key=lambda g: g["margin"])[:TOP]],
        "top_player_starts": sorted(player_starts, key=lambda p: p["points"], reverse=True)[:TOP],
    }

    # ---------------- streaks (H2H games, chronological, across seasons) ----------------
    seq = defaultdict(list)
    for g in sorted(h2h["games"], key=lambda g: (g["season"], int(g["weeks"].split("-")[0]))):
        if g["a_pts"] == g["b_pts"]:
            continue
        win, lose = (g["a"], g["b"]) if g["a_pts"] > g["b_pts"] else (g["b"], g["a"])
        seq[win].append(("W", g)); seq[lose].append(("L", g))
    streaks = {"longest_win": [], "longest_loss": []}
    for mid, s_ in seq.items():
        for kind, key in (("W", "longest_win"), ("L", "longest_loss")):
            best, cur, start = (0, None, None), 0, None
            for i, (res, g) in enumerate(s_):
                if res == kind:
                    cur += 1
                    start = start if cur > 1 else g
                    if cur > best[0]:
                        best = (cur, start, g, i == len(s_) - 1)
                else:
                    cur = 0
            if best[0]:
                active = best[3] and s_[-1][0] == kind
                streaks[key].append({"manager": mid, "name": name(mid), "length": best[0],
                                     "from": f"{best[1]['season']} Wk {best[1]['weeks']}", "to": f"{best[2]['season']} Wk {best[2]['weeks']}",
                                     "active": active})
    for k in streaks:
        streaks[k].sort(key=lambda x: -x["length"])
        streaks[k] = streaks[k][:TOP]

    # ---------------- notable ----------------
    cur = [c for c in career if c["group"] == "current"]
    pf_lead = max(career, key=lambda c: c["pf"])
    ppg_lead = max((c for c in career if c["games"] >= 20), key=lambda c: c["ppg"])
    rec_lead = max((c for c in career if c["games"] >= 20), key=lambda c: c["record_pct"])
    td1 = season_records["most_starter_tds"][0] if season_records["most_starter_tds"] else None
    b1, w1 = season_records["best_record"][0], season_records["worst_record"][0]
    hs, ls = week_records["highest_scores"][0], week_records["lowest_scores"][0]
    bb, cg = week_records["biggest_blowouts"][0], week_records["closest_games"][0]
    ps = week_records["top_player_starts"][0] if week_records["top_player_starts"] else None
    lw, ll = streaks["longest_win"][0], streaks["longest_loss"][0]
    notable = [
        f"All-time points leader: {pf_lead['name']}, {pf_lead['pf']:,.2f} regular-season PF over {pf_lead['games']} games ({pf_lead['ppg']} per game).",
        f"Best career points per game (20+ games): {ppg_lead['name']}, {ppg_lead['ppg']}.",
        f"Best career regular-season record (20+ games): {rec_lead['name']}, {rec_lead['record_str']} ({rec_lead['record_pct']:.3f}).",
        f"Most titles: {', '.join(c['name'] + ' ' + str(c['titles']) for c in sorted(career, key=lambda c: -c['titles']) if c['titles'] == max(x['titles'] for x in career))}.",
        f"Best season record: {b1['name']} {b1['season']}, {b1['record']} ({b1['record_pct']:.3f})"
        + (" (tied on win pct with " + ", ".join(f"{x['name']} {x['season']} {x['record']}" for x in season_records['best_record'][1:] if x['record_pct'] == b1['record_pct']) + "; PF breaks the tie)"
           if any(x['record_pct'] == b1['record_pct'] for x in season_records['best_record'][1:]) else "")
        + f". Worst: {w1['name']} {w1['season']}, {w1['record']} ({w1['record_pct']:.3f}).",
        f"Highest single week: {hs['name']} {hs['points']} ({hs['season']} Wk {hs['week']}). Lowest: {ls['name']} {ls['points']} ({ls['season']} Wk {ls['week']}).",
        f"Biggest blowout: {bb['winner']} over {bb['loser']} by {bb['margin']} ({bb['season']} Wk {bb['week']}). Closest: {cg['winner']} over {cg['loser']} by {cg['margin']} ({cg['season']} Wk {cg['week']}).",
        f"Longest H2H win streak: {lw['name']}, {lw['length']} ({lw['from']} to {lw['to']}). Longest losing streak: {ll['name']}, {ll['length']} ({ll['from']} to {ll['to']}).",
    ]
    if td1:
        notable.insert(5, f"Most starter TDs in a season (2023+): {td1['name']} {td1['season']}, {td1['starter_tds']} ({td1['tds_per_game']} per game).")
    if ps:
        tied = [p for p in week_records["top_player_starts"] if p["points"] == ps["points"]]
        notable.append(("Best individual start (2023+): " if len(tied) == 1 else f"Best individual start (2023+, {len(tied)}-way tie at {ps['points']}): ")
                       + "; ".join(f"{p['player']} {p['points']} for {p['name']} ({p['season']} Wk {p['week']})" for p in tied) + ".")

    # ---------------- cross-check vs history.json (Sleeper seasons, H2H regular, by roster owner) ----------------
    hist_checks = []
    hist_file = ROOT / "data" / "history.json"
    if hist_file.exists():
        hist = json.loads(hist_file.read_text())
        full2short = {t["owner"]: t["short_name"] for t in json.loads(TEAMS_FILE.read_text())} if TEAMS_FILE.exists() else {}
        by_name = {m["name"].lower(): m["id"] for m in h2h["managers"]}
        by_handle = {(m.get("sleeper_handle") or "").lower(): m["id"] for m in h2h["managers"]}

        def hid(owner):
            return (by_name.get(full2short.get(owner, owner).lower()) or by_handle.get(owner.lower()) or by_name.get(owner.lower())
                    or (owner.lower() if owner.lower() in reg else None))
        sleeper_h2h = defaultdict(rec)
        for r in season_rows:
            if r["platform"] == "Sleeper":
                owner = r["manager"]
                for o in overrides:  # history.json credits a split roster to its Sleeper owner
                    if o["season"] == r["season"] and o["manager"] == r["manager"] and o.get("handover_to"):
                        owner = o["handover_to"]
                for k in ("w", "l", "t"):
                    sleeper_h2h[owner][k] += r["h2h"][k]
        for c in hist.get("career", []):
            mid = hid(c["owner"])
            mine = sleeper_h2h.get(mid)
            ok = mine is not None and (mine["w"], mine["l"]) == (c["wins"], c["losses"])
            hist_checks.append(f"{'ok' if ok else 'MISMATCH'} {c['owner']}: history.json {c['wins']}-{c['losses']} vs Sleeper H2H {fmt(mine) if mine else 'n/a'}")
        champs_here = {r["season"]: r["name"] for r in season_rows if r["finish"] == "Champion"}
        for c in hist.get("champions", []):
            se = str(c["season"])
            if c.get("co_champions"):
                ok = sorted(c["co_champions"]) == sorted(r["name"] for r in season_rows if r["season"] == se and r["finish"] == "Co-champion")
            else:
                ok = hid(c["champion"]) == next((r["manager"] for r in season_rows if r["season"] == se and r["finish"] == "Champion"), None)
            hist_checks.append(f"{'ok' if ok else 'MISMATCH'} champion {se}: {c['champion']}")
        bad = [x for x in hist_checks if x.startswith("MISMATCH")]
        print(f"history.json cross-check: {len(hist_checks) - len(bad)} ok, {len(bad)} mismatches")
        for x in bad:
            print("  ", x)

    out = {
        "meta": {
            "generated_at": H.now_iso(),
            "seasons_covered": sorted({r["season"] for r in season_rows}),
            "through": through,
            "labels": H.LABELS,
            "rules": [
                "Keyed by manager (same ids, names and Legacy Owners label as h2h_all_time.json). 2024 roster 2 is split: Mike Dewey Weeks 1-7, Matt Z from Week 8.",
                "Regular-season record = H2H + weekly median where the league used a median game (Sleeper 2023+). 2022 (ESPN) had no median, so its record is H2H only.",
                "PF, PA, points per game and all-play are regular season only. All-play = record if you played every team every week.",
                "Playoff appearances / finals / titles and playoff records come from winners-bracket games; a two-week round is one game on combined score.",
                "2022 titles: Deion Hulse and Jared Messinger are co-champions (counted as a title for both). 2023 and 2024 Doug, 2025 Vlad.",
                "Single-week records include every real weekly pairing: regular season, playoff legs and consolation legs.",
                "Starter TDs = passing + rushing + receiving + IDP defensive + return TDs by starters, regular season only (same logic as team_stats.json). Yards = passing + rushing + receiving.",
                "Season records and best/worst seasons use full seasons only; partial seasons (the in-progress season, or a mid-season owner change) are listed in manager_seasons but not ranked.",
            ],
            "caveats": [
                "Scoring and lineups differ by season: 2022 ESPN 0.5 PPR with K and D/ST, no IDP; 2023 Sleeper had K and team DEF, no IDP; 2024+ Sleeper has IDP and no K/DEF. Compare points per game rather than raw totals across seasons.",
                "Season length differs: 13 regular-season games in 2022-2024, 11 in 2025. Use points per game and win pct for fair comparisons.",
                "2022 records are H2H only (13 games); 2023+ records include the median game (26 results per 13 weeks). Win pct is comparable, raw win totals are not.",
                "Player-level data (starter TDs, yards, individual starts) is N/A for 2022: the saved ESPN data only has final scores.",
                "2023 team-DEF touchdowns are not counted as IDP TDs (team DEF is not IDP); return TDs are counted when Sleeper credits them.",
                "Mike Dewey's 2024 (Weeks 1-7) and Matt Z's 2024 (Weeks 8-17) are partial seasons.",
                "2024 Week 7 (the week of the overturned Mike Dewey / Matt Froemming trade): Sleeper's official standings credit Juan +75.14, roster 2 +20.84 and Matt Froemming +28.86 more points than the current matchup data. That flips four median results, so the official standings records are used for Juan (20-6), Doug (18-8), Matt Froemming (7-19) and Andrew (6-20). PF, all-play and weekly scores use the matchup data (e.g. Juan's 43.20 that week).",
                "Small PF gaps between Sleeper standings and matchup data with no record change: Lij 2024 (+0.40) and Brett 2025 (+9.70). PF here always equals the sum of weekly matchup scores.",
            ],
            "scoring_by_season": sorted(scoring_by_season, key=lambda x: x["season"]),
            "checks": {"passed": len(checks), "discrepancies": problems, "history_json": hist_checks},
        },
        "career": career,
        "season_records": season_records,
        "week_records": week_records,
        "streaks": streaks,
        "manager_seasons": season_rows,
        "best_worst": best_worst,
        "notable": notable,
    }
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes): {len(season_rows)} manager-seasons, {len(week_scores)} week scores, {len(player_starts)} starts")
    print(f"checks passed: {len(checks)}; discrepancies: {len(problems)}")
    for p in problems:
        print("  DISCREPANCY", p)
    for line in notable:
        print(" ", line)


if __name__ == "__main__":
    main()

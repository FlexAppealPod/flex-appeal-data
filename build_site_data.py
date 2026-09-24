#!/usr/bin/env python3
"""Build Flex Appeal FFL website JSON data from Sleeper (current + history).

Run weekly (or after scores finalize) from /workspace/flex-appeal-data:
  python3 build_site_data.py

Writes data/*.json. Prefers live Sleeper API; falls back to local snapshots
under /workspace/flex-numbers-2026/api and /workspace/flex-appeal/sleeper/history.
"""
from __future__ import annotations

import csv
import json
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data"
CACHE_2026 = Path("/workspace/flex-numbers-2026/api")
HISTORY = Path("/workspace/flex-appeal/sleeper/history")
CSV_2026 = Path("/workspace/flex-numbers-2026/csv")

LEAGUE_ID_2026 = "1311998258079371264"
PAST_LEAGUES = [
    (2023, "996133920917856256"),
    (2024, "1048417672926547968"),
    (2025, "1180242334892032000"),
]
BASE = "https://api.sleeper.app/v1"
UA = "FlexAppealFFL-site-data/1.0"
TZ = ZoneInfo("America/Phoenix")

# display_name / sleeper handle -> real name
NAME_BY_HANDLE = {
    "jacobpro": "Jake Prosser",
    "mzacharias95": "Matt Zacharias",
    "PaulB1996": "Paul Bacon",
    "syrax": "Elijah Bruette",
    "DSully701": "Douglas Sullivan",
    "KansasCityKings": "Juan Rodriguez",
    "JRinfidel": "Juan Rodriguez",
    "derekbockk": "Derek Bock",
    "matkinson94": "Matt Atkinson",
    "vladydaddy23": "Vlad Barber",
    "trademarcc": "Marc Caballero",
    "tranachula": "Brett Trana",
    "Blackreed": "Andrew Reed",
}
# user_id overrides (Juan has changed handles)
NAME_BY_USER_ID = {
    "1001211933904744448": "Juan Rodriguez",
    "996131000033988608": "Jake Prosser",
    "895431724404973568": "Matt Zacharias",
    "1131743240228667392": "Paul Bacon",
    "999903205674872832": "Elijah Bruette",
    "1000997586435682304": "Douglas Sullivan",
    "1131401008774623232": "Derek Bock",
    "1135747453468438528": "Matt Atkinson",
    "1001357488391839744": "Vlad Barber",
    "1001983779910672384": "Marc Caballero",
    "1135688681358348288": "Brett Trana",
    "789344057498451968": "Andrew Reed",
}
SHORT = {
    "Jake Prosser": "Jake",
    "Matt Zacharias": "Matt Z",
    "Paul Bacon": "Paul",
    "Elijah Bruette": "Lij",
    "Douglas Sullivan": "Doug",
    "Juan Rodriguez": "Juan",
    "Derek Bock": "Derek",
    "Matt Atkinson": "Matt A",
    "Vlad Barber": "Vlad",
    "Marc Caballero": "Marc",
    "Brett Trana": "Brett",
    "Andrew Reed": "Andrew",
    "Deion": "Deion",
}
# Jake's Sleeper team_name is blank; force display name.
TEAM_NAME_OVERRIDE = {
    "Jake Prosser": "Drought Ends Here",
}
# 2023 roster 3 had owner_id null — label Deion (championship runner-up).
DEION_ROSTER = {(2023, 3): "Deion"}

# Formula weights (lower score = better)
W_STAND, W_PF, W_PD, W_PREV, W_STREAK = 0.35, 0.25, 0.10, 0.15, 0.15

# Week 3 Flex board is LOCKED (posted board). Movement vs last posted board
# (not necessarily week_2_flex.csv). formula/pf/pd/streak come from computed
# week-3 flex / week_3_flex.csv. Order and movements are authoritative.
LOCKED_WEEK3_FLEX = [
    # rank, owner, movement
    (1, "Elijah Bruette", 4),
    (2, "Douglas Sullivan", 2),
    (3, "Paul Bacon", 0),
    (4, "Brett Trana", -3),
    (5, "Matt Zacharias", 6),
    (6, "Marc Caballero", -4),
    (7, "Matt Atkinson", 3),
    (8, "Vlad Barber", -2),
    (9, "Derek Bock", -2),
    (10, "Andrew Reed", 2),
    (11, "Jake Prosser", -3),
    (12, "Juan Rodriguez", -3),
]


def r2(x) -> float:
    return round(float(x) + 1e-12, 2)


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def get_json(url: str, retries: int = 4):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 400):
                return None
            if e.code == 429:
                time.sleep(2 ** (i + 1))
                continue
            time.sleep(1)
        except Exception as e:
            last = e
            time.sleep(1)
    print(f"  WARN fetch failed: {url} last={last}")
    return None


def load_local(path: Path):
    if path.exists():
        return json.loads(path.read_text())
    return None


def fetch_or_cache(url: str, local: Path):
    data = get_json(url)
    if data is not None:
        return data
    cached = load_local(local)
    if cached is not None:
        print(f"  fallback cache: {local}")
        return cached
    raise FileNotFoundError(f"No live data and no cache for {url} / {local}")


def write_json(name: str, obj):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    print(f"  wrote {path}")
    return path


def owner_name(user: dict | None, user_id: str | None = None, season_roster=None) -> str:
    if season_roster and season_roster in DEION_ROSTER:
        return DEION_ROSTER[season_roster]
    if user_id and user_id in NAME_BY_USER_ID:
        return NAME_BY_USER_ID[user_id]
    if not user:
        return "Unknown"
    handle = user.get("display_name") or ""
    if handle in NAME_BY_HANDLE:
        return NAME_BY_HANDLE[handle]
    uid = user.get("user_id")
    if uid and uid in NAME_BY_USER_ID:
        return NAME_BY_USER_ID[uid]
    return handle or "Unknown"


def team_name_for(owner: str, meta_team: str | None) -> str:
    if owner in TEAM_NAME_OVERRIDE:
        return TEAM_NAME_OVERRIDE[owner]
    tn = (meta_team or "").strip()
    return tn if tn else owner


def rank_avg(items, key, reverse=True):
    ordered = sorted(items, key=key, reverse=reverse)
    values = [key(x) for x in ordered]
    ranks = {}
    i = 0
    while i < len(ordered):
        j = i
        while j < len(ordered) and values[j] == values[i]:
            j += 1
        avg = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[ordered[k]["owner"]] = avg
        i = j
    return ranks


def week_is_scored(rows) -> bool:
    return any((r.get("points") or 0) > 0 for r in rows)


# ---------------------------------------------------------------------------
# Current season (2026)
# ---------------------------------------------------------------------------

def build_teams_universe(users, rosters):
    uid = {u["user_id"]: u for u in users}
    teams = {}
    for r in rosters:
        u = uid.get(r.get("owner_id") or "") or {}
        owner = owner_name(u, r.get("owner_id"))
        meta = u.get("metadata") or {}
        tn = team_name_for(owner, meta.get("team_name"))
        teams[r["roster_id"]] = {
            "roster_id": r["roster_id"],
            "owner_id": r.get("owner_id"),
            "handle": u.get("display_name") or "",
            "owner": owner,
            "short_name": SHORT.get(owner, owner.split()[0] if owner else ""),
            "team_name": tn,
            "settings": r.get("settings") or {},
        }
    return teams


def compute_weekly(teams, matchups_by_week):
    weekly = {}
    for week, rows in sorted(matchups_by_week.items()):
        if not week_is_scored(rows):
            continue
        by_rid = {r["roster_id"]: r for r in rows}
        pts = {rid: r2(r.get("points") or 0) for rid, r in by_rid.items()}
        med = r2(median(pts.values())) if pts else 0.0
        by_mid = defaultdict(list)
        for r in rows:
            if r.get("matchup_id") is not None:
                by_mid[r["matchup_id"]].append(r)
        opp, h2h = {}, {}
        for pair in by_mid.values():
            if len(pair) != 2:
                continue
            a, b = pair
            pa, pb = r2(a.get("points") or 0), r2(b.get("points") or 0)
            opp[a["roster_id"]] = (b["roster_id"], pb)
            opp[b["roster_id"]] = (a["roster_id"], pa)
            if pa > pb:
                h2h[a["roster_id"]], h2h[b["roster_id"]] = "W", "L"
            elif pa < pb:
                h2h[a["roster_id"]], h2h[b["roster_id"]] = "L", "W"
            else:
                h2h[a["roster_id"]], h2h[b["roster_id"]] = "T", "T"
        weekly[week] = {}
        for rid in by_rid:
            p = pts[rid]
            if p > med:
                va = "W"
            elif p < med:
                va = "L"
            else:
                va = "T"
            weekly[week][rid] = {
                "points": p,
                "median": med,
                "h2h": h2h.get(rid),
                "vs_median": va,
                "opp_rid": opp.get(rid, (None, None))[0],
                "opp_pts": opp.get(rid, (None, None))[1],
            }
    return weekly


def cumulative(weekly, through_week):
    weeks = [w for w in sorted(weekly) if w <= through_week]
    rids = set()
    for w in weeks:
        rids.update(weekly[w])
    agg = {}
    for rid in rids:
        w = l = t = 0
        h2h_w = h2h_l = h2h_t = 0
        med_w = med_l = med_t = 0
        pf = pa = 0.0
        decisions = []
        for week in weeks:
            info = weekly[week].get(rid)
            if not info:
                continue
            pf += info["points"]
            if info["opp_pts"] is not None:
                pa += info["opp_pts"]
            for res, bucket in (
                (info["h2h"], "h2h"),
                (info["vs_median"], "med"),
            ):
                if not res:
                    continue
                decisions.append(res)
                if res == "W":
                    w += 1
                    if bucket == "h2h":
                        h2h_w += 1
                    else:
                        med_w += 1
                elif res == "L":
                    l += 1
                    if bucket == "h2h":
                        h2h_l += 1
                    else:
                        med_l += 1
                else:
                    t += 1
                    if bucket == "h2h":
                        h2h_t += 1
                    else:
                        med_t += 1
        streak_lab, streak_val = "", 0
        if decisions:
            last = decisions[-1]
            n = 0
            for res in reversed(decisions):
                if res == last:
                    n += 1
                else:
                    break
            if last == "W":
                streak_lab, streak_val = f"{n}W", n
            elif last == "L":
                streak_lab, streak_val = f"{n}L", -n
            else:
                streak_lab, streak_val = f"{n}T", 0
        last_week = weekly[weeks[-1]].get(rid) if weeks else None
        def rec(ww, ll, tt):
            return f"{ww}-{ll}" + (f"-{tt}" if tt else "")
        agg[rid] = {
            "wins": w,
            "losses": l,
            "ties": t,
            "record": rec(w, l, t),
            "h2h_record": rec(h2h_w, h2h_l, h2h_t),
            "median_record": rec(med_w, med_l, med_t),
            "h2h_w": h2h_w,
            "h2h_l": h2h_l,
            "pf": r2(pf),
            "pa": r2(pa),
            "pd": r2(pf - pa),
            "streak": streak_lab,
            "streak_val": streak_val,
            "prev_week_pts": last_week["points"] if last_week else None,
        }
    return agg


def flex_rank_table(teams, weekly, through_week, prev_flex: dict | None):
    agg = cumulative(weekly, through_week)
    items = []
    for rid, t in teams.items():
        if rid not in agg:
            continue
        a = agg[rid]
        items.append({
            "rid": rid,
            "owner": t["owner"],
            "team_name": t["team_name"],
            "wins": a["wins"],
            "losses": a["losses"],
            "ties": a["ties"],
            "record": a["record"],
            "pf": a["pf"],
            "pa": a["pa"],
            "pd": a["pd"],
            "streak": a["streak"],
            "streak_val": a["streak_val"],
            "prev_week_pts": a["prev_week_pts"],
        })
    stand_rank = rank_avg(items, key=lambda r: (r["wins"], r["pf"]), reverse=True)
    pf_rank = rank_avg(items, key=lambda r: r["pf"], reverse=True)
    pd_rank = rank_avg(items, key=lambda r: r["pd"], reverse=True)
    streak_rank = rank_avg(items, key=lambda r: r["streak_val"], reverse=True)
    prev_pts_rank = rank_avg(items, key=lambda r: r["prev_week_pts"] or 0, reverse=True)
    for r in items:
        o = r["owner"]
        r["formula_score"] = r2(
            W_STAND * stand_rank[o]
            + W_PF * pf_rank[o]
            + W_PD * pd_rank[o]
            + W_PREV * prev_pts_rank[o]
            + W_STREAK * streak_rank[o]
        )
    flex_order = sorted(
        items,
        key=lambda r: (r["formula_score"], -r["pf"], -r["pd"], r["owner"]),
    )
    for flex, r in enumerate(flex_order, 1):
        r["rank"] = flex
        if prev_flex and r["owner"] in prev_flex:
            r["movement"] = int(prev_flex[r["owner"]] - r["rank"])
        else:
            r["movement"] = None
    items.sort(key=lambda r: r["rank"])
    return items


def standings_rows(teams, weekly, through_week):
    agg = cumulative(weekly, through_week)
    rows = []
    for rid, t in teams.items():
        if rid not in agg:
            continue
        a = agg[rid]
        rows.append({
            "team_name": t["team_name"],
            "owner": t["owner"],
            "record": a["record"],
            "h2h_record": a["h2h_record"],
            "median_record": a["median_record"],
            "pf": a["pf"],
            "pa": a["pa"],
            "pd": a["pd"],
            "streak": a["streak"],
            "_wins": a["wins"],
            "_pf": a["pf"],
        })
    rows.sort(key=lambda r: (r["_wins"], r["_pf"]), reverse=True)
    out = []
    for i, r in enumerate(rows, 1):
        out.append({
            "rank": i,
            "team_name": r["team_name"],
            "owner": r["owner"],
            "record": r["record"],
            "h2h_record": r["h2h_record"],
            "median_record": r["median_record"],
            "pf": r["pf"],
            "pa": r["pa"],
            "pd": r["pd"],
            "streak": r["streak"],
        })
    return out


def matchup_rows(teams, weekly):
    rows = []
    for week in sorted(weekly):
        for rid, info in weekly[week].items():
            t = teams[rid]
            opp_rid = info["opp_rid"]
            opp = teams.get(opp_rid, {})
            rows.append({
                "week": week,
                "team_name": t["team_name"],
                "owner": t["owner"],
                "points": info["points"],
                "opponent": opp.get("team_name") or opp.get("owner") or "",
                "opponent_points": info["opp_pts"],
                "h2h_result": info["h2h"],
                "weekly_median": info["median"],
                "median_result": info["vs_median"],
            })
    rows.sort(key=lambda r: (r["week"], r["owner"]))
    return rows


def upcoming_pairings(teams, matchup_rows_raw, agg):
    """Build upcoming week pairings from unscored matchup JSON."""
    by_mid = defaultdict(list)
    for r in matchup_rows_raw:
        if r.get("matchup_id") is not None:
            by_mid[r["matchup_id"]].append(r)
    out = []
    # infer week from caller
    return by_mid  # placeholder — filled in main


def season_record_lists(teams, weekly, top_n=10):
    games = []  # one side per H2H
    for week, wk in weekly.items():
        seen = set()
        for rid, info in wk.items():
            opp = info["opp_rid"]
            if opp is None or rid in seen:
                continue
            seen.add(rid)
            seen.add(opp)
            a, b = teams[rid], teams[opp]
            pa, pb = info["points"], info["opp_pts"]
            margin = r2(abs(pa - pb))
            # winner-centric and both sides for highs/lows
            games.append({
                "week": week,
                "owner_a": a["owner"],
                "points_a": pa,
                "owner_b": b["owner"],
                "points_b": pb,
                "margin": margin,
            })
    # high scores: each team-game as a row
    highs = []
    lows = []
    for week, wk in weekly.items():
        for rid, info in wk.items():
            opp = teams.get(info["opp_rid"], {})
            row = {
                "week": week,
                "owner": teams[rid]["owner"],
                "points": info["points"],
                "opponent": opp.get("owner") or "",
                "opponent_points": info["opp_pts"],
                "margin": r2(abs(info["points"] - (info["opp_pts"] or 0))),
            }
            highs.append(row)
            lows.append(dict(row))
    highs.sort(key=lambda r: r["points"], reverse=True)
    lows.sort(key=lambda r: r["points"])
    closest = sorted(
        [
            {
                "week": g["week"],
                "owner": g["owner_a"] if g["points_a"] >= g["points_b"] else g["owner_b"],
                "points": max(g["points_a"], g["points_b"]),
                "opponent": g["owner_b"] if g["points_a"] >= g["points_b"] else g["owner_a"],
                "opponent_points": min(g["points_a"], g["points_b"]),
                "margin": g["margin"],
            }
            for g in games
        ],
        key=lambda r: r["margin"],
    )
    blowouts = sorted(
        [
            {
                "week": g["week"],
                "owner": g["owner_a"] if g["points_a"] >= g["points_b"] else g["owner_b"],
                "points": max(g["points_a"], g["points_b"]),
                "opponent": g["owner_b"] if g["points_a"] >= g["points_b"] else g["owner_a"],
                "opponent_points": min(g["points_a"], g["points_b"]),
                "margin": g["margin"],
            }
            for g in games
        ],
        key=lambda r: r["margin"],
        reverse=True,
    )
    return {
        "high_scores": highs[:top_n],
        "low_scores": lows[:top_n],
        "closest_games": closest[:top_n],
        "blowouts": blowouts[:top_n],
    }


def locked_week3_flex(teams, weekly):
    """Use computed formula/pf/pd/streak but LOCKED order + movements."""
    computed = {r["owner"]: r for r in flex_rank_table(teams, weekly, 2, None)}
    rankings = []
    for rank, owner, movement in LOCKED_WEEK3_FLEX:
        c = computed[owner]
        t = next(t for t in teams.values() if t["owner"] == owner)
        rankings.append({
            "rank": rank,
            "team_name": t["team_name"],
            "owner": owner,
            "record": c["record"],
            "movement": movement,
            "formula_score": c["formula_score"],
            "pf": c["pf"],
            "pd": c["pd"],
            "streak": c["streak"],
        })
    climbers = sorted(
        [r for r in rankings if r["movement"] and r["movement"] > 0],
        key=lambda r: r["movement"],
        reverse=True,
    )
    sliders = sorted(
        [r for r in rankings if r["movement"] and r["movement"] < 0],
        key=lambda r: r["movement"],
    )
    return {
        "week": 3,
        "rankings": rankings,
        "climbers": climbers,
        "sliders": sliders,
        "locked": True,
    }


def build_flex_live(teams, weekly, board_week: int, prev_flex: dict | None):
    """board_week N = ranking heading into week N (data through N-1)."""
    through = board_week - 1
    if through < 1:
        return {"week": board_week, "rankings": [], "climbers": [], "sliders": []}
    rows = flex_rank_table(teams, weekly, through, prev_flex)
    rankings = [
        {
            "rank": r["rank"],
            "team_name": r["team_name"],
            "owner": r["owner"],
            "record": r["record"],
            "movement": r["movement"] if r["movement"] is not None else 0,
            "formula_score": r["formula_score"],
            "pf": r["pf"],
            "pd": r["pd"],
            "streak": r["streak"],
        }
        for r in rows
    ]
    climbers = sorted(
        [r for r in rankings if r["movement"] > 0],
        key=lambda r: r["movement"],
        reverse=True,
    )
    sliders = sorted(
        [r for r in rankings if r["movement"] < 0],
        key=lambda r: r["movement"],
    )
    return {
        "week": board_week,
        "rankings": rankings,
        "climbers": climbers,
        "sliders": sliders,
        "locked": False,
    }


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def resolve_past_owner(season, lid, roster_id, users_by_id, rosters_by_id):
    key = (season, roster_id)
    if key in DEION_ROSTER:
        return DEION_ROSTER[key]
    r = rosters_by_id.get(roster_id) or {}
    oid = r.get("owner_id")
    if not oid:
        return "Deion" if season == 2023 and roster_id == 3 else f"roster-{roster_id}"
    u = users_by_id.get(oid)
    return owner_name(u, oid)


def champions_from_brackets():
    champs = []
    for season, lid in PAST_LEAGUES:
        wb = load_local(HISTORY / f"winners_bracket-{lid}.json")
        if wb is None:
            wb = get_json(f"{BASE}/league/{lid}/winners_bracket")
        users = load_local(HISTORY / f"users-{lid}.json") or []
        rosters = load_local(HISTORY / f"rosters-{lid}.json") or []
        users_by_id = {u["user_id"]: u for u in users}
        rosters_by_id = {r["roster_id"]: r for r in rosters}
        if not wb:
            continue
        for g in wb:
            if g.get("p") == 1:
                champ_rid = g["w"]
                runner_rid = g["t2"] if g["w"] == g.get("t1") else g.get("t1")
                if runner_rid == champ_rid:
                    runner_rid = g.get("t2")
                champs.append({
                    "season": season,
                    "champion": resolve_past_owner(
                        season, lid, champ_rid, users_by_id, rosters_by_id
                    ),
                    "runner_up": resolve_past_owner(
                        season, lid, runner_rid, users_by_id, rosters_by_id
                    ),
                })
                break
    return champs


def load_career_csv():
    path = HISTORY / "manager_career.csv"
    rows = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def load_extremes_csv(name):
    path = HISTORY / name
    rows = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def build_history(season_2026_agg, season_records_2026):
    champs = champions_from_brackets()
    career_csv = load_career_csv()

    # Map 2026 H2H + PF onto career
    by_owner_2026 = {}
    for rid, a in season_2026_agg.items():
        # rid -> need owner; passed as dict owner->agg from main
        pass

    # season_2026_agg is owner -> {h2h_w, h2h_l, pf, ...}
    career = []
    for row in career_csv:
        owner = row["manager"]
        seasons = int(row["seasons_played"])
        wins = int(row["h2h_reg_W"])
        losses = int(row["h2h_reg_L"])
        total_pf = float(row["pf_settings"])
        chips = int(row["championships"])
        playoffs = int(row["playoff_appearances"])
        # fold in 2026 to-date for current managers
        if owner in season_2026_agg:
            a = season_2026_agg[owner]
            seasons += 1
            wins += a["h2h_w"]
            losses += a["h2h_l"]
            total_pf = r2(total_pf + a["pf"])
        total_games = wins + losses
        win_pct = r2(wins / total_games) if total_games else 0.0
        career.append({
            "owner": owner,
            "seasons": seasons,
            "wins": wins,
            "losses": losses,
            "win_pct": win_pct,
            "total_pf": r2(total_pf),
            "championships": chips,
            "playoff_appearances": playoffs,
        })
    # Any 2026-only owners not in career csv (shouldn't happen)
    known = {c["owner"] for c in career}
    for owner, a in season_2026_agg.items():
        if owner not in known:
            wg, lg = a["h2h_w"], a["h2h_l"]
            career.append({
                "owner": owner,
                "seasons": 1,
                "wins": wg,
                "losses": lg,
                "win_pct": r2(wg / (wg + lg)) if (wg + lg) else 0.0,
                "total_pf": a["pf"],
                "championships": 0,
                "playoff_appearances": 0,
            })
    career.sort(key=lambda r: (-r["total_pf"], -r["wins"], r["owner"]))

    def hist_high(row):
        return {
            "season": int(row["season"]),
            "week": int(row["week"]),
            "owner": row["manager"],
            "points": float(row["points"]),
            "opponent": row["opponent"],
        }

    def hist_low(row):
        return hist_high(row)

    def hist_blowout(row):
        return {
            "season": int(row["season"]),
            "week": int(row["week"]),
            "owner": row["winner"],
            "points": float(row["points_a"]) if row["winner"] == row["manager_a"] else float(row["points_b"]),
            "opponent": row["manager_b"] if row["winner"] == row["manager_a"] else row["manager_a"],
            "opponent_points": float(row["points_b"]) if row["winner"] == row["manager_a"] else float(row["points_a"]),
            "margin": float(row["margin"]),
        }

    def hist_closest(row):
        return {
            "season": int(row["season"]),
            "week": int(row["week"]),
            "owner": row["winner"],
            "points": float(row["points_a"]) if row["winner"] == row["manager_a"] else float(row["points_b"]),
            "opponent": row["manager_b"] if row["winner"] == row["manager_a"] else row["manager_a"],
            "opponent_points": float(row["points_b"]) if row["winner"] == row["manager_a"] else float(row["points_a"]),
            "margin": float(row["margin"]),
        }

    all_high = [hist_high(r) for r in load_extremes_csv("top15_team_games.csv")]
    all_low = [hist_low(r) for r in load_extremes_csv("bottom10_team_games.csv")]
    all_blow = [hist_blowout(r) for r in load_extremes_csv("blowouts.csv")]
    all_close = [hist_closest(r) for r in load_extremes_csv("closest_games.csv")]

    # Merge 2026 season extremes
    for h in season_records_2026["high_scores"]:
        all_high.append({
            "season": 2026,
            "week": h["week"],
            "owner": h["owner"],
            "points": h["points"],
            "opponent": h["opponent"],
        })
    for h in season_records_2026["low_scores"]:
        all_low.append({
            "season": 2026,
            "week": h["week"],
            "owner": h["owner"],
            "points": h["points"],
            "opponent": h["opponent"],
        })
    for h in season_records_2026["blowouts"]:
        all_blow.append({
            "season": 2026,
            "week": h["week"],
            "owner": h["owner"],
            "points": h["points"],
            "opponent": h["opponent"],
            "opponent_points": h["opponent_points"],
            "margin": h["margin"],
        })
    for h in season_records_2026["closest_games"]:
        all_close.append({
            "season": 2026,
            "week": h["week"],
            "owner": h["owner"],
            "points": h["points"],
            "opponent": h["opponent"],
            "opponent_points": h["opponent_points"],
            "margin": h["margin"],
        })

    all_high.sort(key=lambda r: r["points"], reverse=True)
    all_low.sort(key=lambda r: r["points"])
    all_blow.sort(key=lambda r: r["margin"], reverse=True)
    all_close.sort(key=lambda r: r["margin"])

    return {
        "champions": champs,
        "career": career,
        "all_time_high_scores": all_high[:15],
        "all_time_low_scores": all_low[:10],
        "biggest_blowouts": all_blow[:15],
        "closest_games": all_close[:15],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Flex Appeal site data build")
    print(f"  time: {now_iso()}")

    # --- fetch current league ---
    print("Fetching 2026 league…")
    league = fetch_or_cache(
        f"{BASE}/league/{LEAGUE_ID_2026}",
        CACHE_2026 / "league.json",
    )
    users = fetch_or_cache(
        f"{BASE}/league/{LEAGUE_ID_2026}/users",
        CACHE_2026 / "users.json",
    )
    rosters = fetch_or_cache(
        f"{BASE}/league/{LEAGUE_ID_2026}/rosters",
        CACHE_2026 / "rosters.json",
    )
    nfl = fetch_or_cache(f"{BASE}/state/nfl", CACHE_2026 / "nfl-state.json")

    season = int(league.get("season") or nfl.get("league_season") or 2026)
    display_week = int(nfl.get("display_week") or nfl.get("week") or 1)
    # Fetch matchups for weeks 1..display_week+1 (upcoming)
    matchups_by_week = {}
    for w in range(1, max(display_week + 1, 4) + 1):
        data = fetch_or_cache(
            f"{BASE}/league/{LEAGUE_ID_2026}/matchups/{w}",
            CACHE_2026 / f"matchups-w{w}.json",
        )
        if data:
            matchups_by_week[w] = data

    teams = build_teams_universe(users, rosters)
    weekly = compute_weekly(teams, matchups_by_week)
    completed_weeks = sorted(weekly.keys())
    through = completed_weeks[-1] if completed_weeks else 0
    print(f"  completed weeks: {completed_weeks}  display_week={display_week}")

    # meta
    write_json("meta.json", {
        "season": season,
        "current_week": display_week,
        "last_updated": now_iso(),
        "league_name": "Flex Appeal FFL",
    })

    # teams
    teams_json = [
        {
            "owner": t["owner"],
            "short_name": t["short_name"],
            "team_name": t["team_name"],
            "sleeper_handle": t["handle"],
        }
        for t in sorted(teams.values(), key=lambda x: x["owner"])
    ]
    write_json("teams.json", teams_json)

    # standings
    standings = standings_rows(teams, weekly, through) if through else []
    write_json("standings.json", standings)

    # matchups (completed only)
    write_json("matchups.json", matchup_rows(teams, weekly))

    # upcoming: first unscored week with matchup_ids
    agg = cumulative(weekly, through) if through else {}
    upcoming = []
    for w in sorted(matchups_by_week):
        if w in weekly:
            continue
        rows = matchups_by_week[w]
        by_mid = defaultdict(list)
        for r in rows:
            if r.get("matchup_id") is not None:
                by_mid[r["matchup_id"]].append(r)
        for mid in sorted(by_mid):
            pair = by_mid[mid]
            if len(pair) != 2:
                continue
            a, b = pair
            ta, tb = teams[a["roster_id"]], teams[b["roster_id"]]
            aa = agg.get(a["roster_id"], {})
            ab = agg.get(b["roster_id"], {})
            upcoming.append({
                "week": w,
                "team_a": ta["team_name"],
                "owner_a": ta["owner"],
                "record_a": aa.get("record", "0-0"),
                "team_b": tb["team_name"],
                "owner_b": tb["owner"],
                "record_b": ab.get("record", "0-0"),
            })
        if upcoming:
            break  # only next upcoming week
    write_json("upcoming.json", upcoming)

    # season records
    season_recs = season_record_lists(teams, weekly) if weekly else {
        "high_scores": [], "low_scores": [], "closest_games": [], "blowouts": []
    }
    write_json("season_records.json", season_recs)

    # flex rankings
    # Board week = ranking heading into current display week.
    # Week 3 board is LOCKED per league decision.
    board_week = display_week  # heading into this week
    if board_week == 3 and through >= 2:
        flex = locked_week3_flex(teams, weekly)
    else:
        # compute previous board for movement
        prev = None
        if board_week >= 3 and through >= board_week - 2:
            prev_rows = flex_rank_table(teams, weekly, board_week - 2, None)
            prev = {r["owner"]: r["rank"] for r in prev_rows}
        flex = build_flex_live(teams, weekly, board_week, prev)
    write_json("flex_rankings.json", flex)

    # history (include 2026 to date)
    by_owner_agg = {}
    if through:
        full_agg = cumulative(weekly, through)
        for rid, t in teams.items():
            if rid in full_agg:
                by_owner_agg[t["owner"]] = full_agg[rid]
    history = build_history(by_owner_agg, season_recs)
    write_json("history.json", history)

    # validate
    print("\nValidation:")
    assert len(teams_json) == 12, f"expected 12 teams, got {len(teams_json)}"
    assert len(standings) == 12, f"expected 12 standings, got {len(standings)}"
    if flex["rankings"]:
        assert len(flex["rankings"]) == 12
    for name in (
        "meta.json", "flex_rankings.json", "standings.json", "matchups.json",
        "upcoming.json", "season_records.json", "history.json", "teams.json",
    ):
        json.loads((OUT / name).read_text())
    print("  JSON parse OK; 12 teams OK")
    print("  Champions:", history["champions"])
    print("  Flex week:", flex.get("week"), "locked=", flex.get("locked"))
    print("Done.")


if __name__ == "__main__":
    main()

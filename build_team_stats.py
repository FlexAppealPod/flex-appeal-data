#!/usr/bin/env python3
"""Build data/team_stats.json: starter TDs and yards per Flex Appeal FFL team.

Run from /workspace/flex-appeal-data (after scores finalize each week):
  python3 build_team_stats.py                 # all completed weeks
  python3 build_team_stats.py --through-week 3

Counts only players in each team's starting lineup, only for the weeks they
started, over completed weeks (weeks before Sleeper's current NFL week).
Stdlib only. Standalone (does not import build_site_data.py).
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data"
CACHE = Path("/workspace/flex-numbers-2026/api")
PLAYERS_CACHE = Path("/workspace/players_nfl.json")  # ~15 MB; not committed
PLAYERS_MAX_AGE = 7 * 86400

LEAGUE_ID = "1311998258079371264"
BASE = "https://api.sleeper.app/v1"
UA = "FlexAppealFFL-site-data/1.0"
TZ = ZoneInfo("America/Phoenix")

# roster_id -> (owner, short_name)
OWNERS = {
    1: ("Jake Prosser", "Jake"),
    2: ("Matt Zacharias", "Matt Z"),
    3: ("Paul Bacon", "Paul"),
    4: ("Elijah Bruette", "Lij"),
    5: ("Douglas Sullivan", "Doug"),
    6: ("Juan Rodriguez", "Juan"),
    7: ("Derek Bock", "Derek"),
    8: ("Matt Atkinson", "Matt A"),
    9: ("Vlad Barber", "Vlad"),
    10: ("Marc Caballero", "Marc"),
    11: ("Brett Trana", "Brett"),
    12: ("Andrew Reed", "Andrew"),
}
# Optional short-name overrides from data/sources/display_names.json (site_short_names).
_DN_FILE = Path(__file__).resolve().parent / "data" / "sources" / "display_names.json"
if _DN_FILE.exists():
    _short = json.loads(_DN_FILE.read_text()).get("site_short_names", {}).get("names", {})
    OWNERS = {rid: (own, _short.get(own, sh)) for rid, (own, sh) in OWNERS.items()}
# Used only if Sleeper metadata.team_name is blank.
TEAM_NAME_FALLBACK = {1: "Top Play"}


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def get_json(url: str, retries: int = 4):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 404):
                return None
            time.sleep(2 ** (i + 1) if e.code == 429 else 1)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1)
    print(f"  WARN fetch failed: {url} last={last}")
    return None


def fetch_or_cache(url: str, local: Path):
    data = get_json(url)
    if data is not None:
        return data
    if local.exists():
        print(f"  fallback cache: {local}")
        return json.loads(local.read_text())
    raise FileNotFoundError(f"No live data and no cache for {url} / {local}")


def load_players() -> dict:
    fresh = PLAYERS_CACHE.exists() and time.time() - PLAYERS_CACHE.stat().st_mtime < PLAYERS_MAX_AGE
    if not fresh:
        data = get_json(f"{BASE}/players/nfl")
        if data:
            PLAYERS_CACHE.write_text(json.dumps(data))
            return data
    return json.loads(PLAYERS_CACHE.read_text()) if PLAYERS_CACHE.exists() else {}


def player_name(players: dict, pid: str) -> str:
    p = players.get(pid) or {}
    return p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or pid


def td_breakdown(st: dict) -> dict:
    g = lambda k: st.get(k) or 0  # noqa: E731
    ret = g("st_td") if g("st_td") else g("pr_td") + g("kr_td")
    return {
        "pass": g("pass_td"),
        "rush": g("rush_td"),
        "rec": g("rec_td"),
        "idp": g("idp_def_td"),
        "return": ret + g("fum_rec_td"),  # kick/punt return + offensive fumble-recovery TDs
    }


def yard_breakdown(st: dict) -> dict:
    g = lambda k: st.get(k) or 0  # noqa: E731
    return {"pass": g("pass_yd"), "rush": g("rush_yd"), "rec": g("rec_yd")}


def competition_rank(rows, key):
    """1,2,2,4 style ranks (higher value = better)."""
    out, prev, rank = {}, None, 0
    for i, r in enumerate(sorted(rows, key=key, reverse=True), 1):
        v = key(r)
        if v != prev:
            rank, prev = i, v
        out[r["roster_id"]] = rank
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--through-week", type=int, help="last week to include (default: last completed)")
    args = ap.parse_args()

    print("Flex Appeal team stats build")
    league = fetch_or_cache(f"{BASE}/league/{LEAGUE_ID}", CACHE / "league.json")
    users = fetch_or_cache(f"{BASE}/league/{LEAGUE_ID}/users", CACHE / "users.json")
    rosters = fetch_or_cache(f"{BASE}/league/{LEAGUE_ID}/rosters", CACHE / "rosters.json")
    nfl = fetch_or_cache(f"{BASE}/state/nfl", CACHE / "nfl-state.json")
    season = int(league.get("season") or nfl.get("league_season") or 2026)
    scoring = league.get("scoring_settings") or {}

    if args.through_week:
        through = args.through_week
    elif str(nfl.get("season")) == str(season) and nfl.get("season_type") == "regular":
        through = int(nfl.get("week") or 1) - 1  # Sleeper week flips once a week is done
    else:
        through = int(league.get("settings", {}).get("last_scored_leg") or 0)
    if through < 1:
        raise SystemExit("No completed weeks yet.")
    print(f"  season={season} through_week={through}")

    uid = {u["user_id"]: u for u in users}
    team_name = {}
    for r in rosters:
        meta = (uid.get(r.get("owner_id") or "") or {}).get("metadata") or {}
        tn = (meta.get("team_name") or "").strip()
        team_name[r["roster_id"]] = tn or TEAM_NAME_FALLBACK.get(r["roster_id"], OWNERS[r["roster_id"]][0])

    players = load_players()
    tds = defaultdict(lambda: Counter({"pass": 0, "rush": 0, "rec": 0, "idp": 0, "return": 0}))
    yds = defaultdict(lambda: Counter({"pass": 0, "rush": 0, "rec": 0}))
    td_by_player = defaultdict(Counter)
    yd_by_player = defaultdict(Counter)
    weekly = defaultdict(list)
    mismatches, checked = [], 0

    for w in range(1, through + 1):
        matchups = fetch_or_cache(f"{BASE}/league/{LEAGUE_ID}/matchups/{w}", CACHE / f"matchups-w{w}.json")
        stats = fetch_or_cache(f"{BASE}/stats/nfl/regular/{season}/{w}", CACHE / f"stats-w{w}.json")
        for m in matchups:
            rid = m["roster_id"]
            wk_td = wk_yd = 0
            for pid in m.get("starters") or []:
                if not pid or pid == "0":
                    continue
                st = stats.get(pid) or {}
                t, y = td_breakdown(st), yard_breakdown(st)
                tds[rid].update(t)
                yds[rid].update(y)
                name = player_name(players, pid)
                td_by_player[rid][name] += sum(t.values())
                yd_by_player[rid][name] += sum(y.values())
                wk_td += sum(t.values())
                wk_yd += sum(y.values())
                # sanity: rebuild fantasy points from stats vs Sleeper players_points
                calc = sum(scoring.get(k, 0) * v for k, v in st.items() if isinstance(v, (int, float)))
                pp = (m.get("players_points") or {}).get(pid)
                checked += 1
                if pp is None or abs(calc - pp) > 0.05:
                    mismatches.append((w, name, round(calc, 2), pp))
            weekly[rid].append({"week": w, "tds": int(wk_td), "yards": int(wk_yd)})

    teams = []
    for rid in sorted(OWNERS):
        t, y = tds[rid], yds[rid]
        owner, short = OWNERS[rid]
        teams.append({
            "roster_id": rid,
            "team_name": team_name.get(rid, owner),
            "owner": owner,
            "short_name": short,
            "tds": {k: int(t[k]) for k in ("pass", "rush", "rec", "idp", "return")} | {"total": int(sum(t.values()))},
            "yards": {
                "pass": int(y["pass"]), "rush": int(y["rush"]), "rec": int(y["rec"]),
                "scrimmage": int(y["rush"] + y["rec"]), "total": int(sum(y.values())),
            },
            "top_td_scorers": [{"player": p, "tds": int(n)} for p, n in td_by_player[rid].most_common() if n][:3],
            "top_yardage": [{"player": p, "yards": int(n)} for p, n in yd_by_player[rid].most_common() if n][:3],
            "weekly": weekly[rid],
        })
    td_rank = competition_rank(teams, key=lambda r: r["tds"]["total"])
    yd_rank = competition_rank(teams, key=lambda r: r["yards"]["total"])
    for r in teams:
        r["tds_rank"], r["yards_rank"] = td_rank[r["roster_id"]], yd_rank[r["roster_id"]]
    teams.sort(key=lambda r: (-r["tds"]["total"], -r["yards"]["total"], r["team_name"]))
    key_order = ["roster_id", "team_name", "owner", "short_name", "tds", "yards",
                 "tds_rank", "yards_rank", "top_td_scorers", "top_yardage", "weekly"]
    teams = [{k: r[k] for k in key_order} for r in teams]

    out = {
        "season": season,
        "through_week": through,
        "last_updated": now_iso(),
        "notes": "Starters only, weeks started. TDs = passing + rushing + receiving + IDP defensive + return; yards exclude return yards.",
        "teams": teams,
    }
    assert len(teams) == 12, f"expected 12 teams, got {len(teams)}"
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "team_stats.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"  wrote {path}")
    print(f"  points check: {checked} starter-weeks, {len(mismatches)} mismatches")
    for mm in mismatches[:10]:
        print("   ", mm)
    for r in teams:
        print(f"  {r['tds_rank']:>2} {r['short_name']:<7} TD {r['tds']['total']:>3}  yds {r['yards']['total']:>5} (#{r['yards_rank']})")


if __name__ == "__main__":
    main()

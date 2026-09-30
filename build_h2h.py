#!/usr/bin/env python3
"""Build data/h2h_all_time.json: all-time head-to-head records for Flex Appeal FFL.

Run from /workspace/flex-appeal-data (after scores finalize each week):
  python3 build_h2h.py

Rules
- Real head-to-head games only (two rosters sharing a matchup_id). Weekly
  median games are never counted.
- Regular season + every winners-bracket game (incl. 3rd/5th-place games,
  typed "placement"). Losers-bracket (toilet bowl) games are excluded.
- A two-week playoff round is ONE meeting decided by combined score.
- Only completed weeks count (league last_scored_leg, capped by NFL state).
- Managers are keyed by Sleeper owner user_id (team names / roster ids change).
  Deion Hulse owned his 2023 roster, but it had no linked Sleeper account, so it
  is mapped to him by (season, roster_id).
- Mid-season owner changes: data/sources/owner_overrides.json credits a roster's
  games in the listed weeks to another manager (Sleeper keeps only the final owner).
- Pre-Sleeper seasons (2022, ESPN) come from processed files in data/sources/
  (espn_<season>_games.json); raw ESPN dumps are never committed.

Stdlib only. Standalone (does not import build_site_data.py).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "data" / "sources"
OUT = ROOT / "data" / "h2h_all_time.json"
BASE = "https://api.sleeper.app/v1"
UA = "FlexAppealFFL-site-data/1.0"
TZ = ZoneInfo("America/Phoenix")

CURRENT_LEAGUE_ID = "1311998258079371264"
# Known chain (Sleeper's 2025 previous_league_id is null, so 2024 must be listed).
KNOWN_LEAGUES = {
    "2023": "996133920917856256",
    "2024": "1048417672926547968",
    "2025": "1180242334892032000",
    "2026": "1311998258079371264",
}
# Sleeper user_id -> first name (current managers)
FIRST_NAMES = {
    "996131000033988608": "Jake",
    "895431724404973568": "Matt Z",
    "1131743240228667392": "Paul",
    "999903205674872832": "Lij",
    "1000997586435682304": "Doug",
    "1001211933904744448": "Juan",   # was "JRinfidel" in 2023, same user_id
    "1131401008774623232": "Derek",
    "1135747453468438528": "Matt A",
    "1001357488391839744": "Vlad",
    "1001983779910672384": "Marc",
    "1135688681358348288": "Brett",
    "789344057498451968": "Andrew",
}
# Display names for Legacy Owners (ids stay stable)
LEGACY_NAMES = {"jmoneymess": "Jared Messinger", "deion": "Deion Hulse", "anupds23": "Anup Singh",
                "mrpfizer": "Matt Froemming", "jakefitzy": "Jake Fitzgerald", "michaeldewey99": "Mike Dewey"}
# Other ESPN team names seen for a manager (2022 ESPN "Team Hulse" = Deion Hulse)
ESPN_TEAM_ALIASES = {"deion": ["Team Hulse"]}
LABELS = {"current": "Current Managers", "former": "Legacy Owners"}
# Rosters with no linked Sleeper account: (season, roster_id) -> manager label (Deion Hulse owned it in 2023)
UNOWNED_LABELS = {("2023", 3): "Deion"}
TEAM_NAME_FALLBACK = {"996131000033988608": "Got the Beam on Me"}  # Jake's Sleeper team name is blank
OVERRIDES_FILE = SOURCES / "owner_overrides.json"  # mid-season owner changes (manual)
PLACEMENT = {1: "Championship", 3: "3rd-place game", 5: "5th-place game"}


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def get_json(path: str, retries: int = 4):
    url = f"{BASE}/{path}"
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
    raise SystemExit(f"fetch failed: {url} ({last})")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def rec():
    return {"w": 0, "l": 0, "t": 0}


def pct(r) -> float:
    g = r["w"] + r["l"] + r["t"]
    return round((r["w"] + 0.5 * r["t"]) / g, 3) if g else 0.0


def pstr(x: float) -> str:
    return f"{x:.3f}".lstrip("0") if x < 1 else f"{x:.3f}"


def fmt(r) -> str:
    return f"{r['w']}-{r['l']}" + (f"-{r['t']}" if r["t"] else "")


def discover_leagues() -> dict[str, str]:
    leagues = dict(KNOWN_LEAGUES)
    lid = CURRENT_LEAGUE_ID
    seen = set()
    while lid and lid not in seen:
        seen.add(lid)
        lg = get_json(f"league/{lid}")
        if not lg:
            break
        leagues[str(lg["season"])] = lid
        lid = lg.get("previous_league_id")
    return dict(sorted(leagues.items()))


def playoff_rounds(settings: dict, n_rounds: int) -> dict[int, list[int]]:
    start = int(settings.get("playoff_week_start") or 15)
    rtype = int(settings.get("playoff_round_type") or 0)  # 0: 1 wk/round, 1: 2-wk final, 2: 2 wks/round
    rounds, wk = {}, start
    for r in range(1, n_rounds + 1):
        length = 2 if rtype == 2 or (rtype == 1 and r == n_rounds) else 1
        rounds[r] = list(range(wk, wk + length))
        wk += length
    return rounds


def round_length_text(rtype: int) -> str:
    return {0: "1 week per round", 1: "1 week per round, 2-week final", 2: "2 weeks per round"}.get(rtype, f"type {rtype}")


def main() -> None:
    leagues = discover_leagues()
    state = get_json("state/nfl") or {}
    managers: dict[str, dict] = {}      # id -> info
    uid_to_id: dict[str, str] = {}
    games: list[dict] = []
    formats: list[dict] = []
    titles: list[dict] = []
    warnings: list[str] = []
    current_season = max(leagues)

    overrides = json.loads(OVERRIDES_FILE.read_text())["overrides"] if OVERRIDES_FILE.exists() else []

    def override_for(season, rid, weeks):
        # all weeks of the game must fall inside the override range
        for o in overrides:
            if o["season"] == season and o["roster_id"] == rid and all(o["weeks"][0] <= w <= o["weeks"][1] for w in weeks):
                mid = o["manager"]
                if mid not in managers:
                    managers[mid] = {"id": mid, "name": LEGACY_NAMES.get(mid, mid), "sleeper_user_id": o.get("sleeper_user_id"), "seasons": []}
                    if o.get("sleeper_user_id"):
                        uid_to_id[o["sleeper_user_id"]] = mid
                if season not in managers[mid]["seasons"]:
                    managers[mid]["seasons"].append(season)
                    managers[mid]["seasons"].sort()
                managers[mid].setdefault("partial_seasons", {})[season] = f"Weeks {o['weeks'][0]}-{o['weeks'][1]}"
                return mid
        return None

    def manager_for(season, rid, rid2uid, users, weeks=None):
        if weeks:
            ov = override_for(season, rid, weeks)
            if ov:
                return ov
        uid = rid2uid.get(rid)
        if uid is None:
            label = UNOWNED_LABELS.get((season, rid), f"Unowned roster {season}-{rid}")
            mid = slug(label)
            managers.setdefault(mid, {"id": mid, "name": LEGACY_NAMES.get(mid, label), "sleeper_user_id": None, "seasons": [],
                                     "note": f"{season} roster had no linked Sleeper account"})
        else:
            if uid not in uid_to_id:
                mid = slug(FIRST_NAMES.get(uid) or users.get(uid, {}).get("display_name") or uid)  # stable id
                name = FIRST_NAMES.get(uid) or LEGACY_NAMES.get(mid) or users.get(uid, {}).get("display_name") or uid
                uid_to_id[uid] = mid
                managers[mid] = {"id": mid, "name": name, "sleeper_user_id": uid, "seasons": []}
            mid = uid_to_id[uid]
            dn = users.get(uid, {}).get("display_name")
            if dn:
                managers[mid]["sleeper_handle"] = dn  # latest season wins (loop is chronological)
        if season not in managers[mid]["seasons"]:
            managers[mid]["seasons"].append(season)
        return mid

    for season, lid in leagues.items():
        lg = get_json(f"league/{lid}")
        s = lg["settings"]
        users = {u["user_id"]: u for u in (get_json(f"league/{lid}/users") or [])}
        rosters = get_json(f"league/{lid}/rosters") or []
        rid2uid = {r["roster_id"]: r.get("owner_id") for r in rosters}
        wb = get_json(f"league/{lid}/winners_bracket") or []
        lb = get_json(f"league/{lid}/losers_bracket") or []
        last_leg = int(s.get("last_scored_leg") or 0)
        if lg.get("status") != "complete" and str(state.get("season")) == season:
            last_leg = min(last_leg, max(int(state.get("week") or 1) - 1, 0))
        pstart = int(s.get("playoff_week_start") or 15)
        n_rounds = max([g["r"] for g in wb] or [0])
        rounds = playoff_rounds(s, n_rounds)
        reg_weeks = [w for w in range(1, pstart) if w <= last_leg]
        for rid in rid2uid:  # register every season participant
            manager_for(season, rid, rid2uid, users)
        if season == current_season:
            for r in rosters:
                uid = r.get("owner_id")
                if uid:
                    tn = ((users.get(uid) or {}).get("metadata") or {}).get("team_name") or ""
                    managers[uid_to_id[uid]]["current_team_name"] = tn.strip() or TEAM_NAME_FALLBACK.get(uid, "")
                    managers[uid_to_id[uid]]["roster_id"] = r["roster_id"]

        weeks_needed = set(reg_weeks) | {w for ws in rounds.values() for w in ws if w <= last_leg}
        mweek = {w: (get_json(f"league/{lid}/matchups/{w}") or []) for w in sorted(weeks_needed)}
        counts = {"regular": 0, "playoff": 0, "placement": 0}
        for w in reg_weeks:
            pairs = defaultdict(list)
            for m in mweek[w]:
                if m.get("matchup_id") is not None:
                    pairs[m["matchup_id"]].append(m)
            for mid_, pr in sorted(pairs.items()):
                if len(pr) != 2:
                    warnings.append(f"{season} wk{w} matchup {mid_} has {len(pr)} rosters; skipped")
                    continue
                a, b = pr
                games.append({"season": season, "weeks": str(w), "type": "regular", "label": "Regular season",
                              "a": manager_for(season, a["roster_id"], rid2uid, users, [w]), "b": manager_for(season, b["roster_id"], rid2uid, users, [w]),
                              "a_pts": round(a["points"] or 0, 2), "b_pts": round(b["points"] or 0, 2)})
                counts["regular"] += 1

        def pts(rid, ws):
            return round(sum(next((m["points"] or 0) for m in mweek[w] if m["roster_id"] == rid) for w in ws), 2)

        for g in wb:
            ws = rounds.get(g["r"], [])
            if not g.get("t1") or not g.get("t2") or not ws or max(ws) > last_leg:
                continue
            prior_losers = {x.get("l") for x in wb if x["r"] < g["r"] and x.get("l")}
            if g.get("p") in PLACEMENT:
                label = PLACEMENT[g["p"]]
            elif g["t1"] in prior_losers and g["t2"] in prior_losers:
                label = "5th-place semifinal"
            else:
                label = "Quarterfinal" if g["r"] == 1 and n_rounds >= 3 else ("Semifinal" if g["r"] == n_rounds - 1 else f"Round {g['r']}")
            gtype = "playoff" if label in ("Championship", "Quarterfinal", "Semifinal") or label.startswith("Round") else "placement"
            a_pts, b_pts = pts(g["t1"], ws), pts(g["t2"], ws)
            if a_pts != b_pts and g.get("w") and (g["t1"] if a_pts > b_pts else g["t2"]) != g["w"]:
                warnings.append(f"{season} bracket m{g.get('m')}: combined-score winner differs from Sleeper bracket")
            ga = {"season": season, "weeks": "-".join(map(str, ws)), "type": gtype, "label": label,
                  "a": manager_for(season, g["t1"], rid2uid, users, ws), "b": manager_for(season, g["t2"], rid2uid, users, ws),
                  "a_pts": a_pts, "b_pts": b_pts}
            games.append(ga)
            counts[gtype] += 1
            if g.get("p") == 1 and a_pts != b_pts:
                win, lose = (ga["a"], ga["b"]) if a_pts > b_pts else (ga["b"], ga["a"])
                titles.append({"season": season, "winner": managers[win]["name"], "loser": managers[lose]["name"],
                               "score": f"{max(a_pts, b_pts):.2f}-{min(a_pts, b_pts):.2f}", "weeks": ga["weeks"]})
        lb_done = sum(1 for g in lb if g.get("t1") and g.get("t2") and rounds.get(g["r"]) and max(rounds[g["r"]]) <= last_leg)
        rtype = int(s.get("playoff_round_type") or 0)
        formats.append({
            "season": season, "platform": "Sleeper", "league_id": lid, "league_name": lg.get("name"), "teams": int(s.get("num_teams") or len(rosters)),
            "regular_weeks": f"1-{pstart - 1}", "playoff_start_week": pstart, "playoff_teams": int(s.get("playoff_teams") or 0),
            "round_length": round_length_text(rtype),
            "playoff_round_weeks": {str(r): ws for r, ws in rounds.items()},
            "status": lg.get("status"), "completed_through_week": last_leg,
            "games_counted": counts, "losers_bracket_games_excluded": lb_done,
        })

    # ---- pre-Sleeper seasons from processed source files (ESPN)
    for src in sorted(SOURCES.glob("espn_*_games.json")):
        sd = json.loads(src.read_text())
        fmt_ = dict(sd["season_format"])
        season = fmt_["season"]
        counts = {"regular": 0, "playoff": 0, "placement": 0}
        for t in sd["teams"]:
            mid = t["manager"]
            if mid not in managers:
                managers[mid] = {"id": mid, "name": LEGACY_NAMES.get(mid, t["espn_owner"]), "sleeper_user_id": None, "seasons": []}
            names = [n for n in [t.get("team_name", "").strip()] + ESPN_TEAM_ALIASES.get(mid, []) if n]
            if names:
                managers[mid].setdefault("espn_team_names", {})[season] = names
            if season not in managers[mid]["seasons"]:
                managers[mid]["seasons"].append(season)
                managers[mid]["seasons"].sort()
        for g in sd["games"]:
            games.append({k: g[k] for k in ("season", "weeks", "type", "label", "a", "b", "a_pts", "b_pts")})
            counts[g["type"]] += 1
            if g["label"] == "Championship" and g["a_pts"] != g["b_pts"]:
                win, lose = (g["a"], g["b"]) if g["a_pts"] > g["b_pts"] else (g["b"], g["a"])
                titles.append({"season": season, "winner": managers[win]["name"], "loser": managers[lose]["name"],
                               "score": f"{max(g['a_pts'], g['b_pts']):.2f}-{min(g['a_pts'], g['b_pts']):.2f}", "weeks": g["weeks"]})
        fmt_.update(games_counted=counts, losers_bracket_games_excluded=len(sd.get("excluded_consolation_games", [])))
        formats.append(fmt_)
        leagues[season] = fmt_["league_id"]
    leagues = dict(sorted(leagues.items()))
    formats.sort(key=lambda f: f["season"])
    titles.sort(key=lambda t: t["season"])
    games.sort(key=lambda g: (g["season"], int(g["weeks"].split("-")[0])))

    for g in games:
        g["winner"] = "tie" if g["a_pts"] == g["b_pts"] else (g["a"] if g["a_pts"] > g["b_pts"] else g["b"])

    # ---- tallies
    matrix: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: {"w": 0, "l": 0, "t": 0, "games": 0}))
    gp = defaultdict(lambda: {"regular_games": 0, "playoff_games": 0})
    for g in games:
        for x, y in ((g["a"], g["b"]), (g["b"], g["a"])):
            c = matrix[x][y]
            c["games"] += 1
            c["t" if g["winner"] == "tie" else ("w" if g["winner"] == x else "l")] += 1
            gp[x]["regular_games" if g["type"] == "regular" else "playoff_games"] += 1

    cur_ids = [managers[uid_to_id[uid]]["id"] for uid in
               sorted((r for r in FIRST_NAMES), key=lambda u: managers.get(uid_to_id.get(u, ""), {}).get("roster_id", 99))
               if uid in uid_to_id and current_season in managers[uid_to_id[uid]]["seasons"]]
    # any current-season manager not in FIRST_NAMES
    for mid, m in managers.items():
        if current_season in m["seasons"] and mid not in cur_ids:
            cur_ids.append(mid)
    cur = set(cur_ids)
    former_ids = sorted((m for m in managers if m not in cur), key=lambda m: (managers[m]["seasons"][0], managers[m]["name"].lower()))
    for mid, m in managers.items():
        m["current"] = mid in cur

    def sum_vs(x, opps):
        r = rec()
        for y in opps:
            c = matrix[x].get(y)
            if c and y != x:
                for k in "wlt":
                    r[k] += c[k]
        return r

    totals = {}
    for mid in list(cur_ids) + former_ids:
        vc, vf = sum_vs(mid, cur_ids), sum_vs(mid, former_ids)
        al = {k: vc[k] + vf[k] for k in "wlt"}
        totals[mid] = {"vs_current": {**vc, "pct": pct(vc)}, "vs_former": {**vf, "pct": pct(vf)}, "all_games": {**al, "pct": pct(al)},
                       "regular_games": gp[mid]["regular_games"], "playoff_games": gp[mid]["playoff_games"],
                       "games_vs_former": vf["w"] + vf["l"] + vf["t"],
                       "total_games": gp[mid]["regular_games"] + gp[mid]["playoff_games"]}

    # ---- sanity checks
    for season in leagues:
        sw, sl = defaultdict(int), defaultdict(int)
        for g in (g for g in games if g["season"] == season and g["winner"] != "tie"):
            sw[g["winner"]] += 1
            sl[g["b"] if g["winner"] == g["a"] else g["a"]] += 1
        assert sum(sw.values()) == sum(sl.values()), season
    for x in matrix:
        for y, c in matrix[x].items():
            o = matrix[y][x]
            assert (c["w"], c["l"], c["t"], c["games"]) == (o["l"], o["w"], o["t"], o["games"]), (x, y)
    assert sum(t["vs_current"]["w"] for m, t in totals.items() if m in cur) == sum(t["vs_current"]["l"] for m, t in totals.items() if m in cur)

    # ---- notable (computed)
    nm = lambda i: managers[i]["name"]
    pairs = [(a, b, matrix[a][b]) for i, a in enumerate(cur_ids) for b in cur_ids[i + 1:] if matrix[a].get(b)]
    notable = []
    if pairs:
        a, b, c = max(pairs, key=lambda p: (abs(p[2]["w"] - p[2]["l"]), p[2]["games"]))
        lead, trail, r = (a, b, c) if c["w"] >= c["l"] else (b, a, matrix[b][a])
        notable.append(f"Most lopsided rivalry: {nm(lead)} is {fmt(r)} all-time vs {nm(trail)}.")
        a, b, c = max(pairs, key=lambda p: (p[2]["games"], -abs(p[2]["w"] - p[2]["l"])))
        lead, trail, r = (a, b, c) if c["w"] >= c["l"] else (b, a, matrix[b][a])
        notable.append(f"Most-played rivalry: {nm(lead)} vs {nm(trail)}, {r['games']} meetings ({nm(lead)} leads {fmt(r)})." if r["w"] != r["l"]
                       else f"Most-played rivalry: {nm(a)} vs {nm(b)}, {r['games']} meetings, dead even at {fmt(r)}.")
        unbeaten = sorted(((x, y, matrix[x][y]) for x in cur_ids for y in cur_ids
                           if x != y and matrix[x].get(y) and matrix[x][y]["l"] == 0 and matrix[x][y]["t"] == 0 and matrix[x][y]["w"] >= 3),
                          key=lambda p: -p[2]["w"])
        if unbeaten:
            notable.append("Unbeaten vs someone (3+ meetings): " + "; ".join(f"{nm(x)} {fmt(c)} vs {nm(y)}" for x, y, c in unbeaten) + ".")
        even = [p for p in pairs if p[2]["w"] == p[2]["l"]]
        big_even = [p for p in even if p[2]["games"] >= 4]
        notable.append(f"{len(even)} of {len(pairs)} current-manager rivalries are dead even" +
                       (": " + ", ".join(f"{nm(a)}-{nm(b)} {fmt(c)}" for a, b, c in big_even) + "." if big_even else "."))
    best_cur = max(cur_ids, key=lambda m: (totals[m]["vs_current"]["pct"], totals[m]["vs_current"]["w"]))
    best_all = max(cur_ids, key=lambda m: (totals[m]["all_games"]["pct"], totals[m]["all_games"]["w"]))
    worst_all = min(cur_ids, key=lambda m: (totals[m]["all_games"]["pct"], -totals[m]["all_games"]["l"]))
    notable.append(f"Best record vs current managers: {nm(best_cur)} {fmt(totals[best_cur]['vs_current'])} ({pstr(totals[best_cur]['vs_current']['pct'])}).")
    if best_all != best_cur:
        notable.append(f"Best record counting every game: {nm(best_all)} {fmt(totals[best_all]['all_games'])} ({pstr(totals[best_all]['all_games']['pct'])}).")
    notable.append(f"Toughest all-time road: {nm(worst_all)} {fmt(totals[worst_all]['all_games'])} ({pstr(totals[worst_all]['all_games']['pct'])}).")
    bf = max(cur_ids, key=lambda m: (totals[m]["vs_former"]["w"] - totals[m]["vs_former"]["l"], totals[m]["vs_former"]["w"]))
    if totals[bf]["games_vs_former"]:
        notable.append(f"Best vs {LABELS['former']}: {nm(bf)} {fmt(totals[bf]['vs_former'])}.")
    if titles:
        notable.append("Title games: " + "; ".join(f"{t['season']} {t['winner']} over {t['loser']} {t['score']}" for t in titles) + ".")

    last_fmt = formats[-1]
    out = {
        "meta": {
            "generated_at": now_iso(),
            "seasons_covered": list(leagues),
            "through": {"season": last_fmt["season"], "week": last_fmt["completed_through_week"]},
            "total_games": len(games),
            "rules": ("Real head-to-head games only (same Sleeper matchup_id); weekly median games are not counted. "
                      "Regular season plus every winners-bracket game, including 3rd/5th-place games (type 'placement'). "
                      "A two-week playoff round counts as one meeting decided by combined score. "
                      "Losers-bracket (toilet bowl) games, Week 18 and unplayed weeks are excluded. "
                      "Managers are keyed by Sleeper account, so team-name and roster changes don't split records. "
                      "2022 was played on ESPN (8 teams); 2023 onward on Sleeper. "
                      "Mid-season owner changes come from data/sources/owner_overrides.json (2024 roster 2: Mike Dewey Weeks 1-7, Matt Z from Week 8). "
                      "Deion Hulse's 2023 Sleeper roster had no linked Sleeper account; it is mapped to him by league records."),
            "labels": LABELS,
            "matrix_note": "matrix[row][col] = row manager's record vs col manager; pairs that never met are omitted.",
            "warnings": warnings,
        },
        "current_managers": cur_ids,
        "former_managers": former_ids,
        "managers": [managers[m] for m in cur_ids + former_ids],
        "totals": totals,
        "matrix": {x: {y: matrix[x][y] for y in cur_ids + former_ids if y in matrix[x]} for x in cur_ids + former_ids},
        "former_manager_rows": [{"id": m, "name": nm(m), "seasons": managers[m]["seasons"],
                                 "vs_current": totals[m]["vs_current"], "vs_former": totals[m]["vs_former"], "all_games": totals[m]["all_games"]}
                                for m in former_ids],
        "season_formats": formats,
        "title_games": titles,
        "notable": notable,
        "games": games,
    }
    for m in out["managers"]:
        m.pop("roster_id", None)
    games_out = out.pop("games")
    body = json.dumps(out, indent=1, ensure_ascii=False)
    glines = ",\n  ".join(json.dumps(g, ensure_ascii=False, separators=(",", ":")) for g in games_out)
    OUT.write_text(body[:-2] + ',\n "games": [\n  ' + glines + "\n ]\n}\n")
    json.loads(OUT.read_text())  # must parse
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes): {len(games)} games, seasons {list(leagues)}, through {out['meta']['through']}")
    for w in warnings:
        print("  WARN", w)
    for m in cur_ids:
        t = totals[m]
        print(f"  {nm(m):7} vs current {fmt(t['vs_current'])}  all {fmt(t['all_games'])}")


if __name__ == "__main__":
    main()

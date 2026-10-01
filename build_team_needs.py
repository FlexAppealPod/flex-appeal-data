#!/usr/bin/env python3
"""Build data/team_needs.json: per-team lineup grades, needs, flags, waiver ideas and trade fits.

Run from /workspace/flex-appeal-data (daily is fine; best after waivers / injury news):
  python3 build_team_needs.py

Same transparent method for every team (also written into the JSON as "method"):
  * Player value = Sleeper's projected points per game for the next 3 weeks, re-scored with
    this league's scoring settings (half-PPR + IDP). No projection -> season PPG.
  * Best lineup = highest-value legal lineup from the current roster (QB, RB x2, WR x2,
    FLEX x2 [RB/WR/TE], REC_FLEX [WR/TE], IDP_FLEX [DL/LB/DB]); Out/IR/PUP/Sus players skipped.
  * Each slot group is graded on two numbers vs the league: best-lineup value per slot (60%)
    and actual starter points per slot so far from Sleeper matchups (40%), as z-scores.
    z >= +0.75 Strength, z <= -0.75 Need, otherwise OK.
  * Waiver ideas: unrostered players at the team's Need positions, ranked by half-PPR PPG over
    the last 3 completed weeks plus a bump (up to +3) for Sleeper 48h trending adds.
  * Trade fits: other teams with surplus (Strength + startable bench above league median)
    at a position this team needs. Fit framing only, no specific offers.

Sources (public Sleeper API): /state/nfl, /league, /users, /rosters, /matchups/{wk},
/players/nfl (cached at /workspace/sleeper_players.json), /players/nfl/trending/add,
api.sleeper.com /stats, /projections and /schedule. Stdlib only.
No private data (no Flex Rankings formula, no emails).
"""
from __future__ import annotations

import itertools
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

from build_team_stats import (  # shared helpers + display-name conventions
    BASE, LEAGUE_ID, OWNERS, TEAM_NAME_FALLBACK, get_json, now_iso,
)

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data"
PLAYERS_CACHE = Path("/workspace/sleeper_players.json")  # ~15 MB; not committed
PLAYERS_MAX_AGE = 20 * 3600  # injury statuses matter here; refresh roughly daily
API2 = "https://api.sleeper.com"
POS_Q = "&".join(f"position[]={p}" for p in ("QB", "RB", "WR", "TE", "DL", "LB", "DB"))

# slot group -> (eligible fantasy positions, slot labels)
GROUPS = {
    "QB": (("QB",), ["QB"]),
    "RB": (("RB",), ["RB1", "RB2"]),
    "WR": (("WR",), ["WR1", "WR2"]),
    "FLEX": (("RB", "WR", "TE"), ["FLEX1", "FLEX2"]),
    "REC_FLEX": (("WR", "TE"), ["REC_FLEX"]),
    "IDP": (("DL", "LB", "DB"), ["IDP_FLEX"]),
}
GROUP_LABEL = {"QB": "QB", "RB": "RB", "WR": "WR", "FLEX": "FLEX (RB/WR/TE)",
               "REC_FLEX": "REC_FLEX (WR/TE)", "IDP": "IDP_FLEX (DL/LB/DB)"}
SLOT_TO_GROUP = {"QB": "QB", "RB": "RB", "WR": "WR", "FLEX": "FLEX", "REC_FLEX": "REC_FLEX",
                 "IDP_FLEX": "IDP"}
NEED_POSITIONS = {"QB": ["QB"], "RB": ["RB"], "WR": ["WR"], "FLEX": ["RB", "WR", "TE"],
                  "REC_FLEX": ["WR", "TE"], "IDP": ["IDP"]}
TRADE_POS = ("QB", "RB", "WR", "TE", "IDP")
POS_GROUP_FOR_SURPLUS = {"QB": ["QB"], "RB": ["RB"], "WR": ["WR"], "TE": ["REC_FLEX", "FLEX"], "IDP": ["IDP"]}
UNAVAILABLE = {"Out", "IR", "PUP", "Sus", "NA", "DNR"}
FLAG_STATUSES = {"Questionable", "Doubtful", "Out", "IR", "PUP", "Sus", "NA", "DNR"}
W_ROSTER, W_ACTUAL = 0.6, 0.4
Z_STRONG, Z_NEED = 0.75, -0.75
PROJ_WEEKS = 3
RECENT_WEEKS = 3
TREND_BONUS = 2.0
DISCLAIMER = "Just for fun. Not financial advice. Your league-mates may disagree."


def load_players() -> dict:
    fresh = PLAYERS_CACHE.exists() and time.time() - PLAYERS_CACHE.stat().st_mtime < PLAYERS_MAX_AGE
    if not fresh:
        data = get_json(f"{BASE}/players/nfl")
        if data:
            PLAYERS_CACHE.write_text(json.dumps(data))
            return data
    if PLAYERS_CACHE.exists():
        return json.loads(PLAYERS_CACHE.read_text())
    raise SystemExit("No Sleeper players file available")


def score(stats: dict, scoring: dict) -> float:
    return sum(float(stats.get(k) or 0) * float(v) for k, v in scoring.items() if k in stats)


def zscores(vals: dict) -> dict:
    xs = list(vals.values())
    mu = statistics.mean(xs)
    sd = statistics.pstdev(xs) or 1.0
    return {k: (v - mu) / sd for k, v in vals.items()}


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else 0.0


def r1(x):
    return None if x is None else round(float(x), 1)


def main():
    print("Flex Appeal team needs build")
    nfl = get_json(f"{BASE}/state/nfl") or {}
    league = get_json(f"{BASE}/league/{LEAGUE_ID}")
    users = get_json(f"{BASE}/league/{LEAGUE_ID}/users")
    rosters = get_json(f"{BASE}/league/{LEAGUE_ID}/rosters")
    if not (league and users and rosters):
        raise SystemExit("Sleeper league/users/rosters unavailable; aborting (no partial publish)")
    season = int(league.get("season") or 2026)
    scoring = league.get("scoring_settings") or {}
    budget = int((league.get("settings") or {}).get("waiver_budget") or 100)
    in_season = str(nfl.get("season")) == str(season) and nfl.get("season_type") == "regular"
    week = int(nfl.get("week") or 1) if in_season else 1  # upcoming / in-progress week
    done = list(range(1, week))  # completed weeks (same convention as build_team_stats.py)
    if not done:
        raise SystemExit("No completed weeks yet")
    roster_positions = league.get("roster_positions") or []
    players = load_players()

    # ---- weekly stats (completed weeks) and projections (next PROJ_WEEKS weeks) ----
    week_pts = defaultdict(dict)  # pid -> {week: pts}
    live_info = {}  # fresher player objects from stats/projections
    for w in done:
        rows = get_json(f"{API2}/stats/nfl/{season}/{w}?season_type=regular&{POS_Q}") or []
        if not rows:
            raise SystemExit(f"No stats for week {w}; aborting")
        for row in rows:
            st = row.get("stats") or {}
            if st.get("gp"):
                week_pts[row["player_id"]][w] = score(st, scoring)
            if row.get("player"):
                live_info[row["player_id"]] = row["player"]
    proj = defaultdict(list)
    proj_next = {}
    proj_weeks_used = []
    for w in range(week, min(week + PROJ_WEEKS, 19)):
        rows = get_json(f"{API2}/projections/nfl/{season}/{w}?season_type=regular&{POS_Q}") or []
        if not rows:
            continue
        proj_weeks_used.append(w)
        for row in rows:
            st = row.get("stats") or {}
            if st.get("gp") or score(st, scoring):
                p = score(st, scoring)
                proj[row["player_id"]].append(p)
                if w == week:
                    proj_next[row["player_id"]] = p
            if row.get("player"):
                live_info[row["player_id"]] = row["player"]  # projections are the freshest
    print(f"  completed weeks {done}; projections for weeks {proj_weeks_used or 'none'}")

    sched = get_json(f"{API2}/schedule/nfl/regular/{season}") or []
    playing = defaultdict(set)
    for g in sched:
        playing[int(g["week"])] |= {g.get("home"), g.get("away")}
    all_teams = set().union(*playing.values()) if playing else set()
    byes = {w: sorted(all_teams - playing[w]) for w in (week, week + 1) if playing.get(w)}

    trending_raw = get_json(f"{BASE}/players/nfl/trending/add?lookback_hours=48&limit=100") or []
    trending = {t["player_id"]: int(t.get("count") or 0) for t in trending_raw}
    tmax = max(trending.values(), default=0)

    def info(pid):
        p = dict(players.get(pid) or {})
        lv = live_info.get(pid)
        if lv:
            for k in ("injury_status", "injury_body_part", "team", "position", "fantasy_positions",
                      "first_name", "last_name"):
                if k in lv:
                    p[k] = lv[k]
        return p

    def fpos(pid) -> set:
        p = info(pid)
        fp = set(p.get("fantasy_positions") or ([p["position"]] if p.get("position") else []))
        out = fp & {"QB", "RB", "WR", "TE"}
        if fp & {"DL", "LB", "DB"}:
            out |= (fp & {"DL", "LB", "DB"})
        return out

    def tpos(pid) -> str | None:  # trade/depth position bucket
        fp = fpos(pid)
        for p in ("QB", "RB", "WR", "TE"):
            if p in fp:
                return p
        return "IDP" if fp & {"DL", "LB", "DB"} else None

    def disp_pos(pid):
        p = info(pid)
        fp = [x for x in (p.get("fantasy_positions") or []) if x in ("QB", "RB", "WR", "TE", "DL", "LB", "DB")]
        return fp[0] if fp else p.get("position")

    def name(pid):
        p = info(pid)
        return (p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or str(pid))

    def inj(pid):
        s = info(pid).get("injury_status")
        return s or None

    def season_ppg(pid):
        wp = week_pts.get(pid) or {}
        return (sum(wp.values()) / len(wp), len(wp)) if wp else (None, 0)

    def recent_ppg(pid):
        wp = {w: v for w, v in (week_pts.get(pid) or {}).items() if w > week - 1 - RECENT_WEEKS}
        return (sum(wp.values()) / len(wp), len(wp)) if wp else (None, 0)

    def value(pid):
        if proj.get(pid):
            return sum(proj[pid]) / len(proj[pid]), "projection"
        sp, _ = season_ppg(pid)
        return (sp or 0.0), ("season_ppg" if sp is not None else "none")

    def pcard(pid, extra=None):
        p = info(pid)
        v, src = value(pid)
        sp, gp = season_ppg(pid)
        d = {"player_id": pid, "name": name(pid), "position": disp_pos(pid), "nfl_team": p.get("team") or "FA",
             "injury_status": inj(pid), "value_ppg": r1(v), "value_source": src,
             "season_ppg": r1(sp), "games": gp}
        if extra:
            d.update(extra)
        return d

    # ---- team identity ----
    uid = {u["user_id"]: u for u in users}
    teams = {}
    rostered = set()
    for r in rosters:
        rid = int(r["roster_id"])
        u = uid.get(r.get("owner_id") or "") or {}
        meta = u.get("metadata") or {}
        avatar = meta.get("avatar") or (f"https://sleepercdn.com/avatars/thumbs/{u['avatar']}" if u.get("avatar") else None)
        allp = [str(p) for p in (r.get("players") or [])]
        reserve = {str(p) for p in (r.get("reserve") or [])}
        taxi = {str(p) for p in (r.get("taxi") or [])}
        rostered |= set(allp) | reserve | taxi
        used = int((r.get("settings") or {}).get("waiver_budget_used") or 0)
        teams[rid] = {
            "rid": rid, "owner": OWNERS[rid][1],
            "team_name": (meta.get("team_name") or "").strip() or TEAM_NAME_FALLBACK.get(rid, OWNERS[rid][1]),
            "avatar": avatar,
            "active": [p for p in allp if p not in reserve and p not in taxi],
            "reserve": sorted(reserve), "set_starters": [str(p) for p in (r.get("starters") or [])],
            "faab": {"budget": budget, "spent": used, "remaining": max(budget - used, 0)},
            "record": {"wins": int((r.get("settings") or {}).get("wins") or 0),
                       "losses": int((r.get("settings") or {}).get("losses") or 0)},
        }

    # ---- best lineup per team ----
    def best_lineup(pids):
        avail = [p for p in pids if inj(p) not in UNAVAILABLE and fpos(p)]
        vals = {p: value(p)[0] for p in avail}
        used, lineup = set(), {}

        def take(group):
            elig, labels = GROUPS[group]
            pool = sorted([p for p in avail if p not in used and fpos(p) & set(elig)], key=lambda p: -vals[p])
            for lab, p in zip(labels, pool):
                lineup[lab] = p
                used.add(p)
            for lab in labels[len(pool):]:
                lineup[lab] = None

        for g in ("QB", "RB", "WR", "IDP"):
            take(g)
        # FLEX x2 + REC_FLEX jointly (small brute force so the WR/TE-only slot is used well)
        pool = sorted([p for p in avail if p not in used and fpos(p) & {"RB", "WR", "TE"}], key=lambda p: -vals[p])[:8]
        best, best_v = (None, None, None), -1.0
        for rec in [p for p in pool if fpos(p) & {"WR", "TE"}] + [None]:
            rest = [p for p in pool if p != rec][:2]
            rest += [None] * (2 - len(rest))
            v = (vals.get(rec, 0) if rec else 0) + sum(vals.get(p, 0) for p in rest if p)
            if v > best_v:
                best, best_v = (rec, rest[0], rest[1]), v
        lineup["REC_FLEX"], lineup["FLEX1"], lineup["FLEX2"] = best
        if lineup["FLEX1"] and lineup["FLEX2"] and vals[lineup["FLEX2"]] > vals[lineup["FLEX1"]]:
            lineup["FLEX1"], lineup["FLEX2"] = lineup["FLEX2"], lineup["FLEX1"]
        return lineup, vals

    for t in teams.values():
        t["lineup"], t["vals"] = best_lineup(t["active"])
        starters = {p for p in t["lineup"].values() if p}
        t["bench"] = [p for p in t["active"] if p not in starters]

    slot_vals = {lab: {rid: (t["vals"].get(t["lineup"][lab], 0.0) if t["lineup"][lab] else 0.0)
                       for rid, t in teams.items()}
                 for g in GROUPS for lab in GROUPS[g][1]}
    slot_median = {lab: med(v.values()) for lab, v in slot_vals.items()}
    roster_group = {g: {rid: statistics.mean(slot_vals[lab][rid] for lab in GROUPS[g][1]) for rid in teams}
                    for g in GROUPS}

    # ---- actual starter points per slot (Sleeper matchups) ----
    act_sum = defaultdict(lambda: defaultdict(float))
    act_n = defaultdict(lambda: defaultdict(int))
    for w in done:
        for m in get_json(f"{BASE}/league/{LEAGUE_ID}/matchups/{w}") or []:
            rid = int(m["roster_id"])
            sts, spts = m.get("starters") or [], m.get("starters_points") or []
            for i, slot in enumerate(roster_positions):
                g = SLOT_TO_GROUP.get(slot)
                if not g or i >= len(sts):
                    continue
                act_sum[rid][g] += float(spts[i] if i < len(spts) and spts[i] is not None else 0.0)
                act_n[rid][g] += 1
    actual_group = {g: {rid: (act_sum[rid][g] / act_n[rid][g] if act_n[rid][g] else 0.0) for rid in teams}
                    for g in GROUPS}

    # ---- grades ----
    zr = {g: zscores(roster_group[g]) for g in GROUPS}
    za = {g: zscores(actual_group[g]) for g in GROUPS}
    comp = {g: {rid: W_ROSTER * zr[g][rid] + W_ACTUAL * za[g][rid] for rid in teams} for g in GROUPS}

    def grade(z):
        return "Strength" if z >= Z_STRONG else "Need" if z <= Z_NEED else "OK"

    # ---- depth ----
    thr_slot = {"QB": ["QB"], "RB": ["FLEX2"], "WR": ["FLEX2", "REC_FLEX"], "TE": ["FLEX2", "REC_FLEX"],
                "IDP": ["IDP_FLEX"]}
    startable_thr = {p: 0.85 * min(slot_median[s] for s in thr_slot[p]) for p in TRADE_POS}
    for t in teams.values():
        bc, sc = Counter(), Counter()
        for p in t["bench"]:
            tp = tpos(p)
            if not tp:
                continue
            bc[tp] += 1
            if inj(p) not in UNAVAILABLE and t["vals"].get(p, 0) >= startable_thr[tp]:
                sc[tp] += 1
        t["bench_counts"] = {p: bc[p] for p in TRADE_POS}
        t["startable_bench"] = {p: sc[p] for p in TRADE_POS}
    bench_med = {p: med([t["bench_counts"][p] for t in teams.values()]) for p in TRADE_POS}
    start_med = {p: med([t["startable_bench"][p] for t in teams.values()]) for p in TRADE_POS}

    # ---- surplus ----
    for rid, t in teams.items():
        t["surplus"] = []
        for p in TRADE_POS:
            g_ok = any(grade(comp[g][rid]) == "Strength" for g in POS_GROUP_FOR_SURPLUS[p])
            if p == "TE":  # a TE "surplus" only if a TE is actually among the strong flex starters or bench
                g_ok = g_ok and any(tpos(x) == "TE" for x in t["bench"])
            if g_ok and t["startable_bench"][p] > start_med[p]:
                bench_ps = sorted([x for x in t["bench"] if tpos(x) == p and inj(x) not in UNAVAILABLE],
                                  key=lambda x: -t["vals"].get(x, 0))
                t["surplus"].append({
                    "position": p, "startable_bench": t["startable_bench"][p], "league_median": start_med[p],
                    "examples": [name(x) for x in bench_ps[:2]],
                    "excess": t["startable_bench"][p] - start_med[p]})

    # ---- per-team output ----
    out_teams = []
    fa_pool_cache = {}

    def fa_pool(pos_bucket):
        if pos_bucket in fa_pool_cache:
            return fa_pool_cache[pos_bucket]
        cands = []
        ids = set(week_pts) | set(proj) | set(trending)
        for pid in ids:
            if pid in rostered:
                continue
            p = info(pid)
            if not p.get("team") or inj(pid) in UNAVAILABLE:
                continue
            if tpos(pid) != pos_bucket:
                continue
            if proj_next and pid not in proj_next:
                continue  # Sleeper doesn't project him to play next week (inactive, backup, etc.)
            rp, rg = recent_ppg(pid)
            tr = trending.get(pid, 0)
            if rp is None and not tr:
                continue
            bump = TREND_BONUS * (math.log1p(tr) / math.log1p(tmax)) if tr and tmax else 0.0
            # one-game samples get half credit so a single spot start doesn't top the list
            cands.append({"pid": pid, "recent": rp or 0.0, "games": rg, "trend": tr,
                          "score": (rp or 0.0) * min(rg, 2) / 2 + bump})
        cands.sort(key=lambda c: -c["score"])
        fa_pool_cache[pos_bucket] = cands
        return cands

    for rid in sorted(teams):
        t = teams[rid]
        slots, needs, strengths = [], [], []
        for g, (elig, labels) in GROUPS.items():
            z = comp[g][rid]
            lab_g = grade(z)
            rank = 1 + sum(1 for o in teams if comp[g][o] > z)
            members = []
            for lab in labels:
                pid = t["lineup"][lab]
                members.append({"slot": lab, **(pcard(pid) if pid else {"player_id": None, "name": "(empty)"}),
                                "league_median_slot_ppg": r1(slot_median[lab])})
            # weakest slot relative to its median drives the reason
            weakest = min(labels, key=lambda lab: slot_vals[lab][rid] - slot_median[lab])
            strongest = max(labels, key=lambda lab: slot_vals[lab][rid] - slot_median[lab])
            key = weakest if lab_g != "Strength" else strongest
            pid = t["lineup"][key]
            who = f"{name(pid)}" if pid else "nobody healthy"
            reason = (f"{key} ({who}) projects {slot_vals[key][rid]:.1f} PPG vs league median {slot_median[key]:.1f}; "
                      f"{GROUP_LABEL[g].split(' ')[0]} starters have scored {actual_group[g][rid]:.1f} per slot per week "
                      f"(median {med(actual_group[g].values()):.1f})")
            slot = {
                "group": g, "label": GROUP_LABEL[g], "eligible": list(elig), "slots": labels,
                "grade": lab_g, "z": round(z, 2), "rank": rank,
                "percentile": round(100 * (12 - rank) / 11) if len(teams) == 12 else None,
                "roster_value_ppg": r1(roster_group[g][rid]),
                "league_median_roster_value_ppg": r1(med(roster_group[g].values())),
                "actual_starter_ppg": r1(actual_group[g][rid]),
                "league_median_actual_ppg": r1(med(actual_group[g].values())),
                "players": members, "reason": reason,
            }
            slots.append(slot)
            if lab_g == "Need":
                needs.append({"group": g, "label": GROUP_LABEL[g], "positions": NEED_POSITIONS[g],
                              "z": round(z, 2), "rank": rank, "reason": reason})
            elif lab_g == "Strength":
                strengths.append({"group": g, "label": GROUP_LABEL[g], "z": round(z, 2), "rank": rank,
                                  "reason": reason})
        needs.sort(key=lambda n: n["z"])
        strengths.sort(key=lambda n: -n["z"])
        weakest_groups = sorted(slots, key=lambda s: s["z"])
        if not needs:
            target_groups = [s["group"] for s in weakest_groups[:2]]
            focus_note = (f"No group grades as a Need; waiver ideas target the lowest-graded spots "
                          f"({', '.join(GROUP_LABEL[g] for g in target_groups)}).")
        else:
            target_groups = [n["group"] for n in needs]
            focus_note = None

        # ---- flags ----
        flags = []
        lineup_ids = [p for p in t["lineup"].values() if p]
        set_ids = [p for p in t["set_starters"] if p and p != "0"]
        watch = list(dict.fromkeys(set_ids + lineup_ids))
        for pid in watch:
            s = inj(pid)
            if s in FLAG_STATUSES:
                sev = "high" if s in UNAVAILABLE else "medium" if s == "Doubtful" else "low"
                where = "in current Sleeper lineup" if pid in set_ids else "in best lineup"
                part = info(pid).get("injury_body_part")
                flags.append({"type": "injury", "severity": sev, "player": pcard(pid),
                              "text": f"{disp_pos(pid)} {name(pid)} ({info(pid).get('team') or 'FA'}) is {s}"
                                      + (f" ({part.lower()})" if part else "") + f", {where}"})
        empty = sum(1 for i, p in enumerate(t["set_starters"]) if (not p or p == "0") and i < len(roster_positions))
        if empty:
            flags.append({"type": "empty_slot", "severity": "high",
                          "text": f"{empty} empty starting slot{'s' if empty > 1 else ''} in the current Sleeper lineup"})
        for w, bye_teams in byes.items():
            on_bye = [p for p in watch if info(p).get("team") in bye_teams]
            if on_bye:
                bits = []
                for p in on_bye:
                    cover = [b for b in t["bench"] if info(b).get("team") not in bye_teams
                             and inj(b) not in UNAVAILABLE and fpos(b) & fpos(p)]
                    bits.append(f"{disp_pos(p)} {name(p)} ({info(p).get('team')})"
                                + ("" if cover else f" - no healthy bench {disp_pos(p)}"))
                flags.append({"type": "bye", "severity": "medium" if w == week else "low", "week": w,
                              "players": [pcard(p) for p in on_bye],
                              "text": f"Week {w} bye: " + ", ".join(bits)})
        for p in ("QB", "RB", "WR", "TE", "IDP"):
            n = t["bench_counts"][p]
            thin = (p in ("RB", "WR") and n <= 1 and n < bench_med[p]) or (p in ("QB", "TE", "IDP") and n == 0 and bench_med[p] >= 1)
            if thin:
                flags.append({"type": "depth", "severity": "low", "position": p,
                              "text": f"Thin {p} depth: {n} on the bench (league median {bench_med[p]:g})"})
        ir = [p for p in t["reserve"]]
        if ir:
            flags.append({"type": "ir", "severity": "info",
                          "text": "On IR: " + ", ".join(f"{disp_pos(p)} {name(p)}" for p in ir)})

        # ---- FA suggestions (round-robin across need groups, most severe first) ----
        rem = t["faab"]["remaining"]
        sugg, seen = [], set()
        queues = []
        for g in target_groups:
            buckets = NEED_POSITIONS[g]
            merged = sorted(itertools.chain.from_iterable(fa_pool(b) for b in buckets), key=lambda c: -c["score"])
            queues.append((g, merged))
        i = 0
        while len(sugg) < 5 and any(q for _, q in queues) and i < 200:
            g, q = queues[i % len(queues)]
            i += 1
            while q and q[0]["pid"] in seen:
                q.pop(0)
            if not q:
                continue
            c = q.pop(0)
            seen.add(c["pid"])
            pid = c["pid"]
            gmed = med(actual_group[g].values())  # actual points vs actual points
            ratio = (c["recent"] / gmed) if gmed else 0
            # FAAB tiers: recent PPG vs league median actual starter PPG for that slot group
            lo, hi = ((0.10, 0.20) if ratio >= 1.0 else (0.05, 0.10) if ratio >= 0.8 else
                      (0.02, 0.05) if ratio >= 0.6 else (0.0, 0.02))
            fmin, fmax = int(rem * lo), max(int(rem * lo), int(round(rem * hi)))
            weakest_lab = min(GROUPS[g][1], key=lambda lab: slot_vals[lab][rid])
            cur = slot_vals[weakest_lab][rid]
            v, _ = value(pid)
            upgrade = (c["recent"] > cur) or (v > cur)
            sugg.append({
                "player_id": pid, "name": name(pid), "position": disp_pos(pid),
                "nfl_team": info(pid).get("team") or "FA", "injury_status": inj(pid),
                "recent_ppg": r1(c["recent"]), "recent_games": c["games"],
                "recent_weeks": [w for w in done if w > week - 1 - RECENT_WEEKS],
                "projected_next_week": r1(proj_next.get(pid)),
                "trending_adds_48h": c["trend"],
                "for_need": g, "for_need_label": GROUP_LABEL[g],
                "fit": (f"Would compete with your {weakest_lab} ({cur:.1f} PPG value)" if upgrade
                        else f"Depth for {GROUP_LABEL[g].split(' ')[0]}"),
                "faab_range": {"min": fmin, "max": fmax,
                               "text": "$0 (no FAAB left; $0 bids are valid)" if rem == 0 else
                               (f"${fmin}" if fmin == fmax else f"${fmin}-${fmax}") + f" of ${rem} left"},
            })

        # ---- trade fits ----
        fits = []
        my_surplus = {s["position"] for s in t["surplus"]}
        for n in needs:
            for orid, o in teams.items():
                if orid == rid:
                    continue
                theirs = [s for s in o["surplus"] if s["position"] in n["positions"]]
                soft = False
                if not theirs:
                    # softer fit: strong group at the needed position, with any startable bench there
                    for p in n["positions"]:
                        gs = POS_GROUP_FOR_SURPLUS[p]
                        if any(grade(comp[gg][orid]) == "Strength" for gg in gs) and o["startable_bench"][p] >= 1:
                            theirs.append({"position": p, "startable_bench": o["startable_bench"][p],
                                           "examples": [], "excess": 0})
                            soft = True
                if not theirs:
                    # possible fit: a startable backup at the needed position, and they're OK or better there
                    for p in n["positions"]:
                        gs = POS_GROUP_FOR_SURPLUS[p]
                        if o["startable_bench"][p] >= 1 and all(grade(comp[gg][orid]) != "Need" for gg in gs):
                            theirs.append({"position": p, "startable_bench": o["startable_bench"][p],
                                           "examples": [], "excess": -0.5})
                            soft = True
                if not theirs:
                    continue
                their_needs = {pp for gg in GROUPS if grade(comp[gg][orid]) == "Need" for pp in NEED_POSITIONS[gg]}
                mutual = sorted(my_surplus & their_needs)
                s0 = max(theirs, key=lambda s: s["excess"])
                fscore = s0["excess"] + (0 if soft else 1) + 1.5 * bool(mutual) - 0.1 * n["z"]
                if mutual:
                    offer = f"Your {', '.join(mutual)} depth lines up with a need of theirs."
                elif my_surplus:
                    offer = (f"Your {', '.join(sorted(my_surplus))} depth is your clearest trade chip, "
                             "though it is not a listed need of theirs.")
                else:
                    deep = [p for p in TRADE_POS if p not in n["positions"]
                            and (t["startable_bench"][p] > 0 or t["bench_counts"][p] > bench_med[p])]
                    deep.sort(key=lambda p: (-t["startable_bench"][p], -(t["bench_counts"][p] - bench_med[p])))
                    offer = ("No clear surplus here; " + (f"bench {deep[0]} depth, " if deep else "")
                             + "future picks or FAAB could help balance a deal.")
                fits.append({
                    "for_need": n["group"], "for_need_label": n["label"],
                    "partner_roster_id": orid, "partner_owner": o["owner"], "partner_team_name": o["team_name"],
                    "partner_surplus": s0["position"],
                    "partner_depth_examples": s0["examples"],
                    "fit_strength": ("strong" if (mutual and not soft) else "good" if not soft else "possible"),
                    "mutual": bool(mutual),
                    "why": (f"{o['owner']} has extra startable {s0['position']} depth "
                            f"({s0['startable_bench']} on the bench)" if not soft else
                            f"{o['owner']} is strong at {s0['position']} with some bench depth" if s0["excess"] == 0 else
                            f"{o['owner']} has a startable backup {s0['position']} on the bench")
                           + (f" and could use {', '.join(mutual)}." if mutual else "."),
                    "you_could_offer": offer, "_score": fscore,
                })
        fits.sort(key=lambda f: -f["_score"])
        top, used_p = [], set()
        for f in fits:  # one entry per partner
            if f["partner_roster_id"] in used_p:
                continue
            used_p.add(f["partner_roster_id"])
            f.pop("_score")
            top.append(f)
            if len(top) == 3:
                break
        if not needs:
            trade_note = "No Need-graded spot, so no trade fits listed."
        elif not top:
            trade_note = "No other team shows surplus at this team's Need positions right now."
        else:
            trade_note = None

        out_teams.append({
            "roster_id": rid, "owner": t["owner"], "team_name": t["team_name"], "avatar": t["avatar"],
            "record": t["record"], "faab": t["faab"],
            "summary": (f"Top need: {needs[0]['label']}" if needs else "No glaring needs")
                       + (f"; strongest: {strengths[0]['label']}" if strengths else ""),
            "slots": slots, "needs": needs, "strengths": strengths,
            "surplus": [{k: v for k, v in s.items() if k != "excess"} for s in t["surplus"]],
            "depth": {"bench_counts": t["bench_counts"], "league_median_bench": bench_med,
                      "startable_bench": t["startable_bench"], "league_median_startable": start_med},
            "flags": flags,
            "fa_suggestions": sugg, "fa_note": focus_note,
            "trade_fits": top, "trade_note": trade_note,
        })

    method = (
        "Every team is graded the same way. Each player gets a value: Sleeper's projected points per game "
        f"for the next {len(proj_weeks_used) or PROJ_WEEKS} weeks, re-scored with our league's half-PPR and IDP "
        "settings (season PPG if Sleeper has no projection). We build each team's best legal lineup from its "
        "current roster (QB, 2 RB, 2 WR, 2 FLEX, REC_FLEX, IDP_FLEX), skipping players who are Out or on IR. "
        "Each slot group is then compared with the other 11 teams on two things: that best-lineup value "
        "(60%) and what the team's starters in those slots have actually scored so far this season (40%). "
        "Clearly above the league is a Strength, clearly below is a Need, and the rest is OK. Waiver ideas "
        "are unrostered players at a team's Need positions, ranked by points per game over the last three "
        "weeks with a small bump for players trending in Sleeper adds; Out/IR players are left off and FAAB "
        "ranges scale with the budget the team has left. Trade fits point to teams with extra startable "
        "depth where you are thin. It is a conversation starter, not an offer sheet."
    )
    out = {
        "updated_at": now_iso(),
        "season": season,
        "week": week,
        "completed_weeks": done,
        "projection_weeks": proj_weeks_used,
        "bye_weeks": {str(w): b for w, b in byes.items()},
        "league": {"league_id": LEAGUE_ID, "name": league.get("name"), "faab_budget": budget,
                   "lineup_slots": [s for s in roster_positions if s != "BN"],
                   "bench_slots": roster_positions.count("BN")},
        "method": method,
        "grade_scale": {"Strength": f"z >= +{Z_STRONG}", "OK": "between", "Need": f"z <= {Z_NEED}"},
        "league_medians": {"slot_value_ppg": {k: r1(v) for k, v in slot_median.items()},
                           "group_actual_ppg": {g: r1(med(actual_group[g].values())) for g in GROUPS}},
        "disclaimer": DISCLAIMER,
        "source": "Sleeper API (public): rosters, matchups, players, stats, projections, trending adds",
        "notes": [
            "PPG figures are half-PPR with this league's IDP scoring.",
            "actual_starter_ppg = points scored by whoever started in that slot group, per slot per week, completed weeks only.",
            "value_ppg = Sleeper projection average for projection_weeks (league scoring); value_source says which was used.",
            "trending_adds_48h = Sleeper-wide add count over the last 48 hours (all Sleeper leagues).",
            "FAAB ranges are suggestions scaled to remaining budget, not bids; $0 bids are valid in Sleeper. "
            "Tiers by recent PPG vs the league median starter PPG for that slot: >=100% -> 10-20% of remaining, "
            "80-100% -> 5-10%, 60-80% -> 2-5%, below -> 0-2%.",
            "week = Sleeper's current NFL week (the upcoming / in-progress week); completed_weeks are scored.",
            "Display names: first names, last initial only for shared names (Matt Z, Matt A).",
        ],
        "teams": out_teams,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "team_needs.json"
    text = json.dumps(out, indent=2, ensure_ascii=False) + "\n"
    json.loads(text)
    path.write_text(text)
    print(f"  wrote {path} ({len(text)//1024} KB)")
    for tm in out_teams:
        fa = tm["fa_suggestions"][0] if tm["fa_suggestions"] else None
        print(f"  {tm['roster_id']:>2} {tm['owner']:<7} need={[n['group'] for n in tm['needs']]} "
              f"str={[s['group'] for s in tm['strengths']]} surplus={[s['position'] for s in tm['surplus']]} "
              f"fa1={fa['name'] + ' ' + fa['position'] + ' ' + str(fa['recent_ppg']) + ' ' + fa['faab_range']['text'] if fa else None} "
              f"fits={[f['partner_owner'] for f in tm['trade_fits']]} flags={len(tm['flags'])}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build data/transactions.json: Flex Appeal FFL roster moves (trades, waivers, FA, FAAB).

Run from /workspace/flex-appeal-data (anytime; weekly after waivers clear is ideal):
  python3 build_transactions.py
  python3 build_transactions.py --no-failed-claims   # omit losing waiver bids

Sources (public Sleeper API): /league/{id}/transactions/{leg} for legs 0..18,
/league/{id} (waiver_budget), /rosters (waiver_budget_used), /users (team names),
/traded_picks, /state/nfl, and /players/nfl (cached at /workspace/sleeper_players.json).
Display names / owner map / fetch helpers come from build_team_stats.py, which
reads data/sources/display_names.json (first names; last initial only when shared).
Stdlib only. No private data (no Flex formula, no emails).
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from build_team_stats import (  # shared helpers + display-name conventions
    BASE, CACHE, LEAGUE_ID, OWNERS, TEAM_NAME_FALLBACK, TZ, fetch_or_cache, get_json, now_iso,
)

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data"
PLAYERS_CACHE = Path("/workspace/sleeper_players.json")  # ~15 MB; not committed
PLAYERS_MAX_AGE = 7 * 86400
MAX_LEG = 18
TYPES = ("trade", "waiver", "free_agent", "commissioner")


def iso(ms) -> str | None:
    return datetime.fromtimestamp(ms / 1000, TZ).isoformat(timespec="seconds") if ms else None


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


def make_player(players: dict):
    def player(pid: str) -> dict:
        p = players.get(str(pid)) or {}
        name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or str(pid)
        pos = p.get("position") or ((p.get("fantasy_positions") or [None])[0])
        return {"player_id": str(pid), "name": name, "position": pos, "nfl_team": p.get("team") or "FA"}
    return player


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-failed-claims", action="store_true", help="omit failed_claims and outbid details")
    args = ap.parse_args()
    show_failed = not args.no_failed_claims

    print("Flex Appeal transactions build")
    league = fetch_or_cache(f"{BASE}/league/{LEAGUE_ID}", CACHE / "league.json")
    users = fetch_or_cache(f"{BASE}/league/{LEAGUE_ID}/users", CACHE / "users.json")
    rosters = fetch_or_cache(f"{BASE}/league/{LEAGUE_ID}/rosters", CACHE / "rosters.json")
    nfl = get_json(f"{BASE}/state/nfl") or {}
    traded_picks = get_json(f"{BASE}/league/{LEAGUE_ID}/traded_picks") or []
    draft = get_json(f"{BASE}/draft/{league.get('draft_id')}") if league.get("draft_id") else None
    season = int(league.get("season") or 2026)
    budget = int((league.get("settings") or {}).get("waiver_budget") or 0)

    # Phase boundaries: before the startup draft = offseason; draft -> NFL season start = preseason.
    draft_start = (draft or {}).get("start_time") or 0
    ssd = nfl.get("season_start_date") if str(nfl.get("season")) == str(season) else None
    season_start = int(datetime.fromisoformat(ssd).replace(tzinfo=TZ).timestamp() * 1000) if ssd else 0

    uid = {u["user_id"]: u for u in users}
    team_name = {}
    for r in rosters:
        meta = (uid.get(r.get("owner_id") or "") or {}).get("metadata") or {}
        tn = (meta.get("team_name") or "").strip()
        team_name[r["roster_id"]] = tn or TEAM_NAME_FALLBACK.get(r["roster_id"], OWNERS[r["roster_id"]][0])

    def mgr(rid) -> dict:
        rid = int(rid)
        return {"roster_id": rid, "manager": OWNERS[rid][1], "team_name": team_name.get(rid, OWNERS[rid][0])}

    def name(rid) -> str:
        return OWNERS[int(rid)][1]

    player = make_player(load_players())

    raw = []
    for leg in range(0, MAX_LEG + 1):
        data = get_json(f"{BASE}/league/{LEAGUE_ID}/transactions/{leg}")
        if data is None:
            cache = CACHE / f"transactions-w{leg}.json"
            if not cache.exists():
                raise SystemExit(f"Could not fetch transactions for leg {leg}; aborting (no partial publish)")
            data = json.loads(cache.read_text())
        raw.extend(data)
    seen, uniq = set(), []
    for t in raw:  # guard against a transaction appearing in two legs
        if t["transaction_id"] not in seen:
            seen.add(t["transaction_id"])
            uniq.append(t)
    raw = uniq
    status_counts = Counter((t.get("type"), t.get("status")) for t in raw)
    print(f"  fetched {len(raw)} transactions: {dict(status_counts)}")

    def phase_week(t):
        ts = t.get("status_updated") or t.get("created") or 0
        if draft_start and ts < draft_start:
            return "offseason", 0
        if season_start and ts < season_start:
            return "preseason", 0
        return "regular", int(t.get("leg") or 0)

    completed = [t for t in raw if t.get("status") == "complete"]
    failed = [t for t in raw if t.get("status") == "failed" and t.get("type") == "waiver"]

    # Failed bids in the same waiver run on the same player (for "outbid" details).
    failed_by_run = defaultdict(list)
    for t in failed:
        for pid in t.get("adds") or {}:
            failed_by_run[(t.get("status_updated"), pid)].append(t)
    winner_by_run = {}
    for t in completed:
        if t.get("type") == "waiver":
            for pid in t.get("adds") or {}:
                winner_by_run[(t.get("status_updated"), pid)] = t

    def sort_key(t):
        return (t.get("status_updated") or t.get("created") or 0, t.get("created") or 0, t["transaction_id"])

    transactions = []
    for t in sorted(completed, key=sort_key, reverse=True):
        ttype = t.get("type")
        phase, week = phase_week(t)
        adds, drops = t.get("adds") or {}, t.get("drops") or {}
        rids = sorted({int(r) for r in (t.get("roster_ids") or [])} | {int(v) for v in adds.values()}
                      | {int(v) for v in drops.values()})
        picks = t.get("draft_picks") or []
        faab_moves = t.get("waiver_budget") or []
        sides = []
        for rid in rids:
            side = mgr(rid)
            side["adds"] = [player(p) for p, r in adds.items() if int(r) == rid]
            side["drops"] = [player(p) for p, r in drops.items() if int(r) == rid]
            if ttype == "trade":
                side["picks_received"] = [
                    {"season": int(p["season"]), "round": int(p["round"]),
                     "original_owner": name(p["roster_id"]), "from": name(p["previous_owner_id"])}
                    for p in picks if int(p["owner_id"]) == rid]
                side["picks_sent"] = [
                    {"season": int(p["season"]), "round": int(p["round"]),
                     "original_owner": name(p["roster_id"]), "to": name(p["owner_id"])}
                    for p in picks if int(p["previous_owner_id"]) == rid]
                side["faab_received"] = sum(int(m.get("amount") or 0) for m in faab_moves if int(m["receiver"]) == rid)
                side["faab_sent"] = sum(int(m.get("amount") or 0) for m in faab_moves if int(m["sender"]) == rid)
            sides.append(side)

        if ttype == "trade":
            parts = []
            for s in sides:
                got = [p["name"] for p in s["adds"]]
                got += [f"{pk['season']} Rd {pk['round']} pick ("
                        f"{'own' if pk['original_owner'] == s['manager'] else pk['original_owner']})"
                        for pk in s["picks_received"]]
                if s["faab_received"]:
                    got.append(f"${s['faab_received']} FAAB")
                parts.append(f"{s['manager']} gets {', '.join(got) if got else 'nothing'}")
            headline = "; ".join(parts)
        else:
            s = sides[0] if sides else {"manager": "?", "adds": [], "drops": []}
            bits = []
            if s["adds"]:
                bits.append("adds " + ", ".join(p["name"] for p in s["adds"]))
            if s["drops"]:
                bits.append("drops " + ", ".join(p["name"] for p in s["drops"]))
            headline = f"{s['manager']} " + " / ".join(bits) if bits else s["manager"]
            if ttype == "waiver":
                headline += f" (${int((t.get('settings') or {}).get('waiver_bid') or 0)} FAAB)"

        entry = {
            "id": t["transaction_id"],
            "type": ttype,
            "week": week,
            "sleeper_leg": int(t.get("leg") or 0),
            "phase": phase,
            "created": iso(t.get("created")),
            "processed": iso(t.get("status_updated")),
            "status": t.get("status"),
            "managers": [mgr(r) for r in rids],
            "headline": headline,
            "sides": sides,
        }
        if ttype == "waiver":
            entry["faab_bid"] = int((t.get("settings") or {}).get("waiver_bid") or 0)
            entry["winning_claim"] = True
            if show_failed:
                losers = []
                for pid in adds:
                    for f in failed_by_run.get((t.get("status_updated"), pid), []):
                        losers.append({"manager": name(f["roster_ids"][0]),
                                       "bid": int((f.get("settings") or {}).get("waiver_bid") or 0)})
                entry["outbid"] = sorted(losers, key=lambda x: -x["bid"])
        transactions.append(entry)

    failed_claims = []
    if show_failed:
        for t in sorted(failed, key=sort_key, reverse=True):
            phase, week = phase_week(t)
            rid = int(t["roster_ids"][0])
            note = (t.get("metadata") or {}).get("notes") or ""
            reason = ("outbid" if "claimed by another" in note else
                      "roster_full" if "too many players" in note else "other")
            pid = next(iter(t.get("adds") or {}), None)
            win = winner_by_run.get((t.get("status_updated"), pid)) if pid else None
            failed_claims.append({
                "id": t["transaction_id"], "week": week, "phase": phase,
                "created": iso(t.get("created")), "processed": iso(t.get("status_updated")),
                "status": "failed", **mgr(rid),
                "player": player(pid) if pid else None,
                "would_drop": [player(p) for p in (t.get("drops") or {})],
                "faab_bid": int((t.get("settings") or {}).get("waiver_bid") or 0),
                "reason": reason, "sleeper_note": note,
                "won_by": ({"manager": name(win["roster_ids"][0]),
                            "bid": int((win.get("settings") or {}).get("waiver_bid") or 0)} if win else None),
            })

    # FAAB
    used = {r["roster_id"]: int((r.get("settings") or {}).get("waiver_budget_used") or 0) for r in rosters}
    claims, spent, big, fails = Counter(), Counter(), {}, Counter()
    faab_in, faab_out = Counter(), Counter()
    for e in transactions:
        if e["type"] == "waiver":
            rid = e["sides"][0]["roster_id"]
            claims[rid] += 1
            spent[rid] += e["faab_bid"]
            if rid not in big or e["faab_bid"] > big[rid]["bid"]:
                big[rid] = {"bid": e["faab_bid"], "player": e["sides"][0]["adds"][0]["name"] if e["sides"][0]["adds"] else None,
                            "week": e["week"]}
        if e["type"] == "trade":
            for s in e["sides"]:
                faab_in[s["roster_id"]] += s["faab_received"]
                faab_out[s["roster_id"]] += s["faab_sent"]
    for t in failed:
        fails[int(t["roster_ids"][0])] += 1
    faab = []
    for rid in sorted(OWNERS):
        row = mgr(rid) | {
            "budget": budget,
            "spent": used.get(rid, 0),
            "remaining": budget - used.get(rid, 0),
            "claims_won": claims[rid],
            "winning_bids_total": spent[rid],
            "biggest_bid": big.get(rid),
        }
        if show_failed:
            row["failed_claims"] = fails[rid]
        if faab_in[rid] or faab_out[rid]:
            row["faab_traded_in"], row["faab_traded_out"] = faab_in[rid], faab_out[rid]
        faab.append(row)
    faab.sort(key=lambda r: (-r["remaining"], r["manager"]))
    mismatch = [r["manager"] for r in faab if r["spent"] != r["winning_bids_total"]]

    # Summary
    def type_counts(rows):
        c = Counter(e["type"] for e in rows)
        return {k: c.get(k, 0) for k in TYPES} | {"total": len(rows)}

    per_mgr = defaultdict(Counter)
    for e in transactions:
        for s in e["sides"]:
            c = per_mgr[s["roster_id"]]
            c["transactions"] += 1
            c[e["type"]] += 1
            c["players_added"] += len(s["adds"])
            c["players_dropped"] += len(s["drops"])
            if e["phase"] == "regular":
                c["regular_season_transactions"] += 1
    by_manager = []
    for rid in sorted(OWNERS):
        c = per_mgr[rid]
        by_manager.append(mgr(rid) | {
            "transactions": c["transactions"],
            "regular_season_transactions": c["regular_season_transactions"],
            "trades": c["trade"], "waiver_claims": c["waiver"], "free_agent_moves": c["free_agent"],
            "players_added": c["players_added"], "players_dropped": c["players_dropped"]})
    by_manager.sort(key=lambda r: (-r["transactions"], r["manager"]))

    def leaders(key):
        top = max((r[key] for r in by_manager), default=0)
        return {"managers": [r["manager"] for r in by_manager if r[key] == top and top], "count": top}

    wins = [e for e in transactions if e["type"] == "waiver"]
    best = max(wins, key=lambda e: (e["faab_bid"], e["processed"] or ""), default=None)
    biggest_bid = None
    if best:
        biggest_bid = {"bid": best["faab_bid"], "manager": best["sides"][0]["manager"],
                       "team_name": best["sides"][0]["team_name"],
                       "player": best["sides"][0]["adds"][0] if best["sides"][0]["adds"] else None,
                       "week": best["week"], "processed": best["processed"], "id": best["id"]}

    add_count, add_by = Counter(), defaultdict(list)
    drop_count = Counter()
    pinfo = {}
    for e in transactions:
        if e["type"] in ("waiver", "free_agent", "commissioner"):
            for s in e["sides"]:
                for p in s["adds"]:
                    add_count[p["player_id"]] += 1
                    add_by[p["player_id"]].append(s["manager"])
                    pinfo[p["player_id"]] = p
                for p in s["drops"]:
                    drop_count[p["player_id"]] += 1
                    pinfo[p["player_id"]] = p
    most_added = [pinfo[pid] | {"adds": n, "added_by": list(dict.fromkeys(add_by[pid]))}
                  for pid, n in sorted(add_count.items(), key=lambda kv: (-kv[1], pinfo[kv[0]]["name"])) if n >= 2][:10]
    most_dropped = [pinfo[pid] | {"drops": n}
                    for pid, n in sorted(drop_count.items(), key=lambda kv: (-kv[1], pinfo[kv[0]]["name"])) if n >= 2][:10]

    by_week = Counter(e["week"] for e in transactions)
    regular = [e for e in transactions if e["phase"] == "regular"]
    summary = {
        "totals_by_type": type_counts(transactions),
        "regular_season_totals_by_type": type_counts(regular),
        "totals_by_phase": dict(Counter(e["phase"] for e in transactions)),
        "totals_by_week": {str(w): by_week[w] for w in sorted(by_week)},
        "failed_waiver_claims": len(failed) if show_failed else None,
        "most_active_manager": leaders("transactions"),
        "most_active_manager_regular_season": leaders("regular_season_transactions"),
        "most_trades": leaders("trades"),
        "biggest_faab_bid": biggest_bid,
        "most_added_players": most_added,
        "most_dropped_players": most_dropped,
        "by_manager": by_manager,
    }
    if not show_failed:
        del summary["failed_waiver_claims"]

    picks_out = []
    for p in sorted(traded_picks, key=lambda p: (int(p["season"]), int(p["round"]), int(p["roster_id"]))):
        picks_out.append({"season": int(p["season"]), "round": int(p["round"]),
                          "original_owner": name(p["roster_id"]), "current_owner": name(p["owner_id"]),
                          "current_team": team_name.get(int(p["owner_id"])),
                          "draft_complete": int(p["season"]) < season or (
                              int(p["season"]) == season and (draft or {}).get("status") == "complete")})

    notes = [
        "Completed transactions only (failed/pending skipped), newest first by processed time.",
        "created = when the move/claim/offer was made; processed = when Sleeper completed it "
        "(waivers clear in a batch; trades may sit in review). Times are America/Phoenix.",
        "week = Sleeper transaction leg for regular-season moves; 0 for offseason (before the "
        f"{season} startup draft) and preseason (draft to NFL season start). phase says which; "
        "sleeper_leg keeps Sleeper's raw leg (Sleeper files all pre-season moves under leg 1).",
        "sides[] = per-manager adds/drops. In trades adds = players received, drops = players sent, "
        "plus picks_received/picks_sent (original_owner = whose pick it was) and FAAB swapped.",
        "FAAB: spent = Sleeper rosters waiver_budget_used; remaining = budget - spent. A $0 bid is a valid claim.",
        "Display names: first names, last initial only for shared names (Matt Z, Matt A).",
        "draft_picks lists current ownership of every traded pick per Sleeper (includes pick trades "
        "made in the prior league before this season's league existed, which are not in transactions).",
    ]
    if show_failed:
        notes.append("failed_claims = UNSUCCESSFUL waiver claims (not roster moves): reason outbid or "
                     "roster_full, with the bid. outbid[] on a winning claim lists the losing bids on that player.")

    out = {
        "meta": {
            "generated_at": now_iso(),
            "season": season,
            "league": {"league_id": LEAGUE_ID, "name": league.get("name"),
                       "waiver_type": "FAAB" if (league.get("settings") or {}).get("waiver_type") == 2 else
                       (league.get("settings") or {}).get("waiver_type")},
            "faab_budget": budget,
            "current_week": int(nfl.get("week") or 0) if str(nfl.get("season")) == str(season) else None,
            "counts": {"transactions": len(transactions), "failed_claims": len(failed_claims) if show_failed else None},
            "checks": {"faab_spent_matches_winning_bids": not mismatch, "faab_mismatch_managers": mismatch},
            "source": "Sleeper API (public)",
            "notes": notes,
        },
        "transactions": transactions,
        "faab": faab,
        "summary": summary,
        "draft_picks": picks_out,
    }
    if show_failed:
        out["failed_claims"] = failed_claims
    else:
        del out["meta"]["counts"]["failed_claims"]

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "transactions.json"
    text = json.dumps(out, indent=2, ensure_ascii=False) + "\n"
    json.loads(text)  # validate
    path.write_text(text)
    print(f"  wrote {path} ({len(text)//1024} KB)")
    print(f"  totals: {summary['totals_by_type']}  failed claims: {len(failed_claims)}")
    print(f"  FAAB check: {'OK' if not mismatch else 'MISMATCH ' + ', '.join(mismatch)}")
    for e in transactions[:5]:
        print(f"   {e['processed']} wk{e['week']} {e['type']:<10} {e['headline']}")


if __name__ == "__main__":
    main()

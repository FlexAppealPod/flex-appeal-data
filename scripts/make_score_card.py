#!/usr/bin/env python3
"""Flex Appeal FFL score-card image for X posts (@FlexAppealFFL).

Builds a 1600x900 (X 16:9) or 1080x1350 (portrait) PNG of the current week's
six matchups, the weekly median, and top starting-lineup performers, straight
from Sleeper's public API (no login, no screenshots).

  python3 scripts/make_score_card.py --label TNF --out /workspace/score-cards/w4-tnf.png
  python3 scripts/make_score_card.py --week 4 --label "MNF final" --out w4-final.png
  python3 scripts/make_score_card.py --label "Sunday early" --format square --out w4-early-sq.png

--label containing "final" (or --final) marks winners (W/L) and titles the card
"WEEK N FINAL". Otherwise the title is "WEEK N · <LABEL> CHECK-IN".

Data:
  league / users / rosters / matchups  Sleeper, fetched live every run
  players (names, NFL team)            Sleeper /players/nfl, cached at
                                       /workspace/cache/sleeper/players_nfl.json,
                                       refreshed at most once per day
  NFL game state ("to play" counts)    ESPN public scoreboard; optional, the card
                                       just omits the counts if it fails
                                       (--no-schedule to skip it)
Names: manager display names / team-name fallbacks come from build_site_data.py
(NAME_BY_USER_ID, SHORT, display_names.json site_short_names); team names from
Sleeper user metadata.team_name.

Requires Pillow (python3 -m pip install pillow). Fonts: Barlow Condensed +
Outfit (the league site's fonts); system copies are used if present, else the
TTFs are downloaded once from github.com/google/fonts into /workspace/cache/fonts.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
LEAGUE_ID = "1311998258079371264"
BASE = "https://api.sleeper.app/v1"
ESPN_SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
                   "?dates={season}&seasontype={stype}&week={week}")
UA = "FlexAppealFFL-score-card/1.0"
TZ = ZoneInfo("America/Phoenix")
CACHE = Path("/workspace/cache")
PLAYERS_CACHE = CACHE / "sleeper" / "players_nfl.json"
FONT_CACHE = CACHE / "fonts"
PLAYERS_MAX_AGE_S = 24 * 3600

# League site palette (flex-appeal-ffl.grok.me light theme CSS variables)
BG = "#ffffff"
SURFACE = "#f4f4f4"
LINE = "#e2e2e2"
FG = "#0a0a0a"
MUTED = "#5c5c5c"
ACCENT = "#e00818"  # site --color-gold (the red accent)
ACCENT_INK = "#ffffff"

# Optional short team names for the card (roster_id -> text). Empty = Sleeper name,
# auto-shrunk / truncated to fit.
TEAM_SHORT_OVERRIDES: dict[int, str] = {}

# Fallback if build_site_data.py can't be imported (roster_id -> display name)
ROSTER_FALLBACK = {1: "Jake", 2: "Matt Z", 3: "Paul", 4: "Lij", 5: "Doug", 6: "Juan",
                   7: "Derek", 8: "Matt A", 9: "Vlad", 10: "Marc", 11: "Brett", 12: "Andrew"}

# ESPN abbreviation -> Sleeper abbreviation
ESPN_TO_SLEEPER = {"WSH": "WAS"}

FONT_FILES = {
    "cond-xbold": ("BarlowCondensed-ExtraBold.ttf", "ofl/barlowcondensed/BarlowCondensed-ExtraBold.ttf"),
    "cond-bold": ("BarlowCondensed-Bold.ttf", "ofl/barlowcondensed/BarlowCondensed-Bold.ttf"),
    "cond-semi": ("BarlowCondensed-SemiBold.ttf", "ofl/barlowcondensed/BarlowCondensed-SemiBold.ttf"),
    "sans": ("Outfit-VariableFont_wght.ttf", "ofl/outfit/Outfit%5Bwght%5D.ttf"),
}
SYSTEM_FONT_DIRS = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
                    Path.home() / ".fonts", Path.home() / ".local/share/fonts",
                    Path("/Library/Fonts"), Path.home() / "Library/Fonts"]


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def get_json(url: str, retries: int = 3, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1 + i)
    raise RuntimeError(f"fetch failed: {url}: {last}")


def load_players(refresh: bool = False) -> dict:
    fresh = PLAYERS_CACHE.exists() and time.time() - PLAYERS_CACHE.stat().st_mtime < PLAYERS_MAX_AGE_S
    if fresh and not refresh:
        return json.loads(PLAYERS_CACHE.read_text())
    try:
        data = get_json(f"{BASE}/players/nfl", timeout=120)
        PLAYERS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PLAYERS_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(PLAYERS_CACHE)
        return data
    except Exception as e:  # noqa: BLE001
        if PLAYERS_CACHE.exists():
            print(f"WARN players refresh failed ({e}); using stale cache", file=sys.stderr)
            return json.loads(PLAYERS_CACHE.read_text())
        raise


def name_maps():
    """(owner_name(user, owner_id) -> full name, SHORT, TEAM_NAME_FALLBACK) from build_site_data."""
    try:
        sys.path.insert(0, str(ROOT))
        import build_site_data as B  # noqa: PLC0415
        return B.owner_name, B.SHORT, B.TEAM_NAME_FALLBACK
    except Exception as e:  # noqa: BLE001
        print(f"WARN build_site_data import failed ({e}); using roster fallback names", file=sys.stderr)
        return None, {}, {}


def nfl_team_states(season: str, week: int, season_type: str) -> dict[str, str] | None:
    """NFL team abbrev (Sleeper style) -> 'pre' | 'in' | 'post'. None if unavailable."""
    stype = {"pre": 1, "regular": 2, "post": 3}.get(season_type, 2)
    try:
        d = get_json(ESPN_SCOREBOARD.format(season=season, stype=stype, week=week), retries=2, timeout=15)
    except Exception as e:  # noqa: BLE001
        print(f"WARN NFL schedule unavailable ({e}); omitting to-play counts", file=sys.stderr)
        return None
    out = {}
    for ev in d.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        state = ((comp.get("status") or {}).get("type") or {}).get("state", "pre")
        for c in comp.get("competitors", []):
            ab = (c.get("team") or {}).get("abbreviation", "")
            out[ESPN_TO_SLEEPER.get(ab, ab)] = state
    return out


def clean_text(s: str) -> str:
    """Drop emoji / symbols the fonts can't draw; collapse whitespace."""
    s = "".join(ch for ch in (s or "") if unicodedata.category(ch) not in ("So", "Cs", "Co", "Cn")
                and ord(ch) not in (0xFE0F, 0x200D))
    return " ".join(s.split())


def collect(week: int | None, refresh_players: bool, use_schedule: bool) -> dict:
    state = get_json(f"{BASE}/state/nfl")
    if week is None:
        week = int(state.get("display_week") or state.get("week") or 1)
    league = get_json(f"{BASE}/league/{LEAGUE_ID}")
    users = get_json(f"{BASE}/league/{LEAGUE_ID}/users") or []
    rosters = get_json(f"{BASE}/league/{LEAGUE_ID}/rosters") or []
    matchups = get_json(f"{BASE}/league/{LEAGUE_ID}/matchups/{week}") or []
    players = load_players(refresh_players)
    season = str(league.get("season") or state.get("season"))
    games = nfl_team_states(season, week, state.get("season_type", "regular")) if use_schedule else None

    owner_name, short, tn_fallback = name_maps()
    uid = {u["user_id"]: u for u in users}
    teams = {}
    for r in rosters:
        rid = r["roster_id"]
        u = uid.get(r.get("owner_id") or "") or {}
        if owner_name:
            full = owner_name(u, r.get("owner_id"))
            mgr = short.get(full, full.split()[0] if full else ROSTER_FALLBACK.get(rid, ""))
        else:
            full, mgr = ROSTER_FALLBACK.get(rid, f"Team {rid}"), ROSTER_FALLBACK.get(rid, f"Team {rid}")
        tn = clean_text((u.get("metadata") or {}).get("team_name") or "")
        tn = TEAM_SHORT_OVERRIDES.get(rid) or tn or tn_fallback.get(full) or f"Team {mgr}"
        teams[rid] = {"roster_id": rid, "manager": mgr, "team": tn}

    rows = []
    for m in matchups:
        rid = m["roster_id"]
        pts = m.get("custom_points")
        if pts is None:
            pts = m.get("points") or 0.0
        pp = m.get("players_points") or {}
        starters = [s for s in (m.get("starters") or []) if s and s != "0"]
        to_play = live = done = 0
        perf = []
        for s in starters:
            p = players.get(s) or {}
            nfl = p.get("team")
            if games is not None and nfl:
                st = games.get(nfl)
                to_play += st == "pre"
                live += st == "in"
                done += st == "post"
            perf.append({
                "pid": s,
                "name": clean_text(p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or s),
                "pos": p.get("position") or "",
                "nfl": nfl or "FA",
                "points": float(pp.get(s) or 0.0),
                "manager": teams.get(rid, {}).get("manager", ""),
            })
        rows.append({**teams.get(rid, {"roster_id": rid, "manager": ROSTER_FALLBACK.get(rid, ""), "team": f"Team {rid}"}),
                     "matchup_id": m.get("matchup_id"), "points": round(float(pts), 2),
                     "to_play": to_play if games is not None else None,
                     "live": live if games is not None else None,
                     "started": (live + done) if games is not None else None, "perf": perf})

    by_mid: dict = {}
    for r in rows:
        if r["matchup_id"] is not None:
            by_mid.setdefault(r["matchup_id"], []).append(r)
    pairs = [sorted(v, key=lambda r: r["roster_id"]) for k, v in sorted(by_mid.items()) if len(v) == 2]

    scores = [r["points"] for r in rows]
    med = round(statistics.median(scores), 2) if scores else 0.0
    above = sum(1 for s in scores if s > med + 1e-9)
    starters_all = [p for r in rows for p in r["perf"]]
    top = sorted([p for p in starters_all if p["points"] > 0], key=lambda p: (-p["points"], p["name"]))
    n_games = len({k for k in (games or {})}) // 2 if games else 0
    n_final = sum(1 for v in (games or {}).values() if v == "post") // 2 if games else 0
    return {"week": week, "season": season, "pairs": pairs, "median": med, "above": above,
            "n_teams": len(scores), "top": top, "games_total": n_games, "games_final": n_final,
            "has_schedule": games is not None, "league_name": league.get("name")}


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

_font_paths: dict[str, Path] = {}
_font_cache: dict = {}


def font_path(key: str) -> Path:
    if key in _font_paths:
        return _font_paths[key]
    fname, gpath = FONT_FILES[key]
    for d in [ROOT / "assets" / "fonts", FONT_CACHE] + SYSTEM_FONT_DIRS:
        if d.exists():
            hit = next(iter(d.rglob(fname)), None)
            if hit:
                _font_paths[key] = hit
                return hit
    FONT_CACHE.mkdir(parents=True, exist_ok=True)
    dest = FONT_CACHE / fname
    url = f"https://github.com/google/fonts/raw/main/{gpath}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        dest.write_bytes(resp.read())
    _font_paths[key] = dest
    return dest


def F(key: str, size: int, weight: int | None = None) -> ImageFont.FreeTypeFont:
    size = max(1, int(round(size)))
    ck = (key, size, weight)
    if ck not in _font_cache:
        f = ImageFont.truetype(str(font_path(key)), size)
        if key == "sans":
            try:
                f.set_variation_by_axes([weight or 500])
            except Exception:  # noqa: BLE001
                pass
        _font_cache[ck] = f
    return _font_cache[ck]


def tw(draw, text, font, tracking=0.0) -> float:
    if not text:
        return 0.0
    return draw.textlength(text, font=font) + tracking * (len(text) - 1)


def draw_tracked(draw, xy, text, font, fill, tracking=0.0, anchor="ls"):
    """Letter-spaced text; anchor 'ls' (left baseline) or 'rs' (right baseline)."""
    x, y = xy
    if anchor == "rs":
        x -= tw(draw, text, font, tracking)
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill, anchor="ls")
        x += draw.textlength(ch, font=font) + tracking


def fit(draw, text, key, max_size, min_size, max_w, weight=None, tracking=0.0):
    """Largest font size in [min,max] that fits max_w; else min size + ellipsis truncation."""
    size = max_size
    while size >= min_size:
        f = F(key, size, weight)
        if tw(draw, text, f, tracking) <= max_w:
            return f, text
        size -= 1
    f = F(key, min_size, weight)
    t = text
    while t and tw(draw, t + "…", f, tracking) > max_w:
        t = t[:-1].rstrip()
    return f, (t + "…") if t else "…"


def cap_h(font) -> float:
    b = font.getbbox("H0", anchor="ls")
    return -b[1]


def paste_logo(img, x, y, size):
    p = ROOT / "assets" / "logo.png"
    if not p.exists():
        return
    logo = Image.open(p).convert("RGBA")
    logo.thumbnail((size, size), Image.LANCZOS)
    img.alpha_composite(logo, (int(x), int(y)))


def fmt(p: float) -> str:
    return f"{p:.2f}"


# ---------------------------------------------------------------------------
# Card pieces
# ---------------------------------------------------------------------------

def draw_matchup(draw, x, y, w, h, pair, final: bool, s: float, median: float | None = None):
    """One matchup box: two team rows, leader highlighted (red score + red bar)."""
    draw.rounded_rectangle([x, y, x + w, y + h], radius=int(10 * s), fill=SURFACE)
    a, b = pair
    lead = None
    if a["points"] != b["points"]:
        lead = a if a["points"] > b["points"] else b
    tied = a["points"] == b["points"] and (a["points"] > 0 or final)
    rh = h / 2
    pad = int(22 * s)
    for i, t in enumerate(pair):
        ry = y + i * rh
        is_lead = t is lead
        if is_lead:
            # red accent pill on the leader's row, inset so it stays inside the rounded card
            inset = rh * 0.16
            draw.rounded_rectangle([x, ry + inset, x + int(7 * s), ry + rh - inset],
                                   radius=int(3 * s), fill=ACCENT)
        score_txt = fmt(t["points"])
        sf = F("cond-xbold", 64 * s)
        score_w = tw(draw, score_txt, sf)
        badge_w = 0
        badge = None
        if final:
            badge = "W" if is_lead else ("T" if tied else "L")
            badge_w = int(34 * s) + int(10 * s)
        right = x + w - pad
        name_max = w - 2 * pad - score_w - badge_w - int(18 * s)
        # team name
        nf, ntxt = fit(draw, t["team"].upper(), "cond-xbold", int(44 * s), int(26 * s), name_max)
        name_base = ry + rh * 0.52
        draw.text((x + pad, name_base), ntxt, font=nf, fill=FG, anchor="ls")
        # manager + to-play line
        sub = t["manager"]
        # sub-line variants, longest first; use the first that fits at a steady size
        variants = [[]]
        if not final and t.get("to_play") is not None:
            tp, lv = t["to_play"], t.get("live") or 0
            if tp == 0 and lv == 0:
                variants = [["all played"], ["done"], []]
            else:
                v_long = ([f"{lv} live"] if lv else []) + ([f"{tp} to play"] if tp else [])
                v_short = ([f"{lv} live"] if lv else []) + ([f"{tp} left"] if tp else [])
                variants = [v_long, v_short, [f"{tp + lv} left"], []]
        if final and median is not None:
            if t["points"] > median:
                variants = [["beat median"], ["beat med"], []]
            elif t["points"] == median:
                variants = [["at median"], []]
            else:
                variants = [["missed median"], ["missed med"], []]
        mgr_f = F("sans", 26 * s, 600)
        sub_base = ry + rh * 0.84
        draw.text((x + pad, sub_base), sub, font=mgr_f, fill=FG, anchor="ls")
        ex = x + pad + tw(draw, sub, mgr_f)
        ef = F("sans", (22 if final else 26) * s, 400)
        for v in variants:
            ex_txt = ("  ·  " + "  ·  ".join(v)) if v else ""
            if tw(draw, ex_txt, ef) <= name_max - (ex - x - pad):
                if ex_txt:
                    draw.text((ex, sub_base), ex_txt, font=ef, fill=MUTED, anchor="ls")
                break
        # score, vertically centred on the row
        sb = ry + rh / 2 + cap_h(sf) / 2
        color = ACCENT if is_lead else FG
        draw.text((right, sb), score_txt, font=sf, fill=color, anchor="rs")
        if badge:
            bs = int(34 * s)
            bx1 = right - score_w - int(10 * s)
            bx0 = bx1 - bs
            by0 = ry + rh / 2 - bs / 2
            fillc = ACCENT if badge == "W" else (LINE if badge == "L" else FG)
            inkc = ACCENT_INK if badge in ("W", "T") else MUTED
            draw.rounded_rectangle([bx0, by0, bx1, by0 + bs], radius=int(6 * s), fill=fillc)
            bf = F("cond-xbold", 26 * s)
            draw.text(((bx0 + bx1) / 2, by0 + bs / 2 + cap_h(bf) / 2), badge, font=bf, fill=inkc, anchor="ms")
    # divider between rows
    draw.line([x + pad, y + rh, x + w - pad, y + rh], fill=LINE, width=max(1, int(2 * s)))
    if tied and not final:
        # small TIED chip on the divider, right-aligned between the two scores
        tf = F("cond-bold", 20 * s)
        txt = "TIED"
        cw_ = tw(draw, txt, tf, 2 * s) + 18 * s
        cx1 = x + w - pad
        draw.rounded_rectangle([cx1 - cw_, y + rh - 13 * s, cx1, y + rh + 13 * s], radius=int(6 * s), fill=FG)
        draw_tracked(draw, (cx1 - cw_ + 9 * s, y + rh + cap_h(tf) / 2), txt, tf, ACCENT_INK, 2 * s)


def title_text(week: int, label: str, final: bool) -> str:
    if final:
        return f"WEEK {week} FINAL"
    lab = (label or "").strip().upper()
    return f"WEEK {week} · {lab} CHECK-IN" if lab else f"WEEK {week} · LIVE CHECK-IN"


def pending_text(omitted, with_names=True) -> str:
    n = len(omitted)
    head = f"{n} matchup{'s' if n != 1 else ''} yet to kick off"
    if not with_names:
        return head
    return head + ":  " + ",  ".join(f"{a['manager']} vs {b['manager']}" for a, b in omitted)


def status_variants(d: dict, final: bool, omitted=(), show_median=True) -> list[str]:
    """Header sub-line candidates, most informative first."""
    games = (f"{d['games_final']} of {d['games_total']} NFL games final"
             if d.get("has_schedule") and d.get("games_total") else "")
    join = lambda *xs: "  ·  ".join(x for x in xs if x)  # noqa: E731
    if omitted:
        return [join(games, pending_text(omitted)), pending_text(omitted),
                join(games, pending_text(omitted, False)), pending_text(omitted, False)]
    if not show_median:
        return [games]
    return [join(games, "H2H + weekly median" if not final else "H2H + weekly median both count"), games]


def updated_text() -> str:
    now = datetime.now(TZ)
    return now.strftime("Updated %a %b %-d, %-I:%M %p MST")


def draw_median_block(draw, x_right, y_top, d, s, align="right", final=False):
    """WEEKLY MEDIAN label / value / 'N of 12 above'. Returns block bottom y."""
    lf = F("cond-bold", 24 * s)
    vf = F("cond-xbold", 64 * s)
    cf = F("sans", 22 * s, 500)
    above_txt = f"{d['above']} of {d['n_teams']} teams above" if not final else \
        f"{d['above']} of {d['n_teams']} teams beat it"
    y1 = y_top + cap_h(lf)
    y2 = y1 + 12 * s + cap_h(vf)
    y3 = y2 + 14 * s + cap_h(cf)
    if align == "right":
        draw_tracked(draw, (x_right, y1), "WEEKLY MEDIAN", lf, ACCENT, 2.5 * s, anchor="rs")
        draw.text((x_right, y2), fmt(d["median"]), font=vf, fill=FG, anchor="rs")
        draw.text((x_right, y3), above_txt, font=cf, fill=MUTED, anchor="rs")
    else:
        draw_tracked(draw, (x_right, y1), "WEEKLY MEDIAN", lf, ACCENT, 2.5 * s)
        draw.text((x_right, y2), fmt(d["median"]), font=vf, fill=FG, anchor="ls")
        draw.text((x_right, y3), above_txt, font=cf, fill=MUTED, anchor="ls")
    return y3


def draw_section_label(draw, x, y, text, s, width=None):
    lf = F("cond-bold", 26 * s)
    draw_tracked(draw, (x, y), text, lf, ACCENT, 3 * s)
    if width:
        lx = x + tw(draw, text, lf, 3 * s) + 16 * s
        cy = y - cap_h(lf) / 2
        draw.line([lx, cy, x + width, cy], fill=LINE, width=max(1, int(2 * s)))


def draw_perf_cell(draw, x, y, w, rank, p, s):
    """Column-style performer: points, name, POS · NFL · manager."""
    pf = F("cond-xbold", 56 * s)
    rf = F("cond-bold", 26 * s)
    base = y + cap_h(pf)
    draw.text((x, base), fmt(p["points"]), font=pf, fill=ACCENT, anchor="ls")
    nf, ntxt = fit(draw, p["name"].upper(), "cond-xbold", int(36 * s), int(24 * s), w)
    nb = base + 14 * s + cap_h(nf)
    draw.text((x, nb), ntxt, font=nf, fill=FG, anchor="ls")
    meta = f"{p['pos']}  ·  {p['nfl']}  ·  {p['manager']}"
    mf, mtxt = fit(draw, meta, "sans", int(25 * s), int(18 * s), w, 500)
    draw.text((x, nb + 12 * s + cap_h(mf)), mtxt, font=mf, fill=MUTED, anchor="ls")
    rtxt = f"#{rank}"
    draw.text((x + w, base), rtxt, font=rf, fill="#b5b5b5", anchor="rs")


def draw_perf_row(draw, x, y, w, h, rank, p, s):
    """Row-style performer (portrait): #  NAME  POS · NFL · mgr  ....  points."""
    draw.rounded_rectangle([x, y, x + w, y + h], radius=int(8 * s), fill=SURFACE)
    pad = 20 * s
    rf = F("cond-xbold", 30 * s)
    pf = F("cond-xbold", 44 * s)
    mid = y + h / 2
    draw.text((x + pad, mid + cap_h(rf) / 2), str(rank), font=rf, fill="#b5b5b5", anchor="ls")
    pts = fmt(p["points"])
    draw.text((x + w - pad, mid + cap_h(pf) / 2), pts, font=pf, fill=ACCENT, anchor="rs")
    nx = x + pad + 36 * s
    avail = w - (nx - x) - pad - tw(draw, pts, pf) - 20 * s
    nf, ntxt = fit(draw, p["name"].upper(), "cond-xbold", int(32 * s), int(22 * s), avail)
    draw.text((nx, mid - 2 * s), ntxt, font=nf, fill=FG, anchor="ls")
    meta = f"{p['pos']}  ·  {p['nfl']}  ·  {p['manager']}"
    mf, mtxt = fit(draw, meta, "sans", int(21 * s), int(16 * s), avail, 500)
    draw.text((nx, mid + 6 * s + cap_h(mf)), mtxt, font=mf, fill=MUTED, anchor="ls")


def draw_footer(draw, W, H, M, s):
    fy = H - 34 * s
    draw.line([M, fy - 36 * s, W - M, fy - 36 * s], fill=LINE, width=max(1, int(2 * s)))
    ff = F("cond-bold", 28 * s)
    tag = "#FlexAppealFFL"
    draw.text((M, fy), tag, font=ff, fill=ACCENT, anchor="ls")
    draw.text((M + tw(draw, tag, ff), fy), "  ·  flex-appeal-ffl.grok.me", font=ff, fill=FG, anchor="ls")
    uf = F("sans", 20 * s, 400)
    draw.text((W - M, fy), f"Sleeper live data  ·  {updated_text()}", font=uf, fill=MUTED, anchor="rs")


def draw_header(img, draw, W, M, d, label, final, s, logo=True, median_right=True, omitted=(),
                show_median=True):
    draw.rectangle([0, 0, W, int(10 * s)], fill=ACCENT)
    logo_sz = int(150 * s)
    right_edge = W - M
    if logo:
        paste_logo(img, W - M - logo_sz + int(10 * s), int(20 * s), logo_sz)
        right_edge = W - M - logo_sz - int(16 * s)
    ef = F("cond-bold", 30 * s)
    ey = 30 * s + cap_h(ef)
    draw_tracked(draw, (M, ey), "FLEX APPEAL FFL", ef, ACCENT, 4 * s)
    title = title_text(d["week"], label, final)
    title_max = (right_edge - M) - (330 * s if median_right else 0)
    tf, ttxt = fit(draw, title, "cond-xbold", int(88 * s), int(52 * s), title_max)
    ty = ey + 14 * s + cap_h(tf)
    draw.text((M, ty), ttxt, font=tf, fill=FG, anchor="ls")
    sf = F("sans", 24 * s, 500)
    sy = ty + 18 * s + cap_h(sf)
    for st in status_variants(d, final, omitted, show_median):
        if tw(draw, st, sf) <= title_max:
            break
    draw.text((M, sy), st, font=sf, fill=MUTED, anchor="ls")
    if median_right:
        draw_median_block(draw, right_edge, 30 * s, d, s, final=final)
    return sy


# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------

def pair_started(pair, has_schedule: bool) -> bool:
    """True once at least one starter on either team has played (game started or final).
    Uses the ESPN game states; falls back to 'either team has nonzero points'."""
    if has_schedule and all(t.get("started") is not None for t in pair):
        return any(t["started"] > 0 for t in pair)
    return any((t.get("points") or 0) > 0 for t in pair)


def select_pairs(d: dict, final: bool):
    """(pairs to show, pairs omitted). Final cards always show all six; live cards hide
    matchups where nobody has played yet (if nobody has played anywhere, show all)."""
    pairs = d["pairs"]
    if final:
        return pairs, []
    shown = [p for p in pairs if pair_started(p, d.get("has_schedule", False))]
    if not shown:
        return pairs, []
    return shown, [p for p in pairs if p not in shown]


# rows per layout: number of boxes in each row (rows with fewer boxes are centred)
GRID_WIDE = {1: [1], 2: [2], 3: [3], 4: [2, 2], 5: [3, 2], 6: [3, 3]}
GRID_SQUARE = {1: [1], 2: [1, 1], 3: [2, 1], 4: [2, 2], 5: [2, 2, 1], 6: [2, 2, 2]}
BOX_BASE_W, BOX_BASE_H = 450, 190     # box size at scale 1.0
BOX_SMAX, BOX_HSTRETCH, BOX_WSTRETCH = 1.5, 1.45, 1.6


def draw_grid(draw, x0, y0, w, h, pairs, spec, final, median, gap):
    """Lay out matchup boxes in the area: boxes scale with the space they get,
    rows are centred horizontally and the whole block is centred vertically.
    Returns the block's bottom y."""
    if not pairs:
        return y0
    rows = spec[len(pairs)]
    ncol, nrow = max(rows), len(rows)
    cell_w = (w - (ncol - 1) * gap) / ncol
    cell_h = (h - (nrow - 1) * gap) / nrow
    sc = min(cell_w / BOX_BASE_W, cell_h / BOX_BASE_H, BOX_SMAX)
    # boxes fill their column; only a very wide lone column (1 matchup, landscape) is capped
    bw = cell_w if cell_w <= 1000 else min(cell_w, BOX_BASE_W * sc * BOX_WSTRETCH)
    bh = min(cell_h, BOX_BASE_H * sc * BOX_HSTRETCH)
    block_h = nrow * bh + (nrow - 1) * gap
    by = y0 + (h - block_h) / 2
    i = 0
    for r, n in enumerate(rows):
        row_w = n * bw + (n - 1) * gap
        bx = x0 + (w - row_w) / 2
        for c in range(n):
            draw_matchup(draw, bx + c * (bw + gap), by + r * (bh + gap), bw, bh, pairs[i], final, sc, median)
            i += 1
    return by + block_h


def draw_pending_note(draw, cx, y, max_w, omitted, s):
    """Muted centred line naming the matchups that haven't kicked off."""
    if not omitted:
        return
    n = len(omitted)
    head = f"{n} matchup{'s' if n != 1 else ''} yet to kick off"
    names = "  ·  ".join(f"{a['manager']} vs {b['manager']}" for a, b in omitted)
    f = F("sans", 24 * s, 500)
    for txt in (f"{head}:  {names}", head):
        if tw(draw, txt, f) <= max_w:
            break
    draw.text((cx, y), txt, font=f, fill=MUTED, anchor="ms")


NOTE_H = 44


def render_wide(d, label, final, show_median=True) -> Image.Image:
    W, H, s, M = 1600, 900, 1.0, 56
    img = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(img)
    shown, omitted = select_pairs(d, final)
    hb = draw_header(img, draw, W, M, d, label, final, s, median_right=show_median, omitted=omitted,
                     show_median=show_median)

    footer_line = H - 70
    py = footer_line - 178          # TOP PERFORMERS label baseline
    gy = hb + 30
    draw_grid(draw, M, gy, W - 2 * M, (py - 46) - gy, shown, GRID_WIDE, final,
              d["median"] if show_median else None, 22)
    draw_section_label(draw, M, py, "TOP PERFORMERS" if not final else "TOP PERFORMERS OF THE WEEK", s, W - 2 * M)
    top = d["top"][:5]
    if top:
        n = 5
        pgap = 36
        pw = (W - 2 * M - (n - 1) * pgap) / n
        for i, p in enumerate(top):
            draw_perf_cell(draw, M + i * (pw + pgap), py + 26, pw, i + 1, p, s)
    else:
        draw.text((M, py + 60), "No starter has scored yet. Check back after kickoff.",
                  font=F("sans", 26, 500), fill=MUTED, anchor="ls")
    draw_footer(draw, W, H, M, s)
    return img.convert("RGB")


def render_square(d, label, final, show_median=True) -> Image.Image:
    W, H, s, M = 1080, 1350, 1.0, 48
    img = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(img)
    s_head = 0.9
    hb = draw_header(img, draw, W, M, d, label, final, s_head, logo=True, median_right=False,
                     show_median=show_median)
    # median strip (hidden on TNF / --no-median; the grid gets that space instead)
    my, mh = hb + 26, 92
    if show_median:
        draw.rounded_rectangle([M, my, W - M, my + mh], radius=10, fill=FG)
        lf = F("cond-bold", 24)
        vf = F("cond-xbold", 56)
        cf = F("sans", 24, 500)
        mid = my + mh / 2
        draw_tracked(draw, (M + 26, mid + cap_h(lf) / 2), "WEEKLY MEDIAN", lf, "#ff5a5f", 2.5)
        vx = M + 26 + tw(draw, "WEEKLY MEDIAN", lf, 2.5) + 22
        draw.text((vx, mid + cap_h(vf) / 2), fmt(d["median"]), font=vf, fill="#ffffff", anchor="ls")
        above_txt = f"{d['above']} of {d['n_teams']} teams {'beat it' if final else 'above'}"
        draw.text((W - M - 26, mid + cap_h(cf) / 2), above_txt, font=cf, fill="#d6d6d6", anchor="rs")

    grid_h = 3 * 168 + 2 * 18          # fixed grid area; boxes re-flow inside it
    gy = my + mh + 24
    if not show_median:
        grid_h += gy - (hb + 30)
        gy = hb + 30
    shown, omitted = select_pairs(d, final)
    area_h = grid_h - (NOTE_H if omitted else 0)
    bottom = draw_grid(draw, M, gy, W - 2 * M, area_h, shown, GRID_SQUARE, final,
                       d["median"] if show_median else None, 18)
    draw_pending_note(draw, W / 2, bottom + NOTE_H - 10, W - 2 * M, omitted, 1.0)
    py = gy + grid_h + 52
    draw_section_label(draw, M, py, "TOP PERFORMERS", 1.0, W - 2 * M)
    footer_line = H - 0.9 * 70
    top = d["top"][:5]
    rgap = 10
    avail = footer_line - 26 - (py + 22)
    rh = min(80, (avail - 4 * rgap) / 5)
    if top:
        for i, p in enumerate(top):
            draw_perf_row(draw, M, py + 22 + i * (rh + rgap), W - 2 * M, rh, i + 1, p, 1.0)
    else:
        draw.text((M, py + 60), "No starter has scored yet. Check back after kickoff.",
                  font=F("sans", 26, 500), fill=MUTED, anchor="ls")
    draw_footer(draw, W, H, M, 0.9)
    return img.convert("RGB")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--week", type=int, default=None, help="NFL week (default: Sleeper state/nfl display_week)")
    ap.add_argument("--label", default="", help="'TNF' | 'Sunday early' | 'SNF' | 'MNF final' ... (title text)")
    ap.add_argument("--final", action="store_true", help="mark W/L (implied when --label contains 'final')")
    mg = ap.add_mutually_exclusive_group()
    mg.add_argument("--median", dest="median", action="store_true", default=None,
                    help="force the Weekly Median block on")
    mg.add_argument("--no-median", dest="median", action="store_false",
                    help="hide the Weekly Median block (default for TNF / Thursday labels)")
    ap.add_argument("--format", choices=["wide", "square"], default="wide",
                    help="wide = 1600x900 (default), square = 1080x1350 portrait")
    ap.add_argument("--out", default=None, help="output PNG path (default /workspace/score-cards/week<N>-<label>.png)")
    ap.add_argument("--refresh-players", action="store_true", help="force re-download of Sleeper players")
    ap.add_argument("--no-schedule", action="store_true", help="skip the ESPN NFL game-state lookup")
    ap.add_argument("--dump-json", default=None, help="also write the card data as JSON here")
    ap.add_argument("--data-json", default=None, help="render from a saved --dump-json file instead of the API")
    a = ap.parse_args(argv)

    final = a.final or "final" in a.label.lower()
    if a.data_json:
        d = json.loads(Path(a.data_json).read_text())
    else:
        d = collect(a.week, a.refresh_players, not a.no_schedule)
    if len(d["pairs"]) != 6:
        print(f"WARN expected 6 matchups, got {len(d['pairs'])}", file=sys.stderr)
    lab = a.label.lower()
    show_median = a.median if a.median is not None else not ("tnf" in lab or "thursday" in lab)
    render = render_square if a.format == "square" else render_wide
    img = render(d, a.label, final, show_median)
    out = Path(a.out) if a.out else Path("/workspace/score-cards") / (
        f"week{d['week']}-{(a.label or 'live').lower().replace(' ', '-')}{'-sq' if a.format == 'square' else ''}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, optimize=True)
    if a.dump_json:
        Path(a.dump_json).write_text(json.dumps(d, indent=2, default=str))
    print(f"wrote {out}  ({img.width}x{img.height})  week {d['week']}  median {fmt(d['median'])}"
          f"{'' if show_median else ' (hidden)'}")
    shown, _ = select_pairs(d, final)
    for a_, b_ in d["pairs"]:
        tag = "" if [a_, b_] in shown else "   (not shown: yet to kick off)"
        print(f"  {a_['manager']:>7} {fmt(a_['points']):>7}  -  {fmt(b_['points']):<7} {b_['manager']}{tag}")


if __name__ == "__main__":
    main()

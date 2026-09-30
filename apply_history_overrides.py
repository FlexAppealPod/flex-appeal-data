#!/usr/bin/env python3
"""Merge manual championship rulings into data/history.json.

build_site_data.py only knows Sleeper seasons (2023+), so run this right after it:

    python3 build_site_data.py && python3 apply_history_overrides.py

Reads data/sources/champions_manual.json. Keeps the existing champions schema
(season / champion / runner_up) and adds co_champions, note and platform.
Idempotent. Stdlib only.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "data" / "history.json"
MANUAL = ROOT / "data" / "sources" / "champions_manual.json"


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
    HISTORY.write_text(json.dumps(hist, indent=2, ensure_ascii=False) + "\n")
    print("history.json champions:", [(c["season"], c["champion"]) for c in hist["champions"]])


if __name__ == "__main__":
    main()

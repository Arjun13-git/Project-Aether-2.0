"""Licence report for the installed Python and web dependency trees (ADR-020).

Usage:  uv run --project apps/api python scripts/licence_report.py [--fail-on-denied]

Reads licence metadata from installed distributions (PyPI ``License-Expression`` / ``License`` /
classifiers) and from apps/web/node_modules package.json files. It flags denied licences (AGPL,
SSPL, non-commercial, share-alike) and unknown ones. This is a tripwire, not legal advice: every
row still needs a register entry (System-Design/14-licensing/LICENSE-REGISTER.md).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from importlib import metadata
from pathlib import Path

DENY = re.compile(r"AGPL|SSPL|BUSL|Commons Clause|NC\b|NonCommercial|-SA\b|ShareAlike|CC-BY-SA|CC-BY-NC", re.I)
ROOT = Path(__file__).resolve().parents[1]


def python_rows() -> list[tuple[str, str, str]]:
    rows = []
    for dist in metadata.distributions():
        md = dist.metadata
        name = md["Name"]
        if name == "aether":
            continue
        lic = md.get("License-Expression") or ""
        if not lic:
            classifiers = [c.split("::")[-1].strip() for c in md.get_all("Classifier") or [] if c.startswith("License ::")]
            lic = (
                "; ".join(classifiers) or (md.get("License") or "").splitlines()[0][:60]
                if (classifiers or md.get("License"))
                else ""
            )
        rows.append((name, dist.version, lic or "UNKNOWN"))
    return sorted(rows, key=lambda r: r[0].lower())


def node_rows() -> list[tuple[str, str, str]]:
    rows = []
    pnpm = ROOT / "apps/web/node_modules/.pnpm"
    if not pnpm.exists():
        return rows
    seen = set()
    for pj in pnpm.glob("*/node_modules/**/package.json"):
        if "/node_modules/" in str(pj.relative_to(pnpm)).split("/node_modules/", 1)[1]:
            continue
        try:
            d = json.loads(pj.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if "name" not in d or "version" not in d or (d["name"], d["version"]) in seen:
            continue
        seen.add((d["name"], d["version"]))
        lic = d.get("license")
        if isinstance(lic, dict):
            lic = lic.get("type")
        rows.append((d["name"], d["version"], str(lic or "UNKNOWN")))
    return sorted(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fail-on-denied", action="store_true")
    args = ap.parse_args()
    denied = []
    for title, rows in (("python", python_rows()), ("web (node_modules)", node_rows())):
        unknown = [r for r in rows if r[2] == "UNKNOWN"]
        bad = [r for r in rows if DENY.search(r[2])]
        denied += bad
        print(f"## {title}: {len(rows)} packages, {len(unknown)} unknown, {len(bad)} denied")
        for r in bad + unknown:
            print(f"  {'DENIED ' if r in bad else 'UNKNOWN'} {r[0]}=={r[1]}  [{r[2]}]")
        counts: dict[str, int] = {}
        for r in rows:
            counts[r[2]] = counts.get(r[2], 0) + 1
        print("  licences:", ", ".join(f"{k} ×{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    if args.fail_on_denied and denied:
        sys.exit(1)


if __name__ == "__main__":
    main()

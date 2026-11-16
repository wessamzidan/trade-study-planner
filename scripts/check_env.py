#!/usr/bin/env python3
"""Stage 0 of the Trade Study Planner: probe the environment and record which data routes are usable.

Writes <study-dir>/env.json (schema 2.0) and prints a short summary.
Standard library only.

Usage:
    python3 check_env.py --study-dir DIR [--timeout 8]

Exit codes: 0 when every REQUIRED package is installed, 2 otherwise (env.json is written either way).
"""
from __future__ import annotations

import argparse
import http.client
import importlib.metadata as md
import importlib.util
import os
import shutil
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

# (label, distribution name, import name, required)
PACKAGES = [
    ("pandas", "pandas", "pandas", True),
    ("openpyxl", "openpyxl", "openpyxl", True),
    ("docx", "python-docx", "docx", True),
    ("searoute", "searoute", "searoute", False),
    ("matplotlib", "matplotlib", "matplotlib", False),
]

# Only presence is recorded. Values are never read into the output or printed.
KEY_NAMES = ("COMTRADE_SUBSCRIPTION_KEY", "FREIGHTOS_API_KEY")

ENDPOINTS = {
    "comtrade_api": "https://comtradeapi.un.org/public/v1/preview/C/A/HS?reporterCode=818&period=2023&partnerCode=0&cmdCode=TOTAL&flowCode=X",
    "wits": "https://wits.worldbank.org/API/V1/wits/datasource/trn/country/ALL",
    "freightos": "https://ship.freightos.com/api/shippingCalculator",
    "github_raw": "https://raw.githubusercontent.com/datasets/un-locode/main/README.md",
}

PIP_COMMAND = "pip install pandas openpyxl python-docx"
FREIGHTOS_KEY_ADVICE = (
    "A free Freightos Rate Estimate key covers 10 requests a day — enough for one study of up to 10 lanes. "
    "Ask the user whether they want to request one."
)
COMTRADE_KEYLESS_NOTE = "keyless preview: 500 rows per call (COMTRADE_SUBSCRIPTION_KEY not set)."
SEAROUTE_ADVICE = "Modelled sea-route distances need searoute: pip install searoute"


def check_package(dist: str, module: str) -> dict:
    """Installed state and version. A module importable without metadata counts as installed, version None."""
    try:
        version = md.version(dist)
    except md.PackageNotFoundError:
        version = None
    installed = version is not None or importlib.util.find_spec(module) is not None
    return {"installed": installed, "version": version}


def probe(url: str, timeout: float) -> tuple[bool, str]:
    """Reachable when the host returns any HTTP response, even an error status.

    A proxy refusing CONNECT surfaces as URLError (an OSError), which counts as unreachable.
    The default SSL context is used and certificate verification is never disabled.
    """
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:
            return True, f"HTTP {resp.status} from host"
    except urllib.error.HTTPError as exc:  # the host answered: reachable
        exc.close()
        return True, f"HTTP {exc.code} from host"
    except (OSError, ValueError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"unreachable: {type(reason).__name__}: {reason}"


def build_routes(net: dict, keys: dict, pkgs: dict) -> tuple[dict, list]:
    advice: list[str] = []

    trade = []
    if net["comtrade_api"]["reachable"]:
        trade.append("api")
        if not keys["COMTRADE_SUBSCRIPTION_KEY"]:
            advice.append(COMTRADE_KEYLESS_NOTE)
    trade += ["connector", "csv_upload"]

    tariff = []
    if net["wits"]["reachable"]:
        tariff.append("wits_api")
    tariff += ["web_lookup", "ask_user"]

    freight = ["client_quote"]
    if net["freightos"]["reachable"]:
        if keys["FREIGHTOS_API_KEY"]:
            freight.append("freightos_api")
        else:
            advice.append(FREIGHTOS_KEY_ADVICE)
    freight.append("published_rates")
    if pkgs["searoute"]["installed"]:
        freight.append("modelled")
    else:
        advice.append(SEAROUTE_ADVICE)

    return {"trade_data": trade, "tariff": tariff, "freight": freight}, advice


def fmt_pkg(label: str, info: dict, required: bool) -> str:
    if info["installed"]:
        return f"{label} {info['version'] or 'installed'}"
    return f"{label} MISSING ({'required' if required else 'optional'})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Probe the environment for the Trade Study Planner.")
    ap.add_argument("--study-dir", required=True, help="study folder; created if missing")
    ap.add_argument("--timeout", type=float, default=8.0, help="seconds per network probe (default 8)")
    args = ap.parse_args(argv)

    study_dir = Path(args.study_dir).expanduser()
    study_dir.mkdir(parents=True, exist_ok=True)

    # 1. Python packages
    pkgs = {}
    for label, dist, module, required in PACKAGES:
        info = check_package(dist, module)
        pkgs[label] = {**info, "required": required}

    # 2. Tools
    tools = {
        "libreoffice": bool(shutil.which("soffice") or shutil.which("libreoffice")),
        "node": bool(shutil.which("node")),
    }

    # 3. Keys (presence only)
    keys = {name: bool(os.environ.get(name)) for name in KEY_NAMES}

    # 4. Network
    net = {}
    for name, url in ENDPOINTS.items():
        ok, detail = probe(url, args.timeout)
        net[name] = {"reachable": ok, "detail": detail}

    # 5. Routes and advice
    routes, advice = build_routes(net, keys, pkgs)

    # 6. Write env.json
    missing_required = [label for label, _d, _m, req in PACKAGES if req and not pkgs[label]["installed"]]
    ready = not missing_required
    doc = {
        "schema_version": common.SCHEMA_VERSION,
        "checked_at": common.now_iso(),
        "python": sys.version.split()[0],
        "packages": pkgs,
        "tools": tools,
        "keys": keys,
        "network": net,
        "routes": routes,
        "advice": advice,
        "ready": ready,
    }
    out = common.write_json(common.study_file(study_dir, "env"), doc)

    # 7. Summary
    print(f"Python: {doc['python']}")
    print("Packages: " + ", ".join(
        fmt_pkg(label, pkgs[label], req) for label, _d, _m, req in PACKAGES))
    print("Tools: " + ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in tools.items()))
    print("Keys: " + ", ".join(f"{k} {'set' if v else 'not set'}" for k, v in keys.items()))
    print("Network: " + ", ".join(
        f"{k} {'reachable' if v['reachable'] else 'unreachable'}" for k, v in net.items()))
    print("Routes: " + "; ".join(f"{k}={','.join(v)}" for k, v in routes.items()))
    for line in advice:
        print(f"Advice: {line}")
    print(f"Wrote {out}")

    if not ready:
        print(f"BLOCKED: missing required packages: {', '.join(missing_required)}. Install with: {PIP_COMMAND}")
        return 2
    print("Ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

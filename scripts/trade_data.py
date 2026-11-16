#!/usr/bin/env python3
"""Stage 2 of the Trade Study Planner: plan, fetch and normalize UN Comtrade data.

Subcommands (all take --study-dir DIR):

  plan       Read inputs.json and write query_plan.json (what to download, in three renderings:
             API parameters, connector instruction, Comtrade web-UI steps).
  fetch      Execute the plan against the Comtrade API (keyed endpoint, or the keyless preview
             endpoint) and save raw responses under raw/.
  normalize  Read every file in raw/ (Comtrade UI CSV, Comtrade API JSON, generic rows) and
             write flows.csv + flows_meta.json (see references/contracts.md section 3).

Standard library only (pandas is not needed here). The API key is read from an environment
variable, sent only in a request header and never printed or stored.

Exit codes: 0 ok, 2 BLOCKED (a required input or route is missing), 1 error.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

COMTRADE_HOST = "https://comtradeapi.un.org"
KEYED_PATH = "/data/v1/get/C/A/HS"
PREVIEW_PATH = "/public/v1/preview/C/A/HS"
UI_URL = "https://comtradeplus.un.org/TradeFlow"
KEY_HEADER = "Ocp-Apim-Subscription-Key"
DEFAULT_KEY_ENV = "COMTRADE_SUBSCRIPTION_KEY"

MAX_PERIODS = 12          # periods per API call
MAX_ROWS_KEYED = 100_000  # rows per keyed call (free key)
MAX_ROWS_PREVIEW = 500    # rows per keyless preview call
CALLS_PER_DAY_FREE = 500
MIN_SLEEP_S = 1.1
MAX_RETRIES = 4
MATRIX_MIN_REPORTERS = 10  # a flow table with partner-level rows from this many reporters counts as a matrix

FLOW_WORD = {"X": "Export", "M": "Import"}
FLOW_ALIASES = {"x": "X", "export": "X", "exports": "X", "m": "M", "import": "M", "imports": "M",
                "rx": "RX", "re-export": "RX", "reexport": "RX", "re export": "RX",
                "rm": "RM", "re-import": "RM", "reimport": "RM", "re import": "RM"}


# =============================================================== inputs and plan

def load_inputs(study_dir) -> dict:
    """Read inputs.json and verify the fields stage 2 needs; BLOCKED when one is missing."""
    inp = common.read_json(common.study_file(study_dir, "inputs"))
    persp = inp.get("perspective")
    common.labels(persp)
    home = common.resolve_country(inp.get("home_country"))
    if not home or not common.is_rankable(home["iso3"]) or home.get("comtrade_code") is None:
        common.blocked(f"home_country {inp.get('home_country')!r} in inputs.json is not a known country")
    hs = str((inp.get("product") or {}).get("hs_code") or "").strip()
    if not hs:
        common.blocked("product.hs_code is missing in inputs.json")
    yrs = inp.get("years") or {}
    try:
        y0, y1 = int(yrs["from"]), int(yrs["to"])
    except (KeyError, TypeError, ValueError):
        common.blocked("years.from and years.to are required in inputs.json")
    if y0 > y1:
        common.blocked("years.from is after years.to in inputs.json")
    return {"inputs": inp, "perspective": persp, "home": home["iso3"], "hs": hs, "years": list(range(y0, y1 + 1))}


def id_stem(qid: str) -> str:
    """File-system safe form of a query id (partner_detail:SAU -> partner_detail_SAU)."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", qid)


def _area_code(iso3: str) -> int:
    rec = common.resolve_country(iso3)
    if not rec or rec.get("comtrade_code") is None:
        common.blocked(f"no Comtrade numeric code known for {iso3!r}")
    return int(rec["comtrade_code"])


def _qs(params: dict) -> str:
    return urllib.parse.urlencode({k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items()}, safe=",")


def _chunks(seq: list, n: int) -> list:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _ui_steps(q: dict, years: list, stem: str) -> list:
    p = q["params"]
    rep = "All" if p["reporter"] == "all" else common.country_name(p["reporter"])
    if p["partner"] == "all":
        par = "All (also tick World so the total row is included)"
    elif p["partner"] == "W00":
        par = "World"
    else:
        par = common.country_name(p["partner"])
    return [
        f"Open {UI_URL}",
        "Type of trade: Goods",
        "Frequency: Annual",
        "Classification: HS (latest revision offered)",
        f"Periods: {years[0]} to {years[-1]} (tick every year; one download if the site allows)",
        f"Reporters: {rep}",
        f"Partners: {par}",
        "2nd partner: World",
        f"Trade flows: {FLOW_WORD[p['flow']]}",
        f"Commodity code: {p['hs_code']}",
        "Modes of transport: TOTAL",
        "Customs code: TOTAL",
        f"Click Download, choose CSV, and save it as raw/{stem}.csv "
        "(keep the original columns; do not edit the file)",
    ]


def make_query(qid, purpose, feeds, reporter, partner, flow, hs, years, note="", per_year=False) -> dict:
    """Build one query object (API parameters, connector hint, UI steps, file name)."""
    stem = id_stem(qid)
    com = {}
    if reporter != "all":
        com["reporterCode"] = _area_code(reporter)
    if partner != "all":
        com["partnerCode"] = 0 if partner == "W00" else _area_code(partner)
    com.update({"flowCode": flow, "cmdCode": hs, "period": ",".join(str(y) for y in years),
                "partner2Code": 0, "customsCode": "C00", "motCode": 0, "includeDesc": True,
                "maxRecords": MAX_ROWS_KEYED})
    size = 1 if per_year else MAX_PERIODS
    chunks = [",".join(str(y) for y in c) for c in _chunks(years, size)]
    keyed = {k: v for k, v in com.items() if k != "period"}
    prev = {k: v for k, v in keyed.items() if k != "maxRecords"}
    prev["maxRecords"] = MAX_ROWS_PREVIEW
    rep_txt = "all reporters" if reporter == "all" else common.country_name(reporter)
    par_txt = "all partners" if partner == "all" else f"partner {'World' if partner == 'W00' else common.country_name(partner)}"
    q = {
        "id": qid, "purpose": purpose, "feeds_views": feeds,
        "params": {"reporter": reporter, "partner": partner, "flow": flow, "hs_code": hs, "years": years},
        "comtrade": com,
        "period_chunks": chunks,
        "api_path": f"{KEYED_PATH}?{_qs({**keyed, 'period': com['period']})}",
        "preview_path": f"{PREVIEW_PATH}?{_qs({**prev})}&period={{year}}",
        "calls_keyed": len(chunks), "calls_keyless": len(years),
        "save_as": f"raw/{stem}", "save_as_csv": f"raw/{stem}.csv", "save_as_json": f"raw/{stem}.json",
        "note": note,
    }
    q["ui_steps"] = _ui_steps(q, years, stem)
    q["connector_hint"] = (
        f"Ask the Comtrade connector for annual HS {hs} {FLOW_WORD[flow].lower()} values (USD, net weight) "
        f"for {rep_txt} with {par_txt}, years {years[0]}-{years[-1]}, totals only (no transport-mode, "
        f"customs or second-partner breakdown), then save the rows as generic-rows JSON "
        f"(year, reporter, partner, flow, value_usd, net_kg, qty, qty_unit, hs_code) to raw/{stem}.json.")
    return q


def selected_partners(study_dir, cli_partners, home) -> list:
    """Partners for partner_detail queries: --partners wins, else selection.json, else none."""
    raw = cli_partners
    if raw is None:
        sel = common.read_json(common.study_file(study_dir, "selection"), required=False)
        raw = (sel or {}).get("partners") or []
    out = []
    for x in raw:
        rec = common.resolve_country(x)
        if not rec or not common.is_rankable(rec["iso3"]):
            common.blocked(f"partner {x!r} is not a known country")
        if rec["iso3"] != home and rec["iso3"] not in out:
            out.append(rec["iso3"])
    return out


def build_plan(ctx: dict, partners: list, matrix: bool) -> dict:
    """The query list of contracts section 3.1 for this study."""
    home, hs, years, persp = ctx["home"], ctx["hs"], ctx["years"], ctx["perspective"]
    lab = common.labels(persp)
    hf, mf = lab["home_flow_code"], lab["mirror_flow_code"]
    export = persp == "export"
    qs = [
        make_query("home_flows", "Where home sells" if export else "Who home buys from",
                   ["home_flows"], home, "all", hf, hs, years,
                   note="Needs the World total row; if the source omits it, normalize/analyze derive the total from partner rows and warn."),
        make_query("world_imports", "Market sizes" if export else "Competing buyers and home's rank",
                   ["world_imports"], "all", "W00", "M", hs, years),
        make_query("world_exports", "Competing exporters and home's rank" if export else "Supplier scale",
                   ["world_exports"], "all", "W00", "X", hs, years),
        make_query("mirror", "What partners say they bought from home" if export
                   else "What suppliers say they sold to home", ["mirror"], "all", home, mf, hs, years),
    ]
    for p in partners:
        qs.append(make_query(f"partner_detail:{p}",
                             "Supplier mix inside that market" if export else "That supplier's other buyers",
                             ["partner_detail"], p, "all", mf, hs, years))
    if matrix:
        qs.append(make_query("matrix", "Every reporter to every partner (replaces partner_detail files)",
                             ["matrix", "partner_detail"], "all", "all", "X", hs, years, per_year=True,
                             note="Large: split by year; not possible with the keyless preview (500 rows per call)."))
    return {
        "schema_version": common.SCHEMA_VERSION, "generated_at": common.now_iso(),
        "perspective": persp, "home": home, "hs_code": hs, "years": years,
        "endpoints": {"keyed": COMTRADE_HOST + KEYED_PATH, "preview": COMTRADE_HOST + PREVIEW_PATH, "ui": UI_URL},
        "limits": {"max_periods_per_call": MAX_PERIODS, "free_key_calls_per_day": CALLS_PER_DAY_FREE,
                   "keyed_rows_per_call": MAX_ROWS_KEYED, "preview_rows_per_call": MAX_ROWS_PREVIEW,
                   "preview_periods_per_call": 1},
        "queries": qs,
    }


def render_plan_text(plan: dict) -> str:
    lines = [f"Query plan: {plan['perspective']} study, home {plan['home']}, HS {plan['hs_code']}, "
             f"{plan['years'][0]}-{plan['years'][-1]}", ""]
    for q in plan["queries"]:
        p = q["params"]
        lines += [f"[{q['id']}] {q['purpose']}  (feeds: {', '.join(q['feeds_views'])})",
                  f"  reporter={p['reporter']} partner={p['partner']} flow={p['flow']}",
                  f"  API calls: {q['calls_keyed']} keyed, {q['calls_keyless']} keyless preview",
                  f"  API:     {COMTRADE_HOST}{q['api_path']}",
                  f"  Preview: {COMTRADE_HOST}{q['preview_path']}",
                  f"  Connector: {q['connector_hint']}",
                  "  Web UI steps:"]
        lines += [f"    {i}. {s}" for i, s in enumerate(q["ui_steps"], 1)]
        if q["note"]:
            lines.append(f"  Note: {q['note']}")
        lines.append("")
    return "\n".join(lines)


def cmd_plan(args) -> int:
    ctx = load_inputs(args.study_dir)
    cli = [s.strip() for s in args.partners.split(",") if s.strip()] if args.partners else None
    partners = selected_partners(args.study_dir, cli, ctx["home"])
    plan = build_plan(ctx, partners, args.matrix)
    common.write_json(common.study_file(args.study_dir, "query_plan"), plan)
    print(json.dumps(plan, indent=2, ensure_ascii=False) if args.format == "json" else render_plan_text(plan))
    return 0


# =============================================================== fetch

def http_get_json(url, headers=None, timeout=60):
    """GET url and return (status_code, parsed_json). HTTP error statuses are returned, not raised.
    Connection problems (URLError, OSError) propagate. Tests monkeypatch this function."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        status, body = err.code, err.read()
    try:
        return status, json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, {"error": body[:200].decode("utf-8", "replace")}


def pause(seconds: float) -> None:
    time.sleep(seconds)


class Unreachable(Exception):
    """The Comtrade host cannot be reached from this machine."""


class FetchFailure(Exception):
    """A call failed for a reason other than connectivity."""


def redact(text, key):
    s = str(text)
    return s.replace(key, "<redacted>") if key else s


class Fetcher:
    """Runs HTTP calls with pacing, retry/backoff and key redaction."""

    def __init__(self, key, timeout=60):
        self.key, self.timeout, self.calls = key, timeout, 0

    def headers(self, shown=False):
        if not self.key:
            return {}
        return {KEY_HEADER: "<redacted>" if shown else self.key}

    def get(self, url):
        if self.calls:
            pause(MIN_SLEEP_S)
        self.calls += 1
        last = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                status, body = http_get_json(url, self.headers(), self.timeout)
            except (urllib.error.URLError, OSError) as err:
                raise Unreachable(redact(err, self.key)) from None
            if status == 200:
                return body
            last = f"HTTP {status}: {redact(str(body)[:200], self.key)}"
            if status in (401, 403):
                raise FetchFailure(f"{last} (key rejected or no access to this endpoint)")
            if status == 429 or status >= 500:
                if attempt < MAX_RETRIES:
                    pause(max(MIN_SLEEP_S, 2.0 ** (attempt + 1)))
                    continue
            break
        raise FetchFailure(last or "unknown error")


def planned_calls(plan: dict, keyed: bool, only) -> tuple:
    """List of calls [{qid, label, url, period}] and a list of skipped-query messages."""
    calls, skipped = [], []
    for q in plan["queries"]:
        if only and q["id"] not in only:
            continue
        if not keyed and q["id"] == "matrix":
            skipped.append("matrix skipped: needs more than 500 rows per call; use a key or upload a CSV")
            continue
        base = COMTRADE_HOST + (KEYED_PATH if keyed else PREVIEW_PATH)
        periods = q["period_chunks"] if keyed else [str(y) for y in q["params"]["years"]]
        for per in periods:
            p = {k: v for k, v in q["comtrade"].items() if k != "period"}
            if not keyed:
                p["maxRecords"] = MAX_ROWS_PREVIEW
            p["period"] = per
            calls.append({"qid": q["id"], "period": per, "url": f"{base}?{_qs(p)}", "stem": id_stem(q["id"])})
    return calls, skipped


def _rows_of(body) -> list:
    return body.get("data") or [] if isinstance(body, dict) else []


def run_query_calls(f: Fetcher, qcalls: list, keyed: bool, raw_dir: Path, log: dict) -> None:
    """Execute all calls of one query, save raw file(s) and append a log entry."""
    qid, stem = qcalls[0]["qid"], qcalls[0]["stem"]
    entry = {"id": qid, "mode": "keyed" if keyed else "preview", "calls": 0, "rows": 0, "truncated": False,
             "files": [], "error": None}
    log["queries"].append(entry)
    limit = MAX_ROWS_KEYED if keyed else MAX_ROWS_PREVIEW
    merged = []
    try:
        for c in qcalls:
            body = f.get(c["url"])
            entry["calls"] += 1
            rows = _rows_of(body)
            if not rows and isinstance(body, dict) and body.get("error"):
                raise FetchFailure(redact(body["error"], f.key))
            if len(rows) >= limit:
                entry["truncated"] = True
                msg = (f"WARNING: {qid} period {c['period']} returned exactly {len(rows)} rows: the response is "
                       f"TRUNCATED. Use a Comtrade key or upload the CSV from the web UI.")
                log["warnings"].append(msg)
                print(msg, file=sys.stderr)
            entry["rows"] += len(rows)
            if keyed:
                merged.append(body)
            else:
                name = f"{stem}_{c['period']}.json"
                common.write_json(raw_dir / name, body)
                entry["files"].append(name)
        if keyed:
            body = dict(merged[0])
            body["data"] = [r for b in merged for r in _rows_of(b)]
            body["count"] = len(body["data"])
            common.write_json(raw_dir / f"{stem}.json", body)
            entry["files"].append(f"{stem}.json")
    except FetchFailure as err:
        entry["error"] = str(err)
        print(f"ERROR: {qid}: {err}", file=sys.stderr)


def cmd_fetch(args) -> int:
    plan = common.read_json(common.study_file(args.study_dir, "query_plan"))
    key = os.environ.get(args.key_env) or None
    keyed = bool(key)
    only = {s.strip() for s in args.only.split(",") if s.strip()} if args.only else None
    calls, skipped = planned_calls(plan, keyed, only)
    mode = "keyed endpoint" if keyed else "keyless preview endpoint (500 rows and one period per call)"
    if args.dry_run:
        print(f"DRY RUN - mode: {mode}; {len(calls)} calls")
        for c in calls:
            print(f"GET {c['url']}\n    header {KEY_HEADER}: {'<redacted>' if keyed else '(none, keyless)'}")
        for s in skipped:
            print(s)
        return 0
    if not keyed:
        print(f"NOTE: no key in ${args.key_env}: using the keyless preview (max {MAX_ROWS_PREVIEW} rows per call, "
              f"one year per call). Large markets will be truncated; prefer a free key or CSV upload.", file=sys.stderr)
    raw_dir = Path(args.study_dir) / "raw"
    log = {"schema_version": common.SCHEMA_VERSION, "started": common.now_iso(), "mode": mode,
           "key_present": keyed, "queries": [], "warnings": list(skipped), "unreachable": None}
    f, status = Fetcher(key), 0
    by_q = defaultdict(list)
    for c in calls:
        by_q[c["qid"]].append(c)
    try:
        for qcalls in by_q.values():
            run_query_calls(f, qcalls, keyed, raw_dir, log)
    except Unreachable as err:
        log["unreachable"] = str(err)
    log["finished"], log["calls_made"] = common.now_iso(), f.calls
    common.write_json(raw_dir / "_fetch_log.json", log)
    if log["unreachable"]:
        common.blocked(f"cannot reach comtradeapi.un.org ({log['unreachable']}). Alternatives: use a Comtrade "
                       f"connector/MCP in this host and save rows as generic-rows JSON under raw/, or download CSVs "
                       f"with the web-UI steps in query_plan.json and save them under raw/.")
    if any(e["error"] for e in log["queries"]):
        status = 1
    n_trunc = sum(1 for e in log["queries"] if e["truncated"])
    print(f"Fetched {len(log['queries'])} queries with {f.calls} calls; truncated: {n_trunc}; log: raw/_fetch_log.json")
    return status


# =============================================================== normalize: parsing

def to_num(x):
    """Float from a CSV/JSON cell; None when blank or not numeric."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def blank(x) -> bool:
    return x is None or (isinstance(x, str) and not x.strip())


def decode_bytes(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def read_csv_rows(path: Path, warnings: list) -> list:
    """CSV -> list of dicts. Tolerates the Comtrade quirk of one extra trailing empty field per data row."""
    reader = csv.reader(io.StringIO(decode_bytes(path.read_bytes()), newline=""))
    header = next(reader, None)
    if not header:
        return []
    header = [h.strip() for h in header]
    out, bad = [], 0
    for rec in reader:
        if not rec or all(not c.strip() for c in rec):
            continue
        if len(rec) == len(header) + 1 and not rec[-1].strip():
            rec = rec[:-1]
        if len(rec) != len(header):
            bad += 1
            continue
        out.append(dict(zip(header, rec)))
    if bad:
        warnings.append(f"{path.name}: {bad} malformed CSV rows skipped (field count differs from header)")
    return out


def read_json_rows(path: Path) -> list:
    obj = json.loads(decode_bytes(path.read_bytes()))
    if isinstance(obj, dict):
        obj = obj.get("data", obj.get("rows", []))
    if not isinstance(obj, list):
        raise ValueError(f"{path.name}: expected a JSON list or an object with a 'data' list")
    return [r for r in obj if isinstance(r, dict)]


def detect_format(rows: list) -> str:
    keys = set(rows[0]) if rows else set()
    if {"reporterCode", "flowCode"} & keys:
        return "comtrade"
    if {"year", "reporter", "partner", "flow"} <= keys:
        return "generic"
    return "unknown"


def norm_flow(x) -> str:
    return FLOW_ALIASES.get(re.sub(r"\s+", " ", str(x or "").strip().lower()), str(x or "").strip().upper())


def norm_hs(x) -> str:
    s = re.sub(r"[^0-9A-Za-z]", "", str(x if x is not None else "")).upper()
    if s.isdigit() and len(s) % 2 == 1:
        s = s.zfill(len(s) + 1)  # leading zero lost by a spreadsheet
    return s


def resolve_area(code, iso, desc) -> dict:
    """Resolve an area by numeric code, then ISO, then name. Unknown areas become `_<code>`."""
    rec = None
    for key in (code, iso, desc):
        if not blank(key):
            rec = common.resolve_country(key.strip() if isinstance(key, str) else key)
            if rec:
                break
    if rec:
        return {"iso3": rec["iso3"], "name": rec["name"], "kind": rec["kind"], "unknown": False}
    ident = str(code).strip() if not blank(code) else re.sub(r"[^A-Za-z0-9]+", "", str(desc or iso or "UNKNOWN"))
    ident = str(int(float(ident))) if re.fullmatch(r"\d+(\.0+)?", ident) else ident
    name = str(desc) if not blank(desc) else f"Area {ident}"
    return {"iso3": f"_{ident}", "name": name, "kind": "area", "unknown": True}


def canon_comtrade(r: dict) -> dict:
    flow = norm_flow(r.get("flowCode") or r.get("flowDesc"))
    val = to_num(r.get("primaryValue"))
    if val is None:
        val = to_num(r.get("cifvalue") if flow == "M" else r.get("fobvalue"))
    p2, mot, cus = r.get("partner2Code"), r.get("motCode"), r.get("customsCode")
    is_total = (blank(p2) or str(p2).strip() in ("0", "0.0")) and (blank(mot) or str(mot).strip() in ("0", "0.0")) \
        and (blank(cus) or str(cus).strip().upper() in ("C00", "TOTAL"))
    year = r.get("refYear") if not blank(r.get("refYear")) else r.get("period")
    return {"year": year, "rep": (r.get("reporterCode"), r.get("reporterISO"), r.get("reporterDesc")),
            "par": (r.get("partnerCode"), r.get("partnerISO"), r.get("partnerDesc")), "flow": flow,
            "hs": r.get("cmdCode"), "value": val, "net_kg": to_num(r.get("netWgt")), "qty": to_num(r.get("qty")),
            "qty_unit": r.get("qtyUnitAbbr") or "", "is_total": is_total}


def canon_generic(r: dict) -> dict:
    return {"year": r.get("year"), "rep": (r.get("reporter"), None, None) if re.fullmatch(r"\d+", str(r.get("reporter")).strip())
            else (None, None, r.get("reporter")),
            "par": (r.get("partner"), None, None) if re.fullmatch(r"\d+", str(r.get("partner")).strip())
            else (None, None, r.get("partner")),
            "flow": norm_flow(r.get("flow")), "hs": r.get("hs_code"), "value": to_num(r.get("value_usd")),
            "net_kg": to_num(r.get("net_kg")), "qty": to_num(r.get("qty")), "qty_unit": r.get("qty_unit") or "",
            "is_total": True}


def parse_file(path: Path, warnings: list) -> tuple:
    """Return (format_label, canonical_rows) for one raw file."""
    rows = read_json_rows(path) if path.suffix.lower() == ".json" else read_csv_rows(path, warnings)
    fmt = detect_format(rows)
    if rows and fmt == "unknown":
        raise ValueError(f"{path.name}: unrecognised columns {sorted(rows[0])[:8]}; see contracts 3.2")
    kind = {"json": "json", "csv": "csv"}.get(path.suffix.lower().lstrip("."), "csv")
    if fmt == "comtrade":
        label = "comtrade_api_json" if kind == "json" else "comtrade_ui_csv"
        return label, [canon_comtrade(r) for r in rows]
    return f"generic_rows_{kind}", [canon_generic(r) for r in rows]


# =============================================================== normalize: cleaning

def clean_rows(canon: list, ctx: dict, drops: Counter, unknown: dict, fname: str, warns: list) -> list:
    """Filter flows/areas/years and resolve identities. Returns rows with iso3 keys, aggregates dropped."""
    out, years = [], set(ctx["years"])
    for r in canon:
        flow = r["flow"]
        if flow in ("RX", "RM"):
            drops["reexports"] += 1
            continue
        if flow not in ("X", "M"):
            drops["other_flows"] += 1
            continue
        try:
            year = int(str(r["year"]).strip()[:4])
        except ValueError:
            drops["unusable_rows"] += 1
            continue
        if year not in years:
            drops["outside_years"] += 1
            continue
        rep, par = resolve_area(*r["rep"]), resolve_area(*r["par"])
        if rep["kind"] == "aggregate" or rep["iso3"] == "W00" or par["kind"] == "aggregate":
            drops["aggregates"] += 1
            continue
        if r["value"] is None:
            drops["unusable_rows"] += 1
            continue
        for a in (rep, par):
            if a["unknown"]:
                unknown.setdefault(a["iso3"], a["name"])
        hs = norm_hs(r["hs"]) if not blank(r["hs"]) else ""
        if not hs:
            hs = norm_hs(ctx["hs"])
            warns.append(f"{fname}: rows without hs_code were assumed to be the agreed code {ctx['hs']}")
        out.append({**r, "year": year, "rep": rep, "par": par, "hs": hs})
    return out


def collapse_file(rows: list, stats: Counter, fname: str, warns: list) -> dict:
    """One file -> {key: row}. Breakdown rows (2nd partner / customs / transport mode) are folded:
    a TOTAL row wins; otherwise the breakdown rows are summed."""
    groups = defaultdict(list)
    for r in rows:
        groups[(r["year"], r["rep"]["iso3"], r["par"]["iso3"], r["flow"], r["hs"])].append(r)
    out = {}
    for key, g in groups.items():
        totals = [r for r in g if r["is_total"]]
        if totals:
            pick = totals[0]
            if len(totals) > 1 and any(abs(t["value"] - pick["value"]) > 0.5 for t in totals):
                warns.append(f"{fname}: conflicting TOTAL rows for {key}; first kept")
            stats["breakdown_rows_ignored"] += len(g) - 1
        elif len(g) == 1:
            pick = g[0]
        else:
            pick = dict(g[0])
            pick["value"] = sum(r["value"] for r in g)
            kgs = [r["net_kg"] for r in g if r["net_kg"] is not None]
            pick["net_kg"] = sum(kgs) if kgs else None
            qs = [r["qty"] for r in g if r["qty"] is not None]
            pick["qty"] = sum(qs) if qs else None
            stats["breakdown_groups_summed"] += 1
        out[key] = pick
    return out


def merge_files(per_file: dict, order: list, warns: list) -> dict:
    """Collapse duplicates across files. `order` lists file names, most specific first."""
    final = {}
    for fname in order:
        for key, r in per_file[fname].items():
            r = {**r, "source_file": fname}
            if key not in final:
                final[key] = r
            elif abs(final[key]["value"] - r["value"]) > max(1.0, 1e-6 * abs(r["value"])):
                warns.append(f"conflicting values for {key}: {final[key]['value']:.0f} ({final[key]['source_file']}) "
                             f"vs {r['value']:.0f} ({fname}); kept the more specific file")
    return final


def detect_views(final: dict, ctx: dict) -> dict:
    """Which of the contract's views the table contains (contracts 3.1/3.3)."""
    lab = common.labels(ctx["perspective"])
    home, hf, mf = ctx["home"], lab["home_flow_code"], lab["mirror_flow_code"]
    keys = list(final)
    world = {fl: {k[1] for k in keys if k[3] == fl and k[2] == "W00"} for fl in ("X", "M")}
    partner_level = defaultdict(lambda: defaultdict(set))   # flow -> reporter -> partners (non-World)
    for y, rep, par, fl, hs in keys:
        if par != "W00":
            partner_level[fl][rep].add(par)
    detail = sorted(r for r, ps in partner_level[mf].items() if r != home and len(ps - {home}) >= 2)
    return {
        "home_flows": bool(partner_level[hf].get(home)),
        "world_imports": len(world["M"]) >= 2,
        "world_exports": len(world["X"]) >= 2,
        "mirror": any(k[3] == mf and k[2] == home and k[1] != home for k in keys),
        "partner_detail": [d for d in detail if common.is_rankable(d)],
        "matrix": sum(1 for r, ps in partner_level["X"].items() if len(ps) >= 2) >= MATRIX_MIN_REPORTERS,
    }


def coverage_report(final: dict, years: list) -> dict:
    """Reporter counts per year for each World-partner flow (the data-coverage check)."""
    cov = {}
    for fl, name in (("M", "world_imports"), ("X", "world_exports")):
        per = defaultdict(set)
        for y, rep, par, f, hs in final:
            if f == fl and par == "W00" and common.is_rankable(rep):
                per[y].add(rep)
        cov[name] = {str(y): len(per.get(y, ())) for y in years}
    return cov


def fmt_num(x):
    if x is None:
        return ""
    return str(int(x)) if float(x).is_integer() else repr(round(float(x), 4))


def write_flows(path: Path, final: dict) -> None:
    cols = ["year", "reporter_iso3", "reporter_name", "partner_iso3", "partner_name", "flow", "hs_code",
            "value_usd", "net_kg", "qty", "qty_unit", "source_file"]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for key in sorted(final):
            r = final[key]
            w.writerow([r["year"], r["rep"]["iso3"], r["rep"]["name"], r["par"]["iso3"], r["par"]["name"], r["flow"],
                        r["hs"], fmt_num(r["value"]), fmt_num(r["net_kg"]), fmt_num(r["qty"]), r["qty_unit"],
                        r["source_file"]])


def list_raw_files(study_dir, files) -> list:
    raw = Path(study_dir) / "raw"
    if files:
        out = []
        for f in files:
            p = Path(f)
            if not p.exists() and (raw / f).exists():
                p = raw / f
            if not p.exists():
                common.blocked(f"file not found: {f}")
            out.append(p)
        return out
    found = sorted(p for p in raw.glob("*") if p.is_file() and not p.name.startswith("_")
                   and p.suffix.lower() in (".csv", ".json")) if raw.is_dir() else []
    if not found:
        common.blocked(f"no CSV/JSON files in {raw}. Run 'fetch', save connector rows there, or upload CSVs "
                       f"(see query_plan.json).")
    return found


def check_hs(found: Counter, ctx: dict, allow: set) -> None:
    agreed = norm_hs(ctx["hs"])
    extra = sorted(set(found) - {agreed} - allow)
    if extra or agreed not in found:
        common.blocked(f"HS codes found in the data {dict(found)} but only the agreed code {agreed} is allowed. "
                       f"Re-extract with the agreed code (or pass --allow-hs {','.join(extra) or 'CODE'} to accept others).")


def cmd_normalize(args) -> int:
    ctx = load_inputs(args.study_dir)
    allow = {norm_hs(c) for c in (args.allow_hs or "").split(",") if c.strip()}
    paths = list_raw_files(args.study_dir, args.files)
    warns, drops, stats, unknown = [], Counter(), Counter(), {}
    per_file, files_meta, spec = {}, [], {}
    for p in paths:
        try:
            fmt, canon = parse_file(p, warns)
        except (ValueError, OSError) as err:
            print(f"ERROR: {err}", file=sys.stderr)
            return 1
        rows = clean_rows(canon, ctx, drops, unknown, p.name, warns)
        per_file[p.name] = collapse_file(rows, stats, p.name, warns)
        spec[p.name] = len({k[1] for k in per_file[p.name]})
        files_meta.append({"file": p.name, "format": fmt, "rows_in": len(canon), "rows_kept": 0})
    found = Counter(k[4] for d in per_file.values() for k in d)
    check_hs(found, ctx, allow)
    final = merge_files(per_file, sorted(per_file, key=lambda n: (spec[n], n)), warns)
    views = detect_views(final, ctx)
    if not views["home_flows"]:
        common.blocked(f"home_flows view absent: no {common.labels(ctx['perspective'])['home_flow']} rows for home "
                       f"{ctx['home']} with partner detail were found. Load the home_flows query (see query_plan.json).")
    kept = Counter(r["source_file"] for r in final.values())
    for fm in files_meta:
        fm["rows_kept"] = kept.get(fm["file"], 0)
    for iso, name in sorted(unknown.items()):
        warns.append(f"unrecognised area kept as {iso} ({name}); never ranked")
    warns = list(dict.fromkeys(warns))
    if len(found) > 1:
        warns.append(f"several HS codes accepted via --allow-hs and summed per cell: {sorted(found)}")
    write_flows(common.study_file(args.study_dir, "flows"), final)
    meta = {"schema_version": common.SCHEMA_VERSION, "hs_codes_found": sorted(found),
            "years": sorted({k[0] for k in final}), "files": files_meta, "views": views,
            "dropped": {"reexports": drops["reexports"], "aggregates": drops["aggregates"], "other_hs": 0,
                        "other_flows": drops["other_flows"], "outside_years": drops["outside_years"],
                        "unusable_rows": drops["unusable_rows"],
                        "breakdown_rows_ignored": stats["breakdown_rows_ignored"],
                        "breakdown_groups_summed": stats["breakdown_groups_summed"]},
            "coverage": coverage_report(final, ctx["years"]), "warnings": warns}
    common.write_json(common.study_file(args.study_dir, "flows_meta"), meta)
    print_normalize_report(meta, len(final))
    return 0


def print_normalize_report(meta: dict, n_rows: int) -> None:
    v = meta["views"]
    print(f"flows.csv: {n_rows} rows from {len(meta['files'])} files; years {meta['years']}")
    print("views: " + ", ".join(f"{k}={'yes' if v[k] else 'NO'}" for k in ("home_flows", "world_imports", "world_exports", "mirror"))
          + f", partner_detail={v['partner_detail']}, matrix={v['matrix']}")
    print(f"dropped: {meta['dropped']}")
    for view, per in meta["coverage"].items():
        print(f"coverage {view} (reporters per year): " + ", ".join(f"{y}:{n}" for y, n in per.items()))
    for w in meta["warnings"][:10]:
        print(f"warning: {w}")
    if len(meta["warnings"]) > 10:
        print(f"... {len(meta['warnings']) - 10} more warnings in flows_meta.json")


# =============================================================== CLI

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="write query_plan.json from inputs.json")
    p.add_argument("--study-dir", required=True)
    p.add_argument("--partners", help="comma-separated ISO3 codes for partner_detail queries (default: selection.json)")
    p.add_argument("--matrix", action="store_true", help="add the optional all-reporters x all-partners query")
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("fetch", help="download the planned queries from the Comtrade API into raw/")
    p.add_argument("--study-dir", required=True)
    p.add_argument("--key-env", default=DEFAULT_KEY_ENV, help="environment variable holding the subscription key")
    p.add_argument("--only", help="comma-separated query ids to run")
    p.add_argument("--dry-run", action="store_true", help="print the calls (key redacted) and do nothing")
    p.set_defaults(fn=cmd_fetch)
    p = sub.add_parser("normalize", help="convert raw/ files into flows.csv and flows_meta.json")
    p.add_argument("--study-dir", required=True)
    p.add_argument("files", nargs="*", help="specific files (default: every csv/json in raw/ not starting with _)")
    p.add_argument("--allow-hs", help="comma-separated extra HS codes to accept besides the agreed one")
    p.set_defaults(fn=cmd_normalize)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except SystemExit:
        raise
    except Exception as err:  # noqa: BLE001 - last-resort reporting, exit code 1
        print(f"ERROR: {type(err).__name__}: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

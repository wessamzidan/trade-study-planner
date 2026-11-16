#!/usr/bin/env python3
"""Freight stage (stage 4): collect rates / calibration points and build freight.json.

Ladder per lane: quote > connector > published > modelled > coverage gap.
A modelled price needs at least one dated, cited calibration point; nothing is ever defaulted.
See references/contracts.md section 6 and references/freight_ladder.md.

Subcommands: init, set-port, add-rate, set-charge, add-calibration, set-multiplier, set-range,
freightos, ports, build.   Exit codes: 0 ok, 2 blocked (input missing), 1 error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

PORTS_FILE = common.DATA_DIR / "ports.json"
RUNGS = ("quote", "connector", "published")          # priced from supplied rates; modelled is computed
RATE_TIERS = RUNGS
CHARGE_TIERS = tuple(t for t in common.TIERS if t != "missing")
MULT_TIERS = CHARGE_TIERS
DEFAULT_RANGE_PCT = 35          # contract section 6.1 example value; only used if the inputs omit model.range_pct
FREIGHTOS_URL = "https://ship.freightos.com/api/rate-estimate/v1/search"
FREIGHTOS_SOURCE = "Freightos Rate Estimate API (market average)"
FREIGHTOS_DAILY_LIMIT = 10
FREIGHTOS_PACKAGING = {"20DC": "container20", "20RF": "container20", "40DC": "container40",
                       "40RF": "container40", "40HC": "container40HC"}
DRY_EQUIVALENT = {"20RF": "20DC", "40RF": "40DC"}
NO_SEAROUTE_MSG = "searoute is not installed: distance and the modelled rung are unavailable (pip install searoute)"


# ---------------------------------------------------------------- helpers

def load_ports() -> dict:
    return common.read_json(PORTS_FILE)


def is_date(s) -> bool:
    try:
        dt.date.fromisoformat(str(s))
        return True
    except ValueError:
        return False


def get_inputs(study_dir) -> dict:
    return common.read_json(common.study_file(study_dir, "inputs"))


def get_selection(study_dir) -> list:
    sel = common.read_json(common.study_file(study_dir, "selection"), required=False)
    if sel is None:
        common.blocked("selection.json not found: the shortlist (checkpoint B) must be decided before freight "
                       "(expected at <study-dir>/selection.json)")
    partners = sel.get("partners") or []
    if not partners:
        common.blocked("selection.json has no partners")
    return list(partners)


def get_fi(study_dir, required=True) -> dict | None:
    return common.read_json(common.study_file(study_dir, "freight_inputs"), required=required)


def save_fi(study_dir, fi) -> None:
    common.write_json(common.study_file(study_dir, "freight_inputs"), fi)


def need_lane(fi, lane) -> str:
    rec = common.resolve_country(lane)
    iso = rec["iso3"] if rec else str(lane).upper()
    if iso not in (fi.get("lanes") or {}):
        common.blocked(f"lane {lane!r} is not in freight_inputs.json; run `estimate_freight.py init` first")
    return iso


def port_from_default(ports_db, iso3):
    """-> (port dict {name, locode} or None, note or None)."""
    c = ports_db["countries"].get(iso3)
    if not c:
        return None, f"no port known for {iso3} in ports.json: set one with set-port"
    if c.get("landlocked"):
        loc = (c.get("gateways") or [None])[0]
        info = ports_db["by_locode"].get(loc, {})
        return ({"name": info.get("name"), "locode": loc},
                f"{common.country_name(iso3)} is landlocked: gateway port {info.get('name')} ({loc}) used; "
                f"inland leg to be costed under origin_inland/destination_inland")
    p = next((p for p in c["ports"] if p.get("default")), c["ports"][0])
    return {"name": p["name"], "locode": p["locode"]}, None


def resolve_port(port, ports_db):
    """Port dict with coordinates (lat/lon None when unknown)."""
    if not port or not port.get("locode"):
        return {"name": (port or {}).get("name"), "locode": None, "lat": None, "lon": None}
    info = ports_db["by_locode"].get(port["locode"])
    name = port.get("name") or (info or {}).get("name")
    lat = port.get("lat", (info or {}).get("lat"))
    lon = port.get("lon", (info or {}).get("lon"))
    return {"name": name, "locode": port["locode"], "lat": lat, "lon": lon}


def sea_nm(lat1, lon1, lat2, lon2):
    """Sea distance in nautical miles via searoute; (None, warning) when unavailable."""
    if None in (lat1, lon1, lat2, lon2):
        return None, "port coordinates unknown"
    try:
        import searoute
    except ImportError:
        return None, NO_SEAROUTE_MSG
    try:
        r = searoute.searoute([lon1, lat1], [lon2, lat2], units="naut")
        return float(r["properties"]["length"]), None
    except Exception as e:  # noqa
        return None, f"searoute failed ({type(e).__name__}: {e})"


def pos_number(x, name):
    try:
        v = float(x)
    except (TypeError, ValueError):
        common.blocked(f"{name} must be a number")
    if not math.isfinite(v):
        common.blocked(f"{name} must be finite")
    return v


def fmt(v, nd=0):
    return "-" if v is None else f"{v:,.{nd}f}"


# ---------------------------------------------------------------- init

def container_type_for(inputs, cli_type):
    com = inputs.get("commercial") or {}
    t = (com.get("container") or {}).get("type") if isinstance(com, dict) else None
    if t:
        return t, (f"--container {cli_type} ignored: inputs.json says {t}" if cli_type and cli_type != t else None)
    if cli_type:
        return cli_type, None
    common.blocked("container type unknown: inputs.commercial.container.type is absent; pass --container "
                   f"({'/'.join(common.CONTAINER_TYPES)})")


def cmd_init(a):
    inputs = get_inputs(a.study_dir)
    partners = get_selection(a.study_dir)
    ctype, msg = container_type_for(inputs, a.container)
    if ctype not in common.CONTAINER_TYPES:
        common.blocked(f"container type {ctype!r} not one of {', '.join(common.CONTAINER_TYPES)}")
    if msg:
        print(f"NOTE: {msg}")
    ports_db = load_ports()
    home = inputs.get("home_country")
    if not home:
        common.blocked("inputs.json has no home_country")
    fi = get_fi(a.study_dir, required=False)
    created = fi is None
    if created:
        fi = {"schema_version": common.SCHEMA_VERSION, "container_type": ctype, "home_port": None, "lanes": {},
              "calibration_points": [], "equipment_multiplier": common.sv_missing("not set: set-multiplier"),
              "model": {"range_pct": DEFAULT_RANGE_PCT}, "meta": {}}
    elif fi.get("container_type") != ctype:
        print(f"WARNING: freight_inputs.json has container_type {fi.get('container_type')}, inputs say {ctype}; kept the file's value")
    fi.setdefault("lanes", {})
    fi.setdefault("calibration_points", [])
    fi.setdefault("meta", {})
    fi.setdefault("model", {"range_pct": DEFAULT_RANGE_PCT})
    fi.setdefault("equipment_multiplier", common.sv_missing("not set: set-multiplier"))
    notes = []
    if not fi.get("home_port"):
        hp, note = port_from_default(ports_db, home)
        fi["home_port"] = hp
        if note:
            fi["home_port_note"] = note
            notes.append(note)
    added = []
    for iso in partners:
        if iso in fi["lanes"]:
            continue
        pp, note = port_from_default(ports_db, iso)
        lane = {"partner_port": pp, "rates": [], "local_charges": {k: common.sv_missing("not yet collected") for k in common.CHARGE_KEYS}}
        if note:
            lane["port_note"] = note
        fi["lanes"][iso] = lane
        added.append(iso)
    for iso, lane in fi["lanes"].items():          # top up charge keys on existing lanes, never overwrite values
        lc = lane.setdefault("local_charges", {})
        for k in common.CHARGE_KEYS:
            lc.setdefault(k, common.sv_missing("not yet collected"))
        lane.setdefault("rates", [])
    save_fi(a.study_dir, fi)
    print(f"{'Created' if created else 'Updated'} freight_inputs.json ({ctype}); lanes added: {', '.join(added) or 'none'}; "
          f"existing lanes untouched: {', '.join(i for i in fi['lanes'] if i not in added) or 'none'}")
    for n in notes:
        print(f"  NOTE: {n}")
    perspective = inputs.get("perspective", "export")
    hp = resolve_port(fi.get("home_port"), ports_db)
    print(f"Home port: {hp['name']} ({hp['locode']})")
    for iso in partners:
        lane = fi["lanes"].get(iso)
        if not lane:
            continue
        pp = resolve_port(lane.get("partner_port"), ports_db)
        o, d = (hp, pp) if perspective == "export" else (pp, hp)
        nm, why = sea_nm(o["lat"], o["lon"], d["lat"], d["lon"])
        need = []
        if not pp["locode"]:
            need.append("partner port (set-port)")
        if not [r for r in lane["rates"]]:
            need.append("a rate (add-rate / freightos)")
        miss = [k for k in common.CHARGE_KEYS if not common.sv_ok(lane["local_charges"].get(k))]
        if miss:
            need.append("charges: " + ", ".join(miss))
        print(f"  {iso}: {o['name']} -> {d['name']}  distance {fmt(nm)} nm" + (f" ({why})" if nm is None else "") +
              f"  | needs: {'; '.join(need) or 'nothing'}")
        if lane.get("port_note"):
            print(f"      NOTE: {lane['port_note']}")
    nc = len(fi["calibration_points"])
    print(f"Calibration points: {nc}" + ("  (none: a modelled price is impossible until add-calibration)" if nc == 0 else ""))
    mv = fi["equipment_multiplier"]
    if not common.sv_ok(mv):
        print("Equipment multiplier: missing (needed only to convert rates/benchmarks of a different container type)")
    return 0


# ---------------------------------------------------------------- setters

def cmd_set_port(a):
    fi = get_fi(a.study_dir)
    ports_db = load_ports()
    loc = a.locode.upper()
    info = ports_db["by_locode"].get(loc)
    port = {"name": a.name or (info or {}).get("name"), "locode": loc}
    if not info:
        if a.lat is None or a.lon is None:
            common.blocked(f"LOCODE {loc} is not in ports.json; give --name, --lat and --lon (cited coordinates) to use it")
        port.update({"lat": a.lat, "lon": a.lon})
    if a.lane.lower() == "home":
        fi["home_port"] = port
        fi.pop("home_port_note", None)
    else:
        iso = need_lane(fi, a.lane)
        fi["lanes"][iso]["partner_port"] = port
        fi["lanes"][iso].pop("port_note", None)
    save_fi(a.study_dir, fi)
    print(f"Port for {a.lane}: {port['name']} ({loc})")
    return 0


def check_cited(source, label="--source"):
    if not source or not str(source).strip():
        common.blocked(f"{label} is required: every figure needs a named source")


def check_date(d, label):
    if d is not None and not is_date(d):
        common.blocked(f"{label} must be YYYY-MM-DD, got {d!r}")


def cmd_add_rate(a):
    fi = get_fi(a.study_dir)
    iso = need_lane(fi, a.lane)
    check_cited(a.source)
    usd = pos_number(a.usd, "--usd")
    if usd <= 0:
        common.blocked("--usd must be greater than 0")
    ctype = a.container_type or fi.get("container_type")
    if ctype not in common.CONTAINER_TYPES:
        common.blocked(f"--container-type must be one of {', '.join(common.CONTAINER_TYPES)}")
    for d, n in ((a.as_of, "--as-of"), (a.valid_until, "--valid-until")):
        check_date(d, n)
    covers = [c.strip() for c in (a.covers or "").split(",") if c.strip()]
    bad = [c for c in covers if c not in common.CHARGE_KEYS]
    if bad:
        common.blocked(f"--covers has unknown charge keys {bad}; valid: {', '.join(common.CHARGE_KEYS)}")
    if covers and not a.all_in:
        common.blocked("--covers only makes sense together with --all-in")
    rate = {"tier": a.tier, "usd": usd, "container_type": ctype, "all_in": bool(a.all_in), "covers": covers,
            "transit_days": None if a.transit_days is None else pos_number(a.transit_days, "--transit-days"),
            "carrier": a.carrier, "source": a.source, "url": a.url, "as_of": a.as_of or common.today(),
            "valid_until": a.valid_until}
    fi["lanes"][iso]["rates"].append(rate)
    save_fi(a.study_dir, fi)
    print(f"Added {a.tier} rate USD {usd:,.2f} ({ctype}) to lane {iso} as of {rate['as_of']}"
          + (f"; all-in covering {', '.join(covers)}" if a.all_in and covers else "")
          + ("; WARNING --all-in without --covers: no local charge will be zeroed" if a.all_in and not covers else ""))
    return 0


def cmd_set_charge(a):
    fi = get_fi(a.study_dir)
    check_cited(a.source)
    if a.key not in common.CHARGE_KEYS:
        common.blocked(f"--key must be one of {', '.join(common.CHARGE_KEYS)}")
    v = pos_number(a.value, "--value")
    if v < 0:
        common.blocked("--value must be >= 0")
    check_date(a.as_of, "--as-of")
    lanes = list(fi["lanes"]) if a.lane.lower() == "all" else [need_lane(fi, a.lane)]
    s = common.sv(v, a.tier, a.source, a.url, a.as_of or common.today())
    for iso in lanes:
        fi["lanes"][iso].setdefault("local_charges", {})[a.key] = dict(s)
    save_fi(a.study_dir, fi)
    print(f"Set {a.key} = {v:,.2f} ({a.tier}) on {', '.join(lanes)}")
    return 0


def cmd_add_calibration(a):
    fi = get_fi(a.study_dir)
    ports_db = load_ports()
    check_cited(a.label, "--label")
    check_cited(a.source)
    if not a.url or not str(a.url).strip():
        common.blocked("--url is required: a benchmark without a citation is rejected")
    if not a.as_of or not is_date(a.as_of):
        common.blocked("--as-of (YYYY-MM-DD) is required: a benchmark without a date is rejected")
    usd = pos_number(a.usd, "--usd")
    if usd <= 0:
        common.blocked("--usd must be greater than 0")
    if a.container_type not in common.CONTAINER_TYPES:
        common.blocked(f"--container-type must be one of {', '.join(common.CONTAINER_TYPES)}")
    pt = {"label": a.label, "from_locode": a.from_locode.upper(), "to_locode": a.to_locode.upper(), "usd": usd,
          "container_type": a.container_type, "source": a.source, "url": a.url, "as_of": a.as_of}
    for end, loc, lat, lon in (("from", pt["from_locode"], a.from_lat, a.from_lon), ("to", pt["to_locode"], a.to_lat, a.to_lon)):
        if loc not in ports_db["by_locode"]:
            if lat is None or lon is None:
                common.blocked(f"LOCODE {loc} is not in ports.json; give --{end}-lat and --{end}-lon")
            pt[f"{end}_lat"], pt[f"{end}_lon"] = lat, lon
    fi.setdefault("calibration_points", []).append(pt)
    save_fi(a.study_dir, fi)
    print(f"Added calibration point '{a.label}' ({len(fi['calibration_points'])} total)")
    return 0


def cmd_set_multiplier(a):
    fi = get_fi(a.study_dir)
    check_cited(a.source)
    v = pos_number(a.value, "--value")
    if v <= 0:
        common.blocked("--value must be greater than 0")
    check_date(a.as_of, "--as-of")
    fi["equipment_multiplier"] = common.sv(v, a.tier, a.source, a.url, a.as_of or common.today())
    save_fi(a.study_dir, fi)
    print(f"Equipment multiplier set to {v} ({a.tier})")
    return 0


def cmd_set_range(a):
    fi = get_fi(a.study_dir)
    v = pos_number(a.pct, "--pct")
    if not 0 <= v < 100:
        common.blocked("--pct must be in [0, 100)")
    fi.setdefault("model", {})["range_pct"] = v
    save_fi(a.study_dir, fi)
    print(f"Modelled range set to +/-{v}%")
    return 0


def cmd_ports(a):
    rec = common.resolve_country(a.country)
    if not rec:
        common.blocked(f"unknown country {a.country!r}")
    db = load_ports()
    c = db["countries"].get(rec["iso3"])
    if not c:
        print(f"{rec['name']} ({rec['iso3']}): no port in ports.json. Use set-port with a cited LOCODE and coordinates.")
        return 0
    if c.get("landlocked"):
        print(f"{rec['name']} ({rec['iso3']}) is landlocked. Gateways:")
        for g in c["gateways"]:
            i = db["by_locode"].get(g, {})
            print(f"  {g}  {i.get('name')}  ({i.get('iso3')})  {i.get('lat')}, {i.get('lon')}")
    else:
        print(f"{rec['name']} ({rec['iso3']}) ports:")
        for p in c["ports"]:
            print(f"  {p['locode']}  {p['name']}  {p['lat']}, {p['lon']}{'  [default]' if p.get('default') else ''}")
    return 0


# ---------------------------------------------------------------- freightos adapter

class HostUnreachable(Exception):
    pass


def _http_post(url, headers, body, timeout=30):
    """The only network call. Returns (status_code, parsed_json_or_None). Raises HostUnreachable."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:  # noqa
            return e.code, None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise HostUnreachable(type(e).__name__) from None


def parse_freightos(resp):
    """-> (usd, currency, transit_days) from the FCL mode, or None when there is no FCL price."""
    for m in (resp or {}).get("modes") or []:
        if str(m.get("mode", "")).upper() != "FCL":
            continue
        ap = m.get("averagePrice") or {}
        if ap.get("value") is None:
            return None
        tt = m.get("averageTransitTime") or {}
        days = None
        if str(tt.get("unit", "days")).lower().startswith("day"):
            ends = [float(tt[k]) for k in ("from", "to") if tt.get(k) is not None]
            days = round(sum(ends) / len(ends), 1) if ends else None
        return float(ap["value"]), str(ap.get("currency", "")).upper(), days
    return None


def cmd_freightos(a):
    inputs = get_inputs(a.study_dir)
    fi = get_fi(a.study_dir)
    key = os.environ.get("FREIGHTOS_API_KEY", "").strip()
    if not key:
        common.blocked("FREIGHTOS_API_KEY is not set. The user can request a free Freightos Rate Estimate API key "
                       "(10 requests a day, about one study of up to 10 lanes), then set it in the environment. "
                       "Other rungs: client quote, published rate, or the modelled estimate from calibration points.")
    ctype = fi.get("container_type")
    pack = FREIGHTOS_PACKAGING.get(ctype)
    if not pack:
        common.blocked(f"container type {ctype!r} cannot be mapped to a Freightos packagingType")
    ports_db = load_ports()
    perspective = inputs.get("perspective", "export")
    wanted = None
    if a.lanes:
        wanted = []
        for x in a.lanes.split(","):
            if x.strip():
                wanted.append(need_lane(fi, x.strip()))
    today = common.today()
    todo, skipped = [], []
    hp = fi.get("home_port") or {}
    for iso, lane in fi["lanes"].items():
        if wanted is not None and iso not in wanted:
            continue
        pp = lane.get("partner_port") or {}
        if not hp.get("locode") or not pp.get("locode"):
            skipped.append(f"{iso}: port missing (set-port)")
            continue
        if not a.force and any(r.get("source") == FREIGHTOS_SOURCE and r.get("as_of") == today and r.get("container_type") == ctype
                               for r in lane.get("rates", [])):
            skipped.append(f"{iso}: already has a Freightos rate from today")
            continue
        o, d = (hp["locode"], pp["locode"]) if perspective == "export" else (pp["locode"], hp["locode"])
        todo.append((iso, o, d))
    calls = fi.setdefault("meta", {}).setdefault("freightos_calls", {})
    used = int(calls.get(today, 0))
    for s in skipped:
        print(f"  skip {s}")
    print(f"Freightos: {len(todo)} request(s) to make; {used} already used today of {FREIGHTOS_DAILY_LIMIT}.")
    if used + len(todo) > FREIGHTOS_DAILY_LIMIT and not a.force:
        common.blocked(f"{len(todo)} requests would exceed the daily limit of {FREIGHTOS_DAILY_LIMIT} ({used} used today). "
                       "Pass --lanes to choose fewer lanes, wait until tomorrow, or use --force.")
    dry = DRY_EQUIVALENT.get(ctype)
    if dry:
        print(f"  note: {ctype} is a reefer; the dry equivalent ({dry}) is requested and the rate is stored as {ctype} "
              "(apply set-multiplier for the dry-to-reefer conversion)")
    headers = {"apiKey": key, "Content-Type": "application/json"}
    added = 0
    for iso, o, d in todo:
        body = {"shipment": {"origin": {"type": "seaport", "code": o}, "destination": {"type": "seaport", "code": d},
                             "load": {"packages": [{"packagingType": pack, "quantity": 1}]}}}
        try:
            status, resp = _http_post(FREIGHTOS_URL, headers, body)
        except HostUnreachable as e:
            save_fi(a.study_dir, fi)
            common.blocked(f"ship.freightos.com is unreachable ({e}). Use the other rungs: a client quote, a published "
                           "rate, or the modelled estimate from dated calibration points.")
        calls[today] = int(calls.get(today, 0)) + 1
        save_fi(a.study_dir, fi)
        if status in (401, 403):
            common.blocked(f"Freightos rejected the API key (HTTP {status}); ask the user to check it")
        if status == 429:
            common.blocked("Freightos daily request limit reached (HTTP 429); try again tomorrow")
        if status != 200:
            print(f"  {iso}: HTTP {status}, nothing recorded")
            continue
        got = parse_freightos(resp)
        if not got:
            print(f"  {iso}: no FCL price in the response, nothing recorded")
            continue
        usd, cur, days = got
        if cur != "USD":
            print(f"  WARNING {iso}: price is in {cur or 'an unknown currency'}, not USD; skipped")
            continue
        fi["lanes"][iso]["rates"].append({
            "tier": "connector", "usd": usd, "container_type": ctype, "all_in": False, "covers": [],
            "transit_days": days, "carrier": None, "source": FREIGHTOS_SOURCE, "url": "https://ship.freightos.com",
            "as_of": today, "valid_until": None})
        added += 1
        print(f"  {iso}: {o} -> {d}  USD {usd:,.0f}" + (f", {days} days" if days else ""))
        save_fi(a.study_dir, fi)
    save_fi(a.study_dir, fi)
    print(f"Freightos: {added} rate(s) recorded; {calls.get(today, 0)} of {FREIGHTOS_DAILY_LIMIT} requests used today.")
    return 0


# ---------------------------------------------------------------- build

def fit_line(xs, ys):
    """-> (a, b, method, warning). Least squares a + b*x; through-origin fallback."""
    n = len(xs)

    def origin():
        sxx = sum(x * x for x in xs)
        return 0.0, sum(x * y for x, y in zip(xs, ys)) / sxx

    if n == 1:
        a_, b_ = origin()
        return a_, b_, "through_origin", None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        a_, b_ = origin()
        return a_, b_, "through_origin", "calibration distances are identical: through-origin fit used"
    b_ = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a_ = my - b_ * mx
    if b_ <= 0 or a_ < 0:
        a2, b2 = origin()
        return a2, b2, "through_origin", (f"least-squares fit unusable (intercept {a_:.0f}, slope {b_:.3f}): "
                                          "through-origin fit used")
    return a_, b_, "least_squares", None


def fit_model(fi, ports_db, ctype, warnings):
    pts_in = fi.get("calibration_points") or []
    mult = common.sv_value(fi.get("equipment_multiplier"))
    pts, xs, ys = [], [], []
    for p in pts_in:
        label = p.get("label", "?")
        if not (p.get("url") and p.get("as_of") and p.get("source") and common.sv_value(p.get("usd")) is not None):
            warnings.append(f"calibration point '{label}' lacks usd, source, url or as_of: ignored")
            continue
        ends = []
        for end in ("from", "to"):
            info = ports_db["by_locode"].get(p.get(f"{end}_locode"))
            if info:
                ends.append((info["lat"], info["lon"]))
            elif p.get(f"{end}_lat") is not None and p.get(f"{end}_lon") is not None:
                ends.append((p[f"{end}_lat"], p[f"{end}_lon"]))
            else:
                ends.append(None)
        if None in ends:
            warnings.append(f"calibration point '{label}': unknown LOCODE without coordinates: skipped")
            continue
        nm, why = sea_nm(ends[0][0], ends[0][1], ends[1][0], ends[1][1])
        if nm is None:
            warnings.append(f"calibration point '{label}': {why}: skipped")
            continue
        if nm <= 0:
            warnings.append(f"calibration point '{label}': zero distance: skipped")
            continue
        usd = float(p["usd"])
        applied = False
        if p.get("container_type") != ctype:
            if mult is None:
                warnings.append(f"calibration point '{label}' is {p.get('container_type')} but lanes are {ctype} and "
                                "equipment_multiplier is missing: not usable")
                continue
            usd *= mult
            applied = True
        pts.append({"label": label, "from_locode": p.get("from_locode"), "to_locode": p.get("to_locode"),
                    "distance_nm": round(nm, 1), "usd": float(p["usd"]), "container_type": p.get("container_type"),
                    "usd_lane_equipment": round(usd, 2), "multiplier_applied": applied, "as_of": p["as_of"],
                    "source": p.get("source"), "url": p.get("url")})
        xs.append(nm)
        ys.append(usd)
    model = {"container_type": ctype, "range_pct": (fi.get("model") or {}).get("range_pct"),
             "n_points": len(pts), "a": None, "b": None, "method": None, "points": pts}
    if pts:
        a_, b_, method, warn = fit_line(xs, ys)
        if warn:
            warnings.append(warn)
        model.update({"a": round(a_, 4), "b": round(b_, 6), "method": method})
    return model


def lane_rates(lane, ctype, mult, notes):
    """Split a lane's rates into usable {tier: [(usd, rate, note)]} and unusable list."""
    usable = {t: [] for t in RUNGS}
    other = []
    today = common.today()
    for r in lane.get("rates") or []:
        t = r.get("tier")
        usd = common.sv_value(r.get("usd"))
        if t not in RUNGS:
            notes.append(f"rate with unknown tier {t!r} ignored (use quote, connector or published)")
            continue
        if usd is None or usd <= 0:
            notes.append(f"{t} rate from '{r.get('source')}' has no valid usd: ignored")
            continue
        rt = r.get("container_type") or ctype
        applied = None
        if rt != ctype:
            if mult is None:
                other.append({"tier": t, "usd": usd, "container_type": rt, "note": "different equipment — not applied"})
                continue
            usd = usd * mult
            applied = f"{rt} rate converted to {ctype} with equipment_multiplier {mult:g}"
        vu = r.get("valid_until")
        if vu and is_date(vu) and vu < today:
            notes.append(f"{t} rate from '{r.get('source')}' expired on {vu} (today {today}); still used, re-confirm before relying on it")
        if applied:
            notes.append(f"{t} rate from '{r.get('source')}': {applied}")
        usable[t].append((usd, r))
    return usable, other


def spread(vals):
    return min(vals), statistics.median(vals), max(vals)


def combine_source(rs):
    if len(rs) == 1:
        r = rs[0]
        return r.get("source") or "", r.get("url"), r.get("as_of")
    src = f"median of {len(rs)} rates: " + "; ".join(str(r.get("source")) for r in rs)
    urls = [r.get("url") for r in rs if r.get("url")]
    dates = [r["as_of"] for r in rs if r.get("as_of")]
    return src, (urls[0] if urls else None), (max(dates) if dates else None)


def transit_formula(nm):
    return (nm / (18 * 24) + 1, nm / (15 * 24) + 3, nm / (13 * 24) + 7)


def build_lane(iso, lane, ctx):
    ports_db, ctype, mult, model, hp, perspective = ctx["ports"], ctx["ctype"], ctx["mult"], ctx["model"], ctx["home_port"], ctx["perspective"]
    notes = []
    pp = resolve_port(lane.get("partner_port"), ports_db)
    for p, who in ((hp, "home"), (pp, "partner")):
        if not p["locode"]:
            notes.append(f"{who} port not set: use set-port")
        elif p["lat"] is None:
            notes.append(f"{who} port {p['locode']} has no coordinates: distance unavailable")
    for n in (lane.get("port_note"), ctx.get("home_note")):
        if n:
            notes.append(n)
    origin, dest = (hp, pp) if perspective == "export" else (pp, hp)
    nm, why = sea_nm(origin["lat"], origin["lon"], dest["lat"], dest["lon"])
    if nm is None and why and why != "port coordinates unknown":
        notes.append(why)
    usable, other = lane_rates(lane, ctype, mult, notes)

    prices = {}                                   # tier -> (low, base, high, rates, usds)
    for t in RUNGS:
        if usable[t]:
            usds = [u for u, _ in usable[t]]
            lo, md, hi = spread(usds)
            prices[t] = (lo, md, hi, [r for _, r in usable[t]])
    # modelled
    mod = None
    if model["n_points"] and nm:
        base = model["a"] + model["b"] * nm
        rp = model.get("range_pct")
        if rp is None:
            rp = DEFAULT_RANGE_PCT
            notes.append(f"model.range_pct not set in freight_inputs.json: contract value {DEFAULT_RANGE_PCT}% used")
        if base > 0:
            mod = (base * (1 - rp / 100), base, base * (1 + rp / 100), rp)
        else:
            notes.append("modelled estimate is not positive: unavailable")
    elif not model["n_points"]:
        notes.append("no usable calibration point: the modelled rung is unavailable")
    elif not nm:
        notes.append("modelled rung unavailable: sea distance unknown")

    chosen_tier = next((t for t in RUNGS if t in prices), "modelled" if mod else None)
    alts = list(other)
    for t in RUNGS:
        if t in prices and t != chosen_tier:
            lo, md, hi, rs = prices[t]
            alts.append({"tier": t, "usd": round(md, 2), "low": round(lo, 2), "high": round(hi, 2),
                         "note": f"{len(rs)} rate(s); lower rung than the one used"})
    if mod and chosen_tier != "modelled":
        alts.append({"tier": "modelled", "usd": round(mod[1], 2), "low": round(mod[0], 2), "high": round(mod[2], 2),
                     "note": f"distance model ({model['method']}, {model['n_points']} point(s)); cross-check only"})

    out = {"partner": {"iso3": iso, "name": common.country_name(iso)},
           "origin_port": origin, "destination_port": dest,
           "distance_nm": None if nm is None else int(round(nm))}
    all_in = False
    covers = []
    chosen_rates = []
    if chosen_tier in RUNGS:
        lo, md, hi, chosen_rates = prices[chosen_tier]
        src, url, as_of = combine_source(chosen_rates)
        fusd = {"low": round(lo, 2), "base": round(md, 2), "high": round(hi, 2)}
        fsv = common.sv(round(md, 2), chosen_tier, src, url, as_of)
    elif chosen_tier == "modelled":
        lo, md, hi, rp = mod
        pts = model["points"]
        labels_ = "; ".join(f"{p['label']} ({p['as_of']})" for p in pts)
        src = (f"Distance model usd = a + b x nm ({model['method']}, a={model['a']}, b={model['b']}) fitted to "
               f"{len(pts)} benchmark(s): {labels_}; range +/-{rp}%")
        fsv = common.sv(round(md, 2), "modelled", src, None, max(p["as_of"] for p in pts))
        fusd = {"low": round(lo, 2), "base": round(md, 2), "high": round(hi, 2)}
        notes.append("modelled from distance and benchmarks: indicative only, never a quote")
    else:
        fsv = common.sv_missing("no rate collected and no calibration point: coverage gap")
        fusd = {"low": None, "base": None, "high": None}
        notes.append("coverage gap: no quote, connector or published rate, and no modelled estimate possible")
    out["freight_usd"] = fusd
    out["freight"] = fsv

    # transit
    tvals = [float(r["transit_days"]) for r in chosen_rates if common.sv_value(r.get("transit_days")) is not None] if chosen_rates else []
    if tvals:
        lo_, md_, hi_ = spread(tvals)
        out["transit_days"] = {"low": lo_, "base": md_, "high": hi_, "tier": chosen_tier}
    elif nm:
        lo_, md_, hi_ = transit_formula(nm)
        out["transit_days"] = {"low": round(lo_, 1), "base": round(md_, 1), "high": round(hi_, 1), "tier": "modelled"}
        if chosen_rates:
            notes.append("rate states no transit time: modelled from distance")
    else:
        out["transit_days"] = {"low": None, "base": None, "high": None, "tier": "missing"}

    # local charges / all-in
    lc = {}
    for k in common.CHARGE_KEYS:
        v = (lane.get("local_charges") or {}).get(k)
        lc[k] = v if isinstance(v, dict) and "tier" in v else common.sv_missing("not collected")
    if chosen_rates:
        flags = [bool(r.get("all_in")) for r in chosen_rates]
        if all(flags):
            sets = [set(r.get("covers") or []) for r in chosen_rates]
            covers = [k for k in common.CHARGE_KEYS if all(k in s for s in sets)]
            all_in = True
            if not covers:
                notes.append("rate is flagged all-in but lists no covered charges: no local charge was zeroed")
            for k in covers:
                lc[k] = {"value": 0, "tier": chosen_tier, "source": "included in all-in rate", "url": None,
                         "as_of": max((r.get("as_of") or "" for r in chosen_rates), default=None) or None}
            if covers:
                notes.append("all-in rate covers: " + ", ".join(covers))
        elif any(flags):
            notes.append("some chosen rates are all-in and some are not: no local charge was zeroed")
    out["all_in"] = all_in
    out["local_charges"] = lc
    out["alternatives"] = alts
    out["coverage_gap"] = chosen_tier is None
    out["notes"] = notes
    return out


def cmd_build(a):
    inputs = get_inputs(a.study_dir)
    partners = get_selection(a.study_dir)
    fi = get_fi(a.study_dir)
    ports_db = load_ports()
    ctype = fi.get("container_type")
    if ctype not in common.CONTAINER_TYPES:
        common.blocked("freight_inputs.json has no valid container_type: run init")
    if not fi.get("home_port"):
        common.blocked("freight_inputs.json has no home_port: run init or set-port --lane home")
    missing_lanes = [p for p in partners if p not in (fi.get("lanes") or {})]
    if missing_lanes:
        common.blocked(f"selected partners without a lane in freight_inputs.json: {', '.join(missing_lanes)}; run init")
    extra = [k for k in fi["lanes"] if k not in partners]
    perspective = inputs.get("perspective", "export")
    warnings = []
    for k in extra:
        warnings.append(f"lane {k} is in freight_inputs.json but not in selection.json: not built")
    mult = common.sv_value(fi.get("equipment_multiplier"))
    model = fit_model(fi, ports_db, ctype, warnings)
    if model["n_points"] == 0 and not fi.get("calibration_points"):
        warnings.append("no calibration points: the modelled rung is unavailable for every lane")
    try:
        import searoute  # noqa: F401
    except ImportError:
        warnings.append(NO_SEAROUTE_MSG)
    hp = resolve_port(fi.get("home_port"), ports_db)
    ctx = {"ports": ports_db, "ctype": ctype, "mult": mult, "model": model, "home_port": hp,
           "perspective": perspective, "home_note": fi.get("home_port_note")}
    lanes = {iso: build_lane(iso, fi["lanes"][iso], ctx) for iso in partners}
    out = {"schema_version": common.SCHEMA_VERSION, "container_type": ctype, "generated_at": common.now_iso(),
           "direction": common.labels(perspective)["direction"], "home": inputs.get("home_country"),
           "lanes": lanes, "model": model, "warnings": warnings}
    common.write_json(common.study_file(a.study_dir, "freight"), out)
    print(f"freight.json written ({ctype}, {out['direction']}, {len(lanes)} lanes)")
    print(f"{'lane':<6}{'rung':<11}{'low':>9}{'base':>9}{'high':>9}  {'transit d (l/b/h)':<19}missing charges")
    for iso, ln in lanes.items():
        rung = ln["freight"]["tier"] if not ln["coverage_gap"] else "GAP"
        f = ln["freight_usd"]
        t = ln["transit_days"]
        tt = "-" if t["base"] is None else f"{t['low']:g}/{t['base']:g}/{t['high']:g}"
        miss = [k for k in common.CHARGE_KEYS if not common.sv_ok(ln["local_charges"][k])]
        print(f"{iso:<6}{rung:<11}{fmt(f['low']):>9}{fmt(f['base']):>9}{fmt(f['high']):>9}  {tt:<19}{', '.join(miss) or '-'}")
    for w in warnings:
        print(f"WARNING: {w}")
    for iso, ln in lanes.items():
        for n in ln["notes"]:
            print(f"  [{iso}] {n}")
    return 0


# ---------------------------------------------------------------- CLI

def make_parser():
    ap = argparse.ArgumentParser(prog="estimate_freight.py", description=__doc__.split("\n\n")[0],
                                 epilog="Exit codes: 0 ok, 2 blocked (input missing), 1 error.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, study_required=True):
        p = sub.add_parser(name, help=help_, description=help_)
        p.add_argument("--study-dir", required=study_required, help="study folder (freight_inputs.json, freight.json live here)")
        p.set_defaults(fn=fn)
        return p

    p = add("init", cmd_init, "create/extend freight_inputs.json from inputs.json + selection.json (never overwrites lane data)")
    p.add_argument("--container", choices=common.CONTAINER_TYPES, help="container type if inputs.commercial.container.type is absent")
    p = add("set-port", cmd_set_port, "set the port of a lane or of the home country")
    p.add_argument("--lane", required=True, help="ISO3 of the partner, or 'home'")
    p.add_argument("--locode", required=True, help="UN/LOCODE, e.g. NLRTM")
    p.add_argument("--name"); p.add_argument("--lat", type=float); p.add_argument("--lon", type=float)
    p = add("add-rate", cmd_add_rate, "add a dated, cited freight rate (USD per container) to a lane")
    p.add_argument("--lane", required=True); p.add_argument("--tier", required=True, choices=RATE_TIERS)
    p.add_argument("--usd", required=True, type=float); p.add_argument("--container-type", choices=common.CONTAINER_TYPES)
    p.add_argument("--transit-days", type=float); p.add_argument("--carrier")
    p.add_argument("--source", required=True); p.add_argument("--url"); p.add_argument("--as-of")
    p.add_argument("--valid-until"); p.add_argument("--all-in", action="store_true", help="the rate includes local charges")
    p.add_argument("--covers", help="comma list of charge keys the all-in rate includes: " + ",".join(common.CHARGE_KEYS))
    p = add("set-charge", cmd_set_charge, "set one local charge on a lane (or all lanes)")
    p.add_argument("--lane", required=True, help="ISO3 or 'all'"); p.add_argument("--key", required=True, choices=common.CHARGE_KEYS)
    p.add_argument("--value", required=True, type=float); p.add_argument("--tier", required=True, choices=CHARGE_TIERS)
    p.add_argument("--source", required=True); p.add_argument("--url"); p.add_argument("--as-of")
    p = add("add-calibration", cmd_add_calibration, "add a dated, cited benchmark for the modelled rung")
    p.add_argument("--label", required=True); p.add_argument("--from-locode", required=True); p.add_argument("--to-locode", required=True)
    p.add_argument("--usd", required=True, type=float); p.add_argument("--container-type", required=True)
    p.add_argument("--source", required=True); p.add_argument("--url", required=True); p.add_argument("--as-of", required=True)
    for k in ("from-lat", "from-lon", "to-lat", "to-lon"):
        p.add_argument("--" + k, type=float, help="only for a LOCODE not in ports.json")
    p = add("set-multiplier", cmd_set_multiplier, "equipment multiplier: converts a rate/benchmark of another container type (e.g. dry to reefer)")
    p.add_argument("--value", required=True, type=float); p.add_argument("--tier", required=True, choices=MULT_TIERS)
    p.add_argument("--source", required=True); p.add_argument("--url"); p.add_argument("--as-of")
    p = add("set-range", cmd_set_range, "set the +/- percentage range of modelled prices")
    p.add_argument("--pct", required=True, type=float)
    p = add("freightos", cmd_freightos, "optional adapter: Freightos Rate Estimate API (needs FREIGHTOS_API_KEY; 10 requests/day)")
    p.add_argument("--lanes", help="comma list of ISO3 (default: all lanes)")
    p.add_argument("--force", action="store_true", help="exceed the daily budget / re-query lanes already done today")
    p = add("ports", cmd_ports, "list known ports for a country", study_required=False)
    p.add_argument("--country", required=True, help="ISO3, ISO2 or name")
    add("build", cmd_build, "write freight.json (ladder: quote > connector > published > modelled > gap)")
    return ap


def main(argv=None):
    args = make_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except SystemExit:
        raise
    except Exception as e:  # noqa
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

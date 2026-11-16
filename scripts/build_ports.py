#!/usr/bin/env python3
"""Build scripts/data/ports.json: default container gateway ports per country.

Selection is CURATED (CURATED / LANDLOCKED below; first LOCODE = default port).
Coordinates are never typed in: they are parsed from the UN/LOCODE code list
(github.com/datasets/un-locode, column "Coordinates", "DDMMN DDDMME"), falling back
to the port data bundled with `searoute`. Each port is validated: ISO2 prefix matches
the country and searoute can route it to SGSIN with a plausible length.
Every dropped or substituted code is printed in the report.

Re-run:  python3 scripts/build_ports.py --unlocode /path/to/code-list.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

# {ISO3: [LOCODE, ...]}  first = default. Large / multi-coast countries get several.
CURATED = {
    # Africa
    "EGY": ["EGALY", "EGDAM", "EGPSD", "EGSOK"], "MAR": ["MACAS", "MATNG"], "DZA": ["DZALG", "DZORN"],
    "TUN": ["TNRAD", "TNSFA"], "LBY": ["LYTIP", "LYMRA"], "SDN": ["SDPZU"], "KEN": ["KEMBA"],
    "TZA": ["TZDAR"], "ZAF": ["ZADUR", "ZACPT", "ZAPLZ"], "NGA": ["NGAPP", "NGLOS", "NGTIN"],
    "GHA": ["GHTEM", "GHTKD"], "CIV": ["CIABJ"], "SEN": ["SNDKR"], "CMR": ["CMDLA"], "AGO": ["AOLAD"],
    "MOZ": ["MZMPM", "MZBEW"], "MUS": ["MUPLU"], "MDG": ["MGTOA"], "DJI": ["DJJIB"], "NAM": ["NAWVB"],
    "GAB": ["GAOWE"], "COG": ["CGPNR"], "COD": ["CDMAT"], "TGO": ["TGLFW"], "BEN": ["BJCOO"],
    "MRT": ["MRNDB"], "GIN": ["GNCKY"], "SLE": ["SLFNA"], "LBR": ["LRMLW"], "GMB": ["GMBJL"],
    "SOM": ["SOMGQ"],
    # Europe
    "NLD": ["NLRTM"], "BEL": ["BEANR", "BEZEE"], "DEU": ["DEHAM", "DEBRV"], "FRA": ["FRLEH", "FRMRS"],
    "GBR": ["GBFXT", "GBSOU", "GBLGP"], "ESP": ["ESVLC", "ESALG", "ESBCN"], "PRT": ["PTLIS", "PTSIE"],
    "ITA": ["ITGOA", "ITSPE", "ITGIT"], "GRC": ["GRPIR"], "TUR": ["TRMER", "TRIZM", "TRIST"],
    "POL": ["PLGDN", "PLGDY"], "SWE": ["SEGOT"], "DNK": ["DKAAR"], "NOR": ["NOOSL"], "FIN": ["FIHEL"],
    "IRL": ["IEDUB"], "ROU": ["ROCND"], "BGR": ["BGVAR"], "HRV": ["HRRJK"], "SVN": ["SIKOP"],
    "EST": ["EETLL"], "LVA": ["LVRIX"], "LTU": ["LTKLJ"], "UKR": ["UAODS"], "RUS": ["RULED", "RUNVS"],
    "GEO": ["GEPTI", "GEBUS"], "CYP": ["CYLMS"], "MLT": ["MTMAR"], "ISL": ["ISREY"], "ALB": ["ALDRZ"],
    "MNE": ["MEBAR"],
    # Asia
    "CHN": ["CNSHA", "CNNGB", "CNSZX", "CNTAO"], "HKG": ["HKHKG"], "TWN": ["TWKHH"],
    "JPN": ["JPTYO", "JPYOK", "JPUKB"], "KOR": ["KRPUS"], "SGP": ["SGSIN"], "MYS": ["MYPKG", "MYTPP"],
    "THA": ["THLCH", "THBKK"], "VNM": ["VNCMT", "VNHPH"], "IDN": ["IDJKT", "IDSUB"], "PHL": ["PHMNL"],
    "IND": ["INNSA", "INMUN", "INMAA"], "PAK": ["PKKHI"], "BGD": ["BDCGP"], "LKA": ["LKCMB"],
    "MMR": ["MMRGN"], "KHM": ["KHKOS"], "SAU": ["SAJED", "SADMM"], "ARE": ["AEJEA", "AEKHL"],
    "OMN": ["OMSLL", "OMSOH"], "QAT": ["QADOH"], "KWT": ["KWSWK", "KWKWI"], "BHR": ["BHKBS"],
    "IRN": ["IRBND"], "IRQ": ["IQUQR"], "ISR": ["ILHFA", "ILASH"], "JOR": ["JOAQJ"], "LBN": ["LBBEY"],
    "SYR": ["SYLTK"], "YEM": ["YEADE"], "BRN": ["BNMUA"],
    # Americas
    "USA": ["USNYC", "USLAX", "USHOU", "USSAV"], "CAN": ["CAVAN", "CAMTR", "CAHAL"],
    "MEX": ["MXZLO", "MXVER", "MXLZC"], "GTM": ["GTSTC", "GTPRQ"], "PAN": ["PABLB", "PAMIT"],
    "COL": ["COCTG", "COBUN"], "VEN": ["VEPBL"], "ECU": ["ECGYE"], "PER": ["PECLL"],
    "CHL": ["CLVAP", "CLSAI"], "ARG": ["ARBUE"], "URY": ["UYMVD"], "BRA": ["BRSSZ", "BRPNG", "BRITJ"],
    "CRI": ["CRMOB"], "DOM": ["DOCAU"], "JAM": ["JMKIN"], "CUB": ["CUMAR"], "TTO": ["TTPOS"],
    "HND": ["HNPCR"], "SLV": ["SVAQJ"], "NIC": ["NICIO"], "BHS": ["BSFPO"], "GUY": ["GYGEO"],
    "SUR": ["SRPBM"],
    # Oceania
    "AUS": ["AUMEL", "AUSYD", "AUBNE", "AUFRE"], "NZL": ["NZAKL", "NZTRG"], "FJI": ["FJSUV"],
    "PNG": ["PGLAE"],
}

# Landlocked countries (no seaport) with their usual gateway ports.
LANDLOCKED = {
    "ETH": ["DJJIB"], "UGA": ["KEMBA", "TZDAR"], "RWA": ["TZDAR", "KEMBA"], "BDI": ["TZDAR"],
    "ZMB": ["TZDAR", "MZBEW"], "ZWE": ["MZBEW", "ZADUR"], "MWI": ["MZBEW", "TZDAR"], "BWA": ["ZADUR", "NAWVB"],
    "SSD": ["KEMBA"], "MLI": ["SNDKR", "CIABJ"], "BFA": ["CIABJ", "GHTEM"], "NER": ["BJCOO", "NGLOS"],
    "TCD": ["CMDLA"], "CAF": ["CMDLA"], "LSO": ["ZADUR"], "SWZ": ["ZADUR", "MZMPM"],
    "CHE": ["NLRTM", "ITGOA"], "AUT": ["DEHAM", "SIKOP"], "CZE": ["DEHAM", "DEBRV"], "SVK": ["DEHAM", "SIKOP"],
    "HUN": ["HRRJK", "DEHAM"], "SRB": ["HRRJK", "ROCND"], "BIH": ["HRPLE"], "MKD": ["GRPIR"],
    "BLR": ["LTKLJ", "PLGDN"], "MDA": ["ROCND", "UAODS"], "LUX": ["BEANR", "NLRTM"],
    "KAZ": ["CNLYG", "GEPTI"], "UZB": ["GEPTI", "IRBND"], "TKM": ["IRBND", "GEPTI"],
    "KGZ": ["CNLYG", "GEPTI"], "TJK": ["GEPTI", "IRBND"], "AFG": ["PKKHI", "IRBND"],
    "NPL": ["INCCU", "INVTZ"], "BTN": ["INCCU"], "LAO": ["THLCH", "VNHPH"], "MNG": ["CNTXG"],
    "ARM": ["GEPTI"], "AZE": ["GEPTI"], "PRY": ["ARBUE", "UYMVD"], "BOL": ["CLARI", "PECLL"],
}

# Alternative LOCODEs for the same port when the first has no coordinates anywhere.
ALTERNATES = {
    "UAODS": ["UAODE", "UAIEV"], "HNPCR": ["HNSAP"], "NICIO": ["NIMGA"],
    "SOMGQ": ["SOBBO"], "BNMUA": ["BNBWN"], "VEPBL": ["VELAG"], "CNTXG": ["CNTSN", "CNXGG"],
    "GHTEM": ["GHTEM"], "BHKBS": ["BHBAH"], "KRPUS": ["KRPUS"], "VNCMT": ["VNSGN", "VNVUT"],
    "TWKHH": ["TWTPE"], "JPYOK": ["JPTYO"], "JPUKB": ["JPOSA"],
}

# Display names only (coordinates are never overridden).
NAME_OVERRIDES = {
    "EGALY": "Alexandria", "EGDAM": "Damietta", "EGSOK": "Ain Sokhna", "DZALG": "Algiers", "RULED": "Saint Petersburg",
    "MYPKG": "Port Klang", "INMAA": "Chennai", "INCCU": "Kolkata", "AEKHL": "Khalifa Port (Abu Dhabi)",
    "IDJKT": "Jakarta", "BEANR": "Antwerp", "SEGOT": "Gothenburg", "ITGOA": "Genoa", "PTLIS": "Lisbon",
    "CAVAN": "Vancouver", "VNCMT": "Cai Mep", "GTSTC": "Santo Tomas de Castilla", "IQUQR": "Umm Qasr",
    "SADMM": "Dammam", "JOAQJ": "Aqaba", "BDCGP": "Chattogram", "KHKOS": "Sihanoukville", "GBLGP": "London Gateway",
    "CNTXG": "Tianjin", "ZAPLZ": "Gqeberha", "KWSWK": "Shuwaikh", "BHKBS": "Khalifa Bin Salman Port",
}

MAX_PLAUSIBLE_NM = 22000.0
HUB_CHECK = "SGSIN"


def parse_coord(s: str):
    """'3112N 02954E' -> (31.2, 29.9). None when absent/invalid."""
    try:
        la, lo = s.split()
        lat = int(la[:-1][:-2]) + int(la[:-1][-2:]) / 60.0
        lon = int(lo[:-1][:-2]) + int(lo[:-1][-2:]) / 60.0
        if la[-1] == "S":
            lat = -lat
        if lo[-1] == "W":
            lon = -lon
        return round(lat, 4), round(lon, 4)
    except Exception:
        return None


def load_unlocode(path):
    out = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            code = (r["Country"] + r["Location"]).strip()
            if not code or "Function" not in r:
                continue
            fn = r.get("Function") or ""
            out[code] = {"name": r.get("NameWoDiacritics") or r.get("Name"), "coord": parse_coord(r["Coordinates"]) if r.get("Coordinates") else None,
                         "port": fn[:1] == "1"}
    return out


def load_searoute_ports():
    try:
        import searoute
    except ImportError:
        return {}, None
    gj = json.loads((Path(searoute.__file__).parent / "data" / "ports.geojson").read_text(encoding="utf-8"))
    d = {}
    for f in gj["features"]:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"]
        d[p["port"]] = {"name": p.get("name"), "coord": (round(lat, 4), round(lon, 4))}
    return d, searoute


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build scripts/data/ports.json from UN/LOCODE + curated port selection.")
    ap.add_argument("--unlocode", required=True, help="path to UN/LOCODE code-list.csv (datasets/un-locode)")
    ap.add_argument("--out", default=str(common.DATA_DIR / "ports.json"), help="output path")
    a = ap.parse_args(argv)
    if not Path(a.unlocode).exists():
        common.blocked(f"UN/LOCODE file not found: {a.unlocode}")
    uno = load_unlocode(a.unlocode)
    sr_ports, sr = load_searoute_ports()
    if sr is None:
        common.blocked("searoute is required to validate ports: pip install searoute")
    hub = uno.get(HUB_CHECK, {}).get("coord")
    report = []

    def resolve(code, iso2):
        """-> (name, lat, lon, source) or None. Reasons appended to report."""
        if code[:2] != iso2:
            report.append(f"DROPPED {code}: prefix does not match country {iso2}")
            return None
        u = uno.get(code)
        coord, src, name = None, None, None
        if u and u["coord"]:
            coord, src, name = u["coord"], "unlocode", u["name"]
        elif code in sr_ports:
            coord, src, name = sr_ports[code]["coord"], "searoute", sr_ports[code]["name"] or (u or {}).get("name")
        if not coord:
            report.append(f"NO COORDINATES {code}")
            return None
        try:
            ln = sr.searoute([coord[1], coord[0]], [hub[1], hub[0]], units="naut")["properties"]["length"]
        except Exception as e:  # noqa
            report.append(f"DROPPED {code}: searoute cannot route to {HUB_CHECK} ({type(e).__name__})")
            return None
        if code != HUB_CHECK and not (0 < ln < MAX_PLAUSIBLE_NM):
            report.append(f"DROPPED {code}: implausible distance to {HUB_CHECK} ({ln:.0f} nm)")
            return None
        return NAME_OVERRIDES.get(code, name), coord[0], coord[1], src

    def resolve_with_alt(code, iso2):
        r = resolve(code, iso2)
        if r:
            return code, r
        for alt in ALTERNATES.get(code, []):
            if alt == code:
                continue
            r = resolve(alt, iso2)
            if r:
                report.append(f"SUBSTITUTED {code} -> {alt}")
                return alt, r
        return None, None

    countries, by_locode = {}, {}
    iso2_of = lambda iso3: (common.load_countries()["iso3"].get(iso3) or {}).get("iso2")  # noqa: E731

    def add_to_index(code, iso3, name, lat, lon, src):
        by_locode[code] = {"iso3": iso3, "name": name, "lat": lat, "lon": lon, "coord_source": src}

    for iso3, codes in CURATED.items():
        iso2 = iso2_of(iso3)
        if not iso2:
            report.append(f"SKIPPED country {iso3}: not in countries.json")
            continue
        ports = []
        for c in codes:
            code, r = resolve_with_alt(c, iso2)
            if not r:
                if not any(c in x for x in report if x.startswith("DROPPED") or x.startswith("NO COORD")):
                    report.append(f"DROPPED {c}")
                continue
            if any(p["locode"] == code for p in ports):
                continue
            name, lat, lon, src = r
            ports.append({"name": name, "locode": code, "lat": lat, "lon": lon, "default": not ports, "coord_source": src})
            add_to_index(code, iso3, name, lat, lon, src)
        if not ports:
            report.append(f"DROPPED country {iso3}: no valid port")
            continue
        countries[iso3] = {"ports": ports, "landlocked": False}

    for iso3, gws in LANDLOCKED.items():
        iso2 = iso2_of(iso3)
        if not iso2:
            report.append(f"SKIPPED country {iso3}: not in countries.json")
            continue
        good = []
        for g in gws:
            gi = g[:2]
            owner = next((k for k, v in ((k, iso2_of(k)) for k in common.load_countries()["iso3"]) if v == gi), None)
            code, r = resolve_with_alt(g, gi)
            if not r:
                continue
            if code not in by_locode:
                add_to_index(code, owner, r[0], r[1], r[2], r[3])
            good.append(code)
        if not good:
            report.append(f"DROPPED country {iso3}: no valid gateway")
            continue
        countries[iso3] = {"ports": [], "landlocked": True, "gateways": good}

    out = {"schema_version": common.SCHEMA_VERSION, "generated": common.today(),
           "source": "UN/LOCODE (github.com/datasets/un-locode) coordinates; curated port selection; searoute bundled ports as fallback",
           "countries": countries, "by_locode": by_locode}
    common.write_json(a.out, out)
    n_ports = sum(len(c["ports"]) for c in countries.values())
    print(f"Wrote {a.out}: {len(countries)} countries ({sum(1 for c in countries.values() if c['landlocked'])} landlocked), "
          f"{n_ports} ports, {len(by_locode)} LOCODEs indexed.")
    print("Dropped / substituted / fallback report:")
    fb = [c for c, v in by_locode.items() if v["coord_source"] != "unlocode"]
    for line in report:
        print("  " + line)
    for c in fb:
        print(f"  COORDS FROM searoute (no UN/LOCODE coordinates): {c}")
    if not report and not fb:
        print("  (none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

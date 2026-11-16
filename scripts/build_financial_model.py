#!/usr/bin/env python3
"""Stage 6 - financial model: financial.json and financial_model.xlsx (contracts section 8).

Reads inputs.json, selection.json, freight.json, tariff.json and (optional) analysis.json from the study
folder, models one lane per selected partner with model_core.py and writes:

  financial.json         every figure, scenario, sensitivity and the full inputs_echo (contracts 8.2)
  financial_model.xlsx   live workbook whose formulas reproduce model_core (contracts 8.3)

Exit codes: 0 ok, 2 blocked (trade-only study, missing file, input error), 1 error.
A selected partner absent from freight.json or tariff.json becomes an incomplete lane, never skipped.
Missing inputs stay missing: JSON null, blank Excel cell, no default is ever substituted.

Workbook cell map (also in the Cover sheet): Assumptions!B5:B8 are the live switches (defined names
Sw_freight, Sw_price_pct, Sw_cost_pct, Sw_fx_pct); every global input is a defined name A_<key>
(for example A_units_per_container); Lanes row 5.. holds one lane per row.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import model_core as mc  # noqa: E402

LANE_FIRST_ROW = 5
FMT = {"money": '#,##0;[Red]-#,##0', "unit": '#,##0.00##', "pct": '0.0"%"', "pctin": '0.0#"%"', "days": '0.0',
       "count": '#,##0.0', "rate": '#,##0.0000', "text": '@', "int": '0'}
GLOBAL_KEYS = [  # (key, kind-of-number)
    ("units_per_container", "count"), ("net_kg_per_unit", "unit"), ("fx_cost_per_model", "rate"), ("fx_price_per_model", "rate"),
    ("insurance_rate_pct", "pctin"), ("target_margin_pct", "pctin"), ("cost_per_unit", "unit"), ("local_price_per_unit", "unit"),
    ("credit_days", "days"), ("advance_pct", "pctin"), ("customer_credit_days", "days"), ("production_lead_days", "days"),
    ("origin_dwell_days", "days"), ("clearance_days", "days"), ("inventory_days", "days"),
    ("fixed_annual", "money"), ("fixed_one_time", "money"),
]
SWITCHES = [("Sw_freight", "Freight scenario (low / base / high)", "base", "text", "choice"),
            ("Sw_price_pct", "Price shock (selling price export / local price import)", 0, "pct", "%"),
            ("Sw_cost_pct", "Goods-cost shock (cost per unit export / purchase price import)", 0, "pct", "%"),
            ("Sw_fx_pct", "FX shock (cost currency export / price currency import; +10 = currency weakens 10 %)", 0, "pct", "%")]


# ------------------------------------------------------------------ rounding for JSON


def _round_tree(x):
    if isinstance(x, float):
        return round(x, 6)
    if isinstance(x, dict):
        return {k: _round_tree(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_round_tree(v) for v in x]
    return x


# ------------------------------------------------------------------ model


def run_model(study_dir, names=None):
    """Load the study files and return (financial_doc, lane_inputs_by_iso, inputs). Exits 2 when blocked."""
    d = Path(study_dir)
    inputs = common.read_json(common.study_file(d, "inputs"))
    selection = common.read_json(common.study_file(d, "selection"))
    freight = common.read_json(common.study_file(d, "freight"))
    tariff = common.read_json(common.study_file(d, "tariff"))
    analysis = common.read_json(common.study_file(d, "analysis"), required=False)
    if inputs.get("commercial") is None:
        common.blocked("inputs.commercial is null: this is a trade-only study, the financial model is skipped")
    persp = inputs.get("perspective")
    common.labels(persp)
    partners = list(selection.get("partners") or [])
    if not partners:
        common.blocked("selection.json lists no partners")
    f_lanes = freight.get("lanes") or {}
    t_lanes = tariff.get("lanes") or {}
    a_rows = {r.get("iso3"): r for r in ((analysis or {}).get("partners") or [])}
    names = names or {}
    li_by, lanes = {}, {}
    for iso in partners:
        li = mc.build_lane_inputs(inputs, f_lanes.get(iso), t_lanes.get(iso), a_rows.get(iso), iso)
        if f_lanes.get(iso) is None:
            li["_notes"].append("lane absent from freight.json: freight and local charges are missing")
        if t_lanes.get(iso) is None:
            li["_notes"].append("lane absent from tariff.json: duty and VAT are missing")
        errs = mc.input_errors(li)
        if errs:
            common.blocked(f"{iso}: " + "; ".join(errs))
        if freight.get("container_type") and li.get("container_type") and freight["container_type"] != li["container_type"]:
            li["_notes"].append(f"freight.json container type {freight['container_type']} differs from inputs {li['container_type']}")
        li_by[iso] = li
        res = mc.lane_summary(li)
        res["iso3"], res["name"] = iso, names.get(iso) or common.country_name(iso)
        lanes[iso] = res
    try:
        port = mc.portfolio(lanes, inputs)
    except ValueError as e:
        common.blocked(str(e))
    li0 = li_by[partners[0]]
    c = inputs["commercial"]
    doc = {"schema_version": common.SCHEMA_VERSION, "perspective": persp, "currency": li0["model_currency"],
           "container_type": li0["container_type"], "units_per_container": li0["units_per_container"], "unit": li0["unit"],
           "incoterm": li0["incoterm"], "incoterm_input": li0["incoterm_raw"], "cost_basis": li0["cost_basis"],
           "study_id": inputs.get("study_id"), "home_country": inputs.get("home_country"), "generated_at": common.now_iso(),
           "lanes": lanes, "portfolio": port, "inputs_echo": mc.echo_rows(li_by),
           "payment_terms": (c.get("payment") or {}).get("terms")}
    return _round_tree(doc), li_by, inputs


# ------------------------------------------------------------------ excel


def build_workbook(path, doc, li_by, inputs, names=None):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter as L
    from openpyxl.workbook.defined_name import DefinedName
    from openpyxl.worksheet.datavalidation import DataValidation

    names = names or {}
    persp = doc["perspective"]
    export = persp == "export"
    T = doc["incoterm"]
    basis = doc["cost_basis"]
    lanes = doc["lanes"]
    isos = list(lanes.keys())
    n = len(isos)
    li0 = li_by[isos[0]]
    model, unit = doc["currency"], doc["unit"]
    FILL_IN = PatternFill("solid", fgColor="FFF2CC")
    FILL_F = PatternFill("solid", fgColor="DDEBF7")
    FILL_R = PatternFill("solid", fgColor="C6E0B4")
    FILL_H = PatternFill("solid", fgColor="1F3864")
    FILL_V = PatternFill("solid", fgColor="F8CBAD")
    FILL_SW = PatternFill("solid", fgColor="FFD966")
    BOLD = Font(bold=True)
    WHITE = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="BFBFBF")
    BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)
    WRAP = Alignment(wrap_text=True, vertical="top")

    wb = Workbook()
    ws_cover = wb.active
    ws_cover.title = "Cover"
    ws_a = wb.create_sheet("Assumptions")
    ws_l = wb.create_sheet("Lanes")
    ws_d = wb.create_sheet("Lane detail")
    ws_s = wb.create_sheet("Scenarios")
    ws_p = wb.create_sheet("Plan & payback")
    ws_w = wb.create_sheet("Working capital")
    ws_v = wb.create_sheet("Provenance")
    ws_lim = wb.create_sheet("Limitations")
    ws_c = wb.create_sheet("_check")
    ws_c.sheet_state = "hidden"

    def title(ws, text, sub=None):
        ws["A1"] = text
        ws["A1"].font = Font(bold=True, size=14)
        if sub:
            ws["A2"] = sub
            ws["A2"].font = Font(italic=True, color="595959")

    def hdr(ws, row, values, col0=1):
        for i, v in enumerate(values):
            c = ws.cell(row=row, column=col0 + i, value=v)
            c.font, c.fill, c.border = WHITE, FILL_H, BORDER
            c.alignment = Alignment(wrap_text=True, vertical="center")

    def put(ws, ref, value, fill=None, fmt=None, bold=False):
        c = ws[ref]
        c.value = value
        if fill is not None:
            c.fill = fill
            c.border = BORDER
        if fmt:
            c.number_format = fmt
        if bold:
            c.font = BOLD
        return c

    def define(name, ref):
        wb.defined_names[name] = DefinedName(name, attr_text=ref)

    # ---------------------------------------------------------------- Assumptions
    title(ws_a, "Assumptions", "Yellow cells are inputs; blank = missing (nothing is defaulted). Every formula in the workbook reads these cells by defined name.")
    hdr(ws_a, 4, ["Item", "Value", "Unit", "Tier", "Source", "As of"])
    r = 5
    for name, label, default, fmtk, unitx in SWITCHES:
        put(ws_a, f"A{r}", label, bold=True)
        put(ws_a, f"B{r}", default, FILL_SW, FMT["pct"] if fmtk == "pct" else None)
        ws_a[f"C{r}"] = unitx
        ws_a[f"D{r}"] = "switch"
        ws_a[f"E{r}"] = "live scenario switch; defaults base / 0 / 0 / 0"
        define(name, f"Assumptions!$B${r}")
        r += 1
    dv = DataValidation(type="list", formula1='"low,base,high"', allow_blank=False)
    ws_a.add_data_validation(dv)
    dv.add("B5")
    r += 1
    hdr(ws_a, r, ["Global input", "Value", "Unit", "Tier", "Source", "As of"])
    r += 1
    for key, kind in GLOBAL_KEYS:
        s = li0["_sv"].get(key)
        applies = s is not None
        label = mc.LABELS.get(key, key)
        put(ws_a, f"A{r}", label)
        put(ws_a, f"B{r}", s["value"] if applies else None, FILL_IN, FMT[kind])
        ws_a[f"C{r}"] = (s.get("unit") if applies else "") or ""
        ws_a[f"D{r}"] = s["tier"] if applies else "n/a for this perspective"
        ws_a[f"E{r}"] = (s.get("source") if applies else "") or ""
        ws_a[f"F{r}"] = (s.get("as_of") if applies else "") or ""
        define("A_" + key, f"Assumptions!$B${r}")
        r += 1
    cap = ((inputs.get("commercial") or {}).get("volume") or {}).get("capacity_units_per_year")
    cs = mc._sv_clean(cap, f"{unit}/yr")
    put(ws_a, f"A{r}", "Capacity (information only, not used in formulas)")
    put(ws_a, f"B{r}", cs["value"], FILL_IN, FMT["count"])
    ws_a[f"C{r}"], ws_a[f"D{r}"], ws_a[f"E{r}"], ws_a[f"F{r}"] = cs["unit"], cs["tier"], cs["source"], cs["as_of"] or ""
    r += 2
    hdr(ws_a, r, ["Study setting (fixed at build time)", "Value", "", "", "", ""])
    r += 1
    for label, val in [("Perspective", persp), ("Home country", common.country_name(doc.get("home_country") or "")),
                       ("Incoterm modelled (FCA treated as FOB)", T), ("Cost basis (export)", basis or "n/a"),
                       ("Model currency", model), ("Cost currency", li0["cost_currency"]), ("Price currency", li0["price_currency"]),
                       ("Container type", doc["container_type"]), ("Unit", unit)]:
        ws_a[f"A{r}"], ws_a[f"B{r}"] = label, val
        r += 1
    ws_a.column_dimensions["A"].width = 58
    for col, w in zip("BCDEF", (16, 20, 14, 46, 12)):
        ws_a.column_dimensions[col].width = w
    ws_a.freeze_panes = "B5"

    G = {k: "A_" + k for k, _ in GLOBAL_KEYS}
    SWF, SWP, SWC, SWX = "Sw_freight", "Sw_price_pct", "Sw_cost_pct", "Sw_fx_pct"
    RATE = f"{G['insurance_rate_pct']}/100"

    # ---------------------------------------------------------------- Lanes: column spec
    def guard(refs, expr, extra=None):
        conds = [f"ISNUMBER({x})" for x in refs] + (extra or [])
        return f'IF(AND({",".join(conds)}),{expr},"")'

    cols = []

    def add(key, header, kind, fmt, width=13, f=None):
        cols.append({"key": key, "header": header, "kind": kind, "fmt": fmt, "width": width, "f": f})

    price_hdr = (f"Selling price ({li0['price_currency']}/{unit})" if export else f"Purchase price ({li0['cost_currency']}/{unit})")
    add("name", "Partner", "text", "text", 24)
    add("iso", "ISO3", "text", "text", 7)
    add("status", "Status (build time)", "text", "text", 34)
    add("price", price_hdr, "input", "unit")
    add("plan", "Planned containers / yr", "input", "count")
    add("transit", "Transit days (base)", "input", "days")
    add("fr_low", f"Freight low ({model}/cont.)", "input", "money")
    add("fr_base", f"Freight base ({model}/cont.)", "input", "money")
    add("fr_high", f"Freight high ({model}/cont.)", "input", "money")
    for k, lab in (("OI", "Origin inland"), ("OD", "Origin docs"), ("OT", "Origin THC"), ("DT", "Destination THC"),
                   ("IC", "Import clearance"), ("DI", "Destination inland")):
        add(k, f"{lab} ({model}/cont.)", "input", "money")
    add("duty", "Duty rate (%)", "input", "pctin")
    add("other", "Other duties (%)", "input", "pctin")
    add("valb", "Valuation basis", "input", "text", 10)
    add("vat", "Import VAT (%)", "input", "pctin")
    add("vatrec", "VAT recoverable", "input", "text", 10)

    def c_(R, k):
        return R(k)

    def FRf(R):
        lo, ba, hi = R("fr_low"), R("fr_base"), R("fr_high")
        return (f'IF({SWF}="low",IF(ISNUMBER({lo}),{lo},""),IF({SWF}="high",IF(ISNUMBER({hi}),{hi},""),IF(ISNUMBER({ba}),{ba},"")))')

    add("FR", f"Freight used ({model})", "formula", "money", 13, FRf)

    if export:
        add("goods", f"Goods ({model})", "formula", "money", 13,
            lambda R: guard([G["cost_per_unit"], G["units_per_container"], G["fx_cost_per_model"]],
                            f"{G['cost_per_unit']}*(1+{SWC}/100)*{G['units_per_container']}/({G['fx_cost_per_model']}*(1+{SWX}/100))"))
        if basis == "EXW":
            add("FOBc", f"FOB cost ({model})", "formula", "money", 13,
                lambda R: guard([R("goods"), R("OI"), R("OD"), R("OT")], f"{R('goods')}+{R('OI')}+{R('OD')}+{R('OT')}"))
        else:
            add("FOBc", f"FOB cost ({model})", "formula", "money", 13, lambda R: guard([R("goods")], R("goods")))
        if T in mc.SELLER_INSURANCE:
            add("INSs", f"Insurance, seller ({model})", "formula", "money", 13,
                lambda R: guard([R("FOBc"), R("FR"), G["insurance_rate_pct"]], f"{RATE}*({R('FOBc')}+{R('FR')})"))
        else:
            add("INSs", f"Insurance, seller ({model})", "formula", "money", 13, lambda R: "0")

        def seller_f(R):
            if T == "EXW":
                return guard([R("goods")], R("goods"))
            if T == "FOB":
                return guard([R("FOBc")], R("FOBc"))
            if T == "CFR":
                return guard([R("FOBc"), R("FR")], f"{R('FOBc')}+{R('FR')}")
            if T == "CIF":
                return guard([R("FOBc"), R("FR"), R("INSs")], f"{R('FOBc')}+{R('FR')}+{R('INSs')}")
            return guard([R("FOBc"), R("FR"), R("INSs"), R("DT"), R("DI")], f"{R('FOBc')}+{R('FR')}+{R('INSs')}+{R('DT')}+{R('DI')}")
        add("seller", f"Seller cost ({model})", "formula", "money", 13, seller_f)
        add("req", f"Required price ({li0['price_currency']}/{unit})", "formula", "unit", 13,
            lambda R: guard([R("seller"), G["target_margin_pct"], G["fx_price_per_model"], G["units_per_container"]],
                            f"{R('seller')}/(1-{G['target_margin_pct']}/100)/{G['units_per_container']}*{G['fx_price_per_model']}",
                            [f"{G['target_margin_pct']}<100"]))
        add("pused", f"Price used ({li0['price_currency']}/{unit})", "formula", "unit", 13,
            lambda R: f'IF(ISNUMBER({R("price")}),{R("price")},IF(ISNUMBER({R("req")}),{R("req")},""))')
        add("V", f"Invoice value ({model})", "formula", "money", 13,
            lambda R: guard([R("pused"), G["units_per_container"], G["fx_price_per_model"]],
                            f"{R('pused')}*(1+{SWP}/100)*{G['units_per_container']}/{G['fx_price_per_model']}"))
    else:
        add("V", f"Invoice value ({model})", "formula", "money", 13,
            lambda R: guard([R("price"), G["units_per_container"], G["fx_cost_per_model"]],
                            f"{R('price')}*(1+{SWC}/100)*{G['units_per_container']}/{G['fx_cost_per_model']}"))

    def fobv_f(R):
        V, OI, OD, OT, FR, DT, DI = (R(k) for k in ("V", "OI", "OD", "OT", "FR", "DT", "DI"))
        ins = G["insurance_rate_pct"]
        if T == "EXW":
            return guard([V, OI, OD, OT], f"{V}+{OI}+{OD}+{OT}")
        if T == "FOB":
            return guard([V], V)
        if T == "CFR":
            return guard([V, FR], f"{V}-{FR}")
        if T == "CIF":
            return guard([V, ins, FR], f"{V}/(1+{RATE})-{FR}")
        return guard([V, DT, DI, ins, FR], f"({V}-{DT}-{DI})/(1+{RATE})-{FR}")

    def cifv_f(R):
        V, FR, DT, DI = (R(k) for k in ("V", "FR", "DT", "DI"))
        ins = G["insurance_rate_pct"]
        if T == "EXW":
            return guard([R("FOBv"), FR, ins], f"({R('FOBv')}+{FR})*(1+{RATE})")
        if T == "FOB":
            return guard([V, FR, ins], f"({V}+{FR})*(1+{RATE})")
        if T == "CFR":
            return guard([V, ins], f"{V}*(1+{RATE})")
        if T == "CIF":
            return guard([V], V)
        return guard([V, DT, DI], f"{V}-{DT}-{DI}")

    add("FOBv", f"FOB value ({model})", "formula", "money", 13, fobv_f)
    add("CIFv", f"CIF value ({model})", "formula", "money", 13, cifv_f)
    add("CV", f"Customs value ({model})", "formula", "money", 13,
        lambda R: (f'IF({R("valb")}="CIF",IF(ISNUMBER({R("CIFv")}),{R("CIFv")},""),'
                   f'IF({R("valb")}="FOB",IF(ISNUMBER({R("FOBv")}),{R("FOBv")},""),""))'))
    add("DU", f"Duty ({model})", "formula", "money", 13,
        lambda R: guard([R("CV"), R("duty"), R("other")], f"(MAX(0,{R('duty')})+{R('other')})/100*{R('CV')}"))
    add("VATnr", f"Non-recoverable VAT ({model})", "formula", "money", 13,
        lambda R: (f'IF(AND(ISNUMBER({R("CIFv")}),ISNUMBER({R("DU")})),IF({R("vatrec")}="Yes",0,'
                   f'IF(AND({R("vatrec")}="No",ISNUMBER({R("vat")})),{R("vat")}/100*({R("CIFv")}+{R("DU")}),"")),"")'))
    add("landed", f"Buyer landed cost ({model})", "result", "money", 14,
        lambda R: guard([R("CIFv"), R("DT"), R("IC"), R("DU"), R("DI"), R("VATnr")],
                        f"{R('CIFv')}+{R('DT')}+{R('IC')}+{R('DU')}+{R('DI')}+{R('VATnr')}"))
    add("landed_pu", f"Landed per unit ({model}/{unit})", "formula", "unit", 13,
        lambda R: guard([R("landed"), G["units_per_container"]], f"{R('landed')}/{G['units_per_container']}"))
    if export:
        add("revenue", f"Revenue ({model})", "result", "money", 13, lambda R: guard([R("V"), R("seller")], R("V")))
        add("user_cost", f"Seller cost ({model})", "result", "money", 13, lambda R: guard([R("V"), R("seller")], R("seller")))
    else:
        add("revenue", f"Revenue ({model})", "result", "money", 13,
            lambda R: guard([R("landed"), G["local_price_per_unit"], G["units_per_container"], G["fx_price_per_model"]],
                            f"{G['local_price_per_unit']}*(1+{SWP}/100)*{G['units_per_container']}/({G['fx_price_per_model']}*(1+{SWX}/100))"))
        add("user_cost", f"Landed cost ({model})", "result", "money", 13, lambda R: guard([R("landed")], R("landed")))
    add("margin", f"Margin / container ({model})", "result", "money", 14, lambda R: guard([R("revenue"), R("user_cost")], f"{R('revenue')}-{R('user_cost')}"))
    add("margin_pct", "Margin (% of revenue)", "result", "pct", 11,
        lambda R: f'IF(ISNUMBER({R("margin")}),IF({R("revenue")}>0,{R("margin")}/{R("revenue")}*100,""),"")')
    if not export:
        add("be_price", f"Break-even local price ({li0['price_currency']}/{unit})", "result", "unit", 14,
            lambda R: guard([R("landed"), G["units_per_container"], G["fx_price_per_model"]],
                            f"{R('landed')}/{G['units_per_container']}*{G['fx_price_per_model']}*(1+{SWX}/100)"))
        add("req_price", f"Required local price ({li0['price_currency']}/{unit})", "result", "unit", 14,
            lambda R: guard([R("landed"), G["target_margin_pct"], G["units_per_container"], G["fx_price_per_model"]],
                            f"{R('landed')}/(1-{G['target_margin_pct']}/100)/{G['units_per_container']}*{G['fx_price_per_model']}*(1+{SWX}/100)",
                            [f"{G['target_margin_pct']}<100"]))
    add("be", "Break-even containers / yr", "result", "count", 13,
        lambda R: (f'IF(ISNUMBER({R("margin")}),IF({R("margin")}>0,IF(ISNUMBER({G["fixed_annual"]}),{G["fixed_annual"]}/{R("margin")},""),""),"")'))

    # waterfall amount columns (Lane detail source)
    wf_template = lanes[isos[0]]["waterfall"]
    insb = G["insurance_rate_pct"]

    def wf_formula(key):
        def f(R):
            V, OI, OD, OT, FR, DT, DI = (R(k) for k in ("V", "OI", "OD", "OT", "FR", "DT", "DI"))
            if key == "goods":
                return R("goods") if export else R("V")
            if key in ("OI", "OD", "OT"):
                return guard([R(key)], R(key)) if (T == "EXW" or (export and basis == "EXW")) else "0"
            if key == "FR":
                if export:
                    return R("FR")
                return R("FR") if T in ("EXW", "FOB") else "0"
            if key == "INS":
                if export and T in mc.SELLER_INSURANCE:
                    return R("INSs")
                if (not export) and T in mc.SELLER_INSURANCE:
                    return "0"
                if T == "EXW":
                    return guard([V, OI, OD, OT, FR, insb], f"{RATE}*({V}+{OI}+{OD}+{OT}+{FR})")
                if T == "FOB":
                    return guard([V, FR, insb], f"{RATE}*({V}+{FR})")
                return guard([V, insb], f"{RATE}*{V}")
            if key in ("DT", "DI"):
                if (not export) and T == "DAP":
                    return "0"
                return guard([R(key)], R(key))
            if key == "IC":
                return guard([R("IC")], R("IC"))
            if key == "DU":
                return R("DU")
            return R("VATnr")
        return f
    for w in wf_template:
        add("wf_" + w["key"], f"WF {w['key']} ({model})", "formula", "money", 11, wf_formula(w["key"]))
    for w in wf_template:
        add("tier_" + w["key"], f"Tier {w['key']}", "meta", "text", 10)

    C = {col["key"]: L(i + 1) for i, col in enumerate(cols)}
    last = LANE_FIRST_ROW + n - 1
    ws_l.sheet_properties.tabColor = "1F3864"
    title(ws_l, f"Lanes ({persp}, incoterm {T}" + (f", cost basis {basis}" if export else "") + ")",
          "One row per partner. Yellow = input (blank = missing), blue = live formula, green = key result. Status text is fixed at build time; formulas react live.")
    ws_l["A3"] = "INPUTS"
    ws_l["A3"].font = BOLD
    hdr(ws_l, 4, [c["header"] for c in cols])
    ws_l.row_dimensions[4].height = 48
    for i, iso in enumerate(isos):
        row = LANE_FIRST_ROW + i
        lane, li = lanes[iso], li_by[iso]

        def R(k, _row=row):
            return f"{C[k]}{_row}"
        st = lane["status"]
        if lane["missing"]:
            status = "MISSING: " + ", ".join(lane["missing"])
        elif st == "modelled":
            status = "modelled" + (" (required price at target margin)" if export and lane["price_basis"] == "required_at_target_margin"
                                   else " (no local price: break-even only)" if (not export and lane["price_basis"] != "user") else "")
            if lane["buyer_side_missing"]:
                status += "; buyer-side gaps: " + ", ".join(lane["buyer_side_missing"])
        else:
            status = st
        vals = {"name": names.get(iso) or lane["name"], "iso": iso, "status": status,
                "price": li["price_per_unit"] if export else li["purchase_price_per_unit"], "plan": li["planned_containers_per_year"],
                "transit": li["transit_days_base"], "fr_low": li["freight_low"], "fr_base": li["freight_base"], "fr_high": li["freight_high"],
                "OI": li["origin_inland"], "OD": li["origin_docs"], "OT": li["origin_thc"], "DT": li["destination_thc"],
                "IC": li["import_clearance"], "DI": li["destination_inland"], "duty": li["duty_rate_pct"], "other": li["other_duties_pct"],
                "valb": li["valuation_basis"], "vat": li["import_vat_pct"],
                "vatrec": None if li["vat_recoverable"] is None else ("Yes" if li["vat_recoverable"] else "No")}
        for w in lane["waterfall"]:
            vals["tier_" + w["key"]] = w["tier"]
        for col in cols:
            cell = ws_l[f"{C[col['key']]}{row}"]
            k = col["key"]
            if col["kind"] == "input":
                cell.value = vals.get(k)
                cell.fill, cell.border = FILL_IN, BORDER
            elif col["kind"] in ("formula", "result"):
                cell.value = "=" + col["f"](R)
                cell.fill, cell.border = (FILL_F if col["kind"] == "formula" else FILL_R), BORDER
            else:
                cell.value = vals.get(k)
            cell.number_format = FMT[col["fmt"]]
    for col in cols:
        ws_l.column_dimensions[C[col["key"]]].width = col["width"]
    ws_l.freeze_panes = f"D{LANE_FIRST_ROW}"
    dvv = DataValidation(type="list", formula1='"CIF,FOB"', allow_blank=True)
    dvr = DataValidation(type="list", formula1='"Yes,No"', allow_blank=True)
    ws_l.add_data_validation(dvv)
    ws_l.add_data_validation(dvr)
    dvv.add(f"{C['valb']}{LANE_FIRST_ROW}:{C['valb']}{last}")
    dvr.add(f"{C['vatrec']}{LANE_FIRST_ROW}:{C['vatrec']}{last}")
    rng = lambda k: f"Lanes!${C[k]}${LANE_FIRST_ROW}:${C[k]}${last}"  # noqa: E731

    # ---------------------------------------------------------------- Lane detail
    title(ws_d, "Lane detail", "Pick a partner in B3; the table reads the live Lanes row through INDEX/MATCH.")
    ws_d["A3"] = "Partner"
    ws_d["A3"].font = BOLD
    put(ws_d, "B3", names.get(isos[0]) or lanes[isos[0]]["name"], FILL_IN)
    dvl = DataValidation(type="list", formula1=f"=Lanes!$A${LANE_FIRST_ROW}:$A${last}", allow_blank=False)
    ws_d.add_data_validation(dvl)
    dvl.add("B3")
    ws_d["A4"] = "Row index"
    put(ws_d, "B4", f'=IFERROR(MATCH($B$3,Lanes!$A${LANE_FIRST_ROW}:$A${last},0),"")', FILL_F)
    hdr(ws_d, 6, ["Item", f"Amount ({model}/container)", "Paid by", "Tier"])
    for i, w in enumerate(wf_template):
        rr = 7 + i
        ws_d[f"A{rr}"] = w["label"]
        put(ws_d, f"B{rr}", f'=IF(ISNUMBER($B$4),INDEX({rng("wf_" + w["key"])},$B$4),"")', FILL_F, FMT["money"])
        ws_d[f"C{rr}"] = w["side"] if w["side"] else ""
        put(ws_d, f"D{rr}", f'=IF(ISNUMBER($B$4),INDEX({rng("tier_" + w["key"])},$B$4),"")', FILL_F)
    rr = 7 + len(wf_template) + 1
    summ = [("Invoice value", "V", "money"), ("Revenue", "revenue", "money"),
            ("Seller cost" if export else "Landed cost", "user_cost", "money"), ("Margin per container", "margin", "money"),
            ("Margin (% of revenue)", "margin_pct", "pct"), ("Buyer landed cost", "landed", "money"),
            ("Landed per unit", "landed_pu", "unit")]
    for lab, k, fk in summ:
        ws_d[f"A{rr}"] = lab
        ws_d[f"A{rr}"].font = BOLD
        put(ws_d, f"B{rr}", f'=IF(ISNUMBER($B$4),INDEX({rng(k)},$B$4),"")', FILL_R, FMT[fk])
        rr += 1
    ws_d[f"A{rr+1}"] = ("Export: seller-side rows sum to the seller cost; invoice value + buyer-side rows = buyer landed cost. "
                        "Import: invoice + buyer-side rows = landed cost; rows marked 'included in invoice' show 0. Blank = missing input.") if export else \
        ("Import: invoice + buyer-side rows = landed cost; rows marked 'included in invoice' show 0. Blank = missing input.")
    ws_d.column_dimensions["A"].width = 46
    for col in "BCD":
        ws_d.column_dimensions[col].width = 20
    ws_d.freeze_panes = "A7"

    # ---------------------------------------------------------------- Scenarios
    title(ws_s, "Scenarios and sensitivity",
          "Live switches are on Assumptions!B5:B8 (freight scenario, price %, goods-cost %, FX %): change them and Lanes, Plan and Working capital recalculate. "
          "The grids below are computed at build time from the base inputs and do not move with the switches.")
    ws_s["A4"] = "Switch"
    ws_s["B4"] = "Current value"
    ws_s["A4"].font = ws_s["B4"].font = BOLD
    for j, (nm, lab, *_rest) in enumerate(SWITCHES):
        ws_s[f"A{5+j}"] = lab
        put(ws_s, f"B{5+j}", f"={nm}", FILL_F)
    rr = 11
    for iso in isos:
        lane = lanes[iso]
        ws_s[f"A{rr}"] = f"{names.get(iso) or lane['name']} ({iso}) - status: {lane['status']}"
        ws_s[f"A{rr}"].font = Font(bold=True, size=12)
        rr += 1
        sens = lane["sensitivity"]
        ws_s[f"A{rr}"] = f"Margin per container ({model}): rows = price shock %, columns = goods-cost shock % (computed at build time from the base inputs)"
        rr += 1
        hdr(ws_s, rr, ["price \\ cost"] + [f"{x:+d}%" for x in sens["cost_pct"]])
        rr += 1
        for pi, p in enumerate(sens["price_pct"]):
            ws_s[f"A{rr}"] = f"{p:+d}%"
            for cj, _ in enumerate(sens["cost_pct"]):
                put(ws_s, f"{L(2+cj)}{rr}", sens["margin"][pi][cj], FILL_F, FMT["money"])
            rr += 1
        rr += 1
        hdr(ws_s, rr, ["Freight scenario", "Margin", "User cost", "Buyer landed"])
        rr += 1
        for scn in mc.FREIGHT_SCENARIOS:
            fs_ = lane["freight_scenarios"][scn]
            ws_s[f"A{rr}"] = scn
            for cj, k in enumerate(("margin", "user_cost", "buyer_landed")):
                put(ws_s, f"{L(2+cj)}{rr}", fs_[k], FILL_F, FMT["money"])
            rr += 1
        rr += 1
        hdr(ws_s, rr, [("FX shock on " + ("cost" if export else "price") + " currency"), "Margin"])
        rr += 1
        for e in lane["fx_sensitivity"]:
            ws_s[f"A{rr}"] = f"{e['shock_pct']:+d}%"
            put(ws_s, f"B{rr}", e["margin"], FILL_F, FMT["money"])
            rr += 1
        rr += 1
        hdr(ws_s, rr, ["Duty shock (pp)", "Buyer landed cost" if export else "Margin"])
        rr += 1
        for e in lane["duty_sensitivity"]:
            ws_s[f"A{rr}"] = f"{e['shock_pp']:+g} pp"
            put(ws_s, f"B{rr}", e["value"], FILL_F, FMT["money"])
            rr += 1
        rr += 2
    ws_s.column_dimensions["A"].width = 60
    for j in range(2, 9):
        ws_s.column_dimensions[L(j)].width = 15

    # ---------------------------------------------------------------- Working capital
    title(ws_w, "Working capital",
          ("Cash cycle (days) = (production lead + origin dwell + credit days) x (1 - advance)" if export else
           "Cash cycle (days) = transit + clearance + inventory + customer credit - credit days x (1 - advance) + production lead x advance")
          + "; peak working capital = planned containers x cycle / 365 x cost per container.")
    if export:
        wc_cols = [("Partner", None, "text"), ("Production lead (days)", "production_lead_days", "days"),
                   ("Origin dwell (days)", "origin_dwell_days", "days"), ("Credit days", "credit_days", "days"),
                   ("Advance (%)", "advance_pct", "pctin")]
    else:
        wc_cols = [("Partner", None, "text"), ("Transit base (days)", "LANE:transit", "days"), ("Clearance (days)", "clearance_days", "days"),
                   ("Inventory (days)", "inventory_days", "days"), ("Customer credit (days)", "customer_credit_days", "days"),
                   ("Supplier credit days", "credit_days", "days"), ("Advance (%)", "advance_pct", "pctin"),
                   ("Production lead (days)", "production_lead_days", "days")]
    k_ = len(wc_cols)
    hdr(ws_w, 4, [c[0] for c in wc_cols] + ["Cash cycle (days)", f"Cost per container ({model})", "Planned containers / yr", f"Peak working capital ({model})"])
    ws_w.row_dimensions[4].height = 45
    for i, iso in enumerate(isos):
        row = 5 + i
        lr = LANE_FIRST_ROW + i
        ws_w[f"A{row}"] = f"=Lanes!A{lr}"
        for j, (lab, key, fk) in enumerate(wc_cols[1:], start=2):
            src = f"Lanes!{C['transit']}{lr}" if key == "LANE:transit" else G[key]
            put(ws_w, f"{L(j)}{row}", f'=IF(ISNUMBER({src}),{src},"")', FILL_F, FMT[fk])
        cc = [f"{L(j)}{row}" for j in range(2, k_ + 1)]
        if export:
            pl, dw, cr, ad = cc
            cyc = guard(cc, f"({pl}+{dw}+{cr})*(1-{ad}/100)")
        else:
            tr, cl, inv, cu, cr, ad, pl = cc
            cyc = guard(cc, f"{tr}+{cl}+{inv}+{cu}-{cr}*(1-{ad}/100)+{pl}*{ad}/100")
        colc, colk, colp, colw = L(k_ + 1), L(k_ + 2), L(k_ + 3), L(k_ + 4)
        put(ws_w, f"{colc}{row}", "=" + cyc, FILL_R, FMT["days"])
        put(ws_w, f"{colk}{row}", f'=IF(ISNUMBER(Lanes!{C["user_cost"]}{lr}),Lanes!{C["user_cost"]}{lr},"")', FILL_F, FMT["money"])
        put(ws_w, f"{colp}{row}", f'=IF(ISNUMBER(Lanes!{C["plan"]}{lr}),Lanes!{C["plan"]}{lr},"")', FILL_F, FMT["count"])
        put(ws_w, f"{colw}{row}", "=" + guard([f"{colp}{row}", f"{colc}{row}", f"{colk}{row}"], f"{colp}{row}*{colc}{row}/365*{colk}{row}"),
            FILL_R, FMT["money"])
    for j in range(1, k_ + 5):
        ws_w.column_dimensions[L(j)].width = 22 if j == 1 else 16
    ws_w.freeze_panes = "B5"
    ws_w[f"A{6+n}"] = "Cash tied per container is the user's own cost per container (seller cost in an export study, landed cost in an import study). A negative cycle means suppliers finance the goods."

    # ---------------------------------------------------------------- Plan & payback
    pf = doc["portfolio"]
    title(ws_p, "Plan & payback",
          "Planned containers are inputs on the Lanes sheet (shown here as links). Monthly override cells are inputs; blank = planned total / 12 (derived).")
    hdr(ws_p, 4, ["Partner", "Planned containers / yr", f"Margin / container ({model})", "Containers in plan", f"Annual margin ({model})"])
    for i, iso in enumerate(isos):
        row = 5 + i
        lr = LANE_FIRST_ROW + i
        ws_p[f"A{row}"] = f"=Lanes!A{lr}"
        put(ws_p, f"B{row}", f'=IF(ISNUMBER(Lanes!{C["plan"]}{lr}),Lanes!{C["plan"]}{lr},"")', FILL_F, FMT["count"])
        put(ws_p, f"C{row}", f'=IF(ISNUMBER(Lanes!{C["margin"]}{lr}),Lanes!{C["margin"]}{lr},"")', FILL_F, FMT["money"])
        put(ws_p, f"D{row}", f'=IF(AND(ISNUMBER(B{row}),ISNUMBER(C{row})),IF(B{row}>0,B{row},0),0)', FILL_F, FMT["count"])
        put(ws_p, f"E{row}", f'=IF(D{row}>0,D{row}*C{row},"")', FILL_F, FMT["money"])
    a, b = 5, 4 + n
    s0 = 6 + n
    items = [
        ("Planned containers in plan (lanes with margin and plan)", f'=IF(SUM(D{a}:D{b})>0,SUM(D{a}:D{b}),"")', "count", FILL_R),
        ("Annual margin", f'=IF(ISNUMBER(B{s0}),SUM(E{a}:E{b}),"")', "money", FILL_R),
        ("Plan-weighted margin per container", f'=IF(ISNUMBER(B{s0}),B{s0+1}/B{s0},"")', "money", FILL_R),
        ("Annual fixed cost", f'=IF(ISNUMBER({G["fixed_annual"]}),{G["fixed_annual"]},"")', "money", FILL_F),
        ("One-time cost", f'=IF(ISNUMBER({G["fixed_one_time"]}),{G["fixed_one_time"]},"")', "money", FILL_F),
        ("Portfolio break-even containers / yr", f'=IF(AND(ISNUMBER(B{s0+2}),ISNUMBER(B{s0+3})),IF(B{s0+2}>0,B{s0+3}/B{s0+2},""),"")', "count", FILL_R),
        ("Cash lag (months) - input derived at build time from the plan-weighted cash cycle / 30", pf["cash_lag_months"], "int", FILL_IN),
    ]
    for j, (lab, fml, fk, fl) in enumerate(items):
        ws_p[f"A{s0+j}"] = lab
        put(ws_p, f"B{s0+j}", fml, fl, FMT[fk])
    lagc = f"$B${s0+6}"
    wmc, fixc, onec, totc = f"$B${s0+2}", f"$B${s0+3}", f"$B${s0+4}", f"$B${s0}"
    ws_p[f"C{s0+6}"] = (f"build-time weighted cash cycle: {pf['weighted_cash_cycle_days']:.1f} days" if pf["weighted_cash_cycle_days"] is not None
                        else "weighted cash cycle not available at build time (see payback note)")
    t0 = s0 + 9
    ws_p[f"A{t0-1}"] = "24-month cash table"
    ws_p[f"A{t0-1}"].font = Font(bold=True, size=12)
    hdr(ws_p, t0, ["Month", "Containers override (input)", "Containers", "Received containers (after lag)", f"Margin received ({model})",
                   f"Fixed cost ({model})", f"One-time ({model})", f"Net cash ({model})", f"Cumulative ({model})", "Reached payback"])
    ws_p.row_dimensions[t0].height = 45
    ramp = ((inputs.get("commercial") or {}).get("volume") or {}).get("monthly_ramp")
    first, lastm = t0 + 1, t0 + 24
    for m in range(1, 25):
        row = t0 + m
        ws_p[f"A{row}"] = m
        put(ws_p, f"B{row}", None if ramp is None else ramp[m - 1], FILL_IN, FMT["count"])
        put(ws_p, f"C{row}", f'=IF(ISNUMBER(B{row}),B{row},IF(ISNUMBER({totc}),{totc}/12,""))', FILL_F, FMT["count"])
        put(ws_p, f"D{row}", f'=IF(ISNUMBER({lagc}),IF(A{row}-{lagc}>=1,IF(ISNUMBER(INDEX($C${first}:$C${lastm},A{row}-{lagc})),INDEX($C${first}:$C${lastm},A{row}-{lagc}),""),0),"")',
            FILL_F, FMT["count"])
        put(ws_p, f"E{row}", f'=IF(AND(ISNUMBER(D{row}),ISNUMBER({wmc})),D{row}*{wmc},"")', FILL_F, FMT["money"])
        put(ws_p, f"F{row}", f'=IF(ISNUMBER({fixc}),{fixc}/12,"")', FILL_F, FMT["money"])
        put(ws_p, f"G{row}", f'=IF(ISNUMBER({onec}),{onec},"")' if m == 1 else "=0", FILL_F, FMT["money"])
        put(ws_p, f"H{row}", f'=IF(AND(ISNUMBER(E{row}),ISNUMBER(F{row}),ISNUMBER(G{row})),E{row}-F{row}-G{row},"")', FILL_F, FMT["money"])
        put(ws_p, f"I{row}", (f'=IF(ISNUMBER(H{row}),H{row},"")' if m == 1 else f'=IF(AND(ISNUMBER(I{row-1}),ISNUMBER(H{row})),I{row-1}+H{row},"")'), FILL_F, FMT["money"])
        put(ws_p, f"J{row}", f'=IF(ISNUMBER(I{row}),IF(I{row}>=0,A{row},""),"")', FILL_F, FMT["int"])
    ws_p[f"A{s0+7}"] = "Payback month (first month with cumulative cash >= 0)"
    ws_p[f"A{s0+7}"].font = BOLD
    put(ws_p, f"B{s0+7}", f'=IF(COUNT(J{first}:J{lastm})=0,"",MIN(J{first}:J{lastm}))', FILL_R, FMT["int"])
    ws_p[f"C{s0+7}"] = pf["payback_note"] or ""
    ws_p.column_dimensions["A"].width = 52
    for j in range(2, 11):
        ws_p.column_dimensions[L(j)].width = 18
    ws_p.freeze_panes = "B5"
    pay_ref = f"'Plan & payback'!$B${s0+7}"
    wm_ref = f"'Plan & payback'!$B${s0+2}"

    # ---------------------------------------------------------------- Provenance
    title(ws_v, "Provenance", "Every input with its tier, source and date. Orange rows are not user/quote data: verify before relying on them. Red = missing.")
    hdr(ws_v, 4, ["Input", "Lane", "Value", "Unit", "Tier", "Source", "URL", "As of", "Check"])
    for i, e in enumerate(doc["inputs_echo"]):
        row = 5 + i
        lab = mc.LABELS.get(e["key"], e["key"])
        vals = [lab, e["lane"] or "(all lanes)", e["value"], e["unit"], e["tier"], e["source"], e["url"] or "", e["as_of"] or "",
                "MISSING" if e["tier"] == "missing" else ("verify" if e["tier"] not in common.FIRM_TIERS else "ok")]
        for j, v in enumerate(vals):
            c = ws_v.cell(row=row, column=1 + j, value=v)
            if vals[-1] != "ok":
                c.fill = FILL_V
    for col, w in zip("ABCDEFGHI", (36, 12, 14, 18, 12, 48, 36, 12, 10)):
        ws_v.column_dimensions[col].width = w
    ws_v.freeze_panes = "A5"

    # ---------------------------------------------------------------- Limitations
    title(ws_lim, "Limitations")
    lim = [
        "This is a unit-economics model, not a demand forecast: planned containers are the client's plan, nothing here says the market will buy them.",
        "It is not tax or legal advice. Duty, VAT and valuation rules must be re-verified with the importing country's customs authority before any commitment.",
        "Tariff and VAT figures marked verify, modelled or published are working values; check the tariff.json status and the Provenance sheet.",
        "Freight is a modelled or quoted per-container figure, not a booking quote. Rates move weekly; low/base/high shows the range, not a guarantee.",
        "Incoterm DDP is not modelled (use DAP). FCA is treated as FOB.",
        "Insurance is a simple percentage of the insured value (CIF-style); real policies differ. Seller-side insurance is r x (FOB cost + freight).",
        "Customs value follows the valuation basis in tariff.json (CIF or FOB). Importers differ: confirm per country.",
        "VAT is shown only when it is not recoverable by the importer; recoverable VAT is a cash-flow item not modelled here.",
        "Cash cycle and payback use simple day counts and a whole-month lag; no interest, FX hedging, or seasonality.",
        "Currency: freight.json is in USD; the model is valid when the model currency is USD. FX shocks move one rate by (1 + x %).",
        "Benchmarks compare per-unit values with Comtrade average unit values (USD per kg); product mix and quality differences are not corrected.",
        "A blank cell means the input is missing. The model never substitutes a default; results that depend on it are blank.",
        "Sensitivity grids and the 24-month cash lag are computed at build time; the live switches change Lanes, Plan and Working capital only.",
    ]
    for i, t in enumerate(lim):
        ws_lim[f"A{3+i}"] = f"{i+1}. {t}"
        ws_lim[f"A{3+i}"].alignment = WRAP
    ws_lim.column_dimensions["A"].width = 140

    # ---------------------------------------------------------------- _check
    hdr(ws_c, 1, ["iso3", "metric", "formula"])
    rowc = 2
    for i, iso in enumerate(isos):
        lr = LANE_FIRST_ROW + i
        for metric, key in (("revenue", "revenue"), ("user_cost", "user_cost"), ("margin", "margin"), ("buyer_landed", "landed")):
            ws_c[f"A{rowc}"], ws_c[f"B{rowc}"] = iso, metric
            ws_c[f"C{rowc}"] = f"=Lanes!{C[key]}{lr}"
            rowc += 1
    for metric, ref in (("weighted_margin", wm_ref), ("payback_month", pay_ref)):
        ws_c[f"A{rowc}"], ws_c[f"B{rowc}"] = "PORTFOLIO", metric
        ws_c[f"C{rowc}"] = f"={ref}"
        rowc += 1

    # ---------------------------------------------------------------- Cover
    title(ws_cover, f"Financial model - {inputs.get('product', {}).get('name', '')}", f"{persp.capitalize()} study, {common.country_name(doc.get('home_country') or '')}")
    info = [("Study", doc.get("study_id")), ("Prepared for", inputs.get("prepared_for")), ("Prepared by", inputs.get("prepared_by")),
            ("Generated", doc["generated_at"]), ("Perspective", persp), ("Incoterm (fixed at build time)", f"{T} (FCA treated as FOB)"),
            ("Model currency", model), ("Container", doc["container_type"]), ("Lanes", ", ".join(isos))]
    for i, (a_, b_) in enumerate(info):
        ws_cover[f"A{4+i}"], ws_cover[f"B{4+i}"] = a_, b_
        ws_cover[f"A{4+i}"].font = BOLD
    r0 = 4 + len(info) + 1
    ws_cover[f"A{r0}"] = "Legend"
    ws_cover[f"A{r0}"].font = BOLD
    for j, (lab, fl) in enumerate([("Input cell (edit me; blank = missing)", FILL_IN), ("Formula cell (do not edit)", FILL_F),
                                   ("Key result", FILL_R), ("Live scenario switch", FILL_SW), ("Verify / missing (Provenance)", FILL_V)]):
        put(ws_cover, f"A{r0+1+j}", lab, fl)
    r1 = r0 + 7
    guide = [
        "How to use: change yellow cells on Assumptions (global inputs) or Lanes (per partner); set the freight scenario and shocks in Assumptions!B5:B8.",
        "Sheets: Assumptions, Lanes (one row per partner, all formulas), Lane detail (waterfall for one partner), Scenarios (switches + static grids),",
        "Plan & payback (24-month cash table), Working capital (cash cycle), Provenance (source of every input), Limitations. Sheet _check is hidden.",
        "Cell map: switches Assumptions!B5:B8 (names Sw_freight, Sw_price_pct, Sw_cost_pct, Sw_fx_pct); global inputs are defined names A_<input>; lanes start at Lanes row 5.",
        "The incoterm, cost basis and perspective are resolved when the workbook is built; to change them, re-run build_financial_model.py with updated inputs.json.",
        "Blank means missing: dependent cells stay blank, never zero. Figures reproduce financial.json (checked by recalculating in LibreOffice).",
    ]
    for i, t in enumerate(guide):
        ws_cover[f"A{r1+i}"] = t
    ws_cover.column_dimensions["A"].width = 60
    ws_cover.column_dimensions["B"].width = 60
    wb.save(path)
    return {"cols": C, "first_row": LANE_FIRST_ROW}


# ------------------------------------------------------------------ main


def _print_table(doc):
    print(f"\nFinancial model: {doc['perspective']} | incoterm {doc['incoterm']} | {doc['currency']} | {doc['container_type']}")
    head = f"{'ISO3':5} {'Partner':22} {'Status':11} {'Price basis':27} {'Margin %':>9} {'Break-even':>10}  Missing"
    print(head)
    print("-" * len(head))
    for iso, ln in doc["lanes"].items():
        mp = "-" if ln["margin_pct"] is None else f"{ln['margin_pct']:.1f}"
        be = "-" if ln["break_even_containers_per_year"] is None else f"{ln['break_even_containers_per_year']:.1f}"
        miss = ", ".join(ln["missing"]) or "-"
        if ln["buyer_side_missing"]:
            miss += " | buyer side: " + ", ".join(ln["buyer_side_missing"])
        print(f"{iso:5} {ln['name'][:22]:22} {ln['status']:11} {str(ln['price_basis'] or '-'):27} {mp:>9} {be:>10}  {miss}")
    p = doc["portfolio"]
    wm = "-" if p["weighted_margin"] is None else f"{p['weighted_margin']:,.0f}"
    print(f"\nPortfolio: planned {p['planned_containers_per_year']} containers/yr | weighted margin {wm} | payback month {p['payback_month']} {p['payback_note']}")


def run(study_dir, no_excel=False, names=None):
    doc, li_by, inputs = run_model(study_dir, names)
    d = Path(study_dir)
    common.write_json(common.study_file(d, "financial"), doc)
    if not no_excel:
        build_workbook(common.study_file(d, "model_xlsx"), doc, li_by, inputs, names)
    return doc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build financial.json and financial_model.xlsx for a study folder (contracts section 8).")
    ap.add_argument("--study-dir", required=True, help="study folder containing inputs.json, selection.json, freight.json, tariff.json")
    ap.add_argument("--no-excel", action="store_true", help="write financial.json only")
    a = ap.parse_args(argv)
    try:
        doc = run(a.study_dir, a.no_excel)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    _print_table(doc)
    print(f"\nWrote {common.study_file(a.study_dir, 'financial')}" + ("" if a.no_excel else f" and {common.study_file(a.study_dir, 'model_xlsx')}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

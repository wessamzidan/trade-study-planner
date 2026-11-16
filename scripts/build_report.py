#!/usr/bin/env python3
"""Stage 7: build study.docx from narrative.json (prose) and the study JSON files (tables, charts).

    python3 build_report.py --study-dir D [--allow-gaps] [--out FILE]

Exit 0 ok · 2 blocked (narrative.json missing, a required section empty, or a required input file missing; --allow-gaps
prints a visible "[to be written]" / "[not available]" placeholder instead) · 1 error.
Every number is read from the JSON files; the only arithmetic is unit scaling for display (USD to USD M, kg to t).
Section order follows references/study_structure.md. Charts need matplotlib (skipped with a note when missing).
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import validate_study as vs  # noqa: E402

try:
    from docx import Document
    from docx.enum.section import WD_ORIENT  # noqa: F401
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor
except ImportError:  # pragma: no cover
    print("ERROR: python-docx is required (pip install python-docx)", file=sys.stderr)
    sys.exit(1)

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAVE_MPL = True
except Exception:  # noqa: BLE001
    HAVE_MPL = False

FONT = "Calibri"
NAVY = RGBColor(0x1F, 0x38, 0x64)
GREY = RGBColor(0x59, 0x59, 0x59)
HEAD_FILL = "1F3864"
ALT_FILL = "F2F5FA"
WARN_FILL = "FFF2CC"
BLUE, ORANGE, LGREY = "#2F5597", "#C55A11", "#A6A6A6"
PLACEHOLDER = "[to be written]"
TIER_WORDS = {"quote": "Quote", "connector": "Live rate tool", "published": "Published rate", "modelled": "Modelled estimate",
              "user": "Client figure", "derived": "Derived", "missing": "Coverage gap"}
MIRROR_WORDS = {"partner_not_reporting": "Partner not reporting", "consistent": "Consistent", "partner_reports_less": "Partner reports less",
                "partner_reports_more": "Partner reports more", "only_home_reports": "Only home reports", "no_mirror_data": "No mirror data"}
CONTENT_CM = 17.0


# ------------------------------------------------------------------ formatting (display only)

def f_int(v):
    return "n/a" if v is None else f"{v:,.0f}"


def f_num(v, d=2):
    return "n/a" if v is None else f"{v:,.{d}f}"


def f_musd(v, d=2):
    return "n/a" if v is None else f"{v / 1e6:,.{d}f}"


def f_pct(v, d=1):
    return "n/a" if v is None else f"{v:,.{d}f}"


def f_auto(v):
    if v is None:
        return "missing"
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, (int, float)):
        s = f"{v:,.4f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:,.2f}".rstrip("0").rstrip(".")
        return s
    return str(v)


def txt(v):
    return "" if v is None else str(v)


# ------------------------------------------------------------------ docx primitives

def set_font(run_or_style, size=None, bold=None, italic=None, color=None):
    f = run_or_style.font
    f.name = FONT
    el = run_or_style._element
    rpr = el.get_or_add_rPr() if hasattr(el, "get_or_add_rPr") else el.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.append(rf)
    for a in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rf.set(qn(a), FONT)
    if size:
        f.size = Pt(size)
    if bold is not None:
        f.bold = bold
    if italic is not None:
        f.italic = italic
    if color is not None:
        f.color.rgb = color


def shade(cell, fill):
    tcPr = cell._element.get_or_add_tcPr()
    for old in tcPr.findall(qn("w:shd")):
        tcPr.remove(old)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tcPr.append(shd)


def add_field(par, instr, size=None, color=None):
    def r():
        run = par.add_run()
        set_font(run, size=size, color=color)
        return run
    a = r()
    e = OxmlElement("w:fldChar")
    e.set(qn("w:fldCharType"), "begin")
    a._r.append(e)
    b = r()
    it = OxmlElement("w:instrText")
    it.set(qn("xml:space"), "preserve")
    it.text = f" {instr} "
    b._r.append(it)
    c = r()
    e = OxmlElement("w:fldChar")
    e.set(qn("w:fldCharType"), "separate")
    c._r.append(e)
    d = r()
    d.text = "1"
    g = r()
    e = OxmlElement("w:fldChar")
    e.set(qn("w:fldCharType"), "end")
    g._r.append(e)


class Report:
    def __init__(self, study, allow_gaps, tmp):
        self.s = study
        self.allow_gaps = allow_gaps
        self.tmp = Path(tmp)
        self.doc = Document()
        self.n_table = 0
        self.n_fig = 0
        self.notes = []
        self._setup()

    # ---- setup
    def _setup(self):
        d = self.doc
        sec = d.sections[0]
        sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
        for m in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
            setattr(sec, m, Cm(2.0))
        st = d.styles
        set_font(st["Normal"], size=10.5)
        st["Normal"].paragraph_format.space_after = Pt(6)
        st["Normal"].paragraph_format.line_spacing = 1.08
        for name, size, before, after in (("Heading 1", 17, 18, 8), ("Heading 2", 13, 12, 4), ("Heading 3", 11, 8, 3)):
            h = st[name]
            set_font(h, size=size, bold=True, color=NAVY)
            h.paragraph_format.space_before = Pt(before)
            h.paragraph_format.space_after = Pt(after)
            h.paragraph_format.keep_with_next = True
        set_font(st["List Bullet"], size=10.5)
        st["List Bullet"].paragraph_format.space_after = Pt(3)
        sec.different_first_page_header_footer = True
        fp = sec.footer.paragraphs[0]
        fp.text = ""
        r = fp.add_run(f"Confidential  |  {self.title}")
        set_font(r, size=8.5, color=GREY)
        fp.add_run("\t")
        tabs = fp.paragraph_format.tab_stops
        tabs.add_tab_stop(Cm(CONTENT_CM), alignment=2)  # right
        rr = fp.add_run("Page ")
        set_font(rr, size=8.5, color=GREY)
        add_field(fp, "PAGE", size=8.5, color=GREY)
        cp = d.core_properties
        cp.title = self.title
        cp.author = self.s["inputs"].get("prepared_by") or "Trade Study Planner"
        cp.subject = f"HS {self.s['inputs']['product']['hs_code']}"

    @property
    def title(self):
        inp = self.s["inputs"]
        t = common.LABELS[inp["perspective"]]["study_title"]
        t = f"{t}: {inp['product']['name']}"
        return t + (" (Quick Scan)" if inp.get("depth") == "quick_scan" else "")

    # ---- text
    def para(self, text="", bold=False, italic=False, size=None, color=None, align=None, after=None, style=None, keep=False, fill=None):
        p = self.doc.add_paragraph(style=style)
        if text:
            r = p.add_run(text)
            set_font(r, size=size, bold=bold, italic=italic, color=color)
        if align == "center":
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif align == "right":
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        if after is not None:
            p.paragraph_format.space_after = Pt(after)
        if keep:
            p.paragraph_format.keep_with_next = True
        if fill:
            pPr = p._p.get_or_add_pPr()
            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:color"), "auto")
            shd.set(qn("w:fill"), fill)
            pPr.append(shd)
        return p

    def heading(self, text, level=1):
        h = self.doc.add_heading(text, level=level)
        for r in h.runs:
            set_font(r)
        return h

    def paras(self, items):
        for t in items or []:
            if isinstance(t, str) and t.strip():
                self.para(t)

    def bullets(self, items):
        for t in items or []:
            if isinstance(t, str) and t.strip():
                self.para(t, style="List Bullet")

    def numbered(self, items):
        for i, t in enumerate([x for x in items or [] if isinstance(x, str) and x.strip()], 1):
            p = self.para(f"{i}.\t{t}", after=3)
            p.paragraph_format.left_indent = Cm(0.8)
            p.paragraph_format.first_line_indent = Cm(-0.8)
            p.paragraph_format.tab_stops.add_tab_stop(Cm(0.8))

    def gap(self, what=PLACEHOLDER):
        self.para(what, bold=True, fill="FFFF00")

    def note(self, text):
        self.para(text, italic=True, size=9, color=GREY)

    def page_break(self):
        self.doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    # ---- tables
    def caption(self, text, kind="Table"):
        if kind == "Table":
            self.n_table += 1
            n = self.n_table
        else:
            self.n_fig += 1
            n = self.n_fig
        self.para(f"{kind} {n}. {text}", bold=True, size=9.5, color=NAVY, after=3, keep=True)

    def table(self, headers, rows, aligns=None, widths=None, caption=None, size=8.5):
        """rows: list of list[str] or (list[str], {'fill':hex,'bold':bool,'merge_from':int})."""
        if caption:
            self.caption(caption)
        n = len(headers)
        aligns = aligns or ["l"] * n
        widths = widths or [CONTENT_CM / n] * n
        tot = sum(widths)
        widths = [w * CONTENT_CM / tot for w in widths]
        t = self.doc.add_table(rows=1, cols=n)
        t.style = "Table Grid"
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        t.autofit = False
        for i, col in enumerate(t.columns):
            col.width = Cm(widths[i])
        tblPr = t._tbl.tblPr
        lay = OxmlElement("w:tblLayout")
        lay.set(qn("w:type"), "fixed")
        tblPr.append(lay)
        borders = OxmlElement("w:tblBorders")
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            e = OxmlElement(f"w:{edge}")
            e.set(qn("w:val"), "single")
            e.set(qn("w:sz"), "4")
            e.set(qn("w:color"), "BFBFBF")
            borders.append(e)
        tblPr.append(borders)
        hdr = t.rows[0]
        trPr = hdr._tr.get_or_add_trPr()
        th = OxmlElement("w:tblHeader")
        th.set(qn("w:val"), "true")
        trPr.append(th)
        for i, h in enumerate(headers):
            c = hdr.cells[i]
            c.width = Cm(widths[i])
            shade(c, HEAD_FILL)
            p = c.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT if aligns[i] == "r" else WD_ALIGN_PARAGRAPH.LEFT
            r = p.add_run(h)
            set_font(r, size=size, bold=True, color=RGBColor(255, 255, 255))
        for ri, row in enumerate(rows):
            cells, opts = row if isinstance(row, tuple) else (row, {})
            tr = t.add_row()
            cs = tr._tr.get_or_add_trPr()
            cant = OxmlElement("w:cantSplit")
            cant.set(qn("w:val"), "true")
            cs.append(cant)
            mf = opts.get("merge_from")
            for i in range(n):
                c = tr.cells[i]
                c.width = Cm(widths[i])
                if opts.get("fill"):
                    shade(c, opts["fill"])
                elif ri % 2 == 1:
                    shade(c, ALT_FILL)
            if mf is not None and mf < n - 1:
                m = tr.cells[mf].merge(tr.cells[n - 1])
                m.text = ""
                values = cells[:mf] + [cells[mf]]
                targets = [tr.cells[i] for i in range(mf)] + [m]
                al = aligns[:mf] + ["l"]
            else:
                values, targets, al = cells, [tr.cells[i] for i in range(n)], aligns
            for v, c, a in zip(values, targets, al):
                p = c.paragraphs[0]
                p.paragraph_format.space_after = Pt(0)
                p.alignment = WD_ALIGN_PARAGRAPH.RIGHT if a == "r" else WD_ALIGN_PARAGRAPH.LEFT
                r = p.add_run(txt(v))
                set_font(r, size=size, bold=opts.get("bold"))
        self.para("", after=4)
        return t

    def callout(self, label, text):
        t = self.doc.add_table(rows=1, cols=1)
        t.style = "Table Grid"
        c = t.rows[0].cells[0]
        shade(c, WARN_FILL)
        p = c.paragraphs[0]
        r = p.add_run(f"{label} ")
        set_font(r, size=10.5, bold=True)
        r = p.add_run(text)
        set_font(r, size=10.5)
        self.para("", after=4)

    def figure(self, path, caption, width=16.0):
        if path is None:
            self.note("[chart omitted: matplotlib is not installed]")
            return
        self.caption(caption, "Figure")
        self.doc.add_picture(str(path), width=Cm(width))
        self.doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
        self.doc.paragraphs[-1].paragraph_format.keep_with_next = False


# ------------------------------------------------------------------ charts (PNG, 200 dpi)

def _style_ax(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(labelsize=8)
    ax.grid(axis="y", color="#E5E5E5", linewidth=0.6)
    ax.set_axisbelow(True)


def chart_trend(R, path):
    if not HAVE_MPL:
        return None
    ann = R.s["analysis"]["home_position"]["annual"]
    fig, ax = plt.subplots(figsize=(7.2, 3.1))
    xs = [str(a["year"]) + (" (partial)" if a.get("partial") else "") for a in ann]
    vals = [(a["value_usd"] or 0) / 1e6 for a in ann]
    bars = ax.bar(xs, vals, color=[LGREY if a.get("partial") else BLUE for a in ann], width=0.6)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v, f"{v:,.1f}", ha="center", va="bottom", fontsize=7.5)
    ax.set_ylabel("USD million", fontsize=8)
    _style_ax(ax)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)
    return path


def chart_ranking(R, rows, home_iso, path):
    if not HAVE_MPL or not rows:
        return None
    fig, ax = plt.subplots(figsize=(7.2, 0.28 * len(rows) + 0.9))
    names = [f"{r['rank']}. {r['name']}" for r in rows]
    vals = [(r["value_usd"] or 0) / 1e6 for r in rows]
    cols = [ORANGE if r["iso3"] == home_iso else BLUE for r in rows]
    ypos = list(range(len(rows)))[::-1]
    ax.barh(ypos, vals, color=cols, height=0.65)
    ax.set_yticks(ypos)
    ax.set_yticklabels(names, fontsize=8)
    for y, v in zip(ypos, vals):
        ax.text(v, y, f" {v:,.1f}", va="center", fontsize=7.5)
    ax.set_xlabel("USD million, latest complete year", fontsize=8)
    ax.tick_params(labelsize=8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)
    return path


def chart_freight(R, path):
    if not HAVE_MPL:
        return None
    lanes = [(iso, ln) for iso, ln in R.s["freight"]["lanes"].items() if (ln.get("freight_usd") or {}).get("base") is not None]
    if not lanes:
        return None
    fig, ax = plt.subplots(figsize=(7.2, 3.1))
    names = [ln["partner"]["name"] for _, ln in lanes]
    base = [ln["freight_usd"]["base"] for _, ln in lanes]
    lo = [b - ln["freight_usd"]["low"] for b, (_, ln) in zip(base, lanes)]
    hi = [ln["freight_usd"]["high"] - b for b, (_, ln) in zip(base, lanes)]
    cols = [BLUE if ln["freight"]["tier"] in ("quote", "connector", "published") else LGREY for _, ln in lanes]
    ax.bar(names, base, color=cols, width=0.6, yerr=[lo, hi], capsize=3, error_kw={"linewidth": 0.8})
    ax.set_ylabel(f"USD per {R.s['freight'].get('container_type', '')} container", fontsize=8)
    ax.tick_params(axis="x", labelrotation=20)
    _style_ax(ax)
    fig.text(0.01, 0.01, "Blue: quote, live or published rate. Grey: modelled estimate. Bars show base; whiskers show low to high.", fontsize=7, color="#595959")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path, dpi=200)
    plt.close(fig)
    return path


def chart_margin(R, path):
    if not HAVE_MPL:
        return None
    lanes = [(iso, ln) for iso, ln in R.s["financial"]["lanes"].items() if ln.get("status") == "modelled" and ln.get("margin") is not None]
    if not lanes:
        return None
    fig, ax = plt.subplots(figsize=(7.2, 3.1))
    names = [ln.get("name") or iso for iso, ln in lanes]
    vals = [ln["margin"] for _, ln in lanes]
    ax.bar(names, vals, color=[BLUE if v >= 0 else ORANGE for v in vals], width=0.6)
    for i, v in enumerate(vals):
        ax.text(i, v, f"{v:,.0f}", ha="center", va="bottom" if v >= 0 else "top", fontsize=7.5)
    ax.axhline(0, color="#595959", linewidth=0.6)
    ax.set_ylabel(f"Margin, {R.s['financial']['currency']} per container", fontsize=8)
    ax.tick_params(axis="x", labelrotation=20)
    _style_ax(ax)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)
    return path


def chart_payback(R, path):
    if not HAVE_MPL:
        return None
    mon = (R.s["financial"].get("portfolio") or {}).get("monthly") or []
    if not mon:
        return None
    fig, ax = plt.subplots(figsize=(7.2, 2.9))
    ax.plot([m["month"] for m in mon], [m["cumulative"] for m in mon], color=BLUE, linewidth=1.8)
    ax.axhline(0, color="#595959", linewidth=0.6)
    pm = R.s["financial"]["portfolio"].get("payback_month")
    if pm:
        ax.axvline(pm, color=ORANGE, linestyle="--", linewidth=1)
        ax.text(pm, ax.get_ylim()[1] * 0.05, f" payback month {pm}", color=ORANGE, fontsize=8)
    ax.set_xlabel("Month", fontsize=8)
    ax.set_ylabel(f"Cumulative cash, {R.s['financial']['currency']}", fontsize=8)
    _style_ax(ax)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)
    return path


# ------------------------------------------------------------------ sections

def sections_plan(S):
    inp, a = S["inputs"], S["analysis"]
    persp = inp["perspective"]
    full = inp.get("depth") != "quick_scan"
    supply = persp == "import"
    plan = [("exec", "1. Executive summary"), ("scope", "2. Scope and method"), ("home", "3. Home position"),
            ("global", "4. " + ("Global supply landscape" if supply else "Global demand landscape"))]
    if full:
        plan.append(("comp", "5. " + ("Supplier profiles and their other buyers" if supply else "Competitor exporters in each market")))
        plan.append(("logi", "6. Logistics and market access"))
    plan.append(("short", "7. Shortlist"))
    if full:
        plan.append(("fin", "8. Financial case"))
        plan.append(("risk", "9. Risks and open questions"))
    plan.append(("next", "10. Next steps"))
    plan.append(("appA", "Appendix A: Sources, tiers and verification checklist"))
    plan.append(("appB", "Appendix B: Method"))
    return plan


def lane_name(S, iso):
    for p in S["analysis"].get("partners", []):
        if p.get("iso3") == iso:
            return p.get("name") or iso
    return common.country_name(iso)


def sel_partners(S):
    return (S.get("selection") or {}).get("partners") or []


def cover_and_toc(R, plan):
    S, inp, a = R.s, R.s["inputs"], R.s["analysis"]
    for _ in range(5):
        R.para("", after=12)
    R.para(common.LABELS[inp["perspective"]]["study_title"] + (" - Quick Scan" if inp.get("depth") == "quick_scan" else ""),
           bold=True, size=30, color=NAVY, after=4)
    R.para(inp["product"]["name"], bold=True, size=20, after=2)
    R.para(f"HS {inp['product']['hs_code']}: {inp['product'].get('hs_description') or ''}".rstrip(": "), size=11, color=GREY, after=18)
    for lab, val in (("Home country", a["meta"]["home"]["name"]),
                     ("Perspective", "Export (home country sells; partners are destination markets)" if inp["perspective"] == "export"
                      else "Import (home country buys; partners are supplier countries)"),
                     ("Prepared for", inp.get("prepared_for") or "not specified"),
                     ("Prepared by", inp.get("prepared_by") or "not specified"),
                     ("Date", inp.get("date_prepared") or common.today())):
        p = R.para(after=3)
        r = p.add_run(f"{lab}:  ")
        set_font(r, size=11, bold=True, color=NAVY)
        r = p.add_run(val)
        set_font(r, size=11)
    R.para("", after=30)
    R.para("CONFIDENTIAL", bold=True, size=12, color=ORANGE.replace("#", "") and RGBColor(0xC5, 0x5A, 0x11))
    R.para("Prepared for the named client only. Figures marked as modelled, derived or published must be verified before any commitment.",
           italic=True, size=9, color=GREY)
    R.page_break()
    R.para("Contents", bold=True, size=17, color=NAVY, after=8)
    # TOC as a real field whose cached result lists the headings (Word updates page numbers on F9 / right-click > Update field)
    entries = [t for _, t in plan]
    first = R.doc.add_paragraph()
    for typ, instr in (("begin", None), (None, ' TOC \\o "1-2" \\h \\z \\u '), ("separate", None)):
        r = first.add_run()
        set_font(r)
        if typ:
            e = OxmlElement("w:fldChar")
            e.set(qn("w:fldCharType"), typ)
            r._r.append(e)
        else:
            it = OxmlElement("w:instrText")
            it.set(qn("xml:space"), "preserve")
            it.text = instr
            r._r.append(it)
    set_font(first.add_run(entries[0]), size=10.5)
    last = first
    for t in entries[1:]:
        last = R.doc.add_paragraph()
        set_font(last.add_run(t), size=10.5)
    r = last.add_run()
    e = OxmlElement("w:fldChar")
    e.set(qn("w:fldCharType"), "end")
    r._r.append(e)
    R.note("Right-click the contents and choose Update field to add page numbers. Headings are navigable in the Word navigation pane.")
    R.page_break()


def sec_exec(R):
    S, N = R.s, R.s["narrative"]
    e = N.get("executive_summary") or {}
    a, inp = S["analysis"], S["inputs"]
    L = a["meta"]["labels"]
    if e.get("headline"):
        R.para(e["headline"], bold=True, size=13, color=NAVY, after=8)
    else:
        R.gap()
    hp, meta = a["home_position"], a["meta"]
    cells = [(f"{'Exports' if inp['perspective'] == 'export' else 'Imports'}, {meta['latest_complete_year']} (USD M)", f_musd(hp.get("latest_value_usd"), 1)),
             (f"Growth {meta['window_label']} (% a year)", f_pct(hp.get("cagr_pct"))),
             ("World rank", f"{hp['world_rank']} of {hp['n_ranked']}" if hp.get("world_rank") else "n/a"),
             (f"{L['tier_core']} / {L['tier_growth']} / {L['tier_new']}", " / ".join(str(len((a.get("shortlist") or {}).get(k) or [])) for k in ("core", "growth", "new")))]
    fin = S.get("financial")
    if fin:
        lanes = fin["lanes"]
        nm = sum(1 for v in lanes.values() if v.get("status") == "modelled")
        cells.append(("Lanes modelled", f"{nm} of {len(lanes)}"))
        pm = fin.get("portfolio", {}).get("payback_month")
        cells.append(("Payback", f"month {pm}" if pm else "not reached / n/a"))
    elif inp.get("depth") != "quick_scan" and inp.get("commercial") is None:
        cells.append(("Financial case", "deferred"))
    R.table([c[0] for c in cells], [[c[1] for c in cells]], aligns=["r"] * len(cells), size=9)
    R.paras(e.get("paragraphs"))
    if not e.get("paragraphs"):
        R.gap()
    R.para("Recommendations", bold=True, size=11.5, color=NAVY, after=3, keep=True)
    if e.get("recommendations"):
        R.numbered(e["recommendations"])
    else:
        R.gap()
    if e.get("key_caveat"):
        R.callout("Key caveat:", e["key_caveat"])
    else:
        R.gap()


def prose(R, key, fields=("paragraphs", "bullets"), required=True):
    sec = (R.s["narrative"].get("sections") or {}).get(key) or {}
    got = False
    for f in fields:
        v = sec.get(f)
        if v:
            got = True
            (R.paras if f == "paragraphs" else R.bullets)(v)
    if required and not got:
        R.gap()
    return sec


def sec_scope(R):
    S, inp, a = R.s, R.s["inputs"], R.s["analysis"]
    m = a["meta"]
    prose(R, "scope_method")
    rows = [["Product", inp["product"]["name"]], ["HS code", inp["product"]["hs_code"]],
            ["HS description", inp["product"].get("hs_description") or inp["product"].get("description") or "n/a"],
            ["HS code confirmed by client", "Yes" if inp["product"].get("hs_confirmed_by_user") else "No"],
            ["Perspective", inp["perspective"].capitalize()], ["Home country", m["home"]["name"]],
            ["Depth", "Quick scan" if inp.get("depth") == "quick_scan" else "Full study"],
            ["Years requested", f"{inp['years']['from']} to {inp['years']['to']}"],
            ["Latest complete year", str(m["latest_complete_year"])],
            ["Partial year(s), not used for growth", ", ".join(str(y) for y in m.get("partial_years") or []) or "none"],
            ["Growth window", m.get("window_label") or "n/a"],
            ["Partners of interest", ", ".join(lane_name(S, i) for i in inp.get("partners_of_interest") or []) or "none"],
            ["Existing partners", ", ".join(lane_name(S, i) for i in inp.get("partners_existing") or []) or "none"],
            ["Excluded", ", ".join(lane_name(S, i) for i in inp.get("partners_excluded") or []) or "none"]]
    if inp.get("depth") != "quick_scan" and inp.get("commercial") is None:
        rows.append(["Financial case", "Deferred: no commercial inputs were provided"])
    R.table(["Item", "Setting"], rows, widths=[5, 12], caption="Scope of the study")


def sec_home(R):
    S, a, inp = R.s, R.s["analysis"], R.s["inputs"]
    meta, hp, L = a["meta"], a["home_position"], a["meta"]["labels"]
    flow = "exports" if inp["perspective"] == "export" else "imports"
    prose(R, "home_position")
    rows = []
    for r in hp["annual"]:
        rows.append(([str(r["year"]), f_musd(r.get("value_usd")), f_int(None if r.get("net_kg") is None else r["net_kg"] / 1000),
                      f_num(r.get("unit_value_usd_per_kg"), 3), "partial (excluded from growth)" if r.get("partial") else "complete"],
                     {"fill": WARN_FILL} if r.get("partial") else {}))
    R.table(["Year", f"Value (USD M)", "Volume (tonnes)", "Unit value (USD/kg)", "Status"], rows,
            aligns=["l", "r", "r", "r", "l"], widths=[2, 3, 3.2, 3.4, 5.4], caption=f"{meta['home']['name']}'s {flow} of {inp['product']['name'].lower()}")
    R.table(["Latest complete year", "Growth (% a year)", "Growth window", "World rank", "World share (%)"],
            [[str(meta["latest_complete_year"]), f_pct(hp.get("cagr_pct"), 2), meta.get("window_label") or "n/a",
              f"{hp['world_rank']} of {hp['n_ranked']}" if hp.get("world_rank") else "n/a", f_pct(hp.get("world_share_pct"), 2)]],
            aligns=["r"] * 5, caption="Key position figures")
    R.figure(chart_trend(R, R.tmp / "trend.png"), f"{meta['home']['name']}'s {flow}, USD million (partial year in grey)")
    top = sorted([p for p in a["partners"] if (p.get("home_flow_latest_usd") or 0) > 0], key=lambda p: -p["home_flow_latest_usd"])[:8]
    rows = [[p["name"], f_musd(p["home_flow_latest_usd"]), f_pct(p.get("home_flow_share_of_home_pct")), f_pct(p.get("home_flow_cagr_pct")),
             f_num(p.get("home_unit_value_usd_per_kg"), 3)] for p in top]
    R.table([L["partner_noun"].capitalize(), "Value (USD M)", "Share of home total (%)", "Growth (% a year)", "Unit value (USD/kg)"], rows,
            aligns=["l", "r", "r", "r", "r"], widths=[5, 2.8, 3.4, 2.9, 3], caption=f"Leading {L['partner_noun']}s for home {flow}, {meta['latest_complete_year']}")


def sec_global(R):
    S, a, inp = R.s, R.s["analysis"], R.s["inputs"]
    meta, L = a["meta"], a["meta"]["labels"]
    persp = inp["perspective"]
    view_key = common.LABELS[persp]["primary_world_view"]
    view = a[view_key]
    noun = "imports" if persp == "export" else "exports"
    prose(R, "global_landscape")
    rows = [([str(r["year"]), f_musd(r.get("value_usd")), f_int(r.get("n_reporters")), "partial (excluded from growth)" if r.get("partial") else "complete"],
             {"fill": WARN_FILL} if r.get("partial") else {}) for r in view["annual"]]
    R.table(["Year", f"World {noun} (USD M)", "Reporting countries", "Status"], rows, aligns=["l", "r", "r", "l"], widths=[2.5, 4, 4, 6.5],
            caption=f"World {noun} of the product, sum of reporting countries")
    home_iso = meta["home"]["iso3"]
    ranking = view.get("ranking") or []
    top = ranking[:15]
    home_row = next((r for r in ranking if r["iso3"] == home_iso), None)
    rows = []
    for r in top:
        rows.append(([str(r["rank"]), r["name"], f_musd(r["value_usd"]), f_pct(r.get("share_pct")), f_pct(r.get("cagr_pct")), f_num(r.get("unit_value_usd_per_kg"), 3)],
                     {"fill": WARN_FILL, "bold": True} if r["iso3"] == home_iso else {}))
    shown = list(top)
    if home_row and home_row not in top:
        rows.append((["..."] + [""] * 5, {}))
        rows.append(([str(home_row["rank"]), home_row["name"] + " (home)", f_musd(home_row["value_usd"]), f_pct(home_row.get("share_pct")),
                      f_pct(home_row.get("cagr_pct")), f_num(home_row.get("unit_value_usd_per_kg"), 3)], {"fill": WARN_FILL, "bold": True}))
        shown.append(home_row)
    R.table(["Rank", "Country", f"{noun.capitalize()} (USD M)", "Share (%)", "Growth (% a year)", "Unit value (USD/kg)"], rows,
            aligns=["r", "l", "r", "r", "r", "r"], widths=[1.4, 4.6, 3.1, 2.2, 2.9, 2.8],
            caption=f"Top 15 by {noun}, {meta['latest_complete_year']} (growth {meta['window_label']})")
    if not home_row:
        R.note(f"{meta['home']['name']} has no row in this world view, so it is not shown in the ranking.")
    R.figure(chart_ranking(R, shown, home_iso, R.tmp / "ranking.png"), f"Top countries by {noun}, USD million, {meta['latest_complete_year']} (home in orange)")
    cnt = {k: len([p for p in a["partners"] if p["tier"] == k]) for k in ("core", "growth", "new", "minor")}
    rules = meta["rules"]
    rows = [[L["tier_core"], f"Home flow at least {f_pct(rules['core_share_pct'])}% of home total, or among home's top {rules['core_top_n']}", str(cnt["core"])],
            [L["tier_growth"], f"Not core; home flow at least {f_pct(rules['min_presence_share_pct'], 2)}% of home total", str(cnt["growth"])],
            [L["tier_new"], f"Remaining countries with at least {f_pct(rules['material_partner_share_pct'], 2)}% of the world view", str(cnt["new"])],
            ["Minor", "The rest; never shortlisted automatically", str(cnt["minor"])]]
    R.table(["Tier", "Rule", "Candidates"], rows, aligns=["l", "l", "r"], widths=[3, 11, 3], caption="How candidates are tiered")
    if meta.get("warnings"):
        R.note("Data notes: " + "; ".join(meta["warnings"]))


def sec_comp(R):
    S, a, inp = R.s, R.s["analysis"], R.s["inputs"]
    meta, L = a["meta"], a["meta"]["labels"]
    persp = inp["perspective"]
    prose(R, "competition")
    home_iso = meta["home"]["iso3"]
    for iso in sel_partners(S):
        name = lane_name(S, iso)
        pdl = (a.get("partner_detail") or {}).get(iso)
        R.heading(name, 2)
        if not pdl:
            R.para(f"{'The partner' + chr(39) + 's own import data' if persp == 'export' else 'Supplier export data'} were not available, "
                   f"so the {'supplier mix' if persp == 'export' else 'buyer mix'} is not shown for {name} rather than estimated.")
            continue
        basis = {"partner_reported": "reported by the partner itself", "counterparty_reported": "reported by counterparties"}.get(pdl["basis"], pdl["basis"])
        R.note(f"Basis: {basis}. {pdl.get('basis_note') or ''}")
        rows_all = pdl["rows"]
        top = rows_all[:8]
        rows = [([r["name"], f_musd(r["value_usd"]), f_pct(r.get("share_pct")), f_num(r.get("unit_value_usd_per_kg"), 3)],
                 {"fill": WARN_FILL, "bold": True} if r["iso3"] == home_iso else {}) for r in top]
        hr = next((r for r in rows_all if r["iso3"] == home_iso), None)
        if hr and hr not in top:
            rows.append((["..."] + [""] * 3, {}))
            rows.append(([hr["name"] + " (home)", f_musd(hr["value_usd"]), f_pct(hr.get("share_pct")), f_num(hr.get("unit_value_usd_per_kg"), 3)], {"fill": WARN_FILL, "bold": True}))
        who = "Exporter" if persp == "export" else "Buyer"
        what = "Imports" if persp == "export" else "Purchases"
        R.table([who, f"{what} from this {L['partner_noun']}'s data (USD M)", "Share (%)", "Unit value (USD/kg)"], rows, aligns=["l", "r", "r", "r"],
                widths=[6, 5, 2.5, 3.5], caption=f"{name}: {'supplier mix' if persp == 'export' else 'buyer mix'}, {pdl['year']}")
        R.para(f"Total {pdl['year']}: USD {f_musd(pdl.get('total_usd'))} M. Home rank: {pdl.get('home_rank') or 'not listed'}; home share: "
               f"{f_pct(pdl.get('home_share_pct'), 2) + '%' if pdl.get('home_share_pct') is not None else 'n/a'}; concentration index (HHI): {f_num(pdl.get('hhi'), 3)}.", size=9.5)


def sec_logi(R):
    S, inp, a = R.s, R.s["inputs"], R.s["analysis"]
    prose(R, "logistics_access")
    fr, ta = S.get("freight"), S.get("tariff")
    if not fr and not ta:
        R.gap("[not available: freight.json and tariff.json were not provided]")
        return
    if fr:
        rows = []
        for iso in sel_partners(S) or list(fr["lanes"]):
            ln = fr["lanes"].get(iso)
            if not ln:
                rows.append(([lane_name(S, iso), "", "", "No lane built", "", "", "", ""], {"fill": WARN_FILL}))
                continue
            fu, tier = ln["freight_usd"], ln["freight"]["tier"]
            op, dp = ln.get("origin_port") or {}, ln.get("destination_port") or {}
            route = f"{op.get('name', 'n/a')} to {dp.get('name', 'n/a')}"
            if ln.get("coverage_gap"):
                rows.append(([ln["partner"]["name"], route, f_int(ln.get("distance_nm")), "Coverage gap: no freight figure", "", "", "", ""], {"fill": WARN_FILL}))
                continue
            td = ln.get("transit_days") or {}
            rows.append(([ln["partner"]["name"], route, f_int(ln.get("distance_nm")), TIER_WORDS.get(tier, tier), f_int(fu["low"]), f_int(fu["base"]),
                          f_int(fu["high"]), f_num(td.get("base"), 1)], {"fill": WARN_FILL} if tier == "modelled" else {}))
        R.table(["Lane", "Route", "Distance (nm)", "Rate basis", "Low (USD)", "Base (USD)", "High (USD)", "Transit, base (days)"], rows,
                aligns=["l", "l", "r", "l", "r", "r", "r", "r"], widths=[2.6, 3.6, 1.7, 2.6, 1.5, 1.6, 1.6, 1.8], size=8,
                caption=f"Sea freight per {fr.get('container_type', '')} container, USD ({'home to partner' if fr.get('direction') == 'home_to_partner' else 'partner to home'})")
        R.note("Shaded rows are modelled estimates fitted to dated benchmarks; they are indicative, not quotes. All-in quotes include the charges they list.")
        R.figure(chart_freight(R, R.tmp / "freight.png"), "Freight per container by lane: base with low to high range")
    elif inp.get("depth") != "quick_scan" and inp.get("commercial") is not None:
        R.gap("[not available: freight.json]")
    if ta:
        rows, ntb = [], []
        for iso in sel_partners(S) or list(ta["lanes"]):
            ln = ta["lanes"].get(iso)
            if not ln:
                continue
            d = ln["duty_rate_pct"]
            pref = ln.get("preference") or {}
            dv = common.sv_value(d)
            vat = common.sv_value(ln.get("import_vat_pct"))
            rec = "" if ln.get("vat_recoverable") is None else (", recoverable" if ln["vat_recoverable"] else ", not recoverable")
            rows.append(([lane_name(S, iso), f_pct(dv) if dv is not None else "not confirmed", f"{d.get('tier', 'missing')}, {d.get('as_of') or 'no date'}",
                          f_pct(common.sv_value(ln.get("mfn_rate_pct"))),
                          (f"{pref['agreement']}: {f_pct(common.sv_value(pref.get('rate_pct')))}" if pref.get("agreement") else "none"),
                          f_pct(common.sv_value(ln.get("other_duties_pct"))), (f_pct(vat) + rec) if vat is not None else "n/a",
                          ln.get("valuation_basis") or "n/a", "VERIFY" if ln["status"] == "verify" else "Sourced"],
                         {"fill": WARN_FILL, "bold": True} if ln["status"] == "verify" else {}))
            if ln.get("ntb"):
                ntb.append(f"{lane_name(S, iso)}: " + "; ".join(ln["ntb"]))
        who = "partner's" if inp["perspective"] == "export" else "home"
        R.table(["Lane", "Duty applied (%)", "Tier, as of", "MFN (%)", "Preference: rate (%)", "Other duties (%)", "Import VAT (%)", "Valuation", "Status"], rows,
                aligns=["l", "r", "l", "r", "l", "r", "l", "l", "l"], widths=[2.4, 1.6, 2.4, 1.2, 2.6, 1.4, 2.2, 1.5, 1.7], size=8,
                caption=f"Import duty and taxes ({who} schedule), HS {ta.get('hs_code', inp['product']['hs_code'])}")
        R.note("Duty applied is the rate the financial model uses. Lanes marked VERIFY could not be confirmed from an official source.")
        if ntb:
            R.para("Non-tariff requirements", bold=True, after=2, keep=True)
            R.bullets(ntb)
    elif inp.get("depth") != "quick_scan" and inp.get("commercial") is not None:
        R.gap("[not available: tariff.json]")


def sec_short(R):
    S, a, inp = R.s, R.s["analysis"], R.s["inputs"]
    L = a["meta"]["labels"]
    sec = prose(R, "shortlist", fields=("paragraphs",))
    pmap = {p["iso3"]: p for p in a["partners"]}
    sel = sel_partners(S)
    sizes = "Imports" if inp["perspective"] == "export" else "Exports"

    def prow(iso):
        p = pmap.get(iso) or {}
        pri = f"Yes (#{sel.index(iso) + 1})" if iso in sel else "No"
        score = "not scored" if p.get("score") is None else f_num(p["score"], 1)
        return [p.get("name") or lane_name(S, iso), pri, score, f_musd(p.get("partner_trade_latest_usd")), f_pct(p.get("partner_trade_cagr_pct")),
                f_musd(p.get("home_flow_latest_usd")), f_pct(p.get("home_flow_share_of_home_pct")), f_num(p.get("partner_unit_value_usd_per_kg"), 3)]

    heads = [L["partner_noun"].capitalize(), "Selected", "Score (0-100)", f"{sizes} of {L['partner_noun']} (USD M)", "Growth (% a year)",
             "Home flow (USD M)", "Share of home (%)", "Unit value (USD/kg)"]
    al = ["l", "l", "r", "r", "r", "r", "r", "r"]
    wd = [3.2, 1.9, 2.0, 2.4, 1.8, 2.0, 1.9, 1.9]
    inlist = set()
    for key, lab in (("core", L["tier_core"]), ("growth", L["tier_growth"]), ("new", L["tier_new"])):
        lst = (a.get("shortlist") or {}).get(key) or []
        inlist.update(lst)
        if lst:
            R.table(heads, [prow(i) for i in lst], aligns=al, widths=wd, size=8, caption=f"{lab} ({L['partner_noun']}s on the automatic shortlist)")
        else:
            R.para(f"{lab}: no {L['partner_noun']} qualified for this tier.", size=9.5)
    extra = [i for i in sel if i not in inlist]
    if extra:
        R.table(heads, [prow(i) for i in extra], aligns=al, widths=wd, size=8, caption="Selected but not on the automatic shortlist")
    R.note("Score is a ranking aid (weighted percentiles), not a verdict. 'not scored' means the partner reported too little trade data in the latest complete year.")
    rows = [[str(i + 1), lane_name(S, iso), (pmap.get(iso) or {}).get("tier", "n/a"),
             sec.get("partner_notes", {}).get(iso) or (S["selection"].get("notes") or {}).get(iso) or ""] for i, iso in enumerate(sel)]
    if rows:
        R.table(["Priority", L["partner_noun"].capitalize(), "Tier", "Note"], rows, widths=[1.6, 3.4, 1.8, 10.2], size=8.5, caption="Selected partners in priority order")
    if (sec.get("partner_notes") or {}) and not rows:
        R.bullets([f"{lane_name(S, i)}: {t}" for i, t in sec["partner_notes"].items()])
    npz = sec.get("not_prioritised") or []
    if npz:
        rows = []
        for r in npz:
            rec = common.resolve_country(r.get("partner"))
            rows.append([rec["name"] if rec else txt(r.get("partner")), txt(r.get("reason"))])
        R.table([L["partner_noun"].capitalize(), "Reason not prioritised"], rows, widths=[4, 13], caption="Candidates not prioritised")
    mc = a.get("mirror_checks") or []
    keep = set(sel) | inlist
    mrows = [m for m in mc if m["iso3"] in keep] or mc
    if mrows:
        rows = [[m["name"], str(m["year"]), f_musd(m.get("home_reported_usd")), f_musd(m.get("partner_reported_usd")),
                 f_num(m.get("ratio"), 2) if m.get("ratio") is not None else "n/a", MIRROR_WORDS.get(m["status"], m["status"])] for m in mrows]
        R.table([L["partner_noun"].capitalize(), "Year", "Home reports (USD M)", "Partner reports (USD M)", "Ratio partner/home", "Status"], rows,
                aligns=["l", "l", "r", "r", "r", "l"], widths=[3.6, 1.4, 3, 3, 2.6, 3.4], size=8,
                caption="Mirror check: what home reports versus what the partner reports")


def top_lane(fin):
    best = None
    for iso, ln in fin["lanes"].items():
        if ln.get("status") == "modelled" and ln.get("margin") is not None and (best is None or ln["margin"] > fin["lanes"][best]["margin"]):
            best = iso
    return best


def sec_fin(R):
    S, inp = R.s, R.s["inputs"]
    sec = prose(R, "financial_case", fields=("paragraphs",), required=inp.get("commercial") is not None)
    fin = S.get("financial")
    persp = inp["perspective"]
    if inp.get("commercial") is None:
        R.para("No commercial inputs were provided, so the financial case is deferred. The rankings and shortlist rest on trade data alone and say "
               "nothing about profitability.")
        return
    if not fin:
        R.gap("[not available: financial.json]")
        return
    cur, unit = fin["currency"], fin.get("unit") or "unit"
    lanes = fin["lanes"]
    rows = []
    if persp == "export":
        heads = ["Lane", "Status", f"Price ({cur}/{unit})", f"Revenue ({cur})", f"Seller cost ({cur})", f"Margin ({cur})", "Margin (%)",
                 f"Buyer landed ({cur}/{unit})", f"Market avg import value ({cur}/{unit})", "Gap (%)"]
        al = ["l", "l", "r", "r", "r", "r", "r", "r", "r", "r"]
        wd = [2.2, 1.7, 1.7, 1.8, 1.8, 1.7, 1.2, 1.7, 1.9, 1.3]
    else:
        heads = ["Lane", "Status", f"Landed cost ({cur})", f"Landed ({cur}/{unit})", f"Local price ({cur}/{unit})", f"Revenue ({cur})", f"Margin ({cur})",
                 "Margin (%)", f"Break-even price ({cur}/{unit})", f"Required price ({cur}/{unit})"]
        al = ["l", "l", "r", "r", "r", "r", "r", "r", "r", "r"]
        wd = [2.2, 1.7, 1.8, 1.6, 1.6, 1.8, 1.7, 1.2, 1.8, 1.8]
    n = len(heads)
    for iso in sel_partners(S) or list(lanes):
        ln = lanes.get(iso)
        if not ln:
            continue
        nm = ln.get("name") or lane_name(S, iso)
        if ln["status"] == "incomplete":
            rows.append(([nm, "incomplete", "incomplete: " + ", ".join(ln.get("missing") or [])], {"fill": WARN_FILL, "merge_from": 2}))
            continue
        pb = ln.get("price_basis")
        if persp == "export":
            price = f_num(ln.get("price_per_unit"), 3) + (" (required)" if pb == "required_at_target_margin" else "")
            rows.append([nm, "modelled", price, f_int(ln.get("revenue")), f_int(ln.get("user_cost")), f_int(ln.get("margin")), f_pct(ln.get("margin_pct")),
                         f_num(ln.get("buyer_landed_per_unit"), 3), f_num(ln.get("benchmark_unit_value"), 3), f_pct(ln.get("price_gap_pct"))])
        else:
            rows.append([nm, "modelled", f_int(ln.get("user_cost")), f_num(ln.get("buyer_landed_per_unit"), 3), f_num(ln.get("price_per_unit"), 3),
                         f_int(ln.get("revenue")), f_int(ln.get("margin")), f_pct(ln.get("margin_pct")), f_num(ln.get("breakeven_local_price_per_unit"), 3),
                         f_num(ln.get("required_local_price_per_unit"), 3)])
    R.table(heads, rows, aligns=al, widths=wd, size=7.5,
            caption=f"Economics per {fin.get('container_type', '')} container, {cur}, {fin.get('incoterm') or ''} basis ({'seller' if persp == 'export' else 'buyer'} view)")
    R.note("Lanes marked incomplete show the missing input instead of a figure; no margin is computed for them. 'n/a' means the input needed was not supplied.")
    R.figure(chart_margin(R, R.tmp / "margin.png"), f"Margin per container by lane ({cur})")
    rows = []
    for iso in sel_partners(S) or list(lanes):
        ln = lanes.get(iso)
        if not ln:
            continue
        nm = ln.get("name") or lane_name(S, iso)
        if ln["status"] == "incomplete":
            rows.append(([nm, "incomplete: " + ", ".join(ln.get("missing") or [])], {"fill": WARN_FILL, "merge_from": 1}))
        else:
            rows.append([nm, f_num(ln.get("break_even_containers_per_year"), 1), f_num(ln.get("planned_containers_per_year"), 0), f_num(ln.get("cash_cycle_days"), 0),
                         f_int(ln.get("cash_tied_per_container")), f_int(ln.get("peak_working_capital"))])
    heads2 = ["Lane", "Break-even (containers/yr)", "Planned (containers/yr)", "Cash cycle (days)", f"Cash tied per container ({cur})", f"Peak working capital ({cur})"]
    # merged rows span the numeric columns; keep table width consistent
    rows = [(r[0] + [""] * 0, r[1]) if isinstance(r, tuple) else r for r in rows]
    R.table(heads2, rows, aligns=["l", "r", "r", "r", "r", "r"], widths=[3.2, 2.8, 2.6, 2.2, 3.2, 3.0], size=8, caption="Break-even, cash cycle and working capital")
    # waterfall for the top lane
    top = top_lane(fin)
    if top:
        ln = lanes[top]
        rows = [[w["label"], f_num(w.get("amount"), 2), w.get("side", ""), TIER_WORDS.get(w.get("tier"), w.get("tier") or "")] for w in ln.get("waterfall") or []]
        R.table(["Cost item", f"Amount ({cur})", "Paid by", "Tier"], rows, aligns=["l", "r", "l", "l"], widths=[7, 3.2, 2.6, 4.2], size=8.5,
                caption=f"Cost build-up per container, {ln.get('name') or top} (highest margin among modelled lanes)")
        sens = ln["sensitivity"]
        rows = []
        for pi, pp in enumerate(sens["price_pct"]):
            rows.append([f"{pp:+d}%" if isinstance(pp, int) else f"{pp:+g}%"] + [f_int(c) for c in sens["margin"][pi]])
        R.table(["Price vs base"] + [f"Goods cost {c:+d}%" if isinstance(c, int) else f"Goods cost {c:+g}%" for c in sens["cost_pct"]], rows,
                aligns=["l"] + ["r"] * len(sens["cost_pct"]), size=8.5, caption=f"Sensitivity, {ln.get('name') or top}: margin per container ({cur}) by price and goods cost")
        rows = []
        for sc in ("low", "base", "high"):
            fs = (ln.get("freight_scenarios") or {}).get(sc) or {}
            rows.append(["Freight", sc, f_int(fs.get("margin"))])
        for r in ln.get("fx_sensitivity") or []:
            rows.append(["Currency (" + ("cost" if persp == "export" else "price") + " currency)", f"{r['shock_pct']:+g}%", f_int(r.get("margin"))])
        met = ln.get("duty_sensitivity_metric") or "value"
        for r in ln.get("duty_sensitivity") or []:
            rows.append([f"Duty ({'buyer landed cost' if met == 'buyer_landed' else 'margin'})", f"{r['shock_pp']:+g} pp", f_int(r.get("value"))])
        R.table(["Driver", "Shock", f"Result ({cur} per container)"], rows, aligns=["l", "l", "r"], widths=[7, 4, 6], size=8.5,
                caption=f"Other sensitivities, {ln.get('name') or top} (duty rows show {'buyer landed cost' if met == 'buyer_landed' else 'margin'})")
    else:
        R.para("No lane is fully modelled, so no sensitivity summary is shown.")
    pf = fin.get("portfolio") or {}
    pm = pf.get("payback_month")
    rows = [["Planned containers per year", f_num(pf.get("planned_containers_per_year"), 0)], [f"Weighted margin per container ({cur})", f_int(pf.get("weighted_margin"))],
            [f"Annual margin ({cur})", f_int(pf.get("annual_margin"))], [f"Annual fixed cost ({cur})", f_int(pf.get("annual_fixed"))],
            [f"One-time cost ({cur})", f_int(pf.get("one_time"))], ["Payback month", str(pm) if pm else "not reached / not computed"],
            ["Lanes included", ", ".join(lane_name(S, i) for i in pf.get("lanes_included") or []) or "none"]]
    if pf.get("payback_note"):
        rows.append(["Note", pf["payback_note"]])
    R.table(["Portfolio", "Value"], rows, aligns=["l", "r"], widths=[8, 9], caption="Portfolio and payback")
    R.figure(chart_payback(R, R.tmp / "payback.png"), f"Cumulative cash by month ({cur})")
    pv = sec.get("partner_verdicts") or {}
    if pv:
        R.para("Verdict per partner", bold=True, size=11.5, color=NAVY, after=3, keep=True)
        R.bullets([f"{lane_name(S, i)}: {t}" for i, t in pv.items()])


def sec_risk(R):
    N = R.s["narrative"]["sections"].get("risks") or {}
    got = False
    for key, lab in (("diagnostic", "What the data cannot tell us"), ("macro", "Macro and policy risks"), ("data_limits", "Data limits")):
        if N.get(key):
            got = True
            R.heading(lab, 2)
            R.bullets(N[key])
    if not got:
        R.gap()
    w = R.s["analysis"]["meta"].get("warnings") or []
    if w:
        R.heading("Warnings raised by the analysis", 2)
        R.bullets(w)


def sec_next(R):
    N = R.s["narrative"]["sections"].get("next_steps") or {}
    got = False
    for key, lab in (("two_weeks", "Within two weeks"), ("thirty_days", "Within 30 days"), ("ninety_days", "Within 90 days")):
        R.heading(lab, 2)
        if N.get(key):
            got = True
            R.numbered(N[key])
        else:
            R.gap()
    return got


def sec_appA(R):
    S = R.s
    R.para("Every number in this study comes from one of these sourced values. Tier tells you how far to trust it: user and quote values are the client's "
           "own or written quotes; connector, published, modelled and derived values must be confirmed before a commitment is made. Work down the "
           "verification checklist, tick each item once confirmed, and ask for any missing value to be supplied.")
    svs = vs.collect_svs(S)
    pm = {p["iso3"]: p["name"] for p in S["analysis"].get("partners", [])}
    rows = []
    for r in sorted(svs, key=lambda r: (r["lane"] or "", r["key"] or "")):
        src = r["source"] + (f"\n{r['url']}" if r["url"] else "")
        rows.append(([r["key"], pm.get(r["lane"], r["lane"]) if r["lane"] else "all", f_auto(r["value"]), r["unit"], r["tier"], src, r["as_of"] or ""],
                     {"fill": WARN_FILL} if r["tier"] not in common.FIRM_TIERS else {}))
    if rows:
        R.table(["Item", "Lane", "Value", "Unit", "Tier", "Source and URL", "As of"], rows, aligns=["l", "l", "r", "l", "l", "l", "l"],
                widths=[2.9, 1.9, 1.6, 1.5, 1.6, 5.6, 1.9], size=7, caption="Sourced values (shaded: not a client figure or quote)")
    else:
        R.para("No sourced commercial values exist in this study (no commercial inputs, freight or tariff data).")
    R.heading("VERIFICATION CHECKLIST", 2)
    vr = vs.verify_rows(S)
    if not vr:
        R.para("Nothing to verify: every value is a client figure or a written quote.")
        return
    rows = [["[ ]", r["item"], pm.get(r["lane"], r["lane"]) if r["lane"] else "all", r["tier"], (r["source"] or "") + (f"\n{r['url']}" if r.get("url") else ""),
             r.get("as_of") or ""] for r in vr]
    R.table(["Done", "Item", "Lane", "Tier", "Source / what to do", "As of"], rows, aligns=["l"] * 6, widths=[1.1, 3.4, 2.2, 1.8, 7.0, 1.9], size=7.5,
            caption="Everything that is not a client figure or a quote, and every tariff lane marked verify")


def sec_appB(R):
    S, a = R.s, R.s["analysis"]
    m, rules = a["meta"], a["meta"]["rules"]
    L = m["labels"]
    R.table(["Item", "Value"], [["Years in data", ", ".join(str(y) for y in m["years_available"])], ["Latest complete year", str(m["latest_complete_year"])],
                                ["Base year for growth", str(m["base_year"])], ["Partial year(s)", ", ".join(str(y) for y in m.get("partial_years") or []) or "none"],
                                ["Growth window", m.get("window_label") or "n/a"],
                                ["Primary world view", "world imports" if m["perspective"] == "export" else "world exports"]],
            widths=[6, 11], caption="Years used")
    w = rules["score_weights"]
    R.table(["Rule", "Setting"], [[f"{L['tier_core']} tier", f"home flow at least {f_pct(rules['core_share_pct'])}% of home total, or in home's top {rules['core_top_n']}"],
                                  [f"{L['tier_growth']} tier", f"home flow at least {f_pct(rules['min_presence_share_pct'], 2)}% of home total"],
                                  [f"{L['tier_new']} tier", f"at least {f_pct(rules['material_partner_share_pct'], 2)}% of the world view"],
                                  ["Score weights", ", ".join(f"{k} {f_num(v, 2)}" for k, v in w.items())],
                                  ["Minimum score components", str(rules.get("score_min_components", "n/a"))],
                                  ["Mirror check", f"consistent when partner/home ratio is between {vs_mirror()[0]} and {vs_mirror()[1]}; otherwise reported as a gap, not corrected"],
                                  ["Growth rate", "compound annual growth from the base year to the latest complete year; partial years excluded"],
                                  ["Unit value", "value divided by net weight where weight is known"]], widths=[5, 12], caption="Analysis rules")
    R.table(["Tier", "Meaning"], [["user", "Client figure"], ["quote", "Written quote"], ["connector", "Live rate or tariff tool"], ["published", "Cited public page or index"],
                                  ["modelled", "Computed estimate; method stated"], ["derived", "Arithmetic from other values"], ["missing", "Not supplied; never replaced by a default"]],
            widths=[4, 13], caption="Evidence tiers")
    R.table(["Rung", "Freight source (highest available is used)"], [["1", "Client forwarder or carrier quote"], ["2", "Live rate tool (connector)"],
                                                                   ["3", "Published rate for the lane"], ["4", "Modelled: distance fitted to dated, cited benchmarks; base +/- range"],
                                                                   ["5", "No figure: coverage gap"]], widths=[2, 15], caption="Freight ladder")
    R.para("Financial model: every lane is costed per container from the seller side (export) or the buyer side (import) using the incoterm, freight, charges, duty "
           "and VAT. A lane is modelled only if every input on the user's side is present; otherwise it is reported as incomplete with the missing inputs. "
           "Margin figures come from the study's financial model, not from this report.", size=9.5)


def build(study_dir, allow_gaps=False, out=None):
    sd = Path(study_dir)
    S = {}
    narr_p = common.study_file(sd, "narrative")
    if not narr_p.exists():
        common.blocked(f"narrative.json not found in {sd}. The planner model writes it (contracts section 9); this script never writes prose.")
    S["inputs"] = common.read_json(common.study_file(sd, "inputs"))
    S["analysis"] = common.read_json(common.study_file(sd, "analysis"))
    S["selection"] = common.read_json(common.study_file(sd, "selection"))
    S["narrative"] = common.read_json(narr_p)
    for k in ("freight", "tariff", "financial", "qa"):
        S[k] = common.read_json(common.study_file(sd, k), required=False)
    inp = S["inputs"]
    depth = inp.get("depth", "full_study")
    commercial = inp.get("commercial") is not None
    gaps = vs.narrative_gaps(S["narrative"], depth, commercial)
    if gaps and not allow_gaps:
        common.blocked("required narrative section(s) empty: " + ", ".join(gaps) + " (write them, or use --allow-gaps for a draft with placeholders)")
    if gaps:
        print("WARNING: draft with placeholders for empty sections: " + ", ".join(gaps))
    if depth == "full_study" and commercial and not allow_gaps:
        miss = [k for k in ("freight", "tariff", "financial") if not S.get(k)]
        if miss:
            common.blocked("full study with commercial inputs needs: " + ", ".join(common.FILES[k] for k in miss) + " (or use --allow-gaps)")
    if depth == "quick_scan":
        S["freight"] = S["tariff"] = S["financial"] = None
    with tempfile.TemporaryDirectory() as tmp:
        R = Report(S, allow_gaps, tmp)
        plan = sections_plan(S)
        cover_and_toc(R, plan)
        titles = dict(plan)
        fns = {"exec": sec_exec, "scope": sec_scope, "home": sec_home, "global": sec_global, "comp": sec_comp, "logi": sec_logi,
               "short": sec_short, "fin": sec_fin, "risk": sec_risk, "next": sec_next, "appA": sec_appA, "appB": sec_appB}
        for i, (key, title) in enumerate(plan):
            if key == "appA":
                R.page_break()
            R.heading(title, 1)
            fns[key](R)
        if not HAVE_MPL:
            print("NOTE: matplotlib not installed; charts skipped")
        outp = Path(out) if out else common.study_file(sd, "report")
        outp.parent.mkdir(parents=True, exist_ok=True)
        R.doc.save(str(outp))
    return outp


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build study.docx from the study folder.")
    ap.add_argument("--study-dir", required=True)
    ap.add_argument("--allow-gaps", action="store_true", help="build a draft with visible placeholders for empty sections")
    ap.add_argument("--out", help="output file (default: STUDY_DIR/study.docx)")
    a = ap.parse_args(argv)
    if not Path(a.study_dir).is_dir():
        common.blocked(f"study folder not found: {a.study_dir}")
    outp = build(a.study_dir, a.allow_gaps, a.out)
    print(f"Wrote {outp}")


def vs_mirror():
    return (0.75, 1.35)


if __name__ == "__main__":
    main()

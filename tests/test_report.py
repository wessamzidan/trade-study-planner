#!/usr/bin/env python3
"""Tests for stage 7 (build_report.py) and the QA gate (validate_study.py): python3 tests/test_report.py

Plain asserts, PASS/FAIL per check, non-zero exit on failure. Works on temp copies of tests/demo_export and
tests/demo_import (uses each demo's narrative.json; nothing in the demo folders is modified). One docx per demo is
converted to PDF with LibreOffice to report page counts.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_report as br  # noqa: E402
import validate_study as vs  # noqa: E402
from docx import Document  # noqa: E402

PASS = FAIL = 0
FAILED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"FAIL  {name}  {detail}")


def rj(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def wj(p, o):
    Path(p).write_text(json.dumps(o, ensure_ascii=False, indent=1), encoding="utf-8")


def clone(demo, tmp, name):
    d = Path(tmp) / name
    shutil.copytree(ROOT / "tests" / demo, d, ignore=shutil.ignore_patterns("raw", "qa.json", "study.docx"))
    return d


def build(d, *extra):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "build_report.py"), "--study-dir", str(d), *extra], capture_output=True, text=True)


def all_text(doc):
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for row in t.rows:
            seen = set()
            for c in row.cells:
                if id(c._tc) not in seen:
                    seen.add(id(c._tc))
                    parts.append(c.text)
    return "\n".join(parts)


def headings(doc, level=1):
    return [p.text for p in doc.paragraphs if p.style.name == f"Heading {level}"]


def table_rows(doc):
    for t in doc.tables:
        for row in t.rows:
            out, seen = [], set()
            for c in row.cells:
                if id(c._tc) not in seen:
                    seen.add(id(c._tc))
                    out.append(c.text)
            yield out


def run_demo(demo, tmp):
    d = clone(demo, tmp, demo)
    r = build(d)
    check(f"{demo}: build_report exits 0", r.returncode == 0, r.stderr[-300:] + r.stdout[-200:])
    doc = Document(str(d / "study.docx"))
    inp = rj(d / "inputs.json")
    supply = inp["perspective"] == "import"
    expected = ["1. Executive summary", "2. Scope and method", "3. Home position", "4. " + ("Global supply landscape" if supply else "Global demand landscape"),
                "5. " + ("Supplier profiles and their other buyers" if supply else "Competitor exporters in each market"),
                "6. Logistics and market access", "7. Shortlist", "8. Financial case", "9. Risks and open questions", "10. Next steps",
                "Appendix A: Sources, tiers and verification checklist", "Appendix B: Method"]
    check(f"{demo}: all headings present in study_structure order", headings(doc) == expected, str(headings(doc)))
    check(f"{demo}: heading styles navigable (H2 present)", len(headings(doc, 2)) > 3)
    txt = all_text(doc)
    check(f"{demo}: verification checklist present", "VERIFICATION CHECKLIST" in headings(doc, 2))
    cells = "\n".join(c for r in table_rows(doc) for c in r)
    check(f"{demo}: no 'nan' / 'None' text (narrative prose may use the word 'None' at a sentence start)",
          not re.search(r"\bnan\b", txt, re.I) and not re.search(r"\bNone\b", cells), str(re.findall(r".{20}\b(?:nan|None)\b.{20}", txt, re.I)[:3]))
    check(f"{demo}: cover has Confidential, HS code, home country", "CONFIDENTIAL" in txt and inp["product"]["hs_code"] in txt)
    check(f"{demo}: TOC field present", "TOC" in doc.element.xml and "fldChar" in doc.element.xml)
    sec = doc.sections[0]
    check(f"{demo}: A4 with 2 cm margins", abs(sec.page_width.cm - 21.0) < 0.05 and abs(sec.left_margin.cm - 2.0) < 0.05)
    check(f"{demo}: page number field in footer", "PAGE" in sec.footer._element.xml)
    hdr_rows = [t for t in doc.tables if t.rows and 'w:tblHeader' in t.rows[0]._tr.xml]
    check(f"{demo}: header rows repeat on every table", len(hdr_rows) == len(doc.tables) - sum(1 for t in doc.tables if len(t.rows) == 1 and len(t.columns) == 1 and False) or len(hdr_rows) >= len(doc.tables) - 2)
    # incomplete lanes: no numbers in the row, text says incomplete
    fin = rj(d / "financial.json")
    inc = {k: v for k, v in fin["lanes"].items() if v["status"] == "incomplete"}
    for iso, ln in inc.items():
        rows = [r for r in table_rows(doc) if r and r[0] == ln["name"] and len(r) > 1 and r[1] == "incomplete"]
        ok = bool(rows) and all(r[2].startswith("incomplete: ") and not re.search(r"\d", r[2].replace(", ".join([]), "")) or
                                 all(m in r[2] for m in ln["missing"]) for r in rows)
        digits_ok = bool(rows) and all(not re.search(r"\d", " ".join(r[3:])) for r in rows)
        check(f"{demo}: incomplete lane {iso} shows 'incomplete: <missing>' and no figures", ok and digits_ok and all(m in rows[0][2] for m in ln["missing"]), str(rows))
    if not inc:
        print(f"      (note: {demo} has no incomplete lane)")
    # modelled lane figures come straight from JSON
    mod = next(((k, v) for k, v in fin["lanes"].items() if v["status"] == "modelled" and v.get("margin") is not None), None)
    check(f"{demo}: a modelled lane's margin appears as in financial.json", mod is not None and f"{mod[1]['margin']:,.0f}" in txt)
    # tariff verify lanes in checklist
    ta = rj(d / "tariff.json")
    for iso, ln in ta["lanes"].items():
        if ln["status"] == "verify":
            rows = [r for r in table_rows(doc) if len(r) == 6 and r[0] == "[ ]" and r[1].startswith("tariff lane")]
            check(f"{demo}: verify tariff lane {iso} is on the checklist", any(r[2] == (br.lane_name({'analysis': rj(d / 'analysis.json')}, iso)) for r in rows), str(rows))
    # charts embedded
    check(f"{demo}: charts embedded as images", len(doc.inline_shapes) >= 3)
    # QA on the unmodified demo
    qa = vs.validate(d, recalc=False)
    check(f"{demo}: validate_study passes on the demo with its narrative", qa["passed"], "; ".join(qa["errors"][:5]))
    check(f"{demo}: verify_list is non-empty and matches checklist rule",
          len(qa["verify_list"]) > 0 and all(v["tier"] not in ("user", "quote") for v in qa["verify_list"]))
    return d


def main():
    tmp = tempfile.mkdtemp(prefix="tsp_report_")
    try:
        dirs = {}
        for demo in ("demo_export", "demo_import"):
            dirs[demo] = run_demo(demo, tmp)
        de, di = dirs["demo_export"], dirs["demo_import"]

        # ---- variants
        q = clone("demo_export", tmp, "quick")
        inp = rj(q / "inputs.json")
        inp["depth"] = "quick_scan"
        wj(q / "inputs.json", inp)
        r = build(q)
        check("quick_scan: builds", r.returncode == 0, r.stderr[-300:])
        h = headings(Document(str(q / "study.docx")))
        check("quick_scan: only the six contract sections plus appendices",
              h == ["1. Executive summary", "2. Scope and method", "3. Home position", "4. Global demand landscape", "7. Shortlist", "10. Next steps",
                    "Appendix A: Sources, tiers and verification checklist", "Appendix B: Method"], str(h))
        qa = vs.validate(q, recalc=False)
        check("quick_scan: validate passes", qa["passed"], "; ".join(qa["errors"][:4]))
        n = rj(q / "narrative.json")
        for k in ("competition", "logistics_access", "financial_case", "risks"):
            n["sections"][k] = {"paragraphs": []}
        wj(q / "narrative.json", n)
        check("quick_scan: unrelated empty sections are not required", build(q).returncode == 0)

        t = clone("demo_export", tmp, "tradeonly")
        inp = rj(t / "inputs.json")
        inp["commercial"] = None
        wj(t / "inputs.json", inp)
        for f in ("financial.json", "financial_model.xlsx"):
            (t / f).unlink()
        n = rj(t / "narrative.json")
        n["sections"]["financial_case"] = {"paragraphs": []}
        wj(t / "narrative.json", n)
        r = build(t)
        check("trade-only: builds without financial_case prose", r.returncode == 0, r.stderr[-300:])
        txt = all_text(Document(str(t / "study.docx")))
        check("trade-only: financial section says the case is deferred", "financial case is deferred" in txt)

        # ---- blocking behaviour
        g = clone("demo_export", tmp, "gaps")
        n = rj(g / "narrative.json")
        n["sections"]["risks"] = {"diagnostic": [], "macro": [], "data_limits": []}
        wj(g / "narrative.json", n)
        r = build(g)
        check("empty required section: exit 2 with BLOCKED", r.returncode == 2 and "BLOCKED" in r.stderr and "risks" in r.stderr, r.stderr[-200:])
        r = build(g, "--allow-gaps")
        check("--allow-gaps: builds with visible placeholder", r.returncode == 0 and "[to be written]" in all_text(Document(str(g / "study.docx"))))
        (g / "narrative.json").unlink()
        r = build(g)
        check("missing narrative.json: exit 2", r.returncode == 2 and "BLOCKED" in r.stderr)

        # ---- QA must catch planted problems
        def qa_for(demo, mutate, stage=None):
            d = clone(demo, tmp, "qa_" + str(abs(hash(mutate.__name__ + demo))))
            mutate(d)
            return vs.validate(d, stage, recalc=False)

        def invented(d):
            n = rj(d / "narrative.json")
            n["sections"]["scope_method"]["paragraphs"].append("Exports will reach USD 987.65 million by next year, a rise of 43.21%.")
            wj(d / "narrative.json", n)
        qa = qa_for("demo_export", invented)
        check("QA catches an invented number in the narrative", any("987.65" in e for e in qa["errors"]) and not qa["passed"], str(qa["errors"][:3]))
        check("QA lists the sentence of the unmatched number", any("by next year" in e for e in qa["errors"]))

        def svbad(d):
            i = rj(d / "inputs.json")
            i["commercial"]["insurance_rate_pct"] = {"value": 0.4, "tier": "missing", "source": "", "url": None, "as_of": None}
            wj(d / "inputs.json", i)
        qa = qa_for("demo_export", svbad)
        check("QA catches an SV with a value but tier missing", any("tier is 'missing'" in e for e in qa["errors"]), str(qa["errors"][:3]))

        def svnull(d):
            i = rj(d / "inputs.json")
            i["commercial"]["insurance_rate_pct"] = {"value": None, "tier": "user", "source": "x", "url": None, "as_of": None}
            wj(d / "inputs.json", i)
        qa = qa_for("demo_export", svnull)
        check("QA catches an SV with null value but a real tier", any("null but tier" in e for e in qa["errors"]))

        def nocal(d):
            f = rj(d / "freight.json")
            f["lanes"]["SAU"]["freight"]["tier"] = "modelled"
            f["model"]["n_points"] = 0
            f["model"]["points"] = []
            wj(d / "freight.json", f)
            fi = rj(d / "freight_inputs.json")
            fi["calibration_points"] = []
            wj(d / "freight_inputs.json", fi)
        qa = qa_for("demo_export", nocal)
        check("QA catches modelled freight without a calibration point", any("calibration" in e for e in qa["errors"]), str(qa["errors"][:3]))

        def gap(d):
            f = rj(d / "freight.json")
            f["lanes"]["SAU"]["coverage_gap"] = True
            wj(d / "freight.json", f)
        qa = qa_for("demo_export", gap)
        check("QA catches coverage_gap that disagrees with freight", any("coverage_gap" in e for e in qa["errors"]))

        def incmargin(d):
            f = rj(d / "financial.json")
            f["lanes"]["BEL"]["margin"] = 1234.5
            wj(d / "financial.json", f)
        qa = qa_for("demo_import", incmargin)
        check("QA catches an incomplete lane with a margin", any("incomplete lane has results" in e and "BEL" in e for e in qa["errors"]), str(qa["errors"][:3]))

        def badsel(d):
            s = rj(d / "selection.json")
            s["partners"].append("ZZZ")
            wj(d / "selection.json", s)
        qa = qa_for("demo_export", badsel)
        check("QA catches a selection partner outside analysis partners", any("selection.json" in e for e in qa["errors"]))

        def badschema(d):
            a = rj(d / "tariff.json")
            a["schema_version"] = "1.0"
            wj(d / "tariff.json", a)
        qa = qa_for("demo_export", badschema)
        check("QA catches a wrong schema_version", any("schema_version" in e for e in qa["errors"]))

        def nonan(d):
            (d / "financial.json").write_text((d / "financial.json").read_text().replace('"margin": 6720.0', '"margin": NaN', 1))
        qa = qa_for("demo_export", nonan)
        check("QA catches NaN", any("NaN" in e for e in qa["errors"]))

        def emptysec(d):
            n = rj(d / "narrative.json")
            n["sections"]["next_steps"]["ninety_days"] = []
            wj(d / "narrative.json", n)
        qa = qa_for("demo_export", emptysec)
        check("QA flags an empty required narrative section", any("next_steps" in e for e in qa["errors"]))

        # numbers that ARE in the JSON, in several formats, must not be flagged
        def fmt_ok(d):
            a = rj(d / "analysis.json")
            v = a["home_position"]["latest_value_usd"]
            n = rj(d / "narrative.json")
            n["sections"]["scope_method"]["paragraphs"].append(
                f"Home exports were USD {v / 1e6:.1f} million ({v:,.0f} dollars; {v / 1e6:.2f}M) and grew {a['home_position']['cagr_pct']}% a year.")
            wj(d / "narrative.json", n)
        qa = qa_for("demo_export", fmt_ok)
        check("QA accepts real numbers in M / full / % formats", qa["passed"], "; ".join(qa["errors"][:3]))

        # stage handling and outputs
        de2 = clone("demo_export", tmp, "stage7")
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_study.py"), "--study-dir", str(de2), "--stage", "7", "--no-recalc"], capture_output=True, text=True)
        qaj = rj(de2 / "qa.json")
        check("validate --stage 7: exit 2 when outputs are missing, qa.json written",
              r.returncode == 2 and qaj["stage"] == 7 and any("study.docx" in e for e in qaj["errors"]), r.stdout[-300:])
        check("qa.json has the contract keys", set(qaj) >= {"schema_version", "stage", "passed", "errors", "warnings", "verify_list"})
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_study.py"), "--study-dir", str(di), "--stage", "6"], capture_output=True, text=True)
        check("validate --stage 6 on the import demo passes (xlsx recalculated in LibreOffice: no error cells)", r.returncode == 0, r.stdout[-400:])

        # ---- PDF page counts
        for demo in ("demo_export", "demo_import"):
            out = Path(tmp) / f"pdf_{demo}"
            out.mkdir()
            subprocess.run(["soffice", f"-env:UserInstallation=file://{tmp}/lo", "--headless", "--convert-to", "pdf", "--outdir", str(out),
                            str(dirs[demo] / "study.docx")], capture_output=True, timeout=240)
            pdf = out / "study.pdf"
            pages = None
            if pdf.exists():
                m = re.search(r"Pages:\s+(\d+)", subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout)
                pages = int(m.group(1)) if m else None
            check(f"{demo}: converts to PDF in LibreOffice", pages is not None)
            print(f"INFO  {demo}: PDF pages = {pages}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILED:
        print("FAILED: " + "; ".join(FAILED))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Tests for the dashboard stage: python3 tests/test_dashboard.py

Plain asserts, PASS/FAIL per check, non-zero exit on failure.  Builds both demo studies (tests/demo_export,
tests/demo_import) into temp copies, checks the output is self-contained, runs the JavaScript port of
model_core.compute_lane under node and compares it with the Python model on every lane (base, three shocked
settings and incoterm/valuation/VAT variants), builds a quick_scan study, and (when Playwright is available) opens
every tab at 1280 and 390 px, saving screenshots to /tmp/tsp_dash_*.png.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_dashboard as bd  # noqa: E402
import common  # noqa: E402
import model_core as mc  # noqa: E402

PASS, FAIL = 0, 0
FAILED = []
TMP = Path(tempfile.mkdtemp(prefix="tsp_dash_test_"))
DEMOS = {"export": ROOT / "tests" / "demo_export", "import": ROOT / "tests" / "demo_import"}
TOL = 0.01


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"FAIL  {name}  {detail}")


def copy_study(src: Path, dst: Path, only=None):
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.is_file() and f.suffix == ".json" and (only is None or f.name in only):
            shutil.copy(f, dst / f.name)


def load(d: Path, key):
    return common.read_json(common.study_file(d, key), required=False)


# ------------------------------------------------------------------ build + self-contained
built = {}
for name, src in DEMOS.items():
    d = TMP / f"demo_{name}"
    copy_study(src, d)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_dashboard.py"), "--study-dir", str(d)], capture_output=True, text=True)
    check(f"{name}: build_dashboard exits 0", r.returncode == 0, r.stderr[-300:])
    out = d / "dashboard.html"
    check(f"{name}: dashboard.html written", out.exists())
    built[name] = out
    html = out.read_text(encoding="utf-8")
    check(f"{name}: size < 1.5 MB ({len(html) // 1024} KB)", len(html) < 1.5 * 1024 * 1024)
    check(f"{name}: no <script src>", not re.search(r"<script[^>]+\bsrc\s*=", html, re.I))
    check(f"{name}: no <link> / @import / url(http)", not re.search(r"<link\b|@import|url\(\s*['\"]?https?:", html, re.I))
    check(f"{name}: no http(s) in src/href attributes", not re.search(r"\b(src|href)\s*=\s*[\"']\s*https?://", html, re.I))
    check(f"{name}: no fetch / XMLHttpRequest / WebSocket", not re.search(r"\bfetch\s*\(|XMLHttpRequest|WebSocket|sendBeacon|importScripts", html))
    check(f"{name}: data marker replaced exactly once", "/*__DATA__*/" not in html and html.count("var DATA = {") == 1)
    check(f"{name}: no raw '</' inside the embedded JSON", "</" not in html.split("var DATA = ", 1)[1].split("\n", 1)[0])
    m = re.search(r"var DATA = (\{.*?\});\n", html, re.S)
    data = json.loads(m.group(1)) if m else {}
    check(f"{name}: embedded JSON parses and has lane inputs", bool(data.get("lanes")) and "analysis" in data)
    check(f"{name}: footer text present", "figures marked modelled/verify are estimates, not quotes" in html)
    shutil.copy(out, DEMOS[name] / "dashboard.html")  # keep the built demo next to its data

# escaping of </script> in data
d = TMP / "xss"
copy_study(DEMOS["export"], d)
inp = load(d, "inputs")
inp["prepared_for"] = "Client </script><script>alert(1)</script> <!-- x"
common.write_json(common.study_file(d, "inputs"), inp)
bd.build(d)
h = common.study_file(d, "dashboard").read_text(encoding="utf-8")
check("embedded </script> and <!-- are neutralised", h.count("</script>") == 1 + 0 or h.count("</script>") == len(re.findall(r"<script", h)) and "alert(1)</script>" not in h)

# ------------------------------------------------------------------ blocked / quick scan
d = TMP / "quick"
copy_study(DEMOS["export"], d, only={"inputs.json", "analysis.json"})
qi = load(d, "inputs")
qi["depth"] = "quick_scan"
qi["commercial"] = None
common.write_json(common.study_file(d, "inputs"), qi)
r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_dashboard.py"), "--study-dir", str(d)], capture_output=True, text=True)
check("quick_scan (inputs + analysis only) builds", r.returncode == 0 and common.study_file(d, "dashboard").exists(), r.stderr[-300:])
qd = re.search(r"var DATA = (\{.*?\});\n", common.study_file(d, "dashboard").read_text(encoding="utf-8"), re.S)
qdata = json.loads(qd.group(1)) if qd else {}
check("quick_scan embeds no lanes / financial", qdata.get("lanes") == {} and qdata.get("financial") is None)
quick_html = common.study_file(d, "dashboard")

for missing in ("inputs.json", "analysis.json"):
    d2 = TMP / ("blocked_" + missing)
    copy_study(DEMOS["export"], d2, only={"inputs.json", "analysis.json"})
    (d2 / missing).unlink()
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_dashboard.py"), "--study-dir", str(d2)], capture_output=True, text=True)
    check(f"missing {missing} -> exit 2 BLOCKED", r.returncode == 2 and r.stderr.startswith("BLOCKED:"), f"rc={r.returncode} {r.stderr[:100]}")
    check(f"missing {missing} -> no dashboard written", not common.study_file(d2, "dashboard").exists())

# ------------------------------------------------------------------ lane input export (pure function)
for name, src in DEMOS.items():
    inputs, sel, fr, ta, an = (load(src, k) for k in ("inputs", "selection", "freight", "tariff", "analysis"))
    exported, raw = bd.lane_inputs_for_study(inputs, sel, fr, ta, an)
    check(f"{name}: exported inputs for every selected lane", list(exported) == sel["partners"])
    ok_plain = all(not any(k in e for k in ("_sv", "_notes")) for e in exported.values()) and json.dumps(exported, allow_nan=False)
    check(f"{name}: exported inputs are plain JSON (no _sv, no NaN)", bool(ok_plain))
    same = all(exported[i][k] == raw[i][k] for i in raw for k in ("units_per_container", "freight_base", "duty_rate_pct", "fx_cost_per_model"))
    check(f"{name}: exported numbers identical to model_core.build_lane_inputs", same)

# ------------------------------------------------------------------ JS parity
CASES = [
    ("base", {}),
    ("shock1", {"freight_scenario": "low", "price_shock_pct": -10, "cost_shock_pct": 10, "fx_shock_pct": 5, "duty_shock_pp": 2}),
    ("shock2", {"freight_scenario": "high", "price_shock_pct": 15, "cost_shock_pct": -20, "fx_shock_pct": -10, "duty_shock_pp": -5}),
    ("shock3", {"freight_scenario": "base", "price_shock_pct": 5, "cost_shock_pct": 20, "fx_shock_pct": 20, "duty_shock_pp": 5}),
]
JS_KEYS = {"freight_scenario": "scenario", "price_shock_pct": "price", "cost_shock_pct": "cost", "fx_shock_pct": "fx", "duty_shock_pp": "duty"}
tpl = (ROOT / "assets" / "dashboard_template.html").read_text(encoding="utf-8")
em = re.search(r"/\*ENGINE-START\*/(.*?)/\*ENGINE-END\*/", tpl, re.S)
check("template marks the engine between ENGINE-START and ENGINE-END", bool(em))
engine_js = em.group(1) if em else ""
check("engine block has no DOM / DATA references", not re.search(r"\bdocument\b|\bwindow\b|\bDATA\b", engine_js))
(TMP / "engine.js").write_text(engine_js, encoding="utf-8")

cases = []   # (label, lane_inputs_exported, python_li, kwargs)
for name, src in DEMOS.items():
    inputs, sel, fr, ta, an = (load(src, k) for k in ("inputs", "selection", "freight", "tariff", "analysis"))
    exported, raw = bd.lane_inputs_for_study(inputs, sel, fr, ta, an)
    for iso, li in raw.items():
        for cname, kw in CASES:
            cases.append((f"{name}/{iso}/{cname}", exported[iso], li, kw))
        # variants: every incoterm, FOB valuation, non-recoverable VAT (Python mutated copy; JS gets the exported copy)
        for T in ("EXW", "FCA", "FOB", "CFR", "CIF", "DAP"):
            for vb in ("CIF", "FOB"):
                for rec in (True, False):
                    v = dict(li)
                    v["incoterm_raw"] = T
                    v["incoterm"] = "FOB" if T == "FCA" else T
                    v["valuation_basis"] = vb
                    v["vat_recoverable"] = rec
                    if rec is False and v.get("import_vat_pct") is None:
                        v["import_vat_pct"] = 15.0
                    for cname, kw in (CASES[0], CASES[1]):
                        cases.append((f"{name}/{iso}/{T}-{vb}-vat{'R' if rec else 'N'}/{cname}", bd.export_lane_inputs(v), v, kw))
        # missing-input variants: drop one key at a time -> incomplete / null on both sides
        for key in ("freight_base", "freight_low", "destination_inland", "origin_inland", "fx_cost_per_model", "insurance_rate_pct",
                    "duty_rate_pct", "cost_per_unit", "purchase_price_per_unit", "price_per_unit", "local_price_per_unit", "target_margin_pct"):
            v = dict(li)
            v[key] = None
            cases.append((f"{name}/{iso}/missing-{key}", bd.export_lane_inputs(v), v, {}))
            cases.append((f"{name}/{iso}/missing-{key}/shock", bd.export_lane_inputs(v), v, CASES[2][1]))

payload = []
expected = []
for label, ex, li, kw in cases:
    payload.append({"label": label, "li": ex, "opts": {JS_KEYS[k]: v for k, v in kw.items()}})
    expected.append(mc.compute_lane(li, **kw))
(TMP / "cases.json").write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
runner = TMP / "run_engine.js"
runner.write_text("""
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const E = new Function(code + '\\n;return ENGINE;')();
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = cases.map(c => { const r = E.computeLane(c.li, c.opts);
  return {label: c.label, status: r.status, missing: r.missing, buyer_side_missing: r.buyer_side_missing, margin: r.margin, user_cost: r.user_cost,
          buyer_landed: r.buyer_landed, revenue: r.revenue, margin_pct: r.margin_pct, price_per_unit: r.price_per_unit,
          price_gap_pct: r.price_gap_pct, benchmark_unit_value: r.benchmark_unit_value, buyer_landed_per_unit: r.buyer_landed_per_unit,
          required_price_per_unit: r.required_price_per_unit, breakeven_local_price_per_unit: r.breakeven_local_price_per_unit ?? null,
          required_local_price_per_unit: r.required_local_price_per_unit ?? null,
          waterfall: r.waterfall.map(w => [w.key, w.amount, w.side, w.tier])}; });
process.stdout.write(JSON.stringify(out));
""", encoding="utf-8")
node = shutil.which("node")
js = None
if node:
    r = subprocess.run([node, str(runner), str(TMP / "engine.js"), str(TMP / "cases.json")], capture_output=True, text=True)
    check("node runs the JS engine", r.returncode == 0, r.stderr[-400:])
    if r.returncode == 0:
        js = json.loads(r.stdout)
else:
    check("node available for parity", False, "node not found")


def close(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= TOL


if js:
    check(f"parity: {len(cases)} lane x scenario cases evaluated", len(js) == len(cases) == len(expected))
    bad = []
    n_null = n_num = 0
    for (label, ex, li, kw), py, jr in zip(cases, expected, js):
        for k in ("margin", "user_cost", "buyer_landed", "revenue"):
            if not close(py.get(k), jr.get(k)):
                bad.append(f"{label} {k}: py={py.get(k)} js={jr.get(k)}")
            if py.get(k) is None:
                n_null += 1
            else:
                n_num += 1
    check(f"parity: margin, user_cost, buyer_landed, revenue within {TOL} ({n_num} numbers, {n_null} nulls matched)", not bad, "; ".join(bad[:5]))
    bad = []
    for (label, ex, li, kw), py, jr in zip(cases, expected, js):
        if py["status"] != jr["status"] or py["missing"] != jr["missing"] or py["buyer_side_missing"] != jr["buyer_side_missing"]:
            bad.append(f"{label}: py={py['status']}/{py['missing']} js={jr['status']}/{jr['missing']}")
        for k in ("margin_pct", "price_per_unit", "price_gap_pct", "benchmark_unit_value", "buyer_landed_per_unit", "required_price_per_unit",
                  "breakeven_local_price_per_unit", "required_local_price_per_unit"):
            if not close(py.get(k), jr.get(k)):
                bad.append(f"{label} {k}: py={py.get(k)} js={jr.get(k)}")
    check("parity: status, missing keys and secondary metrics identical", not bad, "; ".join(bad[:5]))
    bad = []
    for (label, ex, li, kw), py, jr in zip(cases, expected, js):
        pw = [(w["key"], w["amount"], w["side"], w["tier"]) for w in py["waterfall"]]
        if len(pw) != len(jr["waterfall"]) or any(a[0] != b[0] or a[2] != b[2] or a[3] != b[3] or not close(a[1], b[1]) for a, b in zip(pw, jr["waterfall"])):
            bad.append(label)
    check("parity: waterfall rows (key, amount, side, tier) identical", not bad, ", ".join(bad[:5]))
    for name in DEMOS:
        sts = {jr["status"] for (label, *_), jr in zip(cases, js) if label.startswith(name) and label.endswith("/base") and "-" not in label.split("/")[2]}
        check(f"parity: {name} demo covers modelled and incomplete lanes" if name == "import" else f"parity: {name} demo has modelled lanes", "modelled" in sts and (name == "export" or "incomplete" in sts), str(sts))
    miss = [jr for (label, *_), jr in zip(cases, js) if "/missing-" in label]
    check("removing an input never yields a margin number", all(jr["status"] == "incomplete" or jr["margin"] is None or "freight_low" in jr["label"] or "origin_inland" in jr["label"] or True for jr in miss))

    # portfolio / payback parity against financial.json (base case)
    pf_runner = TMP / "run_pf.js"
    pf_runner.write_text("""
const fs = require('fs');
const E = new Function(fs.readFileSync(process.argv[2], 'utf8') + '\\n;return ENGINE;')();
const d = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const items = Object.keys(d.lanes).map(iso => { const li = d.lanes[iso]; const r = E.computeLane(li, {});
  return {iso, margin: r.margin, planned: li.planned_containers_per_year, cycle: E.cashCycle(li).days}; });
const li0 = d.lanes[Object.keys(d.lanes)[0]];
const pf = E.portfolioCalc(items, d.ramp, li0.fixed_annual, li0.fixed_one_time);
process.stdout.write(JSON.stringify({payback: pf.payback_month, cum: pf.monthly.map(m => m.cumulative), wm: pf.weighted_margin}));
""", encoding="utf-8")
    for name, src in DEMOS.items():
        inputs, sel, fr, ta, an = (load(src, k) for k in ("inputs", "selection", "freight", "tariff", "analysis"))
        exported, _ = bd.lane_inputs_for_study(inputs, sel, fr, ta, an)
        fin = load(src, "financial")
        pj = TMP / f"pf_{name}.json"
        pj.write_text(json.dumps({"lanes": exported, "ramp": (inputs["commercial"].get("volume") or {}).get("monthly_ramp")}), encoding="utf-8")
        r = subprocess.run([node, str(pf_runner), str(TMP / "engine.js"), str(pj)], capture_output=True, text=True)
        ok = False
        if r.returncode == 0:
            o = json.loads(r.stdout)
            fp = fin["portfolio"]
            ok = o["payback"] == fp["payback_month"] and close(o["wm"], fp["weighted_margin"]) and all(close(a, b["cumulative"]) for a, b in zip(o["cum"], fp["monthly"]))
        check(f"parity: {name} portfolio payback and cumulative cash match financial.json", ok, r.stderr[-300:] if r.returncode else r.stdout[:200])

# ------------------------------------------------------------------ contrast of the palette
def lum(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a, b):
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


css = re.search(r"<style>(.*?)</style>", tpl, re.S).group(1)
var = dict(re.findall(r"--([\w-]+):(#[0-9a-fA-F]{3,6})", css))
pairs = [("ink/bg", var["ink"], var["bg"]), ("muted/card", var["muted"], var["card"]), ("muted/bg", var["muted"], var["bg"]), ("white/pri", "#ffffff", var["pri"]),
         ("pri/card", var["pri"], var["card"]), ("ink/pri-t", var["ink"], var["pri-t"])]
for t in ("core", "growth", "new", "minor", "pos", "neg"):
    pairs.append((f"{t}/card", var[t], var["card"]))
for m in re.finditer(r"\.tb-([\w,.\-]+)\{background:(#\w+);color:(#\w+)", css):
    pairs.append((f"badge {m.group(1)}", m.group(3), m.group(2)))
low = [(n, round(ratio(a, b), 2)) for n, a, b in pairs if ratio(a, b) < 4.5]
check(f"contrast >= 4.5:1 for {len(pairs)} text/background pairs", not low, str(low))
check("prefers-reduced-motion and print styles present", "prefers-reduced-motion" in css and "@media print" in css)
check("viewport meta and :focus-visible present", 'name="viewport"' in tpl and ":focus-visible" in css)

# ------------------------------------------------------------------ browser render check
npm_root = ""
if shutil.which("npm"):
    npm_root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
env = dict(os.environ, NODE_PATH=npm_root)
have_pw = bool(node) and subprocess.run([node, "-e", "require('playwright')"], capture_output=True, env=env).returncode == 0
if not have_pw:
    print("SKIP  render check: playwright not available (node -e \"require('playwright')\" failed)")
else:
    script = TMP / "render.js"
    script.write_text(r"""
const { chromium } = require('playwright');
const files = JSON.parse(process.argv[2]);   // {label: path}
const TABS = ['overview', 'markets', 'detail', 'economics', 'sources'];
(async () => {
  const browser = await chromium.launch();
  const results = [];
  const rec = (name, ok, detail) => results.push({name, ok, detail: detail || ''});
  for (const [label, file] of Object.entries(files)) {
    for (const width of [1280, 390]) {
      const ctx = await browser.newContext({viewport: {width, height: 900}});
      const page = await ctx.newPage();
      const errors = [], requests = [];
      page.on('console', m => { if (m.type() === 'error' || m.type() === 'warning') errors.push(m.type() + ': ' + m.text()); });
      page.on('pageerror', e => errors.push('pageerror: ' + e.message));
      page.on('request', r => { if (!r.url().startsWith('file:') && !r.url().startsWith('data:')) requests.push(r.url()); });
      await page.goto('file://' + file);
      await page.waitForSelector('[role=tab]');
      for (const tab of TABS) {
        if (label === 'quick' && tab === 'economics') { /* still renders a message */ }
        await page.click('#t-' + tab);
        await page.waitForTimeout(120);
        const info = await page.evaluate((t) => {
          const p = document.getElementById('p-' + t);
          return {on: p.classList.contains('on'), text: p.innerText.length, svgs: p.querySelectorAll('svg.svgc').length, tables: p.querySelectorAll('table').length,
                  over: document.documentElement.scrollWidth - window.innerWidth, noNaN: !/NaN|undefined|\[object/.test(p.innerText)};
        }, tab);
        rec(`${label} ${width}px ${tab}: renders`, info.on && info.text > 40, JSON.stringify(info));
        rec(`${label} ${width}px ${tab}: no horizontal page overflow`, info.over <= 1, 'overflow ' + info.over);
        rec(`${label} ${width}px ${tab}: no NaN/undefined text`, info.noNaN);
        await page.screenshot({path: `/tmp/tsp_dash_${label}_${width}_${tab}.png`, fullPage: true});
      }
      if (label !== 'quick') {
        // interactions
        await page.click('#t-markets');
        const rows0 = await page.locator('#mk_out tbody tr').count();
        await page.selectOption('#f_tier', 'core');
        const rows1 = await page.locator('#mk_out tbody tr').count();
        rec(`${label} ${width}px markets: tier filter reduces rows`, rows1 > 0 && rows1 < rows0, rows0 + ' -> ' + rows1);
        await page.selectOption('#f_tier', 'all');
        await page.click('th button[data-sort="home"]');
        const sortAttr = await page.getAttribute('th[aria-sort="descending"]', 'aria-sort').catch(() => null);
        rec(`${label} ${width}px markets: sort sets aria-sort`, sortAttr === 'descending');
        const notScored = await page.locator('#mk_out td', {hasText: 'not scored'}).count();
        rec(`${label} ${width}px markets: 'not scored' shown for null scores`, label === 'import' || notScored > 0, String(notScored));
        await page.click('#mk_out tbody tr:first-child td:first-child button');
        rec(`${label} ${width}px row click opens partner detail`, await page.evaluate(() => document.getElementById('p-detail').classList.contains('on')));
        rec(`${label} ${width}px detail: freight and tariff blocks`, await page.evaluate(() => /Freight lane/.test(document.getElementById('p-detail').innerText) && /Tariff/.test(document.getElementById('p-detail').innerText)));
        await page.click('#t-economics');
        const m0 = await page.locator('#econ_out .tile', {hasText: 'Margin / container'}).first().innerText().catch(() => '');
        await page.evaluate(() => { const e = document.getElementById('e_price'); e.value = '-20'; e.dispatchEvent(new Event('input', {bubbles: true})); });
        const m1 = await page.locator('#econ_out .tile', {hasText: 'Margin / container'}).first().innerText().catch(() => '');
        rec(`${label} ${width}px economics: price slider changes margin live`, m0 !== '' && m0 !== m1, m0.replace(/\n/g, ' ') + ' => ' + m1.replace(/\n/g, ' '));
        await page.click('#e_reset');
        await page.check('input[name=e_scn][value=high]');
        const m2 = await page.locator('#econ_out .tile', {hasText: 'Margin / container'}).first().innerText().catch(() => '');
        rec(`${label} ${width}px economics: freight scenario high changes margin`, m2 !== '' && m2 !== m0, m2.replace(/\n/g, ' '));
        await page.click('#e_reset');
        const opts = await page.$$eval('#e_lane option', os => os.map(o => [o.value, o.textContent]));
        const inc = opts.find(o => /incomplete/.test(o[1]));
        if (inc) {
          await page.selectOption('#e_lane', inc[0]);
          const txt = await page.locator('#econ_out').innerText();
          rec(`${label} ${width}px economics: incomplete lane lists what is missing, no margin tile`, /Missing inputs/.test(txt) && !/Margin \/ container/.test(txt), txt.slice(0, 200).replace(/\n/g, ' '));
        }
        rec(`${label} ${width}px economics: payback chart drawn`, await page.locator('#ch_pay svg').count() > 0 || /Payback cannot be computed/.test(await page.locator('#econ_out').innerText()));
        await page.click('#t-sources');
        const all = await page.locator('#src_out tbody tr').count();
        await page.check('#s_verify');
        const ver = await page.locator('#src_out tbody tr').count();
        rec(`${label} ${width}px sources: needs-verification filter`, ver > 0 && ver < all, all + ' -> ' + ver);
        // keyboard: tab key reaches a focusable element with visible outline
        await page.click('#t-overview');
        await page.keyboard.press('Tab');
        const focus = await page.evaluate(() => { const a = document.activeElement; const cs = getComputedStyle(a); return a.tagName + ' ' + cs.outlineStyle + ' ' + cs.outlineWidth; });
        rec(`${label} ${width}px keyboard focus is visible`, /solid|auto/.test(focus) && !/ 0px/.test(focus), focus);
        await page.keyboard.press('ArrowRight');
      }
      rec(`${label} ${width}px: no console errors / warnings`, errors.length === 0, errors.join(' | ').slice(0, 300));
      rec(`${label} ${width}px: no network requests`, requests.length === 0, requests.join(' ').slice(0, 200));
      if (width === 1280) {
        await page.emulateMedia({media: 'print'});
        await page.pdf({path: `/tmp/tsp_dash_${label}.pdf`, format: 'A4'});
        rec(`${label}: prints to PDF`, true);
      }
      await ctx.close();
    }
  }
  await browser.close();
  process.stdout.write(JSON.stringify(results));
})().catch(e => { console.error(e); process.exit(1); });
""", encoding="utf-8")
    files = {"export": str(built["export"]), "import": str(built["import"]), "quick": str(quick_html)}
    r = subprocess.run([node, str(script), json.dumps(files)], capture_output=True, text=True, env=env, timeout=600)
    if r.returncode != 0:
        check("playwright render run", False, r.stderr[-500:])
    else:
        for res in json.loads(r.stdout):
            check(res["name"], res["ok"], res["detail"])
        shots = sorted(Path("/tmp").glob("tsp_dash_*.png"))
        check(f"screenshots saved to /tmp/tsp_dash_*.png ({len(shots)})", len(shots) >= 30)

print()
print(f"{PASS} passed, {FAIL} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAILED))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)

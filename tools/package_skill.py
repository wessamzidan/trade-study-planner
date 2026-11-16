"""Build dist/trade-study-planner-skill.zip: the installable skill (no tests, no demo data).

Usage: python3 tools/package_skill.py [--out dist/trade-study-planner-skill.zip]
The zip contains one top-level folder `trade-study-planner/` with SKILL.md, scripts/, references/,
assets/, requirements.txt, README.md and LICENSE — the layout Claude's "Upload a skill" expects.
"""
import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDE = ["SKILL.md", "README.md", "LICENSE", "requirements.txt", "scripts", "references", "assets"]
SKIP_PARTS = {"__pycache__", ".DS_Store"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "dist" / "trade-study-planner-skill.zip"))
    out = Path(ap.parse_args().out)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in INCLUDE:
            src = ROOT / item
            files = [src] if src.is_file() else sorted(p for p in src.rglob("*") if p.is_file())
            for f in files:
                if SKIP_PARTS & set(f.parts) or f.suffix == ".pyc":
                    continue
                zf.write(f, Path("trade-study-planner") / f.relative_to(ROOT))
                n += 1
    print(f"wrote {out} ({n} files, {out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()

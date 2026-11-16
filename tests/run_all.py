"""Run every test suite and print one line per suite. Exit non-zero if any suite fails."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITES = ["test_trade.py", "test_freight.py", "test_financial.py", "test_dashboard.py", "test_report.py"]


def main() -> int:
    failed = 0
    for name in SUITES:
        path = HERE / name
        if not path.exists():
            print(f"SKIP  {name} (not present)")
            continue
        proc = subprocess.run([sys.executable, str(path)], capture_output=True, text=True)
        lines = proc.stdout.splitlines()
        n_pass = sum(1 for ln in lines if ln.startswith("PASS"))
        n_fail = sum(1 for ln in lines if ln.startswith("FAIL"))
        ok = proc.returncode == 0 and n_fail == 0
        failed += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'}  {name}: {n_pass} passed, {n_fail} failed")
        if not ok:
            print("\n".join(ln for ln in lines if ln.startswith("FAIL"))[:2000] or proc.stderr[-2000:])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

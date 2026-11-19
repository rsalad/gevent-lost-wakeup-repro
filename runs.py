"""Run each scenario of repro.py N times and report how many runs failed."""
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import gevent
import greenlet

N = int(sys.argv[1]) if len(sys.argv) > 1 else 200
JOBS = int(os.environ.get("JOBS", "8"))
SCENARIOS = ("reacquire", "handoff")


def run(scenario):
    return subprocess.run(
        [sys.executable, "repro.py", scenario], capture_output=True, text=True
    )


def main():
    from gevent.lock import BoundedSemaphore

    # Only the pure-Python lock carries a native lock to stand in for the GIL.
    compiled = not hasattr(BoundedSemaphore(1), "_lock_lock")
    print(
        f"build={os.environ.get('BUILD', '?')} gevent={gevent.__version__} "
        f"({'compiled' if compiled else 'PURE_PYTHON'}) greenlet={greenlet.__version__} "
        f"python={sys.version.split()[0]} {os.uname().sysname}/{os.uname().machine}"
    )
    failed_any = False
    with ThreadPoolExecutor(JOBS) as pool:
        for scenario in SCENARIOS:
            results = list(pool.map(run, [scenario] * N))
            failed = [r for r in results if r.returncode == 1]
            other = [r for r in results if r.returncode not in (0, 1)]
            print(f"  {scenario:<10} {len(failed)}/{N} runs failed")
            sample = (failed or results)[0].stdout.strip()
            print(f"             e.g. {sample}")
            for r in other[:3]:
                print(f"             unexpected exit {r.returncode}: {r.stderr.strip()[-300:]}")
            failed_any = failed_any or bool(failed) or bool(other)
    return 1 if failed_any else 0


if __name__ == "__main__":
    sys.exit(main())

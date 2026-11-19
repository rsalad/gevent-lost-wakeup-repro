"""gevent drops a cross-thread lock wakeup: the lock is free, the waiter stays parked.

A greenlet waits on a gevent lock. A native thread that has no hub is the
last holder before it. When that thread releases, the greenlet is never
switched back in, although the lock is free and stays free.

    python repro.py reacquire   # the native thread releases, re-acquires, releases
    python repro.py handoff     # nobody re-acquires; the native thread waits its turn

Exit status 0: the greenlet got the lock. 1: it was still parked WAIT seconds
after the lock became free for good.

No monkey-patching and no third-party code besides gevent. The main thread
has a hub; the native thread never does.
"""
import os
import sys
import threading
import time

import gevent
from gevent.lock import BoundedSemaphore

WAIT = float(os.environ.get("WAIT", "1"))

lock = BoundedSemaphore(1)
holding = threading.Event()
free_for_good = threading.Event()
state_when_freed = []


def freed():
    state_when_freed.append(str(lock))
    free_for_good.set()


def greenlet_wait():
    """Wait for the lock from a greenlet. Returns (got it, seconds waited).

    The timeout keeps a timer on the hub, so a wait that would never end
    shows up as False here and not as LoopExit.
    """
    start = time.monotonic()
    got_it = lock.acquire(timeout=WAIT + 1)
    return got_it, time.monotonic() - start


def reacquire():
    def native_holder():
        lock.acquire()
        holding.set()
        while lock.linkcount() < 1:  # the main greenlet is parked on the lock
            time.sleep(0.001)
        lock.release()   # queues the wakeup on the main hub and clears lock.hub
        lock.acquire()   # the main hub has not run the wakeup yet
        time.sleep(0.1)  # the hub runs it now, finds the lock taken, gives up
        lock.release()   # lock.hub is None: tries to switch from this thread
        freed()

    threading.Thread(target=native_holder, daemon=True).start()
    while not holding.is_set():
        gevent.sleep(0.001)
    return greenlet_wait()


def handoff():
    def native_waiter():
        lock.acquire()   # lock.hub is the main hub: a helper greenlet there waits for us
        time.sleep(0.1)
        lock.release()   # lock.hub is None: tries to switch from this thread
        freed()

    lock.acquire()
    threading.Thread(target=native_waiter, daemon=True).start()
    while lock.linkcount() < 1:  # the helper greenlet is parked on the lock
        gevent.sleep(0.001)
    waiter = gevent.spawn(greenlet_wait)
    while lock.linkcount() < 2:  # our own greenlet is parked behind it
        gevent.sleep(0.001)
    lock.release()       # wakes the helper only, and clears lock.hub
    return waiter.get()


def main(name):
    scenario = {"reacquire": reacquire, "handoff": handoff}.get(name)
    if scenario is None:
        sys.exit(__doc__)
    # The main thread must already have a hub. A lock used before any hub
    # exists falls back to native-lock waits, which do not lose the wakeup.
    gevent.get_hub()
    got_it, waited = scenario()
    if got_it:
        print(f"PASS {name}: acquired after {waited:.2f}s")
        return 0
    assert free_for_good.is_set()
    print(
        f"FAIL {name}: still parked after {waited:.2f}s. "
        f"Lock after the last release: {state_when_freed[0]}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))

"""Print every call gevent makes on the lock in repro.py, with the lock's state.

    PURE_PYTHON=1 python trace.py reacquire
    PURE_PYTHON=1 python trace.py handoff

Needs PURE_PYTHON=1: the compiled lock hides _notifier and cannot be wrapped.
Columns: seconds, thread, greenlet, call, then the lock's state at that point
("> call" on entry, "< call" on return).
"""
import functools
import sys
import threading
import time

from greenlet import getcurrent

import repro

METHODS = [
    "acquire",
    "release",
    "rawlink",
    "_check_and_notify",
    "_notify_links_from_threadsafe",
    "_notify_links",
    "_handle_unswitched_notifications",
    "_Semaphore__acquire_using_other_hub",
    "_Semaphore__acquire_without_hubs",
]
events = []
start = time.monotonic()
main_thread = threading.get_ident()


def notifier(lock):
    n = lock._notifier
    if n is None or n == 1:
        return repr(n)
    if type(n).__name__ == "_FakeNotifier":
        return f"FakeNotifier(pending={n.pending})"
    return f"{type(n).__name__}(pending={n.pending})"


def record(lock, what):
    g = getcurrent()
    thread = "main" if threading.get_ident() == main_thread else "native"
    glet = "-" if thread == "native" else "main" if g.parent is None else type(g).__name__
    hub = "None" if lock.hub is None else "main hub"
    events.append(
        f"{time.monotonic() - start:7.4f} {thread:<6} {glet:<18} {what:<36} "
        f"counter={lock.counter} hub={hub:<8} links={lock.linkcount()} notifier={notifier(lock)}"
    )


def wrap(cls, name):
    original = getattr(cls, name)
    short = name.replace("_Semaphore__", "__")

    @functools.wraps(original)
    def traced(self, *args, **kwargs):
        if self is not repro.lock:
            return original(self, *args, **kwargs)
        record(self, f"> {short}")
        try:
            result = original(self, *args, **kwargs)
        except BaseException as e:
            record(self, f"< {short} raised {type(e).__name__}")
            raise
        record(self, f"< {short}" + (f" -> {result}" if name == "acquire" else ""))
        return result

    setattr(cls, name, traced)


if __name__ == "__main__":
    if not hasattr(repro.lock, "_lock_lock"):
        sys.exit(__doc__)
    for method in METHODS:
        wrap(type(repro.lock), method)
    status = repro.main(sys.argv[1] if len(sys.argv) > 1 else "")
    print("\n".join(events))
    sys.exit(status)

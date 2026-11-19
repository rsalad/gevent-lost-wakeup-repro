# gevent drops a cross-thread lock wakeup

A greenlet waits on a gevent lock. A native thread that has no hub is the last holder before it. When that thread releases the lock, gevent never switches the greenlet back in. The lock is free and the greenlet stays parked on it for good.

Reproduces every run on gevent 26.9.0 (the newest release, and identical to `master` in the files involved), compiled and `PURE_PYTHON`, with no monkey-patching and no other library.

## Run it

```sh
./run.sh        # builds three images, runs each scenario 200 times in each
./run.sh 50     # fewer runs
```

Needs Docker. To run one case by hand:

```sh
docker build --target stock -t gevent-lost-wakeup:stock .
docker run --rm gevent-lost-wakeup:stock python repro.py handoff
docker run --rm -e PURE_PYTHON=1 gevent-lost-wakeup:stock python trace.py handoff
```

| File | What it is |
|---|---|
| `repro.py` | The repro. Two scenarios, about 30 lines each. Only imports gevent. |
| `runs.py` | Runs each scenario N times and counts failures. |
| `trace.py` | Prints every call gevent makes on the lock, with the lock's state. `PURE_PYTHON=1` only. |
| `fix.patch` | The one-line change that removes the cause. |
| `Dockerfile` | `stock` (PyPI wheel), `source` (PyPI sdist, unchanged), `fixed` (sdist plus `fix.patch`). |

Pinned: CPython 3.14.8 (`python:3.14.8-slim`), gevent 26.9.0, greenlet 3.3.1, zope.event 6.1, zope.interface 8.2. Seen on Linux aarch64; the production hang this came from was on Linux x86_64.

## Expected and actual

Expected: once the native thread releases the lock, the waiting greenlet acquires it. `repro.py` prints `PASS` and exits 0.

Actual: the greenlet is still parked when its timeout fires, a second or more after the lock became free.

```
FAIL handoff: still parked after 2.01s. Lock after the last release: <BoundedSemaphore at 0xffff97d2d7e0 counter=1 _links[1]>
```

`counter=1` is a free lock. `_links[1]` is the greenlet nobody will wake. Without the timeout the wait never ends. If the hub has nothing else pending, the same wait raises `LoopExit` instead.

## Failure rate

200 runs of each scenario per row, Linux aarch64, 8 at a time.

| gevent 26.9.0 build | Mode | `reacquire` | `handoff` |
|---|---|---|---|
| `stock`: PyPI wheel | compiled | 200/200 fail | 200/200 fail |
| `stock`: PyPI wheel | `PURE_PYTHON=1` | 200/200 fail | 200/200 fail |
| `source`: PyPI sdist, unchanged | compiled | 200/200 fail | 200/200 fail |
| `source`: PyPI sdist, unchanged | `PURE_PYTHON=1` | 200/200 fail | 200/200 fail |
| `fixed`: sdist plus `fix.patch` | compiled | 0/200 fail | 0/200 fail |
| `fixed`: sdist plus `fix.patch` | `PURE_PYTHON=1` | 0/200 fail | 0/200 fail |

The failure is an ordering, not a race. `repro.py` waits on `linkcount()` to put the calls in order, so it does not depend on timing.

Older releases, PyPI wheels on CPython 3.14.8, 3 runs each: 26.8.0, 26.7.0 and 25.9.1 fail both scenarios every time. So this predates the 26.8.0 fix for [#2013](https://github.com/gevent/gevent/issues/2013) and is not a regression from it.

## Root cause

`BoundedSemaphore.release` forgets which hub owns the lock while a greenlet of that hub is still waiting on it. Line numbers are for gevent 26.9.0, under `src/gevent/`.

```python
# _semaphore.py:494-507
def release(self):
    ...
    counter = Semaphore.release(self)
    # When we are absolutely certain that no one holds this semaphore,
    # release our hub and go back to floating. This assists in cross-thread
    # uses.
    if counter == self._initial_value:
        self.hub = None
    return counter
```

Nobody holding the lock is not the same as nobody waiting on it. `Semaphore.release` only schedules the wakeup; the waiter is still in `_links` when `hub` is cleared. `lock.hub` is the only thing that tells a later release from a hub-less thread where the waiter lives:

- With `hub` set, `_capture_hub` raises `InvalidThreadUseError` (`_abstract_linkable.py:219`) and `_check_and_notify` queues the wakeup on the owning hub with `run_callback_threadsafe` (`_abstract_linkable.py:233-265`). This is the path [#2199](https://github.com/gevent/gevent/pull/2199) added in 26.8.0, and it works.
- With `hub` cleared, `_capture_hub(False)` finds no hub on the lock and none in the thread, and returns `None` (`_abstract_linkable.py:210-225`). `_check_and_notify` takes the "no hub, we must be the only thing running" branch and calls the links on the releasing thread (`_abstract_linkable.py:268-275`). The link is the waiter's `greenlet.switch`, which cannot be called from another thread. `_notify_link_list` catches the `greenlet.error` (`_abstract_linkable.py:326-331`), and `Semaphore._handle_unswitched_notifications` puts the link back on `_links` "in the hopes that the other thread will eventually run notifications itself" (`_semaphore.py:288-311`). Nothing is scheduled anywhere, so nothing ever does.

So the wakeup is lost whenever a hub-less thread releases the lock while `hub` is `None` and a greenlet is linked. That needs a hub-less thread to become the holder after the reset and before the greenlet is woken. The two scenarios are two ways to get there.

### `reacquire`: the native thread takes the lock back

N is the native thread, M the main greenlet, H the main thread's hub.

| # | Who | Call | What happens | After |
|---|---|---|---|---|
| 1 | N | `acquire` | No hub on the lock or in the thread. Fast path, `_semaphore.py:230-232`. | counter 0, hub None |
| 2 | M | `acquire` | `_capture_hub(False)` records H (`_abstract_linkable.py:216-217`). Counter is 0, so `_wait` (`_semaphore.py:253`) links `M.switch` and M parks in H. | hub H, 1 link |
| 3 | N | `release` | Counter 1. `_capture_hub` raises; a `_FakeNotifier` is installed and `_notify_links_from_threadsafe` is queued on H (`_abstract_linkable.py:254-260`). Then `_semaphore.py:505-506` clears `hub`. | counter 1, **hub None**, 1 link, wakeup pending |
| 4 | N | `acquire` | `_capture_hub(False)` sees no hub anywhere, so no error and `hub` stays `None`. Fast path. | counter 0, hub None, 1 link |
| 5 | H | `_notify_links_from_threadsafe` | `_notify_link_list` stops at once because the lock is not ready (`_abstract_linkable.py:313-314`). `_notifier` is cleared (`:399`). The trailing `_check_and_notify` (`:410`) does nothing. | 1 link, nothing pending |
| 6 | N | `release` | Counter 1. `_capture_hub(False)` returns `None`: the "no hub" branch runs `M.switch` on N's thread, `greenlet.error` is swallowed, the link is re-queued (`_semaphore.py:311`). | counter 1, hub None, 1 link, nothing pending |

Step 4 only has to come before step 5. In `repro.py` it follows step 3 with no blocking call between them, so it always does.

### `handoff`: nobody re-acquires

T is the native thread. M is the main greenlet, W a second greenlet, both in the main thread. G is the helper greenlet gevent spawns in H to wait on T's behalf.

| # | Who | Call | What happens | After |
|---|---|---|---|---|
| 1 | M | `acquire` | Records H. Fast path. | counter 0, hub H |
| 2 | T | `acquire` | `_capture_hub` raises: the lock belongs to H. T has no hub, so `__acquire_using_other_hub` (`_semaphore.py:406-424`) queues a helper on H and blocks on a native lock. H spawns G; G's `acquire` links `G.switch`. | 1 link (G) |
| 3 | W | `acquire` | Links `W.switch` behind G. | 2 links (G, W) |
| 4 | M | `release` | Counter 1. `_notify_links` is queued on H (`_abstract_linkable.py:267`). Then `_semaphore.py:505-506` clears `hub`. | counter 1, **hub None**, 2 links |
| 5 | H | `_notify_links` | G is switched in and takes the lock at `_semaphore.py:276-278`, which does not call `_capture_hub`, so `hub` stays `None`. G releases T's native lock and ends. The loop stops at W because the lock is no longer ready. | counter 0, hub None, 1 link (W) |
| 6 | T | `release` | T holds a lock with no hub recorded. Same as step 6 above: `W.switch` is run on T's thread, fails, and is re-queued. | counter 1, hub None, 1 link, nothing pending |

`PURE_PYTHON=1 python trace.py handoff` prints this sequence from a real run. Steps 4 to 6:

```
 0.0063 main   main               > release                            counter=0 hub=main hub links=2 notifier=None
 0.0063 main   main               < _check_and_notify                  counter=1 hub=main hub links=2 notifier=callback(pending=True)
 0.0063 main   main               < release                            counter=1 hub=None     links=2 notifier=callback(pending=True)
 0.0063 main   Hub                > _notify_links                      counter=1 hub=None     links=2 notifier=callback(pending=False)
 0.0063 main   TrackedRawGreenlet < acquire -> True                    counter=0 hub=None     links=1 notifier=callback(pending=False)
 0.0063 main   Hub                < _notify_links                      counter=0 hub=None     links=1 notifier=None
 0.0064 native -                  < acquire -> True                    counter=0 hub=None     links=1 notifier=None
 0.1065 native -                  > release                            counter=0 hub=None     links=1 notifier=None
 0.1065 native -                  > _notify_links                      counter=1 hub=None     links=1 notifier=1
 0.1066 native -                  < _handle_unswitched_notifications   counter=1 hub=None     links=1 notifier=None
 0.1067 native -                  < release                            counter=1 hub=None     links=1 notifier=None
 1.5093 main   Greenlet           < acquire -> False                   counter=1 hub=None     links=0 notifier=None
```

## The cause, removed

`fix.patch` keeps the hub for as long as anyone is linked:

```diff
-        if counter == self._initial_value:
+        if counter == self._initial_value and not self._links:
             self.hub = None
```

With it, `hub` is still H at step 6 of both scenarios, `_capture_hub` raises, and the release goes down the `run_callback_threadsafe` path that already works. The `fixed` rows in the table are the same script on the same sdist with only this line changed, and the `source` rows rule out the build as the difference.

This shows the cause. It is not a claim about the right fix: the reporter of #2013 suggested another one, having `Semaphore._handle_unswitched_notifications` schedule `_check_and_notify` on the waiter's hub, which would also cover a link stranded some other way.

gevent's own tests for this area give the same result on the `source` and `fixed` builds, compiled and `PURE_PYTHON=1`: `test__semaphore` (19), `test__lock` (7), `test__issue2013` (3), `test__event` (46), `test__threading` (4), `test__threading_2` (89, 1 skipped) and `test__monkey_logging` all pass. The rest of the suite was not run.

```sh
docker run --rm gevent-lost-wakeup:fixed python -m gevent.tests.test__semaphore
```

## Where this came from, and what turned out not to matter

The hang was first seen in a gunicorn master (`preload_app`, gevent worker class, started with `ddtrace-run`) that stopped part-way through importing a Django app. Its main greenlet was parked in `logging.getLogger`, on `logging`'s module lock, with the lock free. A harness that added a native thread hammering that lock made every boot hang.

Tracing that boot (a proxy around the lock on compiled gevent, then `trace.py`-style wrappers under `PURE_PYTHON=1`) showed the `handoff` ending both times: a hub-less thread released a lock whose `hub` was `None` while the main greenlet was linked. The thread was one of ddtrace's periodic threads calling `log.debug`, which takes the module lock in `Logger.isEnabledFor`. The hub had been cleared two steps earlier, by the harness thread's release, with the main greenlet and ddtrace's helper greenlet both linked.

| Piece | Needed? | What it contributed |
|---|---|---|
| The application and Django | No | Thousands of `getLogger` calls in the main thread, so the main greenlet was often waiting on the lock. |
| gunicorn | No | `preload_app` puts that import in the master's main thread. |
| ddtrace | No | A second native thread using the same lock. Any thread will do. |
| `logging` | No | A lock that every thread happens to share. |
| Monkey-patching | No | It turns `logging`'s lock into a gevent lock. `repro.py` uses `gevent.lock.BoundedSemaphore` directly. |
| A hub in the waiting thread, created before the lock is first contended | **Yes** | Without one the waiter uses a native-lock wait (`_semaphore.py:240-248`) and is woken correctly. |
| A thread with no hub that holds the lock after the reset | **Yes** | Its release is the one that cannot find the waiter. |
| A release that returns the counter to its initial value while a greenlet is linked | **Yes** | That is what clears `hub`. |

An earlier standalone attempt had the hub, the native thread and the contention, and never hung. It left a 2 ms gap between the native thread's release and its next acquire, and had no second thread. The hub runs the queued wakeup in microseconds, so neither ordering above could happen.

The same boot was rerun on a pure-Python copy of gevent 26.9.0 with and without the one-line change, under the same forced contention: without it the master hung; with it the master was healthy in 10 s, 3 of 3.

## Upstream status

Reported as [gevent#2221](https://github.com/gevent/gevent/issues/2221) on 2026-11-18. Not fixed in any release as of that date.

- [#2013](https://github.com/gevent/gevent/issues/2013) is the same symptom with a simpler trigger. [#2199](https://github.com/gevent/gevent/pull/2199) closed it in 26.8.0 by fixing the foreign release while `hub` is still set. It did not touch `_semaphore.py`, and its test has the native thread acquire and release once.
- 26.9.0 is the newest release on PyPI. `master` is one version-bump commit ahead of the tag.

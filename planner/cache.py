"""Two-level cache with single-flight loading.

L1: a small in-process LRU of live Python objects (no pickling, ~1 µs).
L2: Django's ``routes`` cache (file-based by default), shared by every worker
    on the machine and kept across restarts, so a route is fetched from the
    routing provider once, not once per process or per deploy.

Both levels honour the same absolute expiry (``ROUTE_CACHE_SECONDS`` after the
value was loaded): an entry copied into L1 from L2 keeps L2's remaining lifetime.

``get_or_load`` collapses concurrent misses for the same key into one load:
threads in a process wait on a shared future, and processes on the same host
take a file lock next to the file cache, then re-check it. So if ten requests
for a new route arrive together, across any number of local workers, the
provider is called once. (Across several hosts, use a shared cache such as
Redis; it will then deduplicate per host.)
"""

import contextlib
import hashlib
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future

from django.conf import settings
from django.core.cache import caches

try:
    import fcntl
except ImportError:  # Windows: per-process deduplication only.
    fcntl = None

L1_SIZE = 256
LOCK_STRIPES = 256  # Lock files are shared by key hash, so their number stays bounded.

_l1 = OrderedDict()
_l1_lock = threading.Lock()
_inflight = {}
_inflight_lock = threading.Lock()


def _l1_get(key):
    with _l1_lock:
        entry = _l1.get(key)
        if entry is None:
            return None
        expires, value = entry
        if time.time() >= expires:
            del _l1[key]
            return None
        _l1.move_to_end(key)
        return value


def _l1_set(key, value, expires):
    with _l1_lock:
        _l1[key] = (expires, value)
        _l1.move_to_end(key)
        while len(_l1) > L1_SIZE:
            _l1.popitem(last=False)


def _l2_get(key):
    entry = caches["routes"].get(key)
    # Entries written by an older version of this code (another format) count as misses.
    if not isinstance(entry, dict) or not {"expires", "value"} <= entry.keys() or time.time() >= entry["expires"]:
        return None
    return entry


@contextlib.contextmanager
def _host_lock(key):
    """Exclusive lock shared by all processes using the same file cache directory."""
    directory = settings.CACHES["routes"].get("LOCATION")
    if fcntl is None or "filebased" not in settings.CACHES["routes"]["BACKEND"] or not directory:
        yield
        return
    os.makedirs(directory, exist_ok=True)
    stripe = int(hashlib.sha256(key.encode()).hexdigest(), 16) % LOCK_STRIPES
    with open(os.path.join(directory, f"lock-{stripe:03d}"), "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def clear():
    with _l1_lock:
        _l1.clear()
    caches["routes"].clear()


def get_or_load(key, load):
    """Return ``(value, source)``: source is "memory", "disk", "shared" or "loaded".

    ``load()`` runs at most once per key at a time on a host; it must return a
    picklable value. Exceptions from ``load`` reach every waiting caller and are not cached.
    """
    value = _l1_get(key)
    if value is not None:
        return value, "memory"
    entry = _l2_get(key)
    if entry is not None:
        _l1_set(key, entry["value"], entry["expires"])
        return entry["value"], "disk"

    with _inflight_lock:
        future = _inflight.get(key)
        leader = future is None
        if leader:
            future = _inflight[key] = Future()
    if not leader:
        return future.result(), "shared"  # Waited for another request's in-flight load (same process).

    try:
        with _host_lock(key):
            entry = _l2_get(key)  # Another process may have loaded it while we waited for the lock.
            if entry is not None:
                value, source, expires = entry["value"], "shared", entry["expires"]
            else:
                value, source = load(), "loaded"
                expires = time.time() + settings.ROUTE_CACHE_SECONDS
                caches["routes"].set(key, {"expires": expires, "value": value}, settings.ROUTE_CACHE_SECONDS)
        _l1_set(key, value, expires)
        future.set_result(value)
        return value, source
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)

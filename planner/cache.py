"""Two-level cache with single-flight loading.

L1: a small in-process LRU of live Python objects (no pickling, ~1 µs).
L2: Django's ``routes`` cache (file-based by default), shared by every worker
    on the machine and kept across restarts, so a route is fetched from the
    routing provider once, not once per process or per deploy.

``get_or_load`` also collapses concurrent misses for the same key into one
load: if ten requests for a new route arrive together, the provider is called
once and all ten receive the result.
"""

import threading
from collections import OrderedDict
from concurrent.futures import Future

from django.conf import settings
from django.core.cache import caches

L1_SIZE = 256

_l1 = OrderedDict()
_l1_lock = threading.Lock()
_inflight = {}
_inflight_lock = threading.Lock()


def _l1_get(key):
    with _l1_lock:
        if key in _l1:
            _l1.move_to_end(key)
            return _l1[key]
    return None


def _l1_set(key, value):
    with _l1_lock:
        _l1[key] = value
        _l1.move_to_end(key)
        while len(_l1) > L1_SIZE:
            _l1.popitem(last=False)


def clear():
    with _l1_lock:
        _l1.clear()
    caches["routes"].clear()


def get_or_load(key, load):
    """Return ``(value, source)``: source is "memory", "disk", "shared" or "loaded".

    ``load()`` runs at most once per key at a time; it must return a picklable value.
    Exceptions from ``load`` propagate to every waiting caller and are not cached.
    """
    value = _l1_get(key)
    if value is not None:
        return value, "memory"
    value = caches["routes"].get(key)
    if value is not None:
        _l1_set(key, value)
        return value, "disk"

    with _inflight_lock:
        future = _inflight.get(key)
        leader = future is None
        if leader:
            future = _inflight[key] = Future()
    if not leader:
        return future.result(), "shared"  # Waited for another request's in-flight load.

    try:
        value = load()
        caches["routes"].set(key, value, settings.ROUTE_CACHE_SECONDS)
        _l1_set(key, value)
        future.set_result(value)
        return value, "loaded"
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)

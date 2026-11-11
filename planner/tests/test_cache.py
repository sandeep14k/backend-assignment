import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from planner import cache

WORKER = """
import os, sys, time
sys.path.insert(0, sys.argv[1])
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
os.environ["ROUTE_CACHE_DIR"] = sys.argv[2]
import django
django.setup()
from planner import cache

def load():
    with open(sys.argv[3], "a") as counter:
        counter.write("x")
    time.sleep(0.5)
    return {"route": 42}

value, source = cache.get_or_load("route:multi-process-test", load)
print(source, value["route"])
"""


class CacheTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_memory_and_disk_honour_the_same_absolute_expiry(self):
        with override_settings(ROUTE_CACHE_SECONDS=60):
            cache.get_or_load("k", lambda: "v")
        now = time.time()
        with patch("planner.cache.time.time", return_value=now + 61):
            self.assertIsNone(cache._l1_get("k"))
            self.assertIsNone(cache._l2_get("k"))
            self.assertEqual(cache.get_or_load("k", lambda: "fresh"), ("fresh", "loaded"))

    def test_disk_hit_keeps_its_remaining_lifetime_in_memory(self):
        with override_settings(ROUTE_CACHE_SECONDS=60):
            cache.get_or_load("k", lambda: "v")
        cache._l1.clear()
        self.assertEqual(cache.get_or_load("k", lambda: "unused"), ("v", "disk"))
        expires, _ = cache._l1["k"]
        self.assertLessEqual(expires, time.time() + 60)

    def test_entries_in_an_old_format_are_misses_not_errors(self):
        from django.core.cache import caches

        caches["routes"].set("k", {"coordinates": [1, 2]})  # Written by an earlier release.
        self.assertEqual(cache.get_or_load("k", lambda: "new"), ("new", "loaded"))

    def test_separate_processes_share_one_load(self):
        """Four worker processes miss the same key at once; the file lock lets only one load it."""
        with tempfile.TemporaryDirectory() as directory:
            counter = Path(directory) / "loads.txt"
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", WORKER, str(settings.BASE_DIR), directory, str(counter)],
                    stdout=subprocess.PIPE,
                    text=True,
                )
                for _ in range(4)
            ]
            outputs = [process.communicate(timeout=60)[0].split() for process in processes]
            self.assertEqual(counter.read_text(), "x")  # Loaded exactly once.
            self.assertEqual(sorted(source for source, _ in outputs).count("loaded"), 1)
            self.assertTrue(all(value == "42" for _, value in outputs))

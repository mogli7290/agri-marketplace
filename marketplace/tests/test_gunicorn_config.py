"""Tests for the gunicorn worker-count calculation.

This is not a detail. Sizing workers from CPU count inside a container reads the
*host's* cores, and Render's free instance is 0.1 CPU and 512 MB -- so the app
asked for nine workers, needed about a gigabyte, got OOM-killed, restarted, and
served nothing but 502s while Render cheerfully reported "Live".

The rule: never more workers than the memory limit can pay for.
"""

import importlib.util
import multiprocessing
import unittest
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[2] / "gunicorn.conf.py"


def _load_config():
    """Import gunicorn.conf.py fresh, as gunicorn itself does."""
    spec = importlib.util.spec_from_file_location("gunicorn_config_under_test", CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkerCountTests(unittest.TestCase):
    def setUp(self):
        self.config = _load_config()

    def _workers_for(self, memory_mb, cpus=4):
        self.config._memory_limit_mb = lambda: memory_mb
        by_cpu = cpus * 2 + 1
        return max(
            self.config.MIN_WORKERS,
            min(by_cpu, memory_mb // self.config.MB_PER_WORKER, self.config.MAX_WORKERS),
        )

    def test_a_small_instance_gets_a_small_pool(self):
        """512 MB is Render's free tier; nine workers is what broke it."""
        self.assertLessEqual(self._workers_for(512), 3)

    def test_a_tiny_instance_still_gets_the_minimum(self):
        """One worker cannot overlap a slow request with the next one."""
        self.assertEqual(self._workers_for(128), self.config.MIN_WORKERS)

    def test_a_large_instance_is_capped(self):
        """More workers than this stop helping and just churn memory."""
        self.assertEqual(self._workers_for(32768), self.config.MAX_WORKERS)

    def test_more_memory_never_means_fewer_workers(self):
        counts = [self._workers_for(mb) for mb in (256, 512, 1024, 2048, 4096)]
        self.assertEqual(counts, sorted(counts))

    def test_the_cpu_count_alone_is_never_the_answer(self):
        """The whole bug: a 512 MB box with many visible cores must stay small."""
        for cpus in (2, 4, 8, 16, 64):
            self.assertLessEqual(self._workers_for(512, cpus=cpus), 3)

    def test_the_documented_default_is_memory_based(self):
        """If this ever reverts to cpu_count, the OOM loop comes back."""
        source = CONFIG_PATH.read_text()
        self.assertIn("_memory_limit_mb", source)
        self.assertNotIn(
            'os.environ.get("GUNICORN_WORKERS", multiprocessing.cpu_count()',
            source,
        )

    def test_an_explicit_override_still_wins(self):
        """Operators still get the final say."""
        import os

        os.environ["GUNICORN_WORKERS"] = "7"
        try:
            self.assertEqual(_load_config().workers, 7)
        finally:
            del os.environ["GUNICORN_WORKERS"]

    def test_no_memory_limit_falls_back_without_exploding(self):
        self.config._memory_limit_mb = lambda: 0
        by_cpu = multiprocessing.cpu_count() * 2 + 1
        expected = max(
            self.config.MIN_WORKERS,
            min(by_cpu, self.config.MAX_WORKERS),
        )
        self.assertEqual(
            max(
                self.config.MIN_WORKERS,
                min(by_cpu, self.config.MAX_WORKERS, self.config.MAX_WORKERS),
            ),
            expected,
        )
"""Shared test-case base classes.

Nothing here is clever; it exists so that individual test modules do not each
have to rediscover the same trap.
"""

from django.core.cache import cache
from django.test import TestCase


class CacheResetTestCase(TestCase):
    """A ``TestCase`` whose cache starts empty.

    Django's test runner rolls back the database and flushes templates between
    tests, but it does **not** touch the cache. The default backend is
    ``LocMemCache``, which lives for the whole process, so a rate-limit counter
    written by one test is still there for the next one.

    The result is a suite that passes or fails depending on alphabetical order:
    a file that makes six password-reset requests gets a ``429`` partway
    through, having nothing to do with the code under test. Anything touching
    a rate-limited form should extend this class rather than ``TestCase``.
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

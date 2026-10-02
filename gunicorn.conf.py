"""Gunicorn configuration. Docs: https://docs.gunicorn.org/en/stable/settings.html

Worker count is derived from the **memory limit**, not the CPU count.

That distinction matters more than it looks. ``multiprocessing.cpu_count()``
inside a container reports the *host's* cores, not the slice the container was
given: on Render's free plan the instance is 0.1 CPU and 512 MB, but cpu_count()
happily returns 4 or more. Nine Django workers need roughly a gigabyte, so the
container gets OOM-killed, Render restarts it, and it is killed again -- which
surfaces to users as a permanent 502 with the service marked "Live" and the logs
showing a dozen workers booting and nothing ever listening.

Sizing from memory means the number of workers is a consequence of what was
actually allocated, which is the thing that runs out.
"""

import multiprocessing
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

#: Rough per-worker footprint for a Django app with DRF loaded, in megabytes.
#: Two threads per worker means some overlap, and the master process takes its
#: own share, so this is deliberately generous. On a 512 MB instance it yields
#: three workers, which leaves headroom instead of flirting with the OOM killer.
MB_PER_WORKER = 150

#: Never drop below this: a single worker cannot overlap a slow request with
#: the next one, which turns every slow query into a queue.
MIN_WORKERS = 2

#: Beyond this, extra workers stop helping and only add context-switching.
MAX_WORKERS = 8


def _memory_limit_mb() -> int:
    """Memory this container is allowed, in MB.

    Prefers the cgroup v2 file, falls back to v1, and finally gives up rather
    than guessing -- a wrong guess here is an out-of-memory kill.
    """
    candidates = (
        "/sys/fs/cgroup/memory.max",  # cgroup v2: "max" means unlimited
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1
    )
    for path in candidates:
        try:
            with open(path) as handle:
                raw = handle.read().strip()
        except OSError:
            continue
        if raw in ("max", ""):
            continue
        try:
            limit = int(raw)
        except ValueError:
            continue
        # A very large number means "no limit", not "petabytes of RAM".
        if 0 < limit < (1 << 50):
            return limit // (1024 * 1024)
    return 0


def _default_workers() -> int:
    """How many workers this machine can actually afford."""
    by_cpu = multiprocessing.cpu_count() * 2 + 1

    limit_mb = _memory_limit_mb()
    if not limit_mb:
        # No cgroup information: trust the CPU count but stay bounded.
        return max(MIN_WORKERS, min(by_cpu, MAX_WORKERS))

    by_memory = limit_mb // MB_PER_WORKER
    return max(MIN_WORKERS, min(by_cpu, by_memory, MAX_WORKERS))


workers = int(os.environ.get("GUNICORN_WORKERS", _default_workers()))
threads = int(os.environ.get("GUNICORN_THREADS", "2"))
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "sync")
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))
graceful_timeout = 30
keepalive = 5

# Recycling workers bounds the damage from any slow leak. The jitter stops every
# worker restarting at once, which would otherwise look like an outage.
max_requests = 1000
max_requests_jitter = 100

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
forwarded_allow_ips = "*"
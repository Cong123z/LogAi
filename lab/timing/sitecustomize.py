"""Measurement helper for the laptop proof (bk17): with PYTHONPATH=/app:/app/lab/timing, python loads this at start,
wraps fit/day of every lab method with a timer and prints per-method seconds + peak memory at exit. No effect otherwise."""
import atexit
import resource
import sys
import time
from collections import defaultdict

try:
    from scripts.lab import methods
except Exception:           # not in a lab context
    methods = None

T, N = defaultdict(float), defaultdict(int)


def _wrap(name, fn, tag):
    def w(*a, **k):
        t = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            T[name, tag] += time.perf_counter() - t
            N[name, tag] += 1
    return w


def _factory(name, make):
    def f():
        m = make()
        for t in ("fit", "day"):
            setattr(m, t, _wrap(name, getattr(m, t), t))
        return m
    return f


if methods:
    for _n in list(methods.METHODS):
        methods.METHODS[_n] = _factory(_n, methods.METHODS[_n])


@atexit.register
def _report():
    print("TIMING method fit_s day_s fit_n day_n", file=sys.stderr)
    for n in sorted({k[0] for k in T}):
        print(f"TIMING {n} {T[n,'fit']:.1f} {T[n,'day']:.1f} {N[n,'fit']} {N[n,'day']}", file=sys.stderr)
    peak = ""
    try:
        peak = f" cgroup_peak_mb={int(open('/sys/fs/cgroup/memory.peak').read()) / 1048576:.0f}"
    except Exception:
        pass
    print(f"TIMING maxrss_mb={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f}{peak}", file=sys.stderr)

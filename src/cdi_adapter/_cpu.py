"""Native thread-pool caps sized to the container's real CPU quota (ARCHITECTURE §7.4 #4).

The pod's cgroup quota is ~13.6 CPUs while the host reports 128; without caps, OpenCV / NumPy /
ONNX Runtime each spin up 128 threads per document and thrash. This must run *before* those
libraries are imported, so it only touches environment variables; ``cv2.setNumThreads`` and the
RapidOCR ``intra_op_num_threads`` argument read :func:`per_worker_threads` where they are used.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "RAYON_NUM_THREADS",
    "ORT_NUM_THREADS",
)

_CGROUP_V2 = Path("/sys/fs/cgroup/cpu.max")
_CGROUP_V1_QUOTA = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
_CGROUP_V1_PERIOD = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")


def parse_cgroup_v2(text: str) -> float | None:
    """``"1360000 100000"`` → 13.6; ``"max 100000"`` → None (unlimited)."""
    parts = text.split()
    if len(parts) != 2 or parts[0] == "max":
        return None
    quota, period = int(parts[0]), int(parts[1])
    return quota / period if quota > 0 and period > 0 else None


def parse_cgroup_v1(quota_text: str, period_text: str) -> float | None:
    """quota ``-1`` means unlimited."""
    quota, period = int(quota_text.strip()), int(period_text.strip())
    return quota / period if quota > 0 and period > 0 else None


def cpu_budget(
    *,
    v2: Path = _CGROUP_V2,
    v1_quota: Path = _CGROUP_V1_QUOTA,
    v1_period: Path = _CGROUP_V1_PERIOD,
) -> float:
    """CPUs this process may actually use: cgroup quota if set, else ``os.cpu_count()``."""
    host = float(os.cpu_count() or 1)
    quota: float | None = None
    try:
        if v2.exists():
            quota = parse_cgroup_v2(v2.read_text())
        elif v1_quota.exists() and v1_period.exists():
            quota = parse_cgroup_v1(v1_quota.read_text(), v1_period.read_text())
    except (OSError, ValueError):
        quota = None
    return min(quota, host) if quota else host


def per_worker_threads(budget: float, job_max_workers: int) -> int:
    """``(budget - 1) ÷ job_max_workers``, floored, never below 1."""
    return max(1, math.floor((budget - 1) / max(1, job_max_workers)))


def apply_thread_caps(job_max_workers: int, *, budget: float | None = None) -> int:
    """Set the native thread env vars (unless already set) and return the per-worker cap."""
    n = per_worker_threads(cpu_budget() if budget is None else budget, job_max_workers)
    for var in THREAD_ENV_VARS:
        os.environ.setdefault(var, str(n))
    return n

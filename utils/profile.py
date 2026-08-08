"""
System Resource Telemetry Sampler
Queries real-time CPU percentage and process resident RAM usage using psutil.
"""

import os
import time
from typing import Tuple

try:
    import psutil
    HAS_PSUTIL = True
    try:
        psutil.cpu_percent(interval=None)
    except Exception:
        pass
except ImportError:
    HAS_PSUTIL = False

def get_system_resource_telemetry() -> Tuple[float, float]:
    cpu_pct, ram_gb = 0.0, 0.0
    if HAS_PSUTIL:
        try:
            cpu_pct = psutil.cpu_percent(interval=None)
            ram_gb = round(psutil.Process(os.getpid()).memory_info().rss / (1024.0 ** 3), 2)
        except Exception:
            pass
    return cpu_pct, ram_gb
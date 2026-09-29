"""Settings from the environment.

Cens-arr was called Cleanarr. Every variable is read as CENSARR_<NAME> first
and CLEANARR_<NAME> after it, so an install set up under the old name keeps
its settings without being touched.
"""

from __future__ import annotations

import os


def get(name: str, default: str = "") -> str:
    for prefix in ("CENSARR_", "CLEANARR_"):
        value = os.environ.get(prefix + name)
        if value:
            return value
    return default

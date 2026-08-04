"""Activate the site-volume dependency directory used by Docker deployment."""

from __future__ import annotations

import os
import sys


DEFAULT_VENDOR_PATH = "/home/frappe/frappe-bench/sites/.python-dependencies/db_connector"


def activate_vendor() -> str | None:
    path = os.environ.get("DB_CONNECTOR_FUZZY_VENDOR_PATH", DEFAULT_VENDOR_PATH)
    if os.path.isdir(path) and path not in sys.path:
        # Prefer packages already supplied by the ERPNext image. The pinned
        # directory fills missing fuzzy dependencies without replacing Frappe's
        # own runtime dependencies globally.
        sys.path.append(path)
        return path
    return None

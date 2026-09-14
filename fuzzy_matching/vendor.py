"""Activate the site-volume dependency directory used by Docker deployment."""

from __future__ import annotations

import os
import sys


DEFAULT_VENDOR_PATH = "/home/frappe/frappe-bench/sites/.python-dependencies/db_connector"


def activate_vendor() -> str | None:
    path = os.environ.get("DB_CONNECTOR_FUZZY_VENDOR_PATH", DEFAULT_VENDOR_PATH)
    if os.path.isdir(path) and path not in sys.path:
        # Matching validations must use the exact audited dependency set.  A
        # base-image DuckDB previously won because this path was appended,
        # silently pairing Splink with the wrong runtime.
        sys.path.insert(0, path)
        return path
    return None

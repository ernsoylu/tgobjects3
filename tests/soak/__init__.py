"""Soak-test drivers for tgobjects3.

This package holds long-running operator/CI scripts (not pytest tests) that
drive sustained load against a live gateway deployment. See
:mod:`tests.soak.run_soak` for the M3 500 GB soak driver (task 39; design
"Soak Test (M3)"; Requirement 18.1).
"""

from __future__ import annotations

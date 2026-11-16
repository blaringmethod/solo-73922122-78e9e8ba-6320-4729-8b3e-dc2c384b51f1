"""Idempotent demo-data loader.

On container start (``LOAD_DEMO_ON_START=1``) the two demo CSVs shipped in
``demo/`` are loaded *only if no input has ever been accepted*, so opening the
browser immediately shows a working dataset while repeated restarts never
clobber data the user uploaded afterwards.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import db
from .parsing import parse_revisions, parse_samples

DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"


def load_demo_if_empty() -> bool:
    demo_samples = DEMO_DIR / "samples.csv"
    demo_revisions = DEMO_DIR / "revisions.csv"
    with db.session_scope() as session:
        overview = db.get_current_overview(session)
        if overview["sample_set"] is not None or overview["revision_set"] is not None:
            return False
        sample_rows = parse_samples(demo_samples.read_bytes())
        revision_rows = parse_revisions(demo_revisions.read_bytes())
        db.replace_sample_set(session, sample_rows, demo_samples.name)
        db.replace_revision_set(session, revision_rows, demo_revisions.name)
        return True


def maybe_load_demo() -> None:
    if os.environ.get("LOAD_DEMO_ON_START") == "1":
        load_demo_if_empty()

"""Stable portable data directory and bundled reference data paths."""
import os
import sys
from pathlib import Path

APP_NAME = 'EntropiaTracker'

def initialize_runtime():
    # Explorer and shortcuts may choose another working directory. User files
    # belong beside the portable executable, never in its temporary bundle.
    if getattr(sys, 'frozen', False):
        os.chdir(Path(sys.executable).resolve().parent)

def reference_file(name):
    local = Path(name)
    return local if local.is_file() else Path(__file__).resolve().with_name(name)

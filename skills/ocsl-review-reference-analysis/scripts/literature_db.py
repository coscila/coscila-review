#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compatibility entrypoint for the maintained portable corpus library."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from ocsl_review.corpus_library import *  # noqa: F403
if __name__ == "__main__":
    sys.exit(main())

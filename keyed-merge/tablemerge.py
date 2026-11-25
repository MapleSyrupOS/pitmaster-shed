#!/usr/bin/env python3
"""tablemerge: keyed-merge's second name. Runs keyed-merge.py, beside this file, with the same arguments."""
import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "keyed-merge.py"), run_name="__main__")

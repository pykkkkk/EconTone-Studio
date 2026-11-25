#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Assemble lib/ (the shipped bundle) from the sources at the repo root."""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIB = os.path.join(ROOT, "lib")

COPY_FILES = ["index.js", "cordis.patch.yml", "icon.svg"]
COPY_DIRS = ["engine", "prompts", "locale"]


def main():
    os.makedirs(LIB, exist_ok=True)
    for name in COPY_FILES:
        shutil.copy2(os.path.join(ROOT, name), os.path.join(LIB, name))
    for name in COPY_DIRS:
        src = os.path.join(ROOT, name)
        dst = os.path.join(LIB, name)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    out = subprocess.run([sys.executable, os.path.join(HERE, "emit-client.py")], cwd=ROOT)
    return out.returncode


if __name__ == "__main__":
    raise SystemExit(main())

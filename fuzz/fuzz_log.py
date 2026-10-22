#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Atheris fuzz target for llmtop log parsers."""
import os, sys

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
	sys.path.insert(0, _PARENT)

try:
	import atheris
except ImportError:
	atheris = None

log = None

def check(data: bytes) -> None:
	global log
	if log is None:
		from llmtop_lib import log
	try:
		text = data.decode("utf-8", errors="replace")
		lines = text.splitlines()
		for line in lines:
			log.normalize_line(line)
		log.parse_router(lines)
	except Exception as e:
		raise AssertionError(f"log contract violation: {type(e).__name__}: {e}") from e

def TestOneInput(data: bytes) -> None:
	check(data)

def main():
	global log
	import atheris
	with atheris.instrument_imports():
		from llmtop_lib import log
	atheris.Setup(sys.argv, TestOneInput)
	atheris.Fuzz()

if __name__ == "__main__":
	main()

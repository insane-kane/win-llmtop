#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Atheris fuzz target for GGUF header parser."""
import os, struct, sys, time

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
	sys.path.insert(0, _PARENT)

try:
	import atheris
except ImportError:
	atheris = None

model = None
ALLOWED = (ValueError, struct.error, IndexError)

def check(data: bytes) -> None:
	global model
	if model is None:
		from llmtop_lib import model
	t0 = time.monotonic()
	try:
		model._parse(data, len(data))
	except ALLOWED:
		pass
	except Exception as e:
		raise AssertionError(f"_parse violation: {type(e).__name__}: {e}") from e
	if time.monotonic() - t0 > 1.0:
		raise AssertionError(f"_parse timeout: {time.monotonic() - t0:.2f}s")

def TestOneInput(data: bytes) -> None:
	check(data)

def main():
	global model
	import atheris
	with atheris.instrument_imports():
		from llmtop_lib import model
	atheris.Setup(sys.argv, TestOneInput)
	atheris.Fuzz()

if __name__ == "__main__":
	main()

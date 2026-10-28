#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Aaron Kane <software@kanenet.us>
"""Atheris fuzz target for argument parser."""
import os, sys

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
	sys.path.insert(0, _PARENT)

try:
	import atheris
except ImportError:
	atheris = None

model = None

def check(data: bytes) -> None:
	global model
	if model is None:
		from llmtop_lib import model
	text = data.decode("utf-8", errors="replace")
	argv = text.split(chr(0)) if chr(0) in text else text.split()
	try:
		model.parse_args(argv)
	except Exception as e:
		raise AssertionError(f"parse_args violation: {type(e).__name__}: {e}") from e

def TestOneInput(data: bytes) -> None:
	if atheris and hasattr(atheris, "FuzzedDataProvider"):
		fdp = atheris.FuzzedDataProvider(data)
		argv = [fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 64)) for _ in range(fdp.ConsumeIntInRange(0, 32))]
		check(chr(0).join(argv).encode("utf-8"))
	else:
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

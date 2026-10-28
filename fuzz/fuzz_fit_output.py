#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Aaron Kane <software@kanenet.us>
"""Atheris fuzz target for llama-fit-params output parser."""
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
	try:
		model.parse_fit_output(data.decode("utf-8", errors="replace"))
	except Exception as e:
		raise AssertionError(f"parse_fit_output violation: {type(e).__name__}: {e}") from e

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

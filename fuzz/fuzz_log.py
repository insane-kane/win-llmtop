#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Aaron Kane <software@kanenet.us>
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
		asm = log.Assembler("fuzz_model")
		for line in lines:
			log.normalize_line(line)
			done, a_events = asm.feed(line)
			for ev in a_events:
				msg = ev.get("msg")
				if msg is not None:
					assert log.ui.safe(msg) == msg
				detail = ev.get("detail")
				if detail is not None:
					assert log.ui.safe(detail) == detail
				preset = ev.get("preset")
				if preset is not None:
					assert log.ui.safe(preset) == preset
			for req in done:
				preset = req.get("preset")
				if preset is not None:
					assert log.ui.safe(preset) == preset
		assert log.ui.safe(asm.preset) == asm.preset
		events = log.parse_router(lines)
		for ev in events:
			msg = ev.get("msg")
			if msg is not None:
				assert log.ui.safe(msg) == msg
			detail = ev.get("detail")
			if detail is not None:
				assert log.ui.safe(detail) == detail
			preset = ev.get("preset")
			if preset is not None:
				assert log.ui.safe(preset) == preset
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

#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Aaron Kane <software@kanenet.us>
"""Deterministic regression tests running seeds and mutated inputs against fuzz harnesses.

Standard library only. Runs on Windows without Atheris installed.
"""

import pathlib
import random
import struct
import sys

HERE = pathlib.Path(__file__).resolve().parent
PARENT = HERE.parent
if str(PARENT) not in sys.path:
	sys.path.insert(0, str(PARENT))

from fuzz import fuzz_args, fuzz_fit_output, fuzz_gguf, fuzz_log


def mutate_bytes(data: bytes, rng: random.Random) -> bytes:
	"""Apply 1-3 deterministic mutations to a byte string."""
	if not data:
		return rng.randbytes(rng.randint(1, 16))
	mut = bytearray(data)
	action = rng.randint(0, 2)
	if action == 0 and len(mut) > 0:
		pos = rng.randint(0, len(mut) - 1)
		mut[pos] = rng.randint(0, 255)
	elif action == 1 and len(mut) > 1:
		pos = rng.randint(0, len(mut) - 1)
		del mut[pos]
	else:
		pos = rng.randint(0, len(mut))
		mut.insert(pos, rng.randint(0, 255))
	return bytes(mut)


def test_target(target_name: str, check_fn, corpus_dir: pathlib.Path, num_iterations: int = 2000) -> None:
	"""Run seeds, random bytes, and mutated seeds through check_fn."""
	seed_files = sorted(corpus_dir.glob("*"))
	seeds = [p.read_bytes() for p in seed_files if p.is_file()]
	assert seeds, f"no seeds found in {corpus_dir}"

	for seed in seeds:
		check_fn(seed)

	rng = random.Random(0)
	for i in range(num_iterations):
		if i % 2 == 0 or not seeds:
			length = rng.randint(0, 512)
			data = rng.randbytes(length)
		else:
			seed = rng.choice(seeds)
			data = mutate_bytes(seed, rng)
		check_fn(data)


def test_log():
	"""Seeds and mutations through fuzz_log.check."""
	test_target("log", fuzz_log.check, PARENT / "fuzz" / "corpus" / "log")


def test_gguf():
	"""Seeds and mutations through fuzz_gguf.check."""
	test_target("gguf", fuzz_gguf.check, PARENT / "fuzz" / "corpus" / "gguf")


def test_fit_output():
	"""Seeds and mutations through fuzz_fit_output.check."""
	test_target("fit_output", fuzz_fit_output.check, PARENT / "fuzz" / "corpus" / "fit_output")


def test_args():
	"""Seeds and mutations through fuzz_args.check."""
	test_target("args", fuzz_args.check, PARENT / "fuzz" / "corpus" / "args")


def test_allowed_exceptions_rule():
	"""Verify the contract: allowed exceptions pass, unhandled exceptions raise AssertionError."""
	fuzz_gguf.check(b"GGUF\x03\x00")
	fuzz_gguf.check(b"NOT_GGUF_FILE_HEADER")
	buf = bytearray(b"GGUF") + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", 1000) + b"short"
	fuzz_gguf.check(bytes(buf))


CASES = [
	("log", test_log),
	("gguf", test_gguf),
	("fit_output", test_fit_output),
	("args", test_args),
	("allowed_exceptions_rule", test_allowed_exceptions_rule),
]


def main():
	failed = 0
	for label, fn in CASES:
		try:
			fn()
			print(f"PASS {label}")
		except Exception as e:
			print(f"FAIL {label}: {e}")
			failed += 1
	if failed:
		return 1
	print("ALL PASS")
	return 0


if __name__ == "__main__":
	sys.exit(main())

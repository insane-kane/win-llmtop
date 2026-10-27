#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for llmtop_lib.model: flags, draft models, split GGUFs, and fit-params parsing.

Standard library only. All GGUF headers are synthesized in temporary folders using struct.pack.
Runnable as:
	python -I tools/win-llmtop/tests/test_model.py
"""

import builtins
import os
import pathlib
import shutil
import struct
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
PARENT = HERE.parent
if str(PARENT) not in sys.path:
	sys.path.insert(0, str(PARENT))

from llmtop_lib import model


def make_gguf(metadata, tensors, alignment=32, data_bytes=b""):
	"""Pack a valid GGUF v3 file in memory into bytes."""
	buf = bytearray()
	buf.extend(b"GGUF")
	buf.extend(struct.pack("<IQQ", 3, len(tensors), len(metadata)))

	def pack_str(s):
		raw = s.encode("utf-8")
		return struct.pack("<Q", len(raw)) + raw

	for k, v in metadata.items():
		buf.extend(pack_str(k))
		if isinstance(v, str):
			buf.extend(struct.pack("<I", 8))
			buf.extend(pack_str(v))
		elif isinstance(v, bool):
			buf.extend(struct.pack("<I?", 7, v))
		elif isinstance(v, int):
			buf.extend(struct.pack("<II", 4, v))
		elif isinstance(v, float):
			buf.extend(struct.pack("<If", 6, v))
		elif isinstance(v, list):
			buf.extend(struct.pack("<IIQ", 9, 4, len(v)))
			for item in v:
				buf.extend(struct.pack("<I", int(item)))
		else:
			raise ValueError(f"unsupported metadata type: {type(v)}")

	for name, dims, ty, off in tensors:
		buf.extend(pack_str(name))
		buf.extend(struct.pack("<I", len(dims)))
		for d in dims:
			buf.extend(struct.pack("<Q", d))
		buf.extend(struct.pack("<IQ", ty, off))

	pad_len = (alignment - (len(buf) % alignment)) % alignment
	buf.extend(b"\x00" * pad_len)
	buf.extend(data_bytes)
	return bytes(buf)


class TestModelAnalysis(unittest.TestCase):
	def setUp(self):
		self.tmpdir = tempfile.mkdtemp()

	def tearDown(self):
		shutil.rmtree(self.tmpdir, ignore_errors=True)

	def test_small_model_attn_kv(self):
		"""Small model with attn_k and attn_v: tensor count and sizes as built."""
		meta = {
			"general.architecture": "test",
			"test.block_count": 1,
			"test.context_length": 512,
		}
		tensors = [
			("token_embd.weight", (64, 128), 0, 0),
			("blk.0.attn_k.weight", (64, 32), 0, 32768),
			("blk.0.attn_v.weight", (64, 32), 0, 40960),
			("output.weight", (64, 128), 0, 49152),
		]
		raw = make_gguf(meta, tensors, data_bytes=b"\x00" * 32768)
		path = os.path.join(self.tmpdir, "model.gguf")
		with open(path, "wb") as f:
			f.write(raw)

		_, parsed_tensors = model.read_gguf(path)
		self.assertEqual(len(parsed_tensors), 4)
		self.assertEqual(parsed_tensors[0], ("token_embd.weight", (64, 128), "F32", 32768))
		self.assertEqual(parsed_tensors[1], ("blk.0.attn_k.weight", (64, 32), "F32", 8192))
		self.assertEqual(parsed_tensors[2], ("blk.0.attn_v.weight", (64, 32), "F32", 8192))
		self.assertEqual(parsed_tensors[3], ("output.weight", (64, 128), "F32", 32768))

	def test_fused_attn_qkv(self):
		"""Fused attn_qkv: KV derived from metadata is > 0 and equals expected width * context."""
		meta = {
			"general.architecture": "test",
			"test.block_count": 1,
			"test.context_length": 2048,
			"test.attention.head_count": 8,
			"test.attention.head_count_kv": 4,
			"test.attention.key_length": 64,
			"test.attention.value_length": 64,
		}
		tensors = [
			("token_embd.weight", (256, 512), 0, 0),
			("blk.0.attn_qkv.weight", (512, 1024), 0, 524288),
			("output.weight", (256, 512), 0, 2621440),
		]
		raw = make_gguf(meta, tensors, data_bytes=b"\x00" * 524288)
		path = os.path.join(self.tmpdir, "model.gguf")
		with open(path, "wb") as f:
			f.write(raw)

		analysis = model.analyze({"model": path})
		self.assertGreater(analysis["kv"]["full"], 0.0)

		# K width = head_count_kv * key_length = 4 * 64 = 256
		# V width = head_count_kv * value_length = 4 * 64 = 256
		# Default cache types are f16: bk = 2.0, bv = 2.0 bytes/element
		# Per token bytes = 256 * 2.0 + 256 * 2.0 = 1024.0 bytes/token
		self.assertEqual(analysis["kv"]["per_token"], 1024.0)

		# Total KV size in MiB = ctx (2048) * 1024 / (1024 * 1024) = 2.0 MiB
		self.assertEqual(analysis["kv"]["full"], 2.0)

	def test_two_shard_split(self):
		"""Two-shard split: tensors from both shards counted; missing second shard raises FileNotFoundError naming it."""
		meta_shard1 = {
			"general.architecture": "test",
			"split.count": 2,
			"split.no": 0,
		}
		tensors_shard1 = [
			("token_embd.weight", (64, 64), 0, 0),
		]
		raw_shard1 = make_gguf(meta_shard1, tensors_shard1, data_bytes=b"\x00" * 16384)
		path_shard1 = os.path.join(self.tmpdir, "model-00001-of-00002.gguf")
		with open(path_shard1, "wb") as f:
			f.write(raw_shard1)

		meta_shard2 = {
			"general.architecture": "test",
			"split.count": 2,
			"split.no": 1,
		}
		tensors_shard2 = [
			("output.weight", (64, 64), 0, 0),
		]
		raw_shard2 = make_gguf(meta_shard2, tensors_shard2, data_bytes=b"\x00" * 16384)
		path_shard2 = os.path.join(self.tmpdir, "model-00002-of-00002.gguf")
		with open(path_shard2, "wb") as f:
			f.write(raw_shard2)

		_, tensors = model.read_gguf(path_shard1)
		self.assertEqual(len(tensors), 2)
		self.assertEqual(tensors[0][0], "token_embd.weight")
		self.assertEqual(tensors[1][0], "output.weight")

		os.remove(path_shard2)
		with self.assertRaises(FileNotFoundError) as ctx:
			model.read_gguf(path_shard1)
		self.assertIn("model-00002-of-00002.gguf", str(ctx.exception))

	def test_last_tensor_size_from_file_size(self):
		"""Last-tensor size from the file size when tensor type is unknown."""
		meta = {
			"general.architecture": "test",
			"general.alignment": 32,
		}
		tensors = [
			("custom.weight", (10, 10), 999, 0),
		]
		data_len = 1234
		raw = make_gguf(meta, tensors, data_bytes=b"\xaa" * data_len)
		path = os.path.join(self.tmpdir, "model.gguf")
		with open(path, "wb") as f:
			f.write(raw)

		_, parsed_tensors = model.read_gguf(path)
		self.assertEqual(len(parsed_tensors), 1)
		name, _, ty, nbytes = parsed_tensors[0]
		self.assertEqual(name, "custom.weight")
		self.assertEqual(ty, "T999")
		self.assertEqual(nbytes, data_len)

	def test_parse_args_and_flag_precedence(self):
		"""parse_args on argv with short flags, repeated -ot, and last-flag precedence."""
		argv = [
			"llama-server",
			"-m", "m.gguf",
			"-c", "8192",
			"-ngl", "99",
			"-ot", "a=CPU",
			"-ot", "b=CPU",
			"--ctx-size=4096",
			"--spec-draft-model", "d.gguf",
		]
		flags = model.parse_args(argv)
		self.assertEqual(flags["model"], "m.gguf")
		# llama.cpp takes the last ctx-size specified on the command line
		self.assertEqual(flags["ctx-size"], "4096")
		self.assertEqual(flags["n-gpu-layers"], "99")
		self.assertEqual(flags["override-tensor"], "a=CPU,b=CPU")
		self.assertEqual(flags["spec-draft-model"], "d.gguf")

	def test_draft_path_each_flag(self):
		"""draft_path resolution with model-draft, spec-draft-model, both, and none."""
		self.assertEqual(model.draft_path({"model-draft": "d1.gguf"}), "d1.gguf")
		self.assertEqual(model.draft_path({"spec-draft-model": "d2.gguf"}), "d2.gguf")
		# spec-draft-model takes precedence over model-draft when both are present
		self.assertEqual(model.draft_path({"model-draft": "d1.gguf", "spec-draft-model": "d2.gguf"}), "d2.gguf")
		self.assertIsNone(model.draft_path({}))
		self.assertIsNone(model.draft_path({"model": "m.gguf"}))

	def test_fit_projection_output_parser(self):
		"""fit_projection output parser on canned stdout: one GPU, two GPUs, CPU-only."""
		# Format from llama.cpp source common/fit.cpp (common_memory_breakdown_print)
		stdout_one_gpu = (
			"common_memory_breakdown_print: | memory breakdown [MiB]          | total   free    self    model   context compute unaccounted |\n"
			"common_memory_breakdown_print: | - CUDA0 (NVIDIA GeForce RTX 4090) | 24564 = 23000 + ( 1500 = 1000 + 400 + 100 )\n"
			"common_memory_breakdown_print: | - Host                          | 1928 = 919 + 4 + 1004\n"
		)
		res1 = model.parse_fit_output(stdout_one_gpu)
		self.assertIsNotNone(res1)
		self.assertEqual(len(res1["gpus"]), 1)
		self.assertEqual(res1["gpu"]["device"], "CUDA0")
		self.assertEqual(res1["gpu"]["name"], "NVIDIA GeForce RTX 4090")
		self.assertEqual(res1["gpu"]["total"], 24564)
		self.assertEqual(res1["gpu"]["free"], 23000)
		self.assertEqual(res1["gpu"]["self"], 1500)
		self.assertEqual(res1["gpu"]["model"], 1000)
		self.assertEqual(res1["gpu"]["context"], 400)
		self.assertEqual(res1["gpu"]["compute"], 100)
		self.assertEqual(res1["host"]["self"], 1928)
		self.assertEqual(res1["host"]["model"], 919)
		self.assertEqual(res1["host"]["context"], 4)
		self.assertEqual(res1["host"]["compute"], 1004)

		stdout_two_gpus = (
			"common_memory_breakdown_print: | memory breakdown [MiB]          | total   free    self    model   context compute unaccounted |\n"
			"common_memory_breakdown_print: | - CUDA0 (NVIDIA GeForce RTX 4090) | 24564 = 23000 + ( 1500 = 1000 + 400 + 100 )\n"
			"common_memory_breakdown_print: | - CUDA1 (NVIDIA GeForce RTX 3090) | 24564 = 22000 + ( 2000 = 1200 + 600 + 200 )\n"
			"common_memory_breakdown_print: | - Host                          | 1928 = 919 + 4 + 1004\n"
		)
		res2 = model.parse_fit_output(stdout_two_gpus)
		self.assertIsNotNone(res2)
		self.assertEqual(len(res2["gpus"]), 2)
		self.assertEqual(res2["gpus"][0]["device"], "CUDA0")
		self.assertEqual(res2["gpus"][1]["device"], "CUDA1")
		self.assertEqual(res2["gpu"], res2["gpus"][0])

		stdout_cpu_only = (
			"common_memory_breakdown_print: | memory breakdown [MiB]          | total   free    self    model   context compute unaccounted |\n"
			"common_memory_breakdown_print: | - Host                          | 1928 = 919 + 4 + 1004\n"
		)
		res3 = model.parse_fit_output(stdout_cpu_only)
		self.assertIsNotNone(res3)
		self.assertEqual(res3["gpus"], [])
		self.assertNotIn("gpu", res3)
		self.assertEqual(res3["host"]["self"], 1928)
		self.assertEqual(res3["host"]["model"], 919)

	def test_gguf_invalid_alignment(self):
		"""GGUF header with general.alignment <= 0 raises ValueError, not ZeroDivisionError."""
		raw = make_gguf({"general.alignment": 0}, [])
		with self.assertRaises(ValueError):
			model._parse(raw, len(raw))

	def test_corrupt_header_read_is_capped(self):
		"""A key length past the end of the file raises ValueError without reading past HEADER_CAP (or the file size)."""
		header = b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", 2**40)
		file_size = 20 * 1024 * 1024
		path = os.path.join(self.tmpdir, "huge_key.gguf")
		with open(path, "wb") as f:
			f.write(header + b"\x00" * (file_size - len(header)))

		real_open = builtins.open
		read_total = [0]

		def counting_open(*args, **kwargs):
			f = real_open(*args, **kwargs)
			real_read = f.read

			def read(size=-1):
				data = real_read(size)
				read_total[0] += len(data)
				return data

			f.read = read
			return f

		with mock.patch.object(builtins, "open", counting_open):
			with self.assertRaises(ValueError):
				model._read_single(path)
		self.assertLessEqual(read_total[0], file_size + 8 * 1024 * 1024)

		read_total[0] = 0
		with mock.patch.object(builtins, "open", counting_open), mock.patch.object(model, "HEADER_CAP", 8 * 1024 * 1024):
			with self.assertRaises(ValueError):
				model._read_single(path)
		self.assertLessEqual(read_total[0], 8 * 1024 * 1024)


CASES = [
	("small_model_attn_kv", "test_small_model_attn_kv"),
	("fused_attn_qkv", "test_fused_attn_qkv"),
	("two_shard_split", "test_two_shard_split"),
	("last_tensor_size_from_file_size", "test_last_tensor_size_from_file_size"),
	("parse_args_and_flag_precedence", "test_parse_args_and_flag_precedence"),
	("draft_path_each_flag", "test_draft_path_each_flag"),
	("fit_projection_output_parser", "test_fit_projection_output_parser"),
	("gguf_invalid_alignment", "test_gguf_invalid_alignment"),
	("corrupt_header_read_is_capped", "test_corrupt_header_read_is_capped"),
]


def main():
	failed = 0
	for label, method_name in CASES:
		test_case = TestModelAnalysis(methodName=method_name)
		try:
			test_case.setUp()
			getattr(test_case, method_name)()
			test_case.tearDown()
		except Exception as e:
			failed += 1
			print(f"FAIL {label}: {type(e).__name__}: {e}")
		else:
			print(f"PASS {label}")
	if failed:
		print(f"{failed} of {len(CASES)} cases failed")
		sys.exit(1)
	print("ALL PASS")


if __name__ == "__main__":
	main()

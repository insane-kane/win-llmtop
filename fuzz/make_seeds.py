#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate initial seed corpora for llmtop fuzz targets."""

import pathlib
import struct

HERE = pathlib.Path(__file__).resolve().parent
CORPUS = HERE / "corpus"


def pack_str(s: str) -> bytes:
	raw = s.encode("utf-8")
	return struct.pack("<Q", len(raw)) + raw


def make_gguf(metadata: dict, tensors: list, alignment: int = 32, data_bytes: bytes = b"") -> bytes:
	buf = bytearray()
	buf.extend(b"GGUF")
	buf.extend(struct.pack("<IQQ", 3, len(tensors), len(metadata)))

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


def main():
	# 1. GGUF seeds
	gguf_dir = CORPUS / "gguf"
	gguf_dir.mkdir(parents=True, exist_ok=True)

	meta1 = {"general.architecture": "test"}
	tensors1 = [("token_embd.weight", (64, 128), 0, 0)]
	(gguf_dir / "seed_minimal.gguf").write_bytes(make_gguf(meta1, tensors1, data_bytes=b"\x00" * 32))

	meta2 = {
		"general.architecture": "llama",
		"llama.block_count": 2,
		"llama.context_length": 2048,
		"llama.rope.freq_base": 10000.0,
		"general.alignment": 32,
	}
	tensors2 = [
		("token_embd.weight", (64, 128), 0, 0),
		("blk.0.attn_k.weight", (64, 32), 0, 32768),
	]
	(gguf_dir / "seed_scalars.gguf").write_bytes(make_gguf(meta2, tensors2, data_bytes=b"\x00" * 32))

	meta3 = {
		"general.architecture": "split",
		"split.count": 2,
		"split.no": 0,
		"general.alignment": 32,
	}
	tensors3 = [("output.weight", (32, 64), 0, 0)]
	(gguf_dir / "seed_split.gguf").write_bytes(make_gguf(meta3, tensors3, data_bytes=b"\x00" * 32))

	# 2. Log seeds
	log_dir = CORPUS / "log"
	log_dir.mkdir(parents=True, exist_ok=True)
	(log_dir / "seed_b11669.log").write_text(
		"0.00.195.785 I srv  llama_server: initializing ...\n"
		"0.05.059.171 I srv  llama_server: model loaded\n"
		"0.05.110.163 I slot get_availabl: id  0 | task -1 | selected slot by LRU, n_tokens = 0, t_last = -1\n"
		"0.10.858.458 I slot print_timing: id  0 | task 0 | prompt processing, n_tokens =   3662, progress = 0.88, t =   5.43 s / 673.81 tokens per second\n"
		"0.14.307.585 I slot print_timing: id  0 | task 0 |       total time =    9197.48 ms /  4308 tokens\n",
		encoding="utf-8",
	)
	(log_dir / "seed_router.log").write_text(
		"0.00.001.000 I join: request for name=modelA queued at position 0\n"
		"0.00.002.000 I ensure_model: slot available, loading queued model name=modelA\n"
		"0.00.003.000 I tick: evicting idle LRU name=modelB\n"
		"0.00.004.000 I run: instance name=modelA exited with status 0\n"
		"0.00.005.000 W got exception: {\"message\":\"request failed\"}\n",
		encoding="utf-8",
	)
	(log_dir / "seed_forwarded.log").write_text(
		"[8080] I srv  llama_server: model loaded\n"
		"[8080] W srv  warning: slot busy\n"
		"[ 8081] I srv  llama_server: model loaded\n",
		encoding="utf-8",
	)

	# 3. Fit output seeds
	fit_dir = CORPUS / "fit_output"
	fit_dir.mkdir(parents=True, exist_ok=True)
	(fit_dir / "seed_one_gpu.txt").write_text(
		"common_memory_breakdown_print: | memory breakdown [MiB]          | total   free    self    model   context compute unaccounted |\n"
		"common_memory_breakdown_print: | - CUDA0 (NVIDIA GeForce RTX 4090) | 24564 = 23000 + ( 1500 = 1000 + 400 + 100 )\n"
		"common_memory_breakdown_print: | - Host                          | 1928 = 919 + 4 + 1004\n",
		encoding="utf-8",
	)
	(fit_dir / "seed_two_gpus.txt").write_text(
		"common_memory_breakdown_print: | memory breakdown [MiB]          | total   free    self    model   context compute unaccounted |\n"
		"common_memory_breakdown_print: | - CUDA0 (NVIDIA GeForce RTX 4090) | 24564 = 23000 + ( 1500 = 1000 + 400 + 100 )\n"
		"common_memory_breakdown_print: | - CUDA1 (NVIDIA GeForce RTX 3090) | 24564 = 22000 + ( 2000 = 1200 + 600 + 200 )\n"
		"common_memory_breakdown_print: | - Host                          | 1928 = 919 + 4 + 1004\n",
		encoding="utf-8",
	)
	(fit_dir / "seed_cpu_only.txt").write_text(
		"common_memory_breakdown_print: | memory breakdown [MiB]          | total   free    self    model   context compute unaccounted |\n"
		"common_memory_breakdown_print: | - Host                          | 1928 = 919 + 4 + 1004\n",
		encoding="utf-8",
	)

	# 4. Args seeds
	args_dir = CORPUS / "args"
	args_dir.mkdir(parents=True, exist_ok=True)
	(args_dir / "seed_basic.bin").write_bytes(
		b"llama-server\x00-m\x00model.gguf\x00-c\x004096\x00-ngl\x0033"
	)
	(args_dir / "seed_flags.bin").write_bytes(
		b"llama-server\x00--flash-attn\x00on\x00--cpu-moe\x00--no-kv-offload\x00--override-tensor\x00a=CPU"
	)
	(args_dir / "seed_draft.bin").write_bytes(
		b"llama-server\x00-m\x00model.gguf\x00--spec-draft-model\x00draft.gguf\x00--threads\x008"
	)
	print("Seeds generated successfully.")


if __name__ == "__main__":
	main()

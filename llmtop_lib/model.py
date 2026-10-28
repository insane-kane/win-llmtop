# SPDX-License-Identifier: GPL-3.0-or-later
"""Static analysis of a GGUF model for llmtop: where every tensor lives and what the KV cache and recurrent state cost.

llama-server prints no memory map at its default log level, so this derives one from the GGUF header and the preset's flags, and
cross-checks it against llama.cpp's own projection (llama-fit-params, which reads the header and allocates nothing).
"""

import os
import re
import struct
import subprocess

MIB = 2**20
HEADER_CAP = 256 * MIB  # the largest GGUF header win-llmtop reads: a corrupt length must never pull a whole model into memory

# ggml type id: (name, elements per block, bytes per block)
GGML = {0: ("F32", 1, 4), 1: ("F16", 1, 2), 2: ("Q4_0", 32, 18), 3: ("Q4_1", 32, 20), 6: ("Q5_0", 32, 22), 7: ("Q5_1", 32, 24), 8: ("Q8_0", 32, 34),
	9: ("Q8_1", 32, 36), 10: ("Q2_K", 256, 84), 11: ("Q3_K", 256, 110), 12: ("Q4_K", 256, 144), 13: ("Q5_K", 256, 176), 14: ("Q6_K", 256, 210),
	15: ("Q8_K", 256, 292), 16: ("IQ2_XXS", 256, 66), 17: ("IQ2_XS", 256, 74), 18: ("IQ3_XXS", 256, 98), 19: ("IQ1_S", 256, 50), 20: ("IQ4_NL", 32, 18),
	21: ("IQ3_S", 256, 110), 22: ("IQ2_S", 256, 82), 23: ("IQ4_XS", 256, 136), 24: ("I8", 1, 1), 25: ("I16", 1, 2), 26: ("I32", 1, 4), 27: ("I64", 1, 8),
	28: ("F64", 1, 8), 29: ("IQ1_M", 256, 56), 30: ("BF16", 1, 2), 34: ("TQ1_0", 256, 54), 35: ("TQ2_0", 256, 66), 39: ("MXFP4", 32, 17)}
SCALAR = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
# bytes per KV cache element
BPE = {"f32": 4.0, "f16": 2.0, "bf16": 2.0, "q8_0": 34 / 32, "q5_1": 24 / 32, "q5_0": 22 / 32, "q4_1": 20 / 32, "q4_0": 18 / 32, "iq4_nl": 18 / 32}
# flags that change memory use; passed to llama-fit-params
FIT_FLAGS = ("model", "ctx-size", "batch-size", "ubatch-size", "cache-type-k", "cache-type-v", "flash-attn", "n-gpu-layers", "parallel", "override-tensor",
	"tensor-split", "split-mode", "main-gpu", "swa-full", "no-kv-offload", "kv-unified", "cpu-moe", "n-cpu-moe", "device")  # no draft flags: llama-fit-params rejects --model-draft (b11669), and this tuple keys calibration.json
RE_FIT_DEV = re.compile(r"-\s+(\S+)\s+\(([^)]*)\)\s*\|\s*(\d+)\s*=\s*(-?\d+)\s*\+\s*\(\s*(\d+)\s*=\s*(\d+)\s*\+\s*(\d+)\s*\+\s*(\d+)\s*\)")
RE_FIT_HOST = re.compile(r"-\s+Host\s*\|\s*(\d+)\s*=\s*(\d+)\s*\+\s*(\d+)\s*\+\s*(\d+)")
RE_BLK = re.compile(r"^blk\.(\d+)\.(.*)$")
RE_LOCAL_PATH = re.compile(r'[A-Za-z]:[\\/][^:*?"<>|\x00-\x1f]*\Z')  # a drive letter, a separator, then no stream colon, wildcard or control character
DEVICE_NAMES = frozenset({"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)})

# llama.cpp short flag to long flag mappings from common/arg.cpp
SHORT_FLAGS = {
	"m": "model",
	"c": "ctx-size",
	"ngl": "n-gpu-layers",
	"ot": "override-tensor",
	"md": "model-draft",
	"ctk": "cache-type-k",
	"ctv": "cache-type-v",
	"b": "batch-size",
	"ub": "ubatch-size",
	"np": "parallel",
	"fa": "flash-attn",
	"ts": "tensor-split",
	"sm": "split-mode",
	"mg": "main-gpu",
	"dev": "device",
	"cmoe": "cpu-moe",
	"ncmoe": "n-cpu-moe",
	"nkvo": "no-kv-offload",
}
BOOL_FLAGS = {"cpu-moe", "no-kv-offload", "swa-full", "kv-unified"}

_DEFAULTS = {}
_ALIASES = {}


class ServerDefaults(dict):
	"""dict subclass holding server defaults with an attached `aliases` map."""
	aliases: dict

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.aliases = {}


def local_path(p):
	r"""True for an absolute path on a local drive letter (`X:\...` or `X:/...`): the only kind of file path llmtop opens on a server's say-so.

	Network paths (`\\host\share`, `//host/x`, `\\?\...`, `\\.\...`), relative and drive-relative paths, values that are not text,
	streams (a second colon), wildcards and device names (CON, NUL, COM1, ...) are all refused.
	"""
	if not isinstance(p, str) or not RE_LOCAL_PATH.match(p):
		return False
	return not any(part.split(".")[0].rstrip(" ").upper() in DEVICE_NAMES for part in p.replace("/", "\\").split("\\")[1:])


def server_defaults(exe):
	"""{long flag name: default text} taken from `llama-server --help`, which states each default; cached per executable.

	`exe` must be the image path of the llama-server llmtop watches. Anything else (None, a name, a path that is not a file) starts no program and yields no defaults.
	"""
	out = ServerDefaults()
	if not isinstance(exe, str) or not os.path.isabs(exe) or not os.path.isfile(exe):
		return out
	if exe in _DEFAULTS:
		return _DEFAULTS[exe]
	aliases = {}
	try:
		r = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
		text = r.stdout + r.stderr
	except (OSError, subprocess.TimeoutExpired):
		text = ""
	names, block = [], []

	def close():
		m = re.search(r"\(default: ([^,)]+)", " ".join(block)) if names else None
		for n in names if m else ():
			out[n] = m.group(1).strip()

	for line in text.splitlines():
		if line.startswith("-"):
			close()
			parts = re.split(r"\s{3,}", re.sub(r",\s+", ", ", line.strip()), maxsplit=1)
			long_names = re.findall(r"--([a-z0-9-]+)", parts[0])
			short_names = re.findall(r"(?:^|[,\s])-(?!-)([a-zA-Z0-9_-]+)", parts[0])
			if short_names and long_names:
				for s in short_names:
					aliases[s] = long_names[0]
			names, block = long_names, parts[1:]
		elif line.startswith(" ") and names:
			block.append(line.strip())
	close()
	out.aliases = aliases
	_DEFAULTS[exe] = out
	_ALIASES[exe] = aliases
	return out


def server_aliases(exe):
	"""{short flag: long flag} alias map parsed from `llama-server --help`; cached per executable."""
	if exe not in _ALIASES:
		server_defaults(exe)
	return _ALIASES.get(exe, {})


def draft_path(flags):
	"""Path to the speculative decoding draft model from flags, or None.

	Per llama.cpp common/arg.cpp, --spec-draft-model and --model-draft configure the same draft model.
	If both are set in flags, spec-draft-model (the canonical modern option) takes precedence.
	"""
	if not flags:
		return None
	val = flags.get("spec-draft-model")
	if val is None or val is True:
		val = flags.get("model-draft")
	if isinstance(val, str) and val:
		return val
	return None


def setting(flags, defaults, key, *aliases):
	"""(value, source): the preset's own flag, else the default `--help` states, else None."""
	for k in (key,) + aliases:
		if flags.get(k) not in (None, True):
			return flags[k], "flag"
	for k in (key,) + aliases:
		if k in defaults:
			return defaults[k], "default"
	return None, "unknown"


def parse_args(argv, aliases=None):
	"""Router status args (['llama-server.exe', '--flag', 'value', ...]) -> {flag: value, or True for a bare flag}."""
	alias_map = SHORT_FLAGS.copy()
	if aliases:
		for k, v in aliases.items():
			alias_map[k.lstrip("-")] = v.lstrip("-")
	flags, i = {}, 1
	while i < len(argv):
		t = argv[i]
		name, val = None, None
		if "=" in t and (t.startswith("--") or (t.startswith("-") and not (len(t) > 1 and t[1].isdigit()))):
			raw_name, raw_val = t.split("=", 1)
			name = raw_name[2:] if raw_name.startswith("--") else alias_map.get(raw_name[1:], raw_name[1:])
			val = raw_val
			i += 1
		elif t.startswith("--"):
			name = t[2:]
			if name in BOOL_FLAGS:
				val = True
				i += 1
			elif name == "flash-attn" and i + 1 < len(argv) and argv[i + 1] in ("on", "off", "auto"):
				val = argv[i + 1]
				i += 2
			elif i + 1 < len(argv):
				nxt = argv[i + 1]
				is_flag = nxt.startswith("--") or (nxt.startswith("-") and len(nxt) > 1 and not (nxt[1].isdigit() or (nxt[1] == "." and len(nxt) > 2 and nxt[2].isdigit())))
				if not is_flag:
					val = nxt
					i += 2
				else:
					val = True
					i += 1
			else:
				val = True
				i += 1
		elif t.startswith("-") and len(t) > 1 and not (t[1].isdigit() or (t[1] == "." and len(t) > 2 and t[2].isdigit())):
			raw = t[1:]
			name = alias_map.get(raw, raw)
			if name in BOOL_FLAGS:
				val = True
				i += 1
			elif name == "flash-attn" and i + 1 < len(argv) and argv[i + 1] in ("on", "off", "auto"):
				val = argv[i + 1]
				i += 2
			elif i + 1 < len(argv):
				nxt = argv[i + 1]
				is_flag = nxt.startswith("--") or (nxt.startswith("-") and len(nxt) > 1 and not (nxt[1].isdigit() or (nxt[1] == "." and len(nxt) > 2 and nxt[2].isdigit())))
				if not is_flag:
					val = nxt
					i += 2
				else:
					val = True
					i += 1
			else:
				val = True
				i += 1
		else:
			i += 1
			continue

		if name:
			if name == "override-tensor":
				flags[name] = f"{flags[name]},{val}" if name in flags else str(val)
			else:
				flags[name] = val
	return flags


def _int(v, default=0):
	try:
		return int(v)
	except (TypeError, ValueError):
		return default


# ---- GGUF header ---------------------------------------------------------------------------------------------------

def read_gguf(path):
	"""(metadata, tensors) from the GGUF header; tensors are (name, dims, type name, bytes). Long arrays (the vocabulary) are skipped.

	For split GGUFs, merges tensors across all shards in the same directory.
	"""
	meta, tensors = _read_single(path)
	filename = os.path.basename(path)
	m = re.search(r"^(.*?)-(\d{5})-of-(\d{5})\.gguf$", filename, re.IGNORECASE)
	if m:
		prefix = m.group(1)
		curr_idx = int(m.group(2))
		total_shards = int(m.group(3))
	elif int(meta.get("split.count", 0)) > 1:
		total_shards = int(meta["split.count"])
		curr_idx = int(meta.get("split.no", 0)) + 1
		stem = os.path.splitext(filename)[0]
		prefix = stem
	else:
		total_shards = 1
		curr_idx = 1

	if total_shards > 1:
		dirpath = os.path.dirname(path)
		all_tensors = []
		for i in range(1, total_shards + 1):
			shard_name = f"{prefix}-{i:05d}-of-{total_shards:05d}.gguf"
			shard_path = os.path.join(dirpath, shard_name)
			if i == curr_idx or os.path.abspath(shard_path) == os.path.abspath(path):
				all_tensors.extend(tensors)
			else:
				if not os.path.exists(shard_path):
					raise FileNotFoundError(f"missing shard: {shard_name}")
				_, s_tensors = _read_single(shard_path)
				all_tensors.extend(s_tensors)
		tensors = all_tensors

	return meta, tensors


def _read_single(path):
	file_size = os.path.getsize(path)
	limit = min(HEADER_CAP, file_size)
	size = min(8 << 20, limit)
	while True:
		with open(path, "rb") as f:
			buf = f.read(size)
		try:
			return _parse(buf, file_size)
		except (struct.error, IndexError):
			if size >= limit:
				raise ValueError("GGUF header is larger than 256 MiB or corrupt") from None
			if len(buf) < size:
				raise
			size = min(size * 4, limit)


def _parse(b, file_size):
	magic, version, n_tensors, n_kv = struct.unpack_from("<4sIQQ", b, 0)
	if magic != b"GGUF" or version < 2:
		raise ValueError("not a GGUF v2/v3 file")
	o = 24

	def string(o):
		n = struct.unpack_from("<Q", b, o)[0]
		if o + 8 + n > len(b):
			raise IndexError
		return b[o + 8:o + 8 + n].decode("utf-8", "replace"), o + 8 + n

	def value(t, o):
		if t in SCALAR:
			fmt = SCALAR[t]
			return struct.unpack_from(fmt, b, o)[0], o + struct.calcsize(fmt)
		if t == 8:
			return string(o)
		et, n = struct.unpack_from("<IQ", b, o)
		o += 12
		if et in SCALAR:
			fmt = SCALAR[et]
			end = o + n * struct.calcsize(fmt)
			if end > len(b):
				raise IndexError
			return (list(struct.unpack_from("<%d%s" % (n, fmt[1]), b, o)) if n <= 4096 else None), end
		if et == 8:
			for _ in range(n):
				o += 8 + struct.unpack_from("<Q", b, o)[0]
				if o > len(b):  # check inside the loop: a huge length would make the next unpack_from raise OverflowError
					raise IndexError
			return None, o
		raise ValueError("unsupported GGUF array type %d" % et)

	meta = {}
	for _ in range(n_kv):
		key, o = string(o)
		t = struct.unpack_from("<I", b, o)[0]
		meta[key], o = value(t, o + 4)
	raw = []
	for _ in range(n_tensors):
		name, o = string(o)
		nd = struct.unpack_from("<I", b, o)[0]
		dims = struct.unpack_from("<%dQ" % nd, b, o + 4)
		o += 4 + 8 * nd
		ty, off = struct.unpack_from("<IQ", b, o)
		o += 12
		raw.append((name, dims, ty, off))
	order = sorted(range(len(raw)), key=lambda i: raw[i][3])
	gap = {}
	for a, c in zip(order, order[1:], strict=False):
		gap[a] = raw[c][3] - raw[a][3]
	alignment = _int(meta.get("general.alignment", 32), default=32)
	if alignment <= 0:
		raise ValueError("invalid GGUF alignment")
	data_start = o + (alignment - (o % alignment)) % alignment
	if order:
		last_idx = order[-1]
		gap[last_idx] = max(0, file_size - (data_start + raw[last_idx][3]))
	tensors = []
	for i, (name, dims, ty, _off) in enumerate(raw):
		n = 1
		for d in dims:
			n *= d
		if ty in GGML:
			label, block, nbytes = GGML[ty]
			tensors.append((name, dims, label, n // block * nbytes))
		else:
			tensors.append((name, dims, "T%d" % ty, gap.get(i, 0)))
	return meta, tensors


# ---- llama.cpp's own projection ----------------------------------------------------------------------------------------

def parse_fit_output(text):
	"""Parse llama-fit-params output into {'gpus': [...], 'gpu': {...}, 'host': {...}} or None."""
	gpus = []
	res = {}
	for line in text.splitlines():
		if m := RE_FIT_DEV.search(line):
			dev = {"device": m.group(1), "name": m.group(2), "total": int(m.group(3)), "free": int(m.group(4)),
				"self": int(m.group(5)), "model": int(m.group(6)), "context": int(m.group(7)), "compute": int(m.group(8))}
			gpus.append(dev)
		elif "host" not in res and (m := RE_FIT_HOST.search(line)):
			res["host"] = {"self": int(m.group(1)), "model": int(m.group(2)), "context": int(m.group(3)), "compute": int(m.group(4))}
	if gpus:
		res["gpus"] = gpus
		res["gpu"] = gpus[0]
	elif "host" in res:
		res["gpus"] = []
	if "gpu" in res or "host" in res:
		return res
	return None


def fit_projection(server_exe, flags, timeout=60):
	"""llama.cpp's memory projection for these flags: {'gpus': [...], 'gpu': {...MiB}, 'host': {...MiB}} or None. Allocates nothing; takes about a second.

	`server_exe` is the image path of the llama-server llmtop watches. llama-fit-params is looked for only in that folder, never on PATH or in the
	current folder; with no such file (or no usable path) nothing is started and the result is None.
	"""
	if "model" not in flags or not isinstance(server_exe, str) or not os.path.isabs(server_exe) or not local_path(flags["model"]):
		return None
	folder = os.path.dirname(server_exe)
	exe = next((c for c in (os.path.join(folder, "llama-fit-params.exe"), os.path.join(folder, "llama-fit-params")) if os.path.isfile(c)), None)
	if not exe:
		return None
	argv = [exe, "-lv", "4"]
	for k in FIT_FLAGS:
		if k in flags:
			argv += ["--" + k] if flags[k] is True else ["--" + k, str(flags[k])]
	try:
		out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
	except (OSError, subprocess.TimeoutExpired):
		return None
	return parse_fit_output(out.stdout + out.stderr)


# ---- memory map --------------------------------------------------------------------------------------------------------

def _classify(name, layer, kind, n_main):
	"""Component a tensor belongs to."""
	if layer is not None and layer >= n_main:
		return "MTP head"
	if name.startswith(("token_embd", "per_layer_token_embd", "per_layer_model_proj", "per_layer_proj_norm")):
		return "embeddings"
	if layer is None:
		return "output head" if name.startswith("output") else "norms and other"
	base = RE_BLK.match(name).group(2)
	if "_exps" in base:
		return "MoE experts"
	if "_shexp" in base:
		return "shared expert"
	if base.startswith(("ffn_gate_inp", "exp_probs_b")):
		return "MoE router"
	if "norm" in base or base.endswith((".scale", "_scale")):
		return "norms and other"
	if base.startswith("ssm_") or (kind == "linear" and base.startswith(("attn_qkv", "attn_gate"))):
		return "linear attention (SSM)"
	if base.startswith("attn_"):
		return "attention"
	if base.startswith("ffn_"):
		return "FFN (dense)"
	return "norms and other"


def _place(name, layer, n_layer, ngl, overrides, flags):
	"""(on the host?, why) for one tensor: --override-tensor, --cpu-moe, --n-gpu-layers, and llama.cpp's rule that input embeddings stay on the host."""
	for rx, buft in overrides:
		if rx.search(name):
			return buft.startswith("CPU"), "-ot rule"
	if "_exps" in name and flags.get("cpu-moe"):
		return True, "--cpu-moe"
	if "_exps" in name and layer is not None and layer < _int(flags.get("n-cpu-moe")):
		return True, "--n-cpu-moe"
	if ngl <= 0:
		return True, "--n-gpu-layers"
	if name.startswith(("token_embd", "per_layer_token_embd")):
		return True, "llama.cpp default"
	if layer is None:
		if ngl <= n_layer:
			return True, "--n-gpu-layers"
		return False, ""
	if layer < n_layer - ngl:
		return True, "--n-gpu-layers"
	return False, ""


def analyze(flags, fit=None, defaults=None, n_slots=None):
	"""Memory map for a preset's flags: weights by component, per-layer sizes, KV cache and recurrent state, checkpoint sizes. MiB throughout."""
	if not local_path(flags["model"]):
		raise ValueError("the model path is not an absolute path on a local drive")
	meta, tensors = read_gguf(flags["model"])
	embd = next((t for t in tensors if t[0] == "token_embd.weight"), None)
	tied = bool(embd) and not any(t[0] == "output.weight" for t in tensors)
	if tied:
		tensors.append(("output.weight", embd[1], embd[2], embd[3]))  # tied output head: llama.cpp keeps a second copy on the output device
	arch = meta.get("general.architecture", "")
	g = lambda k, d=None: meta.get("%s.%s" % (arch, k), d)
	n_layer = _int(g("block_count"))
	n_mtp = _int(g("nextn_predict_layers"))
	n_main = n_layer - n_mtp
	defaults, assumed = defaults or {}, []
	v, _ = setting(flags, defaults, "n-gpu-layers")
	if v is None or str(v) in ("auto", "-1"):
		ngl = 999
		assumed.append("--n-gpu-layers is %s: every layer is assumed to be offloaded" % ("auto" if v else "not set"))
	else:
		ngl = 999 if str(v) == "all" else _int(v, 999)
	overrides = []
	for part in str(flags.get("override-tensor", "")).split(","):
		if "=" in part:
			rx, buft = part.rsplit("=", 1)
			overrides.append((re.compile(rx), buft.strip().upper()))

	n_head = _int(g("attention.head_count"))
	n_head_kv = _int(g("attention.head_count_kv") or n_head)
	k_len = _int(g("attention.key_length"))
	v_len = _int(g("attention.value_length"))
	if not k_len:
		embd_len = _int(g("embedding_length"))
		k_len = (embd_len // n_head) if n_head else 0
	if not v_len:
		v_len = k_len
	meta_kd = n_head_kv * k_len
	meta_vd = n_head_kv * v_len

	by_layer = {}
	for name, dims, _ty, nbytes in tensors:
		m = RE_BLK.match(name)
		if m:
			by_layer.setdefault(int(m.group(1)), {})[m.group(2)] = (dims, nbytes)
	swa_pattern = g("attention.sliding_window_pattern")
	n_swa = _int(g("attention.sliding_window"))
	kinds, kv_dims = {}, {}
	for i in range(n_layer):
		names = by_layer.get(i, {})
		if any(n.startswith("ssm_") for n in names):
			kinds[i] = "linear"
		elif any(n.startswith("attn_") for n in names):
			kinds[i] = "swa" if n_swa and isinstance(swa_pattern, list) and i < len(swa_pattern) and swa_pattern[i] else "full"
			k = names.get("attn_k.weight", (None, 0))[0]
			v = names.get("attn_v.weight", (None, 0))[0]
			if k and len(k) > 1:
				kv_dims[i] = (k[1], v[1] if (v and len(v) > 1) else k[1])
			elif meta_kd > 0 and meta_vd > 0:
				kv_dims[i] = (meta_kd, meta_vd)
		else:
			kinds[i] = "other"
		if i >= n_main:
			kinds[i] = "mtp"

	groups, layers, top, roster = {}, {i: {"kind": kinds[i], "mib": 0.0, "gpu": 0.0} for i in range(n_layer)}, [], []
	for name, dims, ty, nbytes in tensors:
		m = RE_BLK.match(name)
		layer = int(m.group(1)) if m else None
		cpu, why = _place(name, layer, n_layer, ngl, overrides, flags)
		if name == "output.weight" and tied:
			why = "tied copy" if not cpu else why
		comp = _classify(name, layer, kinds.get(layer), n_main)
		roster.append({"name": name, "dims": dims, "type": ty, "mib": nbytes / MIB, "dev": "CPU" if cpu else "GPU", "why": why, "layer": layer, "comp": comp,
			"kind": kinds.get(layer) if layer is not None else None, "tied": name == "output.weight" and tied})
		d = groups.setdefault(comp, {"gpu": 0.0, "cpu": 0.0, "n": 0})
		d["cpu" if cpu else "gpu"] += nbytes / MIB
		d["n"] += 1
		if layer is not None and layer in layers:
			layers[layer]["mib"] += nbytes / MIB
			layers[layer]["gpu"] += 0.0 if cpu else nbytes / MIB
		top.append((nbytes / MIB, name, ty, "CPU" if cpu else "GPU", why))
	top.sort(reverse=True)

	ctx = _int(setting(flags, defaults, "ctx-size")[0], 0) or _int(g("context_length"))  # --help: 0 = loaded from the model
	ctx += -ctx % 256
	parallel = _int(setting(flags, defaults, "parallel")[0], -1)
	if parallel < 1:
		slots_reported = _int(n_slots if n_slots is not None else flags.get("n_slots"), 0)
		if slots_reported > 0:
			parallel = slots_reported
		else:
			parallel = 4
			assumed.append("--parallel is auto: 4 slots are assumed")
	ubatch = _int(setting(flags, defaults, "ubatch-size")[0], 0)
	if ubatch < 1:
		ubatch = 512
		assumed.append("--ubatch-size is unknown: 512 is assumed")
	ck, cv = setting(flags, defaults, "cache-type-k")[0], setting(flags, defaults, "cache-type-v")[0]
	if str(ck) not in BPE or str(cv) not in BPE:
		assumed.append("KV cache type %s/%s is unknown: f16 is assumed" % (ck, cv))
		ck, cv = (ck if str(ck) in BPE else "f16"), (cv if str(cv) in BPE else "f16")
	bk, bv = BPE[str(ck)], BPE[str(cv)]
	swa_cells = min(ctx, n_swa * parallel + ubatch + (-(n_swa * parallel + ubatch) % 256)) if n_swa and not flags.get("swa-full") else ctx
	kv = {"full": 0.0, "swa": 0.0, "mtp": 0.0, "gpu": 0.0, "cpu": 0.0, "per_token": 0.0, "layers": {"full": 0, "swa": 0, "mtp": 0}}
	for i, (kd, vd) in kv_dims.items():
		kind = kinds[i]
		cells = swa_cells if kind == "swa" else ctx
		size = cells * (kd * bk + vd * bv) / MIB
		kv[kind if kind in ("full", "swa", "mtp") else "full"] += size
		kv["layers"][kind if kind in ("full", "swa", "mtp") else "full"] += 1
		kv["cpu" if (layers[i]["gpu"] == 0 and layers[i]["mib"] > 0) or flags.get("no-kv-offload") else "gpu"] += size
		if kind == "full":
			kv["per_token"] += kd * bk + vd * bv
	d_conv, d_state, d_inner, n_group = _int(g("ssm.conv_kernel")), _int(g("ssm.state_size")), _int(g("ssm.inner_size")), _int(g("ssm.group_count"))
	n_linear = sum(1 for k in kinds.values() if k == "linear")
	rec_layer = ((d_conv - 1) * (d_inner + 2 * n_group * d_state) + d_state * d_inner) * 4 / MIB if n_linear else 0.0
	rec = {"layers": n_linear, "per_layer": rec_layer, "total": rec_layer * n_linear * parallel}
	weights = {k: round(sum(v[k] for n, v in groups.items() if n != "MTP head"), 1) for k in ("gpu", "cpu")}
	draft = None
	dp = draft_path(flags)
	if dp and local_path(dp) and os.path.exists(dp):
		try:
			dmeta, dten = read_gguf(dp)
			draft = {"path": dp, "mib": sum(t[3] for t in dten) / MIB, "arch": dmeta.get("general.architecture", "")}
		except (OSError, ValueError, struct.error, IndexError):
			pass
	checks = []
	if fit:
		est_ctx = kv["gpu"] + kv["cpu"] - kv["mtp"] + rec["total"]
		if "gpu" in fit and "host" in fit:
			for label, est, ref in (("weights on the GPU", weights["gpu"], fit["gpu"]["model"]), ("weights on the host", weights["cpu"], fit["host"]["model"]), ("KV cache and state", est_ctx, fit["gpu"]["context"] + fit["host"]["context"])):
				if abs(est - ref) > max(64.0, 0.03 * ref):
					checks.append("%s: derived %.0f MiB but llama.cpp projects %d MiB: this architecture or flag is not fully modelled" % (label, est, ref))
		elif "host" in fit:
			for label, est, ref in (("weights on the host", weights["cpu"], fit["host"]["model"]), ("KV cache and state", est_ctx, fit["host"]["context"])):
				if abs(est - ref) > max(64.0, 0.03 * ref):
					checks.append("%s: derived %.0f MiB but llama.cpp projects %d MiB: this architecture or flag is not fully modelled" % (label, est, ref))
	return {
		"checks": checks, "roster": roster, "rules": ["%s=%s" % (rx.pattern, buft) for rx, buft in overrides],
		"model": os.path.basename(flags["model"]), "arch": arch, "name": meta.get("general.name", ""), "n_layer": n_layer, "n_main": n_main, "n_mtp": n_mtp,
		"n_expert": _int(g("expert_count")), "n_expert_used": _int(g("expert_used_count")), "ctx": ctx, "parallel": parallel,
		"cache_k": str(ck), "cache_v": str(cv),
		"groups": groups, "layers": layers, "top": top[:12], "weights": weights, "mtp_mib": groups.get("MTP head", {}).get("gpu", 0.0) + groups.get("MTP head", {}).get("cpu", 0.0),
		"kv": kv, "rec": rec, "ckpt_max": _int(setting(flags, defaults, "swa-checkpoints", "ctx-checkpoints")[0], 0),
		"cache_ram": _int(setting(flags, defaults, "cache-ram")[0], 0), "draft": draft, "fit": fit, "assumed": assumed, "verified": bool(fit) and not checks,
		"est_gpu": weights["gpu"] + groups.get("MTP head", {}).get("gpu", 0.0) + kv["gpu"] + rec["total"] + (draft["mib"] if draft else 0.0) + (fit["gpu"]["compute"] if (fit and "gpu" in fit) else 0.0),
	}


def memory_rows(m, measured=None, overhead=None):
	"""[(label, MiB, note)] of everything resident on the GPU. `measured` adds the part the OS reports that nothing else explains;
	`overhead` is a calibrated stand-in for that part when the model is not loaded."""
	rows = []
	for name, d in sorted(m["groups"].items(), key=lambda kv: -kv[1]["gpu"]):
		if name != "MTP head" and d["gpu"] >= 0.5:
			rows.append(("weights: " + name, d["gpu"], ""))
	kv = m["kv"]
	if kv["layers"]["full"] or kv["layers"]["swa"]:
		parts = []
		if kv["layers"]["full"]:
			parts.append("%d layers × %s tokens" % (kv["layers"]["full"], format(m["ctx"], ",")))
		if kv["layers"]["swa"]:
			parts.append("%d sliding-window layers" % kv["layers"]["swa"])
		rows.append(("KV cache (%s/%s)" % (m["cache_k"], m["cache_v"]), kv["gpu"] - kv["mtp"], " + ".join(parts) + " · %.0f KiB/token" % (kv["per_token"] / 1024)))
	if m["rec"]["total"]:
		rows.append(("recurrent state", m["rec"]["total"], "%d layers × %.2f MiB" % (m["rec"]["layers"], m["rec"]["per_layer"])))
	if m["mtp_mib"]:
		rows.append(("MTP head weights", m["mtp_mib"], "%d layer, built into the model file" % m["n_mtp"]))
	if kv["mtp"]:
		rows.append(("MTP head KV (est.)", kv["mtp"], "estimate"))
	if m["draft"]:
		rows.append(("draft model weights", m["draft"]["mib"], os.path.basename(m["draft"]["path"])))
	if m["fit"] and "gpu" in m["fit"]:
		rows.append(("compute buffers", m["fit"]["gpu"]["compute"], "projected by llama.cpp"))
	if measured is not None:
		gap = measured - sum(r[1] for r in rows)
		rows.append(("unattributed" if gap >= 0 else "projection above resident", gap, "resident VRAM minus the rows above" if gap >= 0 else "the rows above exceed resident VRAM"))
	elif overhead:
		rows.append(("runtime overhead (learned)", overhead, "resident VRAM minus the projection, on an earlier load"))
	return rows

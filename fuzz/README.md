# Atheris Fuzz Targets

Fuzzing harnesses for llmtop parsers based on [Google Atheris](https://github.com/google/atheris) (libFuzzer for Python).

Atheris requires Linux (with libFuzzer support). On Linux or inside WSL:

Run from the project root. libFuzzer writes every new input it finds into the corpus folder it is given, so fuzz a scratch copy and keep `fuzz/corpus/` to hand-made seeds:

```bash
pip install --require-hashes --no-deps --only-binary :all: -r .github/requirements/fuzz.txt
for t in gguf log fit_output args; do
	mkdir -p /tmp/corpus-$t && cp fuzz/corpus/$t/* /tmp/corpus-$t/
	python fuzz/fuzz_$t.py -max_total_time=60 -timeout=5 /tmp/corpus-$t
done
```

A crash leaves a `crash-<hash>` file in the current folder. Copy it into `fuzz/corpus/<target>/` under a `regress_` name once fixed, so the Windows test below keeps it from coming back (`regress_string_array_length.gguf` is one).

On Windows, deterministic regressions run via `python -I tests/test_fuzz_corpus.py` without requiring Atheris.

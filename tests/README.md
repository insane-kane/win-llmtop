# Writing tests

How to add a test case without reading a whole test file.

## Running

- `python -I tests/run_all.py` runs every `tests/test_*.py` as its own process and prints a summary; `--quiet` buffers each suite's output and prints it only if the suite failed.
- Every `tests/test_*.py` also runs on its own (`python -I tests/test_pages.py`) and prints `ALL PASS` at the end when every case passes.

## The two styles

Each suite ends with a `CASES` list of `(label, case)` pairs and a `main()` that runs them in order, printing `PASS <label>` or `FAIL <label>: <type>: <message>` per case.

- **Plain functions** (tests/test_pages.py, tests/test_public.py): write a `test_*` function that raises `AssertionError` on failure, and register it in `CASES` as `("label", test_name)`. Examples: `test_summary_short_requests_fall_back` in tests/test_pages.py and `contact_shields_only_the_address` in tests/test_public.py.
- **`unittest.TestCase` methods** (tests/test_state.py, tests/test_cli.py): add a `test_*` method to a `TestCase` class and register it in `CASES` as `("label", TestX("test_name"))`. Example: `TestState("test_router_mode_preset_loaded_and_flags_parsed")` in tests/test_state.py.

## Helpers worth knowing (tests/test_state.py)

- `_StubServer`: a fake llama-server — an ephemeral HTTP server on 127.0.0.1 serving `/models` and `/slots` with JSON you supply, with optional API-key and status-code behaviour, and a log of the requests it saw.
- `_patch_win`: patches every Windows call the state collector makes (process lists, per-process info, DXGI adapters, performance counters, system memory) with stubs you supply. With its default `guard=True`, `subprocess.run` is replaced, and leaving the block fails the test if it was called — so a test that tries to start a program cannot pass.

tests/test_cli.py runs `llmtop.py` itself in a subprocess, always as `sys.executable -I llmtop.py …`, so the tool runs under the same interpreter with isolated site-packages.

## Rules

- Tests must not reach a real server, must not start llama-server or llama-fit-params, and must not depend on what is on `PATH`.
- Keep suites self-contained: build the input data in the test and stub or patch everything the code under test touches.

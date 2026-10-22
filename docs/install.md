# Installing win-llmtop on Windows

**Contents:** [pipx (recommended)](#pipx-recommended) · [uv](#uv) · [pip](#pip-virtual-environment-or-user-site) · [Specific version](#install-a-specific-version) · [Offline install](#offline-or-air-gapped-install) · [Verify downloads](#verify-what-you-downloaded) · [From source](#from-source) · [64-bit Python](#getting-64-bit-python) · [Recommended terminal](#recommended-terminal)

The examples below use `py`, the standard Windows Python Launcher (`py.exe`) included with official [python.org](https://www.python.org/downloads/windows/) installers, which selects your installed 64-bit Python without needing `python.exe` on `PATH`.

After installing with any method, run `win-llmtop --check` (see [`usage.md`](usage.md#first-run)) to confirm that Windows performance counters, GPU telemetry and your local `llama-server` are reachable.

---

## pipx (recommended)

`pipx` installs `win-llmtop` into an isolated virtual environment and exposes the `win-llmtop` command globally on your `PATH`, so it never conflicts with other Python packages on the machine.

Install `pipx` once if you do not have it yet:

```shell
py -m pip install --user pipx
py -m pipx ensurepath
```

Open a new terminal window so the updated `PATH` takes effect, then install `win-llmtop`:

```shell
pipx install win-llmtop
```

Upgrade or uninstall later:

```shell
pipx upgrade win-llmtop
pipx uninstall win-llmtop
```

## uv

If you manage Python tools with `uv`:

```shell
uv tool install win-llmtop
```

Upgrade or uninstall:

```shell
uv tool upgrade win-llmtop
uv tool uninstall win-llmtop
```

## pip (virtual environment or user site)

### Inside a virtual environment

```shell
py -m venv .venv
.venv\Scripts\activate
py -m pip install win-llmtop
```

Upgrade or uninstall inside the virtual environment:

```shell
py -m pip install --upgrade win-llmtop
py -m pip uninstall win-llmtop
```

### Into the user site (`--user`)

```shell
py -m pip install --user win-llmtop
```

> [!NOTE]
> **`PATH` caveat for `--user` installs:** Windows does not add the per-user scripts folder (`%APPDATA%\Python\Python3XX\Scripts`) to `PATH` by default. If `win-llmtop` is not found after `py -m pip install --user win-llmtop`, either add that `Scripts` folder to your user `PATH`, run `py -m llmtop`, or use [`pipx`](#pipx-recommended) instead.

## Install a specific version

Pin an exact release version with `==`:

```shell
pipx install win-llmtop==0.1.0
```

The same syntax works with `uv tool install win-llmtop==0.1.0` and `py -m pip install win-llmtop==0.1.0`.

## Offline or air-gapped install

`win-llmtop` has zero third-party dependencies, so a single `.whl` file is self-contained:

1. On a machine with internet access, download the `.whl` file from the [GitHub Releases page](https://github.com/insane-kane/win-llmtop/releases) or [PyPI](https://pypi.org/project/win-llmtop/#files).
2. Copy the wheel to the offline Windows machine and install it directly:

```shell
py -m pip install <file>
```

You can also pass the wheel path to `pipx install <file>`.

## Verify what you downloaded

- **PyPI packages (available now):** Every wheel and source archive published to [PyPI](https://pypi.org/project/win-llmtop/) carries [PEP 740](https://peps.python.org/pep-0740/) digital attestations generated and signed via Sigstore OIDC by [`pypa/gh-action-pypi-publish`](https://github.com/pypa/gh-action-pypi-publish).
- **GitHub release files (from 0.1.1):** Starting in `0.1.1`, release artifacts attached to [GitHub Releases](https://github.com/insane-kane/win-llmtop/releases) carry GitHub build-provenance attestations. Verify any downloaded `.whl` or `.tar.gz` with the GitHub CLI:

```shell
gh attestation verify <file> --repo insane-kane/win-llmtop
```

## From source

`win-llmtop` uses the Python standard library only and requires no build dependencies beyond Python itself.

Install directly from the repository:

```shell
py -m pip install git+https://github.com/insane-kane/win-llmtop
```

Or clone the repository and run `llmtop.py` in place:

```shell
git clone https://github.com/insane-kane/win-llmtop.git
cd win-llmtop
py llmtop.py
```

## Getting 64-bit Python

`win-llmtop` requires **64-bit Python 3.11 or later** on 64-bit Windows (a 32-bit Python process cannot inspect a 64-bit `llama-server` address space).

Install 64-bit Python from the official [python.org Windows downloads page](https://www.python.org/downloads/windows/) (choose **Windows installer (64-bit)**), or install it with `winget`:

```shell
winget install Python.Python.3.13
```

Check that your active Python is 64-bit (prints `64`):

```shell
py -c "import struct; print(struct.calcsize('P')*8)"
```

## Recommended terminal

**Windows Terminal** (`wt.exe`) is strongly recommended: it draws the finest charts.

`win-llmtop` picks a chart style from the terminal it runs in:

- **Windows Terminal** (`WT_SESSION` set): `braille` — two time samples per character column, four vertical dot rows per line.
- **Other modern terminals** (`TERM_PROGRAM` set, or `ConEmuANSI=ON`): `blocks` (`▁▂▃▄▅▆▇█`), eight steps per line.
- **Classic console** (`conhost.exe`, no other signal): `coarse` (`▄`/`█`), two steps per line. The classic console's raster font has no one-eighth steps, so `blocks` would draw boxes there.

You can cycle the style live with the `s` key (it steps through `braille`, `blocks`, `coarse` and `ascii`), force one at startup with [`--graph-style`](llmtop.1.md#options), or disable ANSI colour codes with [`--no-color`](llmtop.1.md#options). If charts show boxes in the default style, press `s` or start with `--graph-style coarse` — the classic console needs the `coarse` style.

See [`usage.md`](usage.md) for your first run and [`llmtop.1.md`](llmtop.1.md#options) for all command-line options.

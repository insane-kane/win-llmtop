# Security policy

## Supported versions

Only the latest release is supported.

| Version | Supported |
| :--- | :--- |
| Latest release | Yes |
| All earlier releases | No |

## Reporting a vulnerability

Please do not report security issues through public GitHub issues or Discussions.

- Use GitHub's private vulnerability reporting: <https://github.com/insane-kane/win-llmtop/security/advisories/new>
- Or email `software@kanenet.us`.

## What to include

- A description of the issue and, where possible, steps to reproduce it.
- The win-llmtop version and the Windows version.

## Response times

The maintainer acknowledges a report within 7 days.

## Scope

llmtop is read-only. It reads llama-server's HTTP API (optionally with an API key from `--api-key` or `LLMTOP_API_KEY`/`LLAMA_API_KEY`), its log files and Windows performance counters, and writes only to its state folder.

It runs `llama-server --help` and `llama-fit-params` only from the folder of the running `llama-server` it monitors: the process named `llama-server*` that listens on the watched port, when the server is at a loopback address. It opens the model, draft-model and log-file paths a server reports only for such a server, and only when the path is absolute on a local drive letter (`X:\...`); network, device and relative paths are skipped with a notice. It follows no HTTP redirect, and it sends the API key only to the URL given with `--router` or taken from a running `llama-server`'s command line, never to the default-address probe.

In scope:

- anything that makes llmtop write outside its state folder;
- anything that leaks the API key (to disk, the screen or a log);
- anything that runs code from what llmtop reads.

Out of scope:

- vulnerabilities in llama.cpp itself — report those to <https://github.com/ggml-org/llama.cpp/issues>.

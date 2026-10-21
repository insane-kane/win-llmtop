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

In scope:

- anything that makes llmtop write outside its state folder;
- anything that leaks the API key (to disk, the screen or a log);
- anything that runs code from what llmtop reads.

Out of scope:

- vulnerabilities in llama.cpp itself — report those to <https://github.com/ggml-org/llama.cpp/issues>.

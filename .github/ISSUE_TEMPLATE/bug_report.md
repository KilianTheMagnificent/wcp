---
name: Bug report
about: Report an unexpected CLI result, protocol error, or crash in wcp
title: "[bug]: "
labels: bug
assignees: ''
---

> **Security issues:** If you are reporting a suspected security vulnerability or isolation bypass, please use GitHub's [Private Vulnerability Reporting](https://github.com/KilianTheMagnificent/wcp/security/advisories/new) instead of opening a public issue (see [SECURITY.md](../../SECURITY.md)).

## Summary

A clear and concise description of what went wrong.

## Environment

- **OS:** (e.g., macOS 15.x, Ubuntu 24.04)
- **Python version (`python3 --version`):**
- **Git version (`git --version`):**
- **wcp version (`wcp version`):**
- **Install method:** (e.g., `pipx`, `uvx`, `./bin/wcp`, `python3 -m wcp`)

## Steps to Reproduce

Please provide the exact command sequence in a small, synthetic test repository. **Never include real credentials, private repository names, or personal paths.**

```sh
# 1. ...
# 2. ...
```

## Expected Behavior

What you expected `wcp` to do (including expected exit code or JSON output, if relevant).

## Actual Behavior

What happened instead, including stderr/stdout output and exit code.

```text
# paste sanitized output here
```

## Additional Context

Any other context (for example, `wcp status`, `wcp task show <id>`, or `wcp events <id>` output with invented names).

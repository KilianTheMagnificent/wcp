# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.3.x   | Yes       |

## Reporting a Vulnerability

If you discover a vulnerability in `wcp` — such as an unintended write to the canonical checkout outside verified publish, a path-traversal or symlink escape, a bypass of credential-shaped path exclusion, or an integrity flaw in bundle hashing or publish recovery — please report it privately through GitHub's [Private Vulnerability Reporting](https://github.com/KilianTheMagnificent/wcp/security/advisories/new) instead of opening a public issue.

Note that `wcp hook pretooluse` is documented as a guard rail rather than a hard security boundary (for example, shell commands and CLI flags that disable hooks are not blocked by the hook; OS sandboxing is the mechanical backstop).

Please include your OS, Python and Git versions, a minimal synthetic reproduction, and the observed vs. expected behavior. Never include real credentials or private data in a report.

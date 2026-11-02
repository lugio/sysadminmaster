# Security policy

SysAdminMaster runs commands on production Windows servers as `SYSTEM`. We take reports seriously and are
grateful for responsible disclosure.

## Supported versions

| Version | Supported |
|---|---|
| 2.32.x (latest) | ✅ |
| older | ❌ — please upgrade |

## Reporting a vulnerability

**Please do not open a public issue, discussion or pull request for a security problem.**

Use GitHub's private reporting instead: **Security → Report a vulnerability** on this repository
([private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)).

Please include:

- the affected component (`AgentMaster`, dashboard, agent script, installer…) and version;
- a description of the issue and its impact;
- steps to reproduce, or a proof of concept that does **not** touch systems you do not own.

What to expect:

- an acknowledgement within **5 working days**;
- an assessment and, if confirmed, a fix plan within **30 days**;
- credit in the release notes if you wish.

## Scope

In scope: authentication and token handling, command authorisation (what can be queued and executed), path
traversal on served scripts, injection in generated PowerShell, cross-site scripting in the dashboard, secrets
written to logs.

Out of scope: denial of service by flooding a server you operate yourself, issues that require an attacker to
already hold the master token or administrative access to a session host, and missing hardening of a
deployment that ignores [docs/security-model.md](docs/security-model.md).

## Hardening checklist for operators

- Keep the three tokens (`SAM_AGENT_TOKEN`, `SAM_MASTER_TOKEN`, `SAM_READ_TOKEN`) long, random and **different**.
- Expose only the dashboard, behind a TLS reverse proxy; never expose port 5000 beyond the session hosts.
- Restrict port 5000 at the firewall to the session hosts (and to read-only consumers, if any).
- Store `.env` with mode `600`; never commit it (CI fails if a runtime secret file is tracked).
- Rotate the agent token with the built-in two-token window (`SAM_AGENT_TOKEN_LEGACY`), see
  [docs/agents.md](docs/agents.md#rotating-the-agent-token).

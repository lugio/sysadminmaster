# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). The agent has its own version number (`$script:AgentVersion`),
bumped only when the agent script changes; release notes say when an agent update is needed.

## [Unreleased]

## [2.32.0] — 2026-11-02

First public release. The project had been developed and run in production on a 10-host farm since
mid-2026; its earlier history is not published.

### Added
- **Fleet view**: state of every host (online, busy, silent), agent version, CPU, RAM, committed memory,
  disks, services, processes; remote agent updates in waves.
- **Sessions**: active and disconnected sessions, per-user consumption, targeted message, disconnect,
  log off, shadow and screenshot.
- **Profiles**: size per profile, folder analysis, clean-up of old downloads with a dry run first.
- **Microsoft 365 licences**: Office sign-in token age per user and host, shared-computer activation state,
  scope by directory OU decided on the server, two named repairs and a read-only diagnosis.
- **Alerts**: daily e-mail of stale Office tokens (Mailjet), with the history of every check and its
  delivery.
- **Maintenance tools**: temporary files, RAM, print spooler, DNS cache, RDS diagnosis, security audit.
- **Read-only token** (`SAM_READ_TOKEN`, `X-Read-Token`) for monitoring tools, refused on every command
  route ([ADR 0005](docs/architecture/0005-read-only-token.md)).
- Example systemd units in `deploy/systemd/`.
- Documentation in `docs/`, architecture decision records, contribution guide, code of conduct, security
  policy, issue forms and CI.

### Changed
- Every site-specific value (broker host name, dashboard URL, alert sender, Office 365 scope, ignored
  accounts) is read from the environment or `Config/config.json`; see
  [docs/configuration.md](docs/configuration.md).
- Without `SAM_SECRET_KEY`, the dashboard uses a random session key instead of a fixed default.

### Removed
- Legacy Windows-only launchers and modules from the first Windows version of the server.

### Agent
- Agent **2.32.0** (update recommended). New optional parameter `-IgnoredAccounts` (or machine variable `SAM_IGNORED_ACCOUNTS`).

[Unreleased]: https://github.com/lugio/sysadminmaster/compare/v2.32.0...HEAD
[2.32.0]: https://github.com/lugio/sysadminmaster/releases/tag/v2.32.0

# Contributing to SysAdminMaster

Thank you for taking the time to contribute! Bug reports, documentation, translations and code are all welcome.
Issues and pull requests may be written in **English or French**.

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to contribute

- **Report a bug** — use the [bug report form](https://github.com/lugio/sysadminmaster/issues/new/choose). Include
  versions (dashboard, agent), what you expected and what happened, and the relevant log lines **with tokens,
  host names and user names removed**.
- **Suggest a feature** — open a feature request or start a discussion first for anything large, so we can agree
  on the approach before you write code.
- **Improve the docs** — typos, unclear steps and missing explanations are real bugs.
- **Translate the interface** — see the roadmap in the [README](README.md#roadmap).
- **Security issues** — never in public: see [SECURITY.md](SECURITY.md).

## Development setup

```bash
git clone https://github.com/<you>/sysadminmaster.git
cd sysadminmaster
python3 -m venv .venv-linux
./.venv-linux/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env      # development values only
ln -sf ../../tools/hooks/pre-push .git/hooks/pre-push
./start-linux.sh                            # AgentMaster :5000 + dashboard :5050
```

Without a Windows host you can still work on the server and the dashboard: the AgentMaster accepts heartbeats
from any client that sends a valid `X-Agent-Token`. See [docs/testing.md](docs/testing.md) for how to post a
sample heartbeat.

PowerShell work targets **Windows PowerShell 5.1** (the version shipped with Windows Server); install
[PowerShell 7](https://learn.microsoft.com/powershell/scripting/install/installing-powershell) locally if you
want to run the parse check.

## Before you open a pull request

```bash
.venv-linux/bin/python tests/test_read_token.py   # read-only token contract
python3 tools/check-version.py                       # every version declaration == VERSION
python3 tools/check-endpoints.py                     # every UI call targets a live route
python3 tools/check-template-js.py                   # inline JavaScript in templates compiles
python3 tools/check-ps-encoding.py Scripts/*.ps1     # no typographic characters in PowerShell code
```

CI runs the same checks plus a PowerShell parse of every script. The checklist in the pull request template
lists the rest.

## Guidelines

### General
- **One change per pull request**, with a clear description of the *why*.
- **Nothing site-specific in the code**: host names, domains, OUs, URLs, e-mail addresses and accounts belong in
  `.env` or `Config/config.json` (see [docs/configuration.md](docs/configuration.md)). Examples use `CONTOSO`,
  `RDS-BROKER`, `RDSH01`… and `example.com`.
- **No secret in the repository**, not even in a comment or an example. Services must refuse to start without
  their secrets (fail closed) rather than fall back to a default.
- New code comments and docs are written in English; existing French comments may stay.

### Python (AgentMaster and dashboard)
- Read every setting through `Web/core/settings.py` or `os.environ`, with `Config/config.json` as fallback.
- Keep the stack small: Flask, Flask-SocketIO, SQLite. Discuss a new dependency in an issue first.
- Every new route checks its token. Read-only routes use `_check_read_token`; anything that queues a command or
  writes uses `_check_master_token`.

### PowerShell (agent)
- Windows PowerShell **5.1** syntax only (no PowerShell 7-only constructs).
- Outbound HTTP goes through `Invoke-DirectHttp` (explicit `.Proxy = $null`): `Invoke-RestMethod` behind a
  WinINET/WPAD proxy can return a false success. See [ADR 0003](docs/architecture/0003-http-direct-proxy-null.md).
- ASCII in code (strings and identifiers); CI rejects typographic quotes and dashes.
- A new agent action must be added **both** to the agent and to the dashboard allow-list, with a read-only
  preview when it changes anything on the host.
- Bump `$script:AgentVersion` only when the agent itself changes.

### Dashboard
- Look at `/styleguide` first and reuse its classes and the Jinja macros in `Web/templates/_macros.html`.
- Tables offer sorting and filtering (`Web/static/table-utils.js`).
- An empty value must never look like "all good": say when data is missing.

### End-to-end tests
The scripts in `e2e/` drive a real browser against a real dashboard. They install a **network-level read-only
guard** (`e2e/lib/prod-safe.js`) that blocks every mutating request before it leaves the browser. Never weaken
that guard; see [docs/testing.md](docs/testing.md).

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/): `<type>(<scope>): <summary>`, summary ≤ 72
characters, imperative mood.

Types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `chore`, `security`.
Scopes: `agent`, `master`, `dashboard`, `licenses`, `alerts`, `e2e`, `ci`, `docs`.

```
feat(master): add a read-only token for monitoring consumers
fix(agent): bypass the WinINET proxy for outbound heartbeats
```

## Releases

The project follows [Semantic Versioning](https://semver.org/). The single source of truth is the `VERSION`
file; `tools/check-version.py --fix` propagates it. Maintainers update [CHANGELOG.md](CHANGELOG.md), tag
`vX.Y.Z` and publish a GitHub release.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).

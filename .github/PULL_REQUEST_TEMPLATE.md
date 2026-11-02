## What and why

<!-- What does this change, and which problem does it solve? Link the issue: "Fixes #123". -->

## Type

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Refactoring / maintenance

## Checklist

- [ ] The checks in [CONTRIBUTING.md](../CONTRIBUTING.md#before-you-open-a-pull-request) pass locally
- [ ] No secret, host name, domain, OU, URL, e-mail address or IP address from a real site (examples use `CONTOSO`, `RDSH01`, `example.com`, `192.0.2.x`)
- [ ] New settings are read from the environment / `Config/config.json` and documented in `docs/configuration.md`
- [ ] PowerShell code is 5.1-compatible and ASCII-only
- [ ] A new agent action is in the agent **and** the dashboard allow-list, with a read-only preview if it changes the host
- [ ] A new route checks the right token (read-only vs master)
- [ ] `CHANGELOG.md` updated under *Unreleased*
- [ ] Screenshots attached for a UI change

## How was it tested?

<!-- Commands run, pages opened, hosts involved (lab only). -->

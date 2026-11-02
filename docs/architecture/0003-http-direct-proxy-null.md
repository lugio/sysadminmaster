# 0003 — Agent HTTP bypasses the system proxy; Windows PowerShell 5.1 only

- **Status**: Accepted
- **Date**: 2026-06

## Context

An agent logged "heartbeat sent" while the server received nothing. Two independent causes:

1. **WinINET/WPAD proxy under the `SYSTEM` account.** `Invoke-RestMethod` and `Invoke-WebRequest` inherit the
   system proxy settings, which can return a **false success** without ever reaching the server. Setting
   `[System.Net.WebRequest]::DefaultWebProxy = $null` does not cover every code path.
2. **PowerShell 7-only syntax.** `switch -CaseInsensitive` does not exist in Windows PowerShell 5.1: the
   script failed to parse and the agent exited at load time.

## Decision

1. Every outbound HTTP call of the agent goes through `Invoke-DirectHttp`, which builds a
   `[System.Net.HttpWebRequest]` and sets **`.Proxy = $null` on each request**.
2. The agent targets **Windows PowerShell 5.1**, the version shipped with Windows Server. No PowerShell 7
   construct, and ASCII-only code (5.1 reads scripts as CP1252: a typographic dash inside a string becomes a
   closing quote).
3. Scheduled tasks are stopped and started with `schtasks /End` and `/Run`, which behave consistently on old
   hosts, rather than the `*-ScheduledTask` cmdlets.

## Consequences

**Positive**
- Reliable heartbeats regardless of the host's proxy configuration.
- No prerequisite on the hosts beyond what Windows Server ships.

**Negative**
- New agent code must use `Invoke-DirectHttp`; reintroducing a bare `Invoke-RestMethod` brings the bug back.
- CI parses every script and rejects typographic characters, but it runs PowerShell 7: syntax that only
  5.1 rejects must still be caught in review.

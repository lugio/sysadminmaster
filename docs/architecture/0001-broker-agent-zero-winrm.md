# 0001 — Agents push telemetry and pull commands (no WinRM)

- **Status**: Accepted
- **Date**: 2026-06

## Context

We need live telemetry (CPU, RAM, disks, sessions, services, processes, profiles) and day-to-day actions
(log off a user, clean a profile, restart a service) on every host of a Remote Desktop Services farm,
from a Linux server.

The usual approach — WinRM from a central server — needs an inbound port on every host, a domain account
with administrative rights stored on the server, and working Kerberos delegation. In practice remote
execution failed with "Access denied" on most hosts as soon as a group policy did not grant the server's
account local administrator rights, and a single credential gave control of the whole farm.

## Decision

- A PowerShell agent runs as `SYSTEM` on each host (scheduled task) and **initiates every exchange**.
- Each heartbeat is an HTTP `POST` of the telemetry; the **response carries the pending commands** for that
  host; the agent posts each result back.
- The server stores no Windows credential. Agents authenticate with a shared agent token.
- Agent updates are ordinary commands: the agent downloads the current scripts from the server, checks
  that they parse, and reinstalls itself.

## Consequences

**Positive**
- No inbound port and no remote shell on the hosts; no domain credential on the server.
- Works across firewalls that only allow host → server traffic.
- Commands reach only live hosts, and every command and result is recorded.

**Negative**
- Command latency is bounded by the heartbeat period (10 s by default).
- A host whose agent is dead cannot be repaired from the dashboard; the installer therefore registers two
  task triggers (start-up and periodic) so that a stopped agent restarts by itself.
- A command that kills the agent before it reports must not be replayed forever: commands are marked
  `sent` when served and expire after 48 h.

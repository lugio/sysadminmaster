# Architecture

SysAdminMaster has three parts: an **agent** on every Windows session host, the **AgentMaster** that
receives telemetry and queues commands, and the **dashboard** that people use.

```
 Session hosts (Windows, PowerShell 5.1)               Linux server (Python 3.11+)
 ┌──────────────────────────────┐                      ┌──────────────────────────────────┐
 │ SysAdminMaster-Agent.ps1     │  POST heartbeat      │ AgentMaster  :5000               │
 │ scheduled task, SYSTEM       │ ───────────────────▶ │  AgentMaster/master.py           │
 │                              │ ◀─────────────────── │  agentmaster.db (SQLite)         │
 │                              │  pending commands    │                                  │
 │                              │  POST command-result │                                  │
 │                              │ ───────────────────▶ │                                  │
 └──────────────────────────────┘                      │            ▲  X-Master-Token     │
                                                       │            │                     │
                                                       │ Dashboard  :5050                 │
                                                       │  Web/app.py (Flask + Socket.IO)  │
                                                       └────────────┬─────────────────────┘
                                                                    │ HTTPS (reverse proxy)
                                                                    ▼
                                                               Administrators
```

## Pull model, no WinRM

The agent **initiates every connection**. Each heartbeat (every 10 s by default) carries the host's
telemetry; the response carries the commands queued for that host. The agent runs them and posts the
result. Consequences:

- no inbound port on the session hosts, no WinRM, no remote PowerShell;
- the server never stores a domain administrator credential;
- a command reaches a host only when that host is alive and talking.

See [ADR 0001](architecture/0001-broker-agent-zero-winrm.md).

## Components

| Component | Path | Role |
|---|---|---|
| Agent | `Scripts/SysAdminMaster-Agent.ps1` | collects metrics, sessions, services, processes, profiles, Office token state; runs allowed actions |
| Installer | `Scripts/Install-SysAdminMaster-Agent.ps1` | creates the `SysAdminMasterAgent` scheduled task (SYSTEM, at start-up and every few minutes) |
| Bootstrap | `Scripts/Bootstrap-Agent-FromMaster.ps1` | one-shot on a host: downloads the agent and installer from the master and installs them |
| Relay (optional) | `Scripts/Deploy-AgentRelay.ps1` | a transparent relay on a Windows host when agents cannot reach the server directly |
| AgentMaster | `AgentMaster/master.py` | ingestion, command queue, history, licence view, daily alert, script distribution |
| Licence view | `AgentMaster/license_view.py` | one Office token observation per person across hosts |
| Licence alerts | `AgentMaster/license_alerts.py` | daily e-mail of stale Office tokens (Mailjet) |
| Dashboard | `Web/` | pages, JSON API for the pages, authorisation of actions |

## Storage

- `agentmaster.db` (SQLite, written by the AgentMaster): latest heartbeat per host, command queue with
  states (`pending` → `sent` → `done`/`failed`, `expired` after 48 h, `cancelled`), one-minute CPU/RAM
  samples kept 24 h, licence history (on change only), alert history.
- The dashboard keeps no state of its own beyond small caches; it reads the AgentMaster.

## Commands and actions

The dashboard accepts only actions from an allow-list (`Web/routes/actions.py`). It queues them on the
AgentMaster with the master token. The agent also checks that it knows the action. Anything that removes
data offers a dry run first (profile clean-up, Office token repair).

## Office 365 licence tracking

On every host, the agent reads the age of each user's Office sign-in token and the shared-computer
activation (SCA) state. The server, not the agent, decides who is in scope (directory OU, exclusions,
ignored accounts), so changing the scope never requires an agent update. The daily alert lists the people
whose token is older than `SAM_ALERT_STALE_DAYS`.

## Updating agents

`update_agent` is an ordinary queued command: the agent downloads the current scripts from
`/scripts/<name>` (agent token required, fixed allow-list of names), checks that they parse, then
reinstalls itself. Roll out in waves: one pilot host first, the broker host last.

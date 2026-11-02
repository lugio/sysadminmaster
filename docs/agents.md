# Agents

The agent is a single PowerShell 5.1 script, `SysAdminMaster-Agent.ps1`, run as `SYSTEM` by the
`SysAdminMasterAgent` scheduled task. It never listens on the network.

## Install

On the host, elevated:

```powershell
.\Install-SysAdminMaster-Agent.ps1 -BrokerUrl "http://sam-master:5000/api/agent/metrics" -Token "<SAM_AGENT_TOKEN>"
```

The installer copies the agent to `C:\Program Files\SysAdminMaster`, then creates the task with **two
triggers**: at start-up, and a periodic restart-if-stopped. With a start-up trigger alone, an agent stopped
between two reboots never comes back by itself, and session hosts rarely reboot.

`Bootstrap-Agent-FromMaster.ps1` does the same after downloading the current scripts from the master, and
checks that they parse before installing them.

Uninstall: `.\Install-SysAdminMaster-Agent.ps1 -Uninstall`.

## What it reports

Every heartbeat: CPU, RAM and committed memory, disks, sessions (active and disconnected) with per-user
consumption, services, top processes, profile sizes (cached between runs, refreshed by a nightly scan), and
the Office sign-in token state of each user.

## Actions

Actions are queued by the dashboard and executed on the next heartbeat.

| Group | Actions |
|---|---|
| Sessions | `message`, `disconnect`, `logoff`, `shadow`, `screenshot` |
| Profiles | `get_profiles`, `get_profile_size`, `analyze_profile`, `clean_profile` (dry run first) |
| Office | `diagnose_office_auth` (read-only), `repair_office_token`, `repair_office_broker` (both reversible) |
| Maintenance | `clear_temp`, `optimize_ram`, `restart_spooler`, `flush_dns`, `global_maintenance`, `diagnose_rds`, `audit_security` |
| Agent | `restart_agent`, `update_agent` |

A `message`, `logoff` or `disconnect` without an explicit target is refused: an empty target never means
"everyone".

## Updating the fleet

1. Upgrade the server (see [installation.md](installation.md#upgrading-the-server)).
2. Check that no command is pending for the hosts (Agents page).
3. Update **one pilot host**, wait for its heartbeat with the new version.
4. Update the remaining session hosts.
5. Update the **broker host last** — it orchestrates the others.

## Rotating the agent token

The AgentMaster accepts two agent tokens during a rotation window.

1. Set the new value in `SAM_AGENT_TOKEN` and the old one in `SAM_AGENT_TOKEN_LEGACY`; restart
   `sam-agentmaster`. Both tokens are now accepted.
2. Run `update_agent` on every host with the new token (pilot first, broker last).
3. When every host heartbeats with the new token, remove `SAM_AGENT_TOKEN_LEGACY` and restart. The old token
   is now refused.

## Troubleshooting

| Symptom | Check |
|---|---|
| Host never appears | `Test-NetConnection sam-master -Port 5000` from the host; token value; `C:\Program Files\SysAdminMaster` log files |
| "Success" but nothing arrives | a WinINET/WPAD proxy: the agent bypasses it with `Invoke-DirectHttp`, keep `.Proxy = $null` ([ADR 0003](architecture/0003-http-direct-proxy-null.md)) |
| Host shown as busy | it is alive but its last heartbeat is late (long action, heavy load) — not dead |
| Script does not parse on the host | typographic characters in PowerShell code; run `tools/check-ps-encoding.py` |
| Agent killed after every boot | a stale command in the queue; commands expire after 48 h and can be cancelled from the Agents page |

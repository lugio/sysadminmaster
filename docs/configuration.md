# Configuration

Settings are read from the **environment** first (the systemd units load `.env`), then from
`Config/config.json` for the few values that have a JSON equivalent. Nothing site-specific is hard-coded.
Copy [`.env.example`](../.env.example) and [`Config/config.example.json`](../Config/config.example.json) to start.

## Secrets

| Variable | Required | Used by | Meaning |
|---|---|---|---|
| `SAM_AGENT_TOKEN` | **yes** | AgentMaster | token the agents send in `X-Agent-Token` |
| `SAM_AGENT_TOKEN_LEGACY` | no | AgentMaster | previous agent token, accepted **only** during a rotation window — remove it afterwards |
| `SAM_MASTER_TOKEN` | **yes** | AgentMaster, dashboard | token of the dashboard (`X-Master-Token`); it can queue commands |
| `SAM_READ_TOKEN` | no | AgentMaster | read-only token (`X-Read-Token`) for monitoring tools; must differ from the master token; absent = no read-only access |
| `SAM_ADMIN_PASSWORD` | **yes** | dashboard | shared administrator password; without it nobody can sign in |
| `SAM_SECRET_KEY` | recommended | dashboard | session signing key; without it a random key is used and sessions end at each restart |
| `MAILJET_API_KEY`, `MAILJET_SECRET_KEY` | for alerts | AgentMaster | Mailjet credentials for the daily licence alert |

## Site

| Variable | Default | Meaning |
|---|---|---|
| `SAM_BROKER_HOST` | `RDSServers.ConnectionBroker` of config.json, else `RDS-BROKER` | short name of the broker host (it holds the profile disk and orchestrates the others) |
| `SAM_DASHBOARD_URL` | empty | public URL of the dashboard, used for links in e-mails |
| `SAM_MASTER_URL` | `http://127.0.0.1:5000` | where the dashboard reaches the AgentMaster |
| `SAM_MASTER_PORT` | `5000` | AgentMaster listening port |
| `SAM_DASHBOARD_PORT` | `5050` | dashboard listening port |

## Office 365 scope

| Variable | Default | Meaning |
|---|---|---|
| `SAM_O365_SCOPE_OU` | empty = no OU filter | distinguished-name suffix of the Office users, e.g. `OU=Staff,DC=contoso,DC=local` |
| `SAM_O365_SCOPE_EXCLUDE` | empty | comma-separated sub-OUs excluded from the scope (archives, leavers) |
| `SAM_O365_IGNORED_ACCOUNTS` | empty | comma-separated technical accounts to ignore, in addition to the built-in generic list |

The agents report each user's distinguished name; the **server** applies the scope. Changing it never
requires an agent update. On the agent side, `-IgnoredAccounts` (or the machine environment variable
`SAM_IGNORED_ACCOUNTS`) skips accounts before they are even reported.

## Daily licence alert

The alert is **off** until both a sender and a recipient are set.

| Variable | Default | Meaning |
|---|---|---|
| `SAM_ALERT_TO` | empty | comma-separated recipients |
| `SAM_ALERT_FROM` | empty | sender address (must be validated in Mailjet) |
| `SAM_ALERT_HOUR` | `7` | local hour of the daily check |
| `SAM_ALERT_STALE_DAYS` | `30` | a token older than this is reported |
| `SAM_ALERT_HORIZON_DAYS` | `45` | observations older than this are ignored |

## Config/config.json

Optional. It holds thresholds and defaults for the dashboard (`SystemResourceManagement`,
`RDSSessionManagement`, `UserProfiles`…) and the list of hosts in `RDSServers`. Tokens may be placed there
for a lab, but the environment always wins and is the recommended place. The file is ignored by git.

## Agent parameters

| Parameter | Default | Meaning |
|---|---|---|
| `-BrokerUrl` | `http://sam-master:5000/api/agent/metrics` | heartbeat URL of the AgentMaster |
| `-Token` | — | the agent token |
| `-IntervalSeconds` | `10` | heartbeat period |
| `-IgnoredAccounts` | empty | site technical accounts to skip |

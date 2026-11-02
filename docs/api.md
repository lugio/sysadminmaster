# API

Two services expose HTTP routes. JSON in, JSON out.

## Tokens

| Header | Variable | Who | Can |
|---|---|---|---|
| `X-Agent-Token` | `SAM_AGENT_TOKEN` | agents | post heartbeats and results, download scripts |
| `X-Master-Token` | `SAM_MASTER_TOKEN` | dashboard | everything on the AgentMaster, including queueing commands |
| `X-Read-Token` | `SAM_READ_TOKEN` | monitoring tools | the read-only `GET` routes marked below — never a command |

## AgentMaster (port 5000)

| Method | Route | Token | Purpose |
|---|---|---|---|
| `POST` | `/api/agent/metrics` | agent | heartbeat; the response lists pending commands |
| `POST` | `/api/agent/command-result` | agent | result of a command |
| `GET` | `/scripts/<name>` | agent | download an allowed script (agent, installer, relay) |
| `GET` | `/api/master/agents` | master or **read** | latest state of every host |
| `GET` | `/api/master/history?hostname=H` | master or **read** | one-minute CPU/RAM samples (24 h) |
| `GET` | `/api/master/licenses` | master or **read** | Office token view |
| `GET` | `/api/master/alerts/status` | master or **read** | state of the daily alert |
| `GET` | `/api/master/alerts/history` | master or **read** | past checks and their delivery |
| `POST` | `/api/master/command` | master | queue a command `{hostname, action, params}` |
| `GET` | `/api/master/command-status/<id>` | master | state and result of a command |
| `POST` | `/api/master/alerts/test` | master | send a test alert |
| `POST` | `/api/master/alerts/refresh-delivery` | master | refresh delivery status from Mailjet |
| `POST` | `/api/master/agent-deploy` | master | WinRM deployment — Windows server only, returns 501 on Linux |
| `GET` | `/api/health` | none | liveness |

Example, read-only:

```bash
curl -s -H "X-Read-Token: $SAM_READ_TOKEN" http://sam-master:5000/api/master/agents
```

## Dashboard (port 5050)

The dashboard routes serve its own pages and require a signed-in session. They are not a stable public API;
use the AgentMaster read-only routes for integrations. The main groups:

- pages: `/dashboard`, `/sessions`, `/profiles`, `/resources`, `/licenses`, `/alerts`, `/agents`, `/tools`,
  `/reports`, `/logs`, `/optimization`, `/styleguide`;
- data: `/api/metrics`, `/api/sessions`, `/api/servers`, `/api/licenses`, `/api/alerts/*`, `/api/reports/*`…;
- actions: `POST /api/actions` (allow-listed actions), `POST /api/tools`, `/api/admin/*`.

`tools/check-endpoints.py` checks that every call made by the pages targets one of these routes.

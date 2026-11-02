# 0005 — A separate read-only token for monitoring consumers

- **Status**: Accepted
- **Date**: 2026-11

## Context

External monitoring tools (wall displays, alerting, a status page) need the farm state: agents, history,
licences, alerts. The only token able to read it was the master token, which can also queue any action on
any host — log users off, run maintenance. Handing it to a monitoring tool turns a read need into full
control of the farm.

## Decision

- A third token, `SAM_READ_TOKEN`, sent in `X-Read-Token`, is accepted on the consultation routes only:
  `GET /api/master/agents`, `/history`, `/licenses`, `/alerts/status`, `/alerts/history`.
- It is refused everywhere else, including when sent in the `X-Master-Token` header.
- The AgentMaster refuses to start if the read-only token equals the master token.
- Without the variable, read-only access is simply disabled.
- A test (`tests/test_read_token.py`) proves both sides: every read route accepts it, every command route
  refuses it.

## Consequences

**Positive**
- A leaked monitoring credential exposes state, never control.
- Consumers can be granted access at the firewall to port 5000 without receiving the master token.

**Negative**
- One more secret to generate, store and rotate.
- Every new `GET` route must choose explicitly between `_check_read_token` and `_check_master_token`.

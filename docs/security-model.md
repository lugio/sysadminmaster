# Security model

SysAdminMaster can log users off and run maintenance as `SYSTEM` on production servers. This page states
what is trusted, what is not, and what an operator must do.

## Trust boundaries

```
administrators ──HTTPS──▶ reverse proxy ──▶ dashboard ──X-Master-Token──▶ AgentMaster ◀──X-Agent-Token── agents
                                                                             ▲
                                              monitoring tools ──X-Read-Token┘
```

| Asset | Protection |
|---|---|
| dashboard | TLS at the reverse proxy; administrator password (`SAM_ADMIN_PASSWORD`), fail closed; signed session |
| AgentMaster | not published; firewall to the session hosts; one token per kind of caller |
| agent token | lets a holder post fake telemetry and read the agent scripts — **not** queue commands |
| master token | lets a holder run any allowed action on any host — treat it like an administrator password |
| read-only token | reads state; refused on every route that queues or changes anything |

## Design rules

- **Fail closed.** Services refuse to start without their tokens; there is no default token in the code.
  The read-only token is refused if it equals the master token.
- **Pull only.** Agents open every connection; the hosts expose no port and the server holds no domain
  credential.
- **Allow-lists.** Actions are allow-listed in the dashboard *and* known to the agent. Downloadable scripts
  are a fixed list of names, with path traversal rejected.
- **No implicit "all".** A session action without a target is refused.
- **Dry run first.** Destructive actions (profile clean-up) preview what they would remove; Office repairs
  rename rather than delete, so they can be undone.
- **Constant-time comparison** of tokens (`hmac.compare_digest`).

## Known limits

- The dashboard has one shared administrator password; put an identity-aware proxy (SSO) in front of it
  if several people use it.
- Tokens are static secrets; rotate them (agent token: see [agents.md](agents.md#rotating-the-agent-token)).
- Traffic between agents and the AgentMaster is plain HTTP on the internal network by default. Use a
  dedicated VLAN or a TLS relay if that network is not trusted.

Report vulnerabilities privately: [SECURITY.md](../SECURITY.md).

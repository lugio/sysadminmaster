# 0002 — Agents reach the server directly, or through an optional relay

- **Status**: Accepted
- **Date**: 2026-06

## Context

Agents must reach the AgentMaster on TCP 5000. Two situations exist in the field:

1. the session hosts can reach the server directly (same VLAN, or a firewall rule host → server);
2. they cannot yet, but they can reach a Windows host of the farm (typically the broker), and changing
   every agent's target at once is risky.

Pointing agents at a **host name** rather than a literal IP also matters on Windows: a literal IP is more
exposed to the system-proxy problem described in [ADR 0003](0003-http-direct-proxy-null.md).

## Decision

- **Direct path (preferred).** Agents post to `http://<server>:5000/api/agent/metrics`, by name.
- **Relay (optional, transitional).** `Scripts/Deploy-AgentRelay.ps1` installs a small transparent relay on
  a Windows host: it listens where the agents already point and forwards each request verbatim, response
  included, to the server. It stores nothing and holds no token of its own.
- Agents move from the relay to the direct path with `update_agent` and a new `-BrokerUrl`, in waves.

## Consequences

**Positive**
- A farm can be connected without touching every host on day one.
- The relay is stateless: removing it only requires the agents to have moved.

**Negative**
- The relay is one more component to monitor while it exists, and a single point of failure for the hosts
  behind it. Remove it once every agent posts directly.

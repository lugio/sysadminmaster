# 0004 — Secrets live outside the source; services fail closed

- **Status**: Accepted
- **Date**: 2026-06, amended 2026-07

## Context

Early versions kept the agent and master tokens in `Config/config.json`, and had default token values in
the code as a fallback. A default that lives in the source is public the day the source is, and a silent
fallback revives it as soon as one environment variable is forgotten.

## Decision

1. Runtime secrets live in `.env` (mode `600`, ignored by git), loaded by the systemd units.
   `Config/config.json` is ignored as well; only `Config/config.example.json`, with placeholders, is tracked.
2. Settings are resolved environment first, then `config.json`. There is **no default value** for a token:
   the AgentMaster refuses to start without `SAM_AGENT_TOKEN` and `SAM_MASTER_TOKEN`, the dashboard refuses
   every sign-in without `SAM_ADMIN_PASSWORD`.
3. CI and the `pre-push` hook fail if `.env` or `Config/config.json` becomes tracked.
4. The agent token can be rotated without downtime: the previous value is accepted through
   `SAM_AGENT_TOKEN_LEGACY` during the rotation window only.

## Consequences

**Positive**
- A fresh deployment starts from the examples and cannot run with a known token.
- Forgetting a variable stops the service loudly instead of opening it silently.

**Negative**
- Operators must manage `.env` securely and back it up separately from the code.

# Architecture decision records

Short records of the decisions that shape SysAdminMaster: the context, the decision, and what it costs.
A decision that is replaced is not deleted: it is marked *Superseded* and links to its successor.

| # | Decision | Status |
|---|---|---|
| [0001](0001-broker-agent-zero-winrm.md) | Agents push telemetry and pull commands — no WinRM | Accepted |
| [0002](0002-agent-network-path.md) | Agents reach the server directly, or through an optional relay | Accepted |
| [0003](0003-http-direct-proxy-null.md) | Agent HTTP bypasses the system proxy; Windows PowerShell 5.1 only | Accepted |
| [0004](0004-secrets-out-of-source.md) | Secrets live outside the source; services fail closed | Accepted |
| [0005](0005-read-only-token.md) | A separate read-only token for monitoring consumers | Accepted |

New record: copy the structure of an existing one (Status, Date, Context, Decision, Consequences) and add
it to this table in the same pull request.

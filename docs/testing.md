# Testing

## Unit tests and repository checks

```bash
.venv-linux/bin/python tests/test_read_token.py   # read-only token contract (isolated temporary copy)
python3 tools/check-version.py                    # every version declaration == VERSION
python3 tools/check-endpoints.py                  # every UI call targets a live route
python3 tools/check-template-js.py                # inline JavaScript in templates compiles (needs node)
python3 tools/check-ps-encoding.py                # no typographic characters in PowerShell code
```

The CI runs all of them, plus a PowerShell parse of every script. The `pre-push` hook runs the fast ones.

## Posting a sample heartbeat

You can develop the server and the dashboard without Windows:

```bash
curl -s -X POST http://127.0.0.1:5000/api/agent/metrics \
  -H "X-Agent-Token: $SAM_AGENT_TOKEN" -H "Content-Type: application/json" \
  -d '{"hostname":"RDSH01","agent_version":"2.31.2","cpu":12,"ram":{"UsedPercent":40},"sessions":{"Total":0,"Active":0}}'
```

The host then appears on the Agents page.

## End-to-end suite (Playwright)

The scripts in `e2e/` sign in to a real dashboard and walk through its pages.

```bash
npm install
npx playwright install chromium
QA_BASE=https://sam.example.com QA_USER=admin QA_PASS='…' ./e2e/run-tests.sh --gate
```

| Variable | Meaning |
|---|---|
| `QA_BASE` | dashboard URL |
| `QA_USER`, `QA_PASS` | credentials |
| `QA_SHOTS` | folder for screenshots |
| `QA_PLAYWRIGHT_PATH` | path to an existing Playwright install |

`--gate` runs the fast, reliable scripts; `--all` adds the ones that need live agents; `--list` lists them.

### The read-only guard

Every gate script installs `e2e/lib/prod-safe.js`, a **network-level** guard: each request leaving the
browser is checked against an allow-list of read-only routes and actions, and anything else is answered
with a fake response before it reaches the server. This makes the suite safe to run against a production
dashboard. Clicking a button is not enough to trigger an action — the request never leaves.

Never widen the allow-list without proof that the action does not change the host, and keep the fake
responses faithful to the real contract (a fake that does not look like the real answer hides bugs).

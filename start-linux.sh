#!/usr/bin/env bash
# Lance AgentMaster (5000) + Dashboard (5050) sous Linux.
# Ports : SAM_MASTER_PORT / SAM_DASHBOARD_PORT (défauts 5000 / 5050).
# Development / manual start. In production use the systemd units in deploy/systemd/ (docs/installation.md).
set -e
cd "$(dirname "$0")"
[ -f .env ] && { set -a; . ./.env; set +a; }
: "${SAM_MASTER_URL:=http://127.0.0.1:5000}"; export SAM_MASTER_URL
PY=./.venv-linux/bin/python
mkdir -p Logs
echo "→ AgentMaster (:5000)…"; nohup "$PY" AgentMaster/master.py >> Logs/agentmaster-linux.log 2>&1 & echo "  PID $!"
sleep 2
echo "→ Dashboard (:5050)…"; nohup "$PY" Web/app.py >> Logs/dashboard-linux.log 2>&1 & echo "  PID $!"
echo "OK — Dashboard http://<host>:${SAM_DASHBOARD_PORT:-5050}  |  AgentMaster http://<host>:${SAM_MASTER_PORT:-5000}/api/health"

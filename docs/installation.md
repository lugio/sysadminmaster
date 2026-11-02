# Installation

## Requirements

| Where | What |
|---|---|
| Server | Linux, Python **3.11+**, about 200 MB of disk, outbound HTTPS if you use e-mail alerts |
| Session hosts | Windows Server with **Windows PowerShell 5.1**, local administrator rights to install |
| Network | session hosts → server, TCP **5000** (AgentMaster); administrators → reverse proxy → server, TCP **5050** (dashboard) |

## 1. Server

```bash
sudo useradd --system --home /opt/sysadminmaster --shell /usr/sbin/nologin sam
sudo git clone https://github.com/lugio/sysadminmaster.git /opt/sysadminmaster
sudo chown -R sam:sam /opt/sysadminmaster
cd /opt/sysadminmaster
sudo -u sam python3 -m venv .venv-linux
sudo -u sam ./.venv-linux/bin/pip install -r requirements.txt
```

Create the configuration from the example and restrict it:

```bash
sudo -u sam cp .env.example .env
sudo chmod 600 .env
```

Generate the secrets — each one different:

```bash
for v in SAM_AGENT_TOKEN SAM_MASTER_TOKEN SAM_READ_TOKEN SAM_SECRET_KEY; do
  echo "$v=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
done
```

Paste them into `.env`, set `SAM_ADMIN_PASSWORD`, `SAM_BROKER_HOST` and the other values described in
[configuration.md](configuration.md). The services refuse to start without their tokens.

## 2. Services

```bash
sudo cp deploy/systemd/sam-agentmaster.service deploy/systemd/sam-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sam-agentmaster sam-dashboard
curl -s http://127.0.0.1:5000/api/health
```

For a quick trial without systemd, `./start-linux.sh` starts both processes in the background.

## 3. Reverse proxy and firewall

- Publish **only the dashboard** (5050) through a TLS reverse proxy (nginx, Caddy, Traefik…). Socket.IO needs
  WebSocket upgrade headers.
- Allow **5000** only from the session hosts (and from read-only consumers, if you use the read-only token).
- Never publish 5000 on the Internet.

Minimal nginx location:

```nginx
location / {
    proxy_pass http://127.0.0.1:5050;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## 4. First agent

On a session host, in an elevated Windows PowerShell 5.1 prompt, from a copy of the `Scripts` folder:

```powershell
.\Install-SysAdminMaster-Agent.ps1 -BrokerUrl "http://sam-master:5000/api/agent/metrics" -Token "<SAM_AGENT_TOKEN>"
```

Or let the host fetch everything from the master:

```powershell
.\Bootstrap-Agent-FromMaster.ps1 -MasterHost sam-master -Port 5000 -Token "<SAM_AGENT_TOKEN>"
```

The host appears on the **Agents** page within a few seconds. See [agents.md](agents.md) for the rest of the
fleet, updates and troubleshooting.

## Upgrading the server

```bash
cd /opt/sysadminmaster
sudo -u sam git pull
sudo -u sam ./.venv-linux/bin/pip install -r requirements.txt
sudo systemctl restart sam-agentmaster sam-dashboard
```

Read the [CHANGELOG](../CHANGELOG.md) first: a release that changes the agent says so, and the agents are
then updated from the dashboard (**Agents → update**), in waves.

## Backups

Back up `.env`, `Config/config.json` (if used) and `agentmaster.db`. Stop `sam-agentmaster` or use
`sqlite3 agentmaster.db ".backup backup.db"` for a consistent copy.

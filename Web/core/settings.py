"""Deployment settings shared by the dashboard (Web/) and the ingestion service (AgentMaster/).

Every value that depends on a given site (host names, URLs, mail sender, Office 365
scope) is read here, from the environment first and from ``Config/config.json``
second. Nothing site-specific is hard-coded anywhere else in the code base.

| Variable                    | Meaning                                                        | Default                       |
|-----------------------------|----------------------------------------------------------------|-------------------------------|
| ``SAM_BROKER_HOST``         | short name of the broker host (the agent that holds the        | ``RDSServers.ConnectionBroker``|
|                             | profile disk and orchestrates the other hosts)                 | of config.json, else          |
|                             |                                                                | ``RDS-BROKER``                |
| ``SAM_DASHBOARD_URL``       | public URL of the dashboard, used for links in e-mails         | empty = no link               |
| ``SAM_O365_SCOPE_OU``       | OU (sub-tree) of the Office 365 users                          | empty = no OU filter          |
| ``SAM_O365_SCOPE_EXCLUDE``  | comma-separated sub-OUs excluded from the scope                | empty                         |
| ``SAM_O365_IGNORED_ACCOUNTS``| comma-separated extra technical accounts (added to the        | empty                         |
|                             | built-in generic list)                                         |                               |
"""
import json
import os

_REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG_PATH = os.path.join(_REPO_DIR, 'Config', 'config.json')
DEFAULT_BROKER = 'RDS-BROKER'


def _config():
    try:
        with open(_CONFIG_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _list(name):
    return [x.strip() for x in os.environ.get(name, '').split(',') if x.strip()]


def broker_host():
    """Short, upper-case name of the broker host (as agents report their hostname)."""
    v = os.environ.get('SAM_BROKER_HOST', '').strip()
    if not v:
        v = str(_config().get('RDSServers', {}).get('ConnectionBroker') or DEFAULT_BROKER)
    return v.split('.')[0].upper()


def dashboard_url(path=''):
    """Public dashboard URL (no trailing slash) + ``path``; empty string when not configured."""
    base = os.environ.get('SAM_DASHBOARD_URL', '').strip().rstrip('/')
    return (base + path) if base else ''


def o365_scope_ou():
    return os.environ.get('SAM_O365_SCOPE_OU', '').strip()


def o365_scope_exclude():
    return _list('SAM_O365_SCOPE_EXCLUDE')


def o365_ignored_accounts():
    return {x.lower() for x in _list('SAM_O365_IGNORED_ACCOUNTS')}

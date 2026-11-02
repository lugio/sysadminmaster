"""
Garde d'authentification du Dashboard SysAdminMaster (audit sécu 2026-06-22, P0).

Protection intérimaire avant un SSO (reverse proxy + fournisseur d'identité). Tout accès au Dashboard
exige une session authentifiée :
- pages HTML  -> redirection vers /login si non authentifié
- routes /api -> 401 JSON
- public      -> /login, /logout, /static, /assets, /favicon, /api/health

Identifiants :
- utilisateur ∈ General.AdminUsers (config.json)
- mot de passe partagé = variable d'env  SAM_ADMIN_PASSWORD  (OBLIGATOIRE en prod)
  Si non définie : connexion impossible (fail-closed) — définir avant lancement :
      export SAM_ADMIN_PASSWORD='...'      (Linux)
      $env:SAM_ADMIN_PASSWORD='...'        (Windows / tâche planifiée)
"""
import os
import hmac
import logging
from flask import session, request, redirect, jsonify, render_template_string

logger = logging.getLogger(__name__)

_ADMIN_PASSWORD = os.environ.get('SAM_ADMIN_PASSWORD')

# Chemins accessibles sans authentification
PUBLIC_PREFIXES = ('/login', '/logout', '/static/', '/assets/', '/favicon.ico', '/api/health')

LOGIN_HTML = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SysAdminMaster — Connexion</title>
<style>
 body{font-family:Segoe UI,system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;
   align-items:center;justify-content:center;height:100vh;margin:0}
 .card{background:#1e293b;padding:2.5rem;border-radius:14px;box-shadow:0 10px 40px rgba(0,0,0,.4);width:320px}
 h1{font-size:1.15rem;margin:0 0 1.5rem;color:#38bdf8;text-align:center}
 label{display:block;font-size:.8rem;margin:.8rem 0 .3rem;color:#94a3b8}
 input{width:100%;padding:.6rem;border:1px solid #334155;border-radius:8px;background:#0f172a;color:#e2e8f0;box-sizing:border-box}
 button{width:100%;margin-top:1.4rem;padding:.7rem;border:0;border-radius:8px;background:#2563eb;color:#fff;font-weight:600;cursor:pointer}
 button:hover{background:#1d4ed8}
 .err{color:#f87171;font-size:.8rem;margin-top:1rem;text-align:center}
</style></head><body>
<form class="card" method="post" action="/login">
 <h1>🛡️ SysAdminMaster</h1>
 <label>Utilisateur</label><input name="username" autofocus autocomplete="username">
 <label>Mot de passe</label><input name="password" type="password" autocomplete="current-password">
 <button type="submit">Connexion</button>
 {% if error %}<div class="err">{{ error }}</div>{% endif %}
</form></body></html>"""


def _check_credentials(user, password):
    """Vérifie utilisateur (∈ AdminUsers) + mot de passe (env, comparaison constante)."""
    if not _ADMIN_PASSWORD:
        logger.error("SAM_ADMIN_PASSWORD non défini : connexion refusée (fail-closed).")
        return False
    from Web.core.engine import FULL_CONFIG
    admin_users = [u.lower() for u in FULL_CONFIG.get('General', {}).get('AdminUsers', ['administrateur'])]
    user_ok = user.strip().lower() in admin_users
    # ⚠️ compare_digest sur des str lève TypeError si l'une contient un caractère
    # non-ASCII (mdp saisi avec accent/€ → 500 sur /login, constaté 25/08/2026).
    # Encoder en bytes rend la comparaison valide pour tout Unicode → 401 propre.
    pwd_ok = hmac.compare_digest(password.encode('utf-8'), _ADMIN_PASSWORD.encode('utf-8'))
    return user_ok and pwd_ok


def init_auth(app):
    """Enregistre la garde before_request + les routes /login et /logout."""

    @app.before_request
    def _auth_gate():
        if session.get('sam_auth'):
            return None
        path = request.path
        if any(path == p or path.startswith(p) for p in PUBLIC_PREFIXES):
            return None
        if path.startswith('/api/'):
            return jsonify({"error": "Authentification requise"}), 401
        return redirect('/login')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            user = request.form.get('username', '')
            password = request.form.get('password', '')
            if _check_credentials(user, password):
                session['sam_auth'] = True
                session['sam_user'] = user.strip()
                logger.info(f"🔓 Connexion réussie : {user}")
                return redirect('/')
            logger.warning(f"⛔ Échec connexion ({request.remote_addr}) user='{user}'")
            return render_template_string(LOGIN_HTML, error="Identifiants invalides"), 401
        return render_template_string(LOGIN_HTML, error=None)

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect('/login')

    if not _ADMIN_PASSWORD:
        logger.warning("⚠️ AUTH DASHBOARD : SAM_ADMIN_PASSWORD non défini -> personne ne pourra se connecter. "
                       "Définir la variable d'environnement avant lancement en prod.")
    logger.info("🛡️ Garde d'authentification Dashboard activée.")

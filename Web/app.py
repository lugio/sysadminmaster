# Patch eventlet AVANT tout autre import (threading, logging, etc.)
# Suppression des warnings connus (deprecation eventlet + RLock greening)
import warnings, sys, io
warnings.filterwarnings("ignore", category=DeprecationWarning)
import eventlet
_stderr, sys.stderr = sys.stderr, io.StringIO()
eventlet.monkey_patch()
sys.stderr = _stderr
warnings.resetwarnings()
warnings.filterwarnings("ignore", category=ResourceWarning)

import os
import sys
import logging
from logging.handlers import RotatingFileHandler

from flask import Flask, send_from_directory, Response
from flask_socketio import SocketIO
from flask_cors import CORS

# Ajout du chemin racine pour les imports relatifs
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Web.core.engine import db, cache, start_engine
from Web.routes.views import views_bp
from Web.routes.api import api_bp, APP_VERSION
from Web.routes.actions import actions_bp

# Configuration Logging avec rotation automatique (5 Mo max, 5 backups)
log_file = os.path.join(os.path.dirname(__file__), 'web_server.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
# S'assurer que les logs Flask/Werkzeug sont visibles même avec SocketIO
logging.getLogger('werkzeug').setLevel(logging.INFO)
logger = logging.getLogger("SysAdminMaster-Core")

def create_app():
    app = Flask(__name__)
    # Session secret: SAM_SECRET_KEY. Without it, a random key is generated at start-up
    # (no shared default that anyone could read in the source): sessions then do not
    # survive a restart. Set SAM_SECRET_KEY in production.
    _secret = os.environ.get('SAM_SECRET_KEY', '').strip()
    if not _secret:
        import secrets as _secrets
        _secret = _secrets.token_hex(32)
        logging.getLogger(__name__).warning(
            "SAM_SECRET_KEY not set: using a random session key (sessions end at each restart).")
    app.config['SECRET_KEY'] = _secret
    
    CORS(app)
    
    # Initialisation de SocketIO
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')
    
    # Partage des instances globales via l'app
    app.db = db
    app.cache = cache
    app.socketio = socketio

    # Garde d'authentification (P0 sécu 2026-06-22) — avant les blueprints
    from Web.core.auth import init_auth
    init_auth(app)

    # Version applicative disponible dans tous les templates (footer, etc.).
    # Source unique = fichier VERSION lu par api.py ; evite la derive du footer
    # code en dur (bug : l'UI affichait v2.7.6 alors que VERSION = 2.10.x).
    @app.context_processor
    def inject_app_version():
        return {"app_version": APP_VERSION}

    # Valeurs propres au site (Web/core/settings.py), lues par les templates et
    # leur JavaScript (`{{ broker_host | tojson }}`) : aucune n'est codée en dur.
    from Web.core import settings as site_settings

    @app.context_processor
    def inject_site_settings():
        return {"broker_host": site_settings.broker_host(),
                "o365_scope_ou": site_settings.o365_scope_ou()}

    # Enregistrement des Blueprints
    app.register_blueprint(views_bp)
    app.register_blueprint(api_bp, url_prefix='')
    app.register_blueprint(actions_bp, url_prefix='')

    # Redirection pour les assets (compatibilité frontend)
    @app.route('/assets/<path:filename>')
    def serve_assets(filename):
        return send_from_directory(os.path.join(app.root_path, 'static'), filename)

    @app.route('/static/favicon.ico')
    @app.route('/favicon.ico')
    def favicon():
        return Response(status=204)

    # Démarrage des tâches de fond (Telemetrie & DB cleaning)
    with app.app_context():
        start_engine()

    @app.errorhandler(404)
    def page_not_found(e):
        return {"error": "Route non trouvée"}, 404

    return app, socketio

app, socketio = create_app()

if __name__ == '__main__':
    logger.info(f"🚀 Lancement du Backend Modulaire SysAdminMaster v{APP_VERSION}")
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get('SAM_DASHBOARD_PORT', '5050')), log_output=True)

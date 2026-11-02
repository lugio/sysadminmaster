from flask import Blueprint, render_template, redirect, url_for, Response, send_from_directory
import os
from Web.core.engine import measure_performance

views_bp = Blueprint('views', __name__)

@views_bp.route('/')
@measure_performance
def index():
    return redirect(url_for('views.dashboard'))

@views_bp.route('/dashboard')
@measure_performance
def dashboard():
    return render_template('dashboard.html', active_page='dashboard')

@views_bp.route('/sessions')
@measure_performance
def sessions_page():
    return render_template('sessions.html', active_page='sessions')

@views_bp.route('/profiles')
@measure_performance
def profiles_page():
    return render_template('profiles.html', active_page='profiles')

@views_bp.route('/resources')
@measure_performance
def resources_page():
    return render_template('resources.html', active_page='resources')

@views_bp.route('/tools')
@measure_performance
def tools_page():
    return render_template('tools.html', active_page='tools')

@views_bp.route('/shell')
def shell_redirect():
    # Compat : la console shell vit dans la page Outils. Evite un 404 sur d'anciens
    # signets /shell (heritage Windows). Portage Linux 2026-06-22.
    return redirect(url_for('views.tools_page'))

@views_bp.route('/reports')
@measure_performance
def reports_page():
    return render_template('reports.html', active_page='reports')

@views_bp.route('/alerts')
@measure_performance
def alerts_page():
    return render_template('alerts.html', active_page='alerts')

@views_bp.route('/agents')
@measure_performance
def agents_page():
    return render_template('agents.html', active_page='agents')

@views_bp.route('/logs')
@measure_performance
def logs_page():
    return render_template('logs.html', active_page='logs')

@views_bp.route('/optimization')
@measure_performance
def optimization_page():
    return render_template('optimization.html', active_page='optimization')

@views_bp.route('/licenses')
@measure_performance
def licenses_page():
    return render_template('licenses.html', active_page='licenses')

@views_bp.route('/styleguide')
@measure_performance
def styleguide_page():
    # Source de vérité vivante du design system (tokens, composants, utilitaires).
    # Page de référence pour garder une UI cohérente sur toutes les pages.
    return render_template('styleguide.html', active_page='styleguide')

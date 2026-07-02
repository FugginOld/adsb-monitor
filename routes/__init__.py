"""Blueprint registration."""
from flask import Flask

from routes.backup import bp as backup_bp
from routes.dashboard import bp as dashboard_bp
from routes.logs import bp as logs_bp
from routes.services import bp as services_bp
from routes.settings import bp as settings_bp
from routes.stats import bp as stats_bp
from routes.versions import bp as versions_bp


def register_blueprints(app: Flask) -> None:
    for bp in (dashboard_bp, stats_bp, versions_bp, settings_bp, services_bp, logs_bp, backup_bp):
        app.register_blueprint(bp)

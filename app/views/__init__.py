from flask import Flask


def register_blueprints(app: Flask):
    from app.views.files import bp as files_bp
    from app.views.servers import bp as servers_bp
    from app.views.transfers import bp as transfers_bp
    from app.views.logs import bp as logs_bp
    from app.views.keepalive import bp as keepalive_bp

    app.register_blueprint(files_bp)
    app.register_blueprint(servers_bp)
    app.register_blueprint(transfers_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(keepalive_bp)

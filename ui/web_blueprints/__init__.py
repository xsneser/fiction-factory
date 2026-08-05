"""Web UI 蓝图包：自 ui/web_ui.py 按域拆分（dashboard/timeline/desk/books/libraries/tools/settings/publish）。"""


def register_blueprints(app):
    from .dashboard import bp as dashboard_bp
    from .timeline import bp as timeline_bp
    from .desk import bp as desk_bp
    from .books import bp as books_bp
    from .libraries import bp as libraries_bp
    from .tools import bp as tools_bp
    from .settings import bp as settings_bp
    from .publish import bp as publish_bp

    for bp in (dashboard_bp, timeline_bp, desk_bp, books_bp,
               libraries_bp, tools_bp, settings_bp, publish_bp):
        app.register_blueprint(bp)

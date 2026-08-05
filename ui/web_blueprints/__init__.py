"""Web UI 蓝图包：自 ui/web_ui.py 按域拆分（dashboard/storyline/desk/books/libraries/tools/settings/publish/world_builder）。"""


def register_blueprints(app):
    from .dashboard import bp as dashboard_bp
    from .storyline import bp as storyline_bp
    from .desk import bp as desk_bp
    from .books import bp as books_bp
    from .libraries import bp as libraries_bp
    from .tools import bp as tools_bp
    from .settings import bp as settings_bp
    from .publish import bp as publish_bp
    from .world_builder import bp as world_builder_bp

    for bp in (dashboard_bp, storyline_bp, desk_bp, books_bp,
               libraries_bp, tools_bp, settings_bp, publish_bp,
               world_builder_bp):
        app.register_blueprint(bp)

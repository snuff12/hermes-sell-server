"""Hermes directory-plugin entry point."""
def register(ctx):
    from .src.hermes_sell.plugin import register as register_tools
    return register_tools(ctx)

from django import template

from hours_app.seasons import current_season

register = template.Library()


@register.simple_tag
def hours_season():
    """The latest hours season, for the "season is over" banner."""
    try:
        return current_season()
    except Exception:
        return None

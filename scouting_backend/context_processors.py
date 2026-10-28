from utils import config_loader
from authenticate.models import SiteSettings, AuthorizedUser

def _hours_avatar_url(request):
    """
    Profile picture from the hours app, for the settings menu.

    HoursAuthMiddleware resolves request.user from the scouting session, so the
    two systems already agree on who is signed in. Cached in the session
    because the nav renders on every page and this would otherwise add a query
    to each one; hours_app clears the key when the picture changes.
    """
    cached = request.session.get('hours_avatar')
    if cached is not None:
        return cached

    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return ''

    try:
        # Imported here so the scouting site still renders if hours_app is
        # removed from INSTALLED_APPS.
        from hours_app.models import UserProfile
        profile = UserProfile.objects.filter(user=user).only('avatar_url').first()
        avatar = profile.avatar_url if profile else ''
    except Exception:
        return ''

    request.session['hours_avatar'] = avatar
    return avatar


def config_context(request):
    """Make config values available in all templates"""
    config = config_loader.get_config()
    try:
        settings = SiteSettings.objects.first()
    except Exception:
        settings = None

    email = request.session.get('email')
    is_admin = False
    role = None
    if email:
        try:
            authorized_user = AuthorizedUser.objects.filter(email__iexact=email).first()
            roles = authorized_user.get_roles_list() if authorized_user else []
            role = ','.join(roles) or 'student'
            is_admin = bool(authorized_user and authorized_user.is_admin)
        except Exception:
            role = 'student'

    return {
        'app_name': config.get('app_name', 'Scouting System'),
        'team_name': config.get('team_name', 'Team'),
        'theme_color': (settings.theme_color if settings and settings.theme_color else config.get('theme_color', '#e74c3c')),
        'logo_url': (settings.logo_url if settings and settings.logo_url else config.get('logo_url', '/static/images/logo.png')),
        'is_admin': is_admin,
        'is_team_member': (request.session.get('email') or '').lower().endswith('@team2073.com') or bool(is_admin),
        'role': role,
        'user_avatar_url': _hours_avatar_url(request),
        # Roles are stored comma-separated (e.g. "scout_tech,strategist").
        # helpers.role_required() splits them, so templates must too — an exact
        # `role == 'scout_tech'` check hides nav links from multi-role users who
        # can actually open the page.
        'roles': [r.strip() for r in role.split(',') if r.strip()] if role else [],
    }

import functools

from django.conf import settings
from django.http import HttpResponseRedirect, JsonResponse
from django.core.cache import cache


def login_required(function):
    @functools.wraps(function)
    def wrapper(request, *args, **kw):
        if not request.session.get("email"):
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({"error": "Not authenticated — please log in"}, status=401)
            return HttpResponseRedirect('/auth/')
        else:
            return function(request, *args, **kw)

    return wrapper


def role_required(*allowed_roles):
    """
    Decorator that checks the user has one of the allowed roles.
    - 'admin' session role always passes (full access).
    - 'teacher' session role passes for all non-admin-panel routes.
    - On failure renders a 403 page.
    """
    def decorator(function):
        @functools.wraps(function)
        def wrapper(request, *args, **kw):
            if not request.session.get('email'):
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    from django.http import JsonResponse
                    return JsonResponse({'error': 'Not authenticated'}, status=401)
                from django.http import HttpResponseRedirect
                return HttpResponseRedirect('/auth/')
            roles_str = request.session.get('role', 'student')
            user_roles = set(roles_str.split(',')) if roles_str else {'student'}
            # Users in the AuthorizedUser table (is_admin) always have full access
            if request.session.get('is_admin') or bool(user_roles & set(allowed_roles)):
                return function(request, *args, **kw)
            if 'teacher' in user_roles and 'admin_only' not in allowed_roles:
                return function(request, *args, **kw)
            # Access denied
            from django.shortcuts import render
            return render(request, 'authenticate/unauthorized.html',
                          {'email': request.session.get('email'), 'role': roles_str},
                          status=403)
        return wrapper
    return decorator


TEAM_EMAIL_DOMAIN = '@team2073.com'


def is_team_member(request):
    """Signed in with a team email (any @team2073.com account), or an admin."""
    email = (request.session.get('email') or '').lower()
    return email.endswith(TEAM_EMAIL_DOMAIN) or bool(request.session.get('is_admin'))


def team_member_required(function):
    """Open to every @team2073.com account (and admins), whatever their role."""
    @functools.wraps(function)
    def wrapper(request, *args, **kw):
        if not request.session.get('email'):
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'error': 'Not authenticated'}, status=401)
            return HttpResponseRedirect('/auth/')
        if is_team_member(request):
            return function(request, *args, **kw)
        from django.shortcuts import render
        return render(request, 'authenticate/unauthorized.html',
                      {'email': request.session.get('email'),
                       'role': request.session.get('role', 'student')},
                      status=403)
    return wrapper


def rate_limit(max_calls, period_seconds):
    """
    Simple IP-based rate limiter using Django's cache.
    max_calls: maximum number of allowed calls in the window
    period_seconds: window length in seconds
    """
    def decorator(function):
        @functools.wraps(function)
        def wrapper(request, *args, **kw):
            remote_addr = request.META.get('REMOTE_ADDR', 'unknown')
            trusted_proxies = getattr(settings, 'TRUSTED_PROXIES', [])
            if remote_addr in trusted_proxies:
                forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
                ip = forwarded_for.split(',')[0].strip() or remote_addr
            else:
                ip = remote_addr
            cache_key = f"rl:{function.__name__}:{ip}"
            count = cache.get(cache_key, 0)
            if count >= max_calls:
                return JsonResponse(
                    {'error': 'Too many requests. Please try again later.'},
                    status=429
                )
            cache.set(cache_key, count + 1, timeout=period_seconds)
            return function(request, *args, **kw)
        return wrapper
    return decorator


def group_records_by_match(records):
    """
    Group scouting records by match number.

    A match scouted by two people produces two rows (scout_name is part of the
    uniqueness key), so anything that averages the rows directly counts a
    double-scouted match twice. Grouping first lets callers collapse each match
    to a single value before averaging, giving every match equal weight.

    Returns a list of record lists, ordered by match number.
    """
    from collections import defaultdict

    groups = defaultdict(list)
    for record in records:
        groups[getattr(record, 'match_number', None)].append(record)
    return [groups[key] for key in sorted(groups, key=lambda k: (k is None, k))]


def per_match_values(records, get_value):
    """
    Collapse duplicate scout reports into one value per match.

    get_value(record) returns a float, or None when that scout did not record
    the field. Matches where no scout recorded a value are skipped, so a blank
    field never drags an average toward zero. When two scouts disagree, their
    reports are averaged with each other first — that disagreement stays
    contained to its own match instead of double-weighting it.
    """
    values = []
    for group in group_records_by_match(records):
        readings = [v for v in (get_value(r) for r in group) if v is not None]
        if readings:
            values.append(sum(readings) / len(readings))
    return values


def match_count(records):
    """Number of distinct matches, not the number of scouting rows."""
    return len(group_records_by_match(records))

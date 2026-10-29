import io
import json
import base64
import logging
from datetime import datetime, timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib import messages
from django.http import JsonResponse
from django.utils import timezone
from django.db.models import Sum, F, ExpressionWrapper, fields
from django.contrib.auth.models import User

from .models import (
    CheckInOut, SeasonRank, UserProfile, Badge, HoursAdjustment,
    SUBTEAM_CHOICES, SUBTEAM_COLORS, BADGE_CHOICES, BADGE_COLORS, BADGE_META, badge_roster,
)
from . import seasons
from authenticate.models import AuthorizedUser
from helpers import login_required

logger = logging.getLogger(__name__)


def generate_qr_code(url, box_size=10, border=4):
    """Render `url` as a base64 PNG data URI, or '' if qrcode is unavailable."""
    try:
        from qrcode import QRCode
        qr = QRCode(version=None, box_size=box_size, border=border)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill='black', back_color='white')
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()
    except Exception:
        logger.exception("generate_qr_code failed for url=%s", url)
        return ''


ABOUT_MAX_LENGTH = 280
PRONOUNS_MAX_LENGTH = 40

# Cloudinary is already the upload path for robot pictures in pit scouting,
# so profile pictures use the same account rather than introducing storage.
_AVATAR_TRANSFORM = "upload/w_400,h_400,c_fill,g_face,q_auto,f_auto/"


def _upload_avatar(image_file):
    """Validate and upload one image, returning a face-cropped Cloudinary URL.

    Raises ValueError with a user-facing message when the file is not usable.
    """
    if image_file.size > 8 * 1024 * 1024:
        raise ValueError('That image is larger than 8 MB. Please pick a smaller one.')

    try:
        from PIL import Image
        img = Image.open(image_file)
        img.verify()
        if img.format not in ('PNG', 'JPEG', 'WEBP'):
            raise ValueError('format')
        image_file.seek(0)
    except ValueError:
        raise ValueError('Only PNG, JPEG and WebP images are supported.')
    except Exception:
        raise ValueError('That file could not be read as an image.')

    try:
        import os
        import cloudinary
        import cloudinary.uploader
        # teams.views configures cloudinary at import time, but relying on that
        # side effect means an avatar upload breaks if that module ever stops
        # being imported first. Configuring here is idempotent.
        cloudinary.config(
            cloud_name=os.environ.get('CLOUD_NAME'),
            api_key=os.environ.get('CLOUD_API_KEY'),
            api_secret=os.environ.get('CLOUD_API_SECRET'),
            secure=True,
        )
        response = cloudinary.uploader.upload(image_file, folder='hours_avatars')
        url = response['secure_url']
    except Exception:
        logger.exception("Cloudinary avatar upload failed")
        raise ValueError('Upload failed. Please try again in a moment.')

    # Square face crop, so a full-body photo still reads at 40px.
    parts = url.split('upload/')
    if len(parts) == 2:
        url = parts[0] + _AVATAR_TRANSFORM + parts[1]
    return url


def _may_moderate(request):
    """True when the viewer can reset someone else's picture.

    Either an admin-panel admin (AuthorizedUser) or an hours-app lead/mentor —
    the two privilege systems this project already has. A mentor should not
    need an admin-panel seat just to clear an inappropriate photo.
    """
    email = request.session.get('email')
    authorized_user = AuthorizedUser.objects.filter(email__iexact=email).first() if email else None
    if authorized_user and authorized_user.is_admin:
        return True
    profile = UserProfile.objects.filter(user=request.user).first()
    return bool(profile and profile.is_privileged())


def _profile_card(user, profile=None):
    """Display bundle for a person: avatar, initials, about, badges."""
    if profile is None:
        profile = UserProfile.objects.filter(user=user).first()
    earned = list(
        Badge.objects.filter(user=user).order_by('awarded_at').values_list('badge_type', flat=True)
    )
    return {
        'avatar_url': profile.avatar_url if profile else '',
        'initials': profile.initials if profile else (user.username or '?')[:2].upper(),
        'about': profile.about if profile else '',
        'pronouns': profile.pronouns if profile else '',
        'badges': [b for b in badge_roster(earned) if b['earned']],
        'roster': badge_roster(earned),
        'earned_count': len(earned),
        'badge_total': len(BADGE_CHOICES),
    }


@login_required
def dashboard(request):
    if not request.user.is_authenticated:
        return redirect('/auth/')

    CheckInOut.close_forgotten_sessions()

    # Onboarding gate
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if not profile.onboarding_complete:
        return redirect('hours:onboarding')

    # Between seasons the dashboard is a read-only view of the season that
    # just ended, so "this week" means that season's final week.
    season = seasons.current_season()
    season_open = seasons.is_open(season)
    now = timezone.now()
    anchor = now if season_open else season.ended_at
    my_sessions = seasons.sessions_in(season, CheckInOut.objects.filter(user=request.user))
    my_adjustments = seasons.adjustments_in(season, HoursAdjustment.objects.filter(user=request.user)) if season else HoursAdjustment.objects.none()

    active_session = my_sessions.filter(check_out_time__isnull=True).first() if season_open else None

    one_week_before = anchor - timedelta(days=7)
    weekly_stats = my_sessions.filter(
        check_in_time__gte=one_week_before,
        check_in_time__lte=anchor,
        check_out_time__isnull=False,
    ).annotate(
        duration_calc=ExpressionWrapper(
            F('check_out_time') - F('check_in_time'),
            output_field=fields.DurationField()
        )
    ).aggregate(total=Sum('duration_calc'))

    weekly_hours = 0
    if weekly_stats['total']:
        weekly_hours = weekly_stats['total'].total_seconds() / 3600
    weekly_hours += my_adjustments.filter(
        work_date__gt=timezone.localtime(one_week_before).date(),
        work_date__lte=timezone.localtime(anchor).date(),
    ).aggregate(s=Sum('hours'))['s'] or 0
    weekly_hours = round(max(weekly_hours, 0), 1)

    season_rank = SeasonRank.objects.filter(user=request.user).first()
    season_hours = round(season_rank.total_hours, 1) if season_rank else 0

    # Rank position among all ranked users
    ranks_above = SeasonRank.objects.filter(total_hours__gt=season_hours).count()
    total_ranked = SeasonRank.objects.count()
    # No ranking row means no hours logged yet this season: unranked.
    rank_position = ranks_above + 1 if season_rank else None

    # The whole programme, not just what this person has — a locked badge is
    # the only way someone finds out the training exists.
    user_badges = list(Badge.objects.filter(user=request.user).order_by('awarded_at'))
    earned_types = [b.badge_type for b in user_badges]
    badge_data = badge_roster(earned_types)
    earned_count = len(earned_types)

    # Awards the recipient has not been shown yet. Revealed once, then marked
    # seen by the acknowledge endpoint so it never replays.
    unseen = [b for b in user_badges if b.seen_at is None]
    unseen_data = [
        {
            'type': b.badge_type,
            'label': b.get_badge_type_display(),
            'color': BADGE_COLORS.get(b.badge_type, '#888'),
            'svg': b.svg,
            'awarded_by': (
                b.awarded_by.get_full_name() or b.awarded_by.username
            ) if b.awarded_by else '',
        }
        for b in unseen
    ]

    scout_name = request.session.get('name', '')
    comp_code = request.session.get('comp_code', '')
    scouting_stats = _get_scouting_stats(scout_name, comp_code)

    # Who's currently checked in (nobody can be, between seasons)
    checked_in_now = (
        CheckInOut.objects
        .filter(check_out_time__isnull=True)
        .select_related('user')
        .order_by('check_in_time')
    ) if season_open else CheckInOut.objects.none()
    checked_in_count = checked_in_now.count()
    checked_in_names = [s.user.get_full_name() or s.user.username for s in checked_in_now[:5]]

    # The hero's week timeline: one row per day, each session drawn where it
    # actually happened between DAY_START and midnight. A session belongs to
    # the day it started; an open one runs up to now.
    DAY_START, DAY_END = 6, 24
    span = (DAY_END - DAY_START) * 3600
    today = timezone.localtime(anchor).date()
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    rows = {d: {'blocks': [], 'total': 0.0, 'added': 0.0} for d in days}

    def clock(dt):
        return dt.strftime('%I:%M %p').lstrip('0')

    for s in my_sessions.filter(
        check_in_time__date__gte=days[0], check_in_time__date__lte=today,
    ).only('check_in_time', 'check_out_time').order_by('check_in_time'):
        start = timezone.localtime(s.check_in_time)
        end = timezone.localtime(s.check_out_time or now)
        day = start.date()
        if day not in rows:
            continue
        hours = max(0.0, (end - start).total_seconds() / 3600)
        rows[day]['total'] += hours
        base = start.replace(hour=0, minute=0, second=0, microsecond=0)
        a_sec = min(max((start - base).total_seconds() - DAY_START * 3600, 0), span)
        b_sec = min(max((end - base).total_seconds() - DAY_START * 3600, 0), span)
        rows[day]['blocks'].append({
            'left': round(a_sec / span * 100, 2),
            'width': round(max(b_sec - a_sec, span * 0.006) / span * 100, 2),
            'open': s.check_out_time is None,
            'tip': f"{clock(start)} – {'now' if s.check_out_time is None else clock(end)} · {hours:.1f} h",
        })
    # Mentor-added hours have a date but no time of day: they count toward
    # the row's total and show as a "+h" mark instead of a block.
    for adj in my_adjustments.filter(work_date__gte=days[0], work_date__lte=today):
        rows[adj.work_date]['total'] += adj.hours
        rows[adj.work_date]['added'] += adj.hours
    week_rows = [
        {'day': d.strftime('%a'), 'date': d.day, 'today': d == today and season_open,
         'total': round(max(rows[d]['total'], 0), 1), 'blocks': rows[d]['blocks'],
         'added': round(rows[d]['added'], 1)}
        for d in days
    ]
    # Now-line position on today's row, if it falls inside the window.
    now_local = timezone.localtime(now)
    now_sec = now_local.hour * 3600 + now_local.minute * 60 - DAY_START * 3600
    now_pct = round(now_sec / span * 100, 2) if season_open and 0 <= now_sec <= span else None

    # Day streak: consecutive days with any logged time, ending today (or
    # yesterday, so a streak is not "broken" before today's meeting).
    logged_days = set(
        timezone.localtime(t).date() for t in my_sessions.filter(
            check_in_time__gte=anchor - timedelta(days=120),
        ).values_list('check_in_time', flat=True)
    ) | set(my_adjustments.filter(hours__gt=0).values_list('work_date', flat=True))
    streak = 0
    cursor = today if today in logged_days else today - timedelta(days=1)
    while cursor in logged_days:
        streak += 1
        cursor -= timedelta(days=1)

    last_out = (
        my_sessions.filter(check_out_time__isnull=False)
        .order_by('-check_out_time').values_list('check_out_time', flat=True).first()
    )

    def initials(u):
        first, last = (u.first_name or '').strip(), (u.last_name or '').strip()
        return ((first[:1] + last[:1]) or (u.username or '?')[:2]).upper()

    checked_in_faces = [
        {'initials': initials(s.user), 'name': s.user.get_full_name() or s.user.username}
        for s in checked_in_now[:6]
    ]

    check_in_url = request.build_absolute_uri(reverse('hours:check_in_out'))

    return render(request, 'hours_app/dashboard.html', {
        'checked_in_session': active_session,
        'check_in_url': check_in_url,
        'qr_code_url': generate_qr_code(check_in_url),
        'weekly_hours': weekly_hours,
        'season_hours': season_hours,
        'check_in_time_iso': active_session.check_in_time.isoformat() if active_session else None,
        'scouting_stats': scouting_stats,
        'profile': profile,
        'subteam_labels': profile.get_subteam_labels(),
        'badge_data': badge_data,
        'earned_count': earned_count,
        'badge_total': len(BADGE_CHOICES),
        'unseen_badges': unseen_data,
        'rank_position': rank_position,
        'total_ranked': total_ranked,
        'checked_in_count': checked_in_count,
        'checked_in_names': checked_in_names,
        'checked_in_faces': checked_in_faces,
        'week_rows': week_rows,
        'now_pct': now_pct,
        'day_streak': streak,
        'last_out': last_out,
        # One tick per hour of the 40-hour weekly goal, lit up to the hours logged.
        'goal_ticks': [i < int(weekly_hours) for i in range(40)],
        'first_name': request.user.first_name or (request.user.get_full_name() or request.user.username).split(' ')[0],
        'season': season,
        'season_open': season_open,
    })


def _get_scouting_stats(scout_name, comp_code):
    try:
        from teams.models import Team_Match_Data
        from django.db.models import Count
        qs = Team_Match_Data.objects.filter(scout_name__iexact=scout_name) if scout_name else Team_Match_Data.objects.none()
        qs_event = qs.filter(event=comp_code) if comp_code else qs
        total_event = qs_event.count()
        total_all = qs.count()
        last = qs.order_by('-id').first()

        # Rank among all scouts (all events, by total matches scouted)
        scout_rank = None
        total_scouts = 0
        if scout_name:
            scouts_above = (
                Team_Match_Data.objects
                .values('scout_name')
                .annotate(cnt=Count('id'))
                .filter(cnt__gt=total_all)
                .count()
            )
            scout_rank = scouts_above + 1
            total_scouts = Team_Match_Data.objects.values('scout_name').distinct().count()

        return {
            'total_event': total_event,
            'total_all': total_all,
            'last_match': last.match_number if last else None,
            'last_event': last.event if last else None,
            'scout_name': scout_name,
            'scout_rank': scout_rank,
            'total_scouts': total_scouts,
        }
    except Exception:
        logger.exception("_get_scouting_stats failed for scout_name=%s comp_code=%s", scout_name, comp_code)
        return {
            'total_event': 0, 'total_all': 0,
            'last_match': None, 'last_event': None,
            'scout_name': scout_name,
            'scout_rank': None, 'total_scouts': 0,
        }


def _get_scout_leaderboard(limit=None):
    try:
        from teams.models import Team_Match_Data
        from django.db.models import Count
        qs = (
            Team_Match_Data.objects
            .values('scout_name')
            .annotate(match_count=Count('id'))
            .order_by('-match_count')
        )
        if limit:
            qs = qs[:limit]
        return [
            {'scout_name': e['scout_name'], 'match_count': e['match_count'], 'rank': i + 1}
            for i, e in enumerate(qs)
        ]
    except Exception:
        logger.exception("_get_scout_leaderboard failed (limit=%s)", limit)
        return []


@login_required
def scout_leaderboard(request):
    scout_name = request.session.get('name', '')
    raw = _get_scout_leaderboard()
    scout_name_lower = scout_name.lower() if scout_name else ''
    leaderboard = [
        {**entry, 'is_you': entry['scout_name'].lower() == scout_name_lower}
        for entry in raw
    ]
    user_stats = _get_scouting_stats(scout_name, request.session.get('comp_code', ''))
    return render(request, 'hours_app/scout_leaderboard.html', {
        'leaderboard': leaderboard,
        'scout_name': scout_name,
        'user_stats': user_stats,
    })


@login_required
def onboarding(request):
    if not request.user.is_authenticated:
        return redirect('/auth/')

    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if profile.onboarding_complete:
        return redirect('hours:dashboard')

    if request.method == 'POST':
        valid_values = {v for v, _ in SUBTEAM_CHOICES}
        subteams = [s for s in request.POST.getlist('subteams') if s in valid_values]
        profile.subteams = ','.join(subteams)
        profile.onboarding_complete = True
        profile.save()
        return redirect('hours:dashboard')

    return render(request, 'hours_app/onboarding.html', {
        'subteam_choices': SUBTEAM_CHOICES,
    })


@login_required
def profile(request):
    """Edit your own picture, about-me and subteams.

    Onboarding covers subteams once at signup and then locks itself, so this is
    the page the dashboard's Edit link belongs on.
    """
    prof, _ = UserProfile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        action = request.POST.get('action', 'save')

        if action == 'remove_photo':
            prof.avatar_url = ''
            prof.save(update_fields=['avatar_url'])
            # The scouting nav caches this in the session; drop it so the
            # settings menu stops showing the old picture straight away.
            request.session.pop('hours_avatar', None)
            messages.success(request, 'Profile picture removed.')
            return redirect('hours:profile')

        valid_subteams = {v for v, _ in SUBTEAM_CHOICES}
        prof.subteams = ','.join(
            st for st in request.POST.getlist('subteams') if st in valid_subteams
        )

        about = request.POST.get('about', '').strip()
        if len(about) > ABOUT_MAX_LENGTH:
            about = about[:ABOUT_MAX_LENGTH]
        prof.about = about

        # Whatever someone types is what gets stored; leaving it empty is a
        # valid answer and renders as nothing rather than a placeholder.
        prof.pronouns = request.POST.get('pronouns', '').strip()[:PRONOUNS_MAX_LENGTH]

        upload_error = None
        if request.FILES.get('avatar'):
            try:
                prof.avatar_url = _upload_avatar(request.FILES['avatar'])
            except ValueError as exc:
                upload_error = str(exc)

        prof.save(update_fields=['subteams', 'about', 'avatar_url', 'pronouns'])
        request.session.pop('hours_avatar', None)

        if upload_error:
            # The text fields still saved, so say what did and did not work.
            messages.error(request, f'Profile saved, but the picture was not: {upload_error}')
        else:
            messages.success(request, 'Profile updated.')
        return redirect('hours:dashboard')

    selected = prof.get_subteams_list()
    return render(request, 'hours_app/profile.html', {
        'profile': prof,
        'subteam_options': [
            {
                'value': value,
                'label': label,
                'color': SUBTEAM_COLORS.get(value, '#888'),
                'selected': value in selected,
            }
            for value, label in SUBTEAM_CHOICES
        ],
        'selected_subteams': selected,
        'about_max_length': ABOUT_MAX_LENGTH,
        'pronouns_max_length': PRONOUNS_MAX_LENGTH,
        'card': _profile_card(request.user, prof),
    })


@login_required
def acknowledge_badges(request):
    """Mark this user's unseen badges as seen, so the reveal plays only once."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    updated = Badge.objects.filter(user=request.user, seen_at__isnull=True).update(
        seen_at=timezone.now()
    )
    return JsonResponse({'status': 'ok', 'acknowledged': updated})


@login_required
def reset_avatar(request):
    """Clear someone's profile picture. Leads, mentors and admins only."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    if not _may_moderate(request):
        return JsonResponse(
            {'error': 'Permission denied — leads, mentors and admins only'}, status=403
        )

    try:
        data = json.loads(request.body or '{}')
        target_id = int(data['user_id'])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({'error': 'Invalid request body'}, status=400)

    target = get_object_or_404(User, id=target_id)
    target_profile, _ = UserProfile.objects.get_or_create(user=target)
    had_picture = bool(target_profile.avatar_url)
    target_profile.avatar_url = ''
    target_profile.save(update_fields=['avatar_url'])

    logger.info(
        "Avatar reset for user %s by %s", target.username, request.user.username
    )
    return JsonResponse({
        'status': 'ok',
        'had_picture': had_picture,
        'initials': target_profile.initials,
        'name': target.get_full_name() or target.username,
    })


@login_required
def check_in_out(request):
    active_session = CheckInOut.objects.filter(
        user=request.user, check_out_time__isnull=True
    ).first()

    # Between seasons nobody can check in. Checking out stays possible so a
    # session left open by mistake can still be closed.
    season = seasons.current_season()
    if not seasons.is_open(season) and not active_session:
        if request.method == 'POST':
            messages.error(request, f'{season.name} is over. Check-ins open again when the next season starts.')
        return render(request, 'hours_app/check_in_out.html', {
            'active_session': None, 'season_closed': True, 'season': season,
        })

    if request.method == 'POST':
        action = request.POST.get('action')
        reason = request.POST.get('reason', '').strip()[:255]

        if action not in ('check_in', 'check_out'):
            # Without this the request used to fall through and re-render the
            # form unchanged, which looked exactly like a broken button.
            messages.error(
                request,
                'Could not tell whether you meant to check in or out. Please try again.'
            )
        elif action == 'check_in':
            if active_session:
                messages.warning(request, 'You are already checked in.')
            elif not reason or len(reason) < 5:
                messages.error(request, 'Please enter a reason (at least 5 characters).')
            else:
                CheckInOut.objects.create(
                    user=request.user,
                    check_in_time=timezone.now(),
                    reason=reason,
                )
                messages.success(request, 'Checked in successfully.')
                return redirect('hours:dashboard')

        elif action == 'check_out':
            if not active_session:
                messages.error(request, 'You are not currently checked in.')
            else:
                active_session.check_out_time = timezone.now()
                accomplishments = request.POST.get('accomplishments', '').strip()[:1000]
                if accomplishments:
                    active_session.accomplishments = accomplishments
                active_session.save()
                _update_season_rank(request.user)
                messages.success(request, 'Checked out successfully.')
                return redirect('hours:dashboard')

    return render(request, 'hours_app/check_in_out.html', {'active_session': active_session})


def _update_season_rank(user):
    # Current season only: sessions inside its window plus mentor adjustments.
    seasons.update_season_rank(user)


def rankings(request):
    ranks = SeasonRank.objects.select_related('user').order_by('-total_hours')

    all_badges = Badge.objects.select_related('user').all()
    badges_by_user = {}
    for b in all_badges:
        badges_by_user.setdefault(b.user_id, []).append({
            'label': b.get_badge_type_display(),
            'color': BADGE_COLORS.get(b.badge_type, '#888'),
            'svg': b.svg,
        })

    profiles_by_user = {
        p.user_id: p for p in UserProfile.objects.filter(user__in=[r.user_id for r in ranks])
    }

    rankings_list = []
    for r in ranks:
        total = r.total_hours or 0.0
        h = int(total)
        m = int(round((total - h) * 60))
        prof = profiles_by_user.get(r.user_id)
        rankings_list.append({
            'user_id': r.user.id,
            'username': r.user.get_full_name() or r.user.username,
            'hours': h,
            'minutes': m,
            'badges': badges_by_user.get(r.user.id, []),
            'avatar_url': prof.avatar_url if prof else '',
            'initials': prof.initials if prof else (r.user.username or '?')[:2].upper(),
        })
    season = seasons.current_season()
    return render(request, 'hours_app/rankings.html', {
        'rankings': rankings_list,
        'season': season,
        'season_open': seasons.is_open(season),
    })


@login_required
def checked_in_users(request):
    import pytz
    pst = pytz.timezone('America/Los_Angeles')
    today_start = timezone.now().astimezone(pst).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    sessions = CheckInOut.objects.filter(
        check_out_time__isnull=True,
        check_in_time__gte=today_start,
    ).select_related('user')

    all_badges = Badge.objects.filter(
        user__in=[s.user for s in sessions]
    ).select_related('user')
    badges_by_user = {}
    for b in all_badges:
        badges_by_user.setdefault(b.user_id, []).append({
            'type': b.badge_type,
            'label': b.get_badge_type_display(),
            'color': BADGE_COLORS.get(b.badge_type, '#888'),
            'svg': b.svg,
        })

    profiles_by_user = {
        p.user_id: p for p in UserProfile.objects.filter(user__in=[s.user for s in sessions])
    }

    viewer_profile = UserProfile.objects.filter(user=request.user).first()
    viewer_is_privileged = bool(viewer_profile and viewer_profile.is_privileged())

    enriched = []
    for s in sessions:
        user_badge_types = {b['type'] for b in badges_by_user.get(s.user_id, [])}
        prof = profiles_by_user.get(s.user_id)
        enriched.append({
            'session': s,
            'badges': badges_by_user.get(s.user_id, []),
            'badge_types_set': list(user_badge_types),
            'avatar_url': prof.avatar_url if prof else '',
            'initials': prof.initials if prof else (s.user.username or '?')[:2].upper(),
            'about': prof.about if prof else '',
            'pronouns': prof.pronouns if prof else '',
        })

    return render(request, 'hours_app/checked_in.html', {
        'enriched': enriched,
        'viewer_is_privileged': viewer_is_privileged,
        'badge_choices': BADGE_CHOICES,
        'badge_colors': BADGE_COLORS,
        'session_count': len(enriched),
        'badge_meta': BADGE_META,
    })


@login_required
def toggle_badge(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    viewer_profile = UserProfile.objects.filter(user=request.user).first()
    if not viewer_profile or not viewer_profile.is_privileged():
        return JsonResponse({'error': 'Permission denied — leads and mentors only'}, status=403)

    try:
        data = json.loads(request.body)
        target_user_id = int(data['user_id'])
        badge_type = data['badge_type']
    except (KeyError, ValueError, json.JSONDecodeError):
        return JsonResponse({'error': 'Invalid request body'}, status=400)

    valid_types = {v for v, _ in BADGE_CHOICES}
    if badge_type not in valid_types:
        return JsonResponse({'error': 'Invalid badge type'}, status=400)

    target_user = get_object_or_404(User, id=target_user_id)

    badge, created = Badge.objects.get_or_create(
        user=target_user,
        badge_type=badge_type,
        defaults={'awarded_by': request.user},
    )

    if not created:
        badge.delete()
        action = 'removed'
    else:
        action = 'assigned'

    return JsonResponse({
        'status': 'ok',
        'action': action,
        'badge_type': badge_type,
        'badge_label': dict(BADGE_CHOICES).get(badge_type, badge_type),
        'badge_color': BADGE_COLORS.get(badge_type, '#888'),
        # So a pill added live carries the same mark as a server-rendered one.
        'badge_svg': BADGE_META.get(badge_type, {}).get('svg', ''),
    })


@login_required
def user_detail(request, user_id):
    student = get_object_or_404(User, id=user_id)
    season = seasons.current_season()
    logs = seasons.sessions_in(season, CheckInOut.objects.filter(user=student)).order_by('-check_in_time')
    adjustments = (
        seasons.adjustments_in(season, HoursAdjustment.objects.filter(user=student)).select_related('added_by')
        if season else HoursAdjustment.objects.none()
    )
    can_moderate = _may_moderate(request)
    return render(request, 'hours_app/user_detail.html', {
        'student': student,
        'logs': logs,
        'adjustments': adjustments,
        'total': seasons.season_hours(student, season),
        'card': _profile_card(student),
        'can_moderate': can_moderate,
        # Mentors add hours for other people, never for themselves.
        'can_adjust': can_moderate and student.id != request.user.id and seasons.is_open(season) and season is not None,
        'is_self': student.id == request.user.id,
        'season': season,
        'season_open': seasons.is_open(season),
        'today': timezone.localdate().isoformat(),
        'season_start': timezone.localtime(season.started_at).date().isoformat() if season else '',
    })


@login_required
def adjust_hours(request, user_id):
    """A mentor or lead adds (or removes) hours for a member."""
    if request.method != 'POST':
        return redirect('hours:user_detail', user_id=user_id)
    student = get_object_or_404(User, id=user_id)
    if not _may_moderate(request) or student.id == request.user.id:
        messages.error(request, 'Only mentors and leads can add hours, and not for themselves.')
        return redirect('hours:user_detail', user_id=user_id)

    season = seasons.current_season()
    if season is None or not season.is_open:
        messages.error(request, 'Hours can only be added while a season is open.')
        return redirect('hours:user_detail', user_id=user_id)

    try:
        hours = round(float(request.POST.get('hours', '')), 2)
    except ValueError:
        hours = 0
    reason = request.POST.get('reason', '').strip()[:255]
    try:
        work_date = datetime.strptime(request.POST.get('work_date', ''), '%Y-%m-%d').date()
    except ValueError:
        work_date = None

    if not hours or abs(hours) > 24:
        messages.error(request, 'Enter between 0.25 and 24 hours (use a negative number to remove hours).')
    elif len(reason) < 3:
        messages.error(request, 'Add a short reason so the record makes sense later.')
    elif (work_date is None or work_date > timezone.localdate()
          or work_date < timezone.localtime(season.started_at).date()):
        messages.error(request, 'Pick a date in this season, not in the future.')
    else:
        HoursAdjustment.objects.create(
            user=student, season=season, hours=hours,
            work_date=work_date, reason=reason, added_by=request.user,
        )
        _update_season_rank(student)
        name = student.get_full_name() or student.username
        messages.success(request, f"{'Added' if hours > 0 else 'Removed'} {abs(hours):g} h for {name}.")
    return redirect('hours:user_detail', user_id=user_id)


@login_required
def delete_adjustment(request, adjustment_id):
    adjustment = get_object_or_404(HoursAdjustment, id=adjustment_id)
    user_id = adjustment.user_id
    if request.method == 'POST' and _may_moderate(request) and adjustment.season.is_open:
        adjustment.delete()
        _update_season_rank(adjustment.user)
        messages.success(request, 'Hours adjustment removed.')
    else:
        messages.error(request, 'That adjustment cannot be removed.')
    return redirect('hours:user_detail', user_id=user_id)

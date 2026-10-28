import io
import json
import base64
import logging
from datetime import timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib import messages
from django.http import JsonResponse
from django.utils import timezone
from django.db.models import Sum, F, ExpressionWrapper, fields
from django.contrib.auth.models import User

from .models import (
    CheckInOut, SeasonRank, UserProfile, Badge,
    SUBTEAM_CHOICES, SUBTEAM_COLORS, BADGE_CHOICES, BADGE_COLORS, BADGE_META, badge_roster,
)
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

    active_session = CheckInOut.objects.filter(
        user=request.user, check_out_time__isnull=True
    ).first()

    one_week_ago = timezone.now() - timedelta(days=7)
    weekly_stats = CheckInOut.objects.filter(
        user=request.user,
        check_in_time__gte=one_week_ago,
        check_out_time__isnull=False,
    ).annotate(
        duration_calc=ExpressionWrapper(
            F('check_out_time') - F('check_in_time'),
            output_field=fields.DurationField()
        )
    ).aggregate(total=Sum('duration_calc'))

    weekly_hours = 0
    if weekly_stats['total']:
        weekly_hours = round(weekly_stats['total'].total_seconds() / 3600, 1)

    season_rank = SeasonRank.objects.filter(user=request.user).first()
    season_hours = round(season_rank.total_hours, 1) if season_rank else 0

    # Rank position among all ranked users
    ranks_above = SeasonRank.objects.filter(total_hours__gt=season_hours).count()
    total_ranked = SeasonRank.objects.count()
    rank_position = ranks_above + 1 if total_ranked > 0 else None

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

    # Who's currently checked in
    checked_in_now = (
        CheckInOut.objects
        .filter(check_out_time__isnull=True)
        .select_related('user')
        .order_by('check_in_time')
    )
    checked_in_count = checked_in_now.count()
    checked_in_names = [s.user.get_full_name() or s.user.username for s in checked_in_now[:5]]

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
    result = CheckInOut.objects.filter(
        user=user, check_out_time__isnull=False
    ).annotate(
        dur=ExpressionWrapper(
            F('check_out_time') - F('check_in_time'),
            output_field=fields.DurationField()
        )
    ).aggregate(total=Sum('dur'))

    total_hours = 0.0
    if result['total']:
        total_hours = round(result['total'].total_seconds() / 3600, 2)

    SeasonRank.objects.update_or_create(user=user, defaults={'total_hours': total_hours})


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
    return render(request, 'hours_app/rankings.html', {'rankings': rankings_list})


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
    logs = CheckInOut.objects.filter(user=student).order_by('-check_in_time')
    total_seconds = sum(
        (log.check_out_time - log.check_in_time).total_seconds()
        for log in logs
        if log.check_in_time and log.check_out_time
    )
    return render(request, 'hours_app/user_detail.html', {
        'student': student,
        'logs': logs,
        'total': round(total_seconds / 3600, 2),
        'card': _profile_card(student),
        'can_moderate': _may_moderate(request),
        'is_self': student.id == request.user.id,
    })

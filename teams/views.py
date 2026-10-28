import csv
import json
import os
import re
from collections import defaultdict
from urllib.parse import quote
from PIL import Image

import cloudinary
import cloudinary.api
import cloudinary.uploader
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect

from django.db import transaction
from django.db.models import Count
from concurrent.futures import ThreadPoolExecutor
from api.tba import get_teams_list, get_team_events, get_match_videos, get_event_rankings, get_team_avatar
from helpers import login_required, role_required, per_match_values, match_count
from teams.models import Teams, Team_Match_Data, Human_Player_Match
from .forms import NewHumanScoutingData
from django.db.models import Case, When, Value, IntegerField
from utils import config_loader

cloudinary.config(
    cloud_name=os.environ.get("CLOUD_NAME"),
    api_key=os.environ.get("CLOUD_API_KEY"),
    api_secret=os.environ.get("CLOUD_API_SECRET"),
    secure=True)


def home(request):
    robot_picture = None
    try:
        comp_code = request.GET.get('comp', 'testing')
        own_team = Teams.objects.filter(team_number=2073, event=comp_code).first()
        if own_team and own_team.robot_picture:
            url = str(own_team.robot_picture)
            # Only pass external HTTPS URLs (Cloudinary). Skip local dev / IP-only URLs.
            if url.startswith('https://'):
                robot_picture = url
    except Exception:  # nosec B110
        pass
    # Real counts for the landing page's stats bar. It previously showed only
    # static text, so the page said nothing about whether the season was going
    # well - which is most of the reason to open it.
    stats_comp = request.GET.get('comp', 'testing')
    try:
        match_qs = Team_Match_Data.objects.filter(event=stats_comp, is_excluded=False)
        home_stats = {
            'matches': match_qs.count(),
            'scouts': match_qs.values('scout_name').distinct().count(),
            'pit_scouted': Teams.objects.filter(
                event=stats_comp, pit_scout_status=True
            ).count(),
            'comp': stats_comp,
        }
    except Exception:  # nosec B110 - the landing page must render regardless
        home_stats = None

    return render(request, 'home.html', {
        'robot_picture': robot_picture,
        'home_stats': home_stats,
    })


@login_required
def get_events(request):
    return JsonResponse(get_team_events())


@login_required
def team_list_api(request):
    """Lightweight endpoint returning team numbers + names for the global search."""
    comp_code = request.GET.get('comp', 'testing')
    teams = []
    if comp_code == 'testing':
        for t in Teams.objects.filter(event=comp_code).order_by('team_number'):
            teams.append({'number': t.team_number, 'name': ''})
    else:
        for t in get_teams_list(comp_code):
            num = t.get('team_number')
            if num:
                teams.append({'number': num, 'name': t.get('nickname') or t.get('name', '')})
        teams.sort(key=lambda x: x['number'])
    return JsonResponse({'teams': teams})


@login_required
def team_avatars_api(request):
    """Return team avatar URLs for an event. Checks DB cache first, fetches from TBA in parallel for misses."""
    comp_code = request.GET.get('comp', 'testing')
    if comp_code == 'testing':
        return JsonResponse({'avatars': {}})

    config = config_loader.get_config()
    year = config.get('year', 2025)

    team_numbers = [t.get('team_number') for t in get_teams_list(comp_code) if t.get('team_number')]

    cached = {
        row['team_number']: row['logo_url']
        for row in Teams.objects.filter(event=comp_code, team_number__in=team_numbers).values('team_number', 'logo_url')
        if row['logo_url']
    }

    uncached = [n for n in team_numbers if n not in cached]
    result = dict(cached)

    def _fetch(team_num):
        avatar = get_team_avatar(f"frc{team_num}", year)
        if avatar:
            Teams.objects.get_or_create(team_number=team_num, event=comp_code)
            Teams.objects.filter(team_number=team_num, event=comp_code).update(logo_url=avatar)
        return team_num, avatar

    if uncached:
        with ThreadPoolExecutor(max_workers=12) as pool:
            for num, avatar in pool.map(_fetch, uncached[:60]):
                if avatar:
                    result[num] = avatar

    return JsonResponse({'avatars': {str(k): v for k, v in result.items()}})


@login_required
def display_teams(request):
    comp_code = request.GET.get('comp', "testing")
    pit_scouted = set(
        Teams.objects.filter(event=comp_code, pit_scout_status=True)
                     .values_list('team_number', flat=True)
    )

    teams_data = []
    if comp_code == "testing":
        for team in Teams.objects.filter(event=comp_code).order_by('team_number'):
            teams_data.append({'number': team.team_number, 'name': '', 'rank': None})
    else:
        name_map = {}
        for t in get_teams_list(comp_code):
            num = t.get('team_number')
            if num:
                name_map[num] = t.get('nickname') or t.get('name', '')
        rank_map = get_event_rankings(comp_code)
        for num in sorted(name_map.keys()):
            teams_data.append({
                'number': num,
                'name': name_map.get(num, ''),
                'rank': rank_map.get(num),
            })

    # Match counts per team for this event
    match_counts = {
        row['team_number']: row['cnt']
        for row in Team_Match_Data.objects
            .filter(event=comp_code)
            .values('team_number')
            .annotate(cnt=Count('id'))
    }

    # Avatar / logo map — DB cache first, then TBA for misses
    logo_map = {
        row['team_number']: row['logo_url']
        for row in Teams.objects.filter(event=comp_code).values('team_number', 'logo_url')
        if row['logo_url']
    }

    if comp_code != 'testing':
        all_nums = [t['number'] for t in teams_data]
        uncached = [n for n in all_nums if n not in logo_map]
        if uncached:
            year = config_loader.get_config().get('year', 2025)

            def _fetch_avatar(team_num):
                avatar = get_team_avatar(f"frc{team_num}", year)
                if avatar:
                    try:
                        obj, _ = Teams.objects.get_or_create(team_number=team_num, event=comp_code)
                        Teams.objects.filter(pk=obj.pk).update(logo_url=avatar)
                    except Exception:
                        pass
                return team_num, avatar

            with ThreadPoolExecutor(max_workers=12) as pool:
                for num, avatar in pool.map(_fetch_avatar, uncached[:60]):
                    if avatar:
                        logo_map[num] = avatar

    for team in teams_data:
        team['match_count'] = match_counts.get(team['number'], 0)
        team['logo'] = logo_map.get(team['number'])

    total_teams = len(teams_data)
    # Count only teams actually on this event's roster. Pit rows can exist for
    # teams that are not attending (e.g. copied in from another event), and
    # counting those made the progress bar disagree with the green badges,
    # which are only rendered for teams in teams_data.
    listed_numbers = {t['number'] for t in teams_data}
    pit_scouted_count = len(pit_scouted & listed_numbers)

    return render(request, 'teams/view_teams.html', {
        'teams_data': teams_data,
        'pit_scouted': pit_scouted,
        'comp_code': comp_code,
        'pit_scouted_count': pit_scouted_count,
        'total_teams': total_teams,
        'pit_copy_events': known_events(),
    })

def _compute_match_stats(active_matches, metrics):
    """
    Summarise a team's matches for the compare page.

    Double-scouted matches are collapsed to one value per match before
    averaging, so a match two people scouted does not count twice.
    """
    if not active_matches:
        return None
    count = match_count(active_matches)

    dr_vals = per_match_values(
        active_matches,
        lambda m: float(m.driverRanking) if m.driverRanking else None,
    )
    avg_driver = round(sum(dr_vals) / len(dr_vals), 1) if dr_vals else None

    df_vals = per_match_values(
        active_matches,
        lambda m: float(m.defenseRanking) if m.defenseRanking else None,
    )
    avg_defense = round(sum(df_vals) / len(df_vals), 1) if df_vals else None

    # Share of matches where the robot left the starting line. Judged per
    # match: if either scout recorded a leave, the match counts as a leave.
    leave_flags = per_match_values(
        active_matches,
        lambda m: 1.0 if m.autoLeave else 0.0,
    )
    auto_rate = round(sum(1 for v in leave_flags if v > 0) / len(leave_flags) * 100) if leave_flags else 0

    metric_avgs = []
    for metric in metrics:
        key = metric['key']

        def _read(m, k=key):
            try:
                raw = m.data.get(k) if m.data else None
                return None if raw is None else float(raw)
            except (TypeError, ValueError):
                return None

        vals = per_match_values(active_matches, _read)
        if vals:
            metric_avgs.append({
                'key': key,
                'display_name': metric['display_name'],
                'avg': round(sum(vals) / len(vals), 1),
            })

    return {
        'count': count,
        'avg_driver': avg_driver,
        'avg_defense': avg_defense,
        'auto_rate': auto_rate,
        'metric_avgs': metric_avgs,
    }


@login_required
def team_page(request, team_number):
    comp_code = request.GET.get('comp')
    config = config_loader.get_config()

    context = {
        'team_number': team_number,
        'comp_code': comp_code,
        'config_metrics': config.get('metrics', []),
    }

    if comp_code:
        team, created = Teams.objects.get_or_create(team_number=team_number, event=comp_code)
        all_team_match_data = list(Team_Match_Data.objects.filter(
            team_number=team_number,
            event=comp_code
        ).order_by('-match_number'))

        excluded_ids = [m.id for m in all_team_match_data if m.is_excluded]
        active_matches = [m for m in all_team_match_data if not m.is_excluded]

        human_player_records = list(Human_Player_Match.objects.filter(
            team_number=team_number,
            event=comp_code
        ).order_by('match_number'))

        context.update({
            'team': team,
            # Other events this robot was pit-scouted at, so the page can offer
            # to copy that work across instead of re-entering it.
            'pit_copy_sources': pit_source_events(team_number, exclude_event=comp_code),
            'pit_copy_events': known_events(),
            'all_team_match_data': all_team_match_data,
            'excluded_match_ids_json': json.dumps(excluded_ids),
            'excluded_count': len(excluded_ids),
            'match_stats': _compute_match_stats(active_matches, config.get('metrics', [])),
            'human_player_records': human_player_records,
        })
    else:
        context.update({
            'excluded_match_ids_json': '[]',
            'excluded_count': 0,
            'match_stats': None,
        })

    return render(request, 'teams/team_page.html', context)


@login_required
def compare_teams(request):
    comp_code = request.GET.get('comp', 'testing')
    config = config_loader.get_config()
    metrics = config.get('metrics', [])
    pit_questions = config.get('pit_questions', [])

    raw_nums = request.GET.get('teams', '')
    team_numbers = []
    for part in raw_nums.split(','):
        part = part.strip()
        if part.isdigit():
            team_numbers.append(int(part))
    team_numbers = team_numbers[:4]  # cap at 4

    teams_data = []
    for num in team_numbers:
        matches = list(
            Team_Match_Data.objects
            .filter(team_number=num, event=comp_code, is_excluded=False)
            .order_by('match_number')
        )
        stats = _compute_match_stats(matches, metrics)
        team_obj = Teams.objects.filter(team_number=num, event=comp_code).first()
        pit_data = team_obj.pit_data if team_obj else {}

        # Pit answers are rendered through the config so the rows carry the
        # question wording the scouts actually saw, in the order they were
        # asked, instead of raw JSON keys in whatever order they were stored.
        pit_rows = []
        for question in pit_questions:
            value = pit_data.get(question['key'])
            if value in (None, '', [], {}):
                continue
            if isinstance(value, list):
                value = ', '.join(str(v) for v in value)
            pit_rows.append({
                'label': question.get('display_name') or question.get('label') or question['key'],
                'value': value,
            })
        # Anything stored under a key the current config no longer asks about
        # is still worth showing rather than silently dropping.
        known = {q['key'] for q in pit_questions}
        for key, value in (pit_data or {}).items():
            if key in known or value in (None, '', [], {}):
                continue
            if isinstance(value, list):
                value = ', '.join(str(v) for v in value)
            pit_rows.append({'label': key.replace('_', ' ').title(), 'value': value})

        teams_data.append({
            'number': num,
            'stats': stats,
            'pit_data': pit_data,
            'pit_rows': pit_rows,
            'notes': (team_obj.notes or '').strip() if team_obj else '',
            'robot_picture': str(team_obj.robot_picture) if team_obj and team_obj.robot_picture else None,
        })

    return render(request, 'teams/compare_teams.html', {
        'teams_data': teams_data,
        'comp_code': comp_code,
        'config_metrics': metrics,
        'team_numbers_raw': raw_nums,
    })


_SAFE_COMP_CODE = re.compile(r'^[A-Za-z0-9_\-]{1,20}$')

@login_required
def pit_scouting(request, team_number):
    comp_code = request.GET.get('comp')
    if not comp_code or not _SAFE_COMP_CODE.match(comp_code):
        return HttpResponse('Competition code required. Add ?comp=EVENT_CODE to URL', status=400)
    
    config = config_loader.get_config()
    team, created = Teams.objects.get_or_create(team_number=team_number, event=comp_code)
    
    if request.method == 'POST':
        # Handle image upload (Anchor field)
        img_url = None
        logo_url = None
        if 'robot_picture' in request.FILES:
            image_file = request.FILES['robot_picture']
            try:
                img = Image.open(image_file)
                img.verify()
                if img.format not in ('PNG', 'JPEG'):
                    raise ValueError("Unsupported format")
                image_file.seek(0)
            except Exception:
                return JsonResponse({
                    'status': 'error',
                    'message': 'Invalid file type. Only PNG and JPEG images are allowed.'
                }, status=400)

            image_response = cloudinary.uploader.upload(image_file)
            img_url = image_response["secure_url"]
            image_url_list = img_url.split("upload/")
            image_url_list.insert(1, "upload/w_0.4,c_scale/")
            img_url = "".join(image_url_list)

        # Optional team logo upload
        if 'team_logo' in request.FILES:
            logo_file = request.FILES['team_logo']
            try:
                img = Image.open(logo_file)
                img.verify()
                if img.format not in ('PNG', 'JPEG'):
                    raise ValueError("Unsupported format")
                logo_file.seek(0)
            except Exception:
                return JsonResponse({
                    'status': 'error',
                    'message': 'Invalid logo file type. Only PNG and JPEG images are allowed.'
                }, status=400)

            logo_response = cloudinary.uploader.upload(logo_file)
            logo_url = logo_response["secure_url"]
            # small display size
            logo_url_list = logo_url.split("upload/")
            logo_url_list.insert(1, "upload/w_0.4,c_scale/")
            logo_url = "".join(logo_url_list)

        # Handle dynamic pit data
        pit_payload = {}
        validation_errors = []
        
        for q in config.get('pit_questions', []):
            key = q['key']
            if q['type'] == 'multiselect':
                pit_payload[key] = request.POST.getlist(key)
            elif q['type'] == 'boolean':
                pit_payload[key] = request.POST.get(key) == 'on'
            else:
                pit_payload[key] = request.POST.get(key, '')
            
            # Validate number fields with min/max
            if q['type'] == 'number' and pit_payload[key]:
                try:
                    val = float(pit_payload[key])
                    if 'min' in q and val < q['min']:
                        validation_errors.append(f"{q.get('display_name', key)} must be at least {q['min']}")
                    if 'max' in q and val > q['max']:
                        validation_errors.append(f"{q.get('display_name', key)} cannot exceed {q['max']}")
                except (ValueError, TypeError):
                    validation_errors.append(f"{q.get('display_name', key)} must be a valid number")

        # Server-side required-field validation
        field_errors = {}
        for q in config.get('pit_questions', []):
            if q.get('required'):
                val = pit_payload.get(q['key'])
                if q['type'] == 'multiselect':
                    if not val:
                        field_errors[q['key']] = f"{q.get('display_name', q['key'])} is required."
                elif q['type'] in ['select', 'text', 'textarea', 'number']:
                    if val in [None, '', []]:
                        field_errors[q['key']] = f"{q.get('display_name', q['key'])} is required."
        
        # Merge validation errors
        if validation_errors:
            for err in validation_errors:
                field_errors['general'] = field_errors.get('general', []) if isinstance(field_errors.get('general'), list) else []
                field_errors['general'].append(err)

        if field_errors:
            # Return form with errors and previously entered values
            return render(request, "teams/pit_scouting.html", {
                'team_number': team_number,
                'questions': config.get('pit_questions', []),
                'existing_data': pit_payload,
                'field_errors': field_errors,
                'comp_code': comp_code,
                'has_robot_picture': bool(team.robot_picture),
            })
        
        # Save
        team.pit_data = pit_payload
        if img_url:
            team.robot_picture = img_url
        if logo_url:
            team.logo_url = logo_url
        team.pit_scout_status = True
        team.save()
        return redirect(f'/teams/{team_number}/?comp={comp_code}')

    # Ensure existing_data is a dict (some DB rows may have JSON saved as string)
    existing_data = team.pit_data or {}
    if isinstance(existing_data, str):
        try:
            import json as _json
            existing_data = _json.loads(existing_data)
        except Exception:
            existing_data = {}

    return render(request, "teams/pit_scouting.html", {
        'team_number': team_number,
        'questions': config.get('pit_questions', []),
        'existing_data': existing_data,
        'has_robot_picture': bool(team.robot_picture),
        'comp_code': comp_code,
    })
    
@login_required
def clear_pit_data(request, team_number):
    if request.method == 'POST':
        comp_code = request.POST.get('comp_code')
        if comp_code and not _SAFE_COMP_CODE.match(comp_code):
            comp_code = None
        if comp_code:
            team = Teams.objects.filter(team_number=team_number, event=comp_code).first()
            if team:
                team.pit_data = {}
                team.pit_scout_status = False
                team.robot_picture = None
                team.save()
        if comp_code:
            return redirect(f'/teams/{team_number}/?comp={comp_code}')
        return redirect(f'/teams/{team_number}/')
    return redirect(f'/teams/{team_number}/')

@login_required
def human_player_submit(request, team_number):
    comp_code = request.GET.get('comp')
    if request.method == 'POST':
        form = NewHumanScoutingData(request.POST)
        if form.is_valid():
            Human_Player_Match.objects.create(team_number=team_number,
                                              event=comp_code,
                                              match_number=form.cleaned_data.get('match_number'),
                                              human_player_comment=form.cleaned_data.get('human_player_comment'))

            redirect_url = f'/teams/{team_number}/'
            if comp_code:
                redirect_url += f'?comp={comp_code}'
            return redirect(redirect_url)
    else:
        form = NewHumanScoutingData()
    return render(request, "teams/human_player_scout.html", {'form': form, 'team_number': team_number, 'comp_code': comp_code})



@login_required
def save_team_notes(request, team_number):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    comp_code = request.POST.get('comp_code', '').strip()
    if not comp_code or not _SAFE_COMP_CODE.match(comp_code):
        return JsonResponse({'error': 'Invalid comp code'}, status=400)
    notes = request.POST.get('notes', '')[:5000]
    team, _ = Teams.objects.get_or_create(team_number=team_number, event=comp_code)
    team.notes = notes
    team.save(update_fields=['notes'])
    return JsonResponse({'status': 'ok'})


@login_required
def tba_videos(request):
    comp = request.GET.get('comp', '')
    if not comp or not re.match(r'^[A-Za-z0-9_\-]{1,20}$', comp):
        return JsonResponse({})
    return JsonResponse(get_match_videos(comp))


# ---------------------------------------------------------------------------
# Reusing pit data across competitions
#
# A robot is the same machine at every event it attends, so pit answers
# (drivetrain, mechanisms, photos) stay valid all season. Teams rows are keyed
# per (team_number, event), so reusing that work means copying the pit fields
# onto the row for the other event.
# ---------------------------------------------------------------------------

# Fields that describe the robot itself and therefore travel between events.
# `notes` is deliberately excluded - those are per-competition strategy notes.
PIT_COPY_FIELDS = ('pit_data', 'robot_picture', 'logo_url', 'pit_scout_status')


def _copy_pit_fields(source, target):
    """Copy the robot-describing fields from one Teams row onto another."""
    for field in PIT_COPY_FIELDS:
        setattr(target, field, getattr(source, field))
    target.save(update_fields=list(PIT_COPY_FIELDS))


def known_events():
    """Every event code we know of, as [{'code', 'name'}], sorted by code."""
    try:
        names = get_team_events() or {}
    except Exception:  # nosec B110 - TBA lookup is a nicety, not required
        names = {}
    codes = set(Teams.objects.values_list('event', flat=True).distinct())
    codes.update(names.keys())
    return [{'code': c, 'name': names.get(c, c)} for c in sorted(codes) if c]


def pit_source_events(team_number, exclude_event=None):
    """Event codes where this team already has pit data recorded."""
    qs = Teams.objects.filter(team_number=team_number, pit_scout_status=True)
    if exclude_event:
        qs = qs.exclude(event=exclude_event)
    return sorted(qs.values_list('event', flat=True))


@login_required
def copy_pit_data(request, team_number):
    """Copy one team's pit data between two competitions.

    Works in both directions: the team page offers "copy to" when the current
    event already has data, and "copy from" when it does not.
    """
    if request.method != 'POST':
        return redirect(f'/teams/{team_number}/')

    source_comp = request.POST.get('source_comp') or ''
    target_comp = request.POST.get('target_comp') or ''
    overwrite = request.POST.get('overwrite') == 'on'
    return_comp = request.POST.get('comp_code') or ''

    def _back(message, ok=False):
        url = f'/teams/{team_number}/'
        sep = '?'
        if return_comp and _SAFE_COMP_CODE.match(return_comp):
            url += f'?comp={return_comp}'
            sep = '&'
        return redirect(f'{url}{sep}pit_copy={"ok" if ok else "err"}&pit_msg={quote(message)}')

    if not _SAFE_COMP_CODE.match(source_comp) or not _SAFE_COMP_CODE.match(target_comp):
        return _back('Invalid competition code.')
    if source_comp == target_comp:
        return _back('Source and destination competitions are the same.')

    source = Teams.objects.filter(team_number=team_number, event=source_comp).first()
    if source is None or not source.pit_scout_status:
        return _back(f'No pit data recorded for team {team_number} at {source_comp}.')

    target, _created = Teams.objects.get_or_create(team_number=team_number, event=target_comp)
    if target.pit_scout_status and not overwrite:
        return _back(
            f'{target_comp} already has pit data for team {team_number}. '
            'Tick "overwrite" to replace it.'
        )

    _copy_pit_fields(source, target)
    return _back(f'Copied pit data for team {team_number}: {source_comp} to {target_comp}.', ok=True)


@login_required
def bulk_copy_pit_data(request):
    """Copy pit data from one competition to another.

    Copies every scouted team by default, or a single team when `team_number`
    is supplied. Teams that already have pit data at the destination are
    skipped unless `overwrite` is set, so re-running this is safe and will not
    clobber work done at the destination event.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required.'}, status=405)

    source_comp = request.POST.get('source_comp') or ''
    target_comp = request.POST.get('target_comp') or ''
    overwrite = request.POST.get('overwrite') in ('on', 'true', '1')
    team_number_raw = (request.POST.get('team_number') or '').strip()

    if not _SAFE_COMP_CODE.match(source_comp) or not _SAFE_COMP_CODE.match(target_comp):
        return JsonResponse({'status': 'error', 'message': 'Invalid competition code.'}, status=400)
    if source_comp == target_comp:
        return JsonResponse(
            {'status': 'error', 'message': 'Source and destination competitions are the same.'},
            status=400,
        )

    sources = Teams.objects.filter(event=source_comp, pit_scout_status=True).order_by('team_number')

    # Optional single-team mode: same endpoint, narrowed to one robot.
    if team_number_raw:
        if not team_number_raw.isdigit():
            return JsonResponse(
                {'status': 'error', 'message': 'Team number must be digits only.'}, status=400
            )
        sources = sources.filter(team_number=int(team_number_raw))
        if not sources.exists():
            return JsonResponse({
                'status': 'error',
                'message': f'Team {team_number_raw} has no pit data at {source_comp}.',
            }, status=404)

    # Only copy to teams actually attending the destination event. Without this
    # the copy creates rows for robots that are not at that competition: they
    # never appear in the teams list, but they did inflate the pit-scouted
    # count, so the progress bar disagreed with the green badges.
    attending = set()
    try:
        attending = {
            t.get('team_number') for t in (get_teams_list(target_comp) or [])
            if t.get('team_number')
        }
    except Exception:  # nosec B110 - TBA is best-effort; fall back to copying all
        attending = set()

    copied, skipped, not_attending = [], [], []

    with transaction.atomic():
        for source in sources:
            if attending and source.team_number not in attending:
                not_attending.append(source.team_number)
                continue
            target, _created = Teams.objects.get_or_create(
                team_number=source.team_number, event=target_comp
            )
            if target.pit_scout_status and not overwrite:
                skipped.append(source.team_number)
                continue
            _copy_pit_fields(source, target)
            copied.append(source.team_number)

    message = f'Copied {len(copied)} team(s) from {source_comp} to {target_comp}.'
    if skipped:
        message += (
            f' Skipped {len(skipped)} that already had pit data'
            ' (tick overwrite to replace).'
        )
    if not_attending:
        message += f' {len(not_attending)} not competing at {target_comp}.'
    if not copied and not skipped and not not_attending:
        message = f'No pit data found at {source_comp} to copy.'

    return JsonResponse({
        'status': 'ok',
        'copied': copied,
        'skipped': skipped,
        'not_attending': not_attending,
        'copied_count': len(copied),
        'skipped_count': len(skipped),
        'not_attending_count': len(not_attending),
        'source': source_comp,
        'target': target_comp,
        'message': message,
    })


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

# Anchor columns always emitted, in this order. Dynamic keys from the `data`
# JSON bucket are appended after these.
CSV_ANCHOR_FIELDS = (
    'team_number', 'match_number', 'quantifier', 'scout_name', 'start_pos',
    'driverRanking', 'defenseRanking', 'autoLeave',
    'is_broken', 'is_disabled', 'is_tipped', 'is_excluded', 'comment',
)


def _spreadsheet_safe_cell(value):
    if isinstance(value, str) and value.lstrip(' \t\r\n\ufeff').startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


@login_required
def export_matches_csv(request):
    """Download this competition's match data as a CSV.

    Alliance selection happens away from a browser, so the raw rows need to be
    openable in a spreadsheet. Dynamic keys inside each record's JSON bucket
    become their own columns, using the union of keys across all rows so that
    records scouted under different game configs still line up.
    """
    comp_code = request.GET.get('comp', '')
    if not _SAFE_COMP_CODE.match(comp_code):
        return HttpResponse('Competition code required. Add ?comp=EVENT_CODE', status=400)

    quantifier = request.GET.get('quantifier') or ''
    include_excluded = request.GET.get('include_excluded') == '1'

    rows = Team_Match_Data.objects.filter(event=comp_code)
    if quantifier:
        rows = rows.filter(quantifier=quantifier)
    if not include_excluded:
        rows = rows.filter(is_excluded=False)
    rows = rows.order_by('team_number', 'match_number', 'scout_name')

    records = list(rows)

    # Union of dynamic keys, so a column exists even if only some rows have it.
    dynamic_keys = set()
    for r in records:
        if isinstance(r.data, dict):
            dynamic_keys.update(r.data.keys())
    dynamic_cols = sorted(dynamic_keys)

    filename = f'{comp_code}_matches'
    if quantifier:
        filename += f'_{quantifier}'
    filename += '.csv'

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow(_spreadsheet_safe_cell(value) for value in (
        ['event'] + list(CSV_ANCHOR_FIELDS) + dynamic_cols
    ))

    for r in records:
        bucket = r.data if isinstance(r.data, dict) else {}
        row = [comp_code]
        for field in CSV_ANCHOR_FIELDS:
            row.append(getattr(r, field, ''))
        for key in dynamic_cols:
            value = bucket.get(key, '')
            # Lists would otherwise render as "['a', 'b']" and break parsing.
            if isinstance(value, (list, tuple)):
                value = '; '.join(str(v) for v in value)
            elif isinstance(value, dict):
                value = json.dumps(value)
            row.append(value)
        writer.writerow(_spreadsheet_safe_cell(value) for value in row)

    return response


# ---------------------------------------------------------------------------
# Scout accuracy
#
# When two people scout the same match we get free ground truth: wherever they
# disagree, at least one of them is wrong. Aggregating that tells you which
# scouts to trust during alliance selection and who needs coaching.
# ---------------------------------------------------------------------------

# Anchor ratings compared directly. Values are small integers, so exact
# agreement is a fair test.
ACCURACY_ANCHOR_FIELDS = (
    'driverRanking', 'defenseRanking', 'autoLeave', 'start_pos',
    'is_broken', 'is_disabled', 'is_tipped',
)


def _comparable_values(record):
    """Flatten a record into {field: value} for fields worth comparing."""
    values = {}
    for field in ACCURACY_ANCHOR_FIELDS:
        value = getattr(record, field, None)
        if value is not None:
            values[field] = value
    bucket = record.data if isinstance(record.data, dict) else {}
    for key, value in bucket.items():
        # Only compare scalars; free-text and nested structures are noise here.
        if isinstance(value, bool) or isinstance(value, (int, float)):
            values[f'data.{key}'] = value
    return values


def compute_scout_accuracy(comp_code, quantifier=None):
    """Return (per-scout stats, disagreement list) for double-scouted matches."""
    rows = Team_Match_Data.objects.filter(event=comp_code, is_excluded=False)
    if quantifier:
        rows = rows.filter(quantifier=quantifier)

    groups = defaultdict(list)
    per_scout_matches = defaultdict(int)
    for record in rows:
        groups[(record.team_number, record.match_number, record.quantifier)].append(record)
        per_scout_matches[record.scout_name] += 1

    stats = defaultdict(lambda: {'compared': 0, 'agreed': 0, 'overlaps': 0})
    disagreements = []

    for (team_number, match_number, quant), records in groups.items():
        if len(records) < 2:
            continue
        flattened = {r.scout_name: _comparable_values(r) for r in records}
        for scout in flattened:
            stats[scout]['overlaps'] += 1

        # Compare every pair once.
        names = sorted(flattened)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                shared = set(flattened[a]) & set(flattened[b])
                for field in shared:
                    va, vb = flattened[a][field], flattened[b][field]
                    stats[a]['compared'] += 1
                    stats[b]['compared'] += 1
                    if va == vb:
                        stats[a]['agreed'] += 1
                        stats[b]['agreed'] += 1
                    else:
                        disagreements.append({
                            'team_number': team_number,
                            'match_number': match_number,
                            'quantifier': quant,
                            'field': field,
                            'scout_a': a, 'value_a': va,
                            'scout_b': b, 'value_b': vb,
                        })

    # Iterate every scout, not just those with overlap: a scout whose matches
    # were never double-covered still belongs in the table, shown as "no
    # overlap", because that is exactly who needs verification coverage.
    table = []
    for scout in per_scout_matches:
        st = stats.get(scout, {'compared': 0, 'agreed': 0, 'overlaps': 0})
        rate = round(100.0 * st['agreed'] / st['compared'], 1) if st['compared'] else None
        table.append({
            'scout_name': scout,
            'matches': per_scout_matches[scout],
            'overlaps': st['overlaps'],
            'compared': st['compared'],
            'agreed': st['agreed'],
            'agreement': rate,
        })
    # Least reliable first - that is who needs attention.
    table.sort(key=lambda r: (r['agreement'] is None, r['agreement'] or 0, -r['compared']))

    disagreements.sort(key=lambda d: (d['team_number'], d['match_number'], d['field']))
    return table, disagreements


@login_required
def scout_accuracy(request):
    """Page showing how often each scout agrees with others on the same match."""
    comp_code = request.GET.get('comp', '')
    if not _SAFE_COMP_CODE.match(comp_code):
        return HttpResponse('Competition code required. Add ?comp=EVENT_CODE', status=400)

    quantifier = request.GET.get('quantifier') or ''
    table, disagreements = compute_scout_accuracy(comp_code, quantifier or None)

    scouts_with_overlap = [r for r in table if r['compared']]
    overall = None
    if scouts_with_overlap:
        total_compared = sum(r['compared'] for r in scouts_with_overlap)
        total_agreed = sum(r['agreed'] for r in scouts_with_overlap)
        if total_compared:
            overall = round(100.0 * total_agreed / total_compared, 1)

    return render(request, 'teams/scout_accuracy.html', {
        'comp_code': comp_code,
        'quantifier': quantifier,
        'scout_table': table,
        'disagreements': disagreements[:200],
        'disagreement_total': len(disagreements),
        'overall_agreement': overall,
        'has_overlap': bool(scouts_with_overlap),
    })

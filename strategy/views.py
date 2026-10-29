import json
import logging
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)
from pathlib import Path
from time import time

from django.http import JsonResponse, HttpResponse
from django.shortcuts import render

from api.tba import get_single_match, get_teams_list, get_team_avatar, get_match_schedule, get_event_rankings
from teams import models
from helpers import login_required, team_member_required, per_match_values, match_count
from django.shortcuts import render, redirect
from strategy.models import PickList_Data
from teams.models import Team_Match_Data, Teams
from utils import config_loader

_SAFE_COMP_CODE = re.compile(r'^[A-Za-z0-9_\-]{1,20}$')

def get_json_path(comp_code):
    if not comp_code or not _SAFE_COMP_CODE.match(comp_code):
        return None
    # Get the project root directory
    BASE_DIR = Path(__file__).resolve().parent.parent
    # Create a picklists directory if it doesn't exist
    picklist_dir = BASE_DIR / 'picklists'
    try:
        picklist_dir.mkdir(exist_ok=True)
    except OSError:
        pass  # read-only host: reads still work, writes fall back to the database
    return picklist_dir / f'picklist_{comp_code}.json'

def _read_picklist_file(comp_code):
    json_path = get_json_path(comp_code)
    if json_path is None:
        return None
    if json_path.exists():
        with open(json_path, 'r') as f:
            data = json.load(f)
            # Handle both old and new format
            if isinstance(data, list):
                # Convert old format to new format
                return {
                    'timestamp': int(time() * 1000),
                    'data': data
                }
            return data
    return None


def _read_picklist_db(comp_code):
    """The saved board as a payload ([no, 1st, 2nd, 3rd, dnp, 4th]), if it has been saved."""
    row = PickList_Data.objects.filter(event=comp_code).first()
    if row is None or not row.updated_ms:
        return None
    def as_list(value):
        return value if isinstance(value, list) else []
    return {
        'timestamp': row.updated_ms,
        'data': [as_list(row.no_pick), as_list(row.first_pick), as_list(row.second_pick),
                 as_list(row.third_pick), as_list(row.dn_pick), as_list(row.fourth_pick)],
    }


def read_json_picklist(comp_code):
    """Latest board: the JSON file or the database row, whichever was saved last.

    The file is the fast path for live sync. On a host with a read-only disk
    it can only ever be a stale copy shipped with the code, so the database
    wins whenever it is newer."""
    if get_json_path(comp_code) is None:
        return None
    from_file = None
    try:
        from_file = _read_picklist_file(comp_code)
    except (OSError, ValueError):
        pass
    from_db = None
    try:
        from_db = _read_picklist_db(comp_code)
    except Exception:
        logger.exception("could not read the picklist from the database")
    if from_file and from_db:
        return from_db if from_db['timestamp'] > from_file['timestamp'] else from_file
    return from_file or from_db

def write_json_picklist(comp_code, data):
    """Save the board and return its timestamp (ms).

    Always written to the database (that is what works on every host), and to
    the JSON file as well when the disk is writable."""
    json_path = get_json_path(comp_code)
    if json_path is None:
        raise ValueError("Invalid competition code")
    stamp = int(time() * 1000)
    padded = list(data) + [[]] * (6 - len(data))
    PickList_Data.objects.get_or_create(event=comp_code)
    PickList_Data.objects.filter(event=comp_code).update(
        no_pick=padded[0], first_pick=padded[1], second_pick=padded[2],
        third_pick=padded[3], dn_pick=padded[4], fourth_pick=padded[5],
        updated_ms=stamp,
    )
    try:
        with open(json_path, 'w') as f:
            json.dump({'timestamp': stamp, 'data': data}, f)
    except OSError:
        pass  # read-only disk: the database copy above is the live one
    return stamp

@login_required
def rankings(request):
    comp_code = request.GET.get('comp')
    quantifier = request.GET.get('quantifier', 'Quals')
    exclude_zeros = request.GET.get('exclude_zeros', 'true') == 'true'
    min_matches = int(request.GET.get('min_matches', '0'))
    config = config_loader.get_config()

    VALID_SORT_FIELDS = {m['key'] for m in config.get('metrics', [])}
    # Add computed and anchor fields
    VALID_SORT_FIELDS |= {'totalShooting', 'totalPass', 'totalTotal', 'match_count',
                          'driverRanking', 'defenseRanking', 'autoLeave'}
    sort_by = request.GET.get('sort_by', 'totalShooting')
    if sort_by not in VALID_SORT_FIELDS:
        sort_by = 'totalShooting'

    # Build sort options from config rankings columns + computed fields
    SORT_OPTIONS = []
    for col in config.get('rankings', []):
        if col.get('sortable') and col['key'] != 'team_number':
            SORT_OPTIONS.append((col['key'], col['display_name']))
    SORT_OPTIONS.append(('match_count', 'Matches Played'))

    # Get excluded match IDs from session
    excluded_ids = set()  # Now handled by is_excluded field in DB
    logger.warning(
        "RANKINGS user=%s",
        request.session.get('email', '?'),
    )

    # --- Pit scouting attribute filters ---
    pit_questions = config.get('pit_questions', [])
    # Deduplicate by key (config has some dupes)
    seen_keys = set()
    filterable_pit = []
    for q in pit_questions:
        if q['type'] in ('select', 'multiselect') and q['key'] not in seen_keys:
            seen_keys.add(q['key'])
            filterable_pit.append(q)

    # Collect active pit filters from query params (pit__<key>=value)
    active_pit_filters = {}
    for q in filterable_pit:
        vals = request.GET.getlist('pit__' + q['key'])
        if vals:
            active_pit_filters[q['key']] = vals

    # Build a set of team numbers that pass pit filters
    pit_filtered_teams = None
    if active_pit_filters:
        pit_filtered_teams = set()
        for team in models.Teams.objects.filter(event=comp_code, pit_scout_status=True):
            pit_data = team.pit_data or {}
            passes = True
            for key, required_vals in active_pit_filters.items():
                team_val = pit_data.get(key)
                if isinstance(team_val, list):
                    if not any(rv in team_val for rv in required_vals):
                        passes = False
                        break
                else:
                    if str(team_val) not in required_vals:
                        passes = False
                        break
            if passes:
                pit_filtered_teams.add(team.team_number)

    teams = models.Teams.objects.filter(event=comp_code).order_by("team_number")
    team_averages = {}

    # Bulk-fetch ALL match data for this event/quantifier in ONE query
    all_matches = list(models.Team_Match_Data.objects.filter(
        event=comp_code, quantifier=quantifier, is_excluded=False
    ))

    # Group by team number
    matches_by_team = defaultdict(list)
    for m in all_matches:
        matches_by_team[m.team_number].append(m)

    for team in teams:
        if pit_filtered_teams is not None and team.team_number not in pit_filtered_teams:
            continue
        team_matches = matches_by_team.get(team.team_number)
        if not team_matches:
            continue
        stats = _compute_team_averages(team_matches, config, exclude_zeros)
        if not stats or stats.get('match_count', 0) < min_matches:
            continue
        stats['totalPass']     = round((stats.get('autoPass', 0) or 0) + (stats.get('telePass', 0) or 0), 3)
        stats['totalShooting'] = round((stats.get('autoScore', 0) or 0) + (stats.get('teleScore', 0) or 0), 3)
        stats['totalTotal']    = round((stats.get('totalShooting', 0) or 0) + (stats.get('totalPass', 0) or 0), 3)
        team_averages[team.team_number] = stats

    team_averages = dict(sorted(team_averages.items(), key=lambda x: x[1].get(sort_by, 0), reverse=True))

    return render(request, "strategy/rankings.html", {
        'team_averages': team_averages,
        'comp_code': comp_code,
        'selected_quantifier': quantifier,
        'config_metrics': config.get('metrics', []),
        'rankings_columns': config.get('rankings', []),
        'sort_by': sort_by,
        'sort_options': SORT_OPTIONS,
        'exclude_zeros': exclude_zeros,
        'min_matches': min_matches,
        'filterable_pit': filterable_pit,
        'active_pit_filters': active_pit_filters,
        'excluded_count': models.Team_Match_Data.objects.filter(event=comp_code, is_excluded=True).count(),
    })

@login_required
@team_member_required
def picklist(request):
    comp_code = request.GET.get('comp')
    teams = []
    no_pick_teams = []
    first_pick_teams = []
    second_pick_teams = []
    third_pick_teams = []
    fourth_pick_teams = []
    dn_pick_teams = []
    if not comp_code or comp_code.lower() == 'testing':
        return render(request, "strategy/picklist.html", {
            'teams': teams,
            'team_info': {},
        })
    else:
        # Read JSON timestamp so JS won't overwrite server-rendered data on first load
        json_data = read_json_picklist(comp_code)
        json_timestamp = json_data['timestamp'] if json_data else 0

        # Build team_info: {team_number: {"nickname": ..., "logo_url": ...}}
        teams_data = get_teams_list(comp_code)
        year = config_loader.get_config().get('year', 2025)

        # Bulk-fetch all cached logos in one query
        cached_logos = {
            row['team_number']: row['logo_url']
            for row in Teams.objects.filter(event=comp_code).values('team_number', 'logo_url')
        }

        team_info = {}
        uncached_numbers = []
        for team in teams_data:
            number = team.get("team_number")
            if number is None:
                continue
            nickname = team.get("nickname") or f"Team {number}"
            logo_url = cached_logos.get(number) or None
            # Always add to team_info regardless of logo status
            team_info[number] = {
                "nickname": nickname,
                "logo_url": logo_url or None,
            }
            if not logo_url:
                uncached_numbers.append(number)

        # Fetch avatars for all uncached teams in parallel (same pattern as
        # teams/views.py's display_teams/team_avatars_api), then batch the DB writes.
        if uncached_numbers:
            def _fetch_avatar(team_num):
                return team_num, get_team_avatar(f"frc{team_num}", year)

            with ThreadPoolExecutor(max_workers=12) as pool:
                for num, avatar in pool.map(_fetch_avatar, uncached_numbers):
                    if avatar:
                        team_info[num]["logo_url"] = avatar
                        try:
                            Teams.objects.filter(team_number=num, event=comp_code).update(logo_url=avatar)
                        except Exception:
                            pass

        # Attach scouting stats so a strategist can compare teams on the board
        # itself. Without these, judging any team means opening its modal one at
        # a time, which is the main thing a spreadsheet does better.
        # Per-team averages for the board. Computed through
        # _compute_team_averages rather than a database Avg() so that a match
        # scouted by two people is collapsed to one value first — the aggregate
        # this replaced counted such matches twice.
        all_quals = list(
            models.Team_Match_Data.objects
            .filter(event=comp_code, is_excluded=False, quantifier='Quals')
        )
        by_team = defaultdict(list)
        for record in all_quals:
            by_team[record.team_number].append(record)

        metric_keys = [m['key'] for m in config_loader.get_config().get('metrics', [])]
        for number, records in by_team.items():
            entry = team_info.get(number)
            if entry is None:
                continue
            averages = _compute_team_averages(records, config_loader.get_config())
            if not averages:
                continue
            entry['matches'] = averages.get('match_count', 0)
            entry['driver'] = averages.get('driverRanking') or None
            entry['defense'] = averages.get('defenseRanking') or None
            # Sort keys for the column sort control.
            entry['metrics'] = {k: averages.get(k, 0) for k in metric_keys}

        # Pit attributes drive the filter chips, so each card carries the
        # answers to the select/multiselect pit questions. Free-text and number
        # answers are left out — they are not filterable and would only bloat
        # the payload sent to every tablet.
        filterable_keys = [
            q['key'] for q in config_loader.get_config().get('pit_questions', [])
            if q.get('type') in ('select', 'multiselect')
        ]
        pit_rows = Teams.objects.filter(event=comp_code).values('team_number', 'pit_data')
        for row in pit_rows:
            entry = team_info.get(row['team_number'])
            if entry is None:
                continue
            pit_data = row.get('pit_data') or {}
            entry['pit'] = {k: pit_data.get(k) for k in filterable_keys if pit_data.get(k)}

        # Event rank from TBA, shown as a chip on each card.
        try:
            for number, rank in (get_event_rankings(comp_code) or {}).items():
                if number in team_info:
                    team_info[number]['rank'] = rank
        except Exception:  # nosec B110 - rankings are a nicety
            pass

        # Teams with no qualification data at all still need the keys present so
        # the template can render a consistent "no data" chip.
        for entry in team_info.values():
            entry.setdefault('matches', 0)
            entry.setdefault('driver', None)
            entry.setdefault('defense', None)
            entry.setdefault('rank', None)
            entry.setdefault('metrics', {})
            entry.setdefault('pit', {})

        # Filter chips are generated from the game config, so a new season's
        # pit questions produce new filters without touching this view.
        filter_groups = [
            {
                'key': q['key'],
                'label': q.get('display_name') or q.get('label') or q['key'],
                'options': q.get('options', []),
            }
            for q in config_loader.get_config().get('pit_questions', [])
            if q.get('type') in ('select', 'multiselect') and q.get('options')
        ]
        sort_metrics = [
            {'key': m['key'], 'label': m.get('display_name') or m['key']}
            for m in config_loader.get_config().get('metrics', [])
        ]
        picklist_flags = read_picklist_flags(comp_code)

        # Helper: remove already-categorised teams from the pool and surface any new
        # TBA teams not yet in any list (handles teams added after picklist was saved).
        def _uncategorised(all_list_teams):
            categorized = set()
            for t in all_list_teams:
                categorized.add(str(t))
                categorized.add(int(t) if str(t).isdigit() else t)
            return sorted(
                [t["team_number"] for t in teams_data
                 if t.get("team_number") is not None
                 and str(t["team_number"]) not in categorized
                 and t["team_number"] not in categorized]
            )

        if len(PickList_Data.objects.filter(event=comp_code)) == 0:
            for team in teams_data:
                num = team.get("team_number")
                if num is not None:
                    teams.append(num)
            teams.sort()
            if json_data and json_data.get('data'):
                d = json_data['data']
                no_pick_teams     = d[0] if len(d) > 0 else []
                first_pick_teams  = d[1] if len(d) > 1 else []
                second_pick_teams = d[2] if len(d) > 2 else []
                third_pick_teams  = d[3] if len(d) > 3 else []
                dn_pick_teams     = d[4] if len(d) > 4 else []
                fourth_pick_teams = d[5] if len(d) > 5 else []
                all_saved = (no_pick_teams + first_pick_teams + second_pick_teams + third_pick_teams
                             + dn_pick_teams + fourth_pick_teams)
                categorized = set(str(t) for t in all_saved) | set(all_saved)
                teams = [t for t in teams if str(t) not in categorized and t not in categorized]
            return render(request, "strategy/picklist.html", {
                'teams': teams,
                'comp_code': comp_code,
                'json_timestamp': json_timestamp,
                'no_pick_teams': no_pick_teams,
                'first_pick_teams': first_pick_teams,
                'second_pick_teams': second_pick_teams,
                'third_pick_teams': third_pick_teams,
                'fourth_pick_teams': fourth_pick_teams,
                'dn_pick_teams': dn_pick_teams,
                'team_info': team_info,
                'filter_groups': filter_groups,
                'sort_metrics': sort_metrics,
                'picklist_flags': picklist_flags,
            })

        picklist_data = PickList_Data.objects.filter(event=comp_code).values()[0]
        no_pick_teams     = picklist_data['no_pick'] if isinstance(picklist_data['no_pick'], list) else []
        first_pick_teams  = picklist_data['first_pick'] if isinstance(picklist_data['first_pick'], list) else []
        second_pick_teams = picklist_data['second_pick'] if isinstance(picklist_data['second_pick'], list) else []
        third_pick_teams  = picklist_data['third_pick'] if isinstance(picklist_data['third_pick'], list) else []
        dn_pick_teams     = picklist_data['dn_pick'] if isinstance(picklist_data['dn_pick'], list) else []
        fourth_pick_teams = picklist_data['fourth_pick'] if isinstance(picklist_data['fourth_pick'], list) else []
        # Any TBA team not in any saved list appears in the unassigned pool
        all_saved = (no_pick_teams + first_pick_teams + second_pick_teams + third_pick_teams
                     + dn_pick_teams + fourth_pick_teams)
        teams = _uncategorised(all_saved)
        return render(request, "strategy/picklist.html", {
            'teams': teams,
            'comp_code': comp_code,
            'json_timestamp': json_timestamp,
            'no_pick_teams': no_pick_teams,
            'first_pick_teams': first_pick_teams,
            'second_pick_teams': second_pick_teams,
            'third_pick_teams': third_pick_teams,
            'fourth_pick_teams': fourth_pick_teams,
            'dn_pick_teams': dn_pick_teams,
            'team_info': team_info,
            'filter_groups': filter_groups,
            'sort_metrics': sort_metrics,
            'picklist_flags': picklist_flags,
        })
        
PICKLIST_FLAGS = ('starred', 'struck')


def _normalise_team_list(value):
    """Flags are compared against DOM ids, which are always strings."""
    if not isinstance(value, list):
        return []
    return [str(v) for v in value]


def read_picklist_flags(comp_code):
    """Current starred/struck teams for an event, as lists of strings."""
    row = PickList_Data.objects.filter(event=comp_code).values('starred', 'struck').first()
    if not row:
        return {flag: [] for flag in PICKLIST_FLAGS}
    return {flag: _normalise_team_list(row.get(flag)) for flag in PICKLIST_FLAGS}


@login_required
@team_member_required
def picklist_flags(request):
    """
    Toggle a star or strike on one team.

    Flags live outside the five column lists so they survive a team being
    dragged between columns, and so toggling one never races the drag-and-drop
    autosave — the two write different fields.
    """
    comp_code = request.GET.get('comp')
    if not comp_code or not _SAFE_COMP_CODE.match(comp_code):
        return JsonResponse({'status': 'error', 'message': 'Invalid or missing competition code'}, status=400)

    if request.method == 'GET':
        return JsonResponse({'status': 'ok', 'flags': read_picklist_flags(comp_code)})

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request method'}, status=405)

    try:
        payload = json.loads(request.body.decode('utf-8'))
        flag = payload.get('flag')
        team = str(payload.get('team', '')).strip()
        turn_on = bool(payload.get('on'))

        if flag not in PICKLIST_FLAGS:
            return JsonResponse({'status': 'error', 'message': 'Unknown flag'}, status=400)
        if not team or not team.isdigit():
            return JsonResponse({'status': 'error', 'message': 'Invalid team number'}, status=400)

        PickList_Data.objects.get_or_create(event=comp_code)
        row = PickList_Data.objects.filter(event=comp_code).first()

        current = _normalise_team_list(getattr(row, flag, []))
        if turn_on and team not in current:
            current.append(team)
        elif not turn_on and team in current:
            current.remove(team)

        setattr(row, flag, current)
        row.save(update_fields=[flag])

        return JsonResponse({'status': 'success', 'flags': read_picklist_flags(comp_code)})
    except Exception:
        logger.exception("picklist_flags failed")
        return JsonResponse({'status': 'error', 'message': 'An unexpected error occurred'}, status=500)


@login_required
@team_member_required
def picklist_submit(request):
    comp_code = request.GET.get('comp')
    client_timestamp = request.GET.get('timestamp', '0')

    if request.method == 'POST':
        if not comp_code or not _SAFE_COMP_CODE.match(comp_code):
            return JsonResponse({'status': 'error', 'message': 'Invalid or missing competition code'}, status=400)
        try:
            picklist_data = json.loads(request.body.decode('utf-8'))

            # [no, 1st, 2nd, 3rd, dnp] from older pages, plus the 4th pick from current ones.
            if (not isinstance(picklist_data, list) or len(picklist_data) not in (5, 6)
                    or not all(isinstance(slot, list) for slot in picklist_data)):
                return JsonResponse({
                    'status': 'error',
                    'message': 'Invalid picklist data: expected a list of 5 or 6 lists'
                }, status=400)

            # Saved to the database (and to the JSON file where the disk is writable).
            stamp = write_json_picklist(comp_code, picklist_data)

            return JsonResponse({
                'status': 'success',
                'message': 'Data saved successfully',
                'timestamp': stamp
            })
            
        except Exception:
            logger.exception("picklist_submit POST failed")
            return JsonResponse({
                'status': 'error',
                'message': 'An unexpected error occurred'
            }, status=500)

    elif request.method == 'GET':
        if not comp_code or not _SAFE_COMP_CODE.match(comp_code):
            return JsonResponse({'status': 'error', 'message': 'Invalid or missing competition code'}, status=400)
        json_data = read_json_picklist(comp_code)
        
        if not json_data:
            return JsonResponse({
                'status': 'no_data',
                'timestamp': int(time() * 1000),
                'flags': read_picklist_flags(comp_code)
            })

        try:
            client_ts = int(client_timestamp)
        except (ValueError, TypeError):
            client_ts = 0
        if client_ts >= json_data['timestamp']:
            return JsonResponse({
                'status': 'no_change',
                'timestamp': json_data['timestamp'],
                'flags': read_picklist_flags(comp_code)
            })
        
        return JsonResponse({
            'status': 'updated',
            'data': json_data['data'],
            'timestamp': json_data['timestamp'],
            'flags': read_picklist_flags(comp_code)
        })

    return JsonResponse({
        'status': 'error',
        'message': 'Invalid request method'
    }, status=405)

@login_required
def dashboard(request):
    comp_code = request.GET.get('comp')
    config = config_loader.get_config()
    
    if request.method == "POST" and request.headers.get("x-requested-with") == "XMLHttpRequest":
        try:
            data_from_post = json.loads(request.body.decode("utf-8"))
            match_number = data_from_post.get("match_number")
            quantifier = data_from_post.get("quantifier", "Quals")

            # Map quantifier to TBA comp_level
            comp_level = {
                "Quals": "qm",
                "Playoff": "sf",
                "Prac": "pm"
            }.get(quantifier, "qm")

            # Get match with proper comp_level
            match = get_single_match(comp_code, f"{comp_level}{match_number}")
            
            # Check if match data exists
            if not match or 'red' not in match or 'blue' not in match:
                return JsonResponse({"error": "Match data not found or incomplete"}, status=404)

            # Existing team processing with quantifier passthrough
            red_json = {}
            red_teams = []
            blue_json = {}
            blue_teams = []

            # Bulk-fetch all match data for both alliances in ONE query, then group
            # in Python (same bulk-fetch pattern already used in rankings()) instead
            # of calling fetch_team_match_averages once per team.
            all_alliance_teams = list(match['red']) + list(match['blue'])
            try:
                team_numbers_int = [int(t) for t in all_alliance_teams]
            except (TypeError, ValueError):
                team_numbers_int = []

            all_matches = list(models.Team_Match_Data.objects.filter(
                team_number__in=team_numbers_int,
                event=comp_code,
                quantifier=quantifier,
                is_excluded=False,
            ))
            matches_by_team = defaultdict(list)
            for m in all_matches:
                matches_by_team[m.team_number].append(m)

            dash_config = config_loader.get_config()

            for red_team in match['red']:
                team_matches = matches_by_team.get(int(red_team), [])
                red_json[red_team] = _compute_team_averages(team_matches, dash_config)
                red_teams.append(red_team)

            for blue_team in match['blue']:
                team_matches = matches_by_team.get(int(blue_team), [])
                blue_json[blue_team] = _compute_team_averages(team_matches, dash_config)
                blue_teams.append(blue_team)

            response = {
                'red': red_json,
                'blue': blue_json,
                'red_teams': red_teams,
                'blue_teams': blue_teams,
                'quantifier': quantifier
            }
            return JsonResponse(response)

        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON format"}, status=400)
        except Exception:
            return JsonResponse({"error": "An unexpected error occurred"}, status=500)

    return render(request, "strategy/dashboard.html", {
        'config_metrics': config.get('metrics', []),
        'config_metrics_json': json.dumps(config.get('metrics', [])),
        'comp_code': comp_code or '',
    })

def _metric_value(match, key, legacy_keys):
    """Read a metric off one scouting row, falling back to renamed keys."""
    value = match.data.get(key) if match.data else None
    if value is None and legacy_keys:
        for legacy_key in legacy_keys:
            value = match.data.get(legacy_key)
            if value is not None:
                break
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _has_any_data(match):
    """True when a row carries at least one non-zero number (a real scan)."""
    if not match.data:
        return False
    numeric = [float(v) for v in match.data.values() if isinstance(v, (int, float))]
    return bool(numeric) and any(v != 0 for v in numeric)


def _compute_team_averages(team_match_data_list, config, exclude_zeros=True):
    """
    Compute averages from a pre-fetched list of matches (no DB calls).

    Every average collapses double-scouted matches first: the scouts covering
    one match are averaged with each other, and only then are matches averaged
    together. Averaging the rows directly would weight a match scouted by two
    people twice as heavily as one scouted by a single person.
    """
    if exclude_zeros:
        team_match_data_list = [m for m in team_match_data_list if _has_any_data(m)]
    if not team_match_data_list:
        return {}

    # Distinct matches, not scouting rows — two scouts on one match is one match.
    result = {'match_count': match_count(team_match_data_list)}

    for field in ('driverRanking', 'defenseRanking', 'autoLeave'):
        # 0 means "not rated", so it is skipped rather than averaged in.
        values = per_match_values(
            team_match_data_list,
            lambda m, f=field: (getattr(m, f, 0) or None) if getattr(m, f, 0) > 0 else None,
        )
        result[field] = round(sum(values) / len(values), 3) if values else 0

    for metric in config['metrics']:
        key = metric['key']
        aggregation = metric.get('aggregation', 'avg')
        legacy_keys = metric.get('legacy_keys', [])

        values = per_match_values(
            team_match_data_list,
            lambda m, k=key, lk=legacy_keys: _metric_value(m, k, lk),
        )

        if values:
            if aggregation == 'avg':
                result[key] = round(sum(values) / len(values), 3)
            elif aggregation == 'sum':
                # Per-match totals summed across matches; a double-scouted
                # match still contributes only its own (averaged) total.
                result[key] = round(sum(values), 3)
            elif aggregation == 'percent':
                result[key] = round((sum(values) / len(values)) * 100, 1)
        else:
            result[key] = 0

    result['start_pos'] = team_match_data_list[0].start_pos if team_match_data_list else 0
    return result


def fetch_team_match_averages(team_number, comp_code, quantifier, exclude_zeros=True, excluded_ids=None):
    """
    Dynamic calculation of team averages based on game_config.json.

    Thin wrapper over _compute_team_averages so both paths share one set of
    averaging rules; they used to be separate copies that could drift apart.
    """
    config = config_loader.get_config()

    team_match_data = models.Team_Match_Data.objects.filter(
        team_number=team_number,
        event=comp_code,
        quantifier=quantifier,
        is_excluded=False,
    )

    # Legacy: also support session-based exclusion for backward compat
    if excluded_ids:
        team_match_data = team_match_data.exclude(id__in=excluded_ids)

    return _compute_team_averages(list(team_match_data), config, exclude_zeros=exclude_zeros)


@login_required
def toggle_exclude_match(request):
    """Toggle exclusion of a specific match from rankings calculations."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    try:
        body = json.loads(request.body.decode('utf-8'))
        match_id = int(body['match_id'])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return JsonResponse({'error': 'Invalid match_id'}, status=400)

    try:
        match = models.Team_Match_Data.objects.get(id=match_id)
    except models.Team_Match_Data.DoesNotExist:
        return JsonResponse({'error': 'Match not found'}, status=404)

    match.is_excluded = not match.is_excluded
    match.save(update_fields=['is_excluded'])
    action = 'excluded' if match.is_excluded else 'included'

    logger.warning(
        "TOGGLE user=%s match_id=%d team=%d M%d action=%s",
        request.session.get('email', '?'), match_id, match.team_number, match.match_number, action
    )

    return JsonResponse({'status': action, 'match_id': match_id})


@login_required
def team_matches_detail(request, team_number):
    """Return individual match data for a team so strategists can review/exclude."""
    comp_code = request.GET.get('comp')
    quantifier = request.GET.get('quantifier', 'Quals')
    if not comp_code:
        return JsonResponse({'error': 'comp required'}, status=400)

    matches = Team_Match_Data.objects.filter(
        team_number=team_number, event=comp_code, quantifier=quantifier
    ).order_by('match_number')

    excluded = set(request.session.get('excluded_match_ids', []))
    result = []
    for m in matches:
        result.append({
            'id': m.id,
            'match_number': m.match_number,
            'start_pos': m.start_pos,
            'scout_name': m.scout_name,
            'data': m.data,
            'excluded': m.is_excluded,
        })
    return JsonResponse({'matches': result})

@login_required
def get_path_data(request, team_number):
    """API endpoint for retrieving match data - auto_path removed"""
    comp_code = request.GET.get('comp')
    match_number = request.GET.get('match')
    scout_name = request.GET.get('scout')
    
    if not comp_code:
        return JsonResponse({'error': 'Competition code required'}, status=400)
    
    try:
        filters = {
            'team_number': team_number,
            'event': comp_code,
            'match_number': match_number
        }
        
        if scout_name:
            filters['scout_name'] = scout_name
        
        match_data_records = Team_Match_Data.objects.filter(**filters)
        
        if match_data_records.count() == 0:
            return JsonResponse({
                'error': f'Match data not found for team {team_number}, match {match_number}'
            }, status=404)
        
        match_data = match_data_records.first()
        
        response = {
            'data': match_data.data,
            'match_number': match_data.match_number,
            'quantifier': match_data.quantifier,
            'scout_name': match_data.scout_name
        }
        
        if match_data_records.count() > 1:
            response['multiple_records'] = True
            response['all_scouts'] = [record.scout_name for record in match_data_records]
        
        return JsonResponse(response)
        
    except Exception:
        return JsonResponse({'error': 'An unexpected error occurred'}, status=500)


@login_required
def match_schedule_api(request):
    """Return upcoming/recent matches for an event, sorted by match number."""
    comp_code = request.GET.get('comp', '')
    if not comp_code or not _SAFE_COMP_CODE.match(comp_code):
        return JsonResponse({'error': 'Invalid or missing comp code'}, status=400)
    try:
        raw = get_match_schedule(comp_code)
    except Exception:
        return JsonResponse({'error': 'Failed to fetch schedule'}, status=500)
    if not isinstance(raw, list):
        return JsonResponse({'matches': []})

    # Normalize each match into a simple dict
    matches = []
    for m in raw:
        alliances = m.get('alliances') or {}
        red_keys  = alliances.get('red',  {}).get('team_keys', [])
        blue_keys = alliances.get('blue', {}).get('team_keys', [])
        red_teams  = [k.replace('frc', '') for k in red_keys]
        blue_teams = [k.replace('frc', '') for k in blue_keys]
        matches.append({
            'match_number': m.get('match_number'),
            'comp_level':   m.get('comp_level', 'qm'),
            'red_teams':    red_teams,
            'blue_teams':   blue_teams,
            'winning_alliance': m.get('winning_alliance') or '',
            'red_score':  (alliances.get('red',  {}).get('score') or -1),
            'blue_score': (alliances.get('blue', {}).get('score') or -1),
            'time': m.get('predicted_time') or m.get('time') or 0,
        })

    LEVEL_ORDER = {'qm': 0, 'qf': 1, 'sf': 2, 'f': 3}
    matches.sort(key=lambda x: (LEVEL_ORDER.get(x['comp_level'], 9), x['match_number'] or 0))
    return JsonResponse({'matches': matches})


@login_required
def team_info(request, team_number):
    """Return pit scouting info + full match history for a team — used by the rankings quick-view modal."""
    comp_code = request.GET.get('comp')
    quantifier = request.GET.get('quantifier', 'Quals')
    if not comp_code:
        return JsonResponse({'error': 'comp required'}, status=400)

    team = Teams.objects.filter(team_number=team_number, event=comp_code).first()
    matches = Team_Match_Data.objects.filter(
        team_number=team_number, event=comp_code, quantifier=quantifier
    ).order_by('match_number')

    excluded = set()
    config = config_loader.get_config()
    metrics = config.get('metrics', [])

    match_list = []
    for m in matches:
        flags = []
        if m.is_broken:   flags.append('Broken')
        if m.is_disabled: flags.append('Disabled')
        if m.is_tipped:   flags.append('Tipped')
        match_list.append({
            'id': m.id,
            'match_number': m.match_number,
            'start_pos': m.start_pos,
            'scout_name': m.scout_name,
            'autoLeave': m.autoLeave,
            'driverRanking': m.driverRanking,
            'defenseRanking': m.defenseRanking,
            'comment': m.comment,
            'flags': flags,
            'data': m.data,
            'excluded': m.is_excluded,
        })

    # Season averages, computed the same way the rankings table does it, so the
    # numbers a strategist reads in the modal agree with the ones they sorted on.
    raw_averages = fetch_team_match_averages(team_number, comp_code, quantifier)

    average_cards = []
    if raw_averages:
        for metric in metrics:
            key = metric['key']
            if key not in raw_averages:
                continue
            average_cards.append({
                'key': key,
                'label': metric['display_name'],
                'value': raw_averages[key],
                'unit': '%' if metric.get('aggregation') == 'percent' else '',
            })

        for key, label, unit in (
            ('driverRanking', 'Driver', '/5'),
            ('defenseRanking', 'Defense', '/5'),
            ('autoLeave', 'Auto Leave', ''),
        ):
            if raw_averages.get(key):
                average_cards.append({
                    'key': key,
                    'label': label,
                    'value': raw_averages[key],
                    'unit': unit,
                })

    return JsonResponse({
        'team_number': team_number,
        'logo_url': team.logo_url if team else None,
        'robot_picture': str(team.robot_picture) if team and team.robot_picture else None,
        'pit_scout_status': team.pit_scout_status if team else False,
        'pit_data': team.pit_data if team else {},
        'metrics': [{'key': m['key'], 'display_name': m['display_name']} for m in metrics],
        'matches': match_list,
        'averages': average_cards,
        'average_match_count': raw_averages.get('match_count', 0) if raw_averages else 0,
    })


# Axes of the picklist's "web stats" chart, clockwise from the top: scoring on
# the right-hand side, passing and defence on the left. Ratings are on a fixed
# 1-5 scale; everything else is scaled to the best team at the event.
WEB_AXES = (
    ('teleScore',      'Tele scoring', None),
    ('telePass',       'Tele pass',    None),
    ('autoPass',       'Auto pass',    None),
    ('autoScore',      'Auto scoring', None),
    ('defenseRanking', 'Defense',      5),
    ('driverRanking',  'Driver skill', 5),
)


@login_required
@team_member_required
def web_stats(request):
    """Per-team averages for the web chart, plus each axis's scale."""
    comp_code = request.GET.get('comp', '')
    quantifier = request.GET.get('quantifier', 'Quals')
    if not comp_code or not _SAFE_COMP_CODE.match(comp_code):
        return JsonResponse({'error': 'Invalid or missing competition code'}, status=400)

    config = config_loader.get_config()
    by_team = defaultdict(list)
    for m in Team_Match_Data.objects.filter(event=comp_code, quantifier=quantifier, is_excluded=False):
        by_team[m.team_number].append(m)

    teams = {}
    for team_number, matches in by_team.items():
        stats = _compute_team_averages(matches, config, exclude_zeros=True)
        if not stats:
            continue
        teams[str(team_number)] = {
            'values': {key: round(float(stats.get(key) or 0), 2) for key, _, _ in WEB_AXES},
            'matches': stats.get('match_count', 0),
        }

    axes = []
    for key, label, fixed_max in WEB_AXES:
        best = max((t['values'][key] for t in teams.values()), default=0)
        axes.append({'key': key, 'label': label, 'max': fixed_max or best or 1, 'rating': bool(fixed_max)})

    return JsonResponse({'axes': axes, 'teams': teams, 'quantifier': quantifier})

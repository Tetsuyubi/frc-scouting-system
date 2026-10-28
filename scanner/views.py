import json
import logging
import re
from pathlib import Path

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render

logger = logging.getLogger(__name__)
from teams.models import Teams, Team_Match_Data
from plugins import plugin_manager
from utils import config_loader
from helpers import login_required, rate_limit, team_member_required
from scanner.validation import validate_scan_data


_plugin_logger = logging.getLogger('plugins')

_SERVICE_WORKER = Path(__file__).resolve().parent / 'static' / 'scanner' / 'sw.js'


def service_worker(request):
    """The scanner's service worker, served from /scanner/ so its scope covers
    the scanner page (a worker under /static/ could only control /static/)."""
    response = HttpResponse(_SERVICE_WORKER.read_text(encoding='utf-8'),
                            content_type='application/javascript')
    # Browsers re-check the worker on navigation; never let a stale copy stick.
    response['Cache-Control'] = 'no-cache'
    response['Service-Worker-Allowed'] = '/scanner/'
    return response


@login_required
@rate_limit(max_calls=60, period_seconds=60)
@team_member_required
def scanner(request):
    if request.method == "POST" and request.headers.get("x-requested-with") == "XMLHttpRequest":
        try:
            body_data = json.loads(request.body.decode("utf-8"))
            # Handle both direct data and wrapped qr_data format
            if "qr_data" in body_data:
                data_from_post = body_data["qr_data"]
                # If qr_data is a string, parse it
                if isinstance(data_from_post, str):
                    data_from_post = json.loads(data_from_post)
            else:
                data_from_post = body_data
            if not isinstance(data_from_post, dict):
                return JsonResponse({"error": "Scan data must be an object"}, status=400)

            is_valid, validation_errors = validate_scan_data(data_from_post)
            if not is_valid:
                return JsonResponse({
                    "error": "; ".join(validation_errors),
                    "validation_errors": validation_errors,
                }, status=400)

            config = config_loader.get_config()

            # Extract anchor fields
            try:
                team_num = int(data_from_post["teamNumber"])
                event_code = str(data_from_post["comp_code"])
                match_num = int(data_from_post["matchNumber"])
                scout_name = str(data_from_post.get("name", "")).strip()
                comment = str(data_from_post.get("comment", ""))[:256]
            except (KeyError, ValueError, TypeError):
                return JsonResponse({"error": "Missing or invalid field in submission"}, status=400)

            # Validate event_code format: alphanumeric + underscores/hyphens, max 20 chars
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,16}', event_code):
                return JsonResponse({"error": "Invalid competition code format"}, status=400)

            # Ensure team exists
            Teams.objects.get_or_create(team_number=team_num, event=event_code)

            # Helper to safely get values
            def get_value(key, default=None):
                val = data_from_post.get(key)
                return val if val not in [None, ""] else default

            # Build anchor fields — normalize quantifier from QR display labels.
            # Try multiple possible field names since different scouting apps vary.
            QUANTIFIER_MAP = {
                'quals': 'Quals', 'qualifications': 'Quals', 'qual': 'Quals',
                'playoff': 'Playoff', 'play off': 'Playoff', 'playoffs': 'Playoff',
                'elimination': 'Playoff', 'elim': 'Playoff', 'finals': 'Playoff',
                'prac': 'Prac', 'practice': 'Prac',
            }
            quantifier_raw = (
                data_from_post.get("quantifier") or
                data_from_post.get("matchType") or
                data_from_post.get("match_type") or
                data_from_post.get("type") or
                data_from_post.get("phase") or
                data_from_post.get("matchPhase") or
                data_from_post.get("match_phase") or
                ""
            )
            quantifier_val = QUANTIFIER_MAP.get(str(quantifier_raw).strip().lower(), "Quals")

            match_data_defaults = {
                'scout_name': scout_name,
                'quantifier': quantifier_val,
                'start_pos': int(get_value("startPos", 0)),
                'comment': comment,
                'is_broken': bool(int(get_value("isBroken", 0))),
                'is_disabled': bool(int(get_value("isDisabled", 0))),
                'is_tipped': bool(int(get_value("isTipped", 0))),
                'driverRanking': int(get_value("driverRanking", 0)),
                'defenseRanking': int(get_value("defenseRanking", 0)),
                'autoLeave': int(get_value("autoLeave", 0)),
            }

            # Dynamic data bucket - loop through config metrics
            data_bucket = {}
            validation_errors = []
            
            for metric in config['metrics']:
                key = metric['key']
                # Try current key first
                value = get_value(key)
                
                # If not found, try legacy keys
                if value is None and 'legacy_keys' in metric:
                    for legacy_key in metric['legacy_keys']:
                        value = get_value(legacy_key)
                        if value is not None:
                            break
                
                if value is not None:
                    # Validate
                    is_valid, error_msg = config_loader.validate_data({key: value}, key)
                    if not is_valid:
                        validation_errors.append(error_msg)
                        continue
                    
                    # Type conversion
                    if metric['type'] == 'number':
                        try:
                            data_bucket[key] = float(value)
                        except (ValueError, TypeError):
                            data_bucket[key] = 0
                    elif metric['type'] == 'boolean':
                        if isinstance(value, str):
                            data_bucket[key] = value.strip().lower() in ('1', 'true')
                        else:
                            data_bucket[key] = bool(value)
                    else:
                        data_bucket[key] = value

            if validation_errors:
                return JsonResponse({"error": "; ".join(validation_errors)}, status=400)

            # Allow plugins to process QR data and add fields
            plugin_outputs = plugin_manager.execute_hook_with_meta('scanner_data_process', {
                'qr_data': data_from_post,
                'team_number': team_num,
                'event': event_code,
                'match_number': match_num,
                'scout_name': scout_name,
            })

            # Namespace all plugin contributions under data.plugins[plugin_name]
            if plugin_outputs:
                data_bucket.setdefault('plugins', {})
                for out in plugin_outputs:
                    plugin_name = out.get('plugin')
                    res = out.get('result')
                    if plugin_name and isinstance(res, dict):
                        data_bucket['plugins'][plugin_name] = res

            # Controlled anchor patches from plugins
            # Only whitelisted fields can be modified, and only if allowed in permissions config.
            ALLOWED_ANCHOR_PATCH = {
                'start_pos': 'int',
                'comment': 'str',
                'quantifier': ['Quals', 'Playoff', 'Prac'],
                'scout_name': 'str',
                'is_broken': 'bool',
                'is_disabled': 'bool',
                'is_tipped': 'bool',
                'driverRanking': 'int',
                'defenseRanking': 'int',
                'autoLeave': 'int',
            }
            anchor_patch_outputs = plugin_manager.execute_hook_with_meta('scanner_anchor_patch', {
                'current': dict(match_data_defaults),
                'qr_data': data_from_post,
                'team_number': team_num,
                'event': event_code,
                'match_number': match_num,
                'scout_name': scout_name,
            })
            for out in anchor_patch_outputs:
                plugin_name = out.get('plugin')
                patch = out.get('result')
                if not (plugin_name and isinstance(patch, dict)):
                    continue
                allowed_fields = set(plugin_manager.get_anchor_patch_fields(plugin_name))
                if not allowed_fields:
                    continue
                for key, val in patch.items():
                    if key not in ALLOWED_ANCHOR_PATCH or key not in allowed_fields:
                        continue
                    prev = match_data_defaults.get(key)
                    spec = ALLOWED_ANCHOR_PATCH[key]
                    try:
                        if spec == 'int':
                            match_data_defaults[key] = int(val)
                        elif spec == 'str':
                            match_data_defaults[key] = str(val)
                        elif spec == 'bool':
                            match_data_defaults[key] = bool(int(val)) if isinstance(val, (int, str)) else bool(val)
                        elif isinstance(spec, list):
                            match_data_defaults[key] = val if val in spec else prev
                        else:
                            match_data_defaults[key] = val
                        if prev != match_data_defaults[key]:
                            _plugin_logger.info("anchor_patch: plugin=%s key=%s prev=%r new=%r team=%s event=%s match=%s",
                                                plugin_name, key, prev, match_data_defaults[key], team_num, event_code, match_num)
                    except Exception:  # nosec B112
                        continue

            match_data_defaults['data'] = data_bucket

            # Save to database
            obj, created = Team_Match_Data.objects.update_or_create(
                team_number=team_num,
                event=event_code,
                match_number=match_num,
                scout_name=scout_name,
                defaults=match_data_defaults
            )

            confirmation_msg = "Successfully Updated" if not created else "Successfully Sent (New)"
            return JsonResponse({"confirmation": confirmation_msg, "id": obj.id}, status=200)

        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON format"}, status=400)
        except Exception:
            logger.exception("scanner POST failed")
            return JsonResponse({"error": "An unexpected error occurred"}, status=500)

    return render(request, "qr_scanner.html")
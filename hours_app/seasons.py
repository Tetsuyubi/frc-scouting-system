"""Season boundaries for the hours app.

Everything the app shows is scoped to the latest season: while it is open,
hours are tracked as usual; once it ends, the app is read-only and keeps
showing that season until an admin starts the next one, which starts every
total back at zero.
"""
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import CheckInOut, HoursAdjustment, HoursSeason, HoursSeasonTotal, SeasonRank


def current_season():
    """The latest season, open or not (None before any season exists)."""
    return HoursSeason.objects.order_by('-started_at', '-id').first()


def is_open(season=None):
    season = season if season is not None else current_season()
    # No season defined at all: behave as before seasons existed.
    return season is None or season.is_open


def sessions_in(season, queryset=None):
    """Restrict CheckInOut rows to those that started inside the season."""
    qs = queryset if queryset is not None else CheckInOut.objects.all()
    if season is None:
        return qs
    qs = qs.filter(check_in_time__gte=season.started_at)
    if season.ended_at:
        qs = qs.filter(check_in_time__lt=season.ended_at)
    return qs


def adjustments_in(season, queryset=None):
    qs = queryset if queryset is not None else HoursAdjustment.objects.all()
    return qs.filter(season=season) if season is not None else qs


def season_hours(user, season=None):
    """Closed sessions plus mentor adjustments for one person, in hours."""
    season = season if season is not None else current_season()
    total = 0.0
    for start, end in sessions_in(season, CheckInOut.objects.filter(
        user=user, check_out_time__isnull=False,
    )).values_list('check_in_time', 'check_out_time'):
        if start and end:
            total += max(0.0, (end - start).total_seconds() / 3600)
    if season is not None:
        total += adjustments_in(season).filter(user=user).aggregate(s=Sum('hours'))['s'] or 0.0
    return round(max(total, 0.0), 2)


def update_season_rank(user):
    SeasonRank.objects.update_or_create(user=user, defaults={'total_hours': season_hours(user)})


@transaction.atomic
def end_season():
    """Close the open season: check everyone out, save final totals."""
    season = current_season()
    if season is None or not season.is_open:
        return None
    now = timezone.now()
    CheckInOut.objects.filter(check_out_time__isnull=True).update(check_out_time=now)
    season.ended_at = now
    season.save(update_fields=['ended_at'])
    for rank in SeasonRank.objects.all():
        HoursSeasonTotal.objects.update_or_create(
            season=season, user_id=rank.user_id, defaults={'total_hours': rank.total_hours},
        )
    return season


@transaction.atomic
def start_season(name):
    """Open a new season. Totals reset to zero; old records stay in the
    database, outside the new season's window."""
    previous = current_season()
    if previous is not None and previous.is_open:
        end_season()
    season = HoursSeason.objects.create(name=name, started_at=timezone.now())
    SeasonRank.objects.all().delete()
    return season

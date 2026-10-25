from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from django.db.models import F
from django.db.models.functions import Coalesce, Concat
from datetime import timedelta
import pytz

SUBTEAM_CHOICES = [
    ('business', 'Business'),
    ('programming', 'Programming'),
    ('mechanical', 'Mechanical'),
    ('electrical', 'Electrical'),
    ('cad', 'CAD'),
    ('outreach', 'Outreach'),
]

BADGE_CHOICES = [
    ('cad_trained', 'CAD Trained'),
    ('programming_trained', 'Programming Trained'),
    ('lathe_trained', 'Lathe Trained'),
    ('cnc_trained', 'CNC Trained'),
    ('mill_trained', 'Mill Trained'),
    ('electrical_trained', 'Electrical Trained'),
]

ROLE_CHOICES = [
    ('member', 'Member'),
    ('lead', 'Lead'),
    ('mentor', 'Mentor'),
]

BADGE_COLORS = {
    'cad_trained':          '#3b82f6',
    'programming_trained':  '#8b5cf6',
    'lathe_trained':        '#f97316',
    'cnc_trained':          '#14b8a6',
    'mill_trained':         '#ec4899',
    'electrical_trained':   '#eab308',
}

# Each badge carries its own mark rather than a shared checkmark, and the line
# shown on a locked badge so an unearned one tells you how to earn it.
# `svg` is the inner markup of a 24x24 stroke icon, rendered with |safe.
BADGE_META = {
    'cad_trained': {
        'short': 'CAD',
        'how': 'Model a competition part and have a lead review it.',
        'svg': '<path d="M12 2 3 7v10l9 5 9-5V7z"/><path d="M3 7l9 5 9-5"/><path d="M12 12v10"/>',
    },
    'programming_trained': {
        'short': 'Code',
        'how': 'Deploy working code to the robot with a mentor present.',
        'svg': '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
    },
    'lathe_trained': {
        'short': 'Lathe',
        'how': 'Pass the lathe safety check-off with a mentor.',
        'svg': ('<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2.5"/>'
                '<path d="M12 4v3M20 12h-3M12 20v-3M4 12h3"/>'),
    },
    'cnc_trained': {
        'short': 'CNC',
        'how': 'Run a job start to finish on the CNC under supervision.',
        'svg': ('<path d="M10 2h4v8a2 2 0 0 1-4 0z"/><path d="M12 12v4"/>'
                '<rect x="3" y="17" width="18" height="5" rx="1"/>'),
    },
    'mill_trained': {
        'short': 'Mill',
        'how': 'Pass the manual mill safety check-off with a mentor.',
        'svg': ('<rect x="9" y="2" width="6" height="6" rx="1"/><path d="M12 8v4"/>'
                '<path d="M9.5 12h5L12 16.5z"/><rect x="3" y="19" width="18" height="3" rx="1"/>'),
    },
    'electrical_trained': {
        'short': 'Electrical',
        'how': 'Wire and verify a full control board with a mentor.',
        'svg': '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    },
}


def badge_roster(earned_types=()):
    """Every badge in the program, marked earned or locked.

    The badge row is rendered from this rather than from the user's own rows,
    so an unearned badge still shows up as something to go and earn.
    """
    earned = set(earned_types)
    roster = []
    for badge_type, label in BADGE_CHOICES:
        meta = BADGE_META.get(badge_type, {})
        roster.append({
            'type': badge_type,
            'label': label,
            'short': meta.get('short', label),
            'svg': meta.get('svg', ''),
            'how': meta.get('how', ''),
            'color': BADGE_COLORS.get(badge_type, '#888'),
            'earned': badge_type in earned,
        })
    return roster

SUBTEAM_COLORS = {
    'business':    '#3b82f6',
    'programming': '#8b5cf6',
    'mechanical':  '#f97316',
    'electrical':  '#eab308',
    'cad':         '#14b8a6',
    'outreach':    '#22c55e',
}


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='hours_profile')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='member')
    subteams = models.CharField(max_length=200, blank=True, default='')
    onboarding_complete = models.BooleanField(default=False)

    # Cloudinary URL, set from the profile page. Blank falls back to initials,
    # which is also what a mentor reset restores.
    avatar_url = models.URLField(max_length=512, blank=True, default='')
    about = models.TextField(blank=True, default='')

    # Free text rather than a fixed choice list: sets people do not use are
    # common, and an "other" option that has to be explained is worse than a
    # field that simply accepts what someone types. Blank means not stated,
    # which is shown as nothing at all rather than a guess.
    pronouns = models.CharField(max_length=40, blank=True, default='')

    def __str__(self):
        return f"{self.user.username} ({self.role})"

    @property
    def initials(self):
        first = (self.user.first_name or '').strip()
        last = (self.user.last_name or '').strip()
        if first or last:
            return ((first[:1] + last[:1]) or first[:2]).upper()
        return (self.user.username or '?')[:2].upper()

    def get_subteams_list(self):
        return [s for s in self.subteams.split(',') if s]

    def get_subteam_labels(self):
        label_map = dict(SUBTEAM_CHOICES)
        return [(s, label_map.get(s, s), SUBTEAM_COLORS.get(s, '#888')) for s in self.get_subteams_list()]

    def is_privileged(self):
        return self.role in ('lead', 'mentor')


class Badge(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='hours_badges')
    badge_type = models.CharField(max_length=50, choices=BADGE_CHOICES)
    awarded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='badges_awarded'
    )
    awarded_at = models.DateTimeField(auto_now_add=True)
    # Null until the recipient has been shown the award animation once.
    seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('user', 'badge_type')

    def __str__(self):
        return f"{self.get_badge_type_display()} — {self.user.username}"

    @property
    def color(self):
        return BADGE_COLORS.get(self.badge_type, '#888')

    @property
    def svg(self):
        return BADGE_META.get(self.badge_type, {}).get('svg', '')


class SeasonRank(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='rank')
    total_hours = models.FloatField(default=0.0)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username}: {self.total_hours} hrs"


class CheckInOut(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    check_in_time = models.DateTimeField(null=True, blank=True)
    check_out_time = models.DateTimeField(null=True, blank=True)
    logic = models.CharField(max_length=50, blank=True, null=True)
    accomplishments = models.TextField(blank=True, null=True)
    reason = models.CharField(max_length=255, blank=True, null=True)

    def __str__(self):
        if self.check_in_time:
            return f"{self.user.username} - {self.check_in_time.strftime('%m/%d %H:%M')}"
        return f"{self.user.username} - No Time Data"

    @property
    def duration_hours(self):
        if self.check_in_time and self.check_out_time:
            diff = self.check_out_time - self.check_in_time
            return diff.total_seconds() / 3600
        return 0

    @property
    def duration_display(self):
        if self.check_in_time and self.check_out_time:
            diff = self.check_out_time - self.check_in_time
            hours, remainder = divmod(int(diff.total_seconds()), 3600)
            minutes = remainder // 60
            return f"{hours}h {minutes}m"
        return "Active"

    @staticmethod
    def close_forgotten_sessions():
        pst = pytz.timezone('America/Los_Angeles')
        now_pst = timezone.now().astimezone(pst)
        today_start = now_pst.replace(hour=0, minute=0, second=0, microsecond=0)

        CheckInOut.objects.filter(
            check_out_time__isnull=True,
            check_in_time__lt=today_start
        ).update(
            check_out_time=F('check_in_time') + timedelta(hours=2),
            reason=Concat(Coalesce(F('reason'), models.Value('')), models.Value(' (Auto-closed)'))
        )

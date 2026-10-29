from django.test import TestCase
from unittest import skipIf
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from .models import CheckInOut
# hours_app was ported over from the standalone robotics-hours project, which
# is where verify_user_access lives. It has no equivalent here yet, so the
# access-control tests below are skipped rather than deleted — they describe
# behaviour worth keeping if that signal is ever ported across.
try:
    from robotics_hours.signals import verify_user_access
except ModuleNotFoundError:
    verify_user_access = None
import pytz

class CheckInOutTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.pst = pytz.timezone('America/Los_Angeles')
    
    def test_close_forgotten_sessions_queries(self):
        """Verify that close_forgotten_sessions uses only 1 query (bulk update)."""
        now_pst = timezone.now().astimezone(self.pst)
        yesterday = now_pst - timedelta(days=1)
        
        # Create 5 forgotten sessions from yesterday
        for i in range(5):
            CheckInOut.objects.create(
                user=self.user,
                check_in_time=yesterday,
                reason='Test session' if i % 2 == 0 else None  # Mix NULL and non-NULL reasons
            )
        
        # Verify only 1 query is executed (the bulk update)
        with self.assertNumQueries(1):
            CheckInOut.close_forgotten_sessions()
        
        # Verify all sessions were closed correctly
        closed_sessions = CheckInOut.objects.filter(user=self.user, check_out_time__isnull=False)
        self.assertEqual(closed_sessions.count(), 5)
        
        # Verify reason field was updated correctly (including NULL cases)
        for session in closed_sessions:
            self.assertIn('(Auto-closed)', session.reason)
            self.assertEqual(session.check_out_time, session.check_in_time + timedelta(hours=2))

@skipIf(verify_user_access is None, "verify_user_access not ported from robotics_hours")
class UserAccessControlTestCase(TestCase):
    def test_unauthorized_user_deactivated(self):
        """Unauthorized users should be deactivated."""
        user = User.objects.create_user(
            username='hacker',
            email='hacker@evil.com',
            password='test123',
            is_active=True
        )
        verify_user_access(None, user)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
    
    def test_team_member_activated(self):
        """Team members should be active but not staff."""
        user = User.objects.create_user(
            username='student',
            email='student@team2073.com',
            password='test123'
        )
        verify_user_access(None, user)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
    
    def test_admin_granted_privileges(self):
        """Admins should be active, staff, and superuser."""
        user = User.objects.create_user(
            username='admin',
            email='member@team2073.com',
            password='test123'
        )
        verify_user_access(None, user)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)




class SeasonAndAdjustmentTests(TestCase):
    """Seasons gate check-ins; mentors and leads can add hours for others."""

    def setUp(self):
        from authenticate.models import AuthorizedUser
        from .models import UserProfile
        self.client.defaults['HTTP_HOST'] = 'localhost'
        self.client.defaults['wsgi.url_scheme'] = 'https'
        self.mentor = User.objects.create_user(username='mentor', email='mentor@team2073.com', first_name='Mo')
        self.student = User.objects.create_user(username='kid', email='kid@team2073.com', first_name='Kai')
        for u in (self.mentor, self.student):
            UserProfile.objects.create(user=u, onboarding_complete=True)
        UserProfile.objects.filter(user=self.mentor).update(role='mentor')
        AuthorizedUser.objects.create(email='admin@team2073.com', roles='admin')

    def login(self, email):
        session = self.client.session
        session['email'] = email
        session['name'] = 'Test'
        session.save()

    def open_season(self):
        from .seasons import start_season
        return start_season('Test Season')

    def test_migration_leaves_hours_closed(self):
        from .seasons import current_season, is_open
        self.assertIsNotNone(current_season())
        self.assertFalse(is_open())

    def test_check_in_is_refused_while_season_is_closed(self):
        self.login('kid@team2073.com')
        self.client.post('/hours/check-in-out/', {'action': 'check_in', 'reason': 'Build day work'})
        self.assertFalse(CheckInOut.objects.filter(user=self.student).exists())

    def test_check_in_works_once_a_season_is_open(self):
        self.open_season()
        self.login('kid@team2073.com')
        self.client.post('/hours/check-in-out/', {'action': 'check_in', 'reason': 'Build day work'})
        self.assertTrue(CheckInOut.objects.filter(user=self.student, check_out_time__isnull=True).exists())

    def test_new_season_resets_totals_but_keeps_records(self):
        from .models import SeasonRank, HoursSeasonTotal
        from .seasons import update_season_rank, end_season, start_season
        first = self.open_season()
        start = timezone.now()
        CheckInOut.objects.create(user=self.student, check_in_time=start,
                                  check_out_time=start + timedelta(hours=2))
        update_season_rank(self.student)
        self.assertAlmostEqual(SeasonRank.objects.get(user=self.student).total_hours, 2.0, places=1)
        end_season()
        self.assertAlmostEqual(HoursSeasonTotal.objects.get(season=first, user=self.student).total_hours, 2.0, places=1)
        start_season('Next Season')
        self.assertFalse(SeasonRank.objects.filter(user=self.student).exists())
        self.assertEqual(CheckInOut.objects.filter(user=self.student).count(), 1)

    def test_mentor_can_add_hours_for_a_member(self):
        from .models import HoursAdjustment, SeasonRank
        self.open_season()
        self.login('mentor@team2073.com')
        self.client.post(f'/hours/user/{self.student.id}/adjust/', {
            'hours': '2.5', 'work_date': timezone.localdate().isoformat(), 'reason': 'Outreach event',
        })
        adj = HoursAdjustment.objects.get(user=self.student)
        self.assertEqual(adj.added_by, self.mentor)
        self.assertAlmostEqual(SeasonRank.objects.get(user=self.student).total_hours, 2.5)

    def test_member_cannot_add_hours(self):
        from .models import HoursAdjustment
        self.open_season()
        self.login('kid@team2073.com')
        self.client.post(f'/hours/user/{self.mentor.id}/adjust/', {
            'hours': '5', 'work_date': timezone.localdate().isoformat(), 'reason': 'Free hours',
        })
        self.assertFalse(HoursAdjustment.objects.exists())

    def test_mentor_cannot_add_hours_to_themselves_or_while_closed(self):
        from .models import HoursAdjustment
        self.login('mentor@team2073.com')
        self.client.post(f'/hours/user/{self.student.id}/adjust/', {
            'hours': '1', 'work_date': timezone.localdate().isoformat(), 'reason': 'Closed season',
        })
        self.open_season()
        self.client.post(f'/hours/user/{self.mentor.id}/adjust/', {
            'hours': '1', 'work_date': timezone.localdate().isoformat(), 'reason': 'Own hours',
        })
        self.assertFalse(HoursAdjustment.objects.exists())

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



import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

# Create your tests here.
from authenticate.models import AuthorizedUser
from teams.models import Team_Match_Data


class AdminAuthFixtureMixin:
    """Shared session/DB setup for an authorized admin-panel user.
    """

    def _make_admin_client(self):
        client = Client()
        client.defaults['HTTP_HOST'] = 'localhost'
        client.defaults['wsgi.url_scheme'] = 'https'
        AuthorizedUser.objects.get_or_create(
            email='admin@team2073.com',
            defaults={'roles': 'admin'},
        )
        session = client.session
        session['email'] = 'admin@team2073.com'
        session['is_admin'] = True
        session.save()
        return client

    def _make_anon_client(self):
        client = Client()
        client.defaults['HTTP_HOST'] = 'localhost'
        client.defaults['wsgi.url_scheme'] = 'https'
        return client


class UpdateMatchDataTests(AdminAuthFixtureMixin, TestCase):
    """TEST-005: admin match-data editor's raw JSON write (update_match_data)."""

    def setUp(self):
        self.client = self._make_admin_client()
        self.match = Team_Match_Data.objects.create(
            team_number=2073,
            event='testing',
            match_number=1,
            scout_name='Scout1',
            quantifier='Quals',
            start_pos=0,
            comment='old comment',
            data={'autoScore': 1},
        )

    def _url(self):
        return reverse('update_match')

    def test_authorized_post_updates_correct_row(self):
        response = self.client.post(self._url(), data={
            'match_id': self.match.id,
            'data_json': json.dumps({'autoScore': 9, 'teleScore': 4}),
            'start_pos': '3',
            'quantifier': 'Playoff',
            'comment': 'updated comment',
            'is_broken': 'true',
            'is_disabled': 'false',
            'is_tipped': 'false',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'success')

        self.match.refresh_from_db()
        self.assertEqual(self.match.data, {'autoScore': 9, 'teleScore': 4})
        self.assertEqual(self.match.start_pos, 3)
        self.assertEqual(self.match.quantifier, 'Playoff')
        self.assertEqual(self.match.comment, 'updated comment')
        self.assertTrue(self.match.is_broken)
        self.assertFalse(self.match.is_disabled)
        self.assertFalse(self.match.is_tipped)

    def test_nonexistent_match_id_returns_404_branch(self):
        response = self.client.post(self._url(), data={
            'match_id': 999999,
            'data_json': '{}',
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['status'], 'error')

    def test_malformed_json_returns_400_branch(self):
        response = self.client.post(self._url(), data={
            'match_id': self.match.id,
            'data_json': 'not-valid-json{{{',
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['status'], 'error')

        # Nothing should have been persisted from the failed update.
        self.match.refresh_from_db()
        self.assertEqual(self.match.data, {'autoScore': 1})

    def test_unauthorized_session_is_rejected(self):
        anon_client = self._make_anon_client()
        response = anon_client.post(self._url(), data={
            'match_id': self.match.id,
            'data_json': '{}',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/auth/')

        self.match.refresh_from_db()
        self.assertEqual(self.match.data, {'autoScore': 1})


class RemoveUserTests(AdminAuthFixtureMixin, TestCase):
    """TEST-006: admin remove_user is a hard, irreversible delete."""

    def setUp(self):
        self.client = self._make_admin_client()
        self.target = AuthorizedUser.objects.create(email='target@example.com', roles='student')
        self.other = AuthorizedUser.objects.create(email='other@example.com', roles='student')

    def test_authorized_admin_post_deletes_only_target_user(self):
        url = reverse('remove_user', kwargs={'user_id': self.target.id})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

        self.assertFalse(AuthorizedUser.objects.filter(id=self.target.id).exists())
        self.assertTrue(AuthorizedUser.objects.filter(id=self.other.id).exists())

    def test_non_authorized_session_is_rejected(self):
        anon_client = self._make_anon_client()
        url = reverse('remove_user', kwargs={'user_id': self.target.id})
        response = anon_client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/auth/')

        self.assertTrue(AuthorizedUser.objects.filter(id=self.target.id).exists())

    def test_get_request_does_not_delete_user(self):
        # @require_POST guards this view — a GET should be rejected (405) without
        # ever reaching the delete, not silently perform it.
        url = reverse('remove_user', kwargs={'user_id': self.target.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)
        self.assertTrue(AuthorizedUser.objects.filter(id=self.target.id).exists())

    def test_non_admin_role_cannot_remove_users_even_with_stale_admin_session(self):
        student = AuthorizedUser.objects.create(
            email='student@example.com',
            roles='student',
        )
        client = Client()
        client.defaults['HTTP_HOST'] = 'localhost'
        session = client.session
        session['email'] = student.email
        session['role'] = 'student'
        session['is_admin'] = True
        session.save()

        response = client.post(reverse('remove_user', kwargs={'user_id': self.target.id}))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/auth/unauthorized/')
        self.assertTrue(AuthorizedUser.objects.filter(id=self.target.id).exists())

    def test_last_admin_cannot_be_removed(self):
        admin = AuthorizedUser.objects.get(email='admin@team2073.com')

        response = self.client.post(reverse('remove_user', kwargs={'user_id': admin.id}))

        self.assertEqual(response.status_code, 409)
        self.assertTrue(AuthorizedUser.objects.filter(id=admin.id).exists())

    def test_last_admin_cannot_be_demoted(self):
        admin = AuthorizedUser.objects.get(email='admin@team2073.com')

        response = self.client.post(
            reverse('update_user_role', kwargs={'user_id': admin.id}),
            data={'roles': ['teacher']},
        )

        self.assertEqual(response.status_code, 409)
        admin.refresh_from_db()
        self.assertTrue(admin.is_admin)


class AdminRequiredRoleTests(TestCase):
    def test_authorized_student_cannot_access_admin_panel(self):
        student = AuthorizedUser.objects.create(
            email='student@example.com',
            roles='student',
        )
        client = Client()
        client.defaults['HTTP_HOST'] = 'localhost'
        session = client.session
        session['email'] = student.email
        session['role'] = 'student'
        session.save()

        response = client.get(reverse('admin_panel'))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/auth/unauthorized/')


class HoursCheckoutAllTests(AdminAuthFixtureMixin, TestCase):
    """TEST-007: hours_checkout_all force-closes every active attendance session."""

    def setUp(self):
        self.client = self._make_admin_client()

    def test_checkout_all_closes_active_sessions_and_skips_already_closed(self):
        from hours_app.models import CheckInOut

        u1 = User.objects.create_user(username='scout1', email='scout1@example.com')
        u2 = User.objects.create_user(username='scout2', email='scout2@example.com')
        u3 = User.objects.create_user(username='scout3', email='scout3@example.com')

        now = timezone.now()
        active1 = CheckInOut.objects.create(user=u1, check_in_time=now - timedelta(hours=1))
        active2 = CheckInOut.objects.create(user=u2, check_in_time=now - timedelta(hours=2))
        closed_checkout_time = now - timedelta(minutes=30)
        closed = CheckInOut.objects.create(
            user=u3,
            check_in_time=now - timedelta(hours=3),
            check_out_time=closed_checkout_time,
        )

        response = self.client.post(reverse('hours_checkout_all'))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'success')
        self.assertEqual(body['count'], 2)

        active1.refresh_from_db()
        active2.refresh_from_db()
        closed.refresh_from_db()

        self.assertIsNotNone(active1.check_out_time)
        self.assertIsNotNone(active2.check_out_time)
        # The already-closed session's check_out_time must not be overwritten to "now".
        self.assertEqual(closed.check_out_time, closed_checkout_time)
        # check_in_time is untouched by the bulk update.
        self.assertEqual(active1.check_in_time, now - timedelta(hours=1))

    def test_non_post_returns_405(self):
        response = self.client.get(reverse('hours_checkout_all'))
        self.assertEqual(response.status_code, 405)

    def test_non_admin_session_is_rejected(self):
        anon_client = self._make_anon_client()
        response = anon_client.post(reverse('hours_checkout_all'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/auth/')


class AdminRequiredRejectionTests(TestCase):
    """TEST-010: admin_required rejects a session with no AuthorizedUser row."""

    def test_admin_panel_redirects_when_not_an_authorized_user(self):
        anon_client = Client()
        anon_client.defaults['HTTP_HOST'] = 'localhost'
        anon_client.defaults['wsgi.url_scheme'] = 'https'
        session = anon_client.session
        session['email'] = 'not-authorized@example.com'
        session.save()

        response = anon_client.get(reverse('admin_panel'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/auth/unauthorized/')

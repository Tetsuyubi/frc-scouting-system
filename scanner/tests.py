import json

from django.test import TestCase

# Create your tests here.
from django.urls import reverse, resolve
from .views import scanner
from teams.models import Team_Match_Data


class ScannerTests(TestCase):

    def setUp(self):
        # Ensure test client presents an allowed host and simulate logged-in session
        self.client.defaults['HTTP_HOST'] = 'localhost'
        # Make requests appear secure so SECURE_SSL_REDIRECT doesn't force https redirects
        self.client.defaults['wsgi.url_scheme'] = 'https'
        session = self.client.session
        session['email'] = 'test@team2073.com'
        # scanner() is open to every @team2073.com account, whatever their role.
        session['role'] = 'scout_tech'
        session.save()

    def test_scanner_view_success_status_code(self):
        url = reverse('scanner')
        response = self.client.get(url, follow=True)
        self.assertEqual(response.status_code, 200)

    def test_scanner_url_resolves_scanner_view(self):
        view = resolve('/scanner/')
        self.assertEqual(view.func, scanner)

    def test_scanner_is_open_to_any_team_account(self):
        # A plain student on a team email can use the scanner: no scout_tech role needed.
        session = self.client.session
        session['role'] = 'student'
        session.save()
        response = self.client.get(reverse('scanner'), follow=True)
        self.assertEqual(response.status_code, 200)

    def test_scanner_rejects_non_team_account(self):
        # Negative case: the gate still rejects someone who is neither on the team
        # domain nor an admin.
        session = self.client.session
        session['email'] = 'someone@gmail.com'
        session['role'] = 'scout_tech'
        session['is_admin'] = False
        session.save()
        response = self.client.get(reverse('scanner'), follow=True)
        self.assertEqual(response.status_code, 403)

    def test_scanner_post_creates_match_data(self):
        # TEST-003: exercise the actual POST/data-mutation path — the core
        # competition-day function — and assert on the persisted row, not just a
        # 200 status code.
        url = reverse('scanner')
        payload = {
            'teamNumber': 2073,
            'comp_code': 'testing',
            'matchNumber': 7,
            'name': 'ScoutTester',
            'quantifier': 'quals',
            'startPos': 2,
            'comment': 'solid match',
            'isBroken': 0,
            'isDisabled': 0,
            'isTipped': 0,
            'driverRanking': 4,
            'defenseRanking': 3,
            'autoLeave': 1,
            'autoScore': 5,
            'autoPass': 2,
            'autoClimb': 3,
            'teleScore': 12,
            'telePass': 6,
            'endClimb': 2,
            'shootingAccuracy': 75,
            'Steal': 1,
        }
        response = self.client.post(
            url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn('confirmation', body)

        match = Team_Match_Data.objects.get(
            team_number=2073, event='testing', match_number=7, scout_name='ScoutTester'
        )
        self.assertEqual(match.quantifier, 'Quals')
        self.assertEqual(match.start_pos, 2)
        self.assertEqual(match.comment, 'solid match')
        self.assertEqual(match.driverRanking, 4)
        self.assertEqual(match.defenseRanking, 3)
        self.assertEqual(match.autoLeave, 1)
        self.assertEqual(match.data.get('autoScore'), 5.0)
        self.assertEqual(match.data.get('teleScore'), 12.0)

    def test_scanner_post_resubmission_updates_instead_of_duplicating(self):
        # Second submission for the same (team, event, match, scout) should update
        # the existing row via update_or_create, not create a duplicate.
        url = reverse('scanner')

        def _submit(auto_score):
            payload = {
                'teamNumber': 2073,
                'comp_code': 'testing',
                'matchNumber': 9,
                'name': 'ScoutTester',
                'autoScore': auto_score,
            }
            return self.client.post(
                url,
                data=json.dumps(payload),
                content_type='application/json',
                HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            )

        first = _submit(1)
        second = _submit(8)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        matches = Team_Match_Data.objects.filter(
            team_number=2073, event='testing', match_number=9, scout_name='ScoutTester'
        )
        self.assertEqual(matches.count(), 1)
        self.assertEqual(matches.first().data.get('autoScore'), 8.0)

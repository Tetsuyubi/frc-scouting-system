import json

from django.db import IntegrityError, transaction

from django.test import TestCase
from django.urls import reverse

# Create your tests here.
from strategy.models import PickList_Data
from strategy.views import get_json_path, fetch_team_match_averages
from teams.models import Team_Match_Data


class PicklistSubmitTests(TestCase):
    """TEST-011: picklist_submit's dual write (on-disk JSON + PickList_Data row)."""

    COMP_CODE = 'testpicklist2026'

    def setUp(self):
        self.client.defaults['HTTP_HOST'] = 'localhost'
        self.client.defaults['wsgi.url_scheme'] = 'https'
        session = self.client.session
        session['email'] = 'strategist@team2073.com'
        session['role'] = 'strategist'
        session.save()
        # Resolve the on-disk path the same way the view does, so cleanup targets
        # exactly the file the view would write (see strategy/views.py:get_json_path).
        self.json_path = get_json_path(self.COMP_CODE)

    def tearDown(self):
        if self.json_path and self.json_path.exists():
            self.json_path.unlink()

    def _url(self, save_to_db=True):
        qs = f'?comp={self.COMP_CODE}'
        if save_to_db:
            qs += '&save_to_db=true'
        return reverse('picklist_submit') + qs

    def test_valid_payload_writes_json_file_and_db_row(self):
        payload = [[1], [2, 3], [], [], []]
        response = self.client.post(
            self._url(save_to_db=True),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'success')

        # Filesystem write
        self.assertTrue(self.json_path.exists())
        with open(self.json_path) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk['data'], payload)

        # DB write
        row = PickList_Data.objects.get(event=self.COMP_CODE)
        self.assertEqual(row.no_pick, [1])
        self.assertEqual(row.first_pick, [2, 3])
        self.assertEqual(row.second_pick, [])
        self.assertEqual(row.third_pick, [])
        self.assertEqual(row.dn_pick, [])

    def test_malformed_payload_returns_400_and_does_not_write_to_disk(self):
        # Wrong length (3 slots instead of exactly 5) — locks in the FUNC-006 fix:
        # this must be rejected cleanly, not raise an unhandled IndexError/500, and
        # must not perform the file write before validating.
        payload = [[1], [2], [3]]
        response = self.client.post(
            self._url(save_to_db=True),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['status'], 'error')

        self.assertFalse(self.json_path.exists())
        self.assertFalse(PickList_Data.objects.filter(event=self.COMP_CODE).exists())

    def test_non_list_payload_returns_400_and_does_not_write_to_disk(self):
        payload = {'not': 'a list'}
        response = self.client.post(
            self._url(save_to_db=True),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.json_path.exists())


class PicklistUniqueEventTests(TestCase):
    """One PickList_Data row per event, so concurrent saves can't fork the board."""

    def test_second_row_for_same_event_is_rejected(self):
        PickList_Data.objects.create(event='uniqueevent2026')
        with self.assertRaises(IntegrityError), transaction.atomic():
            PickList_Data.objects.create(event='uniqueevent2026')


class DoubleScoutedMatchAveragingTests(TestCase):
    """
    A match scouted by two people must count once.

    Before this, both scouting rows fed the average directly, so a
    double-scouted match pulled the mean toward whatever those two scouts saw
    and counted twice against every singly-scouted match.
    """

    COMP_CODE = 'testavg2026'

    def _match(self, match_number, scout, tele_score, driver=0):
        return Team_Match_Data.objects.create(
            team_number=1234,
            event=self.COMP_CODE,
            match_number=match_number,
            scout_name=scout,
            quantifier='Quals',
            driverRanking=driver,
            data={'teleScore': tele_score},
        )

    def test_double_scouted_match_counts_once(self):
        # Match 1 seen by two scouts who disagree; matches 2 and 3 seen by one.
        self._match(1, 'alice', 10)
        self._match(1, 'bob', 20)
        self._match(2, 'alice', 30)
        self._match(3, 'alice', 50)

        result = fetch_team_match_averages(1234, self.COMP_CODE, 'Quals')

        # Match 1 collapses to (10+20)/2 = 15, then (15+30+50)/3 = 31.667.
        # Averaging the four rows directly would have given 27.5.
        self.assertAlmostEqual(result['teleScore'], 31.667, places=2)

    def test_match_count_is_distinct_matches_not_rows(self):
        self._match(1, 'alice', 10)
        self._match(1, 'bob', 20)
        self._match(2, 'alice', 30)

        result = fetch_team_match_averages(1234, self.COMP_CODE, 'Quals')

        self.assertEqual(result['match_count'], 2)

    def test_scout_who_left_field_blank_is_not_counted_as_zero(self):
        # Only one of the two scouts recorded teleScore for match 1.
        self._match(1, 'alice', 10)
        Team_Match_Data.objects.create(
            team_number=1234,
            event=self.COMP_CODE,
            match_number=1,
            scout_name='bob',
            quantifier='Quals',
            data={'autoScore': 5},          # no teleScore key at all
        )

        result = fetch_team_match_averages(1234, self.COMP_CODE, 'Quals')

        # The blank must not drag the match down to (10+0)/2 = 5.
        self.assertAlmostEqual(result['teleScore'], 10.0, places=2)

    def test_agreeing_scouts_leave_the_average_unchanged(self):
        # When both scouts record the same value, collapsing is a no-op.
        self._match(1, 'alice', 20)
        self._match(1, 'bob', 20)
        self._match(2, 'alice', 40)

        result = fetch_team_match_averages(1234, self.COMP_CODE, 'Quals')

        self.assertAlmostEqual(result['teleScore'], 30.0, places=2)


class PicklistFlagsTests(TestCase):
    """Star and strike persist per team and survive a column change."""

    COMP_CODE = 'testflags2026'

    def setUp(self):
        self.client.defaults['HTTP_HOST'] = 'localhost'
        self.client.defaults['wsgi.url_scheme'] = 'https'
        session = self.client.session
        session['email'] = 'strategist@team2073.com'
        session['role'] = 'strategist'
        session['is_admin'] = True
        session.save()

    def _post(self, payload):
        return self.client.post(
            reverse('picklist_flags') + '?comp=' + self.COMP_CODE,
            data=json.dumps(payload),
            content_type='application/json',
        )

    def test_star_is_stored_and_toggles_off(self):
        response = self._post({'flag': 'starred', 'team': '1234', 'on': True})
        self.assertEqual(response.status_code, 200)
        self.assertIn('1234', response.json()['flags']['starred'])

        response = self._post({'flag': 'starred', 'team': '1234', 'on': False})
        self.assertNotIn('1234', response.json()['flags']['starred'])

    def test_strike_is_independent_of_star(self):
        self._post({'flag': 'starred', 'team': '1234', 'on': True})
        response = self._post({'flag': 'struck', 'team': '1234', 'on': True})
        flags = response.json()['flags']
        self.assertIn('1234', flags['starred'])
        self.assertIn('1234', flags['struck'])

    def test_unknown_flag_is_rejected(self):
        response = self._post({'flag': 'sabotage', 'team': '1234', 'on': True})
        self.assertEqual(response.status_code, 400)

    def test_non_numeric_team_is_rejected(self):
        response = self._post({'flag': 'starred', 'team': 'DROP TABLE', 'on': True})
        self.assertEqual(response.status_code, 400)

    def test_flag_survives_the_team_moving_between_columns(self):
        self._post({'flag': 'struck', 'team': '1234', 'on': True})
        # Saving the board is a separate write path; it must not clear flags.
        self.client.post(
            reverse('picklist_submit') + '?comp=' + self.COMP_CODE + '&save_to_db=true',
            data=json.dumps([[], ['1234'], [], [], []]),
            content_type='application/json',
        )
        row = PickList_Data.objects.get(event=self.COMP_CODE)
        self.assertEqual([str(t) for t in row.struck], ['1234'])
        self.assertEqual([str(t) for t in row.first_pick], ['1234'])


class PicklistAccessTests(TestCase):
    """Picklist is open to every @team2073.com account, whatever their role."""

    COMP_CODE = 'testaccess2026'

    def setUp(self):
        self.client.defaults['HTTP_HOST'] = 'localhost'
        self.client.defaults['wsgi.url_scheme'] = 'https'
        self.url = reverse('picklist_submit') + f'?comp={self.COMP_CODE}'
        self.json_path = get_json_path(self.COMP_CODE)

    def tearDown(self):
        if self.json_path and self.json_path.exists():
            self.json_path.unlink()

    def _login(self, email, role='student', is_admin=False):
        session = self.client.session
        session['email'] = email
        session['role'] = role
        session['is_admin'] = is_admin
        session.save()

    def test_student_on_team_email_can_save(self):
        self._login('kid@team2073.com', role='student')
        response = self.client.post(self.url, data=json.dumps([[1], [], [], [], []]),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 200)

    def test_team_email_check_ignores_case(self):
        self._login('Kid@TEAM2073.com', role='student')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_outside_email_is_rejected(self):
        self._login('someone@gmail.com', role='strategist')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_admin_outside_the_domain_is_allowed(self):
        self._login('coach@school.org', role='teacher', is_admin=True)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)


class PicklistFourthPickTests(TestCase):
    """The board has a 4th pick column, appended after the old five lists."""

    COMP_CODE = 'testfourth2026'

    def setUp(self):
        self.client.defaults['HTTP_HOST'] = 'localhost'
        self.client.defaults['wsgi.url_scheme'] = 'https'
        session = self.client.session
        session['email'] = 'strategist@team2073.com'
        session['role'] = 'strategist'
        session.save()
        self.url = reverse('picklist_submit') + f'?comp={self.COMP_CODE}'
        self.json_path = get_json_path(self.COMP_CODE)

    def tearDown(self):
        if self.json_path and self.json_path.exists():
            self.json_path.unlink()

    def _post(self, payload):
        return self.client.post(self.url, data=json.dumps(payload), content_type='application/json')

    def test_six_lists_are_saved_with_the_fourth_pick(self):
        response = self._post([[1], [2], [3], [4], [5], [6, 7]])
        self.assertEqual(response.status_code, 200)
        row = PickList_Data.objects.get(event=self.COMP_CODE)
        self.assertEqual(row.dn_pick, [5])
        self.assertEqual(row.fourth_pick, [6, 7])
        self.assertGreater(row.updated_ms, 0)

    def test_five_lists_from_an_older_page_still_save(self):
        response = self._post([[1], [2], [], [], [9]])
        self.assertEqual(response.status_code, 200)
        row = PickList_Data.objects.get(event=self.COMP_CODE)
        self.assertEqual(row.dn_pick, [9])
        self.assertEqual(row.fourth_pick, [])

    def test_wrong_number_of_lists_is_rejected(self):
        self.assertEqual(self._post([[1]] * 4).status_code, 400)
        self.assertEqual(self._post([[1]] * 7).status_code, 400)

    def test_read_back_returns_six_lists(self):
        self._post([[1], [2], [3], [4], [5], [6]])
        body = self.client.get(self.url + '&timestamp=0').json()
        self.assertEqual(body['status'], 'updated')
        self.assertEqual(body['data'][5], [6])

    def test_board_still_saves_and_syncs_when_the_disk_is_read_only(self):
        # Vercel has no writable disk: the JSON file can't be written, so the
        # database copy has to carry both the save and the live sync.
        from unittest import mock
        with mock.patch('strategy.views.open', create=True, side_effect=OSError('read-only')):
            response = self._post([[1], [2], [], [], [], [8]])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.json_path.exists())
        body = self.client.get(self.url + '&timestamp=0').json()
        self.assertEqual(body['status'], 'updated')
        self.assertEqual(body['data'][5], [8])
        # A client that already has this version is told nothing changed.
        stamp = body['timestamp']
        again = self.client.get(self.url + f'&timestamp={stamp}').json()
        self.assertEqual(again['status'], 'no_change')

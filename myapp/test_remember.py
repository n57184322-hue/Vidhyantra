"""Stay signed in (myapp.remember): a lost session (a deploy, a changed
SECRET_KEY, a cleared session table) logs the device back in from its token,
but never after a log out, a remote log out of that device, a password change
or a disabled account."""
import json

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.test import TestCase, override_settings

from myapp import devices, remember
from myapp.models import ActiveUserSession, RememberedLogin, StoreProfile


class RememberMeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('asha', email='asha@example.com', password='first-pass-123', first_name='Asha')
        StoreProfile.objects.create(user=self.user, ai_onboarded=True, phone_verified=True, location_consent='denied')

    def log_in(self, password='first-pass-123'):
        response = self.client.post('/AI/api/login/', json.dumps({'identifier': 'asha@example.com', 'password': password}),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def signed_in(self):
        return self.client.get('/').wsgi_request.user.is_authenticated

    def lose_session(self):
        Session.objects.all().delete()

    def test_login_sets_a_hashed_httponly_token(self):
        response = self.log_in()
        cookie = response.cookies[remember.COOKIE_NAME]
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Lax')
        selector, validator = cookie.value.split(':')
        token = RememberedLogin.objects.get(user=self.user)
        self.assertEqual(token.selector, selector)
        self.assertNotIn(validator, token.validator_hash)
        self.assertEqual(token.device.session_key, self.client.session.session_key)

    def test_a_lost_session_comes_back_on_the_same_device(self):
        self.log_in()
        device_pk = ActiveUserSession.objects.get(user=self.user).pk
        logins = StoreProfile.objects.get(user=self.user).login_count
        self.lose_session()
        self.assertTrue(self.signed_in())
        device = ActiveUserSession.objects.get(user=self.user)
        self.assertEqual(device.pk, device_pk)                      # same device, not a new login
        self.assertEqual(device.session_key, self.client.session.session_key)
        self.assertEqual(StoreProfile.objects.get(user=self.user).login_count, logins)
        self.assertIsNotNone(RememberedLogin.objects.get(user=self.user).last_used_at)

    def test_a_changed_secret_key_does_not_log_people_out(self):
        self.log_in()
        with override_settings(SECRET_KEY='a-completely-different-secret-key-for-this-test'):
            self.assertTrue(self.signed_in())

    def test_log_out_ends_it(self):
        self.log_in()
        response = self.client.post('/AI/api/logout/')
        self.assertEqual(response.cookies[remember.COOKIE_NAME].value, '')
        self.assertFalse(RememberedLogin.objects.exists())
        self.assertFalse(self.signed_in())

    def test_a_device_logged_out_from_elsewhere_stays_out(self):
        self.log_in()
        cookie = self.client.cookies[remember.COOKIE_NAME].value
        devices.remove_device(self.user, ActiveUserSession.objects.get(user=self.user).pk)
        self.assertFalse(RememberedLogin.objects.exists())
        self.client.cookies[remember.COOKIE_NAME] = cookie
        self.assertFalse(self.signed_in())

    def test_a_password_change_elsewhere_stops_every_token(self):
        self.log_in()
        self.user.set_password('changed-by-admin-456')
        self.user.save()
        self.lose_session()
        self.assertFalse(self.signed_in())
        self.assertFalse(RememberedLogin.objects.exists())

    def test_changing_your_own_password_keeps_this_device_remembered(self):
        self.log_in()
        response = self.client.post('/AI/api/profile/password/', json.dumps({
            'current_password': 'first-pass-123', 'new_password': 'second-pass-789',
        }), content_type='application/json')
        self.assertEqual(response.json()['status'], 'ok', response.content)
        self.lose_session()
        self.assertTrue(self.signed_in())

    def test_a_guessed_or_tampered_cookie_is_refused_and_burns_the_token(self):
        self.log_in()
        selector = RememberedLogin.objects.get(user=self.user).selector
        self.client.cookies[remember.COOKIE_NAME] = f'{selector}:not-the-real-validator'
        self.lose_session()
        self.assertFalse(self.signed_in())
        self.assertFalse(RememberedLogin.objects.exists())

    def test_a_disabled_account_is_not_let_back_in(self):
        self.log_in()
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.lose_session()
        self.assertFalse(self.signed_in())

    def test_someone_already_logged_in_before_this_gets_a_token_once(self):
        self.client.force_login(self.user)
        RememberedLogin.objects.all().delete()       # as if logged in before tokens existed
        first = self.client.get('/')
        self.assertIn(remember.COOKIE_NAME, first.cookies)
        self.assertEqual(RememberedLogin.objects.count(), 1)
        self.assertNotIn(remember.COOKIE_NAME, self.client.get('/').cookies)   # not re-issued
        self.lose_session()
        self.assertTrue(self.signed_in())

    def test_a_login_on_a_second_device_logs_the_first_out_for_good(self):
        self.log_in()
        first_cookie = self.client.cookies[remember.COOKIE_NAME].value
        other = self.client_class()
        other.post('/AI/api/login/', json.dumps({'identifier': 'asha@example.com', 'password': 'first-pass-123'}),
                   content_type='application/json')
        self.client.cookies[remember.COOKIE_NAME] = first_cookie
        self.assertFalse(self.signed_in())   # one main device per normal account (myapp.devices)

"""stream_xc: opt-in per-user XC login in the provider redirect."""

from unittest.mock import MagicMock, patch

from django.http import HttpResponseRedirect, StreamingHttpResponse
from django.test import RequestFactory, SimpleTestCase

from apps.accounts.models import User

ACCOUNT_URL = "http://provider.example/live/acctuser/acctpass/1234.ts"
OWN_URL = "http://provider.example/live/viewer1/viewerpw1/1234.ts"


def _user(level=User.UserLevel.STANDARD, **props):
    return User(username="viewer1", user_level=level, custom_properties=props)


@patch("apps.proxy.live_proxy.views.close_old_connections")
class StreamXcOwnLoginTests(SimpleTestCase):
    def _call(self, user, provider_response=None):
        from apps.proxy.live_proxy.views import stream_xc

        channel = MagicMock(uuid="channel-uuid")
        request = RequestFactory().get("/live/viewer1/viewerpw1/5.ts")
        if provider_response is None:
            provider_response = HttpResponseRedirect(ACCOUNT_URL)
        with patch(
            "apps.proxy.live_proxy.views._xc_live_channel_or_error",
            return_value=(user, channel, None),
        ), patch(
            "apps.proxy.live_proxy.views.stream_ts",
            return_value=provider_response,
        ):
            return stream_xc(request, "viewer1", "viewerpw1", "5.ts")

    def test_opted_in_user_gets_own_login(self, _close):
        user = _user(xc_password="viewerpw1", use_own_xc_login=True)
        response = self._call(user)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], OWN_URL)

    def test_toggle_off_keeps_account_login(self, _close):
        user = _user(xc_password="viewerpw1")
        self.assertEqual(self._call(user)["Location"], ACCOUNT_URL)
        user = _user(xc_password="viewerpw1", use_own_xc_login=False)
        self.assertEqual(self._call(user)["Location"], ACCOUNT_URL)

    def test_admin_keeps_account_login(self, _close):
        user = _user(
            User.UserLevel.ADMIN, xc_password="viewerpw1", use_own_xc_login=True
        )
        self.assertEqual(self._call(user)["Location"], ACCOUNT_URL)

    def test_user_without_xc_password_keeps_account_login(self, _close):
        user = _user(use_own_xc_login=True)
        self.assertEqual(self._call(user)["Location"], ACCOUNT_URL)
        user = _user(xc_password="", use_own_xc_login=True)
        self.assertEqual(self._call(user)["Location"], ACCOUNT_URL)

    def test_url_without_login_segment_is_left_alone(self, _close):
        user = _user(xc_password="viewerpw1", use_own_xc_login=True)
        odd = HttpResponseRedirect("http://provider.example/stream/1234.ts")
        self.assertEqual(
            self._call(user, odd)["Location"], "http://provider.example/stream/1234.ts"
        )

    def test_non_redirect_response_is_returned_unchanged(self, _close):
        user = _user(xc_password="viewerpw1", use_own_xc_login=True)
        proxied = StreamingHttpResponse(iter([b"data"]))
        self.assertIs(self._call(user, proxied), proxied)

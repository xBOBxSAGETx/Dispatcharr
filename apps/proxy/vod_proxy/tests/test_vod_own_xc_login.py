"""VOD redirect and proxy: opt-in per-user XC login in the provider URL."""

from unittest.mock import MagicMock, patch

from django.http import HttpResponseRedirect, StreamingHttpResponse
from django.test import RequestFactory, SimpleTestCase

from apps.accounts.models import User

VIEWS = "apps.proxy.vod_proxy.views"
PATHS = {"movie": "movie", "episode": "series"}
ODD_URL = "http://provider.example/odd/shape.mkv"


def _account_url(path):
    return f"http://provider.example/{path}/acctuser/acctpass/99.mkv"


def _own_url(path):
    return f"http://provider.example/{path}/viewer1/viewerpw1/99.mkv"


def _user(level=User.UserLevel.STANDARD, **props):
    return User(username="viewer1", user_level=level, custom_properties=props)


def _selected(url):
    return {
        "content_obj": MagicMock(),
        "m3u_account": MagicMock(),
        "m3u_profile": MagicMock(id=1, max_streams=5),
        "current_connections": 0,
        "final_stream_url": url,
    }


def _controls():
    return {
        "toggle off": _user(xc_password="viewerpw1"),
        "admin": _user(
            User.UserLevel.ADMIN, xc_password="viewerpw1", use_own_xc_login=True
        ),
        "no xc_password": _user(use_own_xc_login=True),
    }


class _VodBase(SimpleTestCase):
    def setUp(self):
        patchers = [
            patch(f"{VIEWS}.network_access_allowed", return_value=True),
            patch(f"{VIEWS}._vod_playback_allowed", return_value=True),
            patch(f"{VIEWS}.close_old_connections"),
            patch(f"{VIEWS}._find_idle_vod_session", return_value=None),
            patch(f"{VIEWS}.check_user_stream_limits", return_value=True),
            patch("apps.m3u.utils.get_allowed_m3u_profiles", return_value=None),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _request(self, path="/proxy/vod/movie/uuid/", method="get"):
        request = getattr(RequestFactory(), method)(path, HTTP_USER_AGENT="test-agent")
        request.user = MagicMock(is_authenticated=False)
        return request


class VodRedirectOwnLoginTests(_VodBase):
    def _redirect(self, content_type, user, url):
        from apps.proxy.vod_proxy.views import stream_vod

        with patch(
            "core.models.CoreSettings.is_default_stream_profile_redirect",
            return_value=True,
        ), patch(f"{VIEWS}._select_vod_stream", return_value=_selected(url)):
            return stream_vod(
                self._request(),
                content_type=content_type,
                content_id="uuid",
                session_id=None,
                user=user,
            )

    def test_opted_in_user_gets_own_login(self):
        for content_type, path in PATHS.items():
            with self.subTest(content_type=content_type):
                user = _user(xc_password="viewerpw1", use_own_xc_login=True)
                response = self._redirect(content_type, user, _account_url(path))
                self.assertIsInstance(response, HttpResponseRedirect)
                self.assertEqual(response["Location"], _own_url(path))

    def test_toggle_off_admin_and_no_password_keep_account_login(self):
        for content_type, path in PATHS.items():
            for name, user in _controls().items():
                with self.subTest(content_type=content_type, user=name):
                    response = self._redirect(content_type, user, _account_url(path))
                    self.assertEqual(response["Location"], _account_url(path))

    def test_shape_mismatch_fails_closed_with_503(self):
        user = _user(xc_password="viewerpw1", use_own_xc_login=True)
        for content_type in PATHS:
            with self.subTest(content_type=content_type):
                response = self._redirect(content_type, user, ODD_URL)
                self.assertEqual(response.status_code, 503)
                self.assertNotIsInstance(response, HttpResponseRedirect)

    def test_shape_mismatch_still_redirects_when_not_opted_in(self):
        response = self._redirect("movie", _user(xc_password="viewerpw1"), ODD_URL)
        self.assertEqual(response["Location"], ODD_URL)


class VodProxyOwnLoginTests(_VodBase):
    def _proxy(self, content_type, user, url):
        from apps.proxy.vod_proxy.views import stream_vod

        manager = MagicMock()
        manager.stream_content_with_session.return_value = StreamingHttpResponse(
            streaming_content=iter([b"data"]), content_type="video/mp4"
        )
        with patch(f"{VIEWS}._select_vod_stream", return_value=_selected(url)), patch(
            f"{VIEWS}.MultiWorkerVODConnectionManager"
        ) as manager_cls:
            manager_cls.get_instance.return_value = manager
            response = stream_vod(
                self._request("/proxy/vod/movie/uuid/session123"),
                content_type=content_type,
                content_id="uuid",
                session_id="session123",
                user=user,
            )
        return response, manager

    def test_opted_in_user_streams_with_own_login(self):
        for content_type, path in PATHS.items():
            with self.subTest(content_type=content_type):
                user = _user(xc_password="viewerpw1", use_own_xc_login=True)
                response, manager = self._proxy(content_type, user, _account_url(path))
                self.assertIsInstance(response, StreamingHttpResponse)
                sent = manager.stream_content_with_session.call_args.kwargs
                self.assertEqual(sent["stream_url"], _own_url(path))

    def test_controls_stream_with_account_login(self):
        for name, user in _controls().items():
            with self.subTest(user=name):
                _, manager = self._proxy("movie", user, _account_url("movie"))
                sent = manager.stream_content_with_session.call_args.kwargs
                self.assertEqual(sent["stream_url"], _account_url("movie"))

    def test_shape_mismatch_fails_closed_with_503(self):
        user = _user(xc_password="viewerpw1", use_own_xc_login=True)
        response, manager = self._proxy("movie", user, ODD_URL)
        self.assertEqual(response.status_code, 503)
        manager.stream_content_with_session.assert_not_called()


class VodHeadOwnLoginTests(_VodBase):
    def _head(self, user, url):
        from apps.proxy.vod_proxy.views import head_vod

        with patch(
            "core.models.CoreSettings.is_default_stream_profile_redirect",
            return_value=True,
        ), patch(f"{VIEWS}._user_from_vod_request", return_value=user), patch(
            f"{VIEWS}._select_vod_stream", return_value=_selected(url)
        ):
            return head_vod(
                self._request(method="head"), content_type="movie", content_id="uuid")

    def test_redirect_uses_own_login_for_opted_in_user(self):
        user = _user(xc_password="viewerpw1", use_own_xc_login=True)
        response = self._head(user, _account_url("movie"))
        self.assertEqual(response["Location"], _own_url("movie"))

    def test_controls_keep_account_login(self):
        for name, user in _controls().items():
            with self.subTest(user=name):
                response = self._head(user, _account_url("movie"))
                self.assertEqual(response["Location"], _account_url("movie"))

    def test_shape_mismatch_fails_closed_with_503(self):
        user = _user(xc_password="viewerpw1", use_own_xc_login=True)
        response = self._head(user, ODD_URL)
        self.assertEqual(response.status_code, 503)

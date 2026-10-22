from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, TestCase

from apps.accounts.models import User
from apps.accounts.xc_auth import (
    XC_AUTH_CREDENTIALS,
    XC_AUTH_NETWORK,
    XC_AUTH_RATE_LIMIT,
    authorize_xc_client,
    swap_xc_login,
    uses_own_xc_login,
    xc_auth_error_response,
    xc_password_matches,
)


class XcPasswordMatchesTests(TestCase):
    def test_match(self):
        user = User(custom_properties={"xc_password": "secret"})
        self.assertTrue(xc_password_matches(user, "secret"))

    def test_mismatch(self):
        user = User(custom_properties={"xc_password": "secret"})
        self.assertFalse(xc_password_matches(user, "wrong"))

    def test_missing_xc_password(self):
        user = User(custom_properties={})
        self.assertFalse(xc_password_matches(user, "anything"))
        self.assertFalse(xc_password_matches(user, ""))

    def test_null_custom_properties(self):
        user = User(custom_properties=None)
        self.assertFalse(xc_password_matches(user, "x"))

    def test_coerces_non_string_password(self):
        user = User(custom_properties={"xc_password": "12345"})
        self.assertTrue(xc_password_matches(user, 12345))

    def test_non_ascii_password_is_a_mismatch_not_an_error(self):
        # compare_digest raises TypeError on non-ASCII str, and the supplied
        # password comes straight from the URL, so it must not escape as a 500.
        user = User(custom_properties={"xc_password": "secret"})
        self.assertFalse(xc_password_matches(user, "caf\u00e9"))

    def test_non_ascii_stored_password_still_matches_itself(self):
        user = User(custom_properties={"xc_password": "caf\u00e9"})
        self.assertTrue(xc_password_matches(user, "caf\u00e9"))
        self.assertFalse(xc_password_matches(user, "cafe"))


class UsesOwnXcLoginTests(SimpleTestCase):
    def _user(self, level=User.UserLevel.STANDARD, **props):
        return User(username="viewer", user_level=level, custom_properties=props)

    def test_opted_in_non_admin_with_password(self):
        self.assertTrue(
            uses_own_xc_login(self._user(xc_password="pw", use_own_xc_login=True))
        )

    def test_streamer_is_eligible_too(self):
        user = self._user(User.UserLevel.STREAMER, xc_password="pw", use_own_xc_login=True)
        self.assertTrue(uses_own_xc_login(user))

    def test_off_by_default(self):
        self.assertFalse(uses_own_xc_login(self._user(xc_password="pw")))
        self.assertFalse(
            uses_own_xc_login(self._user(xc_password="pw", use_own_xc_login=False))
        )

    def test_truthy_non_boolean_is_not_enough(self):
        self.assertFalse(
            uses_own_xc_login(self._user(xc_password="pw", use_own_xc_login="true"))
        )

    def test_admin_never_swaps(self):
        user = self._user(User.UserLevel.ADMIN, xc_password="pw", use_own_xc_login=True)
        self.assertFalse(uses_own_xc_login(user))

    def test_needs_xc_password(self):
        self.assertFalse(uses_own_xc_login(self._user(use_own_xc_login=True)))
        self.assertFalse(
            uses_own_xc_login(self._user(xc_password="", use_own_xc_login=True))
        )

    def test_no_user_or_no_properties(self):
        self.assertFalse(uses_own_xc_login(None))
        self.assertFalse(uses_own_xc_login(User(username="x", custom_properties=None)))


class SwapXcLoginTests(SimpleTestCase):
    def setUp(self):
        self.user = User(
            username="viewer",
            user_level=User.UserLevel.STANDARD,
            custom_properties={"xc_password": "pw", "use_own_xc_login": True},
        )

    def test_swaps_each_content_path(self):
        for path in ("live", "movie", "series"):
            url = f"http://h.example:8080/{path}/acct/secret/55.ts"
            self.assertEqual(
                swap_xc_login(url, self.user, ("live", "movie", "series")),
                f"http://h.example:8080/{path}/viewer/pw/55.ts",
            )

    def test_only_listed_paths_are_swapped(self):
        url = "http://h.example/live/acct/secret/55.ts"
        self.assertIsNone(swap_xc_login(url, self.user, ("movie", "series")))

    def test_no_login_segment_returns_none(self):
        self.assertIsNone(swap_xc_login("http://h.example/odd/55.ts", self.user, ("live",)))

    def test_password_with_regex_characters_is_taken_literally(self):
        self.user.custom_properties["xc_password"] = "a.b_c@d-e"
        out = swap_xc_login("http://h.example/live/acct/secret/55.ts", self.user, ("live",))
        self.assertEqual(out, "http://h.example/live/viewer/a.b_c@d-e/55.ts")


class XcAuthErrorResponseTests(SimpleTestCase):
    def test_network_is_403(self):
        response = xc_auth_error_response(XC_AUTH_NETWORK)
        self.assertEqual(response.status_code, 403)

    def test_credentials_are_401(self):
        response = xc_auth_error_response(XC_AUTH_CREDENTIALS)
        self.assertEqual(response.status_code, 401)

    def test_rate_limit_is_429_with_retry_after(self):
        response = xc_auth_error_response(XC_AUTH_RATE_LIMIT, retry_after=12.3)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "12")



class AuthorizeXcClientTests(SimpleTestCase):
    def setUp(self):
        self.request = RequestFactory().get("/player_api.php")

    def _user(self):
        return User(username="alice", custom_properties={"xc_password": "secret"})

    def _authorize(self, username, password, network, key="XC_API"):
        # Patch rate-limit helpers so these unit tests stay about auth order,
        # not Redis budgets (covered in test_xc_auth_rate_limit).
        with (
            patch(
                "apps.accounts.xc_auth.network_access_allowed", side_effect=network
            ),
            patch(
                "apps.accounts.xc_auth.xc_failed_auth_limited",
                return_value=(False, None),
            ),
            patch(
                "apps.accounts.xc_auth.record_xc_failed_auth", return_value=False
            ),
        ):
            return authorize_xc_client(self.request, username, password, key)

    def test_global_deny_skips_user_lookup(self):
        def deny(request, key, user=None):
            return False

        with patch("apps.accounts.xc_auth.get_xc_user") as lookup:
            user, error = self._authorize("alice", "secret", deny)

        lookup.assert_not_called()
        self.assertIsNone(user)
        self.assertEqual(error.status_code, 403)

    def test_blank_credentials_skip_lookup_when_network_allows(self):
        def allow(request, key, user=None):
            return True

        with patch("apps.accounts.xc_auth.get_xc_user") as lookup:
            user, error = self._authorize("alice", "", allow)

        lookup.assert_not_called()
        self.assertIsNone(user)
        self.assertEqual(error.status_code, 401)

    def test_user_network_deny_does_not_depend_on_password(self):
        def allow_global_only(request, key, user=None):
            return user is None

        with patch("apps.accounts.xc_auth.get_xc_user", return_value=self._user()):
            wrong, wrong_error = self._authorize("alice", "wrong", allow_global_only)
            right, right_error = self._authorize("alice", "secret", allow_global_only)

        self.assertIsNone(wrong)
        self.assertIsNone(right)
        self.assertEqual(wrong_error.status_code, 403)
        self.assertEqual(right_error.status_code, 403)

    def test_wrong_password_after_network_allows(self):
        def allow(request, key, user=None):
            return True

        with patch("apps.accounts.xc_auth.get_xc_user", return_value=self._user()):
            user, error = self._authorize("alice", "wrong", allow)

        self.assertIsNone(user)
        self.assertEqual(error.status_code, 401)

    def test_non_ascii_password_returns_401(self):
        def allow(request, key, user=None):
            return True

        with patch("apps.accounts.xc_auth.get_xc_user", return_value=self._user()):
            user, error = self._authorize("alice", "caf\u00e9", allow)

        self.assertIsNone(user)
        self.assertEqual(error.status_code, 401)

    def test_success(self):
        def allow(request, key, user=None):
            return True

        account = self._user()
        with patch("apps.accounts.xc_auth.get_xc_user", return_value=account):
            user, error = self._authorize("alice", "secret", allow, key="XC_API")

        self.assertIs(user, account)
        self.assertIsNone(error)

    def test_checks_system_then_user_network_rules(self):
        calls = []

        def track(request, key, user=None):
            calls.append(user)
            return True

        with patch("apps.accounts.xc_auth.get_xc_user", return_value=self._user()):
            user, error = self._authorize("alice", "secret", track)

        self.assertIsNone(error)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0])
        self.assertEqual(calls[1].username, "alice")
        self.assertIs(user, calls[1])

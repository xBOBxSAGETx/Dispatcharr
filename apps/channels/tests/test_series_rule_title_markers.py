"""Series rules keep matching programmes whose title carries a LIVE/NEW marker."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.channels.models import Channel, Recording
from apps.epg.models import EPGData, EPGSource, ProgramData
from core.models import CoreSettings

NEW = 'ᴺᵉʷ'  # superscript "New"
LIVE = 'ᴸᶦᵛᵉ'  # superscript "Live"
User = get_user_model()


class SeriesRuleTitleMarkerTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        source = EPGSource.objects.create(name="Test EPG", source_type="xmltv")
        self.epg = EPGData.objects.create(
            tvg_id="test.channel.1", name="Test Channel EPG", epg_source=source
        )
        Channel.objects.create(channel_number=1, name="Test Channel", epg_data=self.epg)
        CoreSettings.set_dvr_series_rules(
            [{"tvg_id": "test.channel.1", "mode": "all", "title": "Test Show"}]
        )
        CoreSettings._update_group(
            "dvr_settings", "DVR Settings",
            {"pre_offset_minutes": 0, "post_offset_minutes": 0},
        )

    def _program(self, title, hours, sub_title="Episode 1"):
        start = self.now + timedelta(hours=hours)
        return ProgramData.objects.create(
            epg=self.epg, tvg_id="test.channel.1", start_time=start,
            end_time=start + timedelta(hours=1), title=title, sub_title=sub_title,
        )

    @patch("apps.channels.tasks.prefetch_recording_artwork")
    @patch("apps.channels.signals.schedule_recording_task", return_value="t")
    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_rule_matches_marked_title_and_dedups_with_repeat(self, *_mocks):
        from apps.channels.tasks import evaluate_series_rules_impl

        self._program(f"Test Show {NEW}", 2)  # marked first airing
        self._program("Test Show", 5)  # unmarked repeat, same episode
        result = evaluate_series_rules_impl()
        self.assertEqual(result["scheduled"], 1)
        self.assertEqual(Recording.objects.count(), 1)

    @patch("apps.channels.tasks.prefetch_recording_artwork")
    @patch("apps.channels.signals.schedule_recording_task", return_value="t")
    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_rule_records_marked_title_only(self, *_mocks):
        from apps.channels.tasks import evaluate_series_rules_impl

        self._program(f"Test Show {NEW}", 2)
        result = evaluate_series_rules_impl()
        self.assertEqual(result["scheduled"], 1)

    def test_episode_key_ignores_marker(self):
        from apps.epg.title_markers import strip_title_marker

        self.assertEqual(strip_title_marker(f"Test Show {NEW}"), "Test Show")
        self.assertEqual(strip_title_marker(f"Test Show {LIVE}"), "Test Show")
        self.assertEqual(strip_title_marker("Test Show"), "Test Show")
        self.assertEqual(strip_title_marker(None), "")

    def test_preview_matches_marked_titles(self):
        self._program(f"Test Show {NEW}", 2)
        self._program(f"Test Show {LIVE}", 3, "Episode 2")
        self._program("Test Show", 4, "Episode 3")
        self._program("Test Show Extra", 5, "Episode 4")
        admin = User.objects.create_user(username="dvr_admin", password="x")
        admin.user_level = User.UserLevel.ADMIN
        admin.save()
        client = APIClient()
        client.force_authenticate(admin)
        resp = client.post(
            "/api/channels/series-rules/preview/",
            {"tvg_id": "test.channel.1", "title": "Test Show", "title_mode": "exact"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["total"], 3)

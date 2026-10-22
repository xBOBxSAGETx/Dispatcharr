"""
set-epg / batch-set-epg on auto-synced channels.

Auto channel sync rewrites ``Channel.epg_data`` on every M3U refresh, so an
EPG assigned through these endpoints was lost at the next refresh. For
auto-created channels the endpoints now store the EPG as a ChannelOverride
(the same row bulk edit writes), which sync never touches. Manual channels
keep writing ``Channel.epg_data`` directly.
"""
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.channels.models import (
    Channel,
    ChannelGroup,
    ChannelGroupM3UAccount,
    ChannelOverride,
    ChannelStream,
    Stream,
)
from apps.epg.models import EPGData, EPGSource
from apps.m3u.models import M3UAccount
from apps.m3u.tasks import sync_auto_channels


class SetEpgOverrideTests(TestCase):
    def setUp(self):
        admin = User.objects.create_superuser(
            username="admin_set_epg_override",
            password="pw",
            user_level=10,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=admin)

        source = EPGSource.objects.create(
            name="XML EPG",
            source_type="xmltv",
            url="http://example.com/epg.xml",
        )
        # The guide the user picks, and the one the provider's tvg-id maps to.
        self.user_epg = EPGData.objects.create(
            tvg_id="user.pick", name="User Pick", epg_source=source
        )
        self.provider_epg = EPGData.objects.create(
            tvg_id="provider.map", name="Provider Map", epg_source=source
        )

        self.account = M3UAccount.objects.create(
            name="Provider", server_url="http://example.com/test.m3u"
        )
        self.group = ChannelGroup.objects.create(name="Sports")
        ChannelGroupM3UAccount.objects.create(
            m3u_account=self.account,
            channel_group=self.group,
            enabled=True,
            auto_channel_sync=True,
            auto_sync_channel_start=100,
        )

    def _synced_channel(self, name, number, tvg_id, epg=None):
        stream = Stream.objects.create(
            name=name,
            url=f"http://example.com/{name.lower()}.m3u8",
            m3u_account=self.account,
            channel_group=self.group,
            tvg_id=tvg_id,
            last_seen=timezone.now(),
        )
        channel = Channel.objects.create(
            name=name,
            channel_number=number,
            channel_group=self.group,
            auto_created=True,
            auto_created_by=self.account,
            tvg_id=tvg_id,
            epg_data=epg,
        )
        ChannelStream.objects.create(channel=channel, stream=stream, order=0)
        return channel

    def _sync(self):
        # scan_start_time before the fresh streams so they count as seen.
        return sync_auto_channels(
            self.account.id,
            scan_start_time=(
                timezone.now() - timezone.timedelta(minutes=1)
            ).isoformat(),
        )

    def _effective_epg_id(self, channel):
        response = self.client.get(f"/api/channels/channels/{channel.id}/")
        self.assertEqual(response.status_code, 200)
        return response.data["effective_epg_data_id"]

    def test_set_epg_survives_sync_when_stream_has_no_epg(self):
        channel = self._synced_channel("ESPN", 100, "no.guide.for.this")

        response = self.client.post(
            f"/api/channels/channels/{channel.id}/set-epg/",
            {"epg_data_id": self.user_epg.id},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["channel"]["effective_epg_data_id"],
            self.user_epg.id,
        )

        self.assertEqual(self._sync()["status"], "ok")

        self.assertEqual(self._effective_epg_id(channel), self.user_epg.id)

    def test_set_epg_survives_sync_when_stream_maps_to_other_epg(self):
        channel = self._synced_channel("FOX", 100, "provider.map")

        self.client.post(
            f"/api/channels/channels/{channel.id}/set-epg/",
            {"epg_data_id": self.user_epg.id},
            format="json",
        )
        self.assertEqual(self._sync()["status"], "ok")

        channel.refresh_from_db()
        # Sync still owns the provider-side value ...
        self.assertEqual(channel.epg_data_id, self.provider_epg.id)
        # ... and the user's pick wins in the effective value.
        self.assertEqual(self._effective_epg_id(channel), self.user_epg.id)

    def test_batch_set_epg_mixed_synced_and_manual_channels(self):
        synced_a = self._synced_channel("ESPN", 100, "no.guide.for.this")
        synced_b = self._synced_channel("FOX", 101, "provider.map")
        manual = Channel.objects.create(
            name="Manual", channel_number=900, channel_group=self.group
        )

        response = self.client.post(
            "/api/channels/channels/batch-set-epg/",
            {
                "associations": [
                    {
                        "channel_id": synced_a.id,
                        "epg_data_id": self.user_epg.id,
                    },
                    {
                        "channel_id": synced_b.id,
                        "epg_data_id": self.user_epg.id,
                    },
                    {
                        "channel_id": manual.id,
                        "epg_data_id": self.user_epg.id,
                    },
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["channels_updated"], 3)

        self.assertEqual(self._sync()["status"], "ok")

        self.assertEqual(self._effective_epg_id(synced_a), self.user_epg.id)
        self.assertEqual(self._effective_epg_id(synced_b), self.user_epg.id)
        # Manual channel: unchanged behaviour, plain field, no override row.
        manual.refresh_from_db()
        self.assertEqual(manual.epg_data_id, self.user_epg.id)
        self.assertFalse(
            ChannelOverride.objects.filter(channel=manual).exists()
        )

    def test_set_epg_on_manual_channel_writes_channel_field(self):
        manual = Channel.objects.create(
            name="Manual", channel_number=900, channel_group=self.group
        )

        response = self.client.post(
            f"/api/channels/channels/{manual.id}/set-epg/",
            {"epg_data_id": self.user_epg.id},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        manual.refresh_from_db()
        self.assertEqual(manual.epg_data_id, self.user_epg.id)
        self.assertFalse(
            ChannelOverride.objects.filter(channel=manual).exists()
        )

    def test_set_epg_null_on_synced_channel_drops_override(self):
        channel = self._synced_channel(
            "ESPN", 100, "provider.map", epg=self.provider_epg
        )
        ChannelOverride.objects.create(channel=channel, epg_data=self.user_epg)

        response = self.client.post(
            f"/api/channels/channels/{channel.id}/set-epg/",
            {"epg_data_id": None},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        # Same as bulk edit: the override goes away. The channel's own EPG
        # is cleared too, so the guide is empty right now; the next refresh
        # fills it from the provider's mapping again.
        self.assertFalse(ChannelOverride.objects.filter(channel=channel).exists())
        self.assertIsNone(self._effective_epg_id(channel))
        self.assertEqual(self._sync()["status"], "ok")
        self.assertEqual(self._effective_epg_id(channel), self.provider_epg.id)

    def test_set_epg_null_keeps_other_overrides(self):
        channel = self._synced_channel("ESPN", 100, "no.guide.for.this")
        ChannelOverride.objects.create(
            channel=channel, name="My Name", epg_data=self.user_epg
        )

        self.client.post(
            f"/api/channels/channels/{channel.id}/set-epg/",
            {"epg_data_id": None},
            format="json",
        )

        override = ChannelOverride.objects.get(channel=channel)
        self.assertIsNone(override.epg_data_id)
        self.assertEqual(override.name, "My Name")

    def test_batch_set_epg_null_on_synced_channel_drops_override(self):
        channel = self._synced_channel("ESPN", 100, "no.guide.for.this")
        ChannelOverride.objects.create(channel=channel, epg_data=self.user_epg)

        response = self.client.post(
            "/api/channels/channels/batch-set-epg/",
            {"associations": [{"channel_id": channel.id, "epg_data_id": None}]},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ChannelOverride.objects.filter(channel=channel).exists())
        self.assertIsNone(self._effective_epg_id(channel))

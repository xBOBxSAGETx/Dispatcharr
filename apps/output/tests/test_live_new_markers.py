import base64
import xml.etree.ElementTree as ET
from datetime import timedelta
from uuid import uuid4

from django.db import connection
from django.test import Client, RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.channels.models import Channel, ChannelGroup
from apps.epg.models import EPGData, EPGSource, ProgramData
from apps.output.tests.test_views import OutputEndpointTestMixin, _response_text

LIVE = 'ᴸᶦᵛᵉ'  # superscript "Live"
NEW = 'ᴺᵉʷ'  # superscript "New"

SPORTS = ['NBA Basketball', 'NHL Hockey']

# (title, stored custom_properties flags)
PROGRAMMES = [
    ('NBA Basketball', {}),  # sports, live
    ('NHL Hockey', {'previously_shown': True}),  # sports repeat
    ('NHL Hockey', {'new': True}),  # sports, not a repeat: LIVE wins over NEW
    ('Drama Series', {'new': True}),  # new episode
    ('Drama Series', {'new': True, 'previously_shown': True}),  # new + repeat
    ('News Hour', {}),  # plain
    ('Drama Series', {'previously_shown': True}),  # plain repeat
    ('nba basketball', {}),  # sports match is case-sensitive: not sports
    ('NBA Basketball Tonight', {}),  # sports match is whole-title: not sports
    ('Late Show', {'new': True}),  # NEW, but not in the sports list
    (f'Already {NEW}', {'new': True}),  # feed already carries the marker
    (f'NBA Basketball {LIVE}', {}),  # ditto, a sports-looking title
]


class LiveNewMarkerOutputTestBase(OutputEndpointTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.group = ChannelGroup.objects.create(name=f"Marker Group {uuid4().hex[:8]}")
        self.profile = self._create_isolated_profile("markers")
        self.base = timezone.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    def _epg_url(self):
        base = reverse("output:epg_endpoint", kwargs={"profile_name": self.profile.name})
        return f"{base}?tvg_id_source=tvg_id&days=0&prev_days=0"

    def _source(self, enabled=True, sports=None, name=None, source_type="xmltv"):
        cp = {}
        if enabled is not None:
            cp["add_live_new_markers"] = enabled
        if sports is not None:
            cp["live_marker_sports_titles"] = sports
        return EPGSource.objects.create(
            name=name or f"Marker {uuid4().hex[:8]}",
            source_type=source_type,
            custom_properties=cp or None,
        )

    def _channel_with_programmes(self, source, tvg, number, rows=PROGRAMMES):
        epg = EPGData.objects.create(epg_source=source, tvg_id=tvg, name=tvg)
        self._add_channel_to_profile(
            self.profile, self.group, channel_number=number, name=tvg, tvg_id=tvg, epg_data=epg
        )
        for i, (title, flags) in enumerate(rows):
            start = self.base + timedelta(hours=i)
            ProgramData.objects.create(
                epg=epg,
                start_time=start,
                end_time=start + timedelta(minutes=30),
                title=title,
                tvg_id=tvg,
                custom_properties=dict(flags) or None,
            )
        return epg

    def _served_titles(self, tvg):
        root = ET.fromstring(_response_text(self.client.get(self._epg_url())))
        return [
            p.findtext("title") for p in root.findall("programme") if p.get("channel") == tvg
        ]


class XmltvOutputMarkerTests(LiveNewMarkerOutputTestBase):
    def _expected_on(self):
        return [
            f'NBA Basketball {LIVE}',
            'NHL Hockey',
            f'NHL Hockey {LIVE}',
            f'Drama Series {NEW}',
            f'Drama Series {NEW}',
            'News Hour',
            'Drama Series',
            'nba basketball',
            'NBA Basketball Tonight',
            f'Late Show {NEW}',
            f'Already {NEW}',
            f'NBA Basketball {LIVE}',
        ]

    def test_live_and_new_markers_in_xmltv(self):
        self._channel_with_programmes(self._source(sports=SPORTS), "m.one", 1.0)
        self.assertEqual(self._served_titles("m.one"), self._expected_on())

    def test_live_marker_for_sports_title_that_is_not_a_repeat(self):
        rows = [('NBA Basketball', {}), ('NHL Hockey', {'new': True})]
        self._channel_with_programmes(self._source(sports=SPORTS), "m.live", 1.0, rows)
        self.assertEqual(
            self._served_titles("m.live"), [f'NBA Basketball {LIVE}', f'NHL Hockey {LIVE}']
        )

    def test_sports_repeat_is_unchanged(self):
        rows = [('NBA Basketball', {'previously_shown': True})]
        self._channel_with_programmes(self._source(sports=SPORTS), "m.rep", 1.0, rows)
        self.assertEqual(self._served_titles("m.rep"), ['NBA Basketball'])

    def test_new_marker_for_non_sports_title(self):
        rows = [('Drama Series', {'new': True}), ('Drama Series', {})]
        self._channel_with_programmes(self._source(sports=SPORTS), "m.new", 1.0, rows)
        self.assertEqual(self._served_titles("m.new"), [f'Drama Series {NEW}', 'Drama Series'])

    def test_new_marker_without_a_sports_list(self):
        rows = [('Drama Series', {'new': True})]
        self._channel_with_programmes(self._source(sports=None), "m.nolist", 1.0, rows)
        self.assertEqual(self._served_titles("m.nolist"), [f'Drama Series {NEW}'])

    def test_title_already_ending_in_a_marker_is_not_marked_twice(self):
        rows = [(f'Drama Series {NEW}', {'new': True}), (f'NBA Basketball {LIVE}', {})]
        self._channel_with_programmes(self._source(sports=SPORTS), "m.twice", 1.0, rows)
        self.assertEqual(
            self._served_titles("m.twice"), [f'Drama Series {NEW}', f'NBA Basketball {LIVE}']
        )

    def test_sports_match_is_whole_title_and_case_sensitive(self):
        rows = [
            ('nba basketball', {}),
            ('NBA Basketball Tonight', {}),
            ('NBA', {}),
            ('NBA Basketball', {}),
        ]
        self._channel_with_programmes(self._source(sports=SPORTS), "m.exact", 1.0, rows)
        self.assertEqual(
            self._served_titles("m.exact"),
            ['nba basketball', 'NBA Basketball Tonight', 'NBA', f'NBA Basketball {LIVE}'],
        )

    def test_setting_off_leaves_titles_unchanged(self):
        self._channel_with_programmes(
            self._source(enabled=False, sports=SPORTS), "m.off", 1.0
        )
        self.assertEqual(self._served_titles("m.off"), [t for t, _ in PROGRAMMES])

    def test_setting_absent_leaves_titles_unchanged(self):
        self._channel_with_programmes(self._source(enabled=None), "m.absent", 1.0)
        self.assertEqual(self._served_titles("m.absent"), [t for t, _ in PROGRAMMES])

    def test_setting_applies_per_source(self):
        on = self._source(sports=SPORTS)
        off = self._source(enabled=False, sports=SPORTS)
        rows = [('NBA Basketball', {}), ('Drama Series', {'new': True})]
        self._channel_with_programmes(on, "m.on", 1.0, rows)
        self._channel_with_programmes(off, "m.other", 2.0, rows)
        self.assertEqual(
            self._served_titles("m.on"), [f'NBA Basketball {LIVE}', f'Drama Series {NEW}']
        )
        self.assertEqual(self._served_titles("m.other"), ['NBA Basketball', 'Drama Series'])

    def test_channel_sharing_an_epg_gets_the_marker_on_each_copy(self):
        source = self._source(sports=SPORTS)
        epg = self._channel_with_programmes(source, "m.shared", 1.0, [('Drama Series', {'new': True})])
        self._add_channel_to_profile(
            self.profile, self.group, channel_number=2.0, name="shared 2", tvg_id="m.shared2", epg_data=epg
        )
        root = ET.fromstring(_response_text(self.client.get(self._epg_url())))
        titles = sorted(p.findtext("title") for p in root.findall("programme"))
        self.assertEqual(titles, [f'Drama Series {NEW}', f'Drama Series {NEW}'])

    def test_stored_title_is_unchanged_after_output(self):
        epg = self._channel_with_programmes(self._source(sports=SPORTS), "m.stored", 1.0)
        before = list(
            ProgramData.objects.filter(epg=epg).order_by("start_time").values_list("title", "custom_properties")
        )
        self.assertEqual(self._served_titles("m.stored"), self._expected_on())
        after = list(
            ProgramData.objects.filter(epg=epg).order_by("start_time").values_list("title", "custom_properties")
        )
        self.assertEqual(before, after)
        self.assertEqual([t for t, _ in after], [t for t, _ in PROGRAMMES])

    def test_dummy_epg_programmes_are_unchanged(self):
        source = self._source(sports=SPORTS, source_type="dummy")
        epg = EPGData.objects.create(epg_source=source, tvg_id="m.dummy", name="Dummy")
        self._add_channel_to_profile(
            self.profile, self.group, channel_number=1.0, name="Dummy", tvg_id="m.dummy", epg_data=epg
        )
        root = ET.fromstring(_response_text(self.client.get(self._epg_url())))
        titles = [p.findtext("title") for p in root.findall("programme")]
        self.assertTrue(titles)
        for title in titles:
            self.assertNotIn(LIVE, title)
            self.assertNotIn(NEW, title)

    def test_markers_add_no_queries(self):
        """The per-source setting rides on the channel query; no per-programme lookup."""
        on = self._source(sports=SPORTS)
        off = self._source(enabled=False, sports=SPORTS)
        self._channel_with_programmes(on, "q.on", 1.0)
        self._channel_with_programmes(off, "q.off", 2.0)
        self.client.get(self._epg_url())  # warm content types etc.
        with CaptureQueriesContext(connection) as with_markers:
            _response_text(self.client.get(self._epg_url()))
        on.custom_properties = {}
        on.save(update_fields=["custom_properties"])
        with CaptureQueriesContext(connection) as without_markers:
            _response_text(self.client.get(self._epg_url()))
        self.assertEqual(len(with_markers), len(without_markers))


class XcJsonMarkerTests(OutputEndpointTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username=f"xc-marker-{uuid4().hex[:8]}",
            password="pass",
            user_level=10,
            custom_properties={"xc_password": "xcpass"},
        )
        group = ChannelGroup.objects.create(name=f"G {uuid4().hex[:8]}")
        self.source = EPGSource.objects.create(
            name=f"XC {uuid4().hex[:8]}",
            source_type="xmltv",
            custom_properties={"add_live_new_markers": True, "live_marker_sports_titles": SPORTS},
        )
        epg = EPGData.objects.create(epg_source=self.source, tvg_id="xc.m", name="XC")
        self.channel = Channel.objects.create(
            name="XC Marker Ch", channel_number=1, channel_group=group, user_level=0, epg_data=epg
        )
        now = timezone.now()
        for i, (title, flags) in enumerate(
            [('NBA Basketball', {}), ('Drama Series', {'new': True}), ('News Hour', {})]
        ):
            start = now + timedelta(hours=i)
            ProgramData.objects.create(
                epg=epg, start_time=start, end_time=start + timedelta(minutes=30),
                title=title, custom_properties=flags or None,
            )

    def _titles(self, action):
        from apps.output.views import xc_get_epg

        request = self.factory.get(
            "/player_api.php", {"action": action, "stream_id": str(self.channel.id)}
        )
        listings = xc_get_epg(request, self.user)["epg_listings"]
        return [base64.b64decode(item["title"]).decode() for item in listings]

    def test_short_epg_carries_the_marker(self):
        self.assertEqual(
            self._titles("get_short_epg"),
            [f'NBA Basketball {LIVE}', f'Drama Series {NEW}', 'News Hour'],
        )

    def test_simple_data_table_carries_the_marker(self):
        self.assertEqual(
            self._titles("get_simple_data_table"),
            [f'NBA Basketball {LIVE}', f'Drama Series {NEW}', 'News Hour'],
        )

    def test_setting_off_is_unchanged(self):
        self.source.custom_properties = {"add_live_new_markers": False}
        self.source.save(update_fields=["custom_properties"])
        self.assertEqual(
            self._titles("get_short_epg"), ['NBA Basketball', 'Drama Series', 'News Hour']
        )

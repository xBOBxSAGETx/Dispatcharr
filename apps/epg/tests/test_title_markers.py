import os
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.channels.models import Channel
from apps.epg.models import EPGSource, EPGData, ProgramData
from apps.epg.tasks import parse_programs_for_source, parse_programs_for_tvg_id

LIVE = 'ᴸᶦᵛᵉ'  # superscript "Live"
NEW = 'ᴺᵉʷ'  # superscript "New"

CHANNEL = 'marker.channel'

# (title, extra child elements)
PROGRAMMES = [
    ('NBA Basketball', ''),  # sports, live
    ('NHL Hockey', '<previously-shown/>'),  # sports repeat
    ('NHL Hockey', '<new/>'),  # sports, not a repeat: LIVE wins over NEW
    ('Drama Series', '<new/>'),  # new episode
    ('Drama Series', '<new/><previously-shown/>'),  # new + previously shown
    ('News Hour', ''),  # plain
    ('Drama Series', '<previously-shown/>'),  # plain repeat
]


def _xmltv_file(rows, start, stop):
    programmes = ''.join(
        f'  <programme start="{start(i)}" stop="{stop(i)}" channel="{CHANNEL}">\n'
        f'    <title>{title}</title>\n'
        f'    {extra}\n'
        f'  </programme>\n'
        for i, (title, extra) in enumerate(rows)
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<tv generator-info-name="test">\n{programmes}</tv>\n'
    )
    handle = tempfile.NamedTemporaryFile(
        mode='w', suffix='.xml', delete=False, encoding='utf-8'
    )
    handle.write(body)
    handle.close()
    return handle.name


class _MarkerFixtureMixin:
    """Shared fixture; subclasses provide ``_parse`` for one import path."""

    def setUp(self):
        self.source = EPGSource.objects.create(
            name='Marker Test', source_type='xmltv'
        )
        self.epg = EPGData.objects.create(
            epg_source=self.source, tvg_id=CHANNEL, name='Marker Channel'
        )
        Channel.objects.create(
            channel_number=1, name='Marker Channel', epg_data=self.epg
        )
        base = timezone.now().replace(minute=0, second=0, microsecond=0)

        def start(i):
            return (base + timedelta(hours=i + 1)).strftime('%Y%m%d%H%M%S +0000')

        def stop(i):
            return (base + timedelta(hours=i + 1, minutes=30)).strftime(
                '%Y%m%d%H%M%S +0000'
            )

        self.xml_path = _xmltv_file(PROGRAMMES, start, stop)
        self.source.file_path = self.xml_path
        self.source.save(update_fields=['file_path'])

    def tearDown(self):
        if os.path.exists(self.xml_path):
            os.unlink(self.xml_path)

    def _enable(self, **extra):
        self.source.custom_properties = {
            'add_live_new_markers': True,
            'live_marker_sports_titles': ['NBA Basketball', 'NHL Hockey'],
            **extra,
        }
        self.source.save(update_fields=['custom_properties'])

    def _titles(self):
        return list(
            ProgramData.objects.filter(epg=self.epg)
            .order_by('start_time')
            .values_list('title', flat=True)
        )

    def _expected_on(self):
        return [
            f'NBA Basketball {LIVE}',
            'NHL Hockey',
            f'NHL Hockey {LIVE}',
            f'Drama Series {NEW}',
            f'Drama Series {NEW}',
            'News Hour',
            'Drama Series',
        ]

    def _expected_off(self):
        return [title for title, _ in PROGRAMMES]

    def test_markers_added_when_enabled(self):
        self._enable()
        self._parse()
        self.assertEqual(self._titles(), self._expected_on())

    def test_reparse_does_not_double_the_marker(self):
        self._enable()
        self._parse()
        self._parse()
        self.assertEqual(self._titles(), self._expected_on())

    def test_off_by_default_leaves_titles_unchanged(self):
        self._parse()
        self.assertEqual(self._titles(), self._expected_off())

    def test_explicitly_off_leaves_titles_unchanged(self):
        self._enable(add_live_new_markers=False)
        self._parse()
        self.assertEqual(self._titles(), self._expected_off())

    def test_new_flag_still_stored_in_custom_properties(self):
        self._enable()
        self._parse()
        program = ProgramData.objects.filter(
            epg=self.epg, title=f'Drama Series {NEW}'
        ).first()
        self.assertTrue(program.custom_properties.get('new'))


class MarkersParseProgramsForSourceTests(_MarkerFixtureMixin, TestCase):
    def _parse(self):
        with patch('apps.epg.tasks.log_system_event'), patch(
            'apps.epg.tasks.send_epg_update'
        ):
            parse_programs_for_source(self.source)


class MarkersParseProgramsForTvgIdTests(_MarkerFixtureMixin, TestCase):
    def setUp(self):
        self._lock_patches = [
            patch('apps.epg.tasks.is_task_lock_held', return_value=False),
            patch('apps.epg.tasks.acquire_task_lock', return_value=True),
            patch('apps.epg.tasks.release_task_lock'),
        ]
        for p in self._lock_patches:
            p.start()
        super().setUp()

    def tearDown(self):
        for p in self._lock_patches:
            p.stop()
        super().tearDown()

    def _parse(self):
        parse_programs_for_tvg_id(self.epg.id)


class ApplyLiveNewMarkerTests(TestCase):
    def test_already_marked_title_is_not_marked_again(self):
        from apps.epg.tasks import apply_live_new_marker

        sports = {'NBA Basketball'}
        once = apply_live_new_marker('NBA Basketball', sports, {})
        self.assertEqual(once, f'NBA Basketball {LIVE}')
        # a feed that already ends in the marker is left alone
        self.assertEqual(
            apply_live_new_marker(f'Drama Series {NEW}', sports, {'new': True}),
            f'Drama Series {NEW}',
        )

    def test_marker_survives_the_title_length_limit(self):
        from apps.epg.tasks import apply_live_new_marker

        marked = apply_live_new_marker('A' * 300, {'A' * 300}, {}, 255)
        self.assertEqual(len(marked), 255)
        self.assertTrue(marked.endswith(f' {LIVE}'))
        # a short title is not shortened
        self.assertEqual(
            apply_live_new_marker('Short', {'Short'}, {}, 255), f'Short {LIVE}'
        )

    def test_none_sports_titles_means_feature_off(self):
        from apps.epg.tasks import apply_live_new_marker

        self.assertEqual(
            apply_live_new_marker('Drama Series', None, {'new': True}),
            'Drama Series',
        )

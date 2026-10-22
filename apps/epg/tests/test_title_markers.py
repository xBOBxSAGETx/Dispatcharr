from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.epg.models import EPGSource
from apps.epg.title_markers import (
    LIVE_TITLE_MARKER,
    NEW_TITLE_MARKER,
    apply_live_new_marker,
    get_live_new_marker_sports_titles,
)

LIVE = 'ᴸᶦᵛᵉ'  # superscript "Live"
NEW = 'ᴺᵉʷ'  # superscript "New"
SPORTS = frozenset({'NBA Basketball', 'NHL Hockey'})


class MarkerConstantsTests(SimpleTestCase):
    def test_marker_strings(self):
        self.assertEqual(LIVE_TITLE_MARKER, LIVE)
        self.assertEqual(NEW_TITLE_MARKER, NEW)


class ApplyLiveNewMarkerTests(SimpleTestCase):
    def test_sports_title_gets_live(self):
        self.assertEqual(
            apply_live_new_marker('NBA Basketball', SPORTS, {}), f'NBA Basketball {LIVE}'
        )

    def test_sports_title_with_new_flag_still_gets_live(self):
        self.assertEqual(
            apply_live_new_marker('NHL Hockey', SPORTS, {'new': True}), f'NHL Hockey {LIVE}'
        )

    def test_sports_repeat_is_unchanged(self):
        self.assertEqual(
            apply_live_new_marker('NBA Basketball', SPORTS, {'previously_shown': True}),
            'NBA Basketball',
        )

    def test_new_flag_gets_new(self):
        self.assertEqual(
            apply_live_new_marker('Drama Series', SPORTS, {'new': True}), f'Drama Series {NEW}'
        )

    def test_new_and_previously_shown_still_gets_new(self):
        self.assertEqual(
            apply_live_new_marker('Drama Series', SPORTS, {'new': True, 'previously_shown': True}),
            f'Drama Series {NEW}',
        )

    def test_no_flags_is_unchanged(self):
        self.assertEqual(apply_live_new_marker('News Hour', SPORTS, {}), 'News Hour')
        self.assertEqual(apply_live_new_marker('News Hour', SPORTS, None), 'News Hour')

    def test_already_marked_title_is_not_marked_twice(self):
        self.assertEqual(
            apply_live_new_marker(f'NBA Basketball {LIVE}', SPORTS, {}),
            f'NBA Basketball {LIVE}',
        )
        self.assertEqual(
            apply_live_new_marker(f'Drama Series {NEW}', SPORTS, {'new': True}),
            f'Drama Series {NEW}',
        )
        # either marker blocks the other one
        self.assertEqual(
            apply_live_new_marker(f'Drama Series {NEW}', {'Drama Series'}, {}),
            f'Drama Series {NEW}',
        )

    def test_sports_match_is_whole_title_and_case_sensitive(self):
        self.assertEqual(apply_live_new_marker('nba basketball', SPORTS, {}), 'nba basketball')
        self.assertEqual(
            apply_live_new_marker('NBA Basketball Tonight', SPORTS, {}), 'NBA Basketball Tonight'
        )
        self.assertEqual(apply_live_new_marker('NBA', SPORTS, {}), 'NBA')

    def test_none_sports_titles_means_feature_off(self):
        self.assertEqual(
            apply_live_new_marker('Drama Series', None, {'new': True}), 'Drama Series'
        )

    def test_empty_title_is_unchanged(self):
        self.assertEqual(apply_live_new_marker('', SPORTS, {'new': True}), '')
        self.assertIsNone(apply_live_new_marker(None, SPORTS, {'new': True}))


class GetSportsTitlesTests(SimpleTestCase):
    def _source(self, cp):
        return EPGSource(name='x', source_type='xmltv', custom_properties=cp)

    def test_off_when_absent_or_false(self):
        self.assertIsNone(get_live_new_marker_sports_titles(self._source(None)))
        self.assertIsNone(get_live_new_marker_sports_titles(self._source({})))
        self.assertIsNone(
            get_live_new_marker_sports_titles(self._source({'add_live_new_markers': False}))
        )

    def test_on_without_list_is_empty_set(self):
        self.assertEqual(
            get_live_new_marker_sports_titles(self._source({'add_live_new_markers': True})),
            frozenset(),
        )

    def test_on_with_list(self):
        cp = {'add_live_new_markers': True, 'live_marker_sports_titles': ['A', 'B']}
        self.assertEqual(
            get_live_new_marker_sports_titles(self._source(cp)), frozenset({'A', 'B'})
        )


class MarkerSettingInvalidatesXmltvCacheTests(TestCase):
    """The XMLTV chunk cache holds titles, so saving the marker keys must retire it."""

    def setUp(self):
        self.source = EPGSource.objects.create(
            name='Marker Cache', source_type='xmltv', custom_properties={'other': 1}
        )
        patcher = patch('apps.output.streaming_chunk_cache.invalidate_epg_chunk_cache')
        self.invalidate = patcher.start()
        self.addCleanup(patcher.stop)

    def _save(self, cp, **kwargs):
        self.invalidate.reset_mock()
        self.source.custom_properties = cp
        self.source.save(**kwargs)

    def test_turning_the_marker_on_invalidates(self):
        self._save({'other': 1, 'add_live_new_markers': True})
        self.invalidate.assert_called_once()

    def test_turning_the_marker_on_via_update_fields_invalidates(self):
        self._save({'add_live_new_markers': True}, update_fields=['custom_properties'])
        self.invalidate.assert_called_once()

    def test_changing_the_sports_titles_invalidates(self):
        self._save({'add_live_new_markers': True, 'live_marker_sports_titles': ['A']})
        self._save({'add_live_new_markers': True, 'live_marker_sports_titles': ['A', 'B']})
        self.invalidate.assert_called_once()

    def test_turning_the_marker_off_invalidates(self):
        self._save({'add_live_new_markers': True})
        self._save({'add_live_new_markers': False})
        self.invalidate.assert_called_once()

    def test_unrelated_save_does_not_invalidate(self):
        self._save({'other': 2})
        self.invalidate.assert_not_called()

    def test_status_only_save_does_not_invalidate(self):
        self.source.last_message = 'hello'
        self.invalidate.reset_mock()
        self.source.save(update_fields=['last_message'])
        self.invalidate.assert_not_called()

    def test_sports_titles_while_marker_is_off_do_not_invalidate(self):
        self._save({'live_marker_sports_titles': ['A']})
        self.invalidate.assert_not_called()

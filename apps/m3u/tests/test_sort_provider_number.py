"""
Auto-sync "Provider number" channel sort (channel_sort_order = "stream_chno").

Streams are created with ids that are scrambled relative to their provider
number (Stream.stream_chno). The group uses Fixed numbering, so channel numbers
are handed out from the group's own start number in iteration order. The sort
decides which stream gets which number.
"""
from django.test import TestCase
from django.utils import timezone

from apps.channels.models import (
    Channel,
    ChannelGroup,
    ChannelGroupM3UAccount,
    Stream,
)
from apps.m3u.models import M3UAccount
from apps.m3u.tasks import sync_auto_channels

START = 100


def _setup(custom_properties):
    account = M3UAccount.objects.create(
        name="Provider", server_url="http://example.com/test.m3u"
    )
    group = ChannelGroup.objects.create(name="Sports")
    ChannelGroupM3UAccount.objects.create(
        m3u_account=account,
        channel_group=group,
        enabled=True,
        auto_channel_sync=True,
        auto_sync_channel_start=START,
        custom_properties=custom_properties,
    )
    return account, group


def _stream(account, group, name, stream_chno=None):
    return Stream.objects.create(
        name=name,
        url=f"http://example.com/{name}.m3u8",
        m3u_account=account,
        channel_group=group,
        tvg_id=name.lower(),
        stream_chno=stream_chno,
        last_seen=timezone.now(),
    )


def _sync(account):
    return sync_auto_channels(
        account.id,
        scan_start_time=(timezone.now() - timezone.timedelta(minutes=1)).isoformat(),
    )


def _names_by_number(account):
    """(channel number, name) pairs ordered by the number each channel got."""
    rows = Channel.objects.filter(
        auto_created=True, auto_created_by=account
    ).order_by("channel_number")
    return [(c.channel_number, c.name) for c in rows]


def _names(account):
    return [name for _, name in _names_by_number(account)]


class ProviderNumberSortTests(TestCase):
    def test_provider_number_sort_follows_stream_chno_from_fixed_start(self):
        account, group = _setup({"channel_sort_order": "stream_chno"})
        # Creation order (= id order) is B, D, A, C; provider order is A, B, C, D.
        _stream(account, group, "B", stream_chno=20)
        _stream(account, group, "D", stream_chno=40)
        _stream(account, group, "A", stream_chno=10)
        _stream(account, group, "C", stream_chno=30)

        self.assertEqual(_sync(account)["status"], "ok")

        self.assertEqual(
            _names_by_number(account),
            [(100.0, "A"), (101.0, "B"), (102.0, "C"), (103.0, "D")],
        )

    def test_streams_without_provider_number_go_last_in_id_order(self):
        account, group = _setup({"channel_sort_order": "stream_chno"})
        _stream(account, group, "NoNum1")
        _stream(account, group, "Two", stream_chno=2)
        _stream(account, group, "NoNum2")
        _stream(account, group, "One", stream_chno=1)

        _sync(account)

        self.assertEqual(_names(account), ["One", "Two", "NoNum1", "NoNum2"])

    def test_equal_provider_numbers_break_ties_by_id(self):
        account, group = _setup({"channel_sort_order": "stream_chno"})
        _stream(account, group, "Second", stream_chno=5)
        _stream(account, group, "First", stream_chno=5)
        _stream(account, group, "Zero", stream_chno=1)

        _sync(account)

        self.assertEqual(_names(account), ["Zero", "Second", "First"])

    def test_reverse_puts_highest_provider_number_first_nulls_still_last(self):
        account, group = _setup(
            {"channel_sort_order": "stream_chno", "channel_sort_reverse": True}
        )
        _stream(account, group, "High", stream_chno=9)
        _stream(account, group, "NoNum")
        _stream(account, group, "Low", stream_chno=1)

        _sync(account)

        self.assertEqual(_names(account), ["High", "Low", "NoNum"])

    def test_sort_applies_after_include_regex_filter(self):
        # An include regex turns the queryset into a list; the sort must
        # still follow stream_chno there.
        account, group = _setup(
            {"channel_sort_order": "stream_chno", "name_match_regex": "^Keep"}
        )
        _stream(account, group, "Keep B", stream_chno=20)
        _stream(account, group, "Drop", stream_chno=15)
        _stream(account, group, "Keep NoNum")
        _stream(account, group, "Keep A", stream_chno=10)

        _sync(account)

        self.assertEqual(
            _names_by_number(account),
            [(100.0, "Keep A"), (101.0, "Keep B"), (102.0, "Keep NoNum")],
        )

    def test_reverse_with_include_regex_keeps_nulls_last(self):
        account, group = _setup(
            {
                "channel_sort_order": "stream_chno",
                "channel_sort_reverse": True,
                "name_match_regex": "^Keep",
            }
        )
        _stream(account, group, "Keep High", stream_chno=9)
        _stream(account, group, "Keep NoNum")
        _stream(account, group, "Keep Low", stream_chno=1)

        _sync(account)

        self.assertEqual(_names(account), ["Keep High", "Keep Low", "Keep NoNum"])


class ExistingSortOptionsUnchangedTests(TestCase):
    """Controls: the other sort options ignore stream_chno as before."""

    def _scrambled(self, account, group):
        _stream(account, group, "B", stream_chno=10)
        _stream(account, group, "C", stream_chno=30)
        _stream(account, group, "A", stream_chno=20)

    def test_default_provider_order_is_id_order(self):
        account, group = _setup({})
        self._scrambled(account, group)
        _sync(account)
        self.assertEqual(_names(account), ["B", "C", "A"])

    def test_name_sort_unchanged(self):
        account, group = _setup({"channel_sort_order": "name"})
        self._scrambled(account, group)
        _sync(account)
        self.assertEqual(_names(account), ["A", "B", "C"])

    def test_tvg_id_sort_unchanged(self):
        account, group = _setup({"channel_sort_order": "tvg_id"})
        self._scrambled(account, group)
        _sync(account)
        self.assertEqual(_names(account), ["A", "B", "C"])

    def test_unknown_sort_value_still_falls_back_to_id_order(self):
        account, group = _setup({"channel_sort_order": "bogus"})
        self._scrambled(account, group)
        _sync(account)
        self.assertEqual(_names(account), ["B", "C", "A"])

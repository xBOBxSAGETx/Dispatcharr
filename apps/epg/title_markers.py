"""Unicode superscript markers appended to programme titles.

An EPG source with "Add LIVE/NEW marker to titles" enabled gets a space plus
one of these appended to a programme title at import. Anything that matches or
groups programmes by title (DVR series rules, the rule preview) goes through
the helpers here so a marked title still counts as the same show.
"""
from django.db.models import Q

LIVE_TITLE_MARKER = 'ᴸᶦᵛᵉ'
NEW_TITLE_MARKER = 'ᴺᵉʷ'
TITLE_MARKERS = (LIVE_TITLE_MARKER, NEW_TITLE_MARKER)


def strip_title_marker(title):
    """Return ``title`` without a trailing space-plus-marker, if it has one."""
    title = title or ''
    for marker in TITLE_MARKERS:
        suffix = f' {marker}'
        if title.endswith(suffix):
            return title[:-len(suffix)]
    return title


def exact_title_q(title, field='title'):
    """Case-insensitive exact title match that also accepts the marked forms."""
    q = Q(**{f'{field}__iexact': title})
    for marker in TITLE_MARKERS:
        q |= Q(**{f'{field}__iexact': f'{title} {marker}'})
    return q

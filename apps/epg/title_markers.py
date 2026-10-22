"""LIVE/NEW title markers added to programme titles when the guide is written out.

An EPG source with "Add LIVE/NEW marker to titles" enabled gets a space plus a
Unicode superscript word appended to the ``<title>`` of its programmes in the
XMLTV (and XC JSON) output. Stored ``ProgramData.title`` values are never
changed, so everything that reads the database (DVR rules, recordings, the
in-app guide) keeps seeing the feed's own title.
"""

LIVE_TITLE_MARKER = 'ᴸᶦᵛᵉ'
NEW_TITLE_MARKER = 'ᴺᵉʷ'


def get_live_new_marker_sports_titles(epg_source):
    """Return the sports titles for the LIVE/NEW marker, or None when it is off.

    Stored in ``EPGSource.custom_properties``: ``add_live_new_markers`` turns the
    feature on and ``live_marker_sports_titles`` lists the programme titles that
    count as sports (the XMLTV feed has no sports flag of its own).
    """
    cp = getattr(epg_source, 'custom_properties', None) or {}
    if not cp.get('add_live_new_markers'):
        return None
    return frozenset(cp.get('live_marker_sports_titles') or [])


def apply_live_new_marker(title, sports_titles, custom_props):
    """Return ``title`` with a LIVE or NEW marker appended, or unchanged.

    A sports title (whole-title, case-sensitive match against ``sports_titles``)
    gets LIVE unless the programme is flagged ``previously_shown``; any other
    title gets NEW when the programme is flagged ``new``. A title that already
    ends in a marker is never marked twice. ``sports_titles`` is None when the
    source has the feature off.
    """
    if sports_titles is None or not title:
        return title
    if title.endswith((LIVE_TITLE_MARKER, NEW_TITLE_MARKER)):
        return title
    props = custom_props or {}
    if title in sports_titles:
        marker = None if props.get('previously_shown') else LIVE_TITLE_MARKER
    else:
        marker = NEW_TITLE_MARKER if props.get('new') else None
    if marker is None:
        return title
    return f'{title} {marker}'

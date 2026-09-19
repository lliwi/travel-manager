"""Timezone handling for itinerary instants.

The specification requires connection margins computed in UTC while respecting
each endpoint's own timezone (section 5.3), and requires detecting timezone
shifts that affect planning (section 2.4). A single datetime column cannot serve
both needs, so every instant on the itinerary is stored as a *triple*:

===================  ===========================================================
``<prefix>_local``   naive datetime, exactly what the ticket says
``<prefix>_tz``      IANA timezone name, e.g. ``Europe/Madrid``
``<prefix>_utc``     tz-aware UTC instant, derived -- the only column compared
===================  ===========================================================

``set_instant`` is the single place that derives ``_utc`` from ``_local`` and
``_tz``. Nothing else in the codebase may write a ``_utc`` column, which is what
keeps daylight-saving transitions correct everywhere at once.
"""
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = UTC

#: Fallback when a timezone cannot be resolved. Callers should record low
#: confidence rather than silently trusting this.
FALLBACK_TIMEZONE = 'UTC'


def utcnow():
    """Timezone-aware current instant. Use this instead of ``datetime.utcnow``."""
    return datetime.now(UTC)


def get_zone(tz_name):
    """Resolve an IANA name to a ``ZoneInfo``, falling back to UTC.

    Returns:
        A tuple ``(zone, resolved)`` where ``resolved`` is False when the name
        was unusable and UTC was substituted.
    """
    if not tz_name:
        return ZoneInfo(FALLBACK_TIMEZONE), False
    try:
        return ZoneInfo(str(tz_name)), True
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo(FALLBACK_TIMEZONE), False


def is_valid_timezone(tz_name):
    """True when ``tz_name`` is a timezone the runtime knows."""
    if not tz_name:
        return False
    _, resolved = get_zone(tz_name)
    return resolved


def to_utc(local_dt, tz_name, fold=0):
    """Convert a naive local datetime plus an IANA zone into a UTC instant.

    Args:
        local_dt: Naive datetime as printed on the ticket. An aware datetime is
            accepted and simply converted.
        tz_name: IANA timezone name.
        fold: Disambiguates the repeated hour of a backward DST transition.
            ``0`` picks the first (pre-transition) occurrence, ``1`` the second.

    Returns:
        A timezone-aware datetime in UTC, or None when ``local_dt`` is None.
    """
    if local_dt is None:
        return None
    if local_dt.tzinfo is not None:
        return local_dt.astimezone(UTC)
    zone, _ = get_zone(tz_name)
    return local_dt.replace(tzinfo=zone, fold=fold).astimezone(UTC)


def to_local(utc_dt, tz_name):
    """Render a UTC instant in a target timezone. Returns an aware datetime."""
    if utc_dt is None:
        return None
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=UTC)
    zone, _ = get_zone(tz_name)
    return utc_dt.astimezone(zone)


def set_instant(entity, prefix, local_dt, tz_name):
    """Write the ``_local`` / ``_tz`` / ``_utc`` triple on an entity.

    This is the only supported way to set an itinerary instant. Writing
    ``<prefix>_utc`` directly anywhere else is a bug: it lets the three columns
    drift apart and silently breaks the alert engine.

    Args:
        entity: The model instance to mutate.
        prefix: Column prefix, e.g. ``'salida'`` or ``'check_in'``.
        local_dt: Naive local datetime, or None to clear the triple.
        tz_name: IANA timezone name.

    Returns:
        The resolved timezone name actually stored.
    """
    if local_dt is None:
        setattr(entity, f'{prefix}_local', None)
        setattr(entity, f'{prefix}_tz', None)
        setattr(entity, f'{prefix}_utc', None)
        return None

    if local_dt.tzinfo is not None:
        # An aware value was supplied: trust its offset, store the wall time.
        zone_name = tz_name or str(local_dt.tzinfo)
        utc_value = local_dt.astimezone(UTC)
        local_value = local_dt.replace(tzinfo=None)
    else:
        _, resolved = get_zone(tz_name)
        zone_name = tz_name if resolved else FALLBACK_TIMEZONE
        local_value = local_dt
        utc_value = to_utc(local_dt, zone_name)

    setattr(entity, f'{prefix}_local', local_value)
    setattr(entity, f'{prefix}_tz', zone_name)
    setattr(entity, f'{prefix}_utc', utc_value)
    return zone_name


def get_instant(entity, prefix):
    """Read back the triple as ``(local, tz_name, utc)``."""
    return (
        getattr(entity, f'{prefix}_local', None),
        getattr(entity, f'{prefix}_tz', None),
        getattr(entity, f'{prefix}_utc', None),
    )


def minutes_between(earlier_utc, later_utc):
    """Whole minutes from ``earlier_utc`` to ``later_utc``.

    Negative when the second instant precedes the first, which is exactly what
    the overlap rules need. Returns None if either side is missing.
    """
    if earlier_utc is None or later_utc is None:
        return None
    if earlier_utc.tzinfo is None:
        earlier_utc = earlier_utc.replace(tzinfo=UTC)
    if later_utc.tzinfo is None:
        later_utc = later_utc.replace(tzinfo=UTC)
    return int((later_utc - earlier_utc).total_seconds() // 60)


def utc_offset_hours(tz_name, at_utc=None):
    """The UTC offset of a zone at a given instant, in (possibly fractional) hours."""
    zone, _ = get_zone(tz_name)
    moment = (at_utc or utcnow())
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    offset = moment.astimezone(zone).utcoffset() or timedelta(0)
    return offset.total_seconds() / 3600.0


def timezone_shift_hours(from_tz, to_tz, at_utc=None):
    """Hours of clock change when travelling from one zone to another.

    Positive means the traveller moves their watch forward. Used by the
    timezone-shift alert rule (section 2.4).
    """
    return utc_offset_hours(to_tz, at_utc) - utc_offset_hours(from_tz, at_utc)


def crosses_dst_transition(tz_name, start_utc, end_utc):
    """True when the UTC offset of ``tz_name`` changes between the two instants."""
    if start_utc is None or end_utc is None:
        return False
    return utc_offset_hours(tz_name, start_utc) != utc_offset_hours(tz_name, end_utc)


def local_date(utc_dt, tz_name):
    """The calendar date an instant falls on, in the given zone."""
    localised = to_local(utc_dt, tz_name)
    return localised.date() if localised else None


def overlaps(start_a, end_a, start_b, end_b):
    """Half-open interval overlap test in UTC.

    Touching intervals (one ends exactly when the next begins) do not overlap.
    """
    if None in (start_a, end_a, start_b, end_b):
        return False
    return start_a < end_b and start_b < end_a


# ----------------------------------------------------------------------
# Jinja filters
# ----------------------------------------------------------------------
_MONTHS_ES = (
    'ene', 'feb', 'mar', 'abr', 'may', 'jun',
    'jul', 'ago', 'sep', 'oct', 'nov', 'dic',
)


def format_local(value, tz_name=None, with_tz=True):
    """Render an instant as ``dd mmm yyyy HH:MM (tz)`` in Spanish."""
    if value is None:
        return '—'
    if tz_name:
        value = to_local(value, tz_name)
    stamp = (
        f'{value.day:02d} {_MONTHS_ES[value.month - 1]} {value.year} '
        f'{value.hour:02d}:{value.minute:02d}'
    )
    if with_tz and tz_name:
        stamp = f'{stamp} ({_short_zone(tz_name)})'
    return stamp


def format_local_date(value, tz_name=None):
    """Render just the date part."""
    if value is None:
        return '—'
    if tz_name:
        value = to_local(value, tz_name)
    return f'{value.day:02d} {_MONTHS_ES[value.month - 1]} {value.year}'


def format_local_time(value, tz_name=None):
    """Render just ``HH:MM``."""
    if value is None:
        return '—'
    if tz_name:
        value = to_local(value, tz_name)
    return f'{value.hour:02d}:{value.minute:02d}'


def format_duration(minutes):
    """Render a minute count as ``2 h 15 min``."""
    if minutes is None:
        return '—'
    minutes = int(minutes)
    sign = '-' if minutes < 0 else ''
    minutes = abs(minutes)
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f'{sign}{hours} h {rest} min'
    if hours:
        return f'{sign}{hours} h'
    return f'{sign}{rest} min'


def _short_zone(tz_name):
    """``Europe/Madrid`` -> ``Madrid``, for compact display."""
    return str(tz_name).rsplit('/', 1)[-1].replace('_', ' ')

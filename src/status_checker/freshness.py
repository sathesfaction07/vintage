"""Deriving how old a reading is from what the response actually told us.

The arithmetic follows RFC 9111 section 4.2.3, which defines a response's age from `Age`, `Date`
and the time the request was made. Two departures from a caching proxy's use of that arithmetic:

  * A proxy wants a single number so it can decide whether to serve. We want a BOUND, because we
    are not deciding whether to serve -- we are deciding what a value proves.

  * A proxy treats a missing `Age` as zero. We treat it as unknown. A conforming cache must add
    `Age`, so its absence is *evidence* of a direct response and not proof of one, and the cost
    of the two errors is not symmetric: calling a stale reading fresh reports a status that is no
    longer true, while calling a fresh reading undated only widens an interval.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import AgeBound, Provenance

CacheDirectives = dict[str, str | None]


def parse_cache_control(value: str | None) -> CacheDirectives:
    """Parse a Cache-Control header into directives, lowercased, values kept as strings.

    Unparseable or absent input yields an empty mapping rather than raising: a malformed header is
    a reason to know less about the response, not a reason to abandon the reading.
    """
    if not value:
        return {}
    directives: CacheDirectives = {}
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        if "=" in token:
            name, _, raw = token.partition("=")
            directives[name.strip().lower()] = raw.strip().strip('"')
        else:
            directives[token.lower()] = None
    return directives


def _positive_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def freshness_lifetime(directives: CacheDirectives) -> int | None:
    """The longest a conforming shared cache may serve this response, in seconds.

    `s-maxage` takes precedence over `max-age` for shared caches, and a shared cache is what sits
    between us and a platform we do not control.

    Returns None when no lifetime is stated, which leaves the age unbounded above. `Expires` is
    deliberately not consulted: it is a wall-clock instant interpreted against the origin's clock,
    so using it would silently import clock skew into a bound whose whole purpose is to be safe.
    """
    for directive in ("s-maxage", "max-age"):
        lifetime = _positive_int(directives.get(directive))
        if lifetime is not None:
            return lifetime
    return None


def apparent_age(date_header: datetime | None, observed_at: datetime) -> float:
    """`observed_at - Date`, floored at zero (RFC 9111's apparent_age).

    Floored because a negative value means the origin's clock is ahead of ours, and the only sound
    conclusion from clock skew is that we learn nothing -- not that the response comes from the
    future.
    """
    if date_header is None:
        return 0.0
    return max(0.0, (observed_at - date_header).total_seconds())


def age_from_headers(
    *,
    observed_at: datetime,
    age_header: int | None = None,
    date_header: datetime | None = None,
    cache_control: str | None = None,
    request_sent_at: datetime | None = None,
) -> tuple[Provenance, AgeBound]:
    """Bound a response's age, and say whether a cache is known to have been involved.

    The lower bound is the larger of the `Age` header and the apparent age, plus the request
    round trip if we know it -- all three are floors on how long ago the origin spoke, and the
    largest floor is the one that holds.

    The upper bound is the stated freshness lifetime, because a conforming cache will not serve
    beyond it. Where no lifetime is stated the age is unbounded above, and that is reported rather
    than defaulted.
    """
    directives = parse_cache_control(cache_control)

    response_delay = 0.0
    if request_sent_at is not None:
        response_delay = max(0.0, (observed_at - request_sent_at).total_seconds())

    floor = max(
        float(age_header) + response_delay if age_header is not None else 0.0,
        apparent_age(date_header, observed_at),
    )

    # no-store forbids storing the response; no-cache forbids reusing it without revalidating.
    # Either way what we hold was not served stale out of a cache.
    if "no-store" in directives or "no-cache" in directives:
        return Provenance.ORIGIN, AgeBound.exact(0.0)

    lifetime = freshness_lifetime(directives)

    # max-age=0 cannot be satisfied by any stored copy, so this is a direct response.
    if lifetime == 0 and age_header is None:
        return Provenance.ORIGIN, AgeBound.exact(0.0)

    if age_header is not None:
        ceiling = float(lifetime) if lifetime is not None else None
        if ceiling is not None and ceiling < floor:
            # A cache served the response past its own stated lifetime. The lifetime is therefore
            # not a bound on anything, and pretending otherwise would produce a bound narrower
            # than the evidence supports.
            ceiling = None
        return Provenance.CACHED, AgeBound(min_seconds=floor, max_seconds=ceiling)

    # No Age header. A conforming cache would have added one, so this is probably a direct
    # response -- but "probably" is not a bound, and a stated lifetime tells us how stale a
    # non-conforming cache could have let it get.
    ceiling = float(lifetime) if lifetime is not None else None
    if ceiling is not None and ceiling < floor:
        ceiling = None
    return Provenance.UNDATED, AgeBound(min_seconds=floor, max_seconds=ceiling)


def generation_of(
    *, etag: str | None = None, date_header: datetime | None = None, last_modified: datetime | None = None
) -> str | None:
    """Identify which origin response a reading came from.

    An `ETag` names a specific response body, which is exactly what we need: two readings sharing
    one are the same observation seen twice. `Date` is a weaker fallback -- two genuinely distinct
    responses generated in the same second collide -- and `Last-Modified` is weakest of all, since
    it describes the RESOURCE rather than the response, so an unchanged resource polled a hundred
    times yields one value forever.

    Returns None when nothing identifies the response, and None must not be treated as a shared
    identity: two unidentifiable readings are not thereby the same reading.
    """
    if etag:
        return f"etag:{etag.strip()}"
    if date_header is not None:
        return f"date:{date_header.isoformat()}"
    if last_modified is not None:
        return f"lastmod:{last_modified.isoformat()}"
    return None


def stale_by(bound: AgeBound, tolerance: timedelta) -> bool:
    """True when the reading is provably older than `tolerance`.

    Uses the LOWER bound, so this only fires when staleness is certain. A reading whose age is
    unbounded above is not reported stale by this function -- it is not *provably* stale, it is
    simply undatable, and conflating the two would put an unfalsifiable claim next to a
    demonstrable one.
    """
    return bound.min_seconds > tolerance.total_seconds()

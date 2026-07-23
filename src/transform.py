import datetime
import requests

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
ARCHIVE_TIMEOUT = 10
DRY_MM = 1.0          # below this = "dry" day
WET_MM = 1.0           # at/above this = "wet" day (for rain-window percentile)
MIN_YEARS_SINCE = 10   # "since YYYY" only claimed if >= this many years ago
TROPICAL_MIN_C = 20.0  # tropical-night threshold, always in Celsius internally

RAIN7_DAYS = 7                    # trailing accumulation window
RAIN7_END_SPREAD = 7              # comparator windows END within +/- this many days of today's date
RAIN7_RECORD_FLOOR_MM = 30.0       # floor for week record / top-3 / "since" claims
RAIN7_PCT_FLOOR_MM = 20.0          # floor for the "unusually wet week" percentile tier
RAIN7_PCT_THRESH = 90.0            # percentile threshold for that tier
RAIN_DAY_RECORD_FLOOR_MM = 10.0    # floor for the surviving single-day all-time date record


def _f(v):
    """Safe float, else None."""
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_f(c):
    return c * 9.0 / 5.0 + 32.0


def _to_in(mm):
    return mm / 25.4


def _round1(v):
    return round(v + 0.0, 1)


def _fmt_temp(c, units):
    if c is None:
        return None
    v = _to_f(c) if units == "F" else c
    return _round1(v)


def _fmt_precip(mm, units):
    if mm is None:
        return None
    v = _to_in(mm) if units == "F" else mm
    return round(v + 0.0, 1)


def _day_label(month, day):
    months = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]
    return months[month - 1] + " " + str(day)


def _percentile_midrank(value, sample):
    """Midrank percentile of value inside sample (0-100)."""
    n = len(sample)
    if n == 0:
        return 50.0
    below = 0
    equal = 0
    for s in sample:
        if s < value:
            below += 1
        elif s == value:
            equal += 1
    return (below + 0.5 * equal) / n * 100.0


def _band_for_pct(p):
    """v2 bands: >=90 much above, 75-90 above, 25-75 near normal, 10-25 below, <10 much below."""
    if p >= 90:
        return "much above normal"
    if p >= 75:
        return "above normal"
    if p >= 25:
        return "near normal"
    if p >= 10:
        return "below normal"
    return "much below normal"


def _quantile(sorted_sample, q):
    """Simple linear-interpolation quantile on a pre-sorted list."""
    n = len(sorted_sample)
    if n == 0:
        return None
    if n == 1:
        return sorted_sample[0]
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_sample[lo] + (sorted_sample[hi] - sorted_sample[lo]) * frac


def _parse_coords(input):
    trmnl = input.get("trmnl") or {}
    settings = trmnl.get("plugin_settings") or {}
    fields = settings.get("custom_fields_values") or {}

    lat = fields.get("latitude")
    lon = fields.get("longitude")
    if lat in (None, ""):
        lat = input.get("latitude")
    if lon in (None, ""):
        lon = input.get("longitude")

    lat_f = _f(lat)
    lon_f = _f(lon)
    if lat_f is None or lon_f is None:
        return None, None, fields
    if not (-90.0 <= lat_f <= 90.0) or not (-180.0 <= lon_f <= 180.0):
        return None, None, fields
    return lat_f, lon_f, fields


def _loc_label(fields, lat, lon):
    label = fields.get("location_label")
    if label and str(label).strip():
        return str(label).strip()
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return ""
#    return "%.2f°%s, %.2f°%s" % (abs(lat), ns, abs(lon), ew)


def _units(fields):
    u = (fields.get("units") or "").strip().lower()
    return "F" if u.startswith("f") else "C"


def _build_archive_index(daily, utc_offset=0):
    """Return dict (month, day) -> list of (year, tmax, tmin, prcp), and a
    date-> (tmax,tmin,prcp) map for dry-spell walking.

    utc_offset (seconds) shifts each unixtime stamp to the location's local
    day before bucketing. Open-Meteo's daily unixtime stamps mark the UTC
    instant of local midnight, so for any positive-offset location (all of
    Europe/Asia/etc.) a raw .date() lands on the PREVIOUS calendar day and
    every same-date comparison silently uses the wrong day's data."""
    by_md = {}
    by_date = {}
    times = daily.get("time") or []
    tmax_l = daily.get("temperature_2m_max") or []
    tmin_l = daily.get("temperature_2m_min") or []
    prcp_l = daily.get("precipitation_sum") or []
    n = len(times)
    epoch = datetime.datetime(1970, 1, 1)
    for i in range(n):
        ts = times[i]
        try:
            # timeformat=unixtime -> integer seconds; plain arithmetic instead
            # of utcfromtimestamp so pre-1970 (negative) timestamps work on
            # every platform (Windows raises OSError on negative values).
            # + utc_offset converts the UTC instant to the location's local
            # day so bucketing lands on the correct calendar date.
            d = (epoch + datetime.timedelta(seconds=int(ts) + utc_offset)).date()
        except (TypeError, ValueError, OverflowError):
            try:
                d = datetime.date.fromisoformat(str(ts)[:10])
            except Exception:
                continue
        tmax = _f(tmax_l[i]) if i < len(tmax_l) else None
        tmin = _f(tmin_l[i]) if i < len(tmin_l) else None
        prcp = _f(prcp_l[i]) if i < len(prcp_l) else None
        if tmax is None:
            continue
        by_md.setdefault((d.month, d.day), []).append((d.year, tmax, tmin, prcp))
        by_date[d] = (tmax, tmin, prcp)
    return by_md, by_date


def _window_md_keys(today):
    """List of (month, day) keys for today +/- 7 calendar days (leap-safe)."""
    keys = []
    for offset in range(-7, 8):
        d = today + datetime.timedelta(days=offset)
        keys.append((d.month, d.day))
    return keys


def _clim_span_mean(span_dates, by_md):
    """Climatological mean total precipitation over a calendar span (list of
    dates), computed properly: for each archive year, sum that year's actual
    value on each (month, day) in the span (not a +/-7 window expansion,
    which would massively over-count), then average the per-year totals
    across years that have complete data for the whole span."""
    if not span_dates:
        return None
    md_keys = [(d.month, d.day) for d in span_dates]
    # year -> {(month,day): prcp} for quick per-year lookup
    year_vals = {}
    for k in set(md_keys):
        for (yr, _tmx, _tmn, pr) in by_md.get(k, []):
            if pr is None:
                continue
            year_vals.setdefault(yr, {})[k] = pr

    totals = []
    for yr, vals in year_vals.items():
        if all(k in vals for k in md_keys):
            totals.append(sum(vals[k] for k in md_keys))
    if not totals:
        return None
    return sum(totals) / len(totals)


def _dry_spell_days(today, forecast_daily, by_date):
    """Count consecutive days < DRY_MM, walking back from today (exclusive of
    today itself; "today" rain is reported separately). Uses forecast daily
    past days first, then falls back to archive by_date (which covers every
    day back to 1940, not just the +/-7 window). Returns (days, capped) where
    capped=True means we stopped only because the display caps at "31+ days",
    not because data ran out."""
    times = forecast_daily.get("time") or []
    prcp_l = forecast_daily.get("precipitation_sum") or []
    fmap = {}
    for i, ts in enumerate(times):
        try:
            d = datetime.date.fromisoformat(str(ts)[:10])
        except Exception:
            continue
        p = _f(prcp_l[i]) if i < len(prcp_l) else None
        fmap[d] = p

    count = 0
    cursor = today - datetime.timedelta(days=1)
    display_cap = 31
    steps = 0
    max_steps = 20000  # hard safety valve (~54 years), should never be hit
    while steps < max_steps:
        steps += 1
        p = fmap.get(cursor)
        if p is None:
            entry = by_date.get(cursor)
            p = entry[2] if entry else None
        if p is None:
            # ran out of known data -> stop counting here
            return count, False
        if p < DRY_MM:
            count += 1
            if count >= display_cap:
                return count, True
            cursor = cursor - datetime.timedelta(days=1)
        else:
            return count, False
    return count, True


def _ordinal(n):
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return str(n) + suf


def _num_str(v):
    """Format a rounded-1-decimal float, dropping the .0 for whole numbers."""
    if v is None:
        return "?"
    if float(v).is_integer():
        return str(int(v))
    return ("%.1f" % v)


def _int_str(v):
    if v is None:
        return "?"
    return str(int(round(v)))


def _rain_amt_str(v, units):
    """Rain amounts: whole numbers for mm, 1 decimal for inches."""
    if v is None:
        return "?"
    if units == "F":
        return "%.1f" % v
    return str(int(round(v)))


def _clip(s, n):
    if s is None:
        return s
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _series_stats(value, same_date_series, window_series, today_year):
    """Compute rank/record/since/pct stats for one series (day-high or
    night-low) using same-date-across-years values and a +/-7 window sample.
    same_date_series: list of (year, value) for the exact calendar date,
    current year excluded. window_series: flat list of values for the +/-7
    window, current year excluded.
    Returns a dict with hot-side and cold-side info plus percentile."""
    same_date = [t for t in same_date_series if t[0] != today_year]
    n = len(same_date)

    pct = _percentile_midrank(value, window_series) if window_series else 50.0

    if same_date:
        hi_year, hi_val = max(same_date, key=lambda t: t[1])
        lo_year, lo_val = min(same_date, key=lambda t: t[1])
    else:
        hi_year = lo_year = today_year
        hi_val = lo_val = value

    hotter_count = sum(1 for (_, v) in same_date if v > value)
    hot_rank = hotter_count + 1 if n else None
    colder_count = sum(1 for (_, v) in same_date if v < value)
    cold_rank = colder_count + 1 if n else None

    all_time_hot_record = bool(same_date) and value >= hi_val
    all_time_cold_record = bool(same_date) and value <= lo_val

    # "since" claims must also be genuine same-date extremes -- otherwise a
    # thin same-date sample can produce an old "most-recent-hotter/colder
    # year" purely by chance even when today is unremarkable (e.g. rank
    # 39/86, dead in the middle). Require today to sit in the extreme
    # quarter of the same-date series itself (mirrors the 25/75 normal band,
    # applied to same-date rank instead of the window percentile).
    quarter_cutoff = max(1, -(-n // 4))  # ceil(n/4)

    hottest_since_year = None
    for (yr, v) in sorted(same_date, key=lambda x: x[0], reverse=True):
        if v > value:
            hottest_since_year = yr
            break
    hot_since_eligible = (hottest_since_year is not None and
                          (today_year - hottest_since_year) >= MIN_YEARS_SINCE and
                          hot_rank is not None and hot_rank <= quarter_cutoff)

    coldest_since_year = None
    for (yr, v) in sorted(same_date, key=lambda x: x[0], reverse=True):
        if v < value:
            coldest_since_year = yr
            break
    cold_since_eligible = (coldest_since_year is not None and
                           (today_year - coldest_since_year) >= MIN_YEARS_SINCE and
                           cold_rank is not None and cold_rank <= quarter_cutoff)

    return {
        "n_years": n,
        "pct": pct,
        "record_hi_year": hi_year, "record_hi_val": hi_val,
        "record_lo_year": lo_year, "record_lo_val": lo_val,
        "hot_rank": hot_rank, "cold_rank": cold_rank,
        "all_time_hot_record": all_time_hot_record,
        "all_time_cold_record": all_time_cold_record,
        "hottest_since_year": hottest_since_year, "hot_since_eligible": hot_since_eligible,
        "coldest_since_year": coldest_since_year, "cold_since_eligible": cold_since_eligible,
        "since_year": same_date[0][0] if same_date else today_year,
    }


def _rain7_history(today, by_date, r7_value):
    """Historical comparator for the trailing-7-day rain total (R7).

    For every archive year Y (excluding today.year), build every complete
    7-day precipitation total whose window ENDS within +/- RAIN7_END_SPREAD
    days of today's calendar date in year Y. Returns a dict with the flat
    sample of window totals, the per-year maxima, and rank/record/since/pct
    stats for r7_value against that sample -- pure function of
    (today, by_date, r7_value) so it's unit-testable without touching the
    network or wall clock beyond the passed-in `today`."""
    all_windows = []
    year_max = {}

    years = set(d.year for d in by_date.keys())
    years.discard(today.year)

    for y in years:
        try:
            anchor = datetime.date(y, today.month, today.day)
        except ValueError:
            # Feb 29 in a non-leap year
            anchor = datetime.date(y, today.month, 28)
        for k in range(-RAIN7_END_SPREAD, RAIN7_END_SPREAD + 1):
            end = anchor + datetime.timedelta(days=k)
            if end.year == today.year:
                continue
            total = 0.0
            complete = True
            for j in range(RAIN7_DAYS):
                d = end - datetime.timedelta(days=(RAIN7_DAYS - 1 - j))
                entry = by_date.get(d)
                if entry is None or entry[2] is None:
                    complete = False
                    break
                total += entry[2]
            if not complete:
                continue
            all_windows.append(total)
            if total > year_max.get(end.year, float("-inf")):
                year_max[end.year] = total

    n_years = len(year_max)
    if n_years == 0:
        return {
            "all_windows": all_windows,
            "year_max": year_max,
            "n_years": 0,
            "pct": 50.0,
            "rank": None,
            "is_record": False,
            "since_year": None,
            "since_eligible": False,
            "clim_week_mean": None,
            "max_year": None,
            "max_val": None,
        }

    pct = _percentile_midrank(r7_value, all_windows) if all_windows else 50.0
    rank = 1 + sum(1 for v in year_max.values() if v > r7_value)

    max_year, max_val = max(year_max.items(), key=lambda t: t[1])
    is_record = r7_value >= max_val

    since_year = None
    for (yr, v) in sorted(year_max.items(), key=lambda x: x[0], reverse=True):
        if v > r7_value:
            since_year = yr
            break

    quarter_cutoff = max(1, -(-n_years // 4))  # ceil(n_years/4)
    since_eligible = (since_year is not None and
                      (today.year - since_year) >= MIN_YEARS_SINCE and
                      rank <= quarter_cutoff)

    clim_week_mean = sum(all_windows) / len(all_windows) if all_windows else None

    return {
        "all_windows": all_windows,
        "year_max": year_max,
        "n_years": n_years,
        "pct": pct,
        "rank": rank,
        "is_record": is_record,
        "since_year": since_year,
        "since_eligible": since_eligible,
        "clim_week_mean": clim_week_mean,
        "max_year": max_year,
        "max_val": max_val,
    }


def run(input):
    try:
        input = input or {}
        lat, lon, fields = _parse_coords(input)

        # updated_at needs utc_offset; fall back to input root, else UTC
        utc_offset = input.get("utc_offset_seconds")
        if not isinstance(utc_offset, (int, float)):
            utc_offset = 0
        tz = datetime.timezone(datetime.timedelta(seconds=utc_offset))
        now_local = datetime.datetime.now(datetime.timezone.utc).astimezone(tz)
        updated_at = now_local.strftime("%H:%M")

        if lat is None or lon is None:
            return {"ok": False, "error": "Check your coordinates in plugin settings",
                    "updated_at": updated_at}

        units = _units(fields)
        loc = _loc_label(fields, lat, lon)

        daily = input.get("daily") or {}
        hourly = input.get("hourly") or {}
        current = input.get("current") or {}

        d_times = daily.get("time") or []
        if not d_times:
            return {"ok": False, "error": "Forecast data unavailable right now",
                    "updated_at": updated_at}

        today = now_local.date()
        today_iso = today.isoformat()

        d_tmax_l = daily.get("temperature_2m_max") or [None] * len(d_times)
        d_tmin_l = daily.get("temperature_2m_min") or [None] * len(d_times)
        d_prcp_l = daily.get("precipitation_sum") or [None] * len(d_times)

        # --- today's forecast daily entry ---
        try:
            idx_today = d_times.index(today_iso)
        except ValueError:
            idx_today = len(d_times) - 1  # best effort: last entry

        tmax_fc = _f(d_tmax_l[idx_today]) if idx_today < len(d_tmax_l) else None
        tmin_fc = _f(d_tmin_l[idx_today]) if idx_today < len(d_tmin_l) else None
        precip_today_mm = _f(d_prcp_l[idx_today]) if idx_today < len(d_prcp_l) else None

        # --- observed-so-far high/low from hourly (past_hours=24&forecast_hours=1 => ~25 values) ---
        h_times = hourly.get("time") or []
        h_temps = hourly.get("temperature_2m") or []
        observed_high_so_far = None
        observed_low_so_far = None
        for i, ts in enumerate(h_times):
            try:
                dt = datetime.datetime.fromisoformat(ts)
            except Exception:
                continue
            if dt.date() != today:
                continue
            if dt.replace(tzinfo=None) > now_local.replace(tzinfo=None):
                continue
            v = _f(h_temps[i]) if i < len(h_temps) else None
            if v is None:
                continue
            if observed_high_so_far is None or v > observed_high_so_far:
                observed_high_so_far = v
            if observed_low_so_far is None or v < observed_low_so_far:
                observed_low_so_far = v

        hour_now = now_local.hour
        if hour_now >= 17:
            mode = "observed"
            hi_c = observed_high_so_far if observed_high_so_far is not None else tmax_fc
            tmin_today_c = observed_low_so_far if observed_low_so_far is not None else tmin_fc
        else:
            mode = "forecast"
            candidates = [x for x in (observed_high_so_far, tmax_fc) if x is not None]
            hi_c = max(candidates) if candidates else None
            tmin_today_c = tmin_fc

        if hi_c is None:
            return {"ok": False, "error": "Forecast data unavailable right now",
                    "updated_at": updated_at}

        temp_now_c = _f(current.get("temperature_2m"))
        if temp_now_c is None:
            temp_now_c = hi_c

        # --- fetch archive (unchanged: 1940-01-01 -> today-6d, 3 daily vars) ---
        end_date = (today - datetime.timedelta(days=6)).isoformat()
        try:
            resp = requests.get(
                ARCHIVE_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "start_date": "1940-01-01",
                    "end_date": end_date,
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                    "timezone": "auto",
                    "timeformat": "unixtime",
                },
                timeout=ARCHIVE_TIMEOUT,
            )
            resp.raise_for_status()
            archive = resp.json()
        except Exception:
            return {"ok": False, "error": "Couldn't reach the climate archive — will retry next refresh",
                    "updated_at": updated_at}

        archive_daily = archive.get("daily") or {}
        archive_offset = archive.get("utc_offset_seconds")
        if not isinstance(archive_offset, (int, float)):
            archive_offset = utc_offset
        by_md, by_date = _build_archive_index(archive_daily, archive_offset)

        if not by_md:
            return {"ok": False, "error": "Couldn't reach the climate archive — will retry next refresh",
                    "updated_at": updated_at}

        md_key = (today.month, today.day)
        same_date_all = sorted(by_md.get(md_key, []), key=lambda t: t[0])
        # (year, tmax, tmin, prcp) tuples, excluding current year
        same_date_full = [t for t in same_date_all if t[0] != today.year]

        n_years = len(same_date_full)
        since_year = same_date_full[0][0] if same_date_full else today.year

        # --- window sample (+/-7 days, all years) for percentile + normal range ---
        window_keys = _window_md_keys(today)
        window_tmax = []
        window_tmin = []
        window_wet_prcp = []
        for k in window_keys:
            for (yr, tmx, tmn, pr) in by_md.get(k, []):
                if yr == today.year:
                    continue
                window_tmax.append(tmx)
                if tmn is not None:
                    window_tmin.append(tmn)
                if pr is not None and pr >= WET_MM:
                    window_wet_prcp.append(pr)

        if not window_tmax:
            return {"ok": False, "error": "Not enough climate history for this location",
                    "updated_at": updated_at}

        # === DAY (high) stats ===
        day_same_date = [(yr, t) for (yr, t, _tn, _pr) in same_date_full]
        day_stats = _series_stats(hi_c, day_same_date, window_tmax, today.year)
        pct = day_stats["pct"]
        pct_int = int(round(pct))
        band = _band_for_pct(pct)

        sorted_window_tmax = sorted(window_tmax)
        normal_lo_c = _quantile(sorted_window_tmax, 0.25)
        normal_hi_c = _quantile(sorted_window_tmax, 0.75)

        record_hi_year = day_stats["record_hi_year"]
        record_hi_c = day_stats["record_hi_val"]
        record_lo_year = day_stats["record_lo_year"]
        record_lo_c = day_stats["record_lo_val"]

        # === NIGHT (low) stats — only meaningful if we have a tonight/last-night low ===
        night_stats = None
        if tmin_today_c is not None:
            night_same_date = [(yr, tn) for (yr, _t, tn, _pr) in same_date_full if tn is not None]
            night_window = window_tmin
            if night_same_date and night_window:
                night_stats = _series_stats(tmin_today_c, night_same_date, night_window, today.year)

        tropical_night = False
        if night_stats is not None and tmin_today_c is not None:
            if tmin_today_c >= TROPICAL_MIN_C and night_stats["pct"] >= 90:
                tropical_night = True

        # --- 30-day anomaly (v1 logic, kept) ---
        past_days = []
        for i, ts in enumerate(d_times):
            try:
                dd = datetime.date.fromisoformat(str(ts)[:10])
            except Exception:
                continue
            if dd < today:
                t = _f(d_tmax_l[i]) if i < len(d_tmax_l) else None
                if t is not None:
                    past_days.append((dd, t))
        past_days.sort(key=lambda x: x[0], reverse=True)
        last30 = past_days[:30]

        anom30_c = None
        if last30:
            obs_mean = sum(t for (_, t) in last30) / len(last30)
            per_day_clim_means = []
            for (dd, _t) in last30:
                vals = []
                for k in _window_md_keys(dd):
                    for (yr, tmx, _tmn, _pr) in by_md.get(k, []):
                        vals.append(tmx)
                if vals:
                    per_day_clim_means.append(sum(vals) / len(vals))
            if per_day_clim_means:
                clim_mean = sum(per_day_clim_means) / len(per_day_clim_means)
                anom30_c = obs_mean - clim_mean

        # --- yesterday's high + its own +/-7 band verdict ---
        yday = today - datetime.timedelta(days=1)
        yday_iso = yday.isoformat()
        yday_hi_c = None
        yday_prcp_mm = None
        try:
            idx_y = d_times.index(yday_iso)
            yday_hi_c = _f(d_tmax_l[idx_y]) if idx_y < len(d_tmax_l) else None
            yday_prcp_mm = _f(d_prcp_l[idx_y]) if idx_y < len(d_prcp_l) else None
        except ValueError:
            pass
        if yday_hi_c is None:
            entry = by_date.get(yday)
            if entry:
                yday_hi_c = entry[0]
                if yday_prcp_mm is None:
                    yday_prcp_mm = entry[2]

        yday_band = None
        yday_pct = None
        if yday_hi_c is not None:
            yday_window = []
            for k in _window_md_keys(yday):
                for (yr, tmx, _tmn, _pr) in by_md.get(k, []):
                    if yr == yday.year:
                        continue
                    yday_window.append(tmx)
            if yday_window:
                yday_pct = _percentile_midrank(yday_hi_c, yday_window)
                yday_band = _band_for_pct(yday_pct)

        # --- yesterday's own record/since detection (mirrors today's day_stats,
        # but keyed off yesterday's calendar date and excluding yesterday's year) ---
        yday_date_label = _day_label(yday.month, yday.day)
        yday_record_kind = None   # "hot_record" | "cold_record" | "hot_since" | "cold_since" | None
        yday_record_year = None
        if yday_hi_c is not None:
            yday_md_key = (yday.month, yday.day)
            yday_same_date_all = sorted(by_md.get(yday_md_key, []), key=lambda t: t[0])
            yday_same_date_full = [(yr, tmx) for (yr, tmx, _tmn, _pr) in yday_same_date_all if yr != yday.year]
            if yday_same_date_full:
                yday_stats = _series_stats(yday_hi_c, yday_same_date_full, yday_window or [], yday.year)
                if yday_stats["all_time_hot_record"]:
                    yday_record_kind = "hot_record"
                    yday_record_year = yday_stats["record_hi_year"]
                elif yday_stats["all_time_cold_record"]:
                    yday_record_kind = "cold_record"
                    yday_record_year = yday_stats["record_lo_year"]
                elif yday_stats["hot_since_eligible"]:
                    yday_record_kind = "hot_since"
                    yday_record_year = yday_stats["hottest_since_year"]
                elif yday_stats["cold_since_eligible"]:
                    yday_record_kind = "cold_since"
                    yday_record_year = yday_stats["coldest_since_year"]

        # --- rain facts / dry spell ---
        dry_days, dry_capped = _dry_spell_days(today, daily, by_date)
        is_dry_notable = dry_days >= 14

        # --- R7: trailing 7-day rain total (METHODOLOGY §5, DATA_CONTRACT v3.1) ---
        # r7_locked = sum of the 6 observed days before today (today-6 .. today-1),
        # sourced from the polled forecast daily arrays first, falling back to the
        # archive by_date map -- exactly like _dry_spell_days. A missing day counts
        # as 0.0 (can only suppress a claim, never inflate one).
        fc_prcp_by_date = {}
        for i, ts in enumerate(d_times):
            try:
                dd = datetime.date.fromisoformat(str(ts)[:10])
            except Exception:
                continue
            p = _f(d_prcp_l[i]) if i < len(d_prcp_l) else None
            fc_prcp_by_date[dd] = p

        r7_locked = 0.0
        for i in range(1, RAIN7_DAYS):
            d = today - datetime.timedelta(days=i)
            p = fc_prcp_by_date.get(d)
            if p is None:
                entry = by_date.get(d)
                p = entry[2] if entry else None
            if p is not None:
                r7_locked += p
        r7_total = r7_locked + (precip_today_mm if precip_today_mm is not None else 0.0)

        rain7_hist = _rain7_history(today, by_date, r7_total)
        rain7_hist_locked = _rain7_history(today, by_date, r7_locked)

        # rain 30-day total vs climatological mean total over same window
        last30_prcp_total = None
        last30_dates = [dd for (dd, _t) in last30]
        if last30_dates:
            total = 0.0
            any_val = False
            for i, ts in enumerate(d_times):
                try:
                    dd = datetime.date.fromisoformat(str(ts)[:10])
                except Exception:
                    continue
                if dd in last30_dates:
                    p = _f(d_prcp_l[i]) if i < len(d_prcp_l) else None
                    if p is not None:
                        total += p
                        any_val = True
            if any_val:
                last30_prcp_total = total

        clim_prcp_window_mean = _clim_span_mean(last30_dates, by_md)

        # climatological mean over dry-spell-length window (when dry spell notable)
        dryspell_clim_mean = None
        if is_dry_notable and dry_days > 0:
            spell_dates = [today - datetime.timedelta(days=i) for i in range(1, dry_days + 1)]
            dryspell_clim_mean = _clim_span_mean(spell_dates, by_md)

        # single-day all-time date record check (surviving daily rain tier --
        # the old "wettest since >= 10y" daily path is gone in v3.1, only the
        # all-time date record survives, floor raised to RAIN_DAY_RECORD_FLOOR_MM)
        same_date_wet = [(yr, pr) for (yr, _t, _tn, pr) in same_date_full if pr is not None]
        wettest_all_time = False
        today_prcp_for_compare = precip_today_mm if precip_today_mm is not None else 0.0
        rain_beaten_mark = None
        if same_date_wet and today_prcp_for_compare >= RAIN_DAY_RECORD_FLOOR_MM:
            max_wet = max(pr for (_, pr) in same_date_wet)
            if today_prcp_for_compare >= max_wet:
                wettest_all_time = True
                beaten_year, beaten_val = max(
                    [(yr, pr) for (yr, pr) in same_date_wet if pr <= today_prcp_for_compare] or [(None, None)],
                    key=lambda x: (x[1] if x[1] is not None else -1))
                rain_beaten_mark = (beaten_val, beaten_year)

        date_label_tmp = _day_label(today.month, today.day)
        day_rain_record_headline = "Wettest %s on record" % date_label_tmp

        # --- rain-week (R7) tier eligibility, evaluated for both the actual
        # r7_total and, for the "decisive forecast" prefix rule (METHODOLOGY
        # section 5 final paragraph), for r7_locked alone ---
        def _rain7_record_top3(hist, r7_value):
            return r7_value >= RAIN7_RECORD_FLOOR_MM and (hist["is_record"] or (hist["rank"] is not None and hist["rank"] <= 3))

        def _rain7_since(hist, r7_value):
            return r7_value >= RAIN7_RECORD_FLOOR_MM and hist["since_eligible"]

        def _rain7_pct(hist, r7_value):
            return r7_value >= RAIN7_PCT_FLOOR_MM and hist["pct"] >= RAIN7_PCT_THRESH

        week_record_top3_total = _rain7_record_top3(rain7_hist, r7_total)
        week_record_top3_locked = _rain7_record_top3(rain7_hist_locked, r7_locked)
        week_record_top3_decisive = (mode == "forecast" and
                                     week_record_top3_total and not week_record_top3_locked)

        week_since_total = _rain7_since(rain7_hist, r7_total)
        week_since_locked = _rain7_since(rain7_hist_locked, r7_locked)
        week_since_decisive = (mode == "forecast" and
                               week_since_total and not week_since_locked)

        week_pct_total = _rain7_pct(rain7_hist, r7_total)

        # --- tense-aware output values ---
        date_label = _day_label(today.month, today.day)
        hi_out = _fmt_temp(hi_c, units)
        lo_out = _fmt_temp(tmin_today_c, units)
        temp_now_out = _fmt_temp(temp_now_c, units)
        precip_out = _fmt_precip(precip_today_mm if precip_today_mm is not None else 0.0, units)
        normal_lo_out = _fmt_temp(normal_lo_c, units)
        normal_hi_out = _fmt_temp(normal_hi_c, units)
        record_hi_out = _fmt_temp(record_hi_c, units)
        record_lo_out = _fmt_temp(record_lo_c, units)
        anom30_out = None
        if anom30_c is not None:
            anom30_out = round((anom30_c * 9.0 / 5.0) if units == "F" else anom30_c, 1)

        deg = "°"
        month_word = date_label.split()[0]

        # ================= HEADLINE LADDER (METHODOLOGY §5) =================
        headline = None
        verdict = None
        subline = None

        def _pct_words(p):
            """Round percentile to 'N in 10' language."""
            if p >= 50:
                n10 = int(round(p / 10.0))
                return max(1, min(9, n10)), "Hotter"
            else:
                n10 = int(round((100 - p) / 10.0))
                return max(1, min(9, n10)), "Cooler"

        obs_word_high = "hit" if mode == "observed" else "should reach"
        obs_word_low_should = "should stay" if mode == "forecast" else "stayed"

        forecast_prefix = "Expected " if mode == "forecast" else ""

        rain_unit_word_hdr = "in" if units == "F" else "mm"
        r7_out = _fmt_precip(r7_total, units)

        # Tier 1: day all-time record
        if day_stats["all_time_hot_record"] or day_stats["all_time_cold_record"]:
            if day_stats["all_time_hot_record"]:
                headline = forecast_prefix + "hottest %s on record" % date_label
                verdict = "record_hot"
                beaten_year, beaten_val = day_stats["record_hi_year"], day_stats["record_hi_val"]
            else:
                headline = forecast_prefix + "coldest %s on record" % date_label
                verdict = "record_cold"
                beaten_year, beaten_val = day_stats["record_lo_year"], day_stats["record_lo_val"]
            if forecast_prefix:
                headline = headline[0].upper() + headline[1:]
            beaten_out = _fmt_temp(beaten_val, units)
            record_verb = "Would top" if mode == "forecast" else "Tops"
            subline = "%s the old record of %s%s from %d." % (record_verb, _num_str(beaten_out), deg, beaten_year)

        # Tier 2: rain week record/top-3 (NEW v3.1)
        elif week_record_top3_total:
            week_prefix = "Expected " if week_record_top3_decisive else ""
            if rain7_hist["is_record"]:
                rain_hdr = "wettest week on record"
                verdict = "wet"
                mark_year, mark_val = rain7_hist["max_year"], rain7_hist["max_val"]
                mark_out = _fmt_precip(mark_val, units)
                verb = "Would top" if week_prefix else "Tops"
                subline = "%s the wettest week near this date — %s%s in %d." % (
                    verb, _num_str(mark_out), rain_unit_word_hdr, mark_year)
            else:
                rank = rain7_hist["rank"]
                rain_hdr = "%s wettest week on record" % _ordinal(rank).lower()
                verdict = "wet"
                better_years = sorted([yr for (yr, v) in rain7_hist["year_max"].items() if v > r7_total], reverse=True)
                picked = better_years[:2]
                if len(picked) == 1:
                    subline = "Only %d saw a wetter week here." % picked[0]
                elif len(picked) >= 2:
                    subline = "Only %d and %d saw a wetter week here." % (picked[0], picked[1])
                else:
                    subline = "Nothing else came close."
            headline = week_prefix + rain_hdr
            headline = headline[0].upper() + headline[1:]

        # Tier 3: night all-time record
        elif night_stats is not None and (night_stats["all_time_hot_record"] or night_stats["all_time_cold_record"]):
            if night_stats["all_time_hot_record"]:
                headline = forecast_prefix + "warmest %s night" % date_label
                verdict = "record_hot"
                beaten_year, beaten_val = night_stats["record_hi_year"], night_stats["record_hi_val"]
            else:
                headline = forecast_prefix + "coldest %s night" % date_label
                verdict = "record_cold"
                beaten_year, beaten_val = night_stats["record_lo_year"], night_stats["record_lo_val"]
            if forecast_prefix:
                headline = headline[0].upper() + headline[1:]
            beaten_out = _fmt_temp(beaten_val, units)
            record_verb = "Would top" if mode == "forecast" else "Tops"
            subline = "%s the old night record of %s%s from %d." % (record_verb, _num_str(beaten_out), deg, beaten_year)

        # Tier 4: day rank <= 3
        elif day_stats["hot_rank"] is not None and day_stats["hot_rank"] <= 3:
            headline = forecast_prefix + _ordinal(day_stats["hot_rank"]).lower() + " hottest " + date_label + " on record"
            if forecast_prefix:
                headline = headline[0].upper() + headline[1:]
            verdict = "record_hot"
            better = sorted([yr for (yr, v) in day_same_date if v > hi_c], reverse=True)
            subline = _better_years_sub(better, date_label, "hotter")
        elif day_stats["cold_rank"] is not None and day_stats["cold_rank"] <= 3:
            headline = forecast_prefix + _ordinal(day_stats["cold_rank"]).lower() + " coldest " + date_label + " on record"
            if forecast_prefix:
                headline = headline[0].upper() + headline[1:]
            verdict = "record_cold"
            better = sorted([yr for (yr, v) in day_same_date if v < hi_c], reverse=True)
            subline = _better_years_sub(better, date_label, "colder")

        # Tier 5: night rank <= 3
        elif night_stats is not None and night_stats["hot_rank"] is not None and night_stats["hot_rank"] <= 3:
            headline = forecast_prefix + _ordinal(night_stats["hot_rank"]).lower() + " warmest " + date_label + " night"
            if forecast_prefix:
                headline = headline[0].upper() + headline[1:]
            verdict = "record_hot"
            night_same_date_pairs = [(yr, tn) for (yr, _t, tn, _pr) in same_date_full if tn is not None]
            better = sorted([yr for (yr, v) in night_same_date_pairs if v > tmin_today_c], reverse=True)
            subline = _better_years_sub(better, date_label + " night", "warmer")
        elif night_stats is not None and night_stats["cold_rank"] is not None and night_stats["cold_rank"] <= 3:
            headline = forecast_prefix + _ordinal(night_stats["cold_rank"]).lower() + " coldest " + date_label + " night"
            if forecast_prefix:
                headline = headline[0].upper() + headline[1:]
            verdict = "record_cold"
            night_same_date_pairs = [(yr, tn) for (yr, _t, tn, _pr) in same_date_full if tn is not None]
            better = sorted([yr for (yr, v) in night_same_date_pairs if v < tmin_today_c], reverse=True)
            subline = _better_years_sub(better, date_label + " night", "colder")

        # Tier 6: rain week-since OR single-day all-time date record (REWORKED v3.1)
        elif week_since_total:
            week_prefix = "Expected " if week_since_decisive else ""
            headline = week_prefix + "wettest week since %d" % rain7_hist["since_year"]
            headline = headline[0].upper() + headline[1:]
            verdict = "wet"
            clim_out = _fmt_precip(rain7_hist["clim_week_mean"], units) if rain7_hist["clim_week_mean"] is not None else None
            clim_str = _rain_amt_str(clim_out, units) if clim_out is not None else "?"
            subline = "%s%s in 7 days — a normal week here sees %s%s." % (
                _rain_amt_str(r7_out, units), rain_unit_word_hdr, clim_str, rain_unit_word_hdr)
        elif wettest_all_time:
            headline = forecast_prefix + day_rain_record_headline[0].lower() + day_rain_record_headline[1:]
            headline = headline[0].upper() + headline[1:]
            verdict = "wet"
            if rain_beaten_mark is not None and rain_beaten_mark[1] is not None:
                beaten_out_p = _fmt_precip(rain_beaten_mark[0], units)
                record_verb = "Would top" if mode == "forecast" else "Tops"
                subline = "%s the old record of %s%s from %d." % (
                    record_verb, _num_str(beaten_out_p), rain_unit_word_hdr, rain_beaten_mark[1])
            else:
                subline = "%s%s is heavy for %s." % (_num_str(precip_out), rain_unit_word_hdr, date_label)

        # Tier 7: day since >= 10y
        elif day_stats["hot_since_eligible"]:
            headline = forecast_prefix + "hottest %s since %d" % (date_label, day_stats["hottest_since_year"])
            if forecast_prefix:
                headline = headline[0].upper() + headline[1:]
            verdict = "hot"
            subline = _since_sub(normal_lo_out, normal_hi_out, hi_out, mode, deg, obs_word_high)
        elif day_stats["cold_since_eligible"]:
            headline = forecast_prefix + "coldest %s since %d" % (date_label, day_stats["coldest_since_year"])
            if forecast_prefix:
                headline = headline[0].upper() + headline[1:]
            verdict = "cold"
            subline = _since_sub(normal_lo_out, normal_hi_out, hi_out, mode, deg, obs_word_high)

        # Tier 8: night since >= 10y
        elif night_stats is not None and night_stats["hot_since_eligible"]:
            headline = forecast_prefix + "warmest %s night since %d" % (date_label, night_stats["hottest_since_year"])
            if forecast_prefix:
                headline = headline[0].upper() + headline[1:]
            verdict = "hot"
            subline = _since_sub(normal_lo_out, normal_hi_out, hi_out, mode, deg, obs_word_high)
        elif night_stats is not None and night_stats["cold_since_eligible"]:
            headline = forecast_prefix + "coldest %s night since %d" % (date_label, night_stats["coldest_since_year"])
            if forecast_prefix:
                headline = headline[0].upper() + headline[1:]
            verdict = "cold"
            subline = _since_sub(normal_lo_out, normal_hi_out, hi_out, mode, deg, obs_word_high)

        # Tier 9: day pct >= 90 / <= 10
        elif pct >= 90:
            headline = "Unusually hot today"
            verdict = "hot"
            n10, word = _pct_words(pct)
            subline = "%s than %d in 10 early-%s days." % (word, n10, month_word)
        elif pct <= 10:
            headline = "Unusually cold today"
            verdict = "cold"
            n10, word = _pct_words(pct)
            subline = "%s than %d in 10 early-%s days." % (word, n10, month_word)

        # Tier 10: rain week percentile (NEW v3.1)
        elif week_pct_total:
            headline = "An unusually wet week"
            verdict = "wet"
            n10 = max(1, min(9, int(round(rain7_hist["pct"] / 10.0))))
            subline = "%s%s in 7 days — wetter than %d in 10 weeks this time of year." % (
                _rain_amt_str(r7_out, units), rain_unit_word_hdr, n10)

        # Tier 11: tropical night
        elif tropical_night:
            headline = "Tropical %s night ahead" % date_label if mode == "forecast" else "A tropical %s night" % date_label
            verdict = "tropical"
            subline = "The low %s above %s%s — an uncomfortably warm night." % (obs_word_low_should, _int_str(_fmt_temp(TROPICAL_MIN_C, units)), deg)

        # Tier 12: day pct 75-90 / 10-25
        elif pct >= 75:
            headline = "Warmer than usual"
            verdict = "warm"
            subline = _normal_verdict_sub(deg, precip_today_mm, precip_out, units,
                                          normal_lo_out, normal_hi_out, hi_out, mode,
                                          month_word, yday_hi_c, hi_c)
        elif pct <= 25:
            headline = "Cooler than usual"
            verdict = "cool"
            subline = _normal_verdict_sub(deg, precip_today_mm, precip_out, units,
                                          normal_lo_out, normal_hi_out, hi_out, mode,
                                          month_word, yday_hi_c, hi_c)

        # Tier 13: normal
        else:
            headline = "Perfectly normal weather"
            verdict = "normal"
            subline = _normal_verdict_sub(deg, precip_today_mm, precip_out, units,
                                          normal_lo_out, normal_hi_out, hi_out, mode,
                                          month_word, yday_hi_c, hi_c)

        headline = _clip(headline, 48)
        subline = _clip(subline, 95)

        # ================= SECTIONS (METHODOLOGY §7 / DATA_CONTRACT v2) =================
        today_title = "Today's forecast" if mode == "forecast" else "Today"

        rain_unit_word = "in" if units == "F" else "mm"
        if mode == "forecast":
            parts = "High %s%s · low %s%s" % (_num_str(hi_out), deg, _num_str(lo_out), deg)
            if precip_today_mm is not None and precip_today_mm >= 1.0:
                parts += " · %s%s rain likely" % (_num_str(precip_out), rain_unit_word)
            else:
                parts += " · no rain expected"
        else:
            parts = "High hit %s%s" % (_num_str(hi_out), deg)
            if precip_today_mm is not None and precip_today_mm >= 1.0:
                parts += " · %s%s rain" % (_num_str(precip_out), rain_unit_word)
            else:
                parts += " · no rain"
        today_line = _clip(parts, 44)

        if tropical_night and verdict != "tropical":
            today_note = "Tropical night ahead" if mode == "forecast" else "A tropical night"
        else:
            if pct >= 90:
                today_note = "Top 10% hottest for the date"
            elif pct >= 75:
                today_note = "Warm side of normal"
            elif pct >= 25:
                today_note = "Mid-pack for early %s" % month_word
            elif pct >= 10:
                today_note = "Cool side of normal"
            else:
                today_note = "Bottom 10% for the date"
        today_note = _clip(today_note, 38)

        # today_flag (DATA_CONTRACT v3): short label for a hero temperature number
        if day_stats["all_time_hot_record"]:
            today_flag = "Record high"
        elif day_stats["all_time_cold_record"]:
            today_flag = "Record low"
        elif pct >= 90:
            today_flag = "Top 10% for the date"
        elif pct >= 75:
            today_flag = "Warm side of normal"
        elif pct >= 25:
            today_flag = "Mid-pack for %s" % month_word
        elif pct >= 10:
            today_flag = "Cool side of normal"
        else:
            today_flag = "Bottom 10% for the date"
        today_flag = _clip(today_flag, 24)

        # today_rain_line (DATA_CONTRACT v3)
        has_rain_today = precip_today_mm is not None and precip_today_mm >= 1.0
        if mode == "forecast":
            if has_rain_today:
                today_rain_line = "%s%s rain likely" % (_num_str(precip_out), rain_unit_word)
            else:
                today_rain_line = "No rain expected"
        else:
            if has_rain_today:
                today_rain_line = "%s%s rain fell" % (_num_str(precip_out), rain_unit_word)
            else:
                today_rain_line = "No rain"
        today_rain_line = _clip(today_rain_line, 22)

        # today_night_line (DATA_CONTRACT v3)
        if tropical_night:
            today_night_line = "Tropical night ahead" if mode == "forecast" else "A tropical night"
        else:
            today_night_line = ""

        # yday_line / yday_note (record-aware per DATA_CONTRACT v3)
        yday_out = _fmt_temp(yday_hi_c, units) if yday_hi_c is not None else None
        if yday_hi_c is not None and yday_record_kind is not None:
            if yday_record_kind == "hot_record":
                yday_note_frag = "Hottest %s on record" % yday_date_label
            elif yday_record_kind == "cold_record":
                yday_note_frag = "Coldest %s on record" % yday_date_label
            elif yday_record_kind == "hot_since":
                yday_note_frag = "Hottest %s since %d" % (yday_date_label, yday_record_year)
            else:  # cold_since
                yday_note_frag = "Coldest %s since %d" % (yday_date_label, yday_record_year)
            yday_line = "Hit %s%s — %s" % (_num_str(yday_out), deg, yday_note_frag.lower())
            yday_note = yday_note_frag
        elif yday_hi_c is not None and yday_band is not None:
            band_words = {
                "much above normal": "way hotter than usual",
                "above normal": "hotter than usual",
                "near normal": "a normal day",
                "below normal": "cooler than usual",
                "much below normal": "way cooler than usual",
            }
            words = band_words.get(yday_band, "a normal day")
            if yday_band == "near normal":
                yday_line = "Hit %s%s — %s" % (_num_str(yday_out), deg, words)
                yday_note = "A normal day"
            else:
                yday_line = "Hit %s%s — %s for the date" % (_num_str(yday_out), deg, words)
                yday_note = (words[0].upper() + words[1:] + " for the date")
        else:
            yday_line = "No data for yesterday"
            yday_note = "No data for yesterday"

        if yday_hi_c is not None:
            if yday_prcp_mm is not None and yday_prcp_mm >= 5.0:
                yday_prcp_out = _fmt_precip(yday_prcp_mm, units)
                yday_line += " · %s%s rain" % (_num_str(yday_prcp_out), rain_unit_word)
            yday_line = _clip(yday_line, 46)
        yday_note = _clip(yday_note, 44)

        # t30_line / t30_delta
        if anom30_out is not None:
            sign = "+" if anom30_out >= 0 else ""
            t30_line = "Last 30 days ran %s%s%s vs normal" % (sign, _num_str(anom30_out), deg)
            t30_delta = "%s%s" % (sign, _num_str(anom30_out))
        else:
            t30_line = "Not enough data for the last 30 days"
            t30_delta = None
        t30_line = _clip(t30_line, 40)

        # rain_line
        if is_dry_notable:
            dry_label = "31+" if dry_capped else str(dry_days)
            if dryspell_clim_mean is not None:
                clim_out = _fmt_precip(dryspell_clim_mean, units)
                rain_line = "No rain in %s days — usually %s%s falls" % (
                    dry_label, _rain_amt_str(clim_out, units), rain_unit_word)
            else:
                rain_line = "No rain in %s days" % dry_label
        elif last30_prcp_total is not None:
            total_out = _fmt_precip(last30_prcp_total, units)
            if clim_prcp_window_mean is not None:
                clim_out = _fmt_precip(clim_prcp_window_mean, units)
                rain_line = "%s%s rain in 30 days — usually %s%s" % (
                    _rain_amt_str(total_out, units), rain_unit_word,
                    _rain_amt_str(clim_out, units), rain_unit_word)
            else:
                rain_line = "%s%s rain in the last 30 days" % (_rain_amt_str(total_out, units), rain_unit_word)
        else:
            rain_line = "Not enough rain data"
        rain_line = _clip(rain_line, 44)

        # --- years array (day-high, same-date, ascending, current year excluded) ---
        years_out = []
        for (yr, t, _tn, _pr) in sorted(same_date_full, key=lambda x: x[0]):
            v = _fmt_temp(t, units)
            if v is not None:
                years_out.append([yr, v])

        result = {
            "ok": True,
            "error": "",
            "mode": mode,
            "units": units,
            "loc": loc,
            "date_label": date_label,
            "updated_at": updated_at,

            "headline": headline,
            "verdict": verdict,
            "subline": subline,

            "today_title": today_title,
            "today_line": today_line,
            "today_note": today_note,
            "today_flag": today_flag,
            "today_rain_line": today_rain_line,
            "today_night_line": today_night_line,
            "yday_line": yday_line,
            "yday_hi": yday_out,
            "yday_note": yday_note,
            "t30_line": t30_line,
            "t30_delta": t30_delta,
            "rain_line": rain_line,

            "normal_lo": normal_lo_out,
            "normal_hi": normal_hi_out,
            "record_hi": record_hi_out,
            "record_hi_year": record_hi_year,
            "record_lo": record_lo_out,
            "record_lo_year": record_lo_year,

            "hi": hi_out,
            "lo": lo_out,
            "temp_now": temp_now_out,
            "precip": precip_out,
            "pct": pct_int,
            "band": band,

            "years": years_out,
            "since_year": since_year,
        }
        return result

    except Exception:
        try:
            offset = input.get("utc_offset_seconds") if isinstance(input, dict) else 0
            if not isinstance(offset, (int, float)):
                offset = 0
            tz = datetime.timezone(datetime.timedelta(seconds=offset))
            updated_at = datetime.datetime.now(datetime.timezone.utc).astimezone(tz).strftime("%H:%M")
        except Exception:
            updated_at = datetime.datetime.utcnow().strftime("%H:%M")
        return {"ok": False, "error": "Something went wrong fetching the weather",
                "updated_at": updated_at}


def _better_years_sub(better_years_desc, date_label, comparative):
    """rank-tier subline: cite the 1-2 more-recent years that beat today.
    better_years_desc: years that beat today's value, sorted descending
    (most recent first)."""
    if not better_years_desc:
        return "Nothing else came close."
    picked = better_years_desc[:2]
    if len(picked) == 1:
        return "Only %d saw a %s %s." % (picked[0], comparative, date_label)
    return "Only %d and %d saw a %s %s." % (picked[0], picked[1], comparative, date_label)


def _since_sub(normal_lo_out, normal_hi_out, hi_out, mode, deg, obs_word_high):
    lo_i = _int_str(normal_lo_out)
    hi_i = _int_str(normal_hi_out)
    if mode == "forecast":
        return "Normal high is %s–%s%s — today should reach %s%s." % (lo_i, hi_i, deg, _num_str(hi_out), deg)
    return "Normal high is %s–%s%s — today hit %s%s." % (lo_i, hi_i, deg, _num_str(hi_out), deg)


def _normal_verdict_sub(deg, precip_today_mm, precip_out, units,
                         normal_lo_out, normal_hi_out, hi_out, mode,
                         month_word, yday_hi_c, hi_c):
    """Pick first available per METHODOLOGY §6:
    (a) vs yesterday when |diff| >= 1.5 degrees C (threshold is always in
        Celsius internally, like the tropical-night threshold, so the same
        real-world gap triggers regardless of display units)
    (b) rain today >= 3mm
    (c) fallback: placement inside normal range."""
    lo_i = _int_str(normal_lo_out)
    hi_i = _int_str(normal_hi_out)

    # Where does today actually sit relative to the normal band? Tier 12
    # ("Warmer/Cooler than usual", pct >=75 / <=25) can land outside it, so the
    # subline must not claim "comfortably in the usual range" unconditionally.
    if hi_out > normal_hi_out:
        placement = "above"
    elif hi_out < normal_lo_out:
        placement = "below"
    else:
        placement = "inside"

    if yday_hi_c is not None and hi_c is not None:
        diff_c = hi_c - yday_hi_c
        if abs(diff_c) >= 1.5:
            yday_out = _fmt_temp(yday_hi_c, units)
            diff_out = hi_out - yday_out
            word = "cooler" if diff_c < 0 else "hotter"
            if placement == "inside":
                tail = "comfortably in the usual %s–%s%s" % (lo_i, hi_i, deg)
            elif placement == "above":
                tail = "on the warm side of normal"
            else:
                tail = "on the cool side of normal"
            return "About %s%s %s than yesterday, and %s." % (
                _int_str(abs(diff_out)), deg, word, tail)

    rain_unit_word = "in" if units == "F" else "mm"
    if precip_today_mm is not None and precip_today_mm >= 3.0:
        return "The %s%s of rain is the day's only story — temperatures are textbook." % (
            _num_str(precip_out), rain_unit_word)

    if placement == "above":
        return "The high sits just above the usual %s–%s%s for early %s." % (
            lo_i, hi_i, deg, month_word)
    if placement == "below":
        return "The high sits just below the usual %s–%s%s for early %s." % (
            lo_i, hi_i, deg, month_word)
    return "The high sits squarely inside the usual %s–%s%s for early %s." % (
        lo_i, hi_i, deg, month_word)

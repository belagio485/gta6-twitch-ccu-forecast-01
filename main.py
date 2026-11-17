"""Forecast launch-day Twitch viewers for hours 2..24, as five quantiles.

The program reads every ``*.in`` case file in the input directory and
writes one ``<id>.out`` forecast per case to the output directory.  Each
forecast anchors on the case's hour-1 print and borrows the shape of the
day from the case's comparables: past launches whose release-day hourly
counts are bundled with the case.  For every hour, the log-ratio of that
hour's viewers to hour 1 is pooled across comparables; the pooled median
sets the forecast median and the pooled spread sets the quantile width.

Only the standard library is used.  The program is deterministic: no
randomness, no clock, no network, and the same input bytes always yield
the same output bytes.  Any unexpected input shape falls back to a valid
persistence-style forecast, so a run never crashes and never emits an
invalid file.
"""

import json
import math
import os
import sys
from datetime import datetime, timezone

# Standard-normal quantiles at the five required levels, in level order.
_Z_VALUES = (-1.6448536269514722, -0.6744897501960817, 0.0,
             0.6744897501960817, 1.6448536269514722)
_HOURS = range(2, 25)
_MAX_Q = 1.0e9
_MIN_Q = 0.01
_LOG_MIN_Q = math.log(_MIN_Q)
_LOG_MAX_Q = math.log(_MAX_Q)
# A single comparable's log-ratio is capped here so one degenerate launch
# (for example an hour-1 print of a single viewer) cannot dominate.
_RATIO_CAP = 6.0
# Calibration factor on the comparable-based spread.  The raw IQR
# spread understates the realised dispersion because it omits the
# error in the pooled median itself; the leave-one-out backtest
# showed 90% coverage of only 0.80 without it.  A factor of 1.2 lifts
# coverage toward nominal and lowers the backtest pinball loss.
_SPREAD_CALIBRATION = 1.2


def _num(value):
    """Return value as a finite float, or None when it is not a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
        if math.isfinite(out):
            return out
    return None


def _epoch_seconds(text):
    """Parse an ISO-8601 timestamp to epoch seconds, or None.

    Naive timestamps are read as UTC, matching the case format.  The
    parser accepts a trailing ``Z``, explicit offsets and fractional
    seconds, and rejects anything else instead of guessing.
    """
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    try:
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        moment = datetime.fromisoformat(s)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        return moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _median(ordered):
    """Median of an already-sorted non-empty list, or None when empty."""
    n = len(ordered)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _quantile(ordered, q):
    """Linear-interpolation quantile of an already-sorted list."""
    n = len(ordered)
    if n == 0:
        return None
    if n == 1:
        return ordered[0]
    pos = q * (n - 1)
    low = int(math.floor(pos))
    high = min(low + 1, n - 1)
    frac = pos - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


def _hour_one_print(case):
    """Hour-1 viewer print, floored at 1, from whatever the case offers.

    ``known`` is a one-row list in the practice cases and may be a single
    object elsewhere, so both shapes are accepted.  When no usable print
    exists, the newest history point is used, and finally 1 viewer.
    """
    if isinstance(case, dict):
        known = case.get("known")
        rows = None
        if isinstance(known, dict):
            rows = [known]
        elif isinstance(known, list):
            rows = known
        if rows:
            for row in rows:
                if isinstance(row, dict):
                    value = _num(row.get("viewers"))
                    if value is not None and value > 0:
                        return value
        for key in ("history_hourly", "history"):
            seq = case.get(key)
            if isinstance(seq, list):
                for row in reversed(seq):
                    if isinstance(row, dict):
                        value = _num(row.get("viewers"))
                        if value is not None and value > 0:
                            return value
    return 1.0


def _release_day_viewers(comp):
    """Map hour offset 0..23 to viewers for a comparable's release day.

    Only points inside the 24 hours starting at the comparable's own
    ``day_start`` are kept, and only positive finite counts.  The first
    usable point wins when an hour appears twice.
    """
    if not isinstance(comp, dict):
        return {}
    start = _epoch_seconds(comp.get("day_start"))
    if start is None:
        return {}
    hourly = comp.get("hourly")
    if not isinstance(hourly, list):
        return {}
    out = {}
    for row in hourly:
        if not isinstance(row, dict):
            continue
        when = _epoch_seconds(row.get("from"))
        if when is None:
            continue
        offset = int((when - start) // 3600)
        if 0 <= offset < 24 and offset not in out:
            value = _num(row.get("viewers"))
            if value is not None and value > 0:
                out[offset] = value
    return out


def _pooled_ratios(case):
    """Per-hour pooled log-ratios ln(viewers_h / viewers_hour1).

    One list per forecast hour, across every comparable that has a
    positive hour-1 print and a positive count for that hour.  A
    comparable is skipped when it looks like the case's own launch
    (same game name, or the same release day), so the case can never
    borrow its own answer.  The pre-release history entry drops out on
    its own: it has no release-day hour 1.
    """
    buckets = {hour: [] for hour in _HOURS}
    if not isinstance(case, dict):
        return buckets
    comps = case.get("comparables")
    if not isinstance(comps, list):
        return buckets
    case_day = _epoch_seconds(case.get("day_start"))
    game_name = ""
    game = case.get("game")
    if isinstance(game, dict) and isinstance(game.get("name"), str):
        game_name = game["name"].strip().lower()
    for comp in comps:
        if not isinstance(comp, dict):
            continue
        if game_name and isinstance(comp.get("name"), str):
            if comp["name"].strip().lower() == game_name:
                continue
        comp_day = _epoch_seconds(comp.get("day_start"))
        if case_day is not None and comp_day is not None:
            if comp_day == case_day:
                continue
        series = _release_day_viewers(comp)
        first = series.get(0)
        if first is None:
            continue
        log_first = math.log(first)
        for hour in _HOURS:
            value = series.get(hour - 1)
            if value is None:
                continue
            ratio = math.log(value) - log_first
            if ratio > _RATIO_CAP:
                ratio = _RATIO_CAP
            elif ratio < -_RATIO_CAP:
                ratio = -_RATIO_CAP
            buckets[hour].append(ratio)
    return buckets


def _preheat_damp(case, print_one):
    """Small damping when the category was already hot before midnight.

    Compares the median of the last few whole-hour means strictly before
    the release day with the hour-1 print.  A category already near its
    hour-1 level before the UTC day began was partly unlocked already,
    so the pooled early rise is overstated for it.  The damping is
    capped at 25% and only touches the first few forecast hours; when
    the pre-day level is at most a fifth of the print, it is zero.
    """
    if print_one <= 0:
        return 0.0
    if not isinstance(case, dict):
        return 0.0
    hourly = case.get("history_hourly")
    if not isinstance(hourly, list) or not hourly:
        return 0.0
    start = _epoch_seconds(case.get("day_start"))
    values = []
    for row in reversed(hourly):
        if len(values) >= 6:
            break
        if not isinstance(row, dict):
            continue
        if start is not None:
            when = _epoch_seconds(row.get("from"))
            if when is not None and when >= start:
                continue
        value = _num(row.get("viewers"))
        if value is not None and value > 0:
            values.append(value)
    if not values:
        return 0.0
    values.sort()
    pre = _median(values)
    if pre is None or pre <= 0:
        return 0.0
    ratio = pre / print_one
    if ratio <= 0.2:
        return 0.0
    return 0.25 * min(1.0, (ratio - 0.2) / 0.8)


def _clamp_quantiles(quants):
    """Clamp into the allowed range and enforce non-decreasing order."""
    fixed = [min(max(q, _MIN_Q), _MAX_Q) for q in quants]
    fixed.sort()
    return fixed


def _forecast_rows(case):
    """Build the 23 forecast rows for one case."""
    print_one = _hour_one_print(case)
    if print_one < 1.0:
        print_one = 1.0
    log_p = math.log(print_one)
    buckets = _pooled_ratios(case)
    damp = _preheat_damp(case, print_one)
    rows = []
    for hour in _HOURS:
        values = buckets.get(hour) or []
        count = len(values)
        if count == 0:
            # No comparable evidence for this hour: hold the print with
            # a spread that widens with the horizon.
            center = 0.0
            spread = 0.15 * math.sqrt(hour - 1)
        else:
            ordered = sorted(values)
            center = _median(ordered)
            # Shrink the pooled shape toward persistence.  With many
            # comparables the shrinkage is small; with few, the shape
            # is trusted less.
            center = center * (count / (count + 3.0))
            if hour <= 6 and damp > 0.0:
                center = center * (1.0 - damp)
            if count >= 4:
                lower = _quantile(ordered, 0.25)
                upper = _quantile(ordered, 0.75)
                sigma = (upper - lower) / 1.349
            else:
                sigma = 0.40
            base = sigma if sigma > 0.15 else 0.15
            spread = math.sqrt(base * base + 0.005 * (hour - 1))
            spread = spread * _SPREAD_CALIBRATION
            if spread > 1.25:
                spread = 1.25
        quants = []
        for z in _Z_VALUES:
            log_q = log_p + center + z * spread
            if log_q < _LOG_MIN_Q:
                log_q = _LOG_MIN_Q
            elif log_q > _LOG_MAX_Q:
                log_q = _LOG_MAX_Q
            quants.append(round(math.exp(log_q), 2))
        rows.append({"hour": hour, "q": _clamp_quantiles(quants)})
    return rows


def _fallback_rows(case):
    """Persistence forecast: every hour holds the hour-1 print."""
    try:
        print_one = _hour_one_print(case)
    except Exception:
        print_one = 1.0
    if print_one < 1.0:
        print_one = 1.0
    log_p = math.log(print_one)
    rows = []
    for hour in _HOURS:
        spread = 0.15 * math.sqrt(hour - 1)
        quants = []
        for z in _Z_VALUES:
            log_q = log_p + z * spread
            log_q = min(max(log_q, _LOG_MIN_Q), _LOG_MAX_Q)
            quants.append(round(math.exp(log_q), 2))
        rows.append({"hour": hour, "q": _clamp_quantiles(quants)})
    return rows


def _rows_valid(rows):
    """Check the 23 rows meet the output contract before writing."""
    if not isinstance(rows, list) or len(rows) != 23:
        return False
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"hour", "q"}:
            return False
        hour = row["hour"]
        if hour not in _HOURS or hour in seen:
            return False
        seen.add(hour)
        quants = row["q"]
        if not isinstance(quants, list) or len(quants) != 5:
            return False
        prev = None
        for q in quants:
            value = _num(q)
            if value is None or value <= 0 or value > _MAX_Q:
                return False
            if prev is not None and value < prev:
                return False
            prev = value
    return len(seen) == 23


def forecast_case(case):
    """Forecast payload ``{"hours": [...]}`` for one parsed case dict."""
    try:
        rows = _forecast_rows(case)
    except Exception:
        rows = None
    if rows is None or not _rows_valid(rows):
        try:
            rows = _fallback_rows(case)
        except Exception:
            rows = _fallback_rows({})
    return {"hours": rows}


def _safe_case_id(case, stem):
    """Output stem: the case id when it is a plain safe name, else the
    input file's stem (which equals the id in every real run)."""
    if isinstance(case, dict):
        case_id = case.get("id")
        if isinstance(case_id, str):
            cleaned = case_id.strip()
            if cleaned and cleaned not in (".", ".."):
                if all(ch.isalnum() or ch in "-_." for ch in cleaned):
                    return cleaned
    return stem


def main(argv=None):
    """Read every ``*.in`` file in name order; write one ``*.out`` each."""
    in_dir = os.environ.get("MAKEFUN_INPUT_DIR") or "/input/"
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR") or "/output/"
    try:
        names = sorted(n for n in os.listdir(in_dir) if n.endswith(".in"))
    except OSError:
        names = []
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        return 0
    for name in names:
        stem = name[:-3]
        in_path = os.path.join(in_dir, name)
        case = None
        try:
            with open(in_path, "r", encoding="utf-8") as handle:
                case = json.load(handle)
        except Exception:
            case = None
        if not isinstance(case, dict):
            case = {}
        try:
            payload = forecast_case(case)
        except Exception:
            payload = {"hours": _fallback_rows({})}
        out_name = _safe_case_id(case, stem) + ".out"
        out_path = os.path.join(out_dir, out_name)
        try:
            text = json.dumps(payload, ensure_ascii=False,
                              separators=(",", ":"))
            with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.write("\n")
        except OSError:
            continue
    return 0


if __name__ == "__main__":
    sys.exit(main())

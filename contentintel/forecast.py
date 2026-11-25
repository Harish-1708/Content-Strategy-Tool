"""Batch 3: forecasting with TimesFM 3.0 (Google's time-series foundation model; licence accepted by the owner for this personal, non-commercial tool). Four uses, one model:
  * FORECAST: the next values of a series (weekly activity of a source, a brand's weekly audience engagement, an Observatory metric);
  * ANOMALY DETECTION: a point far outside what the model (or the naive baseline) expected for it;
  * TREND MOMENTUM: is a signal accelerating, steady or fading;
  * IDEA BANK TIMELINESS: ideas whose topic is on a rising signal are timely, ideas on a fading one are not.
HONEST GATE: a forecast model is used only if it BEATS a naive baseline (repeat the last value, or the same week of the last cycle) on at least 60% of held-out series (`backtest`), within the CPU time limit; until
that has been shown on this tool's own data, every answer is labelled with the method that made it, and a series with too little history says so instead of forecasting.
The model is loaded lazily and injected in tests: the library and its weights are installed on the runner by the workflow, never needed to import this file."""
import statistics
import time

MIN_HISTORY = 12                  # a series shorter than this is not forecast: it is reported as 'not enough history'
GATE_SHARE = 0.60                 # the model must beat the naive baseline on this share of held-out series
GATE_SECONDS = 600                # CPU seconds the whole backtest may take


def first_forecast(outputs, horizon):
    """The first series' forecast as a list of floats, from whatever the model call returned: a GENERATOR (it cannot be indexed: MODEL LOAD FAILED: TypeError: 'generator' object is not subscriptable, Batch 3
    tools #7), a list or tuple of results, a result with a `.forecast`, an array with `.tolist()`, or a nested list of one row."""
    results = list(outputs)
    if not results:
        raise ValueError("the model returned no forecast")
    first = results[0]
    values = getattr(first, "forecast", first)
    values = getattr(values, "tolist", lambda: values)()
    flat = values[0] if values and isinstance(values[0], (list, tuple)) else values
    return [float(x) for x in list(flat)[:horizon]]


def naive(series, horizon, season=0):
    """The baseline: the last value repeated, or, when a season is given and there is a full season of history, the same position of the last season."""
    if season and len(series) >= season:
        last = series[-season:]
        return [last[i % season] for i in range(horizon)]
    return [series[-1]] * horizon


TIMESFM_CHECKPOINT = "google/timesfm-3.0-pytorch"                     # TimesFM 3.0, as the owner ruled; there is NO fallback to an older version


def load_timesfm(horizon_limit=64):
    """forecast_fn for TimesFM 3.0 (pip install "timesfm[torch]", weights google/timesfm-3.0-pytorch, CPU), or raises ImportError. It never falls back to 2.0 or 2.5: a runner whose library has no
    timesfm3 module reports that instead. forecast_fn(series, horizon) -> list of floats. The weights are under Google's non-commercial licence, accepted by the owner for this personal tool."""
    try:
        import numpy as np
        from timesfm3 import TimesFM3Forecaster                                          # the 3.0 package's own entry point (checked against the published timesfm 3.0.2 wheel)
    except ImportError as exc:
        raise ImportError(f'TimesFM 3.0 could not be imported ({type(exc).__name__}: {exc}); installed timesfm: {installed_version()}; it needs timesfm 3.0 or newer with the torch extra: pip install "timesfm[torch]>=3.0"; there is no fallback to an older version') from None
    forecaster = TimesFM3Forecaster.from_pretrained(TIMESFM_CHECKPOINT, device="cpu", per_core_batch_size=8)

    def run(series, horizon):
        out = forecaster.predict(np.asarray(series, dtype=np.float32), horizon)          # a ForecastOutput: .forecast is the point forecast
        return first_forecast([out], horizon)
    return run


def installed_version():
    """The installed timesfm distribution's version (or 'not installed'), for the load message: a wrong version was the suspected cause of the first live load failures."""
    try:
        from importlib import metadata
        return metadata.version("timesfm")
    except Exception:                                                                    # noqa: BLE001
        return "not installed"


def mae(actual, predicted):
    return sum(abs(a - p) for a, p in zip(actual, predicted)) / max(1, len(actual))


def backtest(series_list, model_fn, *, horizon=4, season=0, clock=time.monotonic):
    """Hold out the last `horizon` points of every series long enough to test, forecast them with the model and with the naive baseline and count the wins. Returns {tested, model_wins, share, gate, seconds}.
    gate is 'PASS' only with at least GATE_SHARE wins on at least 5 series within GATE_SECONDS; 'NOT ENOUGH SERIES' under 5; otherwise 'FAIL'. The model is never trusted before this passes."""
    t0, tested, wins = clock(), 0, 0
    for s in series_list:
        values = [float(v) for v in s]
        if len(values) < MIN_HISTORY + horizon:
            continue
        train, held = values[:-horizon], values[-horizon:]
        tested += 1
        try:
            guess = model_fn(train, horizon)
        except Exception:                                                                            # noqa: BLE001 - a model that errors on a series did not beat the baseline on it
            continue
        if mae(held, guess) < mae(held, naive(train, horizon, season)):
            wins += 1
        if clock() - t0 > GATE_SECONDS:
            break
    seconds = round(clock() - t0, 2)
    share = round(wins / tested, 3) if tested else 0.0
    gate = "NOT ENOUGH SERIES" if tested < 5 else ("PASS" if share >= GATE_SHARE and seconds <= GATE_SECONDS else "FAIL")
    return {"tested": tested, "model_wins": wins, "share": share, "gate": gate, "seconds": seconds}


def forecast(series, horizon=4, *, model_fn=None, season=0):
    """{method, values, note}. Uses the model when one is given, else the naive baseline, and says which; too little history returns method 'none'."""
    values = [float(v) for v in series]
    if len(values) < MIN_HISTORY:
        return {"method": "none", "values": [], "note": f"not enough history: {len(values)} point(s), at least {MIN_HISTORY} are needed"}
    if model_fn is not None:
        try:
            return {"method": "timesfm", "values": [round(x, 4) for x in model_fn(values, horizon)], "note": ""}
        except Exception as exc:                                                                       # noqa: BLE001
            return {"method": "naive", "values": [round(x, 4) for x in naive(values, horizon, season)], "note": f"the model failed ({type(exc).__name__}), the naive baseline was used"}
    return {"method": "naive", "values": [round(x, 4) for x in naive(values, horizon, season)], "note": "no model on this machine: the naive baseline"}


def anomalies(series, *, model_fn=None, window=MIN_HISTORY, z=3.0):
    """Points far outside what was expected for them: each point from `window` on is compared with a one-step forecast from the points before it (the model's, else the previous value); a point is an
    anomaly when its error is more than z standard deviations of the earlier errors. Returns [{index, value, expected, z}]."""
    values = [float(v) for v in series]
    errors, out = [], []
    for i in range(window, len(values)):
        past = values[:i]
        try:
            expected = model_fn(past, 1)[0] if model_fn else past[-1]
        except Exception:                                                                              # noqa: BLE001
            expected = past[-1]
        err = values[i] - expected
        if len(errors) >= 5:
            sd = statistics.pstdev(errors) or 1e-9
            score = abs(err - statistics.fmean(errors)) / sd
            if score > z:
                out.append({"index": i, "value": values[i], "expected": round(expected, 4), "z": round(score, 2)})
        errors.append(err)
    return out


def momentum(series, window=4):
    """Is the signal accelerating, steady or fading: the mean of the last `window` points against the `window` before them. Returns {label, change, recent, before}; with under 2 windows of history the label is 'unknown'."""
    values = [float(v) for v in series]
    if len(values) < 2 * window:
        return {"label": "unknown", "change": 0.0, "recent": 0.0, "before": 0.0}
    recent, before = statistics.fmean(values[-window:]), statistics.fmean(values[-2 * window:-window])
    change = (recent - before) / before if before else (1.0 if recent else 0.0)
    label = "accelerating" if change >= 0.25 else "fading" if change <= -0.25 else "steady"
    return {"label": label, "change": round(change, 3), "recent": round(recent, 3), "before": round(before, 3)}


def timeliness(ideas, signals):
    """Idea Bank timeliness. ideas: [{id, title, topic}]; signals: {name: momentum-result}. An idea whose topic or title shares a word with a signal's name takes that signal's momentum: rising ideas are timely,
    fading ones are not, ideas with no matching signal are 'unknown' (never guessed). Returns [{id, timeliness: timely|steady|fading|unknown, signal, change}] best first."""
    def words(text):
        return {w for w in "".join(c.lower() if c.isalnum() else " " for c in str(text or "")).split() if len(w) >= 4}
    out = []
    for idea in ideas:
        mine = words(idea.get("topic")) | words(idea.get("title"))
        best = None
        for name, m in signals.items():
            if mine & words(name) and (best is None or abs(m.get("change", 0)) > abs(best[1].get("change", 0))):
                best = (name, m)
        if best is None or best[1].get("label") == "unknown":
            out.append({"id": idea.get("id"), "timeliness": "unknown", "signal": best[0] if best else "", "change": 0.0})
        else:
            label = {"accelerating": "timely", "steady": "steady", "fading": "fading"}[best[1]["label"]]
            out.append({"id": idea.get("id"), "timeliness": label, "signal": best[0], "change": best[1]["change"]})
    order = {"timely": 0, "steady": 1, "unknown": 2, "fading": 3}
    return sorted(out, key=lambda r: (order[r["timeliness"]], -r["change"]))


def weekly_counts(timestamps):
    """{ISO week: count} from ISO timestamps, as a dense list of weekly counts from the first week to the last (empty weeks are 0)."""
    from datetime import date, timedelta
    weeks = {}
    for ts in timestamps:
        try:
            d = date.fromisoformat(str(ts)[:10])
        except ValueError:
            continue
        monday = d - timedelta(days=d.weekday())
        weeks[monday] = weeks.get(monday, 0) + 1
    if not weeks:
        return []
    first, last = min(weeks), max(weeks)
    n = (last - first).days // 7 + 1
    return [weeks.get(first + timedelta(days=7 * i), 0) for i in range(n)]


def series_for_brand(store, slug):
    """The series this tool really has for a brand: weekly counts of what each collector source found (from the collected records' `at`), weekly audience-voice items and their engagement (from the voice
    store), and any Observatory series kept under extras/observatory_series.json ({name: [values]}). Returns {name: [floats]}."""
    series = {}
    by_source = {}
    for rec in (store.read_collected(slug) if hasattr(store, "read_collected") else []):
        if isinstance(rec, dict) and rec.get("at"):
            by_source.setdefault(f"found by {rec.get('source') or 'collector'}", []).extend([rec["at"]] * max(1, len(rec.get("items") or [])))
    for name, stamps in by_source.items():
        series[name] = [float(x) for x in weekly_counts(stamps)]
    voice = store.read_extra(slug, "voice") if hasattr(store, "read_extra") else None
    posted = [i.get("posted") or i.get("first_seen") for i in (voice or {}).get("items", []) if isinstance(i, dict) and (i.get("posted") or i.get("first_seen"))]
    if posted:
        series["audience voice"] = [float(x) for x in weekly_counts(posted)]
    obs = store.read_extra(slug, "observatory_series") if hasattr(store, "read_extra") else None
    for name, vals in (obs or {}).items() if isinstance(obs, dict) else []:
        if isinstance(vals, list) and all(isinstance(v, (int, float)) for v in vals):
            series[f"observatory {name}"] = [float(v) for v in vals]
    return series


def analyse_brand(store, slug, *, model_fn=None, horizon=4, ideas=()):
    """Everything for one brand: per series the forecast, anomalies and momentum, and the timeliness of the given ideas. Returns a plain dict ready to save."""
    series = series_for_brand(store, slug)
    out = {"series": {}, "method": "timesfm" if model_fn else "naive"}
    signals = {}
    for name, values in series.items():
        f = forecast(values, horizon, model_fn=model_fn)
        m = momentum(values)
        signals[name] = m
        out["series"][name] = {"points": len(values), "forecast": f, "momentum": m, "anomalies": anomalies(values, model_fn=model_fn) if len(values) >= MIN_HISTORY + 6 else []}
    out["timeliness"] = timeliness(list(ideas), signals)
    return out


def format_report(slug, analysis, gate=None):
    lines = [f"forecast: {slug}: method {analysis['method']}" + (f"; backtest {gate['gate']}: the model beat the naive baseline on {gate['model_wins']} of {gate['tested']} held-out series ({int(gate['share'] * 100)}%, needs {int(GATE_SHARE * 100)}%), {gate['seconds']} s" if gate else "; no backtest was run, so the model is NOT trusted over the baseline")]
    if not analysis["series"]:
        lines.append(f"forecast: {slug}: no series yet (nothing collected, no voice, no Observatory series)")
    for name, s in analysis["series"].items():
        f, m = s["forecast"], s["momentum"]
        lines.append(f"forecast: {slug}: {name}: {s['points']} weeks, " + (f"next {len(f['values'])}: {f['values']} ({f['method']})" if f["values"] else f["note"]) + f"; momentum {m['label']} ({m['change']:+.0%})" + (f"; {len(s['anomalies'])} anomaly(ies) at week index {[a['index'] for a in s['anomalies']][:5]}" if s["anomalies"] else ""))
    for t in analysis["timeliness"][:10]:
        lines.append(f"forecast: {slug}: idea {t['id']}: {t['timeliness']}" + (f" (signal: {t['signal']})" if t["signal"] else ""))
    return lines

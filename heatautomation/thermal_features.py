"""Build conservative, contiguous training intervals from observed history."""

import numpy as np
import pandas as pd


FEATURES = ["indoor_c", "outdoor_c", "lag_indoor_c",
            "heatpump_capacity_kw", "pelletstove_capacity_kw"]


def training_rows(records):
    frame = pd.DataFrame(records)
    columns = ["timestamp", "indoor_c", "outdoor_c", "heatpump_capacity_kw",
               "pelletstove_capacity_kw"]
    if frame.empty or not set(columns).issubset(frame.columns):
        return pd.DataFrame(columns=FEATURES + ["rate", "regime", "timestamp"])
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    for column in columns[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    valid = (frame["timestamp"].notna()
             & frame[columns[1:]].notna().all(axis=1)
             & np.isfinite(frame[columns[1:]]).all(axis=1)
             & frame["indoor_c"].between(-10, 50)
             & frame["outdoor_c"].between(-60, 60)
             & frame["heatpump_capacity_kw"].between(0, 30)
             & frame["pelletstove_capacity_kw"].between(0, 30))
    interval = frame["timestamp"].diff().dt.total_seconds()
    valid_interval = interval.between(210, 390)
    # A stable 30-minute history and a further 30 minutes after the label
    # exclude incomplete cycles, gaps, startup and shutdown transients.
    state = (frame["heatpump_capacity_kw"] > 0).astype(int) + (
        frame["pelletstove_capacity_kw"] > 0).astype(int) * 2
    boundary = (state.ne(state.shift()) | ~valid | ~valid_interval
                | ~valid.shift(1, fill_value=False))
    segments = boundary.cumsum()
    started = (valid & valid.shift(1, fill_value=False) & valid_interval
               & state.ne(state.shift()))
    ended = (valid & valid.shift(-1, fill_value=False)
             & valid_interval.shift(-1, fill_value=False) & state.ne(state.shift(-1)))
    complete_cycle = (started.groupby(segments).transform("any")
                      & ended.groupby(segments).transform("any"))
    stable = pd.Series(True, index=frame.index)
    for offset in range(-6, 7):
        stable &= valid.shift(offset, fill_value=False) & state.eq(state.shift(offset))
    for offset in range(-5, 7):
        stable &= valid_interval.shift(offset, fill_value=False)
    frame["lag_indoor_c"] = frame["indoor_c"].shift(6)
    next_seconds = interval.shift(-1)
    frame["rate"] = (frame["indoor_c"].shift(-1) - frame["indoor_c"]) * 3600 / next_seconds
    stable &= frame["rate"].abs().le(5)
    stable &= (state == 0) | complete_cycle
    frame["regime"] = np.where(state == 0, "cooling", "heating")
    return frame.loc[stable, FEATURES + ["rate", "regime", "timestamp"]].copy()

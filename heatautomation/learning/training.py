"""Chronological evaluation and periodic retraining of thermal rate models."""

import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

import joblib
import numpy as np
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error

from .features import FEATURES, training_rows


MIN_ROWS = 120


def train(database, model_path):
    rows = training_rows(database.history())
    models, metrics, support = {}, {}, {}
    for regime in ("cooling", "heating"):
        data = rows[rows["regime"] == regime]
        if len(data) < MIN_ROWS:
            logging.info("Thermal %s model needs %d qualifying intervals; found %d.",
                         regime, MIN_ROWS, len(data))
            continue
        split = int(len(data) * 0.8)
        # A 30-minute embargo avoids sharing lagged temperatures with validation.
        cutoff = data.iloc[split]["timestamp"] - timedelta(minutes=30)
        train_data = data[data["timestamp"] < cutoff]
        test_data = data.iloc[split:]
        if len(train_data) < 60 or len(test_data) < 20:
            continue
        candidate = Ridge(alpha=1.0)
        candidate.fit(train_data[FEATURES], train_data["rate"])
        predicted = candidate.predict(test_data[FEATURES])
        metrics[regime] = {
            "train_samples": len(train_data), "validation_samples": len(test_data),
            "mae_c_per_hour": float(mean_absolute_error(test_data["rate"], predicted)),
            "rmse_c_per_hour": float(np.sqrt(mean_squared_error(test_data["rate"], predicted))),
            "validation_start": test_data.iloc[0]["timestamp"].isoformat(),
            "validation_end": test_data.iloc[-1]["timestamp"].isoformat(),
        }
        model = Ridge(alpha=1.0).fit(data[FEATURES], data["rate"])
        models[regime] = model
        combos = ((data["heatpump_capacity_kw"] > 0).astype(int)
                  + 2 * (data["pelletstove_capacity_kw"] > 0).astype(int))
        support[regime] = {int(key): int(value) for key, value in combos.value_counts().items()}

    if not models:
        return False
    artifact = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                "features": FEATURES, "models": models, "metrics": metrics, "support": support}
    directory = os.path.dirname(os.path.abspath(model_path))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".joblib", delete=False) as handle:
            temporary = handle.name
        joblib.dump(artifact, temporary)
        os.replace(temporary, model_path)
        temporary = None
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.remove(temporary)
    database.save_evaluation(metrics)
    logging.info("Thermal models trained: %s", metrics)
    return True

"""Read-only thermal forecasts from explicitly supplied weather and heat plans."""

import math

import joblib
import pandas as pd

if __package__:
    from .thermal_features import FEATURES
else:
    from thermal_features import FEATURES


class PredictionUnavailable(ValueError):
    """Insufficient observations, model coverage, or plan information."""


class ThermalPredictor:
    def __init__(self, model_path):
        # Only load locally generated, trusted joblib files: pickle can execute code.
        try:
            self.artifact = joblib.load(model_path)
        except FileNotFoundError as error:
            raise PredictionUnavailable("No trained thermal model is available yet") from error
        if self.artifact.get("version") != 1 or self.artifact.get("features") != FEATURES:
            raise PredictionUnavailable("Unsupported thermal model format")

    def _rate(self, indoor, outdoor, lag, pump, stove):
        values = [indoor, outdoor, lag, pump, stove]
        if not all(math.isfinite(value) for value in values) or pump < 0 or stove < 0:
            raise ValueError("Temperatures and non-negative capacities must be finite")
        combination = int(pump > 0) + 2 * int(stove > 0)
        regime = "heating" if combination else "cooling"
        model = self.artifact["models"].get(regime)
        count = self.artifact["support"].get(regime, {}).get(combination, 0)
        if model is None or count < 20:
            raise PredictionUnavailable("Insufficient data for heating combination {}".format(combination))
        rate = float(model.predict(pd.DataFrame([values], columns=FEATURES))[0])
        return rate, self.artifact["metrics"][regime]["rmse_c_per_hour"]

    def forecast(self, recent_indoor_c, outdoor_c, heating_plan, target_c=None):
        """Forecast 72 five-minute steps. Inputs must cover the entire six hours.

        recent_indoor_c contains seven oldest-to-newest five-minute readings;
        outdoor_c and heating_plan each contain 72 values, one per future step.
        Plan entries map heatpump and pelletstove to estimated capacity in kW.
        """
        if len(recent_indoor_c) != 7 or len(outdoor_c) != 72 or len(heating_plan) != 72:
            raise ValueError("Expected 7 recent temperatures and 72 weather and plan steps")
        if target_c is not None and not math.isfinite(target_c):
            raise ValueError("Target temperature must be finite")
        temperatures = list(recent_indoor_c)
        if not all(math.isfinite(value) for value in temperatures):
            raise ValueError("Recent indoor temperatures must be finite")
        predictions, error, reached = {}, 0.0, None
        if target_c is not None and temperatures[-1] >= target_c:
            reached = 0.0
        for index, (outdoor, plan) in enumerate(zip(outdoor_c, heating_plan), 1):
            rate, rmse = self._rate(temperatures[-1], outdoor, temperatures[-7],
                                    plan["heatpump"], plan["pelletstove"])
            temperatures.append(temperatures[-1] + rate / 12)
            error += rmse / 12
            if target_c is not None and reached is None and temperatures[-1] >= target_c:
                reached = index / 12
            if index in (12, 24, 36, 72):
                predictions[index // 12] = {
                    "temperature_c": temperatures[-1],
                    "error_scale_c": error,
                }
        return {"predictions": predictions, "heat_up_hours": reached,
                "error_description": "Sum of one-step holdout rate RMSE; heuristic, not a confidence interval",
                "model_metrics": self.artifact["metrics"]}

    def cooling_rate(self, recent_indoor_c, outdoor_c):
        if len(recent_indoor_c) != 7:
            raise ValueError("Expected 7 recent temperatures")
        rate, rmse = self._rate(recent_indoor_c[-1], outdoor_c, recent_indoor_c[0], 0, 0)
        return {"rate_c_per_hour": rate, "holdout_rmse_c_per_hour": rmse}

    def heating_rate(self, recent_indoor_c, outdoor_c, heatpump_kw, pelletstove_kw):
        if len(recent_indoor_c) != 7:
            raise ValueError("Expected 7 recent temperatures")
        if heatpump_kw <= 0 and pelletstove_kw <= 0:
            raise ValueError("At least one heating source must have positive capacity")
        rate, rmse = self._rate(recent_indoor_c[-1], outdoor_c, recent_indoor_c[0],
                                heatpump_kw, pelletstove_kw)
        return {"rate_c_per_hour": rate, "holdout_rmse_c_per_hour": rmse}

    def source_response(self, source):
        indexes = {"heatpump": (3, 1), "pelletstove": (4, 2)}
        if source not in indexes:
            raise ValueError("Unknown heating source")
        index, combination = indexes[source]
        model = self.artifact["models"].get("heating")
        if model is None or self.artifact["support"].get("heating", {}).get(combination, 0) < 20:
            raise PredictionUnavailable("Insufficient independent source observations")
        return {"rate_per_nominal_kw": float(model.coef_[index]),
                "description": "Correlational estimate, not measured thermal output"}

    def heating_demand(self, recent_indoor_c, outdoor_c):
        cooling = self.cooling_rate(recent_indoor_c, outdoor_c)["rate_c_per_hour"]
        heating = self.artifact["models"].get("heating")
        support = self.artifact["support"].get("heating", {})
        if heating is None:
            raise PredictionUnavailable("Heating model is unavailable")
        responses = [
            float(heating.coef_[index])
            for index, combination in ((3, 1), (4, 2))
            if support.get(combination, 0) >= 20 and heating.coef_[index] > 0
        ]
        if not responses:
            raise PredictionUnavailable("No sufficiently observed positive source response")
        return {"estimated_kw": max(0.0, -cooling) / (sum(responses) / len(responses)),
                "description": "Approximate capacity-equivalent demand; not measured heat output"}

"""Cost-based heat-source selection from installation-specific measurements."""

import logging
import math


def interpolate(curve, temperature):
    if temperature >= curve[0][0]:
        return curve[0][1]
    for (upper_t, upper_v), (lower_t, lower_v) in zip(curve, curve[1:]):
        if temperature >= lower_t:
            fraction = (temperature - lower_t) / (upper_t - lower_t)
            return lower_v + fraction * (upper_v - lower_v)
    return curve[-1][1]


def select(temperature, price, capacity, pellet_capacity, pellet_cost, cop_curve,
           demand_curve, hysteresis, current=None, spot_limit=0):
    if not math.isfinite(temperature) or not math.isfinite(price):
        raise ValueError("Temperature and electricity price must be finite")
    cop = interpolate(cop_curve, temperature)
    demand = interpolate(demand_curve, temperature)
    pump_cost = price / cop
    logging.info(
        "Heat model: COP %.2f, demand %.2f kW, heat-pump capacity %.2f kW, "
        "heat-pump %.3f and pellet %.3f SEK/kWh delivered.",
        cop, demand, capacity, pump_cost, pellet_cost
    )
    if demand <= 0:
        return "none", 0.0

    capacities = {"heatpump": capacity, "pelletstove": pellet_capacity,
                  "combined": capacity + pellet_capacity}
    costs = {}
    if demand <= capacity:
        costs["heatpump"] = pump_cost
    if demand <= pellet_capacity:
        costs["pelletstove"] = pellet_cost
    cheaper_capacity = capacity if pump_cost <= pellet_cost else pellet_capacity
    if demand > cheaper_capacity and demand <= capacity + pellet_capacity:
        pump_heat = min(demand, capacity) if pump_cost <= pellet_cost else max(
            0.0, demand - pellet_capacity
        )
        costs["combined"] = (pump_heat * pump_cost + (demand - pump_heat) * pellet_cost) / demand
    if not costs:
        logging.warning(
            "Estimated demand %.2f kW exceeds combined available capacity %.2f kW.",
            demand, capacity + pellet_capacity
        )
        return "combined", capacities["combined"]

    if spot_limit > 0:
        preferred = "pelletstove" if price > spot_limit else "heatpump"
        desired = preferred if preferred in costs else "combined"
        return desired, capacities[desired]

    # Prefer a single source when costs tie; combined operation cannot be modulated.
    desired = min(costs, key=lambda source: (costs[source], source == "combined"))
    if current in costs and current != desired:
        saving = costs[current] - costs[desired]
        if saving <= hysteresis * abs(costs[current]):
            desired = current
    return desired, capacities[desired]

"""Fuel-corrected tyre degradation model.

The central problem: raw lap times get *faster* through a stint as the car burns
fuel, which masks and can even invert the tyre-degradation signal. Zandvoort
2024 shows this plainly - Sargeant's hard-tyre laps (~75.5s) are faster than his
medium-tyre laps (~77.5s) purely because the car is ~50kg lighter by then, not
because the hard compound is quicker.

So we model lap time as:

    lap_time = base_pace(compound) + deg_rate(compound) * tyre_age
               - fuel_effect * laps_completed

THE COLLINEARITY PROBLEM
------------------------
Fitting all of those jointly by plain OLS fails early in a race. Before anyone
pits, `tyre_age` and `laps_completed` are the *same number* - on lap 15 with no
stops, every car has a 15-lap-old tyre. The two regressors are collinear, so the
fit cannot tell "tyre wearing out" from "car getting lighter" and splits the
credit arbitrarily. Measured on Zandvoort 2024, a lap-20 OLS fit returned a fuel
effect of 0.094 s/lap (~2.5x physical reality) and inverted the compound
ordering, claiming MEDIUM degrades faster than SOFT.

That is precisely the moment the strategy engine matters most - deciding a first
pit stop around laps 15-25 - so the naive fit is worst exactly when it is most
needed. Two mitigations, applied together:

1. Pin `fuel_effect` to a physical prior until the field has enough *stint
   offset diversity* to identify it. Once several cars have pitted, tyre_age and
   lap_number decouple and the data can speak. Fuel burn is a known physical
   quantity, so pinning it early costs little and removes the ambiguity.

2. Ridge-regularise the *slope* coefficients toward physical priors, with a
   penalty that decays as clean laps accumulate. Base pace stays unpenalised.

   Validation over 39 races showed this matters far more than expected: with a
   weak penalty, out-of-sample MAE was 1.032s and compound ordering was right
   only 51% of the time - a coin flip. With the tuned penalty, MAE is 0.624s and
   ordering is right 96% of the time. The uncomfortable corollary is that
   per-race degradation rates carry little reliable signal; the priors do most
   of the work, and per-race adaptation happens mainly in base pace.

We also report `confidence`, so the UI can say "low confidence - few stops
observed" instead of presenting a falsely precise recommendation.

Everything here is fit from a *prefix* of the race (laps 1..N) so the strategy
engine can only ever use information available at the moment it decides.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd

# Fuel-corrected pace is only meaningful with enough clean laps to fit. Below
# this we fall back to circuit-agnostic priors rather than fitting noise.
MIN_LAPS_FOR_FIT = 25
MIN_LAPS_PER_COMPOUND = 6

# Fallback priors (seconds). Rough 2024-era dry-compound values.
PRIOR_DEG_RATE = {"SOFT": 0.075, "MEDIUM": 0.045, "HARD": 0.025}
PRIOR_DEG_DEFAULT = 0.05
PRIOR_FUEL_EFFECT = 0.035

# Wet-weather compounds. Crossover strategy is a drying-track problem, not a
# tyre-wear one, so the engine declines to advise rather than emit nonsense.
WET_COMPOUNDS = {"INTERMEDIATE", "WET"}

# Fuel effect is only identifiable once stint offsets vary across the field.
# `spread` = std of (lap_number - tyre_age) over clean laps seen; it is exactly
# 0 while nobody has pitted and grows as stops accumulate.
MIN_STINT_OFFSET_SPREAD = 9.0

# Ridge strength, scaled by n_laps so the prior dominates early and fades later.
#
# Tuned by out-of-sample validation over 39 dry races (see validate.py): fit
# through lap N, predict laps N+1..N+5, measure MAE. Sweeping this constant
# moved MAE from 1.032s (weak penalty) to 0.624s, and the share of races with
# physically-ordered compound degradation from 51% to 96%.
#
# The value is large because the penalty competes with a data term whose scale
# is sum(tyre_age^2) ~ 2e5. Note what the sweep revealed: MAE plateaus here, so
# heavier shrinkage neither helps nor hurts much - per-race degradation rates
# simply are not identifiable from partial-race data, and the physical priors
# predict about as well. What genuinely adapts per race is base pace, which is
# deliberately left unpenalised.
RIDGE_BASE = 300_000.0

# Physical plausibility bounds, used as guards on the final fit.
FUEL_BOUNDS = (0.015, 0.075)
DEG_BOUNDS = (0.0, 0.30)


@dataclasses.dataclass
class DegradationModel:
    """Fitted pace model for one race, from laps 1..fit_through_lap."""

    fit_through_lap: int
    fuel_effect: float               # sec/lap gained as fuel burns off
    base_pace: dict[str, float]      # compound -> pace on a fresh tyre, full fuel
    deg_rate: dict[str, float]       # compound -> sec lost per lap of tyre age
    n_laps_used: int
    fitted: bool                     # False => priors in use
    residual_std: float              # spread of clean-lap residuals (sec)
    fuel_pinned: bool                # True => fuel held at prior (not identifiable)
    stint_offset_spread: float       # identifiability diagnostic
    confidence: str                  # "low" | "medium" | "high"
    is_wet_race: bool                # wet compounds present in laps seen

    def predict(self, compound: str, tyre_age: float, laps_completed: float) -> float:
        """Expected clean-air lap time for a compound at a given tyre age."""
        c = (compound or "").upper()
        base = self.base_pace.get(c)
        if base is None:
            # Unknown/unseen compound: fall back to the fastest known base pace
            # plus a penalty, so it is never spuriously attractive.
            base = min(self.base_pace.values()) + 0.5 if self.base_pace else 90.0
        rate = self.deg_rate.get(c, PRIOR_DEG_DEFAULT)
        return base + rate * tyre_age - self.fuel_effect * laps_completed

    def stint_time(self, compound: str, start_age: float, start_lap: float,
                   n_laps: int) -> float:
        """Total time to run `n_laps` on `compound`, starting at a given age."""
        return float(sum(
            self.predict(compound, start_age + i, start_lap + i)
            for i in range(n_laps)
        ))


def _confidence(n_laps: int, spread: float, fitted: bool) -> str:
    if not fitted:
        return "low"
    if spread < MIN_STINT_OFFSET_SPREAD or n_laps < 150:
        return "low"
    if spread < 8.0 or n_laps < 400:
        return "medium"
    return "high"


def _priors(fit_through_lap: int, compounds: list[str], *,
            spread: float = 0.0, is_wet: bool = False) -> DegradationModel:
    return DegradationModel(
        fit_through_lap=fit_through_lap,
        fuel_effect=PRIOR_FUEL_EFFECT,
        base_pace={c: 90.0 for c in compounds},
        deg_rate={c: PRIOR_DEG_RATE.get(c, PRIOR_DEG_DEFAULT) for c in compounds},
        n_laps_used=0,
        fitted=False,
        residual_std=1.0,
        fuel_pinned=True,
        stint_offset_spread=spread,
        confidence="low",
        is_wet_race=is_wet,
    )


def fit(laps: pd.DataFrame, through_lap: int) -> DegradationModel:
    """Fit the pace model using only clean laps up to and including `through_lap`.

    Ridge-regularised least squares. Design matrix, per clean lap:
        one-hot(compound)                 -> base pace per compound
        one-hot(compound) * tyre_age      -> degradation rate per compound
        -laps_completed                   -> shared fuel effect (dropped when
                                             pinned to the prior)
    """
    seen = laps[(laps["LapNumber"] <= through_lap) & laps["IsCleanLap"]].copy()
    compounds_all = sorted(laps["Compound"].dropna().str.upper().unique().tolist())
    is_wet = bool(WET_COMPOUNDS & set(compounds_all))

    if len(seen) < MIN_LAPS_FOR_FIT:
        return _priors(through_lap, compounds_all, is_wet=is_wet)

    seen["Compound"] = seen["Compound"].str.upper()

    # Identifiability check: how much do stint offsets vary? Zero while nobody
    # has pitted, which is exactly when fuel/degradation are inseparable.
    offset = (seen["LapNumber"] - seen["TyreAge"]).to_numpy(dtype=float)
    spread = float(np.std(offset)) if len(offset) else 0.0
    pin_fuel = spread < MIN_STINT_OFFSET_SPREAD

    # Drop per-driver pace offsets by centring each driver's laps on their own
    # median. Without this, slow cars bias base pace and fast cars look like
    # negative degradation. We add the field median back after fitting.
    driver_med = seen.groupby("Driver")["LapTimeSec"].transform("median")
    field_med = float(seen["LapTimeSec"].median())
    seen["Adj"] = seen["LapTimeSec"] - driver_med + field_med

    # Only fit compounds with enough support; others inherit priors. Wet
    # compounds are excluded from the wear fit entirely - their lap times track
    # track-drying, not tyre age.
    counts = seen["Compound"].value_counts()
    fit_compounds = [c for c in compounds_all
                     if counts.get(c, 0) >= MIN_LAPS_PER_COMPOUND
                     and c not in WET_COMPOUNDS]
    if not fit_compounds:
        return _priors(through_lap, compounds_all, spread=spread, is_wet=is_wet)

    sub = seen[seen["Compound"].isin(fit_compounds)]
    # Defensive: a non-finite age or lap time would poison the least-squares
    # solve with an opaque LAPACK error rather than a clear failure.
    sub = sub[np.isfinite(sub["TyreAge"].to_numpy(dtype=float))
              & np.isfinite(sub["Adj"].to_numpy(dtype=float))]
    if len(sub) < MIN_LAPS_FOR_FIT:
        return _priors(through_lap, compounds_all, spread=spread, is_wet=is_wet)
    n = len(sub)
    k = len(fit_compounds)
    n_cols = 2 * k + (0 if pin_fuel else 1)

    X = np.zeros((n, n_cols))
    age = sub["TyreAge"].to_numpy(dtype=float)
    lapno = sub["LapNumber"].to_numpy(dtype=float)
    y = sub["Adj"].to_numpy(dtype=float)

    for j, c in enumerate(fit_compounds):
        mask = (sub["Compound"] == c).to_numpy()
        X[mask, j] = 1.0                 # base pace
        X[mask, k + j] = age[mask]       # degradation
    if pin_fuel:
        # Fuel effect held at the prior: move its contribution to the target so
        # the remaining coefficients are fit against fuel-corrected lap times.
        y = y + PRIOR_FUEL_EFFECT * lapno
    else:
        X[:, -1] = -lapno                # fuel burn, free parameter

    # Ridge toward priors: penalise deviation of each coefficient from its prior
    # mean, with strength decaying as clean laps accumulate.
    prior = np.zeros(n_cols)
    for j, c in enumerate(fit_compounds):
        prior[j] = field_med                                        # base pace
        prior[k + j] = PRIOR_DEG_RATE.get(c, PRIOR_DEG_DEFAULT)     # degradation
    if not pin_fuel:
        prior[-1] = PRIOR_FUEL_EFFECT

    # Base-pace terms are on a ~90s scale and well identified by the intercept;
    # penalising them would bias pace badly. Penalise slopes only.
    pen = np.zeros(n_cols)
    lam = RIDGE_BASE / max(1.0, n / 100.0)
    for j in range(k):
        pen[k + j] = lam
    if not pin_fuel:
        pen[-1] = lam

    A = np.vstack([X, np.diag(np.sqrt(pen))])
    b = np.concatenate([y, np.sqrt(pen) * prior])

    try:
        coef, *_ = np.linalg.lstsq(A, b, rcond=None)
    except np.linalg.LinAlgError:
        return _priors(through_lap, compounds_all, spread=spread, is_wet=is_wet)

    base = {c: float(coef[j]) for j, c in enumerate(fit_compounds)}
    deg = {c: float(coef[k + j]) for j, c in enumerate(fit_compounds)}
    fuel = PRIOR_FUEL_EFFECT if pin_fuel else float(coef[-1])

    # Physical plausibility guards. A negative degradation rate means the fit
    # picked up something other than tyre wear (track evolution, pace
    # management); fall back to the prior rather than propagate nonsense.
    for c in list(deg):
        if not np.isfinite(deg[c]) or not (DEG_BOUNDS[0] <= deg[c] <= DEG_BOUNDS[1]):
            deg[c] = PRIOR_DEG_RATE.get(c, PRIOR_DEG_DEFAULT)
    if not np.isfinite(fuel) or not (FUEL_BOUNDS[0] <= fuel <= FUEL_BOUNDS[1]):
        fuel = PRIOR_FUEL_EFFECT
        pin_fuel = True

    # Compounds without support inherit priors, offset from the best fitted base.
    for c in compounds_all:
        if c not in base:
            base[c] = min(base.values()) + 0.4 if base else 90.0
            deg[c] = PRIOR_DEG_RATE.get(c, PRIOR_DEG_DEFAULT)

    resid = y - X @ coef
    resid_std = float(np.std(resid)) if np.isfinite(resid).all() else 1.0

    return DegradationModel(
        fit_through_lap=through_lap,
        fuel_effect=fuel,
        base_pace=base,
        deg_rate=deg,
        n_laps_used=n,
        fitted=True,
        residual_std=resid_std,
        fuel_pinned=pin_fuel,
        stint_offset_spread=spread,
        confidence=_confidence(n, spread, True),
        is_wet_race=is_wet,
    )

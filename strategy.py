"""Tyre strategy engine.

Given a race, a lap, and a driver, answer two questions:

1. What is the best remaining strategy? Search candidate (pit lap, compound)
   plans and pick the one minimising projected time to the flag. This is the
   approximation of "highest finishing result" - it optimises the driver's own
   race time rather than simulating rivals' responses, which keeps it tractable
   and honest about what it does.

2. Is there an undercut or overcut available against nearby rivals? Compare
   pitting now against staying out, measured at the point the rival is projected
   to stop.

Everything is computed from a *prefix* of the race: the pace model is fit on
laps 1..N and the state comes from lap N, so nothing downstream can see the
future. That is what makes replayed recommendations meaningful rather than
hindsight.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd

import model

# A stint shorter than this is not a real strategic option - the tyre has no
# chance to pay back the stop.
MIN_STINT = 5

# Fallback pit loss (seconds) when it cannot be measured from the race itself.
DEFAULT_PIT_LOSS = 21.0

# Measured pit loss outside this band means the race gave no representative
# green-flag stop; we fall back to the default rather than report it.
PLAUSIBLE_PIT_LOSS = (15.0, 33.0)

# Dry compounds a driver may fit, best-to-worst durability.
DRY_COMPOUNDS = ("SOFT", "MEDIUM", "HARD")

# Rivals further away than this are not undercut targets.
UNDERCUT_WINDOW_S = 25.0

# An undercut resolves within a few laps; evaluating it over a longer horizon
# compares whole-race time against a small gap and is meaningless.
MAX_UNDERCUT_HORIZON = 12


@dataclasses.dataclass
class CarState:
    driver: str
    team: str
    position: int
    compound: str
    tyre_age: float
    stint: int
    cum_time: float          # cumulative race time at this lap
    gap_to_leader: float
    gap_ahead: float | None  # to car directly ahead
    compounds_used: tuple[str, ...]
    n_stops: int
    last_lap_time: float | None


@dataclasses.dataclass
class PlanOption:
    pit_lap: int | None      # None => run to the flag on current tyres
    compound: str | None
    projected_time: float    # remaining race time (sec)
    delta_vs_best: float


@dataclasses.dataclass
class RivalCall:
    rival: str
    position: int
    gap: float               # +ve => rival is ahead of us
    kind: str                # "undercut" | "overcut" | "exposed" | "safe"
    margin_s: float          # +ve is always in our favour
    horizon: int             # laps over which the move is evaluated
    works: bool              # does the move gain track position?


@dataclasses.dataclass
class Recommendation:
    race: str
    lap: int
    driver: str
    state: CarState
    action: str              # "PIT_NOW" | "STAY_OUT" | "NO_ADVICE"
    compound: str | None
    optimal_pit_lap: int | None
    cost_of_pitting_now: float   # extra sec vs the optimal plan (0 => pit now)
    confidence: str
    reason: str
    plans: list[PlanOption]
    rival_calls: list[RivalCall]
    pit_loss: float


def measure_pit_loss(laps: pd.DataFrame, through_lap: int) -> float:
    """Estimate time lost to a pit stop from the race's own in/out laps.

    Every stop gives a paired observation: the in-lap and out-lap are slower
    than a normal green lap by roughly the pit lane delta. Median over observed
    stops is robust to stops taken under a safety car.
    """
    seen = laps[laps["LapNumber"] <= through_lap]
    green = seen[seen["IsCleanLap"]]
    if green.empty:
        return DEFAULT_PIT_LOSS

    # Reference pace per driver so a slow car's stop is not overstated.
    ref = green.groupby("Driver")["LapTimeSec"].median()

    # A stop spans two laps: the in-lap (last of the old stint) and the out-lap
    # (first of the new one). Those must be summed as one observation - pooling
    # them and doubling the median badly underestimates the loss, because the
    # out-lap excess is typically 3x the in-lap excess.
    losses = []
    for drv, g in seen.sort_values("LapNumber").groupby("Driver"):
        base = ref.get(drv)
        if base is None or not np.isfinite(base):
            continue
        stints = g["Stint"].dropna().unique()
        for st in stints[:-1] if len(stints) else []:
            # Within a stint, the FIRST pit lap is that stint's out-lap and the
            # LAST is its in-lap. Taking iloc[0] for both summed two out-laps and
            # roughly doubled the estimate, clamping several circuits at 40s.
            in_rows = g[(g["Stint"] == st) & g["IsPitLap"]]["LapTimeSec"].dropna()
            out_rows = g[(g["Stint"] == st + 1) & g["IsPitLap"]]["LapTimeSec"].dropna()
            if in_rows.empty or out_rows.empty:
                continue
            excess = (max(0.0, float(in_rows.iloc[-1]) - base)
                      + max(0.0, float(out_rows.iloc[0]) - base))
            if excess > 0:
                losses.append(excess)
    if not losses:
        return DEFAULT_PIT_LOSS
    est = float(np.median(losses))

    # Some races offer no representative green-flag stop at all. Monaco 2024 is
    # the clean example: a lap-1 red flag let the whole field change tyres and
    # most drivers never stopped again, so every observation is a red-flag stop.
    # Clamping such an estimate would report a confidently wrong ~40s, so fall
    # back to the circuit-agnostic default instead.
    if not (PLAUSIBLE_PIT_LOSS[0] <= est <= PLAUSIBLE_PIT_LOSS[1]):
        return DEFAULT_PIT_LOSS
    return est


def race_state(laps: pd.DataFrame, lap: int) -> dict[str, CarState]:
    """Reconstruct every car's state as at the end of `lap`."""
    upto = laps[laps["LapNumber"] <= lap]
    if upto.empty:
        return {}

    # Cumulative race time. Missing lap times (e.g. lap 1 quirks) are filled
    # with the driver's median so gaps stay meaningful rather than vanishing.
    # A driver with no timed laps at all has an unknowable cumulative time; say
    # so explicitly rather than taking a median of nothing, which is where the
    # affected Miami 2025 cars ended up.
    cum: dict[str, float] = {}
    for drv, g in upto.groupby("Driver"):
        t = g["LapTimeSec"]
        valid = t.dropna()
        cum[drv] = float(t.fillna(valid.median()).sum()) if not valid.empty \
            else float("nan")

    at_lap = upto[upto["LapNumber"] == lap].set_index("Driver")
    states: dict[str, CarState] = {}

    for drv, row in at_lap.iterrows():
        hist = upto[upto["Driver"] == drv]
        used = tuple(dict.fromkeys(
            c.upper() for c in hist["Compound"].dropna().tolist()
        ))
        states[drv] = CarState(
            driver=str(drv),
            team=str(row.get("Team", "")),
            position=int(row["Position"]) if pd.notna(row.get("Position")) else 99,
            compound=str(row["Compound"]).upper() if pd.notna(row.get("Compound")) else "",
            tyre_age=float(row["TyreAge"]) if pd.notna(row.get("TyreAge")) else 0.0,
            stint=int(row["Stint"]) if pd.notna(row.get("Stint")) else 1,
            cum_time=cum.get(str(drv), float("nan")),
            gap_to_leader=0.0,
            gap_ahead=None,
            compounds_used=used,
            n_stops=max(0, int(hist["IsPitLap"].sum())),
            last_lap_time=float(row["LapTimeSec"]) if pd.notna(row.get("LapTimeSec")) else None,
        )

    # Gaps from cumulative time, ordered by classification at this lap.
    order = sorted(states.values(), key=lambda s: (s.position, s.cum_time))
    if order:
        leader = order[0].cum_time
        prev = None
        for s in order:
            if np.isfinite(s.cum_time) and np.isfinite(leader):
                s.gap_to_leader = s.cum_time - leader
            if prev is not None and np.isfinite(s.cum_time) and np.isfinite(prev.cum_time):
                s.gap_ahead = s.cum_time - prev.cum_time
            prev = s
    return states


def _legal_compounds(state: CarState, remaining: int) -> list[str]:
    """Compounds worth considering, respecting the two-compound rule.

    A dry race requires at least two different slick compounds. If the driver
    has only used one so far and this is their last planned stop, they must
    switch.
    """
    opts = [c for c in DRY_COMPOUNDS]
    if len(set(state.compounds_used) & set(DRY_COMPOUNDS)) <= 1:
        opts = [c for c in opts if c != state.compound] or list(DRY_COMPOUNDS)
    return opts


def evaluate_plans(m: model.DegradationModel, state: CarState, lap: int,
                   total_laps: int, pit_loss: float) -> list[PlanOption]:
    """Project remaining race time for each candidate plan."""
    remaining = total_laps - lap
    if remaining <= 0:
        return []

    plans: list[PlanOption] = []

    # Option A: no further stop.
    must_stop = len(set(state.compounds_used) & set(DRY_COMPOUNDS)) <= 1
    if not must_stop:
        plans.append(PlanOption(
            pit_lap=None,
            compound=None,
            projected_time=m.stint_time(state.compound, state.tyre_age + 1,
                                        lap + 1, remaining),
            delta_vs_best=0.0,
        ))

    # Option B: one more stop at lap p, onto compound c.
    for p in range(lap, total_laps - MIN_STINT + 1):
        laps_before = p - lap                 # laps still on current tyre
        laps_after = total_laps - p
        if laps_after < MIN_STINT:
            continue
        for c in _legal_compounds(state, remaining):
            t = 0.0
            if laps_before > 0:
                t += m.stint_time(state.compound, state.tyre_age + 1,
                                  lap + 1, laps_before)
            t += pit_loss
            t += m.stint_time(c, 1, p + 1, laps_after)
            plans.append(PlanOption(pit_lap=p, compound=c,
                                    projected_time=t, delta_vs_best=0.0))

    if not plans:
        return []
    best = min(pl.projected_time for pl in plans)
    for pl in plans:
        pl.delta_vs_best = pl.projected_time - best
    plans.sort(key=lambda pl: pl.projected_time)
    return plans


def _project_rival_pit(m: model.DegradationModel, rival: CarState, lap: int,
                       total_laps: int, pit_loss: float) -> int:
    """When is the rival most likely to stop, on their own optimal plan?"""
    plans = evaluate_plans(m, rival, lap, total_laps, pit_loss)
    for pl in plans:
        if pl.pit_lap is not None:
            return pl.pit_lap
    return total_laps  # projected to run to the flag


def rival_calls(m: model.DegradationModel, states: dict[str, CarState],
                me: CarState, lap: int, total_laps: int,
                pit_loss: float) -> list[RivalCall]:
    """Undercut / overcut / exposure assessment against nearby cars.

    The algebra, for a rival ahead by `gap` seconds. Pitting now costs the pit
    loss but buys fresh-tyre pace; the rival keeps ageing their current set. Over
    a horizon of H laps we emerge ahead when

        rival_stay(H)  >  gap + pit_loss + our_fresh(H)

    so the margin is the difference of those two sides: positive means the
    undercut gains track position.

    H must be bounded. An earlier version set H from the rival's projected stop,
    which for a car projected to run to the flag meant comparing 50+ laps of
    absolute race time against a 2-second gap - producing large negative
    "gains" that meant nothing. An undercut is decided within a handful of laps,
    so H is capped.
    """
    calls: list[RivalCall] = []
    fresh = _legal_compounds(me, total_laps - lap)[0]

    for other in states.values():
        if other.driver == me.driver or not other.compound:
            continue
        gap = me.gap_to_leader - other.gap_to_leader   # +ve => rival ahead
        if not np.isfinite(gap) or abs(gap) > UNDERCUT_WINDOW_S:
            continue

        rival_pit = _project_rival_pit(m, other, lap, total_laps, pit_loss)
        H = int(np.clip(rival_pit - lap, 1, MAX_UNDERCUT_HORIZON))
        if lap + H > total_laps:
            H = max(1, total_laps - lap)

        our_fresh = pit_loss + m.stint_time(fresh, 1, lap + 1, H)
        our_stay = m.stint_time(me.compound, me.tyre_age + 1, lap + 1, H)
        rival_stay = m.stint_time(other.compound, other.tyre_age + 1, lap + 1, H)

        if gap > 0:
            # Rival ahead. Do we clear them by stopping now?
            undercut = rival_stay - (gap + our_fresh)
            # Overcut: stay out while they stop, and be ahead when we do stop.
            overcut = (rival_stay + pit_loss) - (gap + our_stay)
            if undercut >= overcut:
                kind, margin = "undercut", undercut
            else:
                kind, margin = "overcut", overcut
        else:
            # Rival behind by |gap|. If they undercut us, do they come out ahead?
            # They emerge ahead when  behind + pit_loss + their_fresh(H) < our_stay,
            # so our cushion is the amount by which that inequality fails. The
            # gap must be added to their cost, not subtracted: a car further back
            # is less of a threat, and an earlier sign slip had it the other way.
            behind = abs(gap)
            margin = (behind + our_fresh) - our_stay
            kind = "safe" if margin > 0 else "exposed"

        calls.append(RivalCall(
            rival=other.driver,
            position=other.position,
            gap=round(float(gap), 2),
            kind=kind,
            margin_s=round(float(margin), 2),
            horizon=H,
            works=bool(margin > 0),
        ))

    # Nearest cars first - those are the ones a strategist actually acts on.
    calls.sort(key=lambda c: abs(c.gap))
    return calls


def recommend(laps: pd.DataFrame, race_slug: str, lap: int,
              driver: str) -> Recommendation:
    total_laps = int(laps["LapNumber"].max())
    lap = max(1, min(lap, total_laps))

    m = model.fit(laps, lap)
    states = race_state(laps, lap)
    pit_loss = measure_pit_loss(laps, lap)

    me = states.get(driver)
    if me is None:
        return Recommendation(
            race=race_slug, lap=lap, driver=driver,
            state=CarState(driver, "", 99, "", 0, 1, float("nan"), 0, None, (), 0, None),
            action="NO_ADVICE", compound=None, optimal_pit_lap=None,
            cost_of_pitting_now=float("nan"), confidence="low",
            reason="driver not running at this lap", plans=[], rival_calls=[],
            pit_loss=pit_loss,
        )

    # Some sessions are missing FastF1's timing-app data, which carries stint,
    # compound and tyre life together - Miami 2025 lacks it for 15 drivers across
    # 354 laps. Without a known compound there is nothing to project a stint on,
    # and falling back to a placeholder would produce confident-looking numbers
    # from a guessed tyre.
    if not me.compound:
        return Recommendation(
            race=race_slug, lap=lap, driver=driver, state=me,
            action="NO_ADVICE", compound=None, optimal_pit_lap=None,
            cost_of_pitting_now=float("nan"), confidence="low",
            reason="tyre compound not recorded for this lap upstream",
            plans=[], rival_calls=[], pit_loss=pit_loss,
        )

    # Wet races are a track-drying problem, not a tyre-wear one. The model has
    # no crossover logic, so it declines rather than guessing.
    if m.is_wet_race or me.compound in model.WET_COMPOUNDS:
        return Recommendation(
            race=race_slug, lap=lap, driver=driver, state=me,
            action="NO_ADVICE", compound=None, optimal_pit_lap=None,
            cost_of_pitting_now=float("nan"), confidence="low",
            reason="wet or mixed conditions - crossover strategy not modelled",
            plans=[], rival_calls=[], pit_loss=pit_loss,
        )

    plans = evaluate_plans(m, me, lap, total_laps, pit_loss)
    if not plans:
        return Recommendation(
            race=race_slug, lap=lap, driver=driver, state=me,
            action="STAY_OUT", compound=None, optimal_pit_lap=None,
            cost_of_pitting_now=float("nan"), confidence=m.confidence,
            reason="too few laps remaining for a stop to pay back",
            plans=[], rival_calls=[], pit_loss=pit_loss,
        )

    best = plans[0]

    # How much does acting immediately cost relative to the optimal plan? This
    # is the decision-relevant number: it is ~0 when now is the right moment and
    # grows as the window is still ahead. (An earlier version compared against a
    # "stay out to the flag" plan, which is structurally unavailable whenever the
    # two-compound rule forces a stop, so it was always 0.)
    now_plans = [pl for pl in plans if pl.pit_lap is not None and pl.pit_lap <= lap]
    cost_now = (min(pl.projected_time for pl in now_plans) - best.projected_time
                if now_plans else float("nan"))

    if best.pit_lap is None:
        action, compound = "STAY_OUT", None
        reason = "both compounds used; running to the flag is fastest"
    elif best.pit_lap <= lap:
        action, compound = "PIT_NOW", best.compound
        reason = f"stop now for {best.compound} - this is the optimal lap"
    else:
        action, compound = "STAY_OUT", best.compound
        extra = "" if not np.isfinite(cost_now) else f"; stopping now costs {cost_now:+.1f}s"
        reason = (f"optimal stop is lap {best.pit_lap} for {best.compound}"
                  f" ({best.pit_lap - lap} laps away){extra}")

    return Recommendation(
        race=race_slug, lap=lap, driver=driver, state=me,
        action=action, compound=compound, optimal_pit_lap=best.pit_lap,
        cost_of_pitting_now=round(float(cost_now), 2),
        confidence=m.confidence, reason=reason,
        plans=plans[:8],
        rival_calls=rival_calls(m, states, me, lap, total_laps, pit_loss),
        pit_loss=round(pit_loss, 2),
    )

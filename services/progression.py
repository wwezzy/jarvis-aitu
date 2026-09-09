from __future__ import annotations


def double_progression_recommendation(
    sets: list[dict],
    *,
    rep_min: int = 8,
    rep_max: int = 12,
    main_lift: bool = True,
) -> dict:
    """Return a deterministic load/reps recommendation from logged working sets."""
    working = [item for item in sets if item.get("reps") is not None]
    if not working:
        return {"action": "insufficient_data", "reason": "No reps logged"}

    reps = [int(item["reps"]) for item in working]
    rir_values = [float(item["rir"]) for item in working if item.get("rir") is not None]
    target_rir = 2.0 if main_lift else 1.0

    if all(reps_value >= rep_max for reps_value in reps) and (
        not rir_values or min(rir_values) >= target_rir
    ):
        return {
            "action": "increase_load",
            "reason": f"All working sets reached {rep_max}+ reps with target RIR preserved",
            "suggested_increment_percent": 2.5 if main_lift else 5.0,
        }

    if any(reps_value < rep_min for reps_value in reps):
        return {
            "action": "hold_or_reduce",
            "reason": f"At least one set is below the {rep_min}-{rep_max} rep range",
        }

    return {
        "action": "hold_load_add_reps",
        "reason": f"Keep load and progress reps toward {rep_max} while preserving RIR",
    }

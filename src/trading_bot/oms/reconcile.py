"""Position reconciliation — broker truth vs the bot's DB record.

The bot's view of open positions must match the broker's. Drift means a missed
fill, a manual trade, a partial close, or a bug — any of which makes it unsafe
to keep trading. This module is pure (diff two snapshots); the live broker
fetch is wired in at the execution layer (Week 4), and the daily `tbot
reconcile` command surfaces any mismatch loudly.

Units are signed: positive = long, negative = short, absent/zero = flat.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PositionDiff:
    instrument: str
    broker_units: float
    db_units: float


@dataclass
class ReconResult:
    matched: list[str] = field(default_factory=list)
    only_broker: list[PositionDiff] = field(default_factory=list)  # broker has, DB doesn't
    only_db: list[PositionDiff] = field(default_factory=list)  # DB has, broker doesn't
    mismatched: list[PositionDiff] = field(default_factory=list)  # both, different size

    @property
    def is_clean(self) -> bool:
        return not (self.only_broker or self.only_db or self.mismatched)


def reconcile_positions(
    broker: dict[str, float],
    db: dict[str, float],
    tolerance: float = 1e-6,
) -> ReconResult:
    """Compare broker and DB position maps (instrument → signed units).

    A flat position may be represented as 0.0 or simply absent; both are
    treated as flat.
    """
    result = ReconResult()
    instruments = {i for i, u in {**broker, **db}.items() if abs(u) > tolerance}

    for inst in sorted(instruments):
        b = broker.get(inst, 0.0)
        d = db.get(inst, 0.0)
        b_open = abs(b) > tolerance
        d_open = abs(d) > tolerance

        if b_open and d_open:
            if abs(b - d) <= tolerance:
                result.matched.append(inst)
            else:
                result.mismatched.append(PositionDiff(inst, b, d))
        elif b_open:
            result.only_broker.append(PositionDiff(inst, b, d))
        elif d_open:
            result.only_db.append(PositionDiff(inst, b, d))

    return result

"""Factor applicability rules and per-record classification.

Every consumption record is judged against the *whole* applicable rule set and
lands in exactly one of two places:

* a :class:`~carbon_factor_ledger.models.Contribution` with a converted
  quantity, emission and attested evidence hash; or
* a :class:`~carbon_factor_ledger.models.QuarantineEntry` carrying **all**
  reasons that fired.  Quarantined records are excluded from the footprint but
  never silently treated as zero — they are reported with quantities and
  reasons.

Rule order does not short-circuit: e.g. a record can be both expired and
geographically out of scope, and both reasons are reported.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .ledger import Ledger
from .models import (
    ConsumptionRecord,
    Contribution,
    FactorVersion,
    QuarantineEntry,
)
from .units import UnitError, convert_quantity, dimension_of, emission_to_kgco2e

#: Geography hierarchy.  An exact scope match always wins; otherwise only a
#: factor declared GLOBAL applies outside its own scope.
GLOBAL_SCOPE = "GLOBAL"
#: Boundary value that covers every lifecycle boundary.
ALL_BOUNDARIES = "ALL"


def _geography_applies(factor: FactorVersion, record: ConsumptionRecord) -> bool:
    return factor.geography == record.geography or factor.geography == GLOBAL_SCOPE


def _boundary_applies(factor: FactorVersion, record: ConsumptionRecord) -> bool:
    if factor.boundary == ALL_BOUNDARIES:
        return True
    if factor.boundary != record.boundary:
        return False
    # Same boundary name; activity and material must describe the same flow.
    return factor.activity == record.activity and factor.material == record.material


def evaluate_record(
    ledger: Ledger, record: ConsumptionRecord
) -> Tuple[Optional[Contribution], Optional[QuarantineEntry]]:
    """Return ``(contribution, quarantine)`` — exactly one is not ``None``."""
    reasons: List[str] = []
    details: List[str] = []

    factor: Optional[FactorVersion]
    if not record.factor_id:
        factor = None
        reasons.append("FACTOR_NOT_FOUND")
        details.append("record does not reference any factor_id")
    else:
        factor = ledger.resolve_factor(
            record.factor_id, record.factor_version, record.occurred_on
        )
        if factor is None:
            known = ledger.versions_of(record.factor_id)
            if not known:
                reasons.append("FACTOR_NOT_FOUND")
                details.append(
                    f"factor {record.factor_id!r} is not registered"
                    + (f" (pinned v{record.factor_version})"
                       if record.factor_version else "")
                )
            else:
                # The factor exists but no version's validity window covers
                # this record date: an expiry/timing failure, not a missing
                # factor. Pinned references name the exact version; unpinned
                # ones are judged against the closest registered versions.
                if record.factor_version is not None:
                    factor = ledger.get_factor(
                        record.factor_id, record.factor_version
                    )
                    if factor is None:
                        reasons.append("FACTOR_NOT_FOUND")
                        details.append(
                            f"pinned {record.factor_id!r} "
                            f"v{record.factor_version} is not registered"
                        )
                else:
                    latest = max(known, key=lambda f: f.version)
                    # Keep the factor object so withdrawal, evidence-chain,
                    # geography and boundary rules are still evaluated; the
                    # timing failure below is recorded as the reason.
                    factor = latest
                    if record.occurred_on < latest.valid_from:
                        reasons.append("FACTOR_NOT_YET_VALID")
                        details.append(
                            f"record date {record.occurred_on.isoformat()} "
                            f"precedes earliest applicable valid_from "
                            f"{latest.valid_from.isoformat()}"
                        )
                    else:
                        reasons.append("FACTOR_EXPIRED")
                        details.append(
                            f"no registered version of {record.factor_id!r} is "
                            f"valid on {record.occurred_on.isoformat()} "
                            f"(latest v{latest.version} valid_to "
                            f"{latest.valid_to.isoformat() if latest.valid_to else 'open'})"
                        )

    converted_quantity = None
    if factor is not None:
        # 1. Evidence chain: evidence must exist and its current hash must
        #    still equal the hash attested at registration.
        evidence = ledger.evidence.get(factor.evidence_id)
        if evidence is None:
            reasons.append("EVIDENCE_MISSING")
            details.append(f"factor references unknown evidence {factor.evidence_id!r}")
        else:
            state = ledger._evidence_state[factor.evidence_id]
            if state["conflicts"]:
                reasons.append("EVIDENCE_HASH_MISMATCH")
                details.append(
                    f"evidence {factor.evidence_id!r} hash changed after "
                    f"registration: attested {state['sha256'][:12]}…, "
                    f"conflicting hashes observed: "
                    f"{', '.join(sorted(state['conflicts']))}"
                )

        # 2. Withdrawal.
        if factor.status == "withdrawn":
            reasons.append("FACTOR_WITHDRAWN")
            details.append(
                f"{factor.factor_id} v{factor.version} withdrawn"
                + (f" at {factor.withdrawn_at.isoformat()}" if factor.withdrawn_at else "")
            )

        # 3. Validity window.  Expiry/not-yet-valid may already have been
        #    diagnosed at resolution time; do not duplicate the reason.
        if record.occurred_on < factor.valid_from:
            if "FACTOR_NOT_YET_VALID" not in reasons:
                reasons.append("FACTOR_NOT_YET_VALID")
                details.append(
                    f"record date {record.occurred_on.isoformat()} precedes "
                    f"valid_from {factor.valid_from.isoformat()}"
                )
        elif factor.valid_to is not None and record.occurred_on > factor.valid_to:
            if "FACTOR_EXPIRED" not in reasons:
                reasons.append("FACTOR_EXPIRED")
                details.append(
                    f"record date {record.occurred_on.isoformat()} is after "
                    f"valid_to {factor.valid_to.isoformat()}"
                )

        # 4. Geographical scope.
        if not _geography_applies(factor, record):
            reasons.append("GEOGRAPHY_OUT_OF_SCOPE")
            details.append(
                f"factor scope {factor.geography!r} does not cover record "
                f"geography {record.geography!r}"
            )

        # 5. Lifecycle boundary / flow identity.
        if not _boundary_applies(factor, record):
            reasons.append("BOUNDARY_MISMATCH")
            details.append(
                f"factor boundary/flow {factor.boundary!r}/"
                f"{factor.activity!r}/{factor.material!r} does not match record "
                f"{record.boundary!r}/{record.activity!r}/{record.material!r}"
            )

        # 6. Unit dimensional compatibility + conversion to reference unit.
        try:
            converted_quantity = convert_quantity(
                record.quantity, record.unit, factor.reference_unit
            )
        except UnitError as exc:
            reasons.append("UNIT_INCOMPATIBLE")
            details.append(str(exc))

    if reasons:
        return None, QuarantineEntry(
            record_id=record.record_id,
            product=record.product,
            period=record.period,
            factor_id=record.factor_id,
            factor_version=record.factor_version,
            reasons=tuple(reasons),
            detail="; ".join(details),
            quantity=record.quantity,
            unit=record.unit,
        )

    assert factor is not None and converted_quantity is not None
    emission = emission_to_kgco2e(
        converted_quantity * factor.factor_value, factor.factor_unit_numerator
    )
    evidence = ledger.evidence[factor.evidence_id]
    return Contribution(
        record_id=record.record_id,
        factor_id=factor.factor_id,
        factor_version=factor.version,
        material=record.material,
        activity=record.activity,
        quantity=record.quantity,
        unit=record.unit,
        converted_quantity=round(converted_quantity, 9),
        reference_unit=factor.reference_unit,
        emission=round(emission, 9),
        evidence_id=evidence.evidence_id,
        evidence_sha256=evidence.sha256,
    ), None

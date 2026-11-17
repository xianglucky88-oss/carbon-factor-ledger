"""Typed errors for the carbon factor ledger.

Every error raised by the domain layer is a subclass of
:class:`CarbonLedgerError` so the CLI can map it to a stable exit code and
machine-readable error payload.
"""


class CarbonLedgerError(Exception):
    """Base class for all expected, auditable ledger failures."""

    code = "ledger_error"


class ValidationError(CarbonLedgerError):
    """The input payload is structurally invalid."""

    code = "validation_error"


class RuleError(CarbonLedgerError):
    """A rule file (factors / units / evidence) is malformed."""

    code = "rule_error"


class FactorError(CarbonLedgerError):
    """A factor reference is unknown or unusable."""

    code = "factor_error"


class UnitError(CarbonLedgerError):
    """A unit has no conversion rule to the base unit."""

    code = "unit_error"


class EvidenceError(CarbonLedgerError):
    """An evidence path is empty, missing, tampered with, or un referenced."""

    code = "evidence_error"

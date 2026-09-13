from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Classification(StrEnum):
    HEALTHY = "HEALTHY"
    DONE_UNRECORDED = "DONE_UNRECORDED"
    BREACHED = "BREACHED"
    DESYNC = "DESYNC"


@dataclass(frozen=True)
class Artifact:
    kind: str
    resource_id: str
    url: str
    source: str


@dataclass
class LedgerEntry:
    ticket: dict[str, Any]
    artifacts: list[Artifact] = field(default_factory=list)
    waiver: bool = False
    remediation_complete: bool = False
    classification: Classification = Classification.HEALTHY

    @property
    def has_required_proof(self) -> bool:
        if self.waiver:
            return True
        required = self.ticket.get("required_proof")
        return any(artifact.kind == required for artifact in self.artifacts)


@dataclass(frozen=True)
class WriteReceipt:
    app: str
    action: str
    target_id: str
    idempotency_key: str
    status_code: int
    applied: bool
    duplicate: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "app": self.app,
            "action": self.action,
            "target_id": self.target_id,
            "idempotency_key": self.idempotency_key,
            "status_code": self.status_code,
            "applied": self.applied,
            "duplicate": self.duplicate,
        }


class PromiseLedgerError(RuntimeError):
    pass


class RefusalError(PromiseLedgerError):
    pass


class ForbiddenOperation(PromiseLedgerError):
    pass


class ApiError(PromiseLedgerError):
    pass

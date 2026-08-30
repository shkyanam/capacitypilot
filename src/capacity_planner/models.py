from datetime import UTC, date, datetime
from typing import Any, Literal, TypedDict
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class InvestigationRequest(BaseModel):
    company_id: int = Field(gt=0)


class DecisionRequest(BaseModel):
    decision: Literal["APPROVE_REVIEW", "MONITOR", "REJECT_INVESTIGATE"]
    note: str = Field(default="", max_length=2000)
    decided_by: str = Field(min_length=2, max_length=200)


class ForecastOverrideItem(BaseModel):
    case_id: UUID
    likelihood_pct: float = Field(ge=0, le=100)
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    timing_days: int | None = Field(default=None, ge=0, le=3650)
    capacity_growth_tib: float | None = Field(default=None, ge=0)
    action: Literal["PLANNER_REVIEW", "MONITOR"]


class BulkForecastOverrideRequest(BaseModel):
    overrides: list[ForecastOverrideItem] = Field(min_length=1, max_length=1000)
    note: str = Field(default="", max_length=2000)
    modified_by: str = Field(min_length=2, max_length=200)


class LocalReservationRequest(BaseModel):
    case_id: UUID
    requested_tib: float = Field(gt=0, le=10_000_000)
    target_date: date
    service: Literal["Storage Capacity"]
    vault_type: Literal[
        "Standard",
        "High Performance",
        "Ultra Performance",
        "System Standard",
        "System Critical",
        "Replication",
        "General Purpose",
    ]
    region: str = Field(min_length=2, max_length=100)
    qfab: str = Field(min_length=2, max_length=100)
    planner_identity: str = Field(min_length=2, max_length=200)
    note: str = Field(default="", max_length=2000)
    confirm_local_only: Literal[True]

    @model_validator(mode="after")
    def validate_service_vault_mapping(self):
        valid_vaults = {
            "Storage Capacity": {
                "Standard",
                "High Performance",
                "Ultra Performance",
                "System Standard",
                "System Critical",
                "Replication",
                "General Purpose",
            },
        }
        if self.vault_type not in valid_vaults[self.service]:
            raise ValueError(f"{self.vault_type} is not valid for {self.service}")
        if self.target_date < datetime.now(UTC).date():
            raise ValueError("target_date cannot be in the past")
        return self


class JiraRequestCreate(BaseModel):
    case_id: UUID | None = None
    request_type: Literal["CAP_RESERVATION", "HUB_INFRASTRUCTURE"]
    region: str | None = Field(default=None, min_length=2, max_length=100)
    service: Literal["Storage Capacity"] | None = None
    vault_type: Literal[
        "Standard",
        "High Performance",
        "Ultra Performance",
        "System Standard",
        "System Critical",
        "Replication",
        "General Purpose",
    ] | None = None
    qfab: str | None = Field(default=None, min_length=2, max_length=100)
    requested_tib: float | None = Field(default=None, gt=0, le=10_000_000)
    target_date: date | None = None
    planner_identity: str = Field(min_length=2, max_length=200)
    note: str = Field(default="", max_length=2000)
    confirm_create: Literal[True]

    @model_validator(mode="after")
    def validate_request_scope(self):
        if self.request_type == "CAP_RESERVATION" and self.case_id is None:
            raise ValueError("case_id is required for a CAP reservation ticket")
        if self.request_type == "HUB_INFRASTRUCTURE" and self.case_id is None:
            required = {
                "region": self.region,
                "service": self.service,
                "vault_type": self.vault_type,
                "qfab": self.qfab,
                "requested_tib": self.requested_tib,
                "target_date": self.target_date,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ValueError(
                    "Regional HUB requests require " + ", ".join(missing)
                )
        if self.target_date and self.target_date < datetime.now(UTC).date():
            raise ValueError("target_date cannot be in the past")
        return self


class SlackDigestRequest(BaseModel):
    force: bool = False
    confirm_send: Literal[True]


class PortfolioChatContext(BaseModel):
    previous_question: str | None = Field(default=None, max_length=1000)
    previous_intent: Literal[
        "RESERVATION_AUDIT_COUNT",
        "RESERVATION_AUDIT_LIST",
        "RESERVATION_AUDIT_UNSUPPORTED",
    ] | None = None
    previous_time_window_hours: int | None = Field(default=None, ge=1, le=8760)


class PortfolioChatRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    context: PortfolioChatContext | None = None


class PortfolioQueryPlan(BaseModel):
    operation: Literal["LIST", "COUNT", "SUMMARY", "UNSUPPORTED"]
    customer_search: str | None = Field(default=None, max_length=100)
    regions: list[str] = Field(default_factory=list, max_length=20)
    min_utilization_pct: float | None = Field(default=None, ge=0, le=100)
    max_utilization_pct: float | None = Field(default=None, ge=0, le=100)
    min_likelihood_pct: float | None = Field(default=None, ge=0, le=100)
    max_likelihood_pct: float | None = Field(default=None, ge=0, le=100)
    min_open_demand_tib: float | None = Field(default=None, ge=0)
    min_annual_growth_tib: float | None = Field(default=None)
    confidence: list[Literal["LOW", "MEDIUM", "HIGH"]] = Field(
        default_factory=list, max_length=3
    )
    demand_stages: list[str] = Field(default_factory=list, max_length=10)
    planner_states: list[
        Literal[
            "NEEDS_REVIEW",
            "IN_PROGRESS",
            "REVIEWED",
            "MONITORING",
            "NOT_INVESTIGATED",
        ]
    ] = Field(default_factory=list, max_length=5)
    sort_by: Literal[
        "utilization_pct",
        "open_demand_tib",
        "annual_growth_tib",
        "likelihood_pct",
        "suggested_growth_tib",
        "company_name",
    ] = "likelihood_pct"
    sort_direction: Literal["ASC", "DESC"] = "DESC"
    limit: int = Field(default=10, ge=1, le=50)
    explanation: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_ranges(self):
        if (
            self.min_utilization_pct is not None
            and self.max_utilization_pct is not None
            and self.min_utilization_pct > self.max_utilization_pct
        ):
            raise ValueError("minimum utilization cannot exceed maximum utilization")
        if (
            self.min_likelihood_pct is not None
            and self.max_likelihood_pct is not None
            and self.min_likelihood_pct > self.max_likelihood_pct
        ):
            raise ValueError("minimum likelihood cannot exceed maximum likelihood")
        return self


class CaseResponse(BaseModel):
    case_id: UUID
    company_id: int
    status: str
    attempt_count: int
    last_error: str | None = None
    recommendation: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class Recommendation(BaseModel):
    likelihood_pct: float = Field(ge=0, le=100)
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    timing_days: int | None = Field(default=None, ge=0, le=3650)
    capacity_growth_tib: float | None = Field(default=None, ge=0)
    action: Literal["PLANNER_REVIEW", "MONITOR"]
    reasons: list[str] = Field(min_length=1, max_length=10)


class AgentState(TypedDict):
    case_id: str
    company_id: int
    evidence: dict[str, Any]
    recommendation: dict[str, Any]
    errors: list[str]

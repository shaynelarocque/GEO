from datetime import datetime
from typing import Literal, Optional
import uuid

from pydantic import BaseModel, Field


# ---------- Input ----------


class ProgramAuditInput(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    program_name: str
    recipient_hint: Optional[str] = None
    submitted_at: datetime = Field(default_factory=datetime.utcnow)


# ---------- Provenance & evidence ----------

ProvenanceTier = Literal[
    "primary_gov",
    "hansard_committee",
    "structured_dataset",
    "established_press",
    "partisan_press",
    "unverified",
]


class Evidence(BaseModel):
    claim: str
    source: str
    tier: ProvenanceTier
    excerpt: Optional[str] = None


# ---------- Lens primitives ----------

LensKey = Literal["stated_objectives", "budget", "adoption", "vendor"]
PhaseKey = Literal[
    "goal_anchor",
    "stated_objectives",
    "budget",
    "adoption",
    "vendor",
    "synthesis",
    "follow_up",
    "other",
]
Verdict = Literal["green", "yellow", "red", "insufficient_evidence"]
EvidenceTier = Literal["strong", "moderate", "limited", "n/a"]


class KeyNumber(BaseModel):
    label: str
    value: str
    sublabel: Optional[str] = None


class BudgetTranche(BaseModel):
    label: str
    date: Optional[str] = None  # ISO date string for portability
    amount_cad: float
    note: Optional[str] = None
    source: Optional[str] = None  # citation marker


class Lens(BaseModel):
    key: LensKey
    verdict: Verdict = "insufficient_evidence"
    evidence_tier: EvidenceTier = "n/a"
    summary: str = ""
    key_numbers: list[KeyNumber] = []
    rationale_md: str = ""
    counter_argument_md: str = ""
    evidence: list[Evidence] = []
    budget_tranches: list[BudgetTranche] = []  # only used when key == "budget"
    revision_count: int = 0


# ---------- Goal anchor ----------


class GoalAnchor(BaseModel):
    stated_objectives: str = ""
    original_budget: Optional[str] = None
    success_metrics: list[str] = []
    timeline: Optional[str] = None
    sources: list[Evidence] = []


# ---------- Synthesis ----------


class Synthesis(BaseModel):
    overall_verdict: Verdict = "insufficient_evidence"
    overall_tier: EvidenceTier = "n/a"
    summary: str = ""
    rationale_md: str = ""


# ---------- Drafted accountability instruments ----------

InstrumentType = Literal["atip", "order_paper_question", "committee_followup"]


class AccountabilityDraft(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    instrument: InstrumentType
    addressed_to: str
    triggered_by_lens: PhaseKey
    triggered_by_gap: str
    body: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------- Reasoning trail ----------

ReasoningKind = Literal[
    "self_assess",
    "pivot",
    "backtrack",
    "decision",
]


class ReasoningItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: ReasoningKind
    phase: PhaseKey
    headline: str
    detail: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ---------- Audit state ----------


class ProgramAudit(BaseModel):
    audit_id: str
    program_name: str
    recipient_hint: Optional[str] = None
    goal_anchor: Optional[GoalAnchor] = None
    lenses: dict[LensKey, Lens] = {}
    drafts: list[AccountabilityDraft] = []
    synthesis: Optional[Synthesis] = None
    reasoning_trail: list[ReasoningItem] = []
    metadata: dict = {}


# ---------- Logging (carried forward from briefbot) ----------


class LogEntry(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    message: str
    level: str = "info"
    details: Optional[str] = None

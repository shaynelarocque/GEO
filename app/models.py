from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
import uuid


class Founder(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: Optional[str] = None
    canada_status: str
    hours_per_week: str
    profile_url: Optional[str] = None
    current_role: str
    relevant_background: str
    prior_founding_experience: Optional[str] = None


class Application(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    startup_name: str
    problem_statement: str
    solution: str
    sdgs: list[str] = []
    prior_incubator: Optional[str] = None
    website_url: Optional[str] = None
    canada_incorporated: bool = False
    how_team_formed: str = ""
    how_long_known: str = ""
    additional_team_info: Optional[str] = None
    founders: list[Founder] = []
    submitted_at: datetime = Field(default_factory=datetime.utcnow)


class HumanReviewFlag(BaseModel):
    section: str
    issue: str
    attempted: str
    suggestion: str
    severity: str = "medium"  # low, medium, high, critical


class BriefOutput(BaseModel):
    app_id: str
    sections: dict[str, str] = {}
    human_review_flags: list[HumanReviewFlag] = []
    metadata: dict = {}


class LogEntry(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    message: str
    level: str = "info"
    details: Optional[str] = None  # Expandable detail content

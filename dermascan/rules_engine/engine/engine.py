"""
Author: Dermascan Team
Date: 14-6-2026
Licence: MIT

Typed interface contracts for the Rules Engine."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field


ReviewType = Literal["product", "skin", "hybrid"]


class SkinContext(BaseModel):
    """Optional structured context for skin-only or hybrid review flows."""

    concerns: list[str] = Field(default_factory=list)
    skin_type: str = ""
    symptoms: list[str] = Field(default_factory=list)
    age_range: str = ""
    notes: str = ""


class SkinCurrentCondition(BaseModel):
    """Observed current state of skin inferred from review text."""

    severity: str = ""
    active_inflammation: bool = False
    barrier_status: str = ""
    observed_patterns: list[str] = Field(default_factory=list)


class SkinNeeds(BaseModel):
    """Derived skin needs used for downstream alignment checks."""

    barrier_repair: bool = False
    anti_inflammatory: bool = False
    comedolytic: bool = False
    hydration: bool = False
    sun_protection: bool = False


class SkinAudit(BaseModel):
    """Canonical bridge schema used between skin and product audits."""

    primary_concern: str = ""
    current_condition: SkinCurrentCondition = Field(default_factory=SkinCurrentCondition)
    needs: SkinNeeds = Field(default_factory=SkinNeeds)
    contraindicated_approaches: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    completeness_score: float = 0.0
    missing_fields: list[str] = Field(default_factory=list)


class RulesEngineRequest(BaseModel):
    """Canonical payload consumed by the rules engine worker."""

    schema_version: str = "v1"
    request_id: str
    case_id: str
    client_id: str
    message_id: str
    channel: str
    review_type: ReviewType = "product"
    escalation_notes: str = ""
    ingredients: list[str] = Field(default_factory=list)
    skin_context: SkinContext = Field(default_factory=SkinContext)
    skin_audit: SkinAudit = Field(default_factory=SkinAudit)
    diagnosis: str = ""
    summary: str = ""
    treatment: str = ""
    product_name: str = ""
    description: str = ""
    indications: str = ""
    safety_in_pregnancy: str = ""
    contraindications: str = ""
    how_to_use: str = ""
    special_instructions: str = ""
    raw_model_json: str = ""


class IngredientIndexResult(BaseModel):
    """Tracks whether an ingredient is already indexed or newly indexed."""

    ingredient: str
    normalized_name: str
    ingredient_id: str = ""
    indexed: bool = False
    source: str = "missing"
    notes: str = ""


class RulesEngineResponse(BaseModel):
    """Structured result emitted by the rules engine to signal stream."""

    request_id: str
    case_id: str
    client_id: str
    message_id: str
    channel: str
    review_type: ReviewType
    summary: str
    sources: list[str] = Field(default_factory=list)
    escalation_notes: str = ""
    indexed_ingredients: list[IngredientIndexResult] = Field(default_factory=list)
    report: dict[str, Any] = Field(default_factory=dict)


class Engine(ABC):
    """Abstract base contract for all rules-engine implementations."""

    @abstractmethod
    def ensure_reports_exists(self, ingredients: list[str]) -> list[IngredientIndexResult]:
        """Ensure ingredient reports are indexed and queryable."""

    @abstractmethod
    def evaluate_safety(self, ingredient_ids: list[str]) -> list[dict[str, Any]]:
        """Evaluate safety evidence from vector search results."""

    @abstractmethod
    def evaluate_contraindications(self, ingredient_ids: list[str]) -> list[dict[str, Any]]:
        """Evaluate contraindication/interaction evidence from vector search."""

    @abstractmethod
    def evaluate_irritation_and_dermatology(self, ingredient_ids: list[str]) -> list[dict[str, Any]]:
        """Evaluate irritation and dermatology evidence from vector search."""

    @abstractmethod
    def assess_product_review(self, request: RulesEngineRequest) -> RulesEngineResponse:
        """Assess a product-review request using vector evidence and summarization."""

    @abstractmethod
    def assess_skin_review(self, request: RulesEngineRequest) -> RulesEngineResponse:
        """Assess a skin-review request using internet research only."""

    @abstractmethod
    def process(self, request: RulesEngineRequest) -> RulesEngineResponse:
        """Route and process product/skin/hybrid requests."""


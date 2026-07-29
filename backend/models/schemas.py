"""Modelos Pydantic v2 de request/response (regla S4: todo endpoint valida su input)."""
from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from backend.analyzers.url_safety import ensure_scheme


class ProjectOut(BaseModel):
    id: int
    slug: str
    name: str
    url: str
    gsc_property: str
    country: str
    language: str
    competitors: list[str]
    is_active: bool


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str
    country: str = Field(default="CO", min_length=2, max_length=2)
    language: str = Field(default="es", min_length=2, max_length=5)
    competitors: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = ensure_scheme(value)  # "ejemplo.com" -> "https://ejemplo.com" (UX)
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("La URL debe empezar con http:// o https://")
        if not parsed.netloc:
            raise ValueError("URL inválida: falta el dominio")
        return value

    @field_validator("competitors")
    @classmethod
    def validate_competitors(cls, value: list[str]) -> list[str]:
        cleaned = []
        for c in value:
            domain = c.strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
            if domain:
                cleaned.append(domain)
        return cleaned


class ScorecardOut(BaseModel):
    seo_score: int | None = None
    seo_score_delta: int | None = None
    score_breakdown: dict[str, int] | None = None
    geo_score: int | None = None
    content_score: int | None = None
    local_score: int | None = None
    clicks_28d: int
    impressions_28d: int
    ctr_28d: float
    avg_position_28d: float | None
    keywords_ranking: int
    issues_open: int
    issues_critical: int
    last_snapshot_at: str | None = None


class GscDailyPoint(BaseModel):
    date: str
    clicks: int
    impressions: int
    ctr: float
    position: float


class GscQueryRow(BaseModel):
    query: str
    page: str | None
    clicks: int
    impressions: int
    ctr: float
    position: float


class RankingsOut(BaseModel):
    daily: list[GscDailyPoint]
    queries: list[GscQueryRow]


class TechnicalPageRow(BaseModel):
    url: str
    last_crawled: str | None
    row: dict[str, str]  # title|meta_description|h1|schema|og|canonical|indexable -> green|yellow|red


class TechnicalOut(BaseModel):
    pages: list[TechnicalPageRow]
    summary: dict[str, int]  # green/yellow/red counts


class IssueOut(BaseModel):
    id: int
    severity: str
    icon: str
    category: str
    title: str
    current_text: str | None
    suggested_text: str | None
    page_url: str | None
    effort: str | None
    impact: int | None
    status: str


class ActionPlanOut(BaseModel):
    critical: list[IssueOut]
    high: list[IssueOut]
    medium: list[IssueOut]


class CollectRequest(BaseModel):
    max_pages: int = Field(default=15, ge=1, le=500)
    keywords: list[str] | None = Field(default=None, max_length=20)  # collector "trends"
    competitor_domain: str | None = None  # collector "competitor"
    # § bug real 2026-07-25: los collectors muy lentos (indexación: ~6 min contra
    # la URL Inspection API) no pueden responderse de forma síncrona — el
    # navegador corta la conexión antes de que termine. Con background=True la
    # petición vuelve al instante y el resultado se recoge por /progress.
    background: bool = False
    # § 2026-07-27: Search Console deja elegir período (7d/28d/3-16 meses) en
    # su propia UI — antes esto era fijo a 30 días. Solo lo usa el collector "gsc".
    lookback_days: int = Field(default=30, ge=1, le=480)


class CollectResult(BaseModel):
    # snapshot_id es None cuando el trabajo apenas se lanzó en segundo plano
    # (status="started"): todavía no existe snapshot que referenciar.
    snapshot_id: int | None = None
    status: str
    summary: dict | None = None


class IssueStatusUpdate(BaseModel):
    status: str = Field(pattern="^(open|done|dismissed)$")


class AIChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class AIChatResponse(BaseModel):
    response: str
    tokens_used: int
    cost_estimate: float


class AIMessageOut(BaseModel):
    role: str
    content: str
    tokens_used: int
    cost_estimate: float
    created_at: str


class AIFixMetaRequest(BaseModel):
    issue_id: int


class AIFixMetaResponse(BaseModel):
    suggestions: list[str]
    cost_estimate: float


class AIGenerateSchemaRequest(BaseModel):
    page_id: int
    schema_type: str = Field(pattern="^(LocalBusiness|Service|FAQPage|Product|Organization)$")


class AIGenerateSchemaResponse(BaseModel):
    schema_jsonld: str
    cost_estimate: float


class AIClassifyIntentRequest(BaseModel):
    keywords: list[str] | None = Field(default=None, max_length=30)
    limit: int = Field(default=20, ge=1, le=30)


class AIClassifyIntentResponse(BaseModel):
    classifications: dict[str, str]
    cost_estimate: float


class AIContentClustersRequest(BaseModel):
    limit: int = Field(default=40, ge=5, le=60)


class ContentClusterOut(BaseModel):
    name: str
    pillar_title: str
    keywords: list[str]


class AIContentClustersResponse(BaseModel):
    clusters: list[ContentClusterOut]
    keywords_used: int
    cost_estimate: float


class AddCompetitorRequest(BaseModel):
    url: str = Field(min_length=1, max_length=500)


class AICompetitorInsightsRequest(BaseModel):
    competitor_domain: str = Field(min_length=1, max_length=255)


class AICompetitorInsightsResponse(BaseModel):
    insights: str
    cost_estimate: float


class IndexNowSubmitRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=10_000)
    engine: str = "bing"


class SerpCompareRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=300)
    # Tope bajo a propósito: cada URL es un fetch real a un sitio ajeno a 1 req/s.
    max_urls: int = Field(default=5, ge=1, le=10)


class QuickAnalysisRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class QuickAnalysisResponse(BaseModel):
    url: str
    title: str | None
    meta_description: str | None
    h1_tags: list[str]
    schema_types: list[str]
    og: dict
    canonical: str | None
    is_indexable: bool
    word_count: int
    technical_row: dict[str, str]
    issues: list[dict]
    geo: dict

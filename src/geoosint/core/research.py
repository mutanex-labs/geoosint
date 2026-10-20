"""Происхождение клиентского веб-исследования; сведения не верифицируются сервером."""

from datetime import datetime
from typing import Literal

from pydantic import AnyHttpUrl, Field, model_validator

from geoosint.core.models import StrictModel


class ClientCapabilities(StrictModel):
    WEB_SEARCH_AVAILABLE: bool | None = None
    WEB_PAGES_AVAILABLE: bool | None = None
    IMAGE_SEARCH_AVAILABLE: bool | None = None
    BROWSER_AVAILABLE: bool | None = None
    MAP_VIEW_AVAILABLE: bool | None = None
    REVERSE_IMAGE_SEARCH_AVAILABLE: bool | None = None


class ResearchSource(StrictModel):
    url: AnyHttpUrl
    title: str = Field(min_length=1, max_length=300)
    retrieved_at: datetime
    access: Literal["SNIPPET_ONLY", "PAGE_OPENED", "IMAGE_VIEWED", "UNAVAILABLE"]
    relevance: str = Field(min_length=1, max_length=1500)
    limitations: list[str] = Field(default_factory=list, max_length=10)
    image_urls: list[AnyHttpUrl] = Field(default_factory=list, max_length=10)
    dependency_group: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def timezone_and_credentials(self) -> "ResearchSource":
        if self.retrieved_at.utcoffset() is None:
            raise ValueError("Время получения требует UTC offset")
        for url in [self.url, *self.image_urls]:
            if url.username or url.password:
                raise ValueError("Учётные данные в ссылке недопустимы")
        return self


class ResearchRecord(StrictModel):
    kind: Literal[
        "SEARCH_RESULT", "PAGE_CONTENT", "VISUAL_REFERENCE", "MODEL_COMPARISON", "GEO_SOURCE"
    ]
    client_tool: str = Field(min_length=1, max_length=128)
    tool_calls: int = Field(default=1, ge=0, le=100)
    queries: list[str] = Field(default_factory=list, max_length=10)
    sources: list[ResearchSource] = Field(default_factory=list, max_length=20)
    capabilities: ClientCapabilities | None = None
    outcome: Literal["OK", "PARTIAL", "UNAVAILABLE", "NO_USEFUL_RESULTS"] = "OK"

    @model_validator(mode="after")
    def bounded_queries(self) -> "ResearchRecord":
        if any(not query or len(query) > 1000 for query in self.queries):
            raise ValueError("Поисковый запрос: 1–1000 знаков")
        if self.kind == "PAGE_CONTENT" and not any(
            source.access == "PAGE_OPENED" for source in self.sources
        ):
            raise ValueError("PAGE_CONTENT требует фактически открытого источника")
        if self.kind in ("VISUAL_REFERENCE", "MODEL_COMPARISON") and not any(
            source.access == "IMAGE_VIEWED" for source in self.sources
        ):
            raise ValueError("Визуальная запись требует просмотра изображения клиентом")
        return self

from pydantic import BaseModel, ConfigDict

MAX_PAGE_NODES = 50_000
MAX_PAGE_ATTRIBUTES = 25_000


class ExtractedArticle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str | None
    text: str | None
    markup_truncated: bool = False

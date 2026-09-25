from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict


def is_none(value: object) -> bool:
    return value is None


def to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


type UtcDateTime = Annotated[datetime, AfterValidator(to_utc)]


class ResponseBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_name=True, validate_by_alias=True)

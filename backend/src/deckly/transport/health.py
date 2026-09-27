from typing import Annotated, Self

from fastapi import APIRouter, Depends, Header
from pydantic import Field

from deckly.application.health import CheckHealth, HealthReport, ServiceStatus
from deckly.transport.body import ResponseBody, is_none
from deckly.transport.dependencies import check_health_use_case
from deckly.transport.generations import CLIENT_ID_HEADER, CanonicalUuid, QuotaBody


class HealthBody(ResponseBody):
    status: ServiceStatus
    version: str
    quota: QuotaBody | None = Field(default=None, exclude_if=is_none)

    @classmethod
    def from_report(cls, report: HealthReport) -> Self:
        return cls(
            status=report.status,
            version=report.version,
            quota=None if report.quota is None else QuotaBody.from_quota(report.quota),
        )


router = APIRouter()


@router.get("/health")
async def get_health(
    client_id: Annotated[CanonicalUuid | None, Header(alias=CLIENT_ID_HEADER)] = None,
    *,
    check: Annotated[CheckHealth, Depends(check_health_use_case)],
) -> HealthBody:
    return HealthBody.from_report(await check(client_id))

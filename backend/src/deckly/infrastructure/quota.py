from datetime import datetime, timedelta
from uuid import UUID

from deckly.application.ports import Quota

QUOTA_WINDOW = timedelta(days=1)


class UnmeteredQuota:
    def __init__(self, limit: int) -> None:
        self._limit = limit

    async def current(self, client_id: UUID, now: datetime) -> Quota:
        del client_id
        return Quota(limit=self._limit, remaining=self._limit, resets_at=now + QUOTA_WINDOW)

import pytest

from deckly.domain.exceptions import JobNotFoundError
from deckly.domain.job import FailureCode, GenerationJob
from tests.domain.builders import JOB_ID, T0, at
from tests.fakes import Harness, generation_request, job_id, scope

pytestmark = pytest.mark.anyio


async def test_created_job_is_returned_as_stored() -> None:
    harness = Harness()
    created = await harness.create(generation_request(), scope())

    assert await harness.get(created.job.job_id) == created.job


async def test_latest_stored_state_is_returned() -> None:
    harness = Harness()
    failed = GenerationJob.queue(JOB_ID, T0).fail(FailureCode.NO_VALID_CONTENT, at(5))
    harness.store.replace(failed)

    assert await harness.get(JOB_ID) == failed


async def test_unknown_job_is_not_found() -> None:
    harness = Harness()
    await harness.create(generation_request(), scope())

    with pytest.raises(JobNotFoundError):
        await harness.get(job_id(999))

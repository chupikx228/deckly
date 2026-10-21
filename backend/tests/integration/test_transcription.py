import asyncio
import io
import itertools
import json
import re
import shutil
import statistics
import threading
import time
from asyncio.subprocess import PIPE
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import av
import pytest
from arq.connections import ArqRedis
from arq.worker import Worker

from deckly.application.generations import lane_for
from deckly.application.pipeline import DocumentSourcing, RunGeneration
from deckly.application.ports import IdempotencyScope
from deckly.config import TRANSCRIPTION_WORKER_MAX_JOBS, Settings
from deckly.domain.document import DocumentFormat
from deckly.domain.job import GenerationJob, Succeeded
from deckly.domain.notes.note_type import NoteType
from deckly.domain.source import TranscriptSource
from deckly.infrastructure.card_generator.generator import LlmCardGenerator
from deckly.infrastructure.card_generator.note_types import NOTE_TYPE_HANDLERS
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.queue import TRANSCRIPTION_QUEUE_NAME
from deckly.infrastructure.recordings.sniffing import detect_recording
from deckly.infrastructure.transcription.client import SAMPLE_RATE, DecodedAudio, Transcript
from deckly.infrastructure.transcription.model import pinned_checksums, require_ffmpeg, require_model
from deckly.infrastructure.transcription.self_hosted import ENGINE_MODULE, SelfHostedTranscriptionClient
from deckly.infrastructure.transcription.transcriber import RecordingTranscriber
from deckly.worker.settings import (
    RUN_GENERATION_KEY,
    TranscriptionBackend,
    TranscriptionWorkerSettings,
    build_recording_transcriber,
    build_transcription_client,
)
from tests.domain.builders import T0
from tests.fakes import (
    DOCUMENT_CLIENT_ID,
    FakeDocuments,
    FakeLlmClient,
    FakeProviders,
    InMemoryJobStore,
    InMemoryResultCache,
    document_request,
    fresh_metrics,
    fresh_telemetry,
    model_reply,
    sequential_job_ids,
)
from tests.ffmpeg import production_decoder
from tests.integration.conftest import Cleanup, job_queue
from tests.logs import captured_json_logs
from tests.recording_files import FIXTURES, understated_timing
from tests.transcription import InMemoryTranscriptCache, recording

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ENGLISH = FIXTURES / "speech-en.mp3"
RUSSIAN = FIXTURES / "speech-ru.mp3"
ENGLISH_LECTURE_WORDS = frozenset({"world", "struggle", "clash", "unavoidable", "imperialism", "existing"})
PAUSED = FIXTURES / "speech-pause.mp3"
PAUSED_SECOND_SENTENCE_STARTS_MS = 17_360
PAUSED_SECOND_SENTENCE_ENDS_MS = 22_700
PAUSED_FIRST_SENTENCE_ENDS_MS = 11_920
TIMING_TOLERANCE_MS = 600
LYING_CAP_SECONDS = 10
UNDERSTATED_MP3_FRAME_DELTA = 32
MPEG_2_LAYER_III_FRAME_SAMPLES = 576
LAG_TICK_SECONDS = 0.01
GENERATION_JOBS = 5
GENERATION_JOB_STEPS = 100
MAX_ISOLATED_LAG_P99_SECONDS = 0.05
MAX_ISOLATED_SLOWDOWN = 1.5
GIL_HOLD_SECONDS = 0.2
STARVED_LAG_MIN_SHARE_OF_HOLD = 0.75
GIL_CALIBRATION_LOOP = 2_000_000
GIL_CALIBRATION_ROUNDS = 5
CONTROL_JOBS = 1
CONTROL_JOB_STEPS = 10
ENGINE_START_SECONDS = 1.5
PROCESS_EXIT_SECONDS = 5


@pytest.fixture
def model_ready(settings: Settings) -> Settings:
    require_model(settings.transcription.model_directory, pinned_checksums())
    return settings


def transcriber(settings: Settings, *, max_seconds: int | None = None) -> RecordingTranscriber:
    backend = TranscriptionBackend(
        client=build_transcription_client(settings), ffmpeg=require_ffmpeg(), cache_redis=ArqRedis()
    )
    built = replace(
        build_recording_transcriber(settings, backend, fresh_metrics()), cache=InMemoryTranscriptCache()
    )
    if max_seconds is None:
        return built
    return replace(built, decoder=replace(built.decoder, max_seconds=max_seconds))


async def decoded(path: Path, workdir: Path, max_seconds: int = 900) -> DecodedAudio:
    return await production_decoder(max_seconds).decode(path, DocumentFormat.MP3, workdir / "audio.pcm")


async def transcript_of(settings: Settings, path: Path, workdir: Path) -> Transcript:
    return await build_transcription_client(settings).transcribe(await decoded(path, workdir))


def words(transcript: Transcript) -> str:
    return " ".join(segment.text for segment in transcript.segments).lower()


def spoken_words(transcript: Transcript) -> set[str]:
    return set(re.findall(r"\w+", words(transcript)))


async def test_english_lecture_is_transcribed_in_english_with_its_words(
    model_ready: Settings, tmp_path: Path
) -> None:
    transcript = await transcript_of(model_ready, ENGLISH, tmp_path)

    assert transcript.language == "en"
    assert spoken_words(transcript) >= ENGLISH_LECTURE_WORDS


async def test_real_engine_reports_the_whole_recording_as_done_when_its_only_chunk_finishes(
    model_ready: Settings, tmp_path: Path
) -> None:
    reported: list[float] = []

    async def report(fraction: float) -> None:
        reported.append(fraction)

    audio = await decoded(PAUSED, tmp_path)

    await build_transcription_client(model_ready).transcribe(audio, report)

    assert reported == [1.0]


async def test_russian_reading_is_detected_as_russian_and_transcribed_in_cyrillic(
    model_ready: Settings, tmp_path: Path
) -> None:
    transcript = await transcript_of(model_ready, RUSSIAN, tmp_path)

    assert transcript.language == "ru"
    spoken = words(transcript)
    for phrase in ("перебираться", "породой людей", "губернии"):
        assert phrase in spoken


async def test_timestamps_follow_the_audio_across_a_pause_and_never_pass_its_end(
    model_ready: Settings, tmp_path: Path
) -> None:
    audio = await decoded(PAUSED, tmp_path)
    transcript = await build_transcription_client(model_ready).transcribe(audio)

    segments = transcript.segments
    assert all(earlier.end_ms <= later.start_ms for earlier, later in itertools.pairwise(segments))
    assert max(segment.end_ms for segment in segments) <= round(audio.seconds * 1000)
    [first_sentence_end] = [
        segment for segment in segments if "existing state of things" in segment.text.lower()
    ]
    assert first_sentence_end.end_ms <= PAUSED_FIRST_SENTENCE_ENDS_MS + TIMING_TOLERANCE_MS
    [second] = [segment for segment in segments if "germany and england" in segment.text.lower()]
    assert abs(second.end_ms - PAUSED_SECOND_SENTENCE_ENDS_MS) <= TIMING_TOLERANCE_MS
    assert first_sentence_end.end_ms <= second.start_ms <= PAUSED_SECOND_SENTENCE_STARTS_MS


async def test_recording_becomes_material_whose_citations_lie_within_the_recording(
    model_ready: Settings, tmp_path: Path
) -> None:
    del tmp_path
    lecture = recording(seconds=22.824, document_format=DocumentFormat.MP3)

    material = await transcriber(model_ready).parse(lecture, await asyncio.to_thread(PAUSED.read_bytes))

    sources = [entry.source for entry in material]
    assert all(isinstance(source, TranscriptSource) for source in sources)
    assert all(
        isinstance(source, TranscriptSource)
        and source.document_id == lecture.document_id
        and 0 <= source.start_ms < source.end_ms <= 22_824
        for source in sources
    )
    assert "germany and england" in " ".join(entry.text for entry in material).lower()


async def output_of(*arguments: str) -> bytes:
    process = await asyncio.create_subprocess_exec(*arguments, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode(errors="replace")
    return stdout


async def mp3_in_mp4(source: Path, target: Path) -> bytes:
    ffmpeg = str(require_ffmpeg())
    await output_of(
        ffmpeg,
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-c:a",
        "copy",
        "-f",
        "mp4",
        str(target),
    )
    return await asyncio.to_thread(target.read_bytes)


async def decoded_seconds_by_ffmpeg_with_t(source: Path, cap_seconds: int) -> float:
    pcm = await output_of(
        str(require_ffmpeg()),
        "-nostdin",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file",
        "-f",
        "mov",
        "-i",
        str(source),
        "-t",
        str(cap_seconds),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-f",
        "s16le",
        "pipe:1",
    )
    return len(pcm) / (SAMPLE_RATE * 2)


def decoded_seconds_by_pyav(source: Path) -> float:
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    buffer = io.BytesIO()
    with av.open(str(source)) as container:
        for frame in container.decode(audio=0):
            for resampled in resampler.resample(frame):
                buffer.write(resampled.to_ndarray().tobytes())
    return len(buffer.getvalue()) / (SAMPLE_RATE * 2)


@dataclass(frozen=True, slots=True)
class LyingRecording:
    path: Path
    content: bytes


@pytest.fixture
async def lying_recording(tmp_path: Path) -> LyingRecording:
    honest = await mp3_in_mp4(PAUSED, tmp_path / "honest.mp4")
    content = understated_timing(honest, UNDERSTATED_MP3_FRAME_DELTA)
    path = tmp_path / "lying.mp4"
    await asyncio.to_thread(path.write_bytes, content)
    return LyingRecording(path=path, content=content)


async def test_recording_that_lies_about_its_length_passes_the_probe_and_fools_both_ffmpeg_t_and_pyav(
    lying_recording: LyingRecording,
) -> None:
    claimed = detect_recording(lying_recording.content, LYING_CAP_SECONDS).duration_seconds

    assert claimed is not None
    assert claimed == pytest.approx(
        22.7 * UNDERSTATED_MP3_FRAME_DELTA / MPEG_2_LAYER_III_FRAME_SAMPLES, abs=0.1
    )
    with_t = await decoded_seconds_by_ffmpeg_with_t(lying_recording.path, LYING_CAP_SECONDS)
    assert with_t > LYING_CAP_SECONDS * 2
    assert await asyncio.to_thread(decoded_seconds_by_pyav, lying_recording.path) > LYING_CAP_SECONDS * 2


async def test_recording_that_lies_about_its_length_is_decoded_exactly_to_the_cap(
    lying_recording: LyingRecording, tmp_path: Path
) -> None:
    target = tmp_path / "a.pcm"

    audio = await production_decoder(LYING_CAP_SECONDS).decode(
        lying_recording.path, DocumentFormat.M4A, target
    )

    assert audio.seconds == LYING_CAP_SECONDS
    assert audio.truncated is True
    assert len(await asyncio.to_thread(target.read_bytes)) == LYING_CAP_SECONDS * SAMPLE_RATE * 2


async def test_recording_that_lies_about_its_length_is_transcribed_only_up_to_the_cap(
    model_ready: Settings, lying_recording: LyingRecording
) -> None:
    claimed = detect_recording(lying_recording.content, LYING_CAP_SECONDS).duration_seconds
    lying = recording(seconds=claimed or 1.0, document_format=DocumentFormat.M4A)
    capped = transcriber(model_ready, max_seconds=LYING_CAP_SECONDS)

    with captured_json_logs() as logs:
        material = await capped.parse(lying, lying_recording.content)

    assert all(
        isinstance(entry.source, TranscriptSource) and entry.source.end_ms <= LYING_CAP_SECONDS * 1000
        for entry in material
    )
    spoken = " ".join(entry.text for entry in material).lower()
    assert "world struggle" in spoken
    assert "germany" not in spoken
    [truncated] = logs.named("recording_decode_truncated")
    assert truncated["decoded_seconds"] == LYING_CAP_SECONDS
    [transcribed] = logs.named("recording_transcribed")
    assert transcribed["audio_seconds"] == LYING_CAP_SECONDS
    assert capped.metrics.registry.get_sample_value("deckly_recording_decode_truncations_total") == 1


async def engines_running() -> bool:
    pgrep = shutil.which("pgrep")
    assert pgrep is not None
    process = await asyncio.create_subprocess_exec(pgrep, "-f", ENGINE_MODULE, stdout=PIPE, stderr=PIPE)
    stdout, _ = await process.communicate()
    return bool(stdout.split())


ENGINE_POLL_SECONDS = 0.05


async def engines_reach(*, running: bool, timeout_seconds: float) -> bool:
    for _ in range(int(timeout_seconds / ENGINE_POLL_SECONDS)):
        if await engines_running() == running:
            return True
        await asyncio.sleep(ENGINE_POLL_SECONDS)
    return await engines_running() == running


async def test_cancelled_transcription_leaves_no_engine_running(
    model_ready: Settings, tmp_path: Path
) -> None:
    audio = await decoded(PAUSED, tmp_path)
    task = asyncio.ensure_future(build_transcription_client(model_ready).transcribe(audio))
    assert await engines_reach(running=True, timeout_seconds=ENGINE_START_SECONDS * 4)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await engines_reach(running=False, timeout_seconds=PROCESS_EXIT_SECONDS)


@dataclass(frozen=True, slots=True)
class Responsiveness:
    lag_p99_seconds: float
    lag_max_seconds: float
    job_seconds: float


async def generation_job(steps: int) -> float:
    started = time.perf_counter()
    for _ in range(steps):
        await asyncio.sleep(0.005)
        json.loads(json.dumps({"notes": [{"front": "q" * 50, "back": "a" * 200}] * 20}))
    return time.perf_counter() - started


async def responsiveness_while(
    background: Callable[[], Coroutine[object, object, None]] | None,
    *,
    jobs: int = GENERATION_JOBS,
    steps: int = GENERATION_JOB_STEPS,
) -> Responsiveness:
    lags: list[float] = []
    stop = asyncio.Event()

    async def tick() -> None:
        while not stop.is_set():
            before = time.perf_counter()
            await asyncio.sleep(LAG_TICK_SECONDS)
            lags.append(time.perf_counter() - before - LAG_TICK_SECONDS)

    ticker = asyncio.create_task(tick())
    running = asyncio.create_task(background()) if background is not None else None
    await asyncio.sleep(ENGINE_START_SECONDS)
    durations = [await generation_job(steps) for _ in range(jobs)]
    stop.set()
    await ticker
    if running is not None:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    lags.sort()
    return Responsiveness(
        lag_p99_seconds=lags[int(len(lags) * 0.99)],
        lag_max_seconds=lags[-1],
        job_seconds=statistics.mean(durations),
    )


def loop_holding_the_gil_for(seconds: float) -> int:
    def timed() -> float:
        started = time.perf_counter()
        sum(range(GIL_CALIBRATION_LOOP))
        return time.perf_counter() - started

    fastest = min(timed() for _ in range(GIL_CALIBRATION_ROUNDS))
    return int(GIL_CALIBRATION_LOOP * seconds / fastest)


@contextmanager
def gil_held_in_process(loop: int) -> Iterator[None]:
    stop = threading.Event()

    def hold() -> None:
        while not stop.is_set():
            sum(range(loop))

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        yield
    finally:
        stop.set()
        holder.join()


async def test_generation_jobs_keep_their_pace_while_a_transcription_runs_beside_them(
    model_ready: Settings, tmp_path: Path
) -> None:
    audio = await decoded(PAUSED, tmp_path)
    client: SelfHostedTranscriptionClient = build_transcription_client(model_ready)

    async def transcribe_in_a_loop() -> None:
        while True:
            await client.transcribe(audio)

    alone = await responsiveness_while(None)
    beside_transcription = await responsiveness_while(transcribe_in_a_loop)

    assert beside_transcription.lag_p99_seconds < MAX_ISOLATED_LAG_P99_SECONDS
    assert beside_transcription.job_seconds < alone.job_seconds * MAX_ISOLATED_SLOWDOWN


async def test_the_same_measurement_catches_work_that_holds_the_interpreter_lock_in_process() -> None:
    alone = await responsiveness_while(None, jobs=CONTROL_JOBS, steps=CONTROL_JOB_STEPS)
    with gil_held_in_process(loop_holding_the_gil_for(GIL_HOLD_SECONDS)):
        starved = await responsiveness_while(None, jobs=CONTROL_JOBS, steps=CONTROL_JOB_STEPS)

    assert starved.lag_max_seconds > GIL_HOLD_SECONDS * STARVED_LAG_MIN_SHARE_OF_HOLD
    assert starved.job_seconds > alone.job_seconds * MAX_ISOLATED_SLOWDOWN


async def test_recording_job_enqueued_like_the_api_does_is_transcribed_by_the_transcription_worker(
    model_ready: Settings, queue_pool: ArqRedis, cleanup: Cleanup
) -> None:
    lecture = replace(
        recording(seconds=22.824, document_format=DocumentFormat.MP3), client_id=DOCUMENT_CLIENT_ID
    )
    documents = FakeDocuments()
    documents.keep(lecture, await asyncio.to_thread(PAUSED.read_bytes))
    providers = FakeProviders()
    llm = FakeLlmClient(
        model_reply(
            {
                "deck": {"title": "The Balkan war", "tags": []},
                "notes": [
                    {
                        "noteType": NoteType.BASIC,
                        "fields": {"front": "Which two powers?", "back": "Germany and England"},
                        "sources": [1],
                    }
                ],
            }
        )
    )
    ids = sequential_job_ids()
    store = InMemoryJobStore()
    run = RunGeneration(
        store=store,
        cache=InMemoryResultCache(),
        retriever=providers,
        parser=providers,
        documents=DocumentSourcing(
            store=documents, storage=documents.storage, parser=transcriber(model_ready), moderator=documents
        ),
        generator=LlmCardGenerator(llm=llm, new_id=lambda: next(ids), handlers=NOTE_TYPE_HANDLERS),
        moderator=providers,
        media=providers,
        clock=utc_now,
        telemetry=fresh_telemetry(),
    )
    job = GenerationJob.queue(uuid4(), T0)
    cleanup.job_ids.add(job.job_id)
    request = replace(document_request(lecture.document_id), include_images=False)
    await store.add(job, request, IdempotencyScope(client_id=DOCUMENT_CLIENT_ID, idempotency_key=uuid4()))
    await job_queue(queue_pool).enqueue(job.job_id, lane_for(lecture))

    await Worker(
        functions=TranscriptionWorkerSettings.functions,
        redis_pool=queue_pool,
        queue_name=TRANSCRIPTION_QUEUE_NAME,
        ctx={RUN_GENERATION_KEY: run},
        handle_signals=False,
        burst=True,
        max_jobs=TRANSCRIPTION_WORKER_MAX_JOBS,
        job_timeout=model_ready.transcription.job_timeout_seconds,
    ).async_run()

    finished = store.jobs[job.job_id]
    assert isinstance(finished.state, Succeeded)
    [note] = finished.state.result.notes
    [source] = note.sources
    assert isinstance(source, TranscriptSource)
    assert source.document_id == lecture.document_id
    assert 0 <= source.start_ms < source.end_ms <= 22_824
    [prompt] = llm.prompts
    assert "Germany and England" in prompt.user

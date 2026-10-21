import math
import time

import pytest

from deckly.application.ports import InspectedUpload
from deckly.domain.document import DocumentFormat
from deckly.domain.exceptions import RecordingTooLongError, UnsupportedDocumentError
from deckly.infrastructure.recordings.esds import MAX_AAC_SAMPLE_RATE, MIN_AAC_SAMPLE_RATE, SHORTEST_AAC_FRAME
from deckly.infrastructure.recordings.isobmff import MAX_FRAGMENT_BOXES, MAX_TIME_TO_SAMPLE_ENTRIES
from deckly.infrastructure.recordings.sniffing import detect_recording
from tests.document_files import MIB
from tests.memory import peak_memory
from tests.recording_files import (
    AAC_FRAME_SAMPLES,
    MPEG1_AUDIO,
    SOUND_TIMESCALE,
    VIDEO_TRACK_ID,
    Track,
    TrackRun,
    audio_specific_config,
    box,
    decoder_configuration,
    fixture,
    fragment_header,
    fragmented_movie,
    fragmented_track,
    full_box,
    isobmff,
    movie,
    movie_extends,
    movie_extends_header,
    movie_fragment,
    sound_entry,
    time_to_sample,
    track,
    track_extends,
    track_fragment,
    track_header,
    track_run,
)

CAP_SECONDS = 900
SHORT_CAP_SECONDS = 60
PROBE_SECONDS_BUDGET = 0.25
PROBE_MEMORY_BUDGET_BYTES = MIB
FLOOD_ROUNDS = 5
FLOOD_SCALE = 8
MAX_FLOOD_COST_GROWTH = 3
FRAGMENT_INDEX_MEMORY_BUDGET_BYTES = 4 * MIB


def probe(content: bytes, cap: int = CAP_SECONDS) -> InspectedUpload:
    return detect_recording(content, cap)


def unsupported(content: bytes, cap: int = CAP_SECONDS) -> None:
    with pytest.raises(UnsupportedDocumentError):
        probe(content, cap)


def cheaply(
    check: type[Exception],
    content: bytes,
    cap: int = CAP_SECONDS,
    memory_budget_bytes: int = PROBE_MEMORY_BUDGET_BYTES,
) -> None:
    started = time.perf_counter()
    with peak_memory() as peak, pytest.raises(check):
        probe(content, cap)
    assert time.perf_counter() - started < PROBE_SECONDS_BUDGET
    assert peak[0] < memory_budget_bytes


def fastest_rejections(*floods: bytes) -> list[float]:
    fastest = [math.inf] * len(floods)
    for _ in range(FLOOD_ROUNDS):
        for index, content in enumerate(floods):
            started = time.perf_counter()
            unsupported(content)
            fastest[index] = min(fastest[index], time.perf_counter() - started)
    return fastest


def frames(count: int) -> float:
    return count * AAC_FRAME_SAMPLES / SOUND_TIMESCALE


def fragment(*runs: bytes, header: bytes | None = None, sequence: int = 1) -> bytes:
    return movie_fragment(track_fragment(header or fragment_header(), *runs), sequence=sequence)


def frame_run(count: int) -> bytes:
    return track_run(count)


@pytest.mark.parametrize(
    ("name", "document_format"),
    [("tone-fragmented.m4a", DocumentFormat.M4A), ("clip-fragmented.mp4", DocumentFormat.MP4)],
)
def test_real_fragmented_recording_is_recognised_with_the_length_ffmpeg_reports(
    name: str, document_format: DocumentFormat
) -> None:
    inspected = probe(fixture(name))

    assert inspected.document_format is document_format
    assert inspected.duration_seconds == pytest.approx(2.064)


def test_duration_of_fragments_comes_from_the_fragment_header_default() -> None:
    content = fragmented_movie(fragment(frame_run(100)), fragment(frame_run(50), sequence=2))

    assert probe(content).duration_seconds == pytest.approx(frames(150))


def test_track_extends_default_is_used_when_the_fragment_header_has_none() -> None:
    content = fragmented_movie(
        fragment(frame_run(100), header=fragment_header(default_duration=None)),
        extends=movie_extends(track_extends(default_duration=AAC_FRAME_SAMPLES)),
    )

    assert probe(content).duration_seconds == pytest.approx(frames(100))


def test_fragment_header_default_overrides_the_track_extends_default() -> None:
    content = fragmented_movie(
        fragment(frame_run(100)), extends=movie_extends(track_extends(default_duration=1))
    )

    assert probe(content).duration_seconds == pytest.approx(frames(100))


@pytest.mark.parametrize(
    "run",
    [
        TrackRun(count=3, durations=[SOUND_TIMESCALE] * 3),
        TrackRun(count=3, durations=[SOUND_TIMESCALE] * 3, sizes=False),
        TrackRun(count=3, durations=[SOUND_TIMESCALE] * 3, sample_flags=True, composition_offsets=True),
        TrackRun(count=3, durations=[SOUND_TIMESCALE] * 3, first_sample_flags=True),
    ],
    ids=["durations-and-sizes", "durations-only", "every-sample-field", "first-sample-flags"],
)
def test_per_sample_durations_are_summed_whatever_else_each_sample_carries(run: TrackRun) -> None:
    content = fragmented_movie(fragment(run.encode()))

    assert probe(content).duration_seconds == pytest.approx(3.0)


@pytest.mark.parametrize(
    "header",
    [
        fragment_header(base_data_offset=True),
        fragment_header(description_index=True),
        fragment_header(base_data_offset=True, description_index=True),
    ],
    ids=["base-data-offset", "description-index", "both"],
)
def test_optional_fragment_header_fields_are_skipped_before_the_default_duration(header: bytes) -> None:
    content = fragmented_movie(fragment(frame_run(100), header=header))

    assert probe(content).duration_seconds == pytest.approx(frames(100))


def test_several_runs_in_one_track_fragment_add_up() -> None:
    content = fragmented_movie(fragment(frame_run(40), frame_run(60)))

    assert probe(content).duration_seconds == pytest.approx(frames(100))


def test_samples_in_the_movie_and_in_fragments_add_up() -> None:
    content = fragmented_movie(
        fragment(frame_run(100)), tracks=[fragmented_track(seconds=frames(50))], header_seconds=0
    )

    assert probe(content).duration_seconds == pytest.approx(frames(150))


def test_fragments_hiding_hours_behind_a_short_movie_header_are_too_long() -> None:
    hour_of_frames = 3600 * SOUND_TIMESCALE // AAC_FRAME_SAMPLES
    content = fragmented_movie(
        *(fragment(frame_run(hour_of_frames // 60), sequence=index) for index in range(1, 121)),
        header_seconds=10,
    )

    cheaply(RecordingTooLongError, content, SHORT_CAP_SECONDS)


def test_fragment_walk_stops_as_soon_as_it_passes_the_cap() -> None:
    second = SOUND_TIMESCALE // AAC_FRAME_SAMPLES
    content = fragmented_movie(
        *(fragment(frame_run(second), sequence=index) for index in range(1, MAX_FRAGMENT_BOXES // 8))
    )

    cheaply(RecordingTooLongError, content, SHORT_CAP_SECONDS)


def test_movie_extends_header_longer_than_the_fragments_is_the_duration() -> None:
    content = fragmented_movie(
        fragment(frame_run(10)),
        extends=movie_extends(track_extends(), movie_extends_header(5000)),
    )

    assert probe(content).duration_seconds == pytest.approx(5.0)


def test_version_1_movie_extends_header_over_the_cap_is_too_long() -> None:
    content = fragmented_movie(
        fragment(frame_run(10)),
        extends=movie_extends(track_extends(), movie_extends_header(10 * 3600 * 1000, version=1)),
    )

    with pytest.raises(RecordingTooLongError):
        probe(content)


def test_video_fragments_are_walked_past_but_do_not_count_as_audio() -> None:
    content = fragmented_movie(
        movie_fragment(
            track_fragment(fragment_header(VIDEO_TRACK_ID), frame_run(1000)),
            track_fragment(fragment_header(), frame_run(100)),
        ),
        tracks=[fragmented_track(), fragmented_track(VIDEO_TRACK_ID, b"vide")],
        extends=movie_extends(track_extends(), track_extends(VIDEO_TRACK_ID)),
    )

    inspected = probe(content)

    assert inspected.document_format is DocumentFormat.MP4
    assert inspected.duration_seconds == pytest.approx(frames(100))


def test_fragmented_movie_with_only_video_fragments_has_no_audio() -> None:
    content = fragmented_movie(
        movie_fragment(track_fragment(fragment_header(VIDEO_TRACK_ID), frame_run(1000))),
        tracks=[fragmented_track(), fragmented_track(VIDEO_TRACK_ID, b"vide")],
    )

    unsupported(content)


def test_version_1_track_header_id_is_read() -> None:
    sound = Track(stts=full_box(b"stts", b"\x00" * 4), header=track_header(7, version=1)).encode()
    content = fragmented_movie(
        fragment(frame_run(100), header=fragment_header(7)),
        tracks=[sound],
        extends=movie_extends(track_extends(7)),
    )

    assert probe(content).duration_seconds == pytest.approx(frames(100))


def test_flood_of_fragments_is_rejected_at_the_fragment_box_budget() -> None:
    content = fragmented_movie(*([box(b"moof")] * (MAX_FRAGMENT_BOXES + 1)))
    larger = fragmented_movie(*([box(b"moof")] * (FLOOD_SCALE * MAX_FRAGMENT_BOXES)))

    at_budget, past_budget = fastest_rejections(content, larger)

    assert past_budget < at_budget * MAX_FLOOD_COST_GROWTH
    with peak_memory() as peak:
        unsupported(content)
    assert peak[0] < FRAGMENT_INDEX_MEMORY_BUDGET_BYTES


def test_track_runs_past_the_entry_budget_are_rejected_quickly() -> None:
    content = fragmented_movie(fragment(TrackRun(count=MAX_TIME_TO_SAMPLE_ENTRIES + 1, sizes=False).encode()))

    cheaply(UnsupportedDocumentError, content)


def test_flood_of_free_boxes_still_spends_the_movie_box_budget_not_the_fragment_one() -> None:
    content = fragmented_movie(*([box(b"free")] * 4097))

    unsupported(content)


@pytest.mark.parametrize(
    "content",
    [
        isobmff(movie(track()), fragment(frame_run(10))),
        fragmented_movie(fragment(frame_run(10), header=fragment_header(9))),
        fragmented_movie(movie_fragment(track_fragment(b"", frame_run(10)))),
        fragmented_movie(movie_fragment(track_fragment(fragment_header() * 2, frame_run(10)))),
        fragmented_movie(
            fragment(frame_run(10), header=fragment_header(default_duration=None)),
            extends=movie_extends(track_extends(9)),
        ),
        fragmented_movie(fragment(TrackRun(count=10, declared=11).encode())),
        fragmented_movie(fragment(TrackRun(count=10, durations=[1] * 10, declared=11).encode())),
        fragmented_movie(fragment(full_box(b"trun", b"\x00\x00"))),
        fragmented_movie(fragment(frame_run(10), header=full_box(b"tfhd", b"\x00\x00"))),
        fragmented_movie(
            fragment(frame_run(10), header=box(b"tfhd", b"\x00\x00\x00\x08", b"\x00\x00\x00\x01"))
        ),
        fragmented_movie(fragment(frame_run(10)), extends=movie_extends(track_extends(), track_extends())),
        fragmented_movie(fragment(frame_run(10)), extends=movie_extends(full_box(b"trex", b"\x00" * 4))),
        fragmented_movie(fragment(frame_run(10)), extends=movie_extends(full_box(b"mehd", b""))),
        isobmff(
            movie(fragmented_track(), movie_extends(), movie_extends()),
            fragment(frame_run(10)),
        ),
        fragmented_movie(fragment(frame_run(10)), tracks=[fragmented_track(), fragmented_track()]),
        fragmented_movie(fragment(frame_run(10)), tracks=[track()]),
        fragmented_movie(fragment(frame_run(10)), tracks=[Track(header=full_box(b"tkhd", b"")).encode()]),
        fragmented_movie(
            fragment(frame_run(10)),
            tracks=[
                Track(
                    header=track_header(), entry_format=b"enca", stts=full_box(b"stts", b"\x00" * 4)
                ).encode()
            ],
        ),
    ],
    ids=[
        "fragments-without-movie-extends",
        "fragment-of-an-undeclared-track",
        "track-fragment-without-header",
        "two-fragment-headers",
        "samples-without-any-duration",
        "run-declaring-more-samples-than-it-holds",
        "run-with-durations-declaring-more-samples-than-it-holds",
        "short-track-run",
        "short-fragment-header",
        "default-duration-flag-without-the-field",
        "two-track-extends-for-one-track",
        "short-track-extends",
        "short-movie-extends-header",
        "two-movie-extends",
        "two-tracks-with-one-id",
        "track-without-a-track-header",
        "short-track-header",
        "encrypted-fragmented-audio",
    ],
)
def test_malformed_fragmented_movie_is_rejected(content: bytes) -> None:
    unsupported(content)


def test_fragmented_movie_with_no_fragments_and_no_samples_has_no_audio() -> None:
    unsupported(fragmented_movie())


def test_unfragmented_movie_with_movie_extends_but_samples_in_the_movie_is_read() -> None:
    content = fragmented_movie(tracks=[fragmented_track(seconds=frames(100))], header_seconds=frames(100))

    assert probe(content).duration_seconds == pytest.approx(frames(100))


def test_fragmented_movie_exactly_at_the_cap_is_accepted_and_one_sample_more_is_too_long() -> None:
    ticks_per_sample = 1000
    cap_samples = SHORT_CAP_SECONDS * SOUND_TIMESCALE // ticks_per_sample
    header = fragment_header(default_duration=ticks_per_sample)
    exact = fragmented_movie(fragment(TrackRun(count=cap_samples, sizes=False).encode(), header=header))
    over = fragmented_movie(fragment(TrackRun(count=cap_samples + 1, sizes=False).encode(), header=header))

    assert probe(exact, SHORT_CAP_SECONDS).duration_seconds == SHORT_CAP_SECONDS
    with pytest.raises(RecordingTooLongError):
        probe(over, SHORT_CAP_SECONDS)


def test_real_fragmented_recording_with_its_sample_description_replaced_by_encryption_is_rejected() -> None:
    content = fixture("tone-fragmented.m4a").replace(b"mp4a", b"enca", 1)

    unsupported(content)


def test_aac_fragments_timed_one_tick_per_packet_cannot_hide_their_length() -> None:
    content = fragmented_movie(fragment(frame_run(20_000), header=fragment_header(default_duration=1)))

    with pytest.raises(RecordingTooLongError):
        probe(content)


def tfhd_defaults_of_one(real: bytes) -> bytearray:
    patched = bytearray(real)
    position = real.find(b"tfhd")
    while position != -1:
        patched[position + 12 : position + 16] = (1).to_bytes(4, "big")
        position = real.find(b"tfhd", position + 4)
    for header in (b"mvhd", b"mdhd"):
        position = real.find(header)
        patched[position + 20 : position + 24] = (1).to_bytes(4, "big")
    return patched


def lying_real_recording(*, entry_rate: int | None = None, entry_format: bytes | None = None) -> bytes:
    real = fixture("tone-fragmented.m4a")
    patched = tfhd_defaults_of_one(real)
    entry = real.find(b"stsd") + 12
    if entry_rate is not None:
        patched[entry + 32 : entry + 34] = entry_rate.to_bytes(2, "big")
    if entry_format is not None:
        patched[entry + 4 : entry + 8] = entry_format
    return bytes(patched)


FFMPEG_DECODED_SECONDS = 2.112


@pytest.mark.parametrize(
    "content",
    [
        lying_real_recording(),
        lying_real_recording(entry_rate=65535),
        lying_real_recording(entry_format=b"alac"),
    ],
    ids=["one-tick-timing", "and-a-lying-sample-entry-rate", "and-a-relabelled-sample-entry"],
)
def test_real_recording_timed_one_tick_per_packet_still_probes_near_what_ffmpeg_decodes(
    content: bytes,
) -> None:
    duration = probe(content).duration_seconds

    assert duration is not None
    assert duration >= SHORTEST_AAC_FRAME / AAC_FRAME_SAMPLES * FFMPEG_DECODED_SECONDS - 0.01


def test_fragmented_raw_pcm_is_rejected_because_only_aac_fragments_can_be_floored() -> None:
    sound = Track(
        entry_format=b"sowt", media_duration=0, stts=full_box(b"stts", b"\x00" * 4), header=track_header()
    ).encode()
    content = fragmented_movie(
        fragment(
            TrackRun(count=SOUND_TIMESCALE, sizes=False).encode(), header=fragment_header(default_duration=1)
        ),
        tracks=[sound],
    )

    unsupported(content)


@pytest.mark.parametrize(
    "children",
    [
        b"",
        decoder_configuration(audio_specific_config(frequency_index=13)),
        decoder_configuration(
            audio_specific_config(frequency_index=15, explicit_rate=MAX_AAC_SAMPLE_RATE + 1)
        ),
        decoder_configuration(
            audio_specific_config(frequency_index=15, explicit_rate=MIN_AAC_SAMPLE_RATE - 1)
        ),
        decoder_configuration(b"\x10"),
        decoder_configuration(raw=b"\x03\x05\x00\x01\x00\x04\x01"),
        decoder_configuration(raw=b"\x04\x01\x00"),
        decoder_configuration(raw=b"\x03\x80\x80\x80\x80\x01\x00"),
        decoder_configuration(raw=b"\x03\x7f\x00"),
        decoder_configuration(raw=b"\x03\x03\x00\x01\x00"),
        decoder_configuration() + decoder_configuration(),
        box(b"wave", decoder_configuration()) + decoder_configuration(),
        b"\x00\x00\x00",
    ],
    ids=[
        "no-decoder-configuration",
        "reserved-frequency-index",
        "explicit-rate-too-high",
        "explicit-rate-too-low",
        "truncated-specific-config",
        "descriptor-past-its-parent",
        "wrong-first-descriptor",
        "size-over-four-bytes",
        "size-past-the-box",
        "no-decoder-config-descriptor",
        "two-configurations",
        "one-in-wave-and-one-outside",
        "garbage-after-the-fixed-fields",
    ],
)
def test_aac_track_whose_decoder_configuration_cannot_be_trusted_is_rejected(children: bytes) -> None:
    sound = Track(
        stts=full_box(b"stts", b"\x00" * 4), header=track_header(), entry=sound_entry(children=children)
    ).encode()

    unsupported(fragmented_movie(fragment(frame_run(10)), tracks=[sound]))


def test_aac_rate_comes_from_the_decoder_configuration_not_from_the_sample_entry() -> None:
    entry = sound_entry(children=decoder_configuration(audio_specific_config(frequency_index=11)))
    sound = Track(stts=full_box(b"stts", b"\x00" * 4), header=track_header(), entry=entry).encode()
    content = fragmented_movie(
        fragment(frame_run(100), header=fragment_header(default_duration=1)), tracks=[sound]
    )

    assert probe(content).duration_seconds == pytest.approx(100 * SHORTEST_AAC_FRAME / 8000)


@pytest.mark.parametrize("version", [1, 2])
def test_quicktime_sound_descriptions_are_read_past_their_longer_fixed_fields(version: int) -> None:
    entry = sound_entry(
        version=version, children=box(b"wave", box(b"frma", b"mp4a"), decoder_configuration())
    )
    sound = Track(stts=full_box(b"stts", b"\x00" * 4), header=track_header(), entry=entry).encode()
    content = fragmented_movie(
        fragment(frame_run(100), header=fragment_header(default_duration=1)), tracks=[sound]
    )

    assert probe(content).duration_seconds == pytest.approx(100 * SHORTEST_AAC_FRAME / SOUND_TIMESCALE)


def test_unknown_sound_description_version_is_rejected() -> None:
    sound = Track(
        stts=full_box(b"stts", b"\x00" * 4), header=track_header(), entry=sound_entry(version=3)
    ).encode()

    unsupported(fragmented_movie(fragment(frame_run(10)), tracks=[sound]))


@pytest.mark.parametrize("object_type", [23, 39])
def test_low_delay_aac_is_floored_at_its_shorter_frame(object_type: int) -> None:
    entry = sound_entry(children=decoder_configuration(audio_specific_config(object_type=object_type)))
    sound = Track(stts=full_box(b"stts", b"\x00" * 4), header=track_header(), entry=entry).encode()
    content = fragmented_movie(
        fragment(frame_run(100), header=fragment_header(default_duration=1)), tracks=[sound]
    )

    assert probe(content).duration_seconds == pytest.approx(100 * 480 / SOUND_TIMESCALE)


def test_fragmented_mp3_in_mp4_is_rejected_because_its_length_cannot_be_floored() -> None:
    entry = sound_entry(children=decoder_configuration(object_type_indication=MPEG1_AUDIO))
    sound = Track(stts=full_box(b"stts", b"\x00" * 4), header=track_header(), entry=entry).encode()

    unsupported(fragmented_movie(fragment(frame_run(10)), tracks=[sound]))


def test_unfragmented_mp3_in_mp4_is_still_accepted_without_a_floor() -> None:
    entry = sound_entry(children=decoder_configuration(object_type_indication=MPEG1_AUDIO))
    content = isobmff(movie(Track(entry=entry).encode()))

    assert probe(content).duration_seconds == 2.0


def test_unfragmented_aac_timed_one_tick_per_packet_is_floored_too() -> None:
    content = isobmff(movie(track(media_duration=1, stts=time_to_sample([(20_000, 1)])), seconds=0.001))

    with pytest.raises(RecordingTooLongError):
        probe(content)

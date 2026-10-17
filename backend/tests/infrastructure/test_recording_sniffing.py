import os
import struct
import time

import pytest

from deckly.application.ports import InspectedUpload
from deckly.domain.document import DocumentFormat
from deckly.domain.exceptions import RecordingTooLongError, UnsupportedDocumentError
from deckly.infrastructure.documents.sniffing import detect_format
from deckly.infrastructure.recordings.binary import BIG_ENDIAN, little_endian, unsigned
from deckly.infrastructure.recordings.isobmff import MAX_BOXES, MAX_TIME_TO_SAMPLE_ENTRIES, MAX_TRACKS
from deckly.infrastructure.recordings.mpeg_audio import MAX_LEADING_PADDING_BYTES, MAX_TRAILING_TAG_BYTES
from deckly.infrastructure.recordings.sniffing import ContentRecordingInspector, detect_recording
from deckly.infrastructure.recordings.wave import MAX_CHUNKS
from tests.document_files import MIB
from tests.documents import pdf_bytes, pptx_bytes
from tests.memory import peak_memory
from tests.recording_files import (
    CD_FRAME_SECONDS,
    EXTENSIBLE,
    IEEE_FLOAT,
    LAYER_II,
    MPEG_2,
    SLOW_FRAME_SECONDS,
    SOUND_TIMESCALE,
    UNKNOWN_DURATION_32,
    MpegFrame,
    Track,
    WaveFormat,
    ape_tag,
    audio_mp4,
    box,
    cd_frame,
    chunk,
    extensible_format,
    fixture,
    frames_for,
    full_box,
    handler,
    id3v1,
    id3v2,
    isobmff,
    large_box,
    media_header,
    movie,
    movie_header,
    mp3,
    pcm_wave,
    sample_description,
    sample_sizes,
    sampled_count,
    shortest_frame,
    slow_frame,
    time_to_sample,
    track,
    uniform_time_to_sample,
    wave,
)

pytestmark = pytest.mark.anyio

CAP_SECONDS = 900
SHORT_CAP_SECONDS = 60
TEN_HOURS_SECONDS = 10 * 60 * 60
PROBE_SECONDS_BUDGET = 0.25
PROBE_MEMORY_BUDGET_BYTES = MIB


def probe(content: bytes, cap: int = CAP_SECONDS) -> InspectedUpload:
    return detect_recording(content, cap)


def unsupported(content: bytes, cap: int = CAP_SECONDS) -> None:
    with pytest.raises(UnsupportedDocumentError):
        probe(content, cap)


def too_long(content: bytes, cap: int = CAP_SECONDS) -> None:
    with pytest.raises(RecordingTooLongError):
        probe(content, cap)


def cheaply_too_long(content: bytes, cap: int = CAP_SECONDS) -> None:
    started = time.perf_counter()
    with peak_memory() as peak:
        too_long(content, cap)
    assert time.perf_counter() - started < PROBE_SECONDS_BUDGET
    assert peak[0] < PROBE_MEMORY_BUDGET_BYTES


@pytest.mark.parametrize(
    ("name", "document_format", "seconds"),
    [
        ("tone.mp3", DocumentFormat.MP3, 2.0),
        ("tone.m4a", DocumentFormat.M4A, 2.0),
        ("apple.m4a", DocumentFormat.M4A, 2.0),
        ("tone.wav", DocumentFormat.WAV, 1.0),
        ("clip.mp4", DocumentFormat.MP4, 2.0),
        ("clip.mov", DocumentFormat.MOV, 2.0),
        ("apple.mov", DocumentFormat.MOV, 2.0),
    ],
)
def test_real_recording_is_recognised_with_its_duration(
    name: str, document_format: DocumentFormat, seconds: float
) -> None:
    inspected = probe(fixture(name))

    assert inspected.document_format is document_format
    assert inspected.duration_seconds is not None
    assert seconds <= inspected.duration_seconds < seconds + 0.5


def test_real_video_without_an_audio_track_is_rejected() -> None:
    unsupported(fixture("silent.mp4"))


def test_real_heic_image_in_an_mp4_family_container_is_rejected() -> None:
    unsupported(fixture("red.heic"))


@pytest.mark.parametrize(
    "name", ["tone.mp3", "tone.m4a", "apple.m4a", "tone.wav", "clip.mp4", "clip.mov", "apple.mov"]
)
def test_real_recording_over_the_cap_is_too_long(name: str) -> None:
    too_long(fixture(name), cap=0)


@pytest.mark.parametrize(
    "content",
    [
        pdf_bytes(),
        pptx_bytes(),
        b"",
        b"ID",
        b"plain text named recording.mp3\n",
        b"RIFF\x00\x00\x00\x00AVI LIST",
        b"OggS\x00\x02" + b"\x00" * 64,
        b"\x1aE\xdf\xa3" + b"\x00" * 64,
        b"fLaC\x00\x00\x00\x22" + b"\x00" * 64,
        b"#!AMR\n" + b"\x00" * 64,
    ],
    ids=["pdf", "pptx", "empty", "two-bytes", "text", "avi", "ogg", "webm", "flac", "amr"],
)
def test_file_that_is_no_supported_recording_is_rejected_whatever_its_name(content: bytes) -> None:
    unsupported(content)


@pytest.mark.parametrize(
    "name", ["tone.mp3", "tone.m4a", "apple.m4a", "tone.wav", "clip.mp4", "clip.mov", "apple.mov"]
)
def test_recording_uploaded_as_a_document_is_rejected_by_the_document_sniffer(name: str) -> None:
    with pytest.raises(UnsupportedDocumentError):
        detect_format(fixture(name))


async def test_inspector_probes_with_its_configured_cap() -> None:
    inspector = ContentRecordingInspector(max_duration_seconds=1)

    with pytest.raises(RecordingTooLongError):
        await inspector.identify(fixture("tone.m4a"))
    assert await ContentRecordingInspector(max_duration_seconds=CAP_SECONDS).identify(
        fixture("tone.wav")
    ) == InspectedUpload(DocumentFormat.WAV, 1.0)


def test_audio_payload_is_never_decoded_so_random_samples_probe_the_same() -> None:
    original = fixture("tone.m4a")
    start = original.index(b"mdat") + 4
    end = start + 4000
    scrambled = original[:start] + os.urandom(end - start) + original[end:]

    assert probe(scrambled) == probe(original)


def test_audio_mp4_built_in_code_is_recognised_as_m4a() -> None:
    assert probe(audio_mp4(seconds=2)) == InspectedUpload(DocumentFormat.M4A, 2.0)


def test_mp4_brand_without_a_video_track_is_audio() -> None:
    assert probe(audio_mp4(brand=b"mp42")).document_format is DocumentFormat.M4A


def test_quicktime_brand_without_a_video_track_is_still_quicktime() -> None:
    content = isobmff(movie(track()), box(b"mdat"), brand=b"qt  ", compatible=(b"qt  ",))

    assert probe(content).document_format is DocumentFormat.MOV


def test_unknown_major_brand_with_a_compatible_mp4_brand_is_accepted() -> None:
    content = isobmff(movie(track()), box(b"mdat"), brand=b"XAVC", compatible=(b"XAVC", b"mp42"))

    assert probe(content).document_format is DocumentFormat.M4A


@pytest.mark.parametrize(
    ("brand", "compatible"),
    [
        (b"M4P ", (b"M4A ", b"mp42", b"isom")),
        (b"heic", (b"mif1", b"heic")),
        (b"avif", (b"avif", b"mif1", b"miaf")),
        (b"abcd", (b"efgh",)),
        (b"3gp4", (b"3gp4",)),
    ],
    ids=["fairplay", "heic", "avif", "unknown", "3gp"],
)
def test_brand_that_is_no_accepted_recording_is_rejected(brand: bytes, compatible: tuple[bytes, ...]) -> None:
    unsupported(isobmff(movie(track()), box(b"mdat"), brand=brand, compatible=compatible))


def test_movie_header_claiming_ten_hours_in_a_tiny_file_is_too_long_without_reading_samples() -> None:
    content = isobmff(movie(track(), header=movie_header(TEN_HOURS_SECONDS * 1000)))

    assert len(content) < 1024
    cheaply_too_long(content)


def test_version_1_movie_header_with_a_64_bit_duration_is_read() -> None:
    header = movie_header((TEN_HOURS_SECONDS * 1000) << 1, version=1)

    too_long(isobmff(movie(track(), header=header)))


def test_media_header_longer_than_the_cap_is_too_long_even_when_the_movie_header_is_short() -> None:
    too_long(isobmff(movie(track(media_duration=(CAP_SECONDS + 1) * SOUND_TIMESCALE), seconds=2)))


def test_sample_table_that_plays_for_hours_behind_short_headers_is_too_long() -> None:
    lying = track(media_duration=2 * SOUND_TIMESCALE, stts=time_to_sample(frames_for(TEN_HOURS_SECONDS)))

    cheaply_too_long(isobmff(movie(lying, seconds=2)))


def test_sample_table_spread_over_many_entries_stops_as_soon_as_it_passes_the_cap() -> None:
    lying = track(media_duration=1, stts=uniform_time_to_sample(MAX_TIME_TO_SAMPLE_ENTRIES, 1024))

    too_long(isobmff(movie(lying, seconds=1)))


def test_unknown_movie_duration_falls_back_to_the_audio_track() -> None:
    header = movie_header(UNKNOWN_DURATION_32)

    assert probe(isobmff(movie(track(seconds=3), header=header))).duration_seconds == pytest.approx(3.008)


def test_duration_is_the_longest_of_the_headers_and_the_sample_table() -> None:
    content = isobmff(movie(track(seconds=2, media_duration=5 * SOUND_TIMESCALE), seconds=7))

    assert probe(content).duration_seconds == 7.0


def test_sample_table_declaring_more_entries_than_it_holds_is_rejected() -> None:
    unsupported(isobmff(movie(track(stts=time_to_sample(frames_for(2), declared=1_000_000)))))


def test_sample_table_with_more_entries_than_the_budget_is_rejected_quickly() -> None:
    content = isobmff(movie(track(stts=uniform_time_to_sample(MAX_TIME_TO_SAMPLE_ENTRIES + 1, 0))))

    started = time.perf_counter()
    unsupported(content)
    assert time.perf_counter() - started < PROBE_SECONDS_BUDGET


def test_entry_budget_is_shared_by_every_track() -> None:
    half = uniform_time_to_sample(MAX_TIME_TO_SAMPLE_ENTRIES // 2 + 1, 0)
    tracks = [
        Track(entry_format=b"sowt", stts=half).encode(),
        Track(entry_format=b"sowt", stts=half).encode(),
    ]

    unsupported(isobmff(movie(*tracks)))


def test_flood_of_tiny_boxes_is_rejected_at_the_box_budget() -> None:
    content = isobmff(box(b"free") * (MAX_BOXES + 1), movie(track()))

    started = time.perf_counter()
    unsupported(content)
    assert time.perf_counter() - started < PROBE_SECONDS_BUDGET


def test_more_tracks_than_the_limit_are_rejected() -> None:
    unsupported(isobmff(movie(*([track(b"vide")] * (MAX_TRACKS + 1)), track())))


@pytest.mark.parametrize(
    "content",
    [
        isobmff(movie(track()), struct.pack(">I", 4096) + b"mdat" + b"\x00" * 64),
        isobmff(movie(track()), struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 1 << 40)),
        isobmff(movie(track()), struct.pack(">I", 1) + b"mda"),
        isobmff(movie(track()), struct.pack(">I", 1) + b"mdat" + b"\x00" * 4),
        isobmff(movie(track()), struct.pack(">I", 4) + b"mdat"),
        isobmff(movie(track()), b"\x00\x00\x00"),
        isobmff(movie(track()), struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 8)),
    ],
    ids=[
        "past-eof",
        "large-past-eof",
        "large-header-cut",
        "large-size-cut",
        "smaller-than-header",
        "cut-header",
        "large-small",
    ],
)
def test_box_that_does_not_fit_its_container_is_rejected(content: bytes) -> None:
    unsupported(content)


def test_large_size_box_is_walked_like_any_other() -> None:
    content = isobmff(movie(track()), large_box(b"mdat", b"\x00" * 64))

    assert probe(content).duration_seconds == 2.0


def test_box_that_runs_to_the_end_of_the_file_is_walked() -> None:
    content = isobmff(movie(track()), struct.pack(">I", 0) + b"mdat" + b"\x00" * 64)

    assert probe(content).document_format is DocumentFormat.M4A


def test_movie_box_after_the_media_data_is_found() -> None:
    content = isobmff(box(b"mdat", b"\x00" * 64), movie(track()))

    assert probe(content).duration_seconds == 2.0


@pytest.mark.parametrize(
    "content",
    [
        isobmff(box(b"mdat")),
        isobmff(movie(track()), movie(track())),
        isobmff(movie(track()), box(b"moof"), box(b"mdat")),
        isobmff(movie(track(), box(b"mvex", full_box(b"mehd", b"\x00" * 4)))),
        isobmff(movie(track(), header=b"")),
        isobmff(movie(track(), header=full_box(b"mvhd", b"\x00" * 8))),
        isobmff(movie(track(), header=movie_header(1, timescale=0))),
        isobmff(movie(Track(entry_format=b"enca").encode())),
        isobmff(movie(Track(entry_format=b"drms").encode())),
        isobmff(movie(track(stts=time_to_sample([])))),
        isobmff(movie(track(stts=full_box(b"stts", b"")))),
        isobmff(movie(box(b"trak", box(b"mdia", full_box(b"hdlr", b"\x00" * 4 + b"so"))))),
        isobmff(movie(box(b"trak", box(b"tkhd")))),
        isobmff(movie(track(b"vide"))),
        isobmff(),
        box(b"ftyp", b"isom"),
    ],
    ids=[
        "no-movie",
        "two-movies",
        "fragments",
        "movie-extends",
        "no-movie-header",
        "short-movie-header",
        "zero-timescale",
        "encrypted",
        "fairplay-sample",
        "empty-sample-table",
        "short-sample-table",
        "short-handler",
        "no-media",
        "video-only",
        "file-type-only",
        "short-file-type",
    ],
)
def test_malformed_or_unsupported_movie_is_rejected(content: bytes) -> None:
    unsupported(content)


def test_box_before_the_file_type_box_is_not_an_mp4() -> None:
    unsupported(box(b"free") + isobmff(movie(track())))


def test_frame_count_header_that_understates_the_stream_is_ignored() -> None:
    xing = cd_frame(body=b"\x00" * 32 + b"Xing" + struct.pack(">II", 0x1, 4))
    content = mp3(frames=2000, frame=cd_frame())
    lying = id3v2() + xing + content[len(id3v2()) :]

    assert probe(lying).duration_seconds == pytest.approx(2001 * CD_FRAME_SECONDS)


def test_mp3_without_an_id3_tag_is_recognised() -> None:
    inspected = probe(cd_frame() * 40)

    assert inspected.document_format is DocumentFormat.MP3
    assert inspected.duration_seconds == pytest.approx(40 * CD_FRAME_SECONDS)


@pytest.mark.parametrize(
    "tail",
    [
        id3v1(),
        ape_tag(),
        ape_tag() + id3v1(),
        id3v2(),
        id3v2(footer=True),
        b"\x00" * 4096,
        b"\xff\xfb",
        cd_frame()[:200],
        ape_tag() * 4,
    ],
    ids=[
        "id3v1",
        "ape",
        "ape-id3v1",
        "id3v2",
        "id3v2-footer",
        "zero-padding",
        "cut-header",
        "cut-frame",
        "four-tags",
    ],
)
def test_mp3_ending_in_a_known_tag_padding_or_a_truncated_frame_is_accepted(tail: bytes) -> None:
    assert probe(mp3(frames=40) + tail).document_format is DocumentFormat.MP3


def test_zero_padding_after_the_id3_tag_is_skipped() -> None:
    content = id3v2() + b"\x00" * MAX_LEADING_PADDING_BYTES + cd_frame() * 40

    assert probe(content).document_format is DocumentFormat.MP3


def test_zero_padding_past_the_window_is_not_skipped() -> None:
    unsupported(id3v2() + b"\x00" * (MAX_LEADING_PADDING_BYTES + 1) + cd_frame() * 40)


def test_up_to_four_id3_tags_in_a_row_are_skipped() -> None:
    assert probe(id3v2() * 4 + cd_frame() * 40).document_format is DocumentFormat.MP3


def test_a_fifth_leading_id3_tag_is_not_skipped() -> None:
    unsupported(id3v2() * 5 + cd_frame() * 40)


@pytest.mark.parametrize(("offset", "size"), [(-1, 4), (0, 5), (2, 4)])
def test_reading_past_either_end_of_a_header_is_refused_not_silently_short(offset: int, size: int) -> None:
    with pytest.raises(UnsupportedDocumentError):
        unsigned(b"\x00\x01\x02\x03", offset, size, BIG_ENDIAN)


def test_reading_inside_a_header_decodes_the_requested_byte_order() -> None:
    assert unsigned(b"\x00\x01\x02\x03", 0, 2, BIG_ENDIAN) == 0x0001
    assert little_endian(b"\x00\x01\x02\x03", 2, 2) == 0x0302


@pytest.mark.parametrize(
    "tail",
    [
        b"junk" + cd_frame() * 4000,
        b"\x01" * 64,
        b"TAG" + b"\x00" * 200,
        b"TAG" + b"\x00" * 20,
        ape_tag(items=b"", header=False),
        ape_tag()[:20],
        b"APETAGEX" + struct.pack("<IIII", 2000, 1 << 20, 1, 1 << 29) + b"\x00" * 8,
        id3v2()[:6],
        (ape_tag() + ape_tag() + ape_tag() + ape_tag() + ape_tag()),
    ],
    ids=[
        "frames-behind-junk",
        "junk",
        "long-id3v1",
        "short-id3v1",
        "ape-footer-first",
        "cut-ape",
        "ape-past-eof",
        "cut-id3v2",
        "too-many-tags",
    ],
)
def test_mp3_with_unaccounted_bytes_after_its_last_frame_is_rejected(tail: bytes) -> None:
    unsupported(mp3(frames=40) + tail)


@pytest.mark.parametrize(
    "content",
    [
        mp3(frames=1),
        id3v2(),
        id3v2() + b"junk" * 100,
        b"ID3\x05\x00\x00" + b"\x00" * 64,
        b"ID3\x04\x00\x00\x00\x00\x80\x00" + b"\x00" * 64,
        b"ID3\x04\x00\x00\x00\x00\x7f\x7f" + b"\x00" * 64,
        b"ID3\x04\x00",
        mp3(frame=MpegFrame(layer=LAYER_II, length=417).encode()),
        mp3(frame=MpegFrame(bitrate_index=0, length=417).encode()),
        mp3(frame=MpegFrame(bitrate_index=15, length=417).encode()),
        mp3(frame=MpegFrame(rate_index=3, length=417).encode()),
        mp3(frame=MpegFrame(version=0b01, length=417).encode()),
        b"\xff\xf1\x50\x80" + b"\x00" * 400,
        cd_frame() * 20 + MpegFrame(version=MPEG_2, length=208).encode() * 20,
        cd_frame() * 20 + MpegFrame(rate_index=1, length=384).encode() * 20,
    ],
    ids=[
        "single-frame",
        "tag-only",
        "tag-then-junk",
        "id3-version-5",
        "id3-not-syncsafe",
        "id3-past-eof",
        "id3-cut",
        "layer-ii",
        "free-bitrate",
        "bad-bitrate",
        "reserved-rate",
        "reserved-version",
        "adts-aac",
        "version-change",
        "rate-change",
    ],
)
def test_malformed_or_unsupported_mpeg_audio_is_rejected(content: bytes) -> None:
    unsupported(content)


def test_low_bitrate_mp3_over_the_cap_is_too_long() -> None:
    frames = round((SHORT_CAP_SECONDS + 1) / SLOW_FRAME_SECONDS)

    too_long(id3v2() + slow_frame() * frames, cap=SHORT_CAP_SECONDS)


def test_low_bitrate_mp3_just_within_the_cap_is_accepted() -> None:
    frames = int(SHORT_CAP_SECONDS / SLOW_FRAME_SECONDS)

    assert probe(id3v2() + slow_frame() * frames, cap=SHORT_CAP_SECONDS).duration_seconds == pytest.approx(
        frames * SLOW_FRAME_SECONDS
    )


def test_mp3_walk_stops_at_the_cap_whatever_the_size_of_the_file() -> None:
    content = shortest_frame() * (48 * MIB // len(shortest_frame()))

    started = time.perf_counter()
    too_long(content)
    assert time.perf_counter() - started < PROBE_SECONDS_BUDGET


def test_wave_in_extensible_format_is_recognised() -> None:
    content = wave(chunk(b"fmt ", extensible_format()), chunk(b"data", b"\x00\x01" * 8000))

    assert probe(content) == InspectedUpload(DocumentFormat.WAV, 1.0)


def test_floating_point_wave_is_recognised() -> None:
    content = wave(
        chunk(b"fmt ", WaveFormat(tag=IEEE_FLOAT, bits=32).encode()), chunk(b"data", b"\x00" * 32000)
    )

    assert probe(content).duration_seconds == 1.0


def test_wave_with_chunks_before_its_audio_and_odd_padding_is_recognised() -> None:
    content = wave(
        chunk(b"fmt ", WaveFormat().encode()),
        chunk(b"LIST", b"INFOISFT\x03\x00\x00\x00ab\x00"),
        chunk(b"bext", b"\x00" * 3),
        chunk(b"data", b"\x00\x01" * 8000),
    )

    assert probe(content).duration_seconds == 1.0


def test_wave_whose_data_size_understates_the_audio_counts_every_byte_after_it() -> None:
    content = pcm_wave(seconds=SHORT_CAP_SECONDS + 1, declared=16000)

    too_long(content, cap=SHORT_CAP_SECONDS)


def test_wave_with_a_streaming_data_size_counts_what_is_there() -> None:
    assert probe(pcm_wave(seconds=2, declared=0xFFFF_FFFF)).duration_seconds == 2.0


def test_long_wave_header_claim_cannot_exceed_what_the_file_holds() -> None:
    assert probe(pcm_wave(seconds=1, declared=1 << 31)).duration_seconds == 1.0


def test_wave_just_within_the_cap_is_accepted() -> None:
    assert probe(pcm_wave(seconds=SHORT_CAP_SECONDS), cap=SHORT_CAP_SECONDS).duration_seconds == 60.0


@pytest.mark.parametrize(
    "content",
    [
        wave(chunk(b"data", b"\x00" * 16), chunk(b"fmt ", WaveFormat().encode())),
        wave(chunk(b"fmt ", WaveFormat().encode())),
        wave(chunk(b"fmt ", WaveFormat().encode()), chunk(b"data", b"")),
        wave(chunk(b"fmt ", WaveFormat(tag=0x55).encode()), chunk(b"data", b"\x00" * 16)),
        wave(chunk(b"fmt ", WaveFormat(tag=0x0011, bits=4).encode()), chunk(b"data", b"\x00" * 16)),
        wave(chunk(b"fmt ", WaveFormat(bits=12).encode()), chunk(b"data", b"\x00" * 16)),
        wave(chunk(b"fmt ", WaveFormat(tag=IEEE_FLOAT, bits=16).encode()), chunk(b"data", b"\x00" * 16)),
        wave(chunk(b"fmt ", WaveFormat(channels=0).encode()), chunk(b"data", b"\x00" * 16)),
        wave(chunk(b"fmt ", WaveFormat(channels=9).encode()), chunk(b"data", b"\x00" * 16)),
        wave(chunk(b"fmt ", WaveFormat(sample_rate=100).encode()), chunk(b"data", b"\x00" * 16)),
        wave(chunk(b"fmt ", WaveFormat(sample_rate=768_000).encode()), chunk(b"data", b"\x00" * 16)),
        wave(chunk(b"fmt ", WaveFormat(block_align=3).encode()), chunk(b"data", b"\x00" * 16)),
        wave(chunk(b"fmt ", WaveFormat(byte_rate=1).encode()), chunk(b"data", b"\x00" * 16)),
        wave(chunk(b"fmt ", WaveFormat().encode()[:12]), chunk(b"data", b"\x00" * 16)),
        wave(chunk(b"fmt ", WaveFormat(tag=EXTENSIBLE).encode()), chunk(b"data", b"\x00" * 16)),
        wave(chunk(b"fmt ", extensible_format(guid_tail=b"\x00" * 14)), chunk(b"data", b"\x00" * 16)),
        wave(chunk(b"fmt ", extensible_format(subformat=0x55)), chunk(b"data", b"\x00" * 16)),
        wave(chunk(b"fmt ", WaveFormat().encode(), declared=1 << 20)),
        wave(
            *[chunk(b"JUNK", b"")] * MAX_CHUNKS,
            chunk(b"fmt ", WaveFormat().encode()),
            chunk(b"data", b"\x00" * 16),
        ),
        wave(),
        b"RF64\xff\xff\xff\xffWAVEds64" + b"\x00" * 64,
        b"RIFF\x00\x00\x00\x00WAV",
    ],
    ids=[
        "data-before-format",
        "no-data",
        "empty-data",
        "mp3-in-wave",
        "adpcm",
        "odd-bit-depth",
        "half-float",
        "no-channels",
        "too-many-channels",
        "rate-too-low",
        "rate-too-high",
        "wrong-block-align",
        "wrong-byte-rate",
        "short-format",
        "short-extensible",
        "unknown-subformat-guid",
        "compressed-subformat",
        "chunk-past-eof",
        "chunk-flood",
        "header-only",
        "rf64",
        "cut-riff-header",
    ],
)
def test_malformed_or_unsupported_wave_is_rejected(content: bytes) -> None:
    unsupported(content)


def test_huge_wave_is_probed_without_copying_it() -> None:
    content = pcm_wave(seconds=1) + b"\x00" * (32 * MIB)

    with peak_memory() as peak:
        too_long(content)
    assert peak[0] < PROBE_MEMORY_BUDGET_BYTES


def test_sample_table_that_times_fewer_samples_than_the_file_holds_is_rejected() -> None:
    table = time_to_sample(frames_for(2))

    unsupported(isobmff(movie(Track(stts=table, sizes=sample_sizes(sampled_count(table) * 1000)).encode())))


def test_sample_table_that_times_more_samples_than_the_file_holds_is_rejected() -> None:
    table = time_to_sample(frames_for(2))

    unsupported(isobmff(movie(Track(stts=table, sizes=sample_sizes(sampled_count(table) - 1)).encode())))


def test_compact_sample_size_table_is_read_like_the_full_one() -> None:
    table = time_to_sample(frames_for(2))
    content = isobmff(movie(Track(stts=table, sizes=sample_sizes(sampled_count(table), b"stz2")).encode()))

    assert probe(content).duration_seconds == 2.0


@pytest.mark.parametrize(
    "sizes",
    [
        b"",
        sample_sizes(31) + sample_sizes(31, b"stz2"),
        sample_sizes(31) + sample_sizes(31),
        full_box(b"stsz", b"\x00" * 4),
    ],
    ids=["no-size-table", "both-size-tables", "two-size-tables", "short-size-table"],
)
def test_track_without_exactly_one_readable_sample_size_table_is_rejected(sizes: bytes) -> None:
    unsupported(isobmff(movie(Track(sizes=sizes).encode())))


@pytest.mark.parametrize(
    "track_box",
    [
        Track(extra_media=media_header(1)).encode(),
        Track(extra_media=handler(b"vide")).encode(),
        Track(extra_media=box(b"minf")).encode(),
        Track(extra_samples=time_to_sample(frames_for(TEN_HOURS_SECONDS))).encode(),
        Track(extra_samples=sample_description(b"enca")).encode(),
        box(b"trak", box(b"mdia"), Track().encode()[8:]),
    ],
    ids=[
        "two-media-headers",
        "two-handlers",
        "two-media-information",
        "two-time-tables",
        "two-descriptions",
        "two-media",
    ],
)
def test_track_with_a_duplicate_of_a_box_the_probe_reads_is_rejected(track_box: bytes) -> None:
    unsupported(isobmff(movie(track_box)))


def test_movie_with_two_movie_headers_is_rejected() -> None:
    unsupported(isobmff(movie(track(), movie_header(TEN_HOURS_SECONDS * 1000))))


@pytest.mark.parametrize("tag", ["ape", "id3v2"])
def test_mp3_frames_hidden_inside_a_large_trailing_tag_are_rejected(tag: str) -> None:
    hidden = cd_frame() * 2000
    tail = ape_tag(items=hidden) if tag == "ape" else id3v2(payload=hidden)

    unsupported(mp3(frames=40) + tail)


def test_trailing_tags_together_over_the_bound_are_rejected() -> None:
    half = b"\x00" * (MAX_TRAILING_TAG_BYTES // 2)

    unsupported(mp3(frames=40) + ape_tag(items=half) + id3v2(payload=half))


def test_trailing_tags_together_within_the_bound_are_accepted() -> None:
    items = b"\x00" * (MAX_TRAILING_TAG_BYTES - len(ape_tag(items=b"")))

    assert probe(mp3(frames=40) + ape_tag(items=items)).document_format is DocumentFormat.MP3


def test_wave_with_a_second_format_chunk_is_rejected() -> None:
    fast = WaveFormat(sample_rate=384_000, channels=8, bits=32).encode()

    unsupported(
        wave(
            chunk(b"fmt ", WaveFormat().encode()), chunk(b"fmt ", fast), chunk(b"data", b"\x00\x01" * 32_000)
        )
    )


SHORTEST_FRAMES_PER_24_SECONDS = 1000


def test_mp3_exactly_at_the_cap_is_accepted_and_one_frame_more_is_too_long() -> None:
    at_cap = shortest_frame() * SHORTEST_FRAMES_PER_24_SECONDS

    assert probe(at_cap, cap=24).duration_seconds == 24.0
    too_long(at_cap + shortest_frame(), cap=24)


def test_sample_table_exactly_at_the_cap_is_accepted_and_one_tick_more_is_too_long() -> None:
    def lying(extra: int) -> bytes:
        table = time_to_sample([(SHORT_CAP_SECONDS * SOUND_TIMESCALE + extra, 1)])
        return isobmff(movie(Track(entry_format=b"sowt", media_duration=1, stts=table).encode(), seconds=1))

    assert probe(lying(0), cap=SHORT_CAP_SECONDS).duration_seconds == SHORT_CAP_SECONDS
    too_long(lying(1), cap=SHORT_CAP_SECONDS)

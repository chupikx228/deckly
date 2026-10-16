import io
import struct
import zipfile
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache

from pypdf import PdfReader, PdfWriter

PRESENTATION_MAIN = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
SLIDE_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"
PRESENTATION = "http://schemas.openxmlformats.org/presentationml/2006/main"
RELATIONSHIPS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
MARKUP_COMPATIBILITY = "http://schemas.openxmlformats.org/markup-compatibility/2006"
SLIDE_RELATIONSHIP = f"{RELATIONSHIPS}/slide"
NOTES_RELATIONSHIP = f"{RELATIONSHIPS}/notesSlide"
LAYOUT_RELATIONSHIP = f"{RELATIONSHIPS}/slideLayout"
MIB = 1024 * 1024
LOCAL_HEADER_SIGNATURE = b"PK\x03\x04"
CENTRAL_HEADER_SIGNATURE = b"PK\x01\x02"
END_SIGNATURE = b"PK\x05\x06"
DOS_EPOCH = 0x21
RAW_DEFLATE_WINDOW = -15
FIRST_SLIDE = "ppt/slides/slide1.xml"
ZIP_BOMB_BYTES = 256 * MIB

SPARSE_STRIDE = 64
LCG_MULTIPLIER = 1_103_515_245
LCG_INCREMENT = 12_345
LCG_MODULUS = 2**31
ALPHABET_SIZE = 26

TOPIC_TEXT = (
    "A red triangle warns of danger ahead. A blue circle gives a mandatory instruction. "
    "An octagon always means stop, whatever language the sign is written in."
)


def pdf_string(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"({escaped})".encode("latin-1")


def text_stream(text: str) -> bytes:
    lines = b"".join(pdf_string(line) + b" Tj 0 -14 Td\n" for line in text.splitlines())
    return b"BT /F1 12 Tf 72 720 Td\n" + lines + b"ET"


def stream_object(body: bytes, *, flate: bool) -> bytes:
    declared_filter = b" /Filter /FlateDecode" if flate else b""
    return b"<< /Length %d%s >>\nstream\n" % (len(body), declared_filter) + body + b"\nendstream"


def flate_blanks(size: int) -> bytes:
    compressor = zlib.compressobj(1)
    chunk = b" " * MIB
    return b"".join(compressor.compress(chunk) for _ in range(size // MIB)) + compressor.flush()


@dataclass(frozen=True, slots=True)
class PdfPage:
    text: str | None = None
    flate: bytes | None = None

    def stream(self) -> bytes | None:
        if self.flate is not None:
            return stream_object(self.flate, flate=True)
        return None if self.text is None else stream_object(text_stream(self.text), flate=False)


def assemble_pdf(objects: Sequence[bytes], trailer: bytes = b"") -> bytes:
    output = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(output)
    output += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    output += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    output += b"trailer\n<< /Size %d /Root 1 0 R%s >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        trailer,
        xref,
    )
    return bytes(output)


def text_pdf(*pages: PdfPage | str | None, trailer: bytes = b"", extra: Sequence[bytes] = ()) -> bytes:
    specs = [page if isinstance(page, PdfPage) else PdfPage(text=page) for page in pages]
    objects: list[bytes] = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids: list[int] = []
    for spec in specs:
        stream = spec.stream()
        contents = b""
        if stream is not None:
            objects.append(stream)
            contents = b" /Contents %d 0 R" % len(objects)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >>%s >>" % contents
        )
        kids.append(len(objects))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % kid for kid in kids),
        len(kids),
    )
    objects.extend(extra)
    return assemble_pdf(objects, trailer)


def form_replay_pdf(form_operations: int, invocations: int) -> bytes:
    form = zlib.compress(b"BT /F1 12 Tf 72 720 Td\n" + b"(a) Tj\n" * form_operations + b"ET")
    content = zlib.compress(b"/X1 Do\n" * invocations)
    return assemble_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [5 0 R] /Count 1 >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            stream_object(content, flate=True),
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
                b"/Resources << /Font << /F1 3 0 R >> /XObject << /X1 6 0 R >> >> >>"
            ),
            (
                b"<< /Type /XObject /Subtype /Form /BBox [0 0 612 792] "
                b"/Resources << /Font << /F1 3 0 R >> >> "
                b"/Length %d /Filter /FlateDecode >>\nstream\n" % len(form)
            )
            + form
            + b"\nendstream",
        ]
    )


def encrypted_pdf(content: bytes, *, user_password: str, owner_password: str) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(content)))
    writer.encrypt(user_password=user_password, owner_password=owner_password, algorithm="RC4-128")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def aes_256_pdf() -> bytes:
    encryption = (
        b"<< /Filter /Standard /V 5 /R 6 /Length 256 /P -4 "
        b"/CF << /StdCF << /CFM /AESV3 /AuthEvent /DocOpen /Length 32 >> >> /StmF /StdCF /StrF /StdCF "
        b"/O <" + b"11" * 48 + b"> /U <" + b"22" * 48 + b"> "
        b"/OE <" + b"33" * 32 + b"> /UE <" + b"44" * 32 + b"> /Perms <" + b"55" * 16 + b"> >>"
    )
    plain = text_pdf(TOPIC_TEXT, extra=(encryption,))
    return plain.replace(b"/Root 1 0 R", b"/Root 1 0 R /Encrypt 6 0 R /ID [<00> <00>]")


@dataclass(frozen=True, slots=True)
class Shape:
    paragraphs: tuple[str, ...]
    placeholder: str | None = None

    def xml(self) -> str:
        placeholder = "" if self.placeholder is None else f'<p:ph type="{self.placeholder}"/>'
        paragraphs = "".join(f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" for text in self.paragraphs)
        return (
            f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="Shape"/><p:cNvSpPr/><p:nvPr>{placeholder}</p:nvPr>'
            f"</p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/>{paragraphs}</p:txBody></p:sp>"
        )


def slide_xml(*shapes: Shape, inner: str = "", root: str = "p:sld") -> bytes:
    body = "".join(shape.xml() for shape in shapes) + inner
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<{root} xmlns:a="{DRAWING}" xmlns:p="{PRESENTATION}" xmlns:r="{RELATIONSHIPS}" '
        f'xmlns:mc="{MARKUP_COMPATIBILITY}"><p:cSld><p:spTree>{body}</p:spTree></p:cSld></{root}>'
    ).encode()


def relationships_xml(*relationships: tuple[str, str, str]) -> bytes:
    entries = "".join(
        f'<Relationship Id="{identifier}" Type="{kind}" Target="{target}"/>'
        for identifier, kind, target in relationships
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="{PACKAGE_RELATIONSHIPS}">{entries}</Relationships>'
    ).encode()


def content_types_xml() -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/ppt/presentation.xml" ContentType="{PRESENTATION_MAIN}"/>'
        "</Types>"
    ).encode()


def presentation_xml(relationship_ids: Sequence[str]) -> bytes:
    slides = "".join(
        f'<p:sldId id="{256 + index}" r:id="{identifier}"/>'
        for index, identifier in enumerate(relationship_ids)
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><p:presentation xmlns:p="{PRESENTATION}" '
        f'xmlns:r="{RELATIONSHIPS}"><p:sldIdLst>{slides}</p:sldIdLst></p:presentation>'
    ).encode()


@dataclass(frozen=True, slots=True)
class Slide:
    shapes: tuple[Shape, ...] = ()
    notes: tuple[Shape, ...] | None = None
    xml: bytes | None = None

    def content(self) -> bytes:
        return self.xml if self.xml is not None else slide_xml(*self.shapes)


def text_slide(*paragraphs: str, notes: tuple[str, ...] | None = None) -> Slide:
    return Slide(
        shapes=(Shape(paragraphs),),
        notes=None if notes is None else (Shape(notes, placeholder="body"),),
    )


def pptx_parts(*slides: Slide, order: Sequence[int] | None = None) -> dict[str, bytes]:
    positions = list(order) if order is not None else list(range(1, len(slides) + 1))
    parts: dict[str, bytes] = {
        "[Content_Types].xml": content_types_xml(),
        "ppt/presentation.xml": presentation_xml([f"rId{position}" for position in positions]),
        "ppt/_rels/presentation.xml.rels": relationships_xml(
            *(
                (f"rId{index}", SLIDE_RELATIONSHIP, f"slides/slide{index}.xml")
                for index in range(1, len(slides) + 1)
            )
        ),
    }
    for index, slide in enumerate(slides, start=1):
        parts[f"ppt/slides/slide{index}.xml"] = slide.content()
        relationships = [("rId1", LAYOUT_RELATIONSHIP, "../slideLayouts/slideLayout1.xml")]
        if slide.notes is not None:
            relationships.append(("rId2", NOTES_RELATIONSHIP, f"../notesSlides/notesSlide{index}.xml"))
            parts[f"ppt/notesSlides/notesSlide{index}.xml"] = slide_xml(
                Shape(("Slide image",), placeholder="sldImg"),
                *slide.notes,
                Shape((str(index),), placeholder="sldNum"),
                root="p:notes",
            )
        parts[f"ppt/slides/_rels/slide{index}.xml.rels"] = relationships_xml(*relationships)
    return parts


def zipped(parts: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def text_pptx(*slides: Slide, order: Sequence[int] | None = None) -> bytes:
    return zipped(pptx_parts(*slides, order=order))


@dataclass(frozen=True, slots=True)
class RawEntry:
    name: str
    payload: bytes
    crc: int
    size: int

    def header_fields(self) -> tuple[int, int, int]:
        return self.crc, len(self.payload), self.size


def raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(6, zlib.DEFLATED, RAW_DEFLATE_WINDOW)
    return compressor.compress(data) + compressor.flush()


def stored_entry(name: str, data: bytes) -> RawEntry:
    return RawEntry(name=name, payload=raw_deflate(data), crc=zlib.crc32(data), size=len(data))


def blank_entry(name: str, size: int) -> RawEntry:
    compressor = zlib.compressobj(1, zlib.DEFLATED, RAW_DEFLATE_WINDOW)
    chunk = b" " * MIB
    crc = 0
    pieces: list[bytes] = []
    for _ in range(size // MIB):
        crc = zlib.crc32(chunk, crc)
        pieces.append(compressor.compress(chunk))
    pieces.append(compressor.flush())
    return RawEntry(name=name, payload=b"".join(pieces), crc=crc, size=size // MIB * MIB)


def handmade_zip(entries: Sequence[RawEntry]) -> bytes:
    local_headers: list[bytes] = []
    central: list[bytes] = []
    offset = 0
    for entry in entries:
        name = entry.name.encode()
        local = struct.pack(
            "<4sHHHHHIIIHH",
            LOCAL_HEADER_SIGNATURE,
            20,
            0,
            zipfile.ZIP_DEFLATED,
            0,
            DOS_EPOCH,
            *entry.header_fields(),
            len(name),
            0,
        )
        local_headers.append(local + name + entry.payload)
        central.append(
            struct.pack(
                "<4sHHHHHHIIIHHHHHII",
                CENTRAL_HEADER_SIGNATURE,
                20,
                20,
                0,
                zipfile.ZIP_DEFLATED,
                0,
                DOS_EPOCH,
                *entry.header_fields(),
                len(name),
                0,
                0,
                0,
                0,
                0,
                offset,
            )
            + name
        )
        offset += len(local) + len(name) + len(entry.payload)
    directory = b"".join(central)
    end = struct.pack(
        "<4sHHHHIIH", END_SIGNATURE, 0, 0, len(entries), len(entries), len(directory), offset, 0
    )
    return b"".join(local_headers) + directory + end


def pptx_with_entry(entry: RawEntry, *slides: Slide) -> bytes:
    parts = pptx_parts(*(slides or (text_slide(TOPIC_TEXT),)))
    parts.pop(entry.name, None)
    return handmade_zip([*(stored_entry(name, data) for name, data in parts.items()), entry])


@cache
def zip_bomb_pptx() -> bytes:
    return pptx_with_entry(blank_entry(FIRST_SLIDE, ZIP_BOMB_BYTES))


def lying_size_pptx(declared: int, actual: int) -> bytes:
    inflated = blank_entry(FIRST_SLIDE, actual)
    return pptx_with_entry(RawEntry(FIRST_SLIDE, inflated.payload, inflated.crc, declared))


def sparse_text(size: int, seed: int) -> str:
    state = seed
    block = bytearray(b" " * size)
    for index in range(0, size, SPARSE_STRIDE):
        state = (state * LCG_MULTIPLIER + LCG_INCREMENT) % LCG_MODULUS
        block[index] = ord("a") + state % ALPHABET_SIZE
    return block.decode("ascii")

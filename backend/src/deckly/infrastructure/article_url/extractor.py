import argparse
import codecs
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import trafilatura

from deckly.infrastructure.article_url.extracted import (
    MAX_PAGE_ATTRIBUTES,
    MAX_PAGE_NODES,
    ExtractedArticle,
)

BYTE_ORDER_MARKS = (codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
NODE = re.compile(rb"<")
START_TAG = re.compile(rb"<[A-Za-z][^<>]*>")
ATTRIBUTE = re.compile(rb"""(?<![\s/])[\s/]++[^\s/>"'=]++(?:\s*+=\s*+(?:"[^"]*+"|'[^']*+'|[^\s"'=<>`]++))?""")
TAG_NAME_LENGTH = 1


@dataclass(frozen=True, slots=True)
class ExtractorArguments:
    page: Path
    charset: str | None
    max_characters: int


def parse_arguments(argv: Sequence[str]) -> ExtractorArguments:
    parser = argparse.ArgumentParser(prog="deckly-article-extractor")
    parser.add_argument("--charset", default=None)
    parser.add_argument("--max-characters", type=int, required=True)
    parser.add_argument("page")
    namespace = parser.parse_args(argv)
    max_characters = int(namespace.max_characters)
    if max_characters <= 0:
        parser.error("--max-characters must be positive")
    charset = namespace.charset
    return ExtractorArguments(
        page=Path(str(namespace.page)),
        charset=None if charset is None else str(charset),
        max_characters=max_characters,
    )


def markup_end(content: bytes, max_nodes: int, max_attributes: int) -> int:
    end = len(content)
    for count, node in enumerate(NODE.finditer(content)):
        if count == max_nodes:
            end = node.start()
            break
    attributes = 0
    for tag in START_TAG.finditer(content, 0, end):
        attributes += len(ATTRIBUTE.findall(tag.group(), TAG_NAME_LENGTH))
        if attributes > max_attributes:
            return tag.start()
    return end


def decoded(content: bytes, charset: str | None) -> bytes | str:
    if charset is None or content.startswith(BYTE_ORDER_MARKS):
        return content
    try:
        return content.decode(charset, errors="replace")
    except LookupError:
        return content


def extract(content: bytes, charset: str | None, max_characters: int) -> ExtractedArticle:
    end = markup_end(content, MAX_PAGE_NODES, MAX_PAGE_ATTRIBUTES)
    truncated = end < len(content)
    document = trafilatura.bare_extraction(
        decoded(content[:end], charset),
        favor_precision=True,
        include_comments=False,
        include_tables=True,
        with_metadata=True,
    )
    if document is None or isinstance(document, dict):
        return ExtractedArticle(title=None, text=None, markup_truncated=truncated)
    text = document.text
    return ExtractedArticle(
        title=document.title,
        text=None if text is None else text[:max_characters],
        markup_truncated=truncated,
    )


def main(argv: Sequence[str], out: TextIO = sys.stdout) -> None:
    arguments = parse_arguments(argv)
    content = arguments.page.read_bytes()
    out.write(extract(content, arguments.charset, arguments.max_characters).model_dump_json())


if __name__ == "__main__":
    main(sys.argv[1:])

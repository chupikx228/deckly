from deckly.application.ports import RetrievedPage, SourceMaterial
from deckly.domain.generation import GenerationRequest
from deckly.domain.notes.note import Note


class UnimplementedSourceRetriever:
    async def retrieve(self, request: GenerationRequest) -> tuple[RetrievedPage, ...]:
        del request
        message = "no source retriever adapter is implemented yet"
        raise NotImplementedError(message)


class UnimplementedSourceParser:
    async def parse(
        self, request: GenerationRequest, pages: tuple[RetrievedPage, ...]
    ) -> tuple[SourceMaterial, ...]:
        del request, pages
        message = "no source parser adapter is implemented yet"
        raise NotImplementedError(message)


class UnimplementedMediaFetcher:
    async def fetch(self, request: GenerationRequest, notes: tuple[Note, ...]) -> tuple[Note, ...]:
        del request, notes
        message = "no media fetcher adapter is implemented yet"
        raise NotImplementedError(message)

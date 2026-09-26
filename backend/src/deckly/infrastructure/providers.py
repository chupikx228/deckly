from deckly.domain.generation import GenerationRequest
from deckly.domain.notes.note import Note


class UnimplementedMediaFetcher:
    async def fetch(self, request: GenerationRequest, notes: tuple[Note, ...]) -> tuple[Note, ...]:
        del request, notes
        message = "no media fetcher adapter is implemented yet"
        raise NotImplementedError(message)

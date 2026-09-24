from fastapi import Request

from deckly.application.generations import CreateGeneration


def create_generation_use_case(request: Request) -> CreateGeneration:
    use_case = request.app.state.create_generation
    if not isinstance(use_case, CreateGeneration):
        message = "create_generation is not wired into the application state"
        raise TypeError(message)
    return use_case

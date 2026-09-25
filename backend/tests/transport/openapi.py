from datetime import datetime
from functools import cache
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, Schema

SPEC_PATH = Path(__file__).resolve().parents[3] / ".claude" / "backend" / "openapi.yaml"
SPEC_URI = "urn:deckly:openapi"
RFC3339_TIME_SEPARATORS = ("T", "t")

format_checker = FormatChecker(["uuid"])


@format_checker.checks("date-time", raises=ValueError)
def is_rfc3339_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    parsed = datetime.fromisoformat(value)
    return any(separator in value for separator in RFC3339_TIME_SEPARATORS) and parsed.utcoffset() is not None


@cache
def spec_registry() -> Registry[Schema]:
    contents = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    resource: Resource[Schema] = Resource.from_contents(contents, default_specification=DRAFT202012)
    registry: Registry[Schema] = Registry()
    return registry.with_resources([(SPEC_URI, resource)])


@cache
def schema_validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": f"{SPEC_URI}#/components/schemas/{name}"},
        registry=spec_registry(),
        format_checker=format_checker,
    )


def spec_errors(name: str, instance: object) -> list[str]:
    return [error.message for error in schema_validator(name).iter_errors(instance)]


@cache
def spec_contents() -> object:
    contents: object = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    return contents


def declared_responses(path: str, method: str) -> dict[str, object]:
    node = spec_contents()
    for key in ("paths", path, method, "responses"):
        assert isinstance(node, dict)
        node = node[key]
    assert isinstance(node, dict)
    return {str(status): response for status, response in node.items()}

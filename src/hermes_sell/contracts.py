"""Immutable, allowlisted public contracts and restricted JSON schemas."""

import hashlib
import json
import re
from typing import Any

from jsonschema import Draft202012Validator

PROTOCOL = "hermes-sell/0.1"
NETWORK = "eip155:84532"
ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
SCHEMA_KEYS = {
    "type", "title", "description", "properties", "required", "additionalProperties",
    "items", "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum", "enum",
}


class Problem(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def canonical(value: Any) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        encoded.encode("utf-8")
        return encoded
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise Problem("invalid_json", "JSON must contain only finite numbers and valid UTF-8 strings.", 422) from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_schema(schema: dict, depth: int = 0) -> None:
    if not isinstance(schema, dict) or depth > 5 or set(schema) - SCHEMA_KEYS:
        raise Problem("unsupported_schema", "This JSON Schema is not supported.")
    kind = schema.get("type")
    if not isinstance(kind, str) or kind not in {"object", "array", "string", "integer", "number", "boolean"}:
        raise Problem("unsupported_schema", "Specify a single explicit type.")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise Problem("invalid_schema", "The JSON Schema is invalid.") from exc
    if kind == "object":
        props = schema.get("properties", {})
        if len(props) > 24 or schema.get("additionalProperties") is not False:
            raise Problem("unbounded_schema", "Limit objects to 24 properties and set additionalProperties=false.")
        if set(schema.get("required", [])) - props.keys():
            raise Problem("invalid_schema", "All required fields must be listed in properties.")
        for child in props.values():
            validate_schema(child, depth + 1)
    if kind == "array":
        if not 0 <= schema.get("maxItems", -1) <= 100:
            raise Problem("unbounded_schema", "Arrays require maxItems between 0 and 100.")
        validate_schema(schema.get("items"), depth + 1)
    if kind == "string" and not 1 <= schema.get("maxLength", 0) <= 20000:
        raise Problem("unbounded_schema", "Strings require maxLength between 1 and 20000.")


def validate_value(value: Any, schema: dict) -> None:
    if len(canonical(value).encode()) > 65536:
        raise Problem("input_too_large", "Input or output must not exceed 64 KiB.", 413)
    error = next(Draft202012Validator(schema).iter_errors(value), None)
    if error:
        path = ".".join(map(str, error.absolute_path)) or "input"
        raise Problem("schema_mismatch", f"{path}: Check the schema requirements.", 422)


def make_revision(spec: dict, bundle: dict) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", spec.get("slug", "")):
        raise Problem("invalid_slug", "Use a 3-64 character slug with lowercase letters, digits, and hyphens. Start with a letter or digit.")
    for key, limit in (("name", 100), ("description", 1500)):
        if not isinstance(spec.get(key), str) or not 1 <= len(spec[key]) <= limit:
            raise Problem("invalid_contract", f"Check the {key} field.")
    for key in ("input_schema", "output_schema"):
        validate_schema(spec[key])
    if spec["input_schema"]["type"] != "object":
        raise Problem("unsupported_schema", "The input schema must have object as its root type.")
    raw_amount = spec.get("amount")
    if not (type(raw_amount) is int or (isinstance(raw_amount, str) and re.fullmatch(r"[0-9]{1,8}", raw_amount))):
        raise Problem("invalid_price", "The price must be an integer or integer string in the smallest currency unit.")
    amount = int(raw_amount)
    if not 1 <= amount <= 10_000_000:
        raise Problem("invalid_price", "The price must be between 1 and 10000000 in the smallest currency unit.")
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", spec.get("pay_to", "")):
        raise Problem("invalid_payee", "Specify a valid EVM recipient address.")
    examples = spec.get("examples", [])
    if not 1 <= len(examples) <= 5:
        raise Problem("invalid_examples", "Provide 1-5 input and output examples.")
    for example in examples:
        validate_value(example["input"], spec["input_schema"])
        validate_value(example["output"], spec["output_schema"])
    public = {key: spec[key] for key in ("slug", "name", "description", "input_schema", "output_schema", "examples", "pay_to")}
    public.update(amount=str(amount), network=NETWORK, asset=ASSET, protocol=PROTOCOL,
                  currency="USDC", decimals=6, execution_limit_seconds=60)
    execution_digest = digest({"bundle": bundle, "input_schema": spec["input_schema"], "output_schema": spec["output_schema"]})
    public["revision"] = digest({"public": public, "execution_digest": execution_digest})
    # Private contents never enter the public document.
    return {"public": public, "bundle": bundle, "execution_digest": execution_digest}

import jsonschema

PREDICTION_SCHEMA = {
    "type": "object",
    "required": ["schema_version", "stuck_item", "smallest_action", "confidence", "flags_raised", "questions_asked"],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "stuck_item": {"type": "string", "minLength": 5, "maxLength": 300},
        "smallest_action": {"type": "string", "minLength": 5, "maxLength": 300},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "flags_raised": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 10,
        },
        "questions_asked": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 5,
        },
    },
}


def validate_prediction(data: dict) -> tuple[bool, str | None]:
    try:
        jsonschema.validate(data, PREDICTION_SCHEMA)
        return True, None
    except jsonschema.ValidationError as e:
        path = ".".join(str(p) for p in e.absolute_path)
        if path:
            return False, f"{path}: {e.message}"
        return False, e.message

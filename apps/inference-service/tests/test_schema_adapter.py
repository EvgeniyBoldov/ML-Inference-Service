import pytest

from app.schema_adapter import mlflow_signature_to_json_schema


def test_mlflow_named_columns_become_object_json_schema() -> None:
    signature = {
        "inputs": [
            {"name": "age", "type": "long", "required": True},
            {"name": "income", "type": "double", "required": False},
        ]
    }

    assert mlflow_signature_to_json_schema(signature, "inputs") == {
        "type": "object",
        "properties": {"age": {"type": "integer"}, "income": {"type": "number"}},
        "required": ["age"],
    }


def test_missing_mlflow_signature_is_rejected() -> None:
    with pytest.raises(ValueError, match="missing"):
        mlflow_signature_to_json_schema(None, "inputs")


def test_mlflow_serialized_signature_is_supported() -> None:
    schema = mlflow_signature_to_json_schema(
        {"outputs": '[{"name":"prediction","type":"integer","required":true}]'}, "outputs"
    )

    assert schema["properties"]["prediction"] == {"type": "integer"}

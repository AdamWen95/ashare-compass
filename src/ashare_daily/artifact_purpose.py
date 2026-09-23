"""Pure production-purpose checks shared by archive readers and publishers."""
from pathlib import Path
from typing import Any


_ARTIFACT_KEYS = {"schema_version", "selection_id", "production_eligible"}
_ARTIFACT_CONTAINERS = {"report", "selection", "result", "input_snapshot", "source_selection", "metadata", "provenance"}


def is_production_artifact(value: Any, *, artifact_context: bool = True) -> bool:
    """Missing legacy markers are compatible; explicit unknown purpose is closed.

    Source-permission purpose is a different field (for example permission for
    personal local research). Nested permission objects are not artifact-purpose
    declarations. Engineering markers anywhere still exclude the entire object.
    """
    if isinstance(value, dict):
        purpose = value.get("purpose")
        if isinstance(purpose, str) and purpose.strip().casefold() == "engineering_validation":
            return False
        if "production_eligible" in value and value["production_eligible"] is not True:
            return False
        if "purpose" in value and (artifact_context or _ARTIFACT_KEYS.intersection(value)) and purpose != "production":
            return False
        for key, item in value.items():
            if isinstance(item, str):
                if str(key).endswith("selection_id") and item.strip().casefold().startswith("validation-sector-"):
                    return False
                if key == "schema_version" and item.strip().casefold().startswith("f3s-validation-"):
                    return False
            if not is_production_artifact(item, artifact_context=key in _ARTIFACT_CONTAINERS):
                return False
    elif isinstance(value, list):
        return all(is_production_artifact(item, artifact_context=artifact_context) for item in value)
    return True


def is_production_path(path: Path) -> bool:
    return not any(part.casefold() == "engineering_validation" or part.casefold().startswith("validation-sector-")
                   for part in Path(path).parts)

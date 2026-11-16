"""Per-claim evaluation for each HandoffClaim verification_mode."""

import json as json_lib
import math
from typing import Any

import msgspec

from sourcerykit.logger import get_logger
from sourcerykit.schemas import HandoffClaim, Outcome, VerificationMode

_log = get_logger(__name__)

# Pre-initialize the msgspec encoder once at module level to reuse its internal buffers
_encoder = msgspec.json.Encoder(order="sorted")


def canonical_json(value: Any) -> str:
    """Returns a deterministic JSON string using msgspec's hyper-optimized C engine."""
    try:
        return _encoder.encode(value).decode("utf-8")
    except Exception as e:
        _log.debug("canonical_json_fallback", error=str(e))
        return str(value)


def evaluate_claim(claim: HandoffClaim, row_response: Any) -> dict[str, Any]:
    verification_mode = claim.verification_mode
    claimed_value = claim.claimed_value

    # Safely extract and format the path display
    json_path = (claim.json_path or "").strip() or "$"

    base = {
        "action_name": claim.action_name,
        "verification_mode": verification_mode,
        "claimed": canonical_json(claimed_value),
        "json_path": json_path,
    }

    # Extract target node by path strings
    try:
        at_path = _get_by_json_path(row_response, json_path)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        return {**base, "result": Outcome.CAUGHT, "detail": f"json_path: {e}"}

    # Lazily
    base["indexed"] = canonical_json(row_response)
    base["indexed_at_path"] = canonical_json(at_path)

    match verification_mode:
        case VerificationMode.FIELD_EXTRACTION:
            is_valid = True

            for entry in claimed_value:
                path = entry["path"] if isinstance(entry, dict) else entry.path
                value = entry["value"] if isinstance(entry, dict) else entry.value
                raw_true_value = _get_by_json_path(row_response, path)

                # Use canonical JSON for arrays/dicts, str() for scalars
                true_value: str | None
                if isinstance(raw_true_value, (list, dict)):
                    true_value = canonical_json(raw_true_value) if raw_true_value is not None else None
                    # Try to parse claimed value as JSON for array/dict comparison
                    try:
                        parsed_claimed = json_lib.loads(value.strip())
                        if isinstance(parsed_claimed, (list, dict)):
                            value = canonical_json(parsed_claimed)
                    except (json_lib.JSONDecodeError, ValueError):
                        pass  # Keep original value for comparison
                else:
                    true_value = str(raw_true_value) if raw_true_value is not None else None

                if true_value is None or value.strip() != true_value.strip():
                    is_valid = False
                    break

            return {**base, "result": Outcome.PASS if is_valid else Outcome.CAUGHT}

        case VerificationMode.RANGE_THRESHOLD:
            if claim.range_min is None and claim.range_max is None:
                return {
                    **base,
                    "result": Outcome.CAUGHT,
                    "detail": "range_threshold requires range_min and/or range_max",
                }

            # The agent's claimed_values ({path, value} entries), or a single number at json_path.
            if isinstance(claimed_value, list):
                if not claimed_value:
                    return {**base, "result": Outcome.CAUGHT, "detail": "claimed_value is empty"}
                try:
                    targets = [
                        (_get_by_json_path(row_response, _entry(e, "path")), _entry(e, "value")) for e in claimed_value
                    ]
                except (KeyError, IndexError, TypeError, ValueError) as e:
                    return {**base, "result": Outcome.CAUGHT, "detail": f"path: {e}"}
            else:
                targets = [(at_path, claimed_value)]

            for indexed, claimed in targets:
                failure = _check_range(claim, indexed, claimed, base)
                if failure is not None:
                    return failure
            return {**base, "result": Outcome.PASS}


def _entry(entry: Any, field: str) -> Any:
    """Reads a field of a ClaimedValue, given as a model or as a dict."""
    return entry[field] if isinstance(entry, dict) else getattr(entry, field)


def _check_range(claim: HandoffClaim, indexed: Any, claimed: Any, base: dict[str, Any]) -> dict[str, Any] | None:
    """Returns a CAUGHT verdict if the indexed value is out of range or differs from the claim, else None."""
    if isinstance(indexed, list):
        if not indexed:
            return {**base, "result": Outcome.CAUGHT, "detail": "indexed list is empty; cannot compute average"}
        try:
            numbers = [_coerce_number(item) for item in indexed]
        except (TypeError, ValueError) as exc:
            return {**base, "result": Outcome.CAUGHT, "detail": f"list element not numeric: {exc}"}
        value = sum(numbers) / len(numbers)
        base["indexed_average"] = str(value)
    else:
        try:
            value = _coerce_number(indexed)
        except (TypeError, ValueError) as exc:
            return {**base, "result": Outcome.CAUGHT, "detail": f"indexed value not numeric: {exc}"}

    if claim.range_min is not None and value < float(claim.range_min):
        return {**base, "result": Outcome.CAUGHT, "detail": f"value {value} below range_min {claim.range_min}"}
    if claim.range_max is not None and value > float(claim.range_max):
        return {**base, "result": Outcome.CAUGHT, "detail": f"value {value} above range_max {claim.range_max}"}

    try:
        num_match = math.isclose(_coerce_number(claimed), value, rel_tol=0.0, abs_tol=1e-9)
    except (TypeError, ValueError):
        num_match = False

    if not num_match:
        return {**base, "result": Outcome.CAUGHT, "detail": "claimed_value does not match indexed numeric at path"}
    return None


def _get_by_json_path(obj: Any, path: str) -> Any:
    """Path walker"""
    p = (path or "").strip()
    if p.startswith("$"):
        p = p[1:].lstrip(".").strip()
    if not p:
        return obj

    # Path normalization
    normalized = p.replace("[", ".").replace("]", "")

    cursor = obj
    for segment in (s.strip() for s in normalized.split(".") if s and not s.isspace()):
        t = type(cursor)

        # Dict lookup using a type comparison
        if t is dict:
            if segment not in cursor:
                raise KeyError(segment)
            cursor = cursor[segment]

        # List index tracking
        elif t is list:
            if segment.isdigit():
                idx = int(segment)
                if idx >= len(cursor):
                    raise IndexError(f"index {idx} out of range")
                cursor = cursor[idx]
            else:
                raise TypeError(f"Cannot index list with string key {segment!r}")

        else:
            raise TypeError(f"Expected dict or list at segment {segment!r}, got {t.__name__}")

    return cursor


def _coerce_number(value: Any) -> float:
    """Fast-pathed numeric coercion strategy."""
    if isinstance(value, bool):
        raise TypeError("boolean is not a numeric bound value")

    try:
        return float(value)
    except (TypeError, ValueError):
        raise TypeError(f"not numeric: {type(value).__name__}")

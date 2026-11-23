from enum import StrEnum


class Outcome(StrEnum):
    """Final verdict for a handoff evaluation."""

    PASS = "PASS"
    PASS_WITH_NOTES = "PASS_WITH_NOTES"
    """Nothing is wrong, but some statements state no number and have no data to check them."""
    CAUGHT = "CAUGHT"
    ERROR = "ERROR"

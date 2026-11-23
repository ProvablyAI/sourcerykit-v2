from enum import StrEnum


class Outcome(StrEnum):
    """Final verdict for a handoff evaluation."""

    PASS = "PASS"
    PASS_WITH_NOTES = "PASS_WITH_NOTES"
    """Nothing is contradicted, but some statements have no data to check them."""
    CAUGHT = "CAUGHT"
    ERROR = "ERROR"

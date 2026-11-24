from enum import StrEnum


class Outcome(StrEnum):
    """The verdict on a whole answer, from the verdicts of its claims."""

    PASS = "PASS"
    """Every statement is checked and backed by the recorded data."""
    PASS_WITH_NOTES = "PASS_WITH_NOTES"
    """Nothing is contradicted, but some statements have no data to check them."""
    CAUGHT = "CAUGHT"
    """A statement is contradicted by the recorded data."""
    ERROR = "ERROR"
    """No statement could be checked: nothing was verified."""

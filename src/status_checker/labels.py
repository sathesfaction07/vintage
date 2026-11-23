"""Locale-aware label mapping, and a static check that the mapping is self-consistent.

Every marketplace prints its own wording for the same underlying state. Mapping those to a
canonical status is the easy half. The half that goes wrong quietly is that the mapping is
*assumed* to be a function of the label alone -- so a fleet-wide "how many are restricted" adds up
counts from tables that may disagree, and nothing raises.
"""

from __future__ import annotations

import json
import unicodedata
from collections import defaultdict
from pathlib import Path

from .models import LabelConflict, Status


def normalise(label: str) -> str:
    """Fold a raw label to a comparison key.

    NFKC and casefold are both required and neither substitutes for the other. This is the same
    reasoning Kinship arrived at for merchant names -- it is a repeat, not a discovery -- and it
    is repeated because the failure is real: a fullwidth form needs NFKC, and the German sharp s
    needs casefold, since `lower()` leaves it alone.

    Whitespace is collapsed because a label scraped from markup carries whatever indentation the
    template had, and a non-breaking space looks identical to a reader while comparing unequal.
    """
    folded = unicodedata.normalize("NFKC", label).casefold()
    return " ".join(folded.split())


class LabelMap:
    """Localised label to canonical status, per marketplace."""

    def __init__(self, mapping: dict[str, dict[str, Status]]) -> None:
        self._mapping = {
            marketplace: {normalise(label): status for label, status in labels.items()}
            for marketplace, labels in mapping.items()
        }
        self._raw = mapping

    @classmethod
    def from_file(cls, path: Path) -> tuple[LabelMap, list[str]]:
        """Load a mapping, returning it alongside any problems found.

        Problems are returned rather than raised so a single unrecognised status name does not
        cost you the whole table -- but they are returned rather than ignored, because a silently
        dropped label becomes an UNKNOWN reading that looks like platform drift.
        """
        problems: list[str] = []
        payload = json.loads(path.read_text(encoding="utf-8"))
        mapping: dict[str, dict[str, Status]] = {}
        for marketplace, labels in payload.items():
            resolved: dict[str, Status] = {}
            for label, status_name in labels.items():
                try:
                    resolved[label] = Status(status_name)
                except ValueError:
                    problems.append(
                        f"{marketplace}: label {label!r} maps to unknown status {status_name!r}"
                    )
                    continue
                if resolved[label] is Status.UNKNOWN:
                    problems.append(
                        f"{marketplace}: label {label!r} maps to 'unknown', which is what an "
                        f"UNMAPPED label already produces -- the entry hides drift rather than "
                        f"recording it"
                    )
            mapping[marketplace] = resolved
        return cls(mapping), problems

    @property
    def marketplaces(self) -> list[str]:
        return sorted(self._mapping)

    def resolve(self, marketplace: str, raw_label: str) -> Status:
        """Canonical status for a label, or UNKNOWN.

        UNKNOWN covers both an unrecognised label and an unrecognised marketplace, which are
        different causes with the same consequence: we hold a string and cannot say what it means.
        `explain` distinguishes them; the status deliberately does not, because a caller acting on
        the status must treat both the same way.
        """
        table = self._mapping.get(marketplace)
        if table is None:
            return Status.UNKNOWN
        return table.get(normalise(raw_label), Status.UNKNOWN)

    def knows_marketplace(self, marketplace: str) -> bool:
        return marketplace in self._mapping

    def labels_for(self, marketplace: str) -> dict[str, Status]:
        return dict(self._raw.get(marketplace, {}))

    def conflicts(self) -> list[LabelConflict]:
        """Labels that mean different things in different marketplaces.

        Static, checkable, and worth failing a build over. A label appearing in two tables with two
        canonical statuses means any aggregate over both is undefined -- and the aggregate will
        still compute, to a number that looks entirely reasonable.

        Compares on the NORMALISED key, because two tables spelling a label with different
        whitespace or case are the same label for every purpose that matters here.
        """
        seen: dict[str, dict[str, Status]] = defaultdict(dict)
        for marketplace, labels in self._mapping.items():
            for label, status in labels.items():
                seen[label][marketplace] = status
        return [
            LabelConflict(label=label, mappings=dict(sorted(mappings.items())))
            for label, mappings in sorted(seen.items())
            if len(set(mappings.values())) > 1
        ]

    def coverage(self) -> dict[str, set[Status]]:
        """Which canonical statuses each marketplace can express.

        A marketplace with no label for RESTRICTED can never report one, so its zero is not a
        finding. Reporting the gap separates "none are restricted" from "this table cannot say".
        """
        return {
            marketplace: set(labels.values()) for marketplace, labels in self._mapping.items()
        }

    def missing_statuses(self) -> dict[str, set[Status]]:
        """Canonical statuses a marketplace's table cannot produce.

        UNKNOWN is excluded: no table should map anything to it, since it is what an absent entry
        already means.
        """
        expressible = {status for status in Status if status is not Status.UNKNOWN}
        return {
            marketplace: expressible - statuses
            for marketplace, statuses in self.coverage().items()
            if expressible - statuses
        }

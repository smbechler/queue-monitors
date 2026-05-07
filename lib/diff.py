"""Generic snapshot diffing for tabular queue data.

Each monitor produces a list of "project" dicts with at least an `id` field.
This module computes added / removed / changed projects between two snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Diff:
    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    # changed is a list of (id, {field: (old_value, new_value)})
    changed: list[tuple[str, dict[str, tuple[Any, Any]]]] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def diff_snapshots(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
    id_field: str = "id",
    ignore_fields: tuple[str, ...] = (),
) -> Diff:
    """Compare two lists of dict records by `id_field`.

    Returns a Diff with added rows, removed rows, and per-field changes.
    `ignore_fields` are excluded from the change-detection comparison
    (useful for noisy fields like timestamps).
    """
    prev_by_id = {row[id_field]: row for row in previous if row.get(id_field)}
    curr_by_id = {row[id_field]: row for row in current if row.get(id_field)}

    prev_ids = set(prev_by_id)
    curr_ids = set(curr_by_id)

    added = [curr_by_id[i] for i in sorted(curr_ids - prev_ids)]
    removed = [prev_by_id[i] for i in sorted(prev_ids - curr_ids)]

    changed: list[tuple[str, dict[str, tuple[Any, Any]]]] = []
    for project_id in sorted(curr_ids & prev_ids):
        old_row = prev_by_id[project_id]
        new_row = curr_by_id[project_id]
        field_changes: dict[str, tuple[Any, Any]] = {}
        all_keys = set(old_row) | set(new_row)
        for key in all_keys:
            if key in ignore_fields or key == id_field:
                continue
            old_val = old_row.get(key)
            new_val = new_row.get(key)
            if _normalize(old_val) != _normalize(new_val):
                field_changes[key] = (old_val, new_val)
        if field_changes:
            changed.append((project_id, field_changes))

    return Diff(added=added, removed=removed, changed=changed)


def _normalize(value: Any) -> Any:
    """Normalize values for comparison: strip strings, treat empty as None."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return value

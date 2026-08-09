"""Versioned matching-policy configuration."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_ALIASES: dict[str, tuple[str, ...]] = {
    "chi_surname": ("chi_surname",),
    "chi_firstname": ("chi_firstname",),
    "eng_surname": ("eng_surname",),
    "eng_firstname": ("eng_firstname",),
    "phone": ("phone_num", "res_phone", "phone"),
    "email": ("email", "contact_email"),
    "birthday": ("birthday", "dob"),
    "hksr_num": ("hksr_num",),
    "hkid": ("hkid", "hkid_num"),
}


@dataclass(frozen=True)
class SourceProfile:
    source: str
    field_map: dict[str, str] = field(default_factory=dict)
    identifier_scope: dict[str, str] = field(default_factory=dict)
    disabled_attributes: frozenset[str] = frozenset()

    def field_for(self, attribute: str) -> str | None:
        if attribute in self.disabled_attributes:
            return None
        return self.field_map.get(attribute)

    def id_scope(self, attribute: str) -> str:
        return self.identifier_scope.get(attribute, "unknown")


@dataclass(frozen=True)
class MatchingPolicy:
    name: str = "ccd-default-shadow-policy"
    version: str = "1.0.0"
    aliases: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_ALIASES))
    source_profiles: dict[str, SourceProfile] = field(default_factory=dict)
    trusted_global_identifiers: frozenset[str] = frozenset()
    high_precision_target: float = 0.95
    minimum_high_samples: int = 30
    minimum_positive_labels_per_split: int = 10
    max_block_size: int = 10_000
    max_candidate_pairs: int = 500_000

    def profile(self, source: str) -> SourceProfile:
        return self.source_profiles.get(source, SourceProfile(source=source))

    def sources(self) -> tuple[str, ...]:
        """Return the explicitly governed CCD sources for this policy."""
        return tuple(sorted(self.source_profiles))

    def value(self, record: dict[str, Any], attribute: str) -> Any:
        source = str(record.get("source") or record.get("ccd_reg_source") or "")
        profile = self.profile(source)
        # Evaluation records are already projected into canonical attribute
        # names. Do not look up their original source fieldname a second time.
        if record.get("record_id") and attribute in record:
            return record.get(attribute)
        explicit = profile.field_for(attribute)
        if explicit:
            return record.get(explicit)
        if attribute in profile.disabled_attributes:
            return None
        for fieldname in self.aliases.get(attribute, (attribute,)):
            value = record.get(fieldname)
            if value not in (None, ""):
                return value
        return None

    def globally_comparable(self, source: str, attribute: str) -> bool:
        return (
            attribute in self.trusted_global_identifiers
            and self.profile(source).id_scope(attribute) == "global"
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MatchingPolicy:
        profiles: dict[str, SourceProfile] = {}
        for raw in value.get("source_profiles", []):
            profile = SourceProfile(
                source=str(raw["source"]),
                field_map=dict(raw.get("field_map") or {}),
                identifier_scope=dict(raw.get("identifier_scope") or {}),
                disabled_attributes=frozenset(raw.get("disabled_attributes") or ()),
            )
            profiles[profile.source] = profile
        aliases = {
            key: tuple(fields)
            for key, fields in (value.get("aliases") or DEFAULT_ALIASES).items()
        }
        return cls(
            name=str(value.get("name") or "ccd-default-shadow-policy"),
            version=str(value.get("version") or "1.0.0"),
            aliases=aliases,
            source_profiles=profiles,
            trusted_global_identifiers=frozenset(value.get("trusted_global_identifiers") or ()),
            high_precision_target=float(value.get("high_precision_target", 0.95)),
            minimum_high_samples=int(value.get("minimum_high_samples", 30)),
            minimum_positive_labels_per_split=int(
                value.get("minimum_positive_labels_per_split", 10)
            ),
            max_block_size=int(value.get("max_block_size", 10_000)),
            max_candidate_pairs=int(value.get("max_candidate_pairs", 500_000)),
        )

    def attributes(self) -> Iterable[str]:
        return self.aliases.keys()

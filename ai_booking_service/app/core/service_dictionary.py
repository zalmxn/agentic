import difflib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_DICTIONARY_PATH = Path(__file__).resolve().parent / "service_dictionary.json"


@dataclass(frozen=True)
class ServiceEntry:
    code: str
    display_name: str
    category: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class LocationEntry:
    key: str
    display_name: str
    aliases: tuple[str, ...]
    latitude: float
    longitude: float


class ServiceDictionary:
    """
    Configurable service & location dictionary loaded from JSON.
    Adding new services or aliases requires only updating the JSON file
    (or calling register_service) without modifying parser logic.
    """

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or DEFAULT_DICTIONARY_PATH
        self.services: dict[str, ServiceEntry] = {}
        self.locations: dict[str, LocationEntry] = {}
        self._alias_to_service: list[tuple[str, str]] = []
        self._single_word_aliases: dict[str, str] = {}
        self._alias_to_location: list[tuple[str, str]] = []
        self.reload()

    def reload(self) -> None:
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.services.clear()
        self.locations.clear()

        for code, info in raw.get("services", {}).items():
            aliases = tuple(sorted({a.lower().strip() for a in info.get("aliases", [])}, key=len, reverse=True))
            self.services[code] = ServiceEntry(
                code=code,
                display_name=info.get("display_name", code.replace("_", " ").title()),
                category=info.get("category", "general"),
                aliases=aliases,
            )

        for key, info in raw.get("locations", {}).items():
            aliases = tuple(sorted({a.lower().strip() for a in info.get("aliases", [])}, key=len, reverse=True))
            self.locations[key] = LocationEntry(
                key=key,
                display_name=info.get("display_name", key.replace("_", " ").title()),
                aliases=aliases,
                latitude=float(info["latitude"]),
                longitude=float(info["longitude"]),
            )
        self._rebuild_indexes()

    def register_service(
        self,
        code: str,
        display_name: str,
        category: str,
        aliases: list[str],
    ) -> None:
        normalized = tuple(sorted({a.lower().strip() for a in aliases}, key=len, reverse=True))
        self.services[code] = ServiceEntry(
            code=code,
            display_name=display_name,
            category=category,
            aliases=normalized,
        )
        self._rebuild_indexes()

    def _rebuild_indexes(self) -> None:
        pairs: list[tuple[str, str]] = []
        single: dict[str, str] = {}
        for code, entry in self.services.items():
            for alias in entry.aliases:
                pairs.append((alias, code))
                if " " not in alias and len(alias) >= 5:
                    single[alias] = code
        # Longest aliases matched first
        pairs.sort(key=lambda item: len(item[0]), reverse=True)
        self._alias_to_service = pairs
        self._single_word_aliases = single

        loc_pairs: list[tuple[str, str]] = []
        for key, entry in self.locations.items():
            for alias in entry.aliases:
                loc_pairs.append((alias, key))
        loc_pairs.sort(key=lambda item: len(item[0]), reverse=True)
        self._alias_to_location = loc_pairs

    def match_services(self, normalized_text: str) -> list[ServiceEntry]:
        """Return all unique matched services ordered by first occurrence in text."""
        found_positions: dict[str, int] = {}
        for alias, code in self._alias_to_service:
            m = re.search(rf"\b{re.escape(alias)}\b", normalized_text)
            if m:
                if code not in found_positions or m.start() < found_positions[code]:
                    found_positions[code] = m.start()

        # Fuzzy single-word fallback for slight misspellings not in static typo map
        if not found_positions:
            vocab = list(self._single_word_aliases.keys())
            for token in normalized_text.split():
                if len(token) < 5 or not token.isalpha():
                    continue
                matches = difflib.get_close_matches(token, vocab, n=1, cutoff=0.84)
                if matches:
                    code = self._single_word_aliases[matches[0]]
                    found_positions.setdefault(code, normalized_text.find(token))

        ordered_codes = sorted(found_positions.keys(), key=lambda c: found_positions[c])
        return [self.services[c] for c in ordered_codes]

    def match_location(self, normalized_text: str) -> LocationEntry | None:
        for alias, key in self._alias_to_location:
            if re.search(rf"\b{re.escape(alias)}\b", normalized_text):
                return self.locations[key]
        return None

    def list_supported_service_names(self) -> list[str]:
        return [entry.display_name for entry in self.services.values()]


@lru_cache(maxsize=1)
def get_service_dictionary() -> ServiceDictionary:
    return ServiceDictionary()

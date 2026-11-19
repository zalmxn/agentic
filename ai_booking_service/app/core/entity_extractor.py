import re
from datetime import datetime

from app.core.date_time_parser import DateTimeParser
from app.core.service_dictionary import ServiceDictionary, get_service_dictionary
from app.schemas.intents import ExtractedEntities, ProviderReference
from app.utils.normalization import normalize_text
from app.utils.validators import extract_booking_identifier

ORDINAL_MAP: dict[str, int] = {
    "first": 0, "1st": 0, "1": 0, "one": 0,
    "second": 1, "2nd": 1, "2": 1, "two": 1,
    "third": 2, "3rd": 2, "3": 2, "three": 2,
    "fourth": 3, "4th": 3, "4": 3, "four": 3,
    "fifth": 4, "5th": 4, "5": 4, "five": 4,
}

NON_LOCATION_STOPWORDS = {
    "me", "here", "my", "home", "today", "tomorrow", "now", "noon",
    "morning", "afternoon", "evening", "night", "am", "pm",
    "booking", "service", "provider", "status", "time", "date",
}

NON_SERVICE_STOPWORDS = {
    "provider", "providers", "service", "services", "option", "options",
    "booking", "bookings", "slot", "slots", "cheapest", "nearest", "closest",
    "best", "available", "status", "one", "that", "this", "it",
    "first", "second", "third", "fourth", "fifth",
}


class EntityExtractor:
    """Pure rule-based entity extractor (no LLM / no ML)."""

    def __init__(
        self,
        service_dict: ServiceDictionary | None = None,
        timezone_name: str = "Asia/Kolkata",
    ):
        self.service_dict = service_dict or get_service_dictionary()
        self.datetime_parser = DateTimeParser(timezone_name=timezone_name)

    def extract(
        self,
        raw_text: str,
        reference_now: datetime | None = None,
        known_provider_names: list[str] | None = None,
    ) -> ExtractedEntities:
        normalized = normalize_text(raw_text)
        entities = ExtractedEntities()

        # 1. Booking ID
        entities.booking_id = extract_booking_identifier(raw_text)

        # 2. Services
        matched_services = self.service_dict.match_services(normalized)
        if matched_services:
            entities.matched_services = [s.code for s in matched_services]
            entities.service = matched_services[0].code
            entities.service_display = matched_services[0].display_name
            entities.service_category = matched_services[0].category
        else:
            entities.unknown_service_mention = self._detect_unknown_service_mention(normalized)

        # 3. Date & Time
        dt = self.datetime_parser.parse(normalized, reference_now=reference_now)
        entities.date = dt.date_str
        entities.time = dt.time_str
        entities.date_ambiguity = dt.date_ambiguity
        entities.time_ambiguity = dt.time_ambiguity
        entities.invalid_date_reason = dt.invalid_date_reason

        # 4. Location
        if re.search(r"\b(near me|around me|nearby|close to me)\b", normalized):
            entities.near_me = True

        loc = self.service_dict.match_location(normalized)
        if loc is not None:
            entities.location = loc.display_name
            entities.latitude = loc.latitude
            entities.longitude = loc.longitude
        elif not entities.near_me:
            unresolved = self._detect_unresolved_location(normalized)
            if unresolved:
                entities.unresolved_location = unresolved
                entities.location = unresolved

        # 5. Price preference & budget constraint
        if re.search(
            r"\b(cheapest|lowest\s*-?\s*priced|lowest\s+price|least\s+expensive|most\s+affordable|budget)\b",
            normalized,
        ):
            entities.price_preference = "cheapest"
            entities.constraints.append("sort:price_asc")

        m_price = re.search(
            r"\b(?:under|below|less\s+than|max|within|up\s+to)\s+(?:rs\.?|inr)?\s*(\d{2,6})\b(?!\s*km)",
            normalized,
        )
        if m_price:
            entities.max_price = float(m_price.group(1))
            entities.constraints.append(f"max_price:{entities.max_price:.0f}")

        # 6. Distance preference & radius constraint
        if re.search(r"\b(nearest|closest)\b", normalized):
            entities.distance_preference = "nearest"
            entities.constraints.append("sort:distance_asc")

        m_dist = re.search(r"\b(?:within|under|less\s+than|max)\s+(\d{1,3}(?:\.\d+)?)\s*km\b", normalized)
        if m_dist:
            entities.max_distance_km = float(m_dist.group(1))
            entities.constraints.append(f"max_distance_km:{entities.max_distance_km}")

        # 7. Rating preference
        if re.search(r"\b(highest\s*-?\s*rated|top\s*-?\s*rated|best\s*-?\s*rated|most\s+rated)\b", normalized):
            entities.rating_preference = "highest_rated"
            entities.constraints.append("sort:rating_desc")

        # 8. Availability / earliest preference
        if re.search(r"\b(earliest|soonest|first\s+available|asap)\b", normalized):
            entities.availability_preference = "earliest"
            entities.constraints.append("sort:earliest")

        # 9. Provider name match (from conversation context if available)
        if known_provider_names:
            for pname in known_provider_names:
                if pname and pname.lower() in normalized:
                    entities.provider_name = pname
                    entities.provider_reference = ProviderReference(
                        kind="name",
                        provider_name=pname,
                        raw=pname,
                    )
                    break

        # 10. Provider selection references (ordinal / qualifier / anaphoric)
        if entities.provider_reference is None:
            entities.provider_reference = self._extract_provider_reference(normalized)

        return entities

    def _detect_unresolved_location(self, normalized: str) -> str | None:
        m = re.search(
            r"\b(?:near|in|around)\s+([a-z][a-z\s]{1,28}?)(?=\s+(?:on|at|for|tomorrow|today|next|under|below|within|with|by)\b|$)",
            normalized,
        )
        if not m:
            return None
        candidate = m.group(1).strip()
        words = candidate.split()
        if not words or words[0] in NON_LOCATION_STOPWORDS or candidate in NON_LOCATION_STOPWORDS:
            return None
        # Ignore if candidate is actually a matched service or preference
        if self.service_dict.match_services(candidate):
            return None
        if len(words) > 3:
            return None
        return candidate.title()

    def _detect_unknown_service_mention(self, normalized: str) -> str | None:
        m = re.search(
            r"\b(?:find|book|need|want|search\s+for|looking\s+for|get\s+me)\s+(?:a|an|the)?\s*"
            r"([a-z]+(?:\s+[a-z]+)?)(?=\s+(?:near|in|around|for|at|tomorrow|today|on|service|provider)\b|$)",
            normalized,
        )
        if not m:
            return None
        candidate = m.group(1).strip()
        tokens = candidate.split()
        if not tokens or all(t in NON_SERVICE_STOPWORDS for t in tokens):
            return None
        if tokens[0] in ("cheapest", "nearest", "closest", "available", "best", "lowest", "my", "status"):
            return None
        # Ignore known location names
        if self.service_dict.match_location(candidate):
            return None
        return candidate

    def _extract_provider_reference(self, normalized: str) -> ProviderReference | None:
        # Ordinal provider/option reference: "second provider", "option 2", "#2", "the 1st one"
        m_ord = re.search(
            r"\b(?:the\s+)?(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th)\s+(?:provider|option|one|service|result)?\b",
            normalized,
        )
        if m_ord:
            word = m_ord.group(1)
            # Avoid matching "first available" as ordinal selection
            if word == "first" and re.search(r"\bfirst\s+available\b", normalized):
                return None
            return ProviderReference(
                kind="index",
                index=ORDINAL_MAP[word],
                raw=m_ord.group(0).strip(),
            )

        m_opt_num = re.search(r"\b(?:option|provider|number|choice)\s+(\d)\b", normalized)
        if m_opt_num:
            idx = int(m_opt_num.group(1)) - 1
            if 0 <= idx <= 9:
                return ProviderReference(kind="index", index=idx, raw=m_opt_num.group(0))

        # Qualifier selection: "book the cheapest one", "select the nearest provider"
        m_qual = re.search(
            r"\b(?:book|select|choose|pick|take|go\s+with|want)\s+(?:the\s+)?(cheapest|nearest|closest|highest\s*-?\s*rated|top\s*-?\s*rated|earliest)\s*(?:one|provider|option)?\b",
            normalized,
        )
        if m_qual:
            q = m_qual.group(1)
            if "cheap" in q:
                qual = "cheapest"
            elif "near" in q or "close" in q:
                qual = "nearest"
            elif "rated" in q:
                qual = "highest_rated"
            else:
                qual = "earliest"
            return ProviderReference(kind="qualifier", qualifier=qual, raw=m_qual.group(0))

        # Standalone qualifier reference like "the cheapest one"
        if re.fullmatch(r"(?:the\s+)?(cheapest|nearest|closest|highest\s*-?\s*rated|earliest)\s+(?:one|provider|option)", normalized):
            if "cheap" in normalized:
                return ProviderReference(kind="qualifier", qualifier="cheapest", raw=normalized)
            if "near" in normalized or "close" in normalized:
                return ProviderReference(kind="qualifier", qualifier="nearest", raw=normalized)
            if "rated" in normalized:
                return ProviderReference(kind="qualifier", qualifier="highest_rated", raw=normalized)
            return ProviderReference(kind="qualifier", qualifier="earliest", raw=normalized)

        # Anaphoric references: "that one", "this provider", "book it", "book this one"
        m_ana = re.search(
            r"\b(that\s+one|this\s+one|this\s+provider|that\s+provider|book\s+it|select\s+it|take\s+it)\b",
            normalized,
        )
        if m_ana:
            return ProviderReference(kind="anaphoric", raw=m_ana.group(1))

        return None

import re
from datetime import datetime

from app.core.entity_extractor import EntityExtractor
from app.schemas.intents import ConversationState, IntentType, ParsedIntent
from app.utils.normalization import normalize_text

CONFIRM_PATTERNS = re.compile(
    r"^(?:yes|yeah|yep|yup|sure|ok|okay|confirm|confirmed|confirm\s+booking|yes\s+confirm|yes\s+please|go\s+ahead|proceed|book\s+it|do\s+it|yes\s+book\s+it)$"
)
CANCEL_SHORT_PATTERNS = re.compile(
    r"^(?:no|nope|nah|cancel|stop|abort|never\s*mind|do\s+not\s+book|dont\s+book)$"
)
GREETING_HELP_PATTERNS = re.compile(
    r"\b(?:hi|hello|hey|good\s+morning|good\s+evening|help|what\s+can\s+you\s+do|what\s+services|supported\s+services|how\s+does\s+this\s+work|thanks|thank\s+you)\b"
)


class IntentParser:
    """
    Deterministic rule-based intent parser supporting all 13 required intents,
    confidence scoring, ambiguity detection, and conversation-state context.
    """

    def __init__(self, entity_extractor: EntityExtractor | None = None, timezone_name: str = "Asia/Kolkata"):
        self.extractor = entity_extractor or EntityExtractor(timezone_name=timezone_name)

    def parse(
        self,
        raw_text: str,
        current_state: ConversationState | None = None,
        reference_now: datetime | None = None,
        known_provider_names: list[str] | None = None,
    ) -> ParsedIntent:
        normalized = normalize_text(raw_text)
        entities = self.extractor.extract(
            raw_text,
            reference_now=reference_now,
            known_provider_names=known_provider_names,
        )

        ambiguities: list[str] = []
        if len(entities.matched_services) > 1:
            ambiguities.append(
                f"Multiple services mentioned ({', '.join(entities.matched_services)}). Please choose one service at a time."
            )
        if entities.date_ambiguity:
            ambiguities.append(entities.date_ambiguity)
        if entities.time_ambiguity:
            ambiguities.append(entities.time_ambiguity)
        if entities.invalid_date_reason:
            ambiguities.append(entities.invalid_date_reason)

        sort_by = self._determine_sort_by(entities)
        intent_type, confidence = self._classify_intent(normalized, entities, current_state)

        return ParsedIntent(
            type=intent_type,
            confidence=confidence,
            normalized_text=normalized,
            entities=entities,
            ambiguities=ambiguities,
            sort_by=sort_by,
        )

    def _determine_sort_by(self, entities) -> str:
        if entities.price_preference == "cheapest":
            return "price"
        if entities.distance_preference == "nearest" or entities.near_me:
            return "distance"
        if entities.rating_preference == "highest_rated":
            return "rating"
        if entities.availability_preference == "earliest":
            return "earliest"
        return "best"

    def _classify_intent(
        self,
        normalized: str,
        entities,
        current_state: ConversationState | None,
    ) -> tuple[IntentType, float]:
        # 1. Explicit confirmation / rejection when awaiting confirmation
        if current_state == ConversationState.AWAITING_CONFIRMATION:
            if CONFIRM_PATTERNS.match(normalized) or re.search(r"\bconfirm\b", normalized):
                return IntentType.CONFIRM_BOOKING, 0.98
            if CANCEL_SHORT_PATTERNS.match(normalized):
                return IntentType.CANCEL_BOOKING, 0.95

        # Explicit confirm phrasing even outside short reply
        if re.search(r"\b(confirm\s+(?:the\s+|my\s+)?booking|confirm\s+this|yes\s+confirm)\b", normalized):
            return IntentType.CONFIRM_BOOKING, 0.96

        # 2. Explicit cancellation
        if re.search(r"\b(cancel|abort|revoke|delete\s+booking|drop\s+booking)\b", normalized):
            return IntentType.CANCEL_BOOKING, 0.95

        # 3. Booking status check
        if re.search(
            r"\b(booking\s+status|my\s+bookings?|status\s+of|check\s+(?:my\s+)?booking|show\s+(?:my\s+)?booking|track\s+(?:my\s+)?booking|view\s+(?:my\s+)?booking)\b",
            normalized,
        ):
            return IntentType.CHECK_BOOKING_STATUS, 0.95

        # 4. Modify booking / time / date / location
        if re.search(
            r"\b(change|modify|reschedule|update|switch|move|shift)\b",
            normalized,
        ) and (
            entities.time
            or entities.date
            or entities.time_ambiguity
            or entities.date_ambiguity
            or entities.location
            or re.search(r"\b(time|date|slot|booking|location|it)\b", normalized)
        ):
            return IntentType.MODIFY_BOOKING, 0.92

        # 5. Provider selection (ordinal, anaphoric, named provider, or qualifier selection)
        if entities.provider_reference is not None:
            # Make sure "find the cheapest car wash" doesn't get misclassified as SELECT_PROVIDER
            if entities.service is None or entities.provider_reference.kind in ("index", "anaphoric", "name"):
                return IntentType.SELECT_PROVIDER, 0.94

        # 6. Price-focused search (FIND_CHEAPEST)
        if entities.price_preference == "cheapest":
            return IntentType.FIND_CHEAPEST, 0.93

        # 7. Distance-focused search (FIND_NEAREST)
        if entities.distance_preference == "nearest":
            return IntentType.FIND_NEAREST, 0.93

        # 8. Availability check
        if re.search(r"\b(available|availability|free\s+slots?|open\s+slots?|slots?\s+for)\b", normalized):
            return IntentType.CHECK_AVAILABILITY, 0.90

        # 9. Explicit booking request for a service
        if re.search(r"\b(book|schedule|reserve|appoint|appointment)\b", normalized) and (
            entities.service is not None or entities.unknown_service_mention is not None or entities.time or entities.date
        ):
            return IntentType.BOOK_SERVICE, 0.91

        # 10. Provider search / browse-all phrasing
        if re.search(r"\b(provider|providers|technician|technicians|specialist|specialists|shop|shops|center|centers|centre|centres)\b", normalized):
            return IntentType.FIND_PROVIDER, 0.88

        # 11. General service search or list-all browse
        if entities.service is not None:
            return IntentType.SEARCH_SERVICE, 0.90
        if entities.list_all:
            return IntentType.FIND_PROVIDER, 0.88

        # 12. Follow-up slot filling in an active conversation (e.g. "Tomorrow at 2 PM", "Aluva")
        if current_state in (
            ConversationState.COLLECTING_INFORMATION,
            ConversationState.OPTIONS_FOUND,
            ConversationState.PROVIDER_SELECTED,
            ConversationState.AWAITING_CONFIRMATION,
        ):
            if (
                entities.date
                or entities.time
                or entities.location
                or entities.near_me
                or entities.date_ambiguity
                or entities.time_ambiguity
                or entities.invalid_date_reason
            ):
                if current_state in (ConversationState.OPTIONS_FOUND, ConversationState.AWAITING_CONFIRMATION):
                    return IntentType.MODIFY_BOOKING, 0.84
                return IntentType.SEARCH_SERVICE, 0.85

        # 13. General greeting / help query
        if GREETING_HELP_PATTERNS.search(normalized):
            return IntentType.GENERAL_QUERY, 0.90

        # 14. Unrecognized service explicitly asked to be searched/booked
        if entities.unknown_service_mention:
            return IntentType.SEARCH_SERVICE, 0.60

        # 15. Standalone confirm/cancel fallback without active confirmation
        if CONFIRM_PATTERNS.match(normalized):
            return IntentType.CONFIRM_BOOKING, 0.70
        if CANCEL_SHORT_PATTERNS.match(normalized):
            return IntentType.CANCEL_BOOKING, 0.70

        return IntentType.UNKNOWN_INTENT, 0.25

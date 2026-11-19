import logging
from datetime import datetime
from typing import Any

from app.config import get_settings
from app.core.intent_parser import IntentParser
from app.core.service_dictionary import ServiceDictionary, get_service_dictionary
from app.repositories.conversation_repository import ConversationRecord, ConversationRepository
from app.schemas.intents import ConversationState, IntentType, ParsedIntent, ProviderReference
from app.schemas.responses import AssistantResponse, IntentSummary, ProviderOption
from app.services.backend_client import (
    BackendClientError,
    BookingConflictError,
    PriceChangedError,
    SlotUnavailableError,
)
from app.services.booking_coordinator import BookingCoordinator
from app.services.search_coordinator import SearchCoordinator

logger = logging.getLogger("ai_booking_service.conversation")


class InvalidStateTransitionError(Exception):
    def __init__(self, current: ConversationState, target: ConversationState):
        super().__init__(f"Invalid conversation state transition: {current.value} -> {target.value}")
        self.current = current
        self.target = target


ALLOWED_TRANSITIONS: dict[ConversationState, set[ConversationState]] = {
    ConversationState.INITIAL: {
        ConversationState.INITIAL,
        ConversationState.COLLECTING_INFORMATION,
        ConversationState.READY_TO_SEARCH,
        ConversationState.CANCELLED,
        ConversationState.EXPIRED,
    },
    ConversationState.COLLECTING_INFORMATION: {
        ConversationState.COLLECTING_INFORMATION,
        ConversationState.READY_TO_SEARCH,
        ConversationState.CANCELLED,
        ConversationState.EXPIRED,
    },
    ConversationState.READY_TO_SEARCH: {
        ConversationState.SEARCHING,
        ConversationState.COLLECTING_INFORMATION,
        ConversationState.CANCELLED,
        ConversationState.EXPIRED,
    },
    ConversationState.SEARCHING: {
        ConversationState.OPTIONS_FOUND,
        ConversationState.COLLECTING_INFORMATION,
        ConversationState.CANCELLED,
        ConversationState.EXPIRED,
    },
    ConversationState.OPTIONS_FOUND: {
        ConversationState.OPTIONS_FOUND,
        ConversationState.PROVIDER_SELECTED,
        ConversationState.AWAITING_CONFIRMATION,
        ConversationState.READY_TO_SEARCH,
        ConversationState.COLLECTING_INFORMATION,
        ConversationState.CANCELLED,
        ConversationState.EXPIRED,
    },
    ConversationState.PROVIDER_SELECTED: {
        ConversationState.AWAITING_CONFIRMATION,
        ConversationState.OPTIONS_FOUND,
        ConversationState.READY_TO_SEARCH,
        ConversationState.COLLECTING_INFORMATION,
        ConversationState.CANCELLED,
        ConversationState.EXPIRED,
    },
    ConversationState.AWAITING_CONFIRMATION: {
        ConversationState.BOOKING_IN_PROGRESS,
        ConversationState.AWAITING_CONFIRMATION,
        ConversationState.READY_TO_SEARCH,
        ConversationState.OPTIONS_FOUND,
        ConversationState.COLLECTING_INFORMATION,
        ConversationState.CANCELLED,
        ConversationState.EXPIRED,
    },
    ConversationState.BOOKING_IN_PROGRESS: {
        ConversationState.BOOKING_CONFIRMED,
        ConversationState.BOOKING_FAILED,
        ConversationState.AWAITING_CONFIRMATION,
    },
    ConversationState.BOOKING_CONFIRMED: {
        ConversationState.BOOKING_CONFIRMED,
        ConversationState.CANCELLED,
        ConversationState.INITIAL,
        ConversationState.COLLECTING_INFORMATION,
        ConversationState.READY_TO_SEARCH,
    },
    ConversationState.BOOKING_FAILED: {
        ConversationState.OPTIONS_FOUND,
        ConversationState.READY_TO_SEARCH,
        ConversationState.COLLECTING_INFORMATION,
        ConversationState.CANCELLED,
    },
    ConversationState.CANCELLED: {
        ConversationState.CANCELLED,
        ConversationState.INITIAL,
        ConversationState.COLLECTING_INFORMATION,
        ConversationState.READY_TO_SEARCH,
    },
    ConversationState.EXPIRED: {
        ConversationState.EXPIRED,
    },
}


def validate_transition(current: ConversationState, target: ConversationState) -> None:
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidStateTransitionError(current, target)


def _format_price(price: float, currency: str = "INR") -> str:
    symbol = "₹" if currency.upper() == "INR" else f"{currency} "
    if float(price).is_integer():
        return f"{symbol}{int(price)}"
    return f"{symbol}{price:.2f}"


def _format_time_display(hhmm: str | None) -> str:
    if not hhmm:
        return ""
    try:
        hour, minute = map(int, hhmm.split(":"))
        suffix = "AM" if hour < 12 else "PM"
        display_hour = hour % 12 or 12
        return f"{display_hour}:{minute:02d} {suffix}"
    except Exception:
        return hhmm


class ConversationManager:
    """
    Stateful workflow engine managing multi-turn booking conversations via
    persistent storage and strict FSM transitions.
    """

    def __init__(
        self,
        repository: ConversationRepository,
        search_coordinator: SearchCoordinator,
        booking_coordinator: BookingCoordinator,
        intent_parser: IntentParser | None = None,
        service_dict: ServiceDictionary | None = None,
    ):
        settings = get_settings()
        self.repository = repository
        self.search_coordinator = search_coordinator
        self.booking_coordinator = booking_coordinator
        self.service_dict = service_dict or get_service_dictionary()
        self.intent_parser = intent_parser or IntentParser(timezone_name=settings.default_timezone)

    def _transition(self, record: ConversationRecord, target: ConversationState) -> None:
        validate_transition(record.state, target)
        record.state = target

    @staticmethod
    def _build_intent_summary(parsed: ParsedIntent, context: dict[str, Any]) -> IntentSummary:
        return IntentSummary(
            type=parsed.type,
            confidence=parsed.confidence,
            service=parsed.entities.service or context.get("service"),
            service_category=parsed.entities.service_category or context.get("service_category"),
            location=parsed.entities.location or context.get("location"),
            date=parsed.entities.date or context.get("date"),
            time=parsed.entities.time or context.get("time"),
            sort_by=context.get("sort_by") or parsed.sort_by,
            booking_id=parsed.entities.booking_id or context.get("confirmed_booking_code"),
            ambiguities=parsed.ambiguities,
        )

    @staticmethod
    def _append_history(context: dict[str, Any], role: str, text: str) -> None:
        history = list(context.get("history") or [])
        history.append({"role": role, "text": text})
        context["history"] = history[-20:]

    async def handle_message(
        self,
        *,
        message: str,
        authorization: str,
        conversation_id: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        request_id: str | None = None,
        reference_now: datetime | None = None,
    ) -> AssistantResponse:
        # 1. Load or create persistent conversation (ownership enforced by backend)
        if conversation_id:
            record = await self.repository.get(
                conversation_id=conversation_id,
                authorization=authorization,
                request_id=request_id,
            )
            if record.is_expired or record.state == ConversationState.EXPIRED:
                if record.state != ConversationState.EXPIRED:
                    record.state = ConversationState.EXPIRED
                    await self.repository.save(
                        record, authorization=authorization, request_id=request_id, refresh_ttl=False
                    )
                return AssistantResponse(
                    success=False,
                    conversation_id=record.conversation_id,
                    status=ConversationState.EXPIRED.value,
                    message="This conversation has expired. Please start a new message to begin a fresh booking.",
                    error_code="CONVERSATION_EXPIRED",
                )
        else:
            record = await self.repository.create(
                authorization=authorization,
                initial_context={},
                request_id=request_id,
            )

        context = record.context
        known_names = [
            p.get("provider_name", "")
            for p in (context.get("search_results") or [])
            if isinstance(p, dict)
        ]

        # 2. Parse intent & entities deterministically
        parsed = self.intent_parser.parse(
            message,
            current_state=record.state,
            reference_now=reference_now,
            known_provider_names=known_names,
        )
        self._append_history(context, "customer", message)
        context["extracted_intent"] = parsed.type.value

        # Inject client GPS coordinates if provided
        if latitude is not None and longitude is not None:
            context["latitude"] = latitude
            context["longitude"] = longitude
            if parsed.entities.near_me and not context.get("location"):
                context["location"] = "Current Location"

        # 3. Handle ambiguities & invalid dates explicitly (never fabricate values)
        if parsed.entities.invalid_date_reason:
            reply = parsed.entities.invalid_date_reason
            self._append_history(context, "assistant", reply)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=False,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=reply,
                intent=self._build_intent_summary(parsed, context),
                missing_fields=["date"],
                error_code="INVALID_DATE",
            )

        if len(parsed.entities.matched_services) > 1:
            names = [
                self.service_dict.services[c].display_name
                for c in parsed.entities.matched_services
                if c in self.service_dict.services
            ]
            reply = (
                f"You mentioned multiple services ({', '.join(names)}). "
                "Please tell me which single service you would like to book first."
            )
            self._append_history(context, "assistant", reply)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=False,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=reply,
                intent=self._build_intent_summary(parsed, context),
                missing_fields=["service"],
                error_code="AMBIGUOUS_SERVICE",
            )

        # 4. Route by intent
        if parsed.type == IntentType.GENERAL_QUERY:
            supported = ", ".join(self.service_dict.list_supported_service_names())
            reply = (
                f"Hello! I can help you find, compare, and book local services: {supported}. "
                "Try saying 'Find the cheapest car wash near Aluva tomorrow at 2 PM' or 'Show my booking status'."
            )
            self._append_history(context, "assistant", reply)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=True,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=reply,
                intent=self._build_intent_summary(parsed, context),
                data={"supported_services": self.service_dict.list_supported_service_names()},
            )

        if parsed.type == IntentType.UNKNOWN_INTENT:
            supported = ", ".join(self.service_dict.list_supported_service_names())
            reply = (
                "I didn't quite understand that booking request. "
                f"I currently support: {supported}. "
                "You can ask to search providers, compare prices, book a slot, or check booking status."
            )
            self._append_history(context, "assistant", reply)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=False,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=reply,
                intent=self._build_intent_summary(parsed, context),
                error_code="UNKNOWN_INTENT",
            )

        if parsed.type == IntentType.CHECK_BOOKING_STATUS:
            return await self._handle_booking_status(record, parsed, authorization, request_id)

        if parsed.type == IntentType.CANCEL_BOOKING:
            return await self._handle_cancel_intent(record, parsed, authorization, request_id)

        if parsed.type == IntentType.CONFIRM_BOOKING:
            return await self.confirm_booking(
                conversation_id=record.conversation_id,
                confirm=True,
                authorization=authorization,
                request_id=request_id,
                parsed_intent=parsed,
                loaded_record=record,
            )

        if parsed.type == IntentType.SELECT_PROVIDER:
            return await self._handle_select_provider(record, parsed, authorization, request_id)

        if parsed.type == IntentType.MODIFY_BOOKING:
            return await self._handle_modify_booking(record, parsed, authorization, request_id)

        # Search / Find / Availability / Book flows
        return await self._handle_search_or_collect(record, parsed, authorization, request_id)

    async def _handle_booking_status(
        self,
        record: ConversationRecord,
        parsed: ParsedIntent,
        authorization: str,
        request_id: str | None,
    ) -> AssistantResponse:
        context = record.context
        target_id = parsed.entities.booking_id or context.get("confirmed_booking_id")
        bookings = await self.booking_coordinator.get_booking_status(
            booking_id=parsed.entities.booking_id,
            authorization=authorization,
            request_id=request_id,
        )
        if not bookings and target_id:
            bookings = await self.booking_coordinator.get_booking_status(
                booking_id=target_id,
                authorization=authorization,
                request_id=request_id,
            )

        if not bookings:
            reply = "You do not have any bookings yet."
        elif len(bookings) == 1:
            b = bookings[0]
            reply = (
                f"Booking {b['booking_code']} is {b['status']}: {b['service_name']} with "
                f"{b['provider_name']} on {b['service_date']} at {_format_time_display(b['start_time'])} "
                f"for {_format_price(b['booked_price'], b['currency'])}."
            )
        else:
            latest = bookings[0]
            reply = (
                f"You have {len(bookings)} recent bookings. Latest: {latest['booking_code']} "
                f"({latest['status']}) — {latest['service_name']} with {latest['provider_name']} "
                f"on {latest['service_date']} at {_format_time_display(latest['start_time'])}."
            )

        self._append_history(context, "assistant", reply)
        await self.repository.save(record, authorization=authorization, request_id=request_id)
        return AssistantResponse(
            success=True,
            conversation_id=record.conversation_id,
            status=record.state.value,
            message=reply,
            intent=self._build_intent_summary(parsed, context),
            data={"bookings": bookings},
        )

    async def _handle_cancel_intent(
        self,
        record: ConversationRecord,
        parsed: ParsedIntent,
        authorization: str,
        request_id: str | None,
    ) -> AssistantResponse:
        return await self.cancel_booking(
            conversation_id=record.conversation_id,
            booking_id=parsed.entities.booking_id,
            reason="Cancelled via conversational assistant",
            authorization=authorization,
            request_id=request_id,
            parsed_intent=parsed,
        )

    async def _handle_modify_booking(
        self,
        record: ConversationRecord,
        parsed: ParsedIntent,
        authorization: str,
        request_id: str | None,
    ) -> AssistantResponse:
        context = record.context
        if parsed.entities.time_ambiguity or parsed.entities.date_ambiguity:
            msg = parsed.entities.time_ambiguity or parsed.entities.date_ambiguity or "Please clarify the new date or time."
            self._append_history(context, "assistant", msg)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=False,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=msg,
                intent=self._build_intent_summary(parsed, context),
                missing_fields=["time"] if parsed.entities.time_ambiguity else ["date"],
                error_code="AMBIGUOUS_DATETIME",
            )

        updated_fields: list[str] = []
        if parsed.entities.date:
            context["date"] = parsed.entities.date
            updated_fields.append(f"date to {parsed.entities.date}")
        if parsed.entities.time:
            context["time"] = parsed.entities.time
            updated_fields.append(f"time to {_format_time_display(parsed.entities.time)}")
        if parsed.entities.location:
            context["location"] = parsed.entities.location
            context["latitude"] = parsed.entities.latitude
            context["longitude"] = parsed.entities.longitude
            updated_fields.append(f"location to {parsed.entities.location}")
        if parsed.entities.service:
            context["service"] = parsed.entities.service
            context["service_display"] = parsed.entities.service_display
            context["service_category"] = parsed.entities.service_category
            updated_fields.append(f"service to {parsed.entities.service_display}")

        if not updated_fields:
            reply = "What would you like to change (for example, 'change the time to 4 PM' or 'change date to tomorrow')?"
            self._append_history(context, "assistant", reply)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=False,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=reply,
                intent=self._build_intent_summary(parsed, context),
                missing_fields=["date", "time"],
                error_code="MISSING_MODIFICATION_DETAILS",
            )

        # Clear previously locked slot since schedule/criteria changed
        prev_selected = context.get("selected_provider")
        context["selected_provider"] = None

        # If a service is already known and date/time are present, re-search immediately
        if context.get("service") and context.get("date") and context.get("time"):
            return await self._execute_search(
                record,
                parsed,
                authorization,
                request_id,
                prefix_note=f"Updated {', '.join(updated_fields)}. ",
                preferred_provider_id=prev_selected.get("provider_id") if isinstance(prev_selected, dict) else None,
            )

        if record.state != ConversationState.COLLECTING_INFORMATION:
            self._transition(record, ConversationState.COLLECTING_INFORMATION)
        missing = self._compute_missing_fields(context)
        reply = f"Updated {', '.join(updated_fields)}. Please provide: {', '.join(missing)}."
        self._append_history(context, "assistant", reply)
        await self.repository.save(record, authorization=authorization, request_id=request_id)
        return AssistantResponse(
            success=True,
            conversation_id=record.conversation_id,
            status=record.state.value,
            message=reply,
            intent=self._build_intent_summary(parsed, context),
            missing_fields=missing,
        )

    async def _handle_search_or_collect(
        self,
        record: ConversationRecord,
        parsed: ParsedIntent,
        authorization: str,
        request_id: str | None,
    ) -> AssistantResponse:
        context = record.context

        # If conversation was previously finished/cancelled, reset booking slots cleanly for a fresh search
        if record.state in (
            ConversationState.BOOKING_CONFIRMED,
            ConversationState.CANCELLED,
            ConversationState.BOOKING_FAILED,
        ) and parsed.entities.service:
            context.pop("search_results", None)
            context.pop("selected_provider", None)
            context.pop("confirmed_booking_id", None)
            context.pop("confirmed_booking_code", None)
            context.pop("date", None)
            context.pop("time", None)
            context.pop("location", None)
            context.pop("latitude", None)
            context.pop("longitude", None)

        # Handle explicitly mentioned unknown service (e.g., "Find a dog walker")
        if parsed.entities.unknown_service_mention and not parsed.entities.service and not context.get("service"):
            supported = ", ".join(self.service_dict.list_supported_service_names())
            reply = (
                f"Sorry, we don't currently offer '{parsed.entities.unknown_service_mention}'. "
                f"Available services are: {supported}."
            )
            self._append_history(context, "assistant", reply)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=False,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=reply,
                intent=self._build_intent_summary(parsed, context),
                missing_fields=["service"],
                error_code="UNKNOWN_SERVICE",
            )

        # Merge newly extracted entities into persistent context
        if parsed.entities.service:
            context["service"] = parsed.entities.service
            context["service_display"] = parsed.entities.service_display
            context["service_category"] = parsed.entities.service_category
        if parsed.entities.location:
            context["location"] = parsed.entities.location
            context["latitude"] = parsed.entities.latitude
            context["longitude"] = parsed.entities.longitude
            context["unresolved_location"] = parsed.entities.unresolved_location
        if parsed.entities.date:
            context["date"] = parsed.entities.date
        if parsed.entities.time:
            context["time"] = parsed.entities.time
        if parsed.entities.max_price is not None:
            context["max_price"] = parsed.entities.max_price
        if parsed.entities.max_distance_km is not None:
            context["max_distance_km"] = parsed.entities.max_distance_km

        # Preserve explicit ranking preference across turns
        if parsed.sort_by != "best" or "sort_by" not in context:
            context["sort_by"] = parsed.sort_by

        # If ambiguous date/time was given (e.g., "morning", "next week", "at 4"), ask for clarification
        if parsed.entities.date_ambiguity or parsed.entities.time_ambiguity:
            self._transition(record, ConversationState.COLLECTING_INFORMATION)
            missing = self._compute_missing_fields(context)
            clarification = " ".join(
                part for part in (parsed.entities.date_ambiguity, parsed.entities.time_ambiguity) if part
            )
            self._append_history(context, "assistant", clarification)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=True,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=clarification,
                intent=self._build_intent_summary(parsed, context),
                missing_fields=missing,
                error_code="AMBIGUOUS_DATETIME",
            )

        # If user asked "near me" without GPS coordinates and without a city name
        if parsed.entities.near_me and context.get("latitude") is None and not context.get("location"):
            self._transition(record, ConversationState.COLLECTING_INFORMATION)
            missing = self._compute_missing_fields(context)
            if "location" not in missing:
                missing.insert(0, "location")
            reply = (
                "Please share your location or area name (for example, Aluva, Kochi, Kakkanad, or Edappally) "
                "so I can find nearby providers."
            )
            self._append_history(context, "assistant", reply)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=True,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=reply,
                intent=self._build_intent_summary(parsed, context),
                missing_fields=missing,
            )

        missing = self._compute_missing_fields(context)

        # If service is present and date+time are present -> run full availability search!
        if not missing:
            return await self._execute_search(record, parsed, authorization, request_id)

        # Otherwise collect missing fields cleanly (with preview count if service is known)
        self._transition(record, ConversationState.COLLECTING_INFORMATION)
        preview_providers: list[ProviderOption] = []
        if context.get("service"):
            preview_providers = await self.search_coordinator.search_and_rank(
                service_code=context.get("service"),
                location_name=context.get("location"),
                latitude=context.get("latitude"),
                longitude=context.get("longitude"),
                date=context.get("date"),
                start_time=context.get("time"),
                sort_by=context.get("sort_by", "best"),
                max_price=context.get("max_price"),
                max_distance_km=context.get("max_distance_km"),
                request_id=request_id,
            )

        reply = self._build_missing_fields_prompt(context, missing, len(preview_providers))
        self._append_history(context, "assistant", reply)
        await self.repository.save(record, authorization=authorization, request_id=request_id)
        return AssistantResponse(
            success=True,
            conversation_id=record.conversation_id,
            status=record.state.value,
            message=reply,
            intent=self._build_intent_summary(parsed, context),
            data={"preview_provider_count": len(preview_providers)} if context.get("service") else {},
            missing_fields=missing,
        )

    def _compute_missing_fields(self, context: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        if not context.get("service"):
            missing.append("service")
        if not context.get("date"):
            missing.append("date")
        if not context.get("time"):
            missing.append("time")
        return missing

    def _build_missing_fields_prompt(
        self,
        context: dict[str, Any],
        missing: list[str],
        preview_count: int,
    ) -> str:
        svc_name = context.get("service_display") or "service"
        loc = context.get("location")
        loc_part = f" near {loc}" if loc else ""

        if "service" in missing:
            supported = ", ".join(self.service_dict.list_supported_service_names())
            return f"Which service are you looking for? We currently offer: {supported}."

        if preview_count > 0:
            intro = f"I found {preview_count} available {svc_name.lower()} provider{'s' if preview_count != 1 else ''}{loc_part}. "
        else:
            intro = f"Got it — {svc_name}{loc_part}. "

        if "date" in missing and "time" in missing:
            return intro + "What date and time do you prefer? (e.g., 'Tomorrow at 2 PM')"
        if "date" in missing:
            return intro + "What date do you prefer? (e.g., 'Today' or 'Tomorrow')"
        if "time" in missing:
            return intro + "What time do you prefer? (e.g., '2 PM' or '14:00')"
        return intro + "Please provide the remaining booking details."

    async def _execute_search(
        self,
        record: ConversationRecord,
        parsed: ParsedIntent,
        authorization: str,
        request_id: str | None,
        prefix_note: str = "",
        preferred_provider_id: str | None = None,
    ) -> AssistantResponse:
        context = record.context
        self._transition(record, ConversationState.READY_TO_SEARCH)
        self._transition(record, ConversationState.SEARCHING)

        sort_by = context.get("sort_by") or parsed.sort_by or "best"
        providers = await self.search_coordinator.search_and_rank(
            service_code=context.get("service"),
            location_name=context.get("location"),
            latitude=context.get("latitude"),
            longitude=context.get("longitude"),
            date=context.get("date"),
            start_time=context.get("time"),
            sort_by=sort_by,
            max_price=context.get("max_price"),
            max_distance_km=context.get("max_distance_km"),
            request_id=request_id,
        )

        if not providers:
            self._transition(record, ConversationState.COLLECTING_INFORMATION)
            context["search_results"] = []
            loc_str = f" near {context['location']}" if context.get("location") else ""
            reply = (
                f"{prefix_note}I couldn't find available {context.get('service_display', 'service')} providers"
                f"{loc_str} on {context.get('date')} at {_format_time_display(context.get('time'))}. "
                "Would you like to try a different time, date, or nearby area?"
            )
            self._append_history(context, "assistant", reply)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=False,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=reply,
                intent=self._build_intent_summary(parsed, context),
                data={"providers": []},
                error_code="NO_PROVIDERS_FOUND",
            )

        self._transition(record, ConversationState.OPTIONS_FOUND)
        serialized = [p.model_dump() for p in providers]
        context["search_results"] = serialized

        # If user modified time after selecting a specific provider and that provider is available, highlight it
        top = providers[0]
        if preferred_provider_id:
            for item in providers:
                if item.provider_id == preferred_provider_id:
                    top = item
                    break

        count = len(providers)
        price_txt = _format_price(top.price, top.currency)
        time_txt = _format_time_display(top.start_time or context.get("time"))

        if sort_by == "price":
            lead = f"The cheapest option is {top.provider_name} for {price_txt}"
        elif sort_by == "distance":
            dist_txt = f" ({top.distance_km:.2f} km away)" if top.distance_km is not None else ""
            lead = f"The nearest option is {top.provider_name}{dist_txt} for {price_txt}"
        elif sort_by == "rating":
            rating_txt = f" (rated {top.rating:.1f}/5)" if top.rating is not None else ""
            lead = f"The highest-rated option is {top.provider_name}{rating_txt} for {price_txt}"
        else:
            lead = f"The top match is {top.provider_name} for {price_txt}"

        reply = (
            f"{prefix_note}I found {count} available provider{'s' if count != 1 else ''} "
            f"for {context.get('date')} at {time_txt}. {lead}. "
            "Say 'Book that one' or pick a provider (e.g., 'Book the second provider') to proceed."
        )

        self._append_history(context, "assistant", reply)
        await self.repository.save(record, authorization=authorization, request_id=request_id)
        return AssistantResponse(
            success=True,
            conversation_id=record.conversation_id,
            status=record.state.value,
            message=reply,
            intent=self._build_intent_summary(parsed, context),
            data={"providers": serialized, "sort_by": sort_by},
            requires_confirmation=False,
            missing_fields=[],
        )

    def _resolve_provider_reference(
        self,
        ref: ProviderReference,
        results: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not results:
            return None

        if ref.kind == "index" and ref.index is not None:
            if 0 <= ref.index < len(results):
                return results[ref.index]
            return None

        if ref.kind == "anaphoric":
            return results[0]

        if ref.kind == "name" and ref.provider_name:
            target = ref.provider_name.lower()
            for item in results:
                if target in str(item.get("provider_name", "")).lower():
                    return item
            return None

        if ref.kind == "qualifier":
            if ref.qualifier == "cheapest":
                return min(results, key=lambda r: float(r.get("price", float("inf"))))
            if ref.qualifier == "nearest":
                with_dist = [r for r in results if r.get("distance_km") is not None]
                return min(with_dist, key=lambda r: float(r["distance_km"])) if with_dist else results[0]
            if ref.qualifier == "highest_rated":
                return max(results, key=lambda r: float(r.get("rating") or -1.0))
            if ref.qualifier == "earliest":
                return min(
                    results,
                    key=lambda r: (str(r.get("slot_date") or "9999-12-31"), str(r.get("start_time") or "23:59")),
                )

        return None

    async def _handle_select_provider(
        self,
        record: ConversationRecord,
        parsed: ParsedIntent,
        authorization: str,
        request_id: str | None,
    ) -> AssistantResponse:
        context = record.context
        results = context.get("search_results") or []

        if not results or record.state not in (
            ConversationState.OPTIONS_FOUND,
            ConversationState.PROVIDER_SELECTED,
            ConversationState.AWAITING_CONFIRMATION,
        ):
            reply = "Please search for a service first so I can show you available providers to choose from."
            self._append_history(context, "assistant", reply)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=False,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=reply,
                intent=self._build_intent_summary(parsed, context),
                error_code="NO_OPTIONS_TO_SELECT",
            )

        ref = parsed.entities.provider_reference
        chosen = self._resolve_provider_reference(ref, results) if ref else None
        if not chosen:
            reply = (
                f"I couldn't find that provider option. Please pick a number between 1 and {len(results)}, "
                "or say 'Book that one' or 'Book the cheapest one'."
            )
            self._append_history(context, "assistant", reply)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=False,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=reply,
                intent=self._build_intent_summary(parsed, context),
                data={"providers": results},
                error_code="INVALID_PROVIDER_SELECTION",
            )

        # Live availability & price verification against main_backend
        avail = await self.booking_coordinator.verify_slot_availability(
            provider_service_id=chosen["provider_service_id"],
            slot_id=chosen.get("slot_id"),
            date=chosen.get("slot_date") or context.get("date"),
            start_time=chosen.get("start_time") or context.get("time"),
            request_id=request_id,
        )
        if not avail.get("available"):
            reply = (
                f"{chosen['provider_name']} is no longer available for that slot "
                f"({avail.get('reason', 'slot taken')}). Please choose another provider or time."
            )
            self._append_history(context, "assistant", reply)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=False,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=reply,
                intent=self._build_intent_summary(parsed, context),
                data={"providers": results},
                error_code="SLOT_UNAVAILABLE",
            )

        current_price = float(avail.get("current_price", chosen["price"]))
        selected = {
            "provider_id": chosen["provider_id"],
            "provider_name": chosen["provider_name"],
            "service_name": chosen.get("service_name") or context.get("service_display") or "Service",
            "provider_service_id": chosen["provider_service_id"],
            "slot_id": avail.get("slot_id") or chosen["slot_id"],
            "slot_date": avail.get("slot_date") or chosen.get("slot_date") or context.get("date"),
            "start_time": avail.get("start_time") or chosen.get("start_time") or context.get("time"),
            "end_time": avail.get("end_time") or chosen.get("end_time"),
            "expected_price": current_price,
            "currency": avail.get("currency", chosen.get("currency", "INR")),
        }
        context["selected_provider"] = selected

        if record.state == ConversationState.OPTIONS_FOUND:
            self._transition(record, ConversationState.PROVIDER_SELECTED)
            self._transition(record, ConversationState.AWAITING_CONFIRMATION)
        elif record.state == ConversationState.PROVIDER_SELECTED:
            self._transition(record, ConversationState.AWAITING_CONFIRMATION)
        elif record.state == ConversationState.AWAITING_CONFIRMATION:
            self._transition(record, ConversationState.AWAITING_CONFIRMATION)

        price_txt = _format_price(selected["expected_price"], selected["currency"])
        time_txt = _format_time_display(selected["start_time"])
        reply = (
            f"Do you want to confirm {selected['provider_name']} for {selected['service_name']} "
            f"on {selected['slot_date']} at {time_txt} for {price_txt}? Reply 'Yes' to confirm or 'Cancel' to stop."
        )

        self._append_history(context, "assistant", reply)
        await self.repository.save(record, authorization=authorization, request_id=request_id)
        return AssistantResponse(
            success=True,
            conversation_id=record.conversation_id,
            status=record.state.value,
            message=reply,
            intent=self._build_intent_summary(parsed, context),
            data={"selected_provider": selected},
            requires_confirmation=True,
            missing_fields=[],
        )

    async def confirm_booking(
        self,
        *,
        conversation_id: str,
        confirm: bool,
        authorization: str,
        request_id: str | None = None,
        parsed_intent: ParsedIntent | None = None,
        loaded_record: ConversationRecord | None = None,
    ) -> AssistantResponse:
        record = loaded_record or await self.repository.get(
            conversation_id=conversation_id,
            authorization=authorization,
            request_id=request_id,
        )
        context = record.context

        if record.is_expired or record.state == ConversationState.EXPIRED:
            if record.state != ConversationState.EXPIRED:
                record.state = ConversationState.EXPIRED
                await self.repository.save(
                    record, authorization=authorization, request_id=request_id, refresh_ttl=False
                )
            return AssistantResponse(
                success=False,
                conversation_id=record.conversation_id,
                status=ConversationState.EXPIRED.value,
                message="This conversation has expired. Please start a new search.",
                error_code="CONVERSATION_EXPIRED",
            )

        # Idempotent handling if already confirmed
        if record.state == ConversationState.BOOKING_CONFIRMED and context.get("confirmed_booking"):
            existing_b = context["confirmed_booking"]
            return AssistantResponse(
                success=True,
                conversation_id=record.conversation_id,
                status=ConversationState.BOOKING_CONFIRMED.value,
                message=(
                    f"Your booking ({existing_b['booking_code']}) is already confirmed with "
                    f"{existing_b['provider_name']}."
                ),
                data={"booking": existing_b, "idempotent_replay": True},
                requires_confirmation=False,
            )

        if record.state != ConversationState.AWAITING_CONFIRMATION:
            return AssistantResponse(
                success=False,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message="There is no pending provider selection awaiting confirmation.",
                error_code="INVALID_STATE_TRANSITION",
            )

        if not confirm:
            self._transition(record, ConversationState.CANCELLED)
            reply = "Booking confirmation was declined. Let me know if you'd like to search again."
            self._append_history(context, "assistant", reply)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=True,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=reply,
                requires_confirmation=False,
            )

        selected = context.get("selected_provider")
        if not isinstance(selected, dict) or not selected.get("provider_service_id") or not selected.get("slot_id"):
            return AssistantResponse(
                success=False,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message="Selected provider details are incomplete. Please select a provider again.",
                error_code="INCOMPLETE_SELECTION",
            )

        # Move to BOOKING_IN_PROGRESS and invoke atomic backend booking API
        self._transition(record, ConversationState.BOOKING_IN_PROGRESS)
        await self.repository.save(record, authorization=authorization, request_id=request_id)

        try:
            booking = await self.booking_coordinator.create_booking(
                user_id=record.user_id,
                conversation_id=record.conversation_id,
                provider_service_id=selected["provider_service_id"],
                slot_id=selected["slot_id"],
                expected_price=float(selected["expected_price"]),
                authorization=authorization,
                request_id=request_id,
                notes=f"Booked via AI assistant conversation {record.conversation_id}",
            )
        except PriceChangedError as exc:
            # Update expected price and ask explicit customer re-confirmation
            if exc.current_price is not None:
                selected["expected_price"] = exc.current_price
                context["selected_provider"] = selected
            self._transition(record, ConversationState.AWAITING_CONFIRMATION)
            new_price_txt = (
                _format_price(exc.current_price, exc.currency)
                if exc.current_price is not None
                else "the updated price"
            )
            reply = (
                f"The price for {selected['provider_name']} changed to {new_price_txt}. "
                f"Do you want to confirm the booking at {new_price_txt}?"
            )
            self._append_history(context, "assistant", reply)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=False,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=reply,
                intent=self._build_intent_summary(parsed_intent, context) if parsed_intent else None,
                data={
                    "selected_provider": selected,
                    "expected_price": exc.expected_price,
                    "current_price": exc.current_price,
                },
                requires_confirmation=True,
                error_code="PRICE_CHANGED",
            )
        except (SlotUnavailableError, BookingConflictError) as exc:
            self._transition(record, ConversationState.BOOKING_FAILED)
            reply = (
                f"Unable to complete your booking: {exc.message} "
                "Please choose a different time slot or another provider."
            )
            self._append_history(context, "assistant", reply)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=False,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=reply,
                intent=self._build_intent_summary(parsed_intent, context) if parsed_intent else None,
                error_code=exc.code,
            )
        except BackendClientError as exc:
            self._transition(record, ConversationState.BOOKING_FAILED)
            reply = f"Booking could not be confirmed right now: {exc.message}"
            self._append_history(context, "assistant", reply)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=False,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=reply,
                intent=self._build_intent_summary(parsed_intent, context) if parsed_intent else None,
                error_code=exc.code,
            )

        # Success!
        self._transition(record, ConversationState.BOOKING_CONFIRMED)
        context["confirmed_booking_id"] = booking["booking_id"]
        context["confirmed_booking_code"] = booking["booking_code"]
        context["confirmed_booking"] = booking
        reply = (
            f"Your booking is confirmed! Booking code: {booking['booking_code']} — "
            f"{booking['service_name']} with {booking['provider_name']} on {booking['service_date']} "
            f"at {_format_time_display(booking['start_time'])} for "
            f"{_format_price(booking['booked_price'], booking['currency'])}."
        )
        self._append_history(context, "assistant", reply)
        await self.repository.save(record, authorization=authorization, request_id=request_id)
        return AssistantResponse(
            success=True,
            conversation_id=record.conversation_id,
            status=record.state.value,
            message=reply,
            intent=self._build_intent_summary(parsed_intent, context) if parsed_intent else None,
            data={"booking": booking},
            requires_confirmation=False,
            missing_fields=[],
        )

    async def cancel_booking(
        self,
        *,
        authorization: str,
        conversation_id: str | None = None,
        booking_id: str | None = None,
        reason: str | None = None,
        request_id: str | None = None,
        parsed_intent: ParsedIntent | None = None,
    ) -> AssistantResponse:
        record: ConversationRecord | None = None
        if conversation_id:
            record = await self.repository.get(
                conversation_id=conversation_id,
                authorization=authorization,
                request_id=request_id,
            )

        target_booking_id = booking_id or (
            record.context.get("confirmed_booking_id") if record else None
        )

        # Case 1: Cancel an actual confirmed booking in main_backend
        if target_booking_id:
            cancelled = await self.booking_coordinator.cancel_booking(
                booking_id=target_booking_id,
                reason=reason,
                authorization=authorization,
                request_id=request_id,
            )
            reply = (
                f"Booking {cancelled['booking_code']} with {cancelled['provider_name']} "
                "has been cancelled."
            )
            if record:
                if record.state != ConversationState.EXPIRED:
                    self._transition(record, ConversationState.CANCELLED)
                record.context["confirmed_booking"] = cancelled
                self._append_history(record.context, "assistant", reply)
                await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=True,
                conversation_id=record.conversation_id if record else None,
                status=ConversationState.CANCELLED.value,
                message=reply,
                intent=self._build_intent_summary(parsed_intent, record.context)
                if (parsed_intent and record)
                else None,
                data={"booking": cancelled},
                requires_confirmation=False,
            )

        # Case 2: Cancel an in-progress conversation before booking creation
        if record:
            if record.state == ConversationState.EXPIRED:
                return AssistantResponse(
                    success=False,
                    conversation_id=record.conversation_id,
                    status=ConversationState.EXPIRED.value,
                    message="This conversation has already expired.",
                    error_code="CONVERSATION_EXPIRED",
                )
            self._transition(record, ConversationState.CANCELLED)
            record.context["selected_provider"] = None
            reply = "Your pending booking request has been cancelled."
            self._append_history(record.context, "assistant", reply)
            await self.repository.save(record, authorization=authorization, request_id=request_id)
            return AssistantResponse(
                success=True,
                conversation_id=record.conversation_id,
                status=record.state.value,
                message=reply,
                intent=self._build_intent_summary(parsed_intent, record.context) if parsed_intent else None,
                requires_confirmation=False,
            )

        return AssistantResponse(
            success=False,
            conversation_id=None,
            status="ERROR",
            message="Please specify a booking_id or conversation_id to cancel.",
            error_code="MISSING_CANCELLATION_TARGET",
        )

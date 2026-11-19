import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import dateparser

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

VAGUE_TIME_WORDS = {
    "morning": "morning (e.g., 10:00 AM)",
    "afternoon": "afternoon (e.g., 2:00 PM)",
    "evening": "evening (e.g., 5:00 PM)",
    "night": "night (please specify an exact time during working hours)",
    "later": "later (please specify an exact time like 3:00 PM)",
    "soon": "soon (please specify an exact time)",
    "sometime": "sometime (please specify an exact date and time)",
    "anytime": "anytime (please specify a preferred time slot)",
}

VAGUE_DATE_PHRASES = (
    "next week",
    "this week",
    "next month",
    "this weekend",
    "weekend",
    "someday",
)


@dataclass
class DateTimeExtraction:
    date_str: str | None = None
    time_str: str | None = None
    date_ambiguity: str | None = None
    time_ambiguity: str | None = None
    invalid_date_reason: str | None = None


class DateTimeParser:
    """
    Deterministic + dateparser-assisted date and time extractor pinned to an
    explicit timezone. Never invents dates or times for ambiguous phrases.
    """

    def __init__(self, timezone_name: str = "Asia/Kolkata"):
        self.timezone_name = timezone_name
        self.tz = ZoneInfo(timezone_name)

    def parse(self, normalized_text: str, reference_now: datetime | None = None) -> DateTimeExtraction:
        now = reference_now.astimezone(self.tz) if reference_now else datetime.now(self.tz)
        today = now.date()
        result = DateTimeExtraction()

        # ---------- 1. Extract TIME ----------
        self._extract_time(normalized_text, result)

        # ---------- 2. Extract DATE ----------
        self._extract_date(normalized_text, today, result)

        return result

    def _extract_time(self, text: str, result: DateTimeExtraction) -> None:
        # 12-hour format with explicit am/pm: e.g., "2 pm", "2:30pm", "11:15 am"
        m12 = re.search(r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*(am|pm)\b", text)
        if m12:
            hour = int(m12.group(1))
            minute = int(m12.group(2) or 0)
            meridiem = m12.group(3)
            if meridiem == "pm" and hour != 12:
                hour += 12
            elif meridiem == "am" and hour == 12:
                hour = 0
            result.time_str = f"{hour:02d}:{minute:02d}"
            return

        # 24-hour format: e.g., "14:00", "09:30" (reject invalid like 25:00)
        m24 = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
        if m24:
            hour = int(m24.group(1))
            minute = int(m24.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                result.time_str = f"{hour:02d}:{minute:02d}"
                return
            result.time_ambiguity = f"'{m24.group(0)}' is not a valid clock time. Please provide a time like 2:00 PM or 14:00."
            return

        # "noon" / "midday"
        if re.search(r"\b(noon|midday)\b", text):
            result.time_str = "12:00"
            return

        # Bare hour with preposition like "at 4" or "for 2" without am/pm -> ambiguous!
        m_bare = re.search(r"\b(?:at|for|around|by|to)\s+(\d{1,2})\b(?!\s*(?:am|pm|km|rs|inr|rupees|provider|providers|option|st|nd|rd|th))", text)
        if m_bare:
            hour = int(m_bare.group(1))
            if 1 <= hour <= 12:
                result.time_ambiguity = (
                    f"Did you mean {hour}:00 AM or {hour}:00 PM? Please specify AM or PM."
                )
                return
            if 13 <= hour <= 23:
                result.time_str = f"{hour:02d}:00"
                return
            result.time_ambiguity = f"'{hour}' is not a valid hour. Please provide a time like 2:00 PM."
            return

        # Check vague time expressions (never invent an exact hour)
        for word, hint in VAGUE_TIME_WORDS.items():
            if re.search(rf"\b{word}\b", text):
                result.time_ambiguity = f"'{word}' is ambiguous — please specify an exact time such as {hint}."
                return

    def _extract_date(self, text: str, today: date, result: DateTimeExtraction) -> None:
        # Reject explicit past relative words
        if re.search(r"\byesterday\b", text):
            result.invalid_date_reason = "Cannot book for a past date ('yesterday'). Please choose today or a future date."
            return

        # ISO YYYY-MM-DD (validate strictly so 2026-02-30 is caught!)
        m_iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
        if m_iso:
            y, m, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
            self._validate_and_set_date(y, m, d, today, m_iso.group(0), result)
            return

        # DD/MM/YYYY or DD-MM-YYYY
        m_dmy = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", text)
        if m_dmy:
            d, m, y = int(m_dmy.group(1)), int(m_dmy.group(2)), int(m_dmy.group(3))
            self._validate_and_set_date(y, m, d, today, m_dmy.group(0), result)
            return

        # "day after tomorrow"
        if re.search(r"\bday after tomorrow\b", text):
            result.date_str = (today + timedelta(days=2)).isoformat()
            return

        # "tomorrow"
        if re.search(r"\btomorrow\b", text):
            result.date_str = (today + timedelta(days=1)).isoformat()
            return

        # "today"
        if re.search(r"\btoday\b", text):
            result.date_str = today.isoformat()
            return

        # Month-name dates: "20 Nov", "20th November 2026", "Nov 20", "Feb 30"
        month_pattern = "|".join(MONTHS.keys())
        m_day_mon = re.search(
            rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({month_pattern})(?:\s+(\d{{4}}))?\b",
            text,
        )
        if m_day_mon:
            d = int(m_day_mon.group(1))
            m = MONTHS[m_day_mon.group(2)]
            y = int(m_day_mon.group(3)) if m_day_mon.group(3) else today.year
            self._validate_and_set_date(
                y, m, d, today, m_day_mon.group(0), result, roll_next_year=m_day_mon.group(3) is None
            )
            return

        m_mon_day = re.search(
            rf"\b({month_pattern})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+(\d{{4}}))?\b",
            text,
        )
        if m_mon_day:
            m = MONTHS[m_mon_day.group(1)]
            d = int(m_mon_day.group(2))
            y = int(m_mon_day.group(3)) if m_mon_day.group(3) else today.year
            self._validate_and_set_date(
                y, m, d, today, m_mon_day.group(0), result, roll_next_year=m_mon_day.group(3) is None
            )
            return

        # Weekday: "next monday", "on friday", "this saturday"
        wd_pattern = "|".join(WEEKDAYS.keys())
        m_wd = re.search(rf"\b(?:(next|this|on)\s+)?({wd_pattern})\b", text)
        if m_wd:
            qualifier = m_wd.group(1) or ""
            target_wd = WEEKDAYS[m_wd.group(2)]
            days_ahead = (target_wd - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            elif qualifier == "next" and days_ahead <= 2:
                # "next Monday" usually means upcoming Monday, or +7 if explicitly next week
                pass
            result.date_str = (today + timedelta(days=days_ahead)).isoformat()
            return

        # "in N days"
        m_in_days = re.search(r"\bin\s+(\d{1,2})\s+days?\b", text)
        if m_in_days:
            n_days = int(m_in_days.group(1))
            if n_days == 0:
                result.date_str = today.isoformat()
            elif n_days <= 60:
                result.date_str = (today + timedelta(days=n_days)).isoformat()
            else:
                result.invalid_date_reason = "Bookings can only be scheduled up to 60 days in advance."
            return

        # Vague date phrases
        for phrase in VAGUE_DATE_PHRASES:
            if re.search(rf"\b{re.escape(phrase)}\b", text):
                result.date_ambiguity = (
                    f"'{phrase}' is ambiguous — please specify an exact date (e.g., tomorrow or 2026-11-22)."
                )
                return

        # Bounded dateparser fallback for phrases like "next tuesday" or localized date strings
        # Only invoke if text has a clear date cue to avoid false positives on numbers/prices.
        if re.search(r"\b(date|on\s+\d|next\s+\w+)\b", text):
            parsed = dateparser.parse(
                text,
                settings={
                    "TIMEZONE": self.timezone_name,
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DATES_FROM": "future",
                    "RELATIVE_BASE": datetime.combine(today, datetime.min.time()),
                },
            )
            if parsed is not None:
                parsed_date = parsed.date()
                if parsed_date < today:
                    result.invalid_date_reason = "Requested date is in the past."
                else:
                    result.date_str = parsed_date.isoformat()

    def _validate_and_set_date(
        self,
        year: int,
        month: int,
        day: int,
        today: date,
        raw: str,
        result: DateTimeExtraction,
        roll_next_year: bool = False,
    ) -> None:
        if not (1 <= month <= 12):
            result.invalid_date_reason = f"'{raw}' is not a valid calendar date."
            return
        max_day = calendar.monthrange(year, month)[1]
        if not (1 <= day <= max_day):
            result.invalid_date_reason = f"'{raw}' is not a valid calendar date."
            return
        candidate = date(year, month, day)
        if candidate < today and roll_next_year:
            # e.g. asking for "Jan 5" in November means next year
            next_year = year + 1
            if day <= calendar.monthrange(next_year, month)[1]:
                candidate = date(next_year, month, day)
        if candidate < today:
            result.invalid_date_reason = (
                f"'{raw}' is in the past. Please choose today ({today.isoformat()}) or a future date."
            )
            return
        result.date_str = candidate.isoformat()

# Rule-Based AI Service Booking Engine

A production-oriented, **100% deterministic, rule-based conversational service booking engine** built as an independent FastAPI microservice (`ai_booking_service/`) integrated with a core FastAPI + SQLite service-booking backend (`main_backend/`).

> **Zero LLM · Zero External AI APIs · Zero GPU · Zero Docker**
> Natural-language understanding is implemented deterministically using Python text normalization, configurable JSON service & location dictionaries, regex & `difflib` fuzzy matching, timezone-aware date/time extraction (`dateparser` + strict calendar validation), deterministic multi-factor ranking, and a persistent 12-state Finite State Machine (FSM).

---

## Table of Contents

1. [System Architecture & Component Topology](#1-system-architecture--component-topology)
2. [Database Schema (ER Diagram)](#2-database-schema-er-diagram)
3. [Deterministic NLU & Intent Classification Pipeline](#3-deterministic-nlu--intent-classification-pipeline)
4. [12-State Conversational Workflow FSM](#4-12-state-conversational-workflow-fsm)
5. [End-to-End Multi-Turn Booking Sequence](#5-end-to-end-multi-turn-booking-sequence)
6. [Concurrency-Safe Atomic Booking & Cancellation](#6-concurrency-safe-atomic-booking--cancellation)
7. [Deterministic Multi-Factor Ranking Engine](#7-deterministic-multi-factor-ranking-engine)
8. [Security, Rate Limiting & Error Propagation](#8-security-rate-limiting--error-propagation)
9. [Folder Structure](#9-folder-structure)
10. [Installation, Configuration & Running Locally](#10-installation-configuration--running-locally)
11. [Complete API Reference & Payloads](#11-complete-api-reference--payloads)
12. [Automated Tests & Locust Load Testing](#12-automated-tests--locust-load-testing)
13. [Production Security & Scaling Guide](#13-production-security--scaling-guide)

---

## 1. System Architecture & Component Topology

The platform enforces **strict service boundary separation**: `ai_booking_service` is completely stateless and never touches SQLite directly—all database reads, conversation persistence, availability verification, and atomic booking writes pass through authenticated `/internal/*` APIs on `main_backend`.

```mermaid
flowchart TB
    subgraph Clients["Client Layer"]
        WebUI["Web Chat UI + FSM Inspector<br/>(main_backend/app/static/index.html)"]
        APIClient["External API Client / Mobile App"]
        Locust["Locust Load Test<br/>(load_tests/locustfile.py)"]
    end

    subgraph MainBackend["Main FastAPI Backend (:8000)"]
        direction TB
        MB_MW["HTTP Middleware<br/>CORS · X-Request-ID · Latency Logging · Safe 500 Handler"]
        MB_Auth["Auth Router (/api/v1/auth/*)<br/>Register · Login · Me<br/>PBKDF2-SHA256 (200k) · HS256 JWT"]
        MB_Pub["Public Customer Router (/api/v1/*)<br/>Catalog · Bookings · Chat Gateway Proxy"]
        MB_Int["Internal Service Router (/internal/*)<br/>Guarded by constant-time X-Internal-Token"]
        MB_Svc["Domain Booking Service (booking_service.py)<br/>Haversine Distance · Slot Availability<br/>Atomic Create & Cancel · Idempotency"]
        MB_DB["SQLAlchemy ORM + SQLite Engine<br/>WAL Mode · foreign_keys=ON<br/>busy_timeout=5000ms · BEGIN IMMEDIATE"]

        MB_MW --> MB_Auth
        MB_MW --> MB_Pub
        MB_MW --> MB_Int
        MB_Auth --> MB_DB
        MB_Pub --> MB_Svc
        MB_Int --> MB_Svc
        MB_Int --> MB_DB
        MB_Svc --> MB_DB
    end

    subgraph AIService["Rule-Based AI Booking Microservice (:8001) — Stateless"]
        direction TB
        AI_MW["HTTP Middleware<br/>CORS · X-Request-ID<br/>Sliding-Window Rate Limiter (60 req / 60s)"]
        AI_API["Assistant & Confirmation Routers<br/>POST /v1/assistant/message · /confirm · /cancel<br/>GET /v1/assistant/conversations/{id} · /health · /ready"]
        AI_FSM["ConversationManager (12-State FSM)<br/>Multi-turn Slot Filling · Reference Resolution<br/>TTL Expiration · Confirmation Guard"]

        subgraph NLU["Deterministic NLU Engine (Zero LLM)"]
            Norm["Text Normalizer<br/>Typo Map · Contractions · ₹ → rs"]
            Dict["Service & Location Dictionary<br/>JSON Aliases · Longest-Match · Fuzzy 0.84"]
            DTP["DateTimeParser (Asia/Kolkata)<br/>12h/24h · Relative · Calendar Validation"]
            Ext["EntityExtractor<br/>Booking ID · Constraints · Provider Ref"]
            IntP["IntentParser<br/>13 Priority-Ordered Intents + Confidence"]
            Norm --> Ext
            Dict --> Ext
            DTP --> Ext
            Ext --> IntP
        end

        AI_Rank["RankingEngine<br/>price · distance · rating · earliest · best"]
        AI_Coord["Coordinators & Repository<br/>SearchCoordinator · BookingCoordinator<br/>ConversationRepository"]
        AI_Client["Async HTTPX BackendClient<br/>X-Internal-Token · Bearer Pass-Through<br/>Typed Domain Error Mapping"]

        AI_MW --> AI_API
        AI_API --> AI_FSM
        AI_FSM --> NLU
        AI_FSM --> AI_Coord
        AI_Coord --> AI_Rank
        AI_Coord --> AI_Client
    end

    SQLite[("SQLite Database (WAL Mode)<br/>main_backend/booking_platform.db")]

    WebUI -->|"Bearer JWT"| MB_MW
    Locust -->|"Bearer JWT"| MB_MW
    APIClient -->|"Bearer JWT"| MB_MW
    APIClient -.->|"Direct Bearer JWT"| AI_MW

    MB_Pub -->|"Async HTTPX Proxy<br/>Bearer JWT + X-Request-ID"| AI_MW
    AI_Client -->|"Async HTTPX<br/>X-Internal-Token + Bearer JWT + X-Request-ID"| MB_Int
    MB_DB --> SQLite
```

### Strict Responsibility Separation

| Service | Port | Owns | Explicitly Does **Not** Do |
|---|---|---|---|
| **`main_backend/`** | `8000` | SQLite persistence (WAL + `BEGIN IMMEDIATE`), user auth (PBKDF2-SHA256 + HS256 JWT), service/provider catalog, availability slots, atomic booking creation & cancellation, price revalidation, idempotency, conversation record storage, `/internal/*` APIs, local Web Chat UI | No NLU, no regex parsing, no conversational FSM transitions, no composite ranking explanations |
| **`ai_booking_service/`** | `8001` | Deterministic NLU (normalization, dictionary, date/time parser, entity extractor, 13-intent classifier), multi-factor ranking, 12-state conversational FSM, reference resolution, sliding-window rate limiting, correlation IDs | Never opens SQLite, never validates passwords or mints JWTs, never mutates slots directly |

---

## 2. Database Schema (ER Diagram)

All tables are defined in [`main_backend/app/models.py`](main_backend/app/models.py) and managed by SQLAlchemy 2.0 on SQLite with `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=5000`, and `PRAGMA synchronous=NORMAL`.

```mermaid
erDiagram
    USERS {
        string id PK "UUID (36)"
        string email UK "Unique, Indexed"
        string full_name "VARCHAR(150)"
        string password_hash "pbkdf2_sha256$200000$salt$dk"
        string current_location_name "Nullable saved user area/label"
        float current_latitude "Nullable saved user GPS latitude"
        float current_longitude "Nullable saved user GPS longitude"
        boolean is_active "Default true"
        datetime created_at "UTC"
    }

    SERVICES {
        string id PK "UUID (36)"
        string code UK "e.g. car_wash, plumbing"
        string name "Display name"
        string category "Indexed (automotive, appliance...)"
        text description "Service description"
        int default_duration_minutes "Default 60"
    }

    PROVIDERS {
        string id PK "UUID (36)"
        string listing_id "Indexed (e.g. ERK-001)"
        string name "Indexed"
        string location_name "Indexed (Aluva, Kalamassery, Edappally...)"
        string address "Full street address"
        string phone_public "Nullable public contact phone"
        float latitude "WGS84 locality coordinate"
        float longitude "WGS84 locality coordinate"
        float rating "Nullable (never fabricated)"
        int review_count "Default 0"
        string work_start_time "HH:MM (default 09:00)"
        string work_end_time "HH:MM (default 19:00)"
        string verification_status "Nullable onboarding verification flag"
        string source_name "Nullable listing source"
        boolean is_active "Indexed, default true"
    }

    PROVIDER_SERVICES {
        string id PK "UUID (36)"
        string provider_id FK "-> providers.id"
        string service_id FK "-> services.id"
        float price "Service price"
        string currency "Default INR"
        int duration_minutes "Default 60"
        boolean is_active "Default true"
    }

    AVAILABILITY_SLOTS {
        string id PK "UUID (36)"
        string provider_id FK "-> providers.id"
        string slot_date "YYYY-MM-DD"
        string start_time "HH:MM"
        string end_time "HH:MM"
        int capacity "CHECK capacity >= 1"
        int booked_count "CHECK 0 <= booked_count <= capacity"
        boolean is_active "Default true"
    }

    BOOKINGS {
        string id PK "UUID (36)"
        string booking_code UK "BK-YYYYMMDD-XXXX"
        string user_id FK "-> users.id"
        string provider_id FK "-> providers.id"
        string provider_service_id FK "-> provider_services.id"
        string slot_id FK "-> availability_slots.id"
        string service_date "YYYY-MM-DD"
        string start_time "HH:MM"
        string end_time "HH:MM"
        float booked_price "Revalidated at commit"
        string currency "Default INR"
        string status "CONFIRMED | CANCELLED"
        string idempotency_key UK "idem-<sha256[:32]>"
        string request_fingerprint "SHA-256 of booking tuple"
        text notes "Optional notes + cancel reason"
        datetime created_at "UTC"
        datetime updated_at "UTC"
        datetime cancelled_at "Nullable UTC"
    }

    CONVERSATIONS {
        string id PK "UUID (36)"
        string user_id FK "-> users.id"
        string state "12-state FSM value"
        text context_json "Serialized slots, results & history"
        datetime expires_at "UTC TTL (default +30m)"
        datetime created_at "UTC"
        datetime updated_at "UTC"
    }

    USERS ||--o{ BOOKINGS : "places"
    USERS ||--o{ CONVERSATIONS : "owns"
    PROVIDERS ||--o{ PROVIDER_SERVICES : "offers"
    SERVICES ||--o{ PROVIDER_SERVICES : "cataloged_in"
    PROVIDERS ||--o{ AVAILABILITY_SLOTS : "schedules"
    PROVIDERS ||--o{ BOOKINGS : "fulfills"
    PROVIDER_SERVICES ||--o{ BOOKINGS : "priced_by"
    AVAILABILITY_SLOTS ||--o{ BOOKINGS : "reserves"
```

### Key Constraints & Composite Indexes

| Table | Constraint / Index | Purpose |
|---|---|---|
| `provider_services` | `UNIQUE(provider_id, service_id)` · `IX(service_id, price)` | One active offering per provider+service; fast cheapest-service lookup |
| `availability_slots` | `UNIQUE(provider_id, slot_date, start_time)` · `CHECK(capacity >= 1)` · `CHECK(booked_count >= 0 AND booked_count <= capacity)` | Database-level invariant preventing negative counts or overbooking |
| `bookings` | `UNIQUE(booking_code)` · `UNIQUE(idempotency_key)` · `IX(user_id, created_at)` · `IX(slot_id, status)` | Guarantees idempotent retries and fast user/slot duplicate checks |
| `conversations` | `IX(user_id, updated_at)` | Fast lookup of user-owned conversation state |

---

## 3. Deterministic NLU & Intent Classification Pipeline

Every customer message enters a 4-stage deterministic pipeline with **zero stochasticity**, **zero fabricated data**, and explicit ambiguity detection.

```mermaid
flowchart TD
    Raw["Raw Customer Message<br/>(Validated: non-empty, <= 1000 chars)"] --> Norm

    subgraph Stage1["Stage 1: Normalization (normalization.py)"]
        Norm["1. Lowercase & strip<br/>2. Replace ₹ -> rs, smart quotes -> '<br/>3. Expand contractions (i'd, don't, that's...)<br/>4. Strip noise punctuation, preserve : - / .<br/>5. Apply COMMON_TYPO_MAP (tmrw, plumer, cheepest, alwaye...)"]
    end

    Norm --> BK & SVC & DT & LOC & PREF & REF

    subgraph Stage2["Stage 2: Parallel Rule-Based Entity Extraction (entity_extractor.py)"]
        BK["Booking Identifier<br/>BK-YYYYMMDD-XXXX or UUID"]
        SVC["Service Matching (service_dictionary.py)<br/>Longest-alias word-boundary regex<br/>-> Fuzzy fallback (difflib cutoff=0.84)<br/>-> Unknown service mention detector"]
        DT["DateTimeParser (Asia/Kolkata)<br/>TIME: 12h AM/PM · 24h HH:MM · noon · bare-hour ambiguity · vague words<br/>DATE: ISO · DD/MM/YYYY · today/tomorrow · Month+Day · Weekday · in N days · bounded dateparser<br/>Rejects past & invalid dates (e.g. Feb 30)"]
        LOC["Location Resolution<br/>near me flag + 10-area Kochi/Aluva gazetteer (lat/lon)<br/>-> Unresolved location capture"]
        PREF["Preferences & Filters<br/>cheapest · nearest · highest-rated · earliest<br/>max_price (under/below N) · max_distance_km (within N km)"]
        REF["Provider Reference<br/>1. Known provider name in context<br/>2. Ordinal (second provider, option 2)<br/>3. Qualifier (the cheapest one)<br/>4. Anaphoric (that one, book it)"]
    end

    BK & SVC & DT & LOC & PREF & REF --> AmbCheck

    subgraph Stage3["Stage 3: Ambiguity & Sort Resolution"]
        AmbCheck["Collect Ambiguities<br/>- Multiple services mentioned<br/>- Ambiguous date (next week, weekend)<br/>- Ambiguous time (morning, at 4)<br/>- Invalid/past date reason"]
        SortDet["Determine sort_by<br/>cheapest → price · nearest → distance<br/>highest_rated → rating · earliest → earliest<br/>default → best"]
    end

    AmbCheck & SortDet --> Classifier

    subgraph Stage4["Stage 4: Priority-Ordered 13-Intent Classifier (intent_parser.py)"]
        Classifier["State-Aware Rule Evaluation (Top-to-Bottom)<br/>1. AWAITING_CONFIRMATION yes/no -> CONFIRM / CANCEL (0.98 / 0.95)<br/>2. Explicit confirm/cancel phrasing -> CONFIRM / CANCEL (0.96 / 0.95)<br/>3. Booking status keywords -> CHECK_BOOKING_STATUS (0.95)<br/>4. Modify/reschedule + slot/time/date -> MODIFY_BOOKING (0.92)<br/>5. Provider reference (index/anaphoric/name/qualifier) -> SELECT_PROVIDER (0.94)<br/>6. Price / Distance preference -> FIND_CHEAPEST / FIND_NEAREST (0.93)<br/>7. Availability keywords -> CHECK_AVAILABILITY (0.90)<br/>8. Book/reserve + service/datetime -> BOOK_SERVICE (0.91)<br/>9. Provider/shop keywords -> FIND_PROVIDER (0.88)<br/>10. Matched service -> SEARCH_SERVICE (0.90)<br/>11. Active-conversation follow-up slot fill -> MODIFY / SEARCH (0.84 / 0.85)<br/>12. Greeting / help -> GENERAL_QUERY (0.90)<br/>13. Fallback -> UNKNOWN_INTENT (0.25)"]
    end

    Classifier --> Out["ParsedIntent<br/>(type, confidence, entities, ambiguities, sort_by)"]
```

### Supported Intents Reference (13 Intents)

| # | Intent | Typical Confidence | Example Customer Utterance |
|---|---|---|---|
| 1 | `CONFIRM_BOOKING` | `0.98` / `0.96` / `0.70` | `"Yes"`, `"Confirm booking"`, `"Go ahead"` |
| 2 | `CANCEL_BOOKING` | `0.95` / `0.70` | `"Cancel"`, `"Cancel booking BK-20261120-AB12"`, `"No"` (while confirming) |
| 3 | `CHECK_BOOKING_STATUS` | `0.95` | `"Show my booking status"`, `"Check my bookings"` |
| 4 | `MODIFY_BOOKING` | `0.92` / `0.84` | `"Change it to 4 PM"`, `"Reschedule to tomorrow"` |
| 5 | `SELECT_PROVIDER` | `0.94` | `"Book that one"`, `"Book the second provider"`, `"Pick the cheapest one"`, `"Sparkle Auto Spa"` |
| 6 | `FIND_CHEAPEST` | `0.93` | `"Find the cheapest car wash near Aluva tomorrow at 2 PM"` |
| 7 | `FIND_NEAREST` | `0.93` | `"Find the nearest plumber in Kochi tomorrow at 10 AM"` |
| 8 | `CHECK_AVAILABILITY` | `0.90` | `"Check availability for AC repair tomorrow at 11 AM"` |
| 9 | `BOOK_SERVICE` | `0.91` | `"Book an electrician near Kakkanad tomorrow at 4 PM"` |
| 10 | `FIND_PROVIDER` | `0.88` | `"List all the car wash centres"`, `"Find providers near Edappally"` |
| 11 | `SEARCH_SERVICE` | `0.90` / `0.85` / `0.60` | `"List all the plumbers near me"`, `"I need home cleaning"`, `"Tomorrow at 2 PM"` (slot-filling follow-up) |
| 12 | `GENERAL_QUERY` | `0.90` | `"Hi"`, `"Help"`, `"What services do you support?"` |
| 13 | `UNKNOWN_INTENT` | `0.25` | Unrecognized non-booking utterances |

### Configurable Dictionary (`service_dictionary.json`)

- **6 Services**: `car_wash` (automotive, 60m), `plumbing` (home_maintenance, 60m), `ac_repair` (appliance, 60m), `electrical` (home_maintenance, 60m), `home_cleaning` (cleaning, 120m), `appliance_repair` (appliance, 60m) — each with rich multi-word aliases.
- **10 Kochi/Aluva Locations with Coordinates**: `Aluva` (10.1076, 76.3516), `Kochi` (9.9312, 76.2673), `Ernakulam` (9.9816, 76.2999), `Kakkanad` (10.0159, 76.3419), `Edappally` (10.0261, 76.3125), `Kalamassery` (10.0553, 76.3219), `Kaloor` (9.9942, 76.2921), `Vyttila` (9.9685, 76.3182), `Fort Kochi` (9.9658, 76.2421), `Tripunithura` (9.9487, 76.3464).

---

## 4. 12-State Conversational Workflow FSM

`ConversationManager` enforces strict transitions via `ALLOWED_TRANSITIONS` in [`ai_booking_service/app/services/conversation_manager.py`](ai_booking_service/app/services/conversation_manager.py). Invalid transitions raise `InvalidStateTransitionError`, and every conversation is persisted in SQLite with a sliding TTL (`CONVERSATION_TTL_MINUTES = 30`).

```mermaid
stateDiagram-v2
    [*] --> INITIAL : New Conversation Created

    INITIAL --> COLLECTING_INFORMATION : Missing service / date / time or Ambiguity
    INITIAL --> READY_TO_SEARCH : All required fields present or list-all browse query
    INITIAL --> CANCELLED : Cancel request
    INITIAL --> EXPIRED : TTL exceeded

    COLLECTING_INFORMATION --> COLLECTING_INFORMATION : Partial slot fill / Ambiguity
    COLLECTING_INFORMATION --> READY_TO_SEARCH : All fields collected (service + date + time)
    COLLECTING_INFORMATION --> CANCELLED : Cancel request
    COLLECTING_INFORMATION --> EXPIRED : TTL exceeded

    READY_TO_SEARCH --> SEARCHING : Invoke SearchCoordinator
    READY_TO_SEARCH --> COLLECTING_INFORMATION : Missing fields
    READY_TO_SEARCH --> CANCELLED : Cancel
    READY_TO_SEARCH --> EXPIRED : TTL exceeded

    SEARCHING --> OPTIONS_FOUND : Providers available & ranked
    SEARCHING --> COLLECTING_INFORMATION : NO_PROVIDERS_FOUND (prompt alternate time/area)
    SEARCHING --> CANCELLED : Cancel
    SEARCHING --> EXPIRED : TTL exceeded

    OPTIONS_FOUND --> PROVIDER_SELECTED : User selects option & slot verified available
    OPTIONS_FOUND --> READY_TO_SEARCH : Modify date/time/location & re-search
    OPTIONS_FOUND --> COLLECTING_INFORMATION : Modify requires missing field
    OPTIONS_FOUND --> CANCELLED : Cancel
    OPTIONS_FOUND --> EXPIRED : TTL exceeded

    PROVIDER_SELECTED --> AWAITING_CONFIRMATION : Prompt customer for explicit confirmation
    PROVIDER_SELECTED --> OPTIONS_FOUND : Change selection
    PROVIDER_SELECTED --> CANCELLED : Cancel
    PROVIDER_SELECTED --> EXPIRED : TTL exceeded

    AWAITING_CONFIRMATION --> BOOKING_IN_PROGRESS : Customer confirms (Yes / /confirm)
    AWAITING_CONFIRMATION --> AWAITING_CONFIRMATION : Switch provider selection
    AWAITING_CONFIRMATION --> READY_TO_SEARCH : Modify date/time/location & re-search
    AWAITING_CONFIRMATION --> CANCELLED : Customer declines or cancels
    AWAITING_CONFIRMATION --> EXPIRED : TTL exceeded

    BOOKING_IN_PROGRESS --> BOOKING_CONFIRMED : Atomic booking committed (201)
    BOOKING_IN_PROGRESS --> AWAITING_CONFIRMATION : PRICE_CHANGED (409) -> re-confirm updated price
    BOOKING_IN_PROGRESS --> BOOKING_FAILED : SLOT_UNAVAILABLE / Conflict / Backend error

    BOOKING_CONFIRMED --> CANCELLED : Cancel confirmed booking (releases slot)
    BOOKING_CONFIRMED --> READY_TO_SEARCH : Start new booking (all fields)
    BOOKING_CONFIRMED --> COLLECTING_INFORMATION : Start new booking (partial fields)

    BOOKING_FAILED --> READY_TO_SEARCH : Retry search with new time/provider
    BOOKING_FAILED --> COLLECTING_INFORMATION : Provide alternate criteria
    BOOKING_FAILED --> CANCELLED : Cancel

    CANCELLED --> READY_TO_SEARCH : Start fresh search (all fields)
    CANCELLED --> COLLECTING_INFORMATION : Start fresh search (partial fields)

    EXPIRED --> [*] : Terminal (start new conversation)
```

### Persistent `context_json` Structure

Stored inside `conversations.context_json` in `main_backend`:
- **Slots & Filters**: `service`, `service_display`, `service_category`, `location`, `latitude`, `longitude`, `date`, `time`, `sort_by`, `max_price`, `max_distance_km`
- **Search & Selection**: `search_results` (ranked provider snapshots), `selected_provider` (verified `provider_service_id`, `slot_id`, `expected_price`, `currency`)
- **Booking Snapshot**: `confirmed_booking_id`, `confirmed_booking_code`, `confirmed_booking`
- **Audit Window**: `extracted_intent`, `history` (last 20 customer/assistant turns)

---

## 5. End-to-End Multi-Turn Booking Sequence

The following sequence diagram traces a full authenticated booking session—including live availability checks, deterministic idempotency, atomic slot reservation, and conflict handling (`PRICE_CHANGED`, `SLOT_UNAVAILABLE`, and idempotent replay).

```mermaid
sequenceDiagram
    autonumber
    actor User as Customer / Web Chat UI
    participant Proxy as Main Backend (:8000)<br/>Auth & /api/v1/chat/*
    participant AI as AI Microservice (:8001)<br/>NLU · Ranking · FSM
    participant Internal as Main Backend (:8000)<br/>/internal/* + BookingService
    participant DB as SQLite (WAL)<br/>booking_platform.db

    rect rgb(235, 245, 255)
        Note over User,DB: Phase 1 — Authentication
        User->>Proxy: POST /api/v1/auth/login (email, password)
        Proxy->>DB: Verify PBKDF2-SHA256 hash
        DB-->>Proxy: Active User record
        Proxy-->>User: 200 OK { access_token (HS256 JWT), user }
    end

    rect rgb(240, 252, 244)
        Note over User,DB: Phase 2 — Search & Deterministic Ranking
        User->>Proxy: POST /api/v1/chat/message<br/>"Find the cheapest car wash near Aluva tomorrow at 2 PM"
        Proxy->>Proxy: Validate JWT & close DB session
        Proxy->>AI: POST /v1/assistant/message (Bearer JWT + X-Request-ID)
        AI->>AI: Rate Limit Check (60 req/60s)
        AI->>Internal: POST /internal/conversations (X-Internal-Token + JWT)
        Internal->>DB: INSERT conversations (state=INITIAL, TTL=+30m)
        DB-->>Internal: conversation_id
        Internal-->>AI: ConversationRecord
        AI->>AI: NLU Parse -> FIND_CHEAPEST<br/>service=car_wash, loc=Aluva (10.1076,76.3516), date, time=14:00
        AI->>Internal: POST /internal/services/search
        Internal->>DB: Query ProviderService + Haversine + AvailabilitySlot
        DB-->>Internal: Available providers
        Internal-->>AI: InternalSearchResponse
        AI->>AI: RankingEngine.rank(sort_by="price")<br/>FSM: INITIAL -> READY_TO_SEARCH -> SEARCHING -> OPTIONS_FOUND
        AI->>Internal: PUT /internal/conversations/{id} (state=OPTIONS_FOUND, search_results)
        Internal->>DB: UPDATE conversations
        AI-->>Proxy: 200 OK { status: OPTIONS_FOUND, providers: [...] }
        Proxy-->>User: Ranked providers + explanations
    end

    rect rgb(255, 248, 235)
        Note over User,DB: Phase 3 — Reference Resolution & Live Slot Verification
        User->>Proxy: POST /api/v1/chat/message<br/>{ conversation_id, message: "Book that one" }
        Proxy->>AI: Forward POST /v1/assistant/message
        AI->>Internal: GET /internal/conversations/{id} (ownership verified)
        Internal-->>AI: ConversationRecord (OPTIONS_FOUND)
        AI->>AI: NLU -> SELECT_PROVIDER (anaphoric -> results[0])
        AI->>Internal: POST /internal/availability/check (provider_service_id, slot_id)
        Internal->>DB: Verify active provider, working hours, capacity > booked_count
        DB-->>Internal: available=true, current_price=300.0
        Internal-->>AI: AvailabilityCheckResponse
        AI->>AI: FSM: OPTIONS_FOUND -> PROVIDER_SELECTED -> AWAITING_CONFIRMATION
        AI->>Internal: PUT /internal/conversations/{id} (selected_provider, expected_price=300)
        AI-->>Proxy: 200 OK { status: AWAITING_CONFIRMATION, requires_confirmation: true }
        Proxy-->>User: "Do you want to confirm Amazing Carwash ... for ₹300?"
    end

    rect rgb(245, 240, 255)
        Note over User,DB: Phase 4 — Atomic Confirmation & Concurrency Safety
        User->>Proxy: POST /api/v1/chat/confirm { conversation_id, confirm: true }
        Proxy->>AI: Forward POST /v1/assistant/confirm
        AI->>Internal: PUT /internal/conversations/{id} (state=BOOKING_IN_PROGRESS)
        AI->>AI: Compute deterministic idempotency_key<br/>idem-SHA256(user:conv:ps:slot:price)[:32]
        AI->>Internal: POST /internal/bookings/create<br/>{ provider_service_id, slot_id, expected_price, idempotency_key }
        Internal->>DB: BEGIN IMMEDIATE (acquire RESERVED write lock)
        alt Happy Path — Price Matches & Slot Available
            Internal->>DB: Check idempotency_key, price, hours, capacity, duplicates
            Internal->>DB: UPDATE availability_slots SET booked_count = booked_count + 1
            Internal->>DB: INSERT bookings (BK-YYYYMMDD-XXXX, status=CONFIRMED)
            Internal->>DB: COMMIT
            Internal-->>AI: 201 Created { BookingResponse }
            AI->>Internal: PUT /internal/conversations/{id} (state=BOOKING_CONFIRMED)
            AI-->>Proxy: 200 OK { status: BOOKING_CONFIRMED, booking: {...} }
            Proxy-->>User: Booking confirmed with code BK-YYYYMMDD-XXXX
        else Price Changed Before Confirmation (409 PRICE_CHANGED)
            Internal->>DB: ROLLBACK
            Internal-->>AI: 409 { error_code: PRICE_CHANGED, current_price }
            AI->>Internal: PUT /internal/conversations/{id}<br/>(state=AWAITING_CONFIRMATION, updated expected_price)
            AI-->>Proxy: 200 OK { requires_confirmation: true, error_code: PRICE_CHANGED }
            Proxy-->>User: Ask explicit re-confirmation at new price
        else Slot Sold Out Under Race Condition (409 SLOT_UNAVAILABLE)
            Internal->>DB: ROLLBACK
            Internal-->>AI: 409 { error_code: SLOT_UNAVAILABLE }
            AI->>Internal: PUT /internal/conversations/{id} (state=BOOKING_FAILED)
            AI-->>Proxy: 200 OK { status: BOOKING_FAILED, error_code: SLOT_UNAVAILABLE }
            Proxy-->>User: Prompt user to pick another slot or provider
        end
    end
```

---

## 6. Concurrency-Safe Atomic Booking & Cancellation

SQLite defaults to deferred read transactions that can cause write deadlocks or double-booking races under concurrent requests. [`main_backend/app/database.py`](main_backend/app/database.py) solves this with `isolation_level=None` and `immediate_transaction()`, which executes **`BEGIN IMMEDIATE`** to acquire SQLite's `RESERVED` write lock at the very start of the transaction (SQLite's equivalent of PostgreSQL's `SELECT ... FOR UPDATE`).

```mermaid
flowchart TD
    Start["POST /internal/bookings/create<br/>Compute SHA-256 request_fingerprint"] --> Lock["BEGIN IMMEDIATE<br/>(Acquire SQLite RESERVED Write Lock)"]

    Lock --> Idem{"1. idempotency_key<br/>exists in bookings?"}
    Idem -->|Yes| FpMatch{"Same user_id AND<br/>same fingerprint?"}
    FpMatch -->|Yes| Replay["Return Existing Booking<br/>(Idempotent Replay — No Double Charge)"]
    FpMatch -->|No| ErrIdem["ROLLBACK<br/>409 IDEMPOTENCY_CONFLICT"]

    Idem -->|No| Active{"2. Provider & ProviderService<br/>exist and active?"}
    Active -->|No| ErrSvc["ROLLBACK<br/>404 SERVICE_UNAVAILABLE"]

    Active -->|Yes| Price{"3. abs(ps.price - expected_price)<br/><= 0.01?"}
    Price -->|No| ErrPrice["ROLLBACK<br/>409 PRICE_CHANGED<br/>(returns current_price)"]

    Price -->|Yes| SlotExist{"4. AvailabilitySlot exists<br/>for provider & active?"}
    SlotExist -->|No| ErrSlot404["ROLLBACK<br/>404 SLOT_NOT_FOUND"]

    SlotExist -->|Yes| Hours{"5. Slot + duration within<br/>provider work_start..work_end?"}
    Hours -->|No| ErrHours["ROLLBACK<br/>409 OUTSIDE_WORKING_HOURS"]

    Hours -->|Yes| Cap{"6. slot.booked_count<br/>< slot.capacity?"}
    Cap -->|No| ErrCap["ROLLBACK<br/>409 SLOT_UNAVAILABLE"]

    Cap -->|Yes| Dup{"7. User already has<br/>CONFIRMED booking on slot?"}
    Dup -->|Yes| ErrDup["ROLLBACK<br/>409 DUPLICATE_BOOKING"]

    Dup -->|No| Write["8. Atomic Reservation<br/>slot.booked_count += 1<br/>INSERT Booking (BK-YYYYMMDD-XXXX, CONFIRMED)"]
    Write --> Commit["COMMIT Transaction"]
    Commit --> Ok["201 Created — BookingResponse"]
```

### Atomic Cancellation Flow (`POST /internal/bookings/{booking_id}/cancel`)

```mermaid
flowchart LR
    CStart["POST /internal/bookings/{id}/cancel<br/>(Accepts UUID or BK-... code)"] --> CLock["BEGIN IMMEDIATE"]
    CLock --> CFind{"Booking found?"}
    CFind -->|No| C404["404 BOOKING_NOT_FOUND"]
    CFind -->|Yes| COwn{"booking.user_id ==<br/>JWT user.id?"}
    COwn -->|No| C403["403 FORBIDDEN"]
    COwn -->|Yes| CStat{"Current status?"}
    CStat -->|CANCELLED| CIdem["Return booking<br/>(Idempotent)"]
    CStat -->|Other != CONFIRMED| C409["409 INVALID_CANCELLATION"]
    CStat -->|CONFIRMED| CUpdate["status = CANCELLED<br/>cancelled_at = UTC now<br/>Append reason to notes<br/>slot.booked_count -= 1 (if > 0)<br/>COMMIT"]
    CUpdate --> COk["200 OK — Slot Released"]
```

---

## 7. Deterministic Multi-Factor Ranking Engine

[`ai_booking_service/app/core/ranking_engine.py`](ai_booking_service/app/core/ranking_engine.py) filters out unavailable providers, optionally applies `max_distance_km`, and produces deterministic scores plus human-readable `ranking_explanation` strings. **Missing ratings or distances are never fabricated.**

```mermaid
flowchart TD
    In["Raw Providers from /internal/services/search"] --> FilterAvail["Filter: available == True"]
    FilterAvail --> FilterDist{"max_distance_km<br/>specified?"}
    FilterDist -->|Yes| ApplyDist["Keep distance_km != None<br/>AND distance_km <= max_distance_km"]
    FilterDist -->|No| ModeSwitch
    ApplyDist --> ModeSwitch{"sort_by Mode"}

    ModeSwitch -->|"price"| MPrice["Sort by (price ASC, distance ASC, rating DESC)<br/>Score = 1 / rank"]
    ModeSwitch -->|"distance"| MDist["Sort by (distance_is_none, distance ASC, price ASC)<br/>Score = 1 / rank"]
    ModeSwitch -->|"rating"| MRate["Sort by (rating_is_none, rating DESC, reviews DESC, price ASC)<br/>Score = rating / 5.0"]
    ModeSwitch -->|"earliest"| MEarly["Sort by (slot_date ASC, start_time ASC, price ASC)<br/>Score = 1 / rank"]
    ModeSwitch -->|"best (default)"| MBest["Normalized Composite Score<br/>(Dynamic Weight Renormalization)"]

    MBest --> Formula["Compute Factor Scores:<br/>• Price (w=0.35): (max_p - p) / (max_p - min_p)<br/>• Distance (w=0.25): (max_d - d) / (max_d - min_d) [only if real distance exists]<br/>• Rating (w=0.25): clamp(rating / 5.0, 0, 1) [only if real rating exists]<br/>• Availability (w=0.15): 1.0 if available & slot_id else 0.0<br/>Final Score = sum(w_i * s_i) / sum(active w_i)<br/>Sort by (score DESC, price ASC)"]

    MPrice & MDist & MRate & MEarly & Formula --> Out["Ranked list[ProviderOption]<br/>with ranking_score & ranking_explanation"]
```

### Composite Ranking Weights (Configurable via `.env`)

| Factor | Env Variable | Default Weight | Normalization Formula | Missing Data Behavior |
|---|---|---|---|---|
| **Price** | `RANK_WEIGHT_PRICE` | `0.35` | `(max_p - price) / (max_p - min_p)` (`1.0` if equal) | Always present |
| **Distance** | `RANK_WEIGHT_DISTANCE` | `0.25` | `(max_d - dist) / (max_d - min_d)` (`1.0` if equal) | Excluded from numerator & denominator (`active_weight`) |
| **Rating** | `RANK_WEIGHT_RATING` | `0.25` | `clamp(rating / 5.0, 0.0, 1.0)` | Excluded from numerator & denominator (e.g., unrated listings like `Electro Tech Home Appliance Services` with `rating=None`) |
| **Availability** | `RANK_WEIGHT_AVAILABILITY` | `0.15` | `1.0` if `available && slot_id` else `0.0` | Always evaluated |

---

## 8. Security, Rate Limiting & Error Propagation

```mermaid
flowchart LR
    Req["Incoming Request"] --> Corr["X-Request-ID<br/>Generated or Propagated<br/>Returned in Response Header"]
    Corr --> RL{"AI Service Path<br/>/health or /ready?"}
    RL -->|Yes| Auth
    RL -->|No| Window["SlidingWindowRateLimiter<br/>Key: client_ip:path_prefix<br/>Max 60 req / 60s"]
    Window -->|Exceeded| E429["429 Too Many Requests<br/>error_code: RATE_LIMITED"]
    Window -->|Allowed| Auth

    Auth["Bearer JWT Check<br/>HS256 Signature + exp + active User"] -->|Invalid/Expired| E401["401 Unauthorized"]
    Auth -->|Valid| S2S["Internal Service Call<br/>X-Internal-Token via secrets.compare_digest<br/>+ Customer Bearer Pass-Through"]
    S2S -->|Invalid Token| E401Int["401 Unauthorized Internal Request"]
    S2S -->|Valid| Own["Strict Ownership Check<br/>record.user_id == jwt_user.id<br/>(Never trusts body user_id)"]
    Own -->|Mismatch| E403["403 Forbidden"]
    Own -->|Authorized| Domain["Domain Execution &<br/>Typed Error Mapping"]
```

### Security Highlights
- **Zero Trust on Client Payloads**: `user_id` is always derived server-side from the verified JWT `sub` claim—never accepted from request JSON.
- **Password Hashing**: Salted PBKDF2-SHA256 with `200,000` iterations, `16`-byte random salt, and constant-time verification (`hmac.compare_digest`).
- **Service-to-Service Auth**: All `/internal/*` endpoints enforce constant-time `secrets.compare_digest` verification of `X-Internal-Token`.
- **Safe Logging**: Neither `Authorization` headers nor `X-Internal-Token` values are ever logged; structured JSON logs include `request_id`, `method`, `path`, `status_code`, and `duration_ms`.
- **Typed Backend Error Mapping (`backend_client.py`)**: Network timeouts (`504 BACKEND_TIMEOUT`), connection failures (`503 BACKEND_UNAVAILABLE`), auth/ownership errors (`401`/`403`/`404`), and booking conflicts (`409 PRICE_CHANGED`, `SLOT_UNAVAILABLE`, `BOOKING_CONFLICT`) are mapped to clean domain responses without leaking stack traces.

---

## 9. Folder Structure

```text
agentic/
├── main_backend/
│   ├── app/
│   │   ├── main.py                     # FastAPI entrypoint, lifespan auto-seeding, CORS, X-Request-ID, UI
│   │   ├── config.py                   # Typed pydantic-settings configuration
│   │   ├── database.py                 # SQLite WAL engine, PRAGMAs & BEGIN IMMEDIATE context manager
│   │   ├── models.py                   # 7 SQLAlchemy ORM models with CHECK & UNIQUE constraints
│   │   ├── schemas.py                  # Public & internal Pydantic v2 contracts
│   │   ├── security.py                 # PBKDF2-SHA256 (200k), HS256 JWT, constant-time X-Internal-Token
│   │   ├── seed.py                     # Idempotent seeder (2 demo users, 6 services, 33 Ernakulam/Kochi providers, 44 offerings, 7d slots)
│   │   ├── api/
│   │   │   ├── auth.py                 # /api/v1/auth/register, /login, /me
│   │   │   ├── public.py               # /api/v1/services, /providers, /bookings, /chat/* gateway proxy
│   │   │   └── internal.py             # /internal/* service-to-service APIs (10 endpoints)
│   │   ├── services/
│   │   │   └── booking_service.py      # Haversine search, availability check, atomic create & cancel
│   │   └── static/
│   │       └── index.html              # Interactive Web Chat UI, User GPS/Area Location Panel, Route/Directions & FSM Inspector
│   ├── tests/
│   │   ├── conftest.py
│   │   └── test_booking_concurrency.py # Multi-threaded SQLite race, rollback, idempotency, ownership & auth
│   ├── requirements.txt
│   └── .env.example
├── ai_booking_service/
│   ├── app/
│   │   ├── main.py                     # FastAPI app factory, lifespan DI, safe exception handlers
│   │   ├── config.py                   # URLs, timezone, TTL, rate limits, composite ranking weights
│   │   ├── logging_config.py           # Structured JSON logger
│   │   ├── middleware.py               # X-Request-ID correlation & SlidingWindowRateLimiter
│   │   ├── api/
│   │   │   ├── assistant.py            # POST /v1/assistant/message, GET /v1/assistant/conversations/{id}
│   │   │   ├── confirmation.py         # POST /v1/assistant/confirm, POST /v1/assistant/cancel
│   │   │   └── health.py               # GET /health, GET /ready
│   │   ├── core/
│   │   │   ├── service_dictionary.json # Configurable 6 services, aliases & 10-location gazetteer
│   │   │   ├── service_dictionary.py   # Longest-phrase regex + difflib fuzzy token matcher (0.84)
│   │   │   ├── date_time_parser.py     # Timezone-aware parser, calendar validator & ambiguity detector
│   │   │   ├── entity_extractor.py     # Service, location, price, distance, ref & booking ID extractor
│   │   │   ├── intent_parser.py        # 13-intent priority-ordered deterministic classifier
│   │   │   └── ranking_engine.py       # Price, distance, rating, earliest & normalized composite ranker
│   │   ├── services/
│   │   │   ├── backend_client.py       # Async HTTPX client with typed error mapping
│   │   │   ├── search_coordinator.py   # Backend search + RankingEngine orchestration
│   │   │   ├── booking_coordinator.py  # Availability verification, SHA-256 idempotency, status & cancel
│   │   │   └── conversation_manager.py # 12-state FSM, multi-turn slot filling & reference resolution
│   │   ├── repositories/
│   │   │   └── conversation_repository.py
│   │   ├── schemas/
│   │   │   ├── intents.py
│   │   │   ├── requests.py
│   │   │   └── responses.py
│   │   └── utils/
│   │       ├── normalization.py        # Typo map, contractions & currency normalization
│   │       └── validators.py           # Length validation & booking code/UUID regex extraction
│   ├── tests/
│   │   ├── conftest.py
│   │   ├── test_intent_parser.py
│   │   ├── test_entity_extractor.py
│   │   ├── test_search.py
│   │   ├── test_conversation.py
│   │   └── test_booking_flow.py
│   ├── requirements.txt
│   └── .env.example
├── load_tests/
│   └── locustfile.py                   # Locust RPS, latency & slot-contention load test
├── start_services.ps1                  # One-command local launcher with startup health checks
└── README.md
```

---

## 10. Installation, Configuration & Running Locally

### 1. Create Virtual Environment & Install Dependencies
```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -r main_backend\requirements.txt -r ai_booking_service\requirements.txt
```

### 2. Configure Environment Variables (Optional for Local Dev)
```powershell
Copy-Item main_backend\.env.example main_backend\.env
Copy-Item ai_booking_service\.env.example ai_booking_service\.env
```

> **Important:** `INTERNAL_API_TOKEN` must match in both `main_backend/.env` and `ai_booking_service/.env`.

#### Environment Variables Reference

**`main_backend/.env`**
| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///.../booking_platform.db` | SQLAlchemy database URL |
| `JWT_SECRET_KEY` | dev default | Secret key for signing HS256 customer JWTs (override in prod) |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm |
| `JWT_EXPIRE_MINUTES` | `1440` | Access token lifetime in minutes (24 hours) |
| `INTERNAL_API_TOKEN` | dev default | Shared secret verified via `X-Internal-Token` on `/internal/*` |
| `AI_SERVICE_URL` | `http://127.0.0.1:8001` | Base URL of `ai_booking_service` |
| `AI_REQUEST_TIMEOUT` | `15.0` | Proxy timeout in seconds for forwarding to `ai_booking_service` |
| `DEFAULT_TIMEZONE` | `Asia/Kolkata` | Explicit IANA timezone for slot generation |
| `AUTO_SEED_DEMO_DATA` | `true` | Idempotently seed demo users, services, providers & 7-day slots |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

**`ai_booking_service/.env`**
| Variable | Default | Description |
|---|---|---|
| `BACKEND_URL` | `http://127.0.0.1:8000` | Base URL of `main_backend` |
| `INTERNAL_API_TOKEN` | dev default | Shared token sent in `X-Internal-Token` |
| `REQUEST_TIMEOUT` | `10.0` | HTTPX request timeout to `main_backend` (seconds) |
| `DEFAULT_TIMEZONE` | `Asia/Kolkata` | Explicit IANA timezone for date/time parsing |
| `CONVERSATION_TTL_MINUTES` | `30` | Sliding expiration window per conversation |
| `RATE_LIMIT_REQUESTS` | `60` | Max requests per sliding window per client IP + prefix |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Sliding window duration in seconds |
| `MAX_MESSAGE_LENGTH` | `1000` | Max customer message character length |
| `MAX_SEARCH_RESULTS` | `10` | Max ranked providers returned per search |
| `RANK_WEIGHT_PRICE` | `0.35` | Composite `"best"` ranking weight for price |
| `RANK_WEIGHT_DISTANCE` | `0.25` | Composite `"best"` ranking weight for distance |
| `RANK_WEIGHT_RATING` | `0.25` | Composite `"best"` ranking weight for customer rating |
| `RANK_WEIGHT_AVAILABILITY` | `0.15` | Composite `"best"` ranking weight for availability |
| `LOG_LEVEL` | `INFO` | Structured JSON logging level |

### 3. Run Both Services Locally

**Option A: One-Command PowerShell Launcher (Recommended)**
Starts both services via `.venv\Scripts\python.exe -m uvicorn`, checks startup exit codes after 2 seconds, monitors both processes, and cleanly terminates both on `Ctrl+C`:
```powershell
.\start_services.ps1
```

**Option B: Two Separate Terminals**
```powershell
# Terminal 1 — Main Backend (:8000)
cd main_backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# Terminal 2 — AI Booking Microservice (:8001)
cd ai_booking_service
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

### Interactive Endpoints & Seeded Demo Data
- **Web Chat UI & FSM Inspector**: `http://127.0.0.1:8000/`
- **Main Backend Swagger UI**: `http://127.0.0.1:8000/docs`
- **AI Microservice Swagger UI**: `http://127.0.0.1:8001/docs`
- **Seeded Demo Accounts**:
  - `alice@example.com` / `Password123!`
  - `bob@example.com` / `Password123!`
- **Seeded Catalog**: 6 services, **33 Ernakulam/Kochi/Aluva-area providers** (**44 service offerings** — including unrated listings with `rating=None` to verify zero rating fabrication, plus `listing_id`, `phone_public`, `verification_status`, and `source_name`), and 8 daily 1-hour slots (`09:00`–`12:00`, `14:00`–`17:00`, capacity `1`) for `today` through `today + 7 days` (**2,112 total slots**).
  - Default INR base prices by category: Car Wash (`₹300`, 60m), Electrical Repair (`₹300`, 60m), Plumbing (`₹350`, 60m), Appliance Repair (`₹450`, 60m), AC Repair (`₹500`, 60m), Home Cleaning (`₹750`, 120m).

---

## 11. Complete API Reference & Payloads

### Main Backend Public APIs (`:8000`)

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/` | Public | Serves interactive Web Chat UI (`index.html`) |
| `GET` | `/health` · `/ready` | Public | Liveness probe & SQLite `SELECT 1` readiness check |
| `POST` | `/api/v1/auth/register` | Public | Register account (returns HS256 JWT + user) |
| `POST` | `/api/v1/auth/login` | Public | Authenticate email/password (returns HS256 JWT) |
| `GET` | `/api/v1/auth/me` | Bearer JWT | Return authenticated customer profile (including saved current location) |
| `PUT` | `/api/v1/auth/location` | Bearer JWT | Save or clear the customer's current GPS coordinates & area label |
| `GET` | `/api/v1/services` | Public | List service catalog |
| `GET` | `/api/v1/providers` | Public | List active providers |
| `GET` | `/api/v1/bookings` | Bearer JWT | List current customer's bookings |
| `POST` | `/api/v1/chat/message` | Bearer JWT | Proxy to `ai_booking_service` `/v1/assistant/message` |
| `POST` | `/api/v1/chat/confirm` | Bearer JWT | Proxy to `ai_booking_service` `/v1/assistant/confirm` |
| `POST` | `/api/v1/chat/cancel` | Bearer JWT | Proxy to `ai_booking_service` `/v1/assistant/cancel` |
| `GET` | `/api/v1/chat/conversations/{id}` | Bearer JWT | Proxy to `ai_booking_service` `/v1/assistant/conversations/{id}` |

### AI Booking Microservice APIs (`:8001`)

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `POST` | `/v1/assistant/message` | Bearer JWT | Process natural-language message through NLU + FSM |
| `POST` | `/v1/assistant/confirm` | Bearer JWT | Explicitly confirm (`true`) or decline (`false`) pending selection |
| `POST` | `/v1/assistant/cancel` | Bearer JWT | Cancel pending conversation or confirmed booking |
| `GET` | `/v1/assistant/conversations/{id}` | Bearer JWT | Fetch user-owned conversation state & context |
| `GET` | `/health` · `/ready` | Public | Liveness & downstream `main_backend` readiness probe |

### Main Backend Internal APIs (`:8000/internal/*`, `X-Internal-Token` Required)

| Method | Endpoint | Customer JWT | Description |
|---|---|---|---|
| `POST` | `/internal/auth/verify` | Required | Verify customer Bearer token & return user identity |
| `POST` | `/internal/services/search` | Not required | Filter providers by service, location/Haversine, price & slot |
| `POST` | `/internal/availability/check` | Not required | Live check of active provider, working hours, price & remaining capacity |
| `POST` | `/internal/bookings/create` | Required | Concurrency-safe `BEGIN IMMEDIATE` atomic booking creation |
| `GET` | `/internal/bookings` | Required | List user's recent bookings |
| `GET` | `/internal/bookings/{booking_id}` | Required | Fetch booking by UUID or `BK-...` code (ownership enforced) |
| `POST` | `/internal/bookings/{booking_id}/cancel` | Required | Atomic cancellation & slot capacity release |
| `POST` | `/internal/conversations` | Required | Create persistent conversation record with TTL |
| `GET` | `/internal/conversations/{id}` | Required | Fetch conversation record (ownership enforced) |
| `PUT` | `/internal/conversations/{id}` | Required | Update FSM state, `context_json`, and optional sliding TTL |

### Example Request & Response (`POST /v1/assistant/message`)

**Request** (optionally includes the user's live GPS/area coordinates to measure distance to every provider)
```json
{
  "message": "Find the cheapest car wash near Aluva tomorrow at 2 PM",
  "latitude": 10.0553,
  "longitude": 76.3219,
  "location_label": "Kalamassery"
}
```

**Response**
```json
{
  "success": true,
  "conversation_id": "a6f7f8c0-...",
  "status": "OPTIONS_FOUND",
  "message": "I found 3 available providers for 2026-11-20 at 2:00 PM. The cheapest option is Topnotch Car Wash and Detailing (4.32 km from Kalamassery) for ₹300. Say 'Book that one' or pick a provider (e.g., 'Book the second provider') to proceed.",
  "intent": {
    "type": "FIND_CHEAPEST",
    "confidence": 0.93,
    "service": "car_wash",
    "service_category": "automotive",
    "location": "Aluva",
    "date": "2026-11-20",
    "time": "14:00",
    "sort_by": "price",
    "booking_id": null,
    "ambiguities": []
  },
  "data": {
    "user_location": {
      "label": "Kalamassery",
      "latitude": 10.0553,
      "longitude": 76.3219
    },
    "providers": [
      {
        "provider_id": "...",
        "listing_id": "ERK-002",
        "provider_name": "Topnotch Car Wash and Detailing",
        "location_name": "Aluva",
        "address": "Metro Pillar 100, NH47, Companypady, Choornikkara, Aluva, Kerala 683106",
        "phone_public": "+91 97479 99804",
        "latitude": 10.0882,
        "longitude": 76.3428,
        "distance_km": 4.32,
        "rating": 4.9,
        "review_count": 18,
        "verification_status": "public_listing_found_verify_before_onboarding",
        "source_name": "Google business listing",
        "provider_service_id": "...",
        "price": 300.0,
        "currency": "INR",
        "slot_id": "...",
        "slot_date": "2026-11-20",
        "start_time": "14:00",
        "end_time": "15:00",
        "available": true,
        "ranking_score": 1.0,
        "ranking_explanation": "Ranked #1 by lowest price (INR 300, available)."
      }
    ],
    "sort_by": "price"
  },
  "requires_confirmation": false,
  "missing_fields": [],
  "error_code": null
}
```

### Standard Error Codes

| Error Code | Source | Meaning |
|---|---|---|
| `INVALID_INPUT` | AI Service | Empty message or length > `MAX_MESSAGE_LENGTH` |
| `RATE_LIMITED` | AI Service (`429`) | Client exceeded sliding-window rate limit |
| `CONVERSATION_EXPIRED` | AI Service | Conversation exceeded `CONVERSATION_TTL_MINUTES` |
| `INVALID_DATE` / `AMBIGUOUS_DATETIME` | NLU | Past/invalid date (e.g., `2026-02-30`) or vague phrase (`morning`, `next week`) |
| `AMBIGUOUS_SERVICE` / `UNKNOWN_SERVICE` | NLU | Multiple services in one turn or unsupported service requested |
| `NO_PROVIDERS_FOUND` | Search | No active available provider matched criteria |
| `PRICE_CHANGED` | Main Backend (`409`) | Provider price changed since selection; triggers explicit re-confirmation |
| `SLOT_UNAVAILABLE` / `OUTSIDE_WORKING_HOURS` | Main Backend (`409`) | Slot filled concurrently or outside provider working hours |
| `DUPLICATE_BOOKING` / `IDEMPOTENCY_CONFLICT` | Main Backend (`409`) | Duplicate active slot booking or mismatched idempotency fingerprint |
| `BACKEND_TIMEOUT` / `BACKEND_UNAVAILABLE` | HTTPX Client (`504`/`503`) | Downstream `main_backend` unreachable or timed out |

---

## 12. Automated Tests & Locust Load Testing

### Pytest Suites
```powershell
# 1. AI Microservice unit & integration tests (44 tests)
#    Covers: 13 intents, list-all browse queries, entity extraction, typos, date ambiguities, ranking, 12-state FSM, user GPS location, end-to-end booking flows
.\.venv\Scripts\python.exe -m pytest ai_booking_service\tests -v

# 2. Main Backend concurrency, security & persistence tests (6 tests)
#    Covers: multi-threaded SQLite BEGIN IMMEDIATE race conditions, idempotency replay/conflict,
#            price change rollback, cancellation slot release, user current location persistence & distance search, JWT & X-Internal-Token enforcement
.\.venv\Scripts\python.exe -m pytest main_backend\tests -v
```

### Locust Load Testing
[`load_tests/locustfile.py`](load_tests/locustfile.py) registers unique virtual customers and runs a weighted traffic mix:
- **Weight 4**: Single-turn search & ranking queries across services and areas
- **Weight 2**: Multi-turn search → select (`"Book that one"`) → confirm contention on the same popular slot (verifying `SLOT_UNAVAILABLE` conflicts are handled safely with zero 500s)
- **Weight 1**: Booking status lookups

```powershell
# Headless benchmark (30 concurrent users, spawn rate 5/s, 30s duration)
.\.venv\Scripts\locust.exe -f load_tests\locustfile.py --host http://127.0.0.1:8000 --headless -u 30 -r 5 -t 30s

# Or launch the Locust Web UI on http://localhost:8089
.\.venv\Scripts\locust.exe -f load_tests\locustfile.py --host http://127.0.0.1:8000
```

---

## 13. Production Security & Scaling Guide

1. **Strong Secrets Management**: Replace default `JWT_SECRET_KEY` and `INTERNAL_API_TOKEN` with cryptographically random secrets injected from a secret manager or environment variables.
2. **Private Network Isolation**: Never expose `/internal/*` on public ingress/reverse proxies; restrict access to private mesh/VPC traffic alongside `X-Internal-Token`.
3. **Distributed Rate Limiting**: Replace the in-memory `SlidingWindowRateLimiter` with a Redis-backed limiter when scaling `ai_booking_service` horizontally across multiple pods/workers.
4. **Horizontal Scaling & PostgreSQL Migration**:
   - `ai_booking_service` is completely stateless and scales horizontally behind any HTTP load balancer.
   - To scale writes beyond local SQLite, point `DATABASE_URL` to PostgreSQL and swap SQLite's `BEGIN IMMEDIATE` in [`main_backend/app/database.py`](main_backend/app/database.py) for row-level `SELECT ... FOR UPDATE` on `availability_slots`—with **zero API contract changes**.
5. **Extending Services & Locations**: Add new services, aliases, or cities directly in [`ai_booking_service/app/core/service_dictionary.json`](ai_booking_service/app/core/service_dictionary.json) without touching parser code.

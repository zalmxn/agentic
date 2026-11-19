from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AvailabilitySlot, Provider, ProviderService, Service, User
from app.security import hash_password

DEMO_USERS = [
    ("alice@example.com", "Alice Nair", "Password123!"),
    ("bob@example.com", "Bob Thomas", "Password123!"),
]

SERVICES_SEED = [
    ("car_wash", "Car Wash", "automotive", "Exterior and interior car washing and detailing", 60),
    ("plumbing", "Plumbing", "home_maintenance", "Pipe repair, leak fixing, and bathroom fittings", 60),
    ("ac_repair", "AC Repair", "appliance", "Air conditioner repair, gas refill, and servicing", 60),
    ("electrical", "Electrical Repair", "home_maintenance", "Wiring, switchboard, fan, and inverter repairs", 60),
    ("home_cleaning", "Home Cleaning", "cleaning", "Deep home and kitchen cleaning services", 120),
    ("appliance_repair", "Appliance Repair", "appliance", "Washing machine, fridge, and microwave repair", 60),
]

# Realistic Kochi / Aluva area providers
PROVIDERS_SEED = [
    {
        "name": "ABC Car Wash",
        "location_name": "Aluva",
        "address": "Pump Junction, Aluva, Kerala",
        "latitude": 10.1076,
        "longitude": 76.3516,
        "rating": 4.6,
        "review_count": 142,
        "work_start_time": "09:00",
        "work_end_time": "19:00",
        "services": [("car_wash", 250.0, 60)],
    },
    {
        "name": "Sparkle Auto Spa",
        "location_name": "Aluva",
        "address": "Bank Junction, Aluva, Kerala",
        "latitude": 10.1105,
        "longitude": 76.3542,
        "rating": 4.8,
        "review_count": 218,
        "work_start_time": "09:00",
        "work_end_time": "19:00",
        "services": [("car_wash", 350.0, 60)],
    },
    {
        "name": "Metro Car Care",
        "location_name": "Kalamassery",
        "address": "NH 544, Kalamassery, Kochi",
        "latitude": 10.0553,
        "longitude": 76.3219,
        "rating": 4.3,
        "review_count": 88,
        "work_start_time": "09:00",
        "work_end_time": "18:00",
        "services": [("car_wash", 290.0, 60)],
    },
    {
        "name": "Kochi Quick Plumbers",
        "location_name": "Kochi",
        "address": "MG Road, Ernakulam, Kochi",
        "latitude": 9.9679,
        "longitude": 76.2869,
        "rating": 4.5,
        "review_count": 165,
        "work_start_time": "08:00",
        "work_end_time": "19:00",
        "services": [("plumbing", 300.0, 60), ("electrical", 320.0, 60)],
    },
    {
        "name": "Reliable PipeWorks",
        "location_name": "Kochi",
        "address": "Kaloor Kadavanthra Rd, Kochi",
        "latitude": 9.9942,
        "longitude": 76.2921,
        "rating": 4.7,
        "review_count": 97,
        "work_start_time": "09:00",
        "work_end_time": "18:00",
        "services": [("plumbing", 420.0, 60)],
    },
    {
        "name": "Aluva Flow Plumbing",
        "location_name": "Aluva",
        "address": "Bypass Rd, Aluva, Kerala",
        "latitude": 10.1041,
        "longitude": 76.3488,
        "rating": 4.4,
        "review_count": 64,
        "work_start_time": "09:00",
        "work_end_time": "18:00",
        "services": [("plumbing", 280.0, 60)],
    },
    {
        "name": "CoolBreeze AC Experts",
        "location_name": "Kochi",
        "address": "Edappally Toll, Kochi",
        "latitude": 10.0261,
        "longitude": 76.3125,
        "rating": 4.8,
        "review_count": 310,
        "work_start_time": "09:00",
        "work_end_time": "19:00",
        "services": [("ac_repair", 550.0, 60), ("appliance_repair", 450.0, 60)],
    },
    {
        "name": "FrostLine HVAC Care",
        "location_name": "Aluva",
        "address": "Periyar Nagar, Aluva",
        "latitude": 10.1128,
        "longitude": 76.3571,
        "rating": 4.4,
        "review_count": 109,
        "work_start_time": "09:00",
        "work_end_time": "18:00",
        "services": [("ac_repair", 480.0, 60)],
    },
    {
        "name": "VoltMaster Electricians",
        "location_name": "Kakkanad",
        "address": "Infopark Rd, Kakkanad, Kochi",
        "latitude": 10.0159,
        "longitude": 76.3419,
        "rating": 4.7,
        "review_count": 195,
        "work_start_time": "08:00",
        "work_end_time": "20:00",
        "services": [("electrical", 350.0, 60), ("appliance_repair", 400.0, 60)],
    },
    {
        "name": "SafeWire Electricals",
        "location_name": "Aluva",
        "address": "Market Rd, Aluva",
        "latitude": 10.1060,
        "longitude": 76.3502,
        "rating": 4.2,
        "review_count": 51,
        "work_start_time": "09:00",
        "work_end_time": "18:00",
        "services": [("electrical", 260.0, 60)],
    },
    {
        "name": "Pristine Home Cleaners",
        "location_name": "Edappally",
        "address": "Lulu Mall Junction, Edappally, Kochi",
        "latitude": 10.0274,
        "longitude": 76.3083,
        "rating": 4.9,
        "review_count": 240,
        "work_start_time": "09:00",
        "work_end_time": "18:00",
        "services": [("home_cleaning", 899.0, 120)],
    },
    {
        "name": "Budget Shine Cleaners",
        "location_name": "Kochi",
        "address": "Vyttila Hub Rd, Kochi",
        "latitude": 9.9685,
        "longitude": 76.3182,
        "rating": None,  # Intentionally unrated to verify no fabricated ratings
        "review_count": 0,
        "work_start_time": "09:00",
        "work_end_time": "18:00",
        "services": [("home_cleaning", 650.0, 120), ("car_wash", 275.0, 60)],
    },
]

SLOT_HOURS = [9, 10, 11, 12, 14, 15, 16, 17]


def seed_database(db: Session, days_ahead: int = 7) -> None:
    """Idempotently populate demo users, services, providers, pricing, and slots."""
    # 1. Users
    for email, full_name, password in DEMO_USERS:
        existing = db.query(User).filter(User.email == email).first()
        if not existing:
            db.add(User(email=email, full_name=full_name, password_hash=hash_password(password)))
    db.flush()

    # 2. Services
    service_map: dict[str, Service] = {}
    for code, name, category, desc, duration in SERVICES_SEED:
        svc = db.query(Service).filter(Service.code == code).first()
        if not svc:
            svc = Service(
                code=code,
                name=name,
                category=category,
                description=desc,
                default_duration_minutes=duration,
            )
            db.add(svc)
            db.flush()
        service_map[code] = svc

    # 3. Providers + ProviderService mappings
    providers_list: list[Provider] = []
    for pdata in PROVIDERS_SEED:
        provider = db.query(Provider).filter(Provider.name == pdata["name"]).first()
        if not provider:
            provider = Provider(
                name=pdata["name"],
                location_name=pdata["location_name"],
                address=pdata["address"],
                latitude=pdata["latitude"],
                longitude=pdata["longitude"],
                rating=pdata["rating"],
                review_count=pdata["review_count"],
                work_start_time=pdata["work_start_time"],
                work_end_time=pdata["work_end_time"],
                is_active=True,
            )
            db.add(provider)
            db.flush()
        providers_list.append(provider)

        for svc_code, price, duration in pdata["services"]:
            svc = service_map[svc_code]
            ps = (
                db.query(ProviderService)
                .filter(
                    ProviderService.provider_id == provider.id,
                    ProviderService.service_id == svc.id,
                )
                .first()
            )
            if not ps:
                db.add(
                    ProviderService(
                        provider_id=provider.id,
                        service_id=svc.id,
                        price=price,
                        currency="INR",
                        duration_minutes=duration,
                        is_active=True,
                    )
                )
    db.flush()

    # 4. Availability slots for today..today+days_ahead in DEFAULT_TIMEZONE
    tz = ZoneInfo(get_settings().default_timezone)
    today = datetime.now(tz).date()

    for provider in providers_list:
        for offset in range(days_ahead + 1):
            slot_date = (today + timedelta(days=offset)).isoformat()
            existing_times = {
                row[0]
                for row in db.query(AvailabilitySlot.start_time)
                .filter(
                    AvailabilitySlot.provider_id == provider.id,
                    AvailabilitySlot.slot_date == slot_date,
                )
                .all()
            }
            for hour in SLOT_HOURS:
                start_str = f"{hour:02d}:00"
                end_str = f"{hour + 1:02d}:00"
                if start_str < provider.work_start_time or end_str > provider.work_end_time:
                    continue
                if start_str not in existing_times:
                    db.add(
                        AvailabilitySlot(
                            provider_id=provider.id,
                            slot_date=slot_date,
                            start_time=start_str,
                            end_time=end_str,
                            capacity=1,
                            booked_count=0,
                            is_active=True,
                        )
                    )
    db.commit()

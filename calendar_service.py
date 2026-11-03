import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]
TIME_ZONE = os.getenv("GOOGLE_CALENDAR_TIMEZONE", "Europe/Zagreb")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
CREDENTIALS_FILE = os.getenv(
    "GOOGLE_CALENDAR_CREDENTIALS_FILE",
    "google_credentials.json",
)
TOKEN_FILE = os.getenv("GOOGLE_CALENDAR_TOKEN_FILE", "google_token.json")


def get_credentials():
    credentials = None

    if os.path.exists(TOKEN_FILE):
        credentials = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"Nedostaje {CREDENTIALS_FILE}. Preuzmi OAuth podatke za "
                    "Desktop app iz Google Cloud Consolea."
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE,
                SCOPES,
            )
            credentials = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w", encoding="utf-8") as token:
            token.write(credentials.to_json())

    return credentials


def _service():
    return build(
        "calendar",
        "v3",
        credentials=get_credentials(),
        cache_discovery=False,
    )


def _iso(value):
    if not value:
        return value
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(TIME_ZONE))
    return parsed.isoformat()


def _start_of_day_iso(value):
    """Pretvori početak datumskog raspona u lokalnu ponoć.

    Cjelodnevni događaji, uključujući rođendane, počinju u 00:00. Ako model
    pošalje trenutačni sat kao timeMin, Google ih inače više ne vraća.
    """
    if not value:
        return value
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    local_zone = ZoneInfo(TIME_ZONE)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_zone)
    else:
        parsed = parsed.astimezone(local_zone)
    return parsed.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _local_datetime(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    local_zone = ZoneInfo(TIME_ZONE)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=local_zone)
    return parsed.astimezone(local_zone)


def _event_local_date(event):
    start = event.get("start", {})
    date_value = start.get("date")
    if date_value:
        return datetime.fromisoformat(date_value).date()
    date_time_value = start.get("dateTime")
    if date_time_value:
        return _local_datetime(date_time_value).date()
    return None


def _requested_date_range(time_min, time_max):
    start = _local_datetime(time_min)
    end = _local_datetime(time_max)
    start_date = start.date()
    # timeMax je ekskluzivan. Ako nije točno ponoć, uključuje i taj datum.
    if end.timetz().replace(tzinfo=None) == time.min:
        end_date = end.date()
    else:
        end_date = end.date() + timedelta(days=1)
    return start_date, end_date


def _event_view(event):
    start = event.get("start", {})
    end = event.get("end", {})
    return {
        "id": event.get("id"),
        "title": event.get("summary", "(bez naziva)"),
        "start": start.get("dateTime", start.get("date")),
        "end": end.get("dateTime", end.get("date")),
        "location": event.get("location"),
        "description": event.get("description"),
        "link": event.get("htmlLink"),
        "event_type": event.get("eventType", "default"),
        "birthday_properties": event.get("birthdayProperties"),
    }


def list_calendars():
    items = _service().calendarList().list().execute().get("items", [])
    return [
        {
            "id": item.get("id"),
            "name": item.get("summary"),
            "primary": item.get("primary", False),
            "access": item.get("accessRole"),
        }
        for item in items
    ]


def list_events(time_min, time_max, limit=20, calendar_id=None,
                start_from_beginning_of_day=False):
    events = (
        _service()
        .events()
        .list(
            calendarId=calendar_id or CALENDAR_ID,
            timeMin=(
                _start_of_day_iso(time_min)
                if start_from_beginning_of_day
                else _iso(time_min)
            ),
            timeMax=_iso(time_max),
            maxResults=max(1, min(int(limit), 50)),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )
    return [_event_view(event) for event in events]


def _fold_calendar_text(value):
    return (value or "").strip().lower().translate(
        str.maketrans("čćđšž", "ccdsz")
    )


def _is_birthdays_calendar(calendar):
    calendar_id = _fold_calendar_text(calendar.get("id"))
    name = _fold_calendar_text(calendar.get("summary"))
    description = _fold_calendar_text(calendar.get("description"))
    return (
        "#contacts@group.v.calendar.google.com" in calendar_id
        or "birthday" in name
        or "rodendan" in name
        or "birthday" in description
        or "rodendan" in description
    )


def _is_birthday_event(event):
    if event.get("eventType") == "birthday" or event.get("birthdayProperties"):
        return True
    text = _fold_calendar_text(
        f"{event.get('summary', '')} {event.get('description', '')}"
    )
    return "birthday" in text or "rodendan" in text


def list_birthdays(time_min, time_max, limit=100):
    """Dohvati rođendane iz Googleova i ručno vođenih kalendara."""
    service = _service()
    local_zone = ZoneInfo(TIME_ZONE)
    start_date, end_date = _requested_date_range(time_min, time_max)
    # Namjerno širi API raspon za jedan dan na obje strane. Konačni rezultat
    # filtrira se lokalno, čime uklanjamo razlike kalendarske i lokalne zone.
    query_start = datetime.combine(
        start_date - timedelta(days=1), time.min, tzinfo=local_zone
    )
    query_end = datetime.combine(
        end_date + timedelta(days=1), time.min, tzinfo=local_zone
    )
    calendars = (
        service.calendarList()
        .list(maxResults=250, showHidden=True)
        .execute()
        .get("items", [])
    )

    # Google rođendan može biti eventType="birthday" u bilo kojem čitljivom
    # kalendaru, a naslov ne mora sadržavati riječ "rođendan". Zato pregledamo
    # sve kalendare i potom strogo filtriramo događaje.
    selected = [
        calendar for calendar in calendars
        if calendar.get("accessRole") in {
            "reader", "writerWithoutPrivateAccess", "writer", "owner"
        }
    ]

    result = []
    seen = set()
    max_items = max(1, min(int(limit), 250))

    for calendar in selected:
        remaining = max_items - len(result)
        if remaining <= 0:
            break
        try:
            events = (
                service.events()
                .list(
                    calendarId=calendar.get("id"),
                    timeMin=query_start.isoformat(),
                    timeMax=query_end.isoformat(),
                    timeZone=TIME_ZONE,
                    maxResults=250,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
                .get("items", [])
            )
        except Exception as error:
            print(
                "KALENDAR ROĐENDANI - preskačem kalendar:",
                calendar.get("summary"),
                repr(error),
            )
            continue
        birthdays_calendar = _is_birthdays_calendar(calendar)
        for event in events:
            if not birthdays_calendar and not _is_birthday_event(event):
                continue
            event_date = _event_local_date(event)
            if event_date is None or not (start_date <= event_date < end_date):
                continue
            item = _event_view(event)
            item["calendar_id"] = calendar.get("id")
            item["calendar"] = calendar.get("summary")
            unique_key = (item.get("title"), item.get("start"), item.get("end"))
            if unique_key in seen:
                continue
            seen.add(unique_key)
            result.append(item)

    result.sort(key=lambda item: (item.get("start") or "", item.get("title") or ""))
    return result[:max_items]


def diagnose_birthdays(day=None):
    """Vrati sažet, sirov pregled onoga što Google API daje oko ciljanog dana."""
    local_zone = ZoneInfo(TIME_ZONE)
    target_date = day or datetime.now(local_zone).date()
    service = _service()
    calendars = (
        service.calendarList()
        .list(maxResults=250, showHidden=True)
        .execute()
        .get("items", [])
    )
    readable = [
        calendar for calendar in calendars
        if calendar.get("accessRole") in {
            "reader", "writerWithoutPrivateAccess", "writer", "owner"
        }
    ]
    query_start = datetime.combine(
        target_date - timedelta(days=1), time.min, tzinfo=local_zone
    )
    query_end = datetime.combine(
        target_date + timedelta(days=2), time.min, tzinfo=local_zone
    )
    calendar_rows = []
    event_rows = []

    for calendar in readable:
        calendar_rows.append({
            "name": calendar.get("summary", "(bez naziva)"),
            "access": calendar.get("accessRole"),
            "birthdays_calendar": _is_birthdays_calendar(calendar),
        })
        try:
            events = (
                service.events()
                .list(
                    calendarId=calendar.get("id"),
                    timeMin=query_start.isoformat(),
                    timeMax=query_end.isoformat(),
                    timeZone=TIME_ZONE,
                    maxResults=250,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
                .get("items", [])
            )
        except Exception as error:
            event_rows.append({
                "calendar": calendar.get("summary", "(bez naziva)"),
                "error": str(error),
            })
            continue

        for event in events:
            event_date = _event_local_date(event)
            if event_date is None:
                continue
            event_rows.append({
                "calendar": calendar.get("summary", "(bez naziva)"),
                "title": event.get("summary", "(bez naziva)"),
                "start": event.get("start", {}).get(
                    "date", event.get("start", {}).get("dateTime")
                ),
                "event_type": event.get("eventType", "default"),
                "has_birthday_properties": bool(event.get("birthdayProperties")),
                "recognized_as_birthday": (
                    _is_birthdays_calendar(calendar) or _is_birthday_event(event)
                ),
                "is_target_date": event_date == target_date,
            })

    start = datetime.combine(target_date, time.min, tzinfo=local_zone)
    end = start + timedelta(days=1)
    detected_today = list_birthdays(start.isoformat(), end.isoformat(), limit=250)
    return {
        "local_now": datetime.now(local_zone).isoformat(),
        "target_date": target_date.isoformat(),
        "calendars": calendar_rows,
        "events_around_target": event_rows,
        "detected_birthdays_today": detected_today,
    }


def get_event(event_id, calendar_id=None):
    event = (
        _service()
        .events()
        .get(calendarId=calendar_id or CALENDAR_ID, eventId=event_id)
        .execute()
    )
    return _event_view(event)


def check_free_busy(time_min, time_max, calendar_ids=None):
    ids = calendar_ids or [CALENDAR_ID]
    result = (
        _service()
        .freebusy()
        .query(
            body={
                "timeMin": _iso(time_min),
                "timeMax": _iso(time_max),
                "timeZone": TIME_ZONE,
                "items": [{"id": value} for value in ids],
            }
        )
        .execute()
    )
    return {
        calendar_id: data.get("busy", [])
        for calendar_id, data in result.get("calendars", {}).items()
    }


def create_event(title, start, end, description=None, location=None, calendar_id=None):
    body = {
        "summary": title,
        "start": {"dateTime": _iso(start), "timeZone": TIME_ZONE},
        "end": {"dateTime": _iso(end), "timeZone": TIME_ZONE},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location

    event = (
        _service()
        .events()
        .insert(calendarId=calendar_id or CALENDAR_ID, body=body)
        .execute()
    )
    return _event_view(event)


def update_event(event_id, title=None, start=None, end=None, description=None,
                 location=None, calendar_id=None):
    service = _service()
    selected_calendar = calendar_id or CALENDAR_ID
    event = service.events().get(
        calendarId=selected_calendar,
        eventId=event_id,
    ).execute()

    if title is not None:
        event["summary"] = title
    if start is not None:
        event["start"] = {"dateTime": _iso(start), "timeZone": TIME_ZONE}
    if end is not None:
        event["end"] = {"dateTime": _iso(end), "timeZone": TIME_ZONE}
    if description is not None:
        event["description"] = description
    if location is not None:
        event["location"] = location

    updated = service.events().update(
        calendarId=selected_calendar,
        eventId=event_id,
        body=event,
    ).execute()
    return _event_view(updated)


def delete_event(event_id, calendar_id=None):
    _service().events().delete(
        calendarId=calendar_id or CALENDAR_ID,
        eventId=event_id,
    ).execute()
    return {"deleted": True, "event_id": event_id}

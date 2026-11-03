from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

import calendar_service


def _service():
    return build(
        "tasks",
        "v1",
        credentials=calendar_service.get_credentials(),
        cache_discovery=False,
    )


def list_task_lists():
    return [
        {"id": item.get("id"), "title": item.get("title", "Zadaci")}
        for item in _service().tasklists().list(maxResults=100).execute().get("items", [])
    ]


def _task_view(task, task_list):
    return {
        "id": task.get("id"),
        "title": task.get("title", "(bez naziva)"),
        "notes": task.get("notes", ""),
        "due": task.get("due"),
        "status": task.get("status"),
        "task_list_id": task_list.get("id"),
        "task_list": task_list.get("title", "Zadaci"),
    }


def _selected_task_list(service, task_list_id=None):
    if task_list_id:
        return {"id": task_list_id, "title": "Zadaci"}
    lists = service.tasklists().list(maxResults=100).execute().get("items", [])
    if not lists:
        raise RuntimeError("Google Tasks nema dostupnu listu zadataka.")
    return lists[0]


def list_open_tasks(limit=50, task_list_id=None):
    service = _service()
    lists = [_selected_task_list(service, task_list_id)] if task_list_id else list_task_lists()
    result = []

    for task_list in lists:
        page_token = None
        while len(result) < max(1, min(int(limit), 100)):
            response = service.tasks().list(
                tasklist=task_list["id"],
                showCompleted=False,
                showHidden=False,
                maxResults=min(100, max(1, int(limit) - len(result))),
                pageToken=page_token,
            ).execute()
            result.extend(
                _task_view(task, task_list) for task in response.get("items", [])
            )
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    return result[:max(1, min(int(limit), 100))]


def _local_due_date(value):
    """Return the calendar date of a Google Task deadline.

    Google Tasks API documents ``due`` as a *date* even though it serializes it
    as an RFC 3339 timestamp. Converting that value to a local timezone can
    move a task to the previous or following day, so retain the API date part.
    The API does not expose the task's reminder time or recurrence rule.
    """
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def get_due_tasks(today=None, upcoming_days=7):
    local_zone = ZoneInfo(calendar_service.TIME_ZONE)
    today = today or datetime.now(local_zone).date()
    upcoming_until = today + timedelta(days=upcoming_days)
    service = _service()
    lists = service.tasklists().list(maxResults=100).execute().get("items", [])
    overdue = []
    due_today = []
    upcoming = []
    without_due = []

    for task_list in lists:
        page_token = None
        while True:
            response = service.tasks().list(
                tasklist=task_list["id"],
                showCompleted=False,
                # Recurring Google Tasks may be returned as hidden generated
                # instances. Fetch all open tasks and filter locally so that a
                # due-date filter does not omit today's recurring occurrence.
                showHidden=True,
                maxResults=100,
                pageToken=page_token,
            ).execute()

            for task in response.get("items", []):
                due_date = _local_due_date(task.get("due"))
                item = _task_view(task, task_list)
                if not due_date:
                    # Google Tasks does not expose reminder time through its
                    # public API. A task scheduled only with a reminder has no
                    # ``due`` value, but it still must be included in the
                    # daily report instead of being silently omitted.
                    without_due.append(item)
                    continue
                item["due"] = due_date.isoformat()
                if due_date < today:
                    overdue.append(item)
                elif due_date == today:
                    due_today.append(item)
                elif due_date <= upcoming_until:
                    upcoming.append(item)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

    overdue.sort(key=lambda item: (item["due"], item["title"]))
    due_today.sort(key=lambda item: item["title"])
    upcoming.sort(key=lambda item: (item["due"], item["title"]))
    without_due.sort(key=lambda item: (item["task_list"], item["title"]))
    return {
        "overdue": overdue,
        "today": due_today,
        "upcoming": upcoming,
        "without_due": without_due,
    }


def create_task(title, due=None, notes=None, task_list_id=None):
    service = _service()
    task_list = _selected_task_list(service, task_list_id)
    body = {"title": title}
    if due:
        body["due"] = due
    if notes:
        body["notes"] = notes
    task = service.tasks().insert(tasklist=task_list["id"], body=body).execute()
    return _task_view(task, task_list)


def update_task(task_id, title=None, due=None, notes=None, task_list_id=None):
    service = _service()
    task_list = _selected_task_list(service, task_list_id)
    task = service.tasks().get(tasklist=task_list["id"], task=task_id).execute()
    if title is not None:
        task["title"] = title
    if due is not None:
        task["due"] = due
    if notes is not None:
        task["notes"] = notes
    updated = service.tasks().update(
        tasklist=task_list["id"], task=task_id, body=task
    ).execute()
    return _task_view(updated, task_list)


def delete_task(task_id, task_list_id=None):
    service = _service()
    task_list = _selected_task_list(service, task_list_id)
    service.tasks().delete(tasklist=task_list["id"], task=task_id).execute()
    return {"deleted": True, "task_id": task_id}

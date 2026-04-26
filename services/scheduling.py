from datetime import datetime
from models import db, TaskSchedule
from task_definitions import TASKS


def get_task_dates(task_number: int) -> tuple:
    """
    回傳 (opens_at, deadline_at, date_source)。
    先查 TaskSchedule（effective_from <= now 的最新 row）；
    無則 fallback 到 task_definitions.py 的靜態值。
    """
    sched = (TaskSchedule.query
             .filter_by(task_number=task_number)
             .filter(TaskSchedule.effective_from <= datetime.utcnow())
             .order_by(TaskSchedule.effective_from.desc())
             .first())
    if sched:
        return sched.opens_at, sched.deadline_at, sched.date_source

    task_def = TASKS.get(task_number, {})
    opens_str    = task_def.get('opens_at', '2025-09-01')
    deadline_str = task_def.get('deadline_at', '2025-12-31')
    date_source  = task_def.get('date_source', 'syllabus_default')
    return (
        datetime.fromisoformat(opens_str),
        datetime.fromisoformat(deadline_str),
        date_source,
    )

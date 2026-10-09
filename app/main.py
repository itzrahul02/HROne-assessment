"""
Employee Attendance & Analytics API.

Run from the repository root:
    uvicorn app.main:app --port 8000

MONGO_URI and MONGO_DB come from the environment. A local .env is loaded for
convenience and never overrides variables that are already set. Copy
.env.example and replace the dummy MongoDB URL before starting the app.
"""
import calendar
import json
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# Real environment variables win over .env. The dummy URL lives in
# .env.example so a real URI is never hard-coded here.
def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and replace the dummy MongoDB URL."
        )
    return value


MONGO_URI = _required_env("MONGO_URI")
MONGO_DB = _required_env("MONGO_DB")

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
ABSENCE = ("ABSENT", "LEAVE")
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000
DAY_SECONDS = 24 * 60 * 60

IDX_EMP_CODE = "emp_code_unique"
IDX_DEPT_JOINED = "dept_joined"
IDX_JOINED = "joined_on"
IDX_EMP_DATE = "emp_date_unique"
IDX_DATE_EMP = "date_emp"
IDX_STATUS_DATE_EMP = "status_date_emp"
IDX_EMP_STATUS_DATE = "emp_status_date"
IDX_DATE_LATE = "date_late"
IDX_EMP_PUNCH = "emp_punch_in"

client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    tzinfo=UTC,
    serverSelectionTimeoutMS=8000,
    connectTimeoutMS=8000,
)
db = client[MONGO_DB]

_indexes_ready = False
_indexes_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Time and business rules (R1-R5)
# --------------------------------------------------------------------------- #
def hhmm_minutes(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    # Numeric comparison. Zero-padded HH:MM strings do not sort chronologically
    # ("09:30" > "18:30"), so a string compare would mis-classify day shifts.
    return hhmm_minutes(shift_end) <= hhmm_minutes(shift_start)


def to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = value - epoch
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def ms_to_utc(ms: int) -> datetime:
    """Truncate to a whole second, then convert. The truncated instant is stored."""
    seconds = int(ms) // 1000
    return datetime.fromtimestamp(seconds, tz=UTC)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def combine_ist(day: date, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1. IST calendar date, with the overnight shift rolled back before shift_end."""
    local = punch_in.astimezone(IST)
    if is_overnight(shift_start, shift_end):
        end_h, end_m = map(int, shift_end.split(":"))
        if (local.hour, local.minute, local.second) < (end_h, end_m, 0):
            return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def shift_start_dt(attendance_day: str, shift_start: str) -> datetime:
    return combine_ist(date.fromisoformat(attendance_day), shift_start)


def shift_end_dt(attendance_day: str, shift_start: str, shift_end: str) -> datetime:
    day = date.fromisoformat(attendance_day)
    if is_overnight(shift_start, shift_end):
        day += timedelta(days=1)
    return combine_ist(day, shift_end)


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: str) -> int:
    """R2. Late only when strictly more than 10:00 after shift start.

    late_minutes is the whole minutes since shift_start, not since the grace ended.
    09:30 shift: 09:40:00 -> 0, 09:40:01 -> 10, 10:15:59 -> 45.
    """
    elapsed = (punch_in - shift_start_dt(attendance_day, shift_start)).total_seconds()
    if elapsed <= 10 * 60:
        return 0
    return int(elapsed // 60)


def work_hours_and_half_day(punch_in: datetime, punch_out: datetime) -> tuple[float, bool]:
    """R4 and R5. Half-up to 2 decimals; half day when that rounded value is < 4.50."""
    seconds = int((punch_out - punch_in).total_seconds())
    rounded = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    half = rounded < Decimal("4.50")
    return json.loads(f"{rounded:.2f}"), half


def compute_overtime(
    punch_out: datetime, shift_start: str, shift_end: str, attendance_day: str
) -> int:
    """R3. Whole minutes after shift end, and only when that is at least 30."""
    elapsed = (punch_out - shift_end_dt(attendance_day, shift_start, shift_end)).total_seconds()
    if elapsed < 0:
        return 0
    minutes = int(elapsed // 60)
    return minutes if minutes >= 30 else 0


def derive_fields(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    shift_start: str,
    shift_end: str,
    attendance_day: str,
) -> dict[str, Any]:
    if status in ABSENCE:
        return {
            "status": status,
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    late = compute_late_minutes(punch_in, shift_start, attendance_day)
    if punch_out is None:
        return {
            "status": status,
            "punch_in": punch_in,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": late,
            "overtime_minutes": 0,
            "half_day": False,
        }
    hours, half = work_hours_and_half_day(punch_in, punch_out)
    return {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, shift_start, shift_end, attendance_day),
        "half_day": half,
    }


# --------------------------------------------------------------------------- #
# Reported-number rounding (R8) and JSON shaping
# --------------------------------------------------------------------------- #
def div_round_half_up_scaled(numerator: int, denominator: int, places: int) -> int:
    """round_half_up(numerator / denominator, places), returned as an integer scaled by 10**places."""
    scale = 10 ** places
    quotient, remainder = divmod(int(numerator) * scale, int(denominator))
    if remainder * 2 >= int(denominator):
        quotient += 1
    return quotient


def scaled_int_to_float(scaled: int, places: int) -> float:
    text = f"{Decimal(int(scaled)) / (Decimal(10) ** places):.{places}f}"
    return json.loads(text)


def ratio_half_up(numerator: int, denominator: int, places: int) -> float:
    return scaled_int_to_float(div_round_half_up_scaled(numerator, denominator, places), places)


def half_units_to_number(units: int) -> int | float:
    whole, remainder = divmod(int(units), 2)
    if remainder == 0:
        return whole
    return json.loads(f"{whole}.5")


def as_int(value: Any) -> int:
    if value is None:
        return 0
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def maybe_scaled(value: Any, places: int) -> Optional[float]:
    if value is None:
        return None
    return scaled_int_to_float(as_int(value), places)


def same_work_hours(left: Any, right: Any) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    quant = Decimal("0.01")
    return (
        Decimal(str(left)).quantize(quant, rounding=ROUND_HALF_UP)
        == Decimal(str(right)).quantize(quant, rounding=ROUND_HALF_UP)
    )


def _change_value(field: str, value: Any) -> Any:
    if field in ("punch_in", "punch_out"):
        return to_ms(value) if isinstance(value, datetime) else value
    return value


def serialize_history(history: Optional[list]) -> list:
    if not history:
        return []
    rendered = []
    for entry in history:
        changes = {}
        for field, diff in (entry.get("changes") or {}).items():
            changes[field] = {
                "from": _change_value(field, diff.get("from")),
                "to": _change_value(field, diff.get("to")),
            }
        rendered.append(
            {
                "at": to_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )
    return rendered


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": as_int(doc.get("late_minutes")),
        "overtime_minutes": as_int(doc.get("overtime_minutes")),
        "half_day": bool(doc.get("half_day", False)),
        "history": serialize_history(doc.get("history")),
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc["created_at"]),
    }


def fail_validation(message: str, loc: list) -> None:
    raise RequestValidationError(
        [{"type": "value_error", "loc": tuple(loc), "msg": message, "input": None}]
    )


def parse_date_str(value: str, loc: list) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        fail_validation("date must be YYYY-MM-DD", loc)
    try:
        date.fromisoformat(value)
    except ValueError:
        fail_validation("date must be a real calendar date", loc)
    return value


def parse_month(value: Optional[str], loc: Optional[list] = None) -> str:
    loc = loc or ["query", "month"]
    if value is None:
        fail_validation("month is required", loc)
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        fail_validation("month must be YYYY-MM", loc)
    return value


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = map(int, month.split("-"))
    start = date(year, mon, 1)
    end = date(year, mon, calendar.monthrange(year, mon)[1])
    return start, end


# --------------------------------------------------------------------------- #
# Indexes. Created at startup and retried on the first request if MongoDB
# was not reachable yet. create_index is idempotent.
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    with _indexes_lock:
        if _indexes_ready:
            return
        db.employees.create_index([("emp_code", 1)], unique=True, name=IDX_EMP_CODE)
        db.employees.create_index([("department", 1), ("joined_on", 1)], name=IDX_DEPT_JOINED)
        db.employees.create_index([("joined_on", 1)], name=IDX_JOINED)
        db.attendance_logs.create_index(
            [("emp_code", 1), ("date", 1)], unique=True, name=IDX_EMP_DATE
        )
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name=IDX_DATE_EMP)
        db.attendance_logs.create_index(
            [("status", 1), ("date", -1), ("emp_code", 1)], name=IDX_STATUS_DATE_EMP
        )
        db.attendance_logs.create_index(
            [("emp_code", 1), ("status", 1), ("date", -1)], name=IDX_EMP_STATUS_DATE
        )
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)], name=IDX_DATE_LATE)
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name=IDX_EMP_PUNCH)
        _indexes_ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except Exception:
        # Health must still be able to answer 503 when MongoDB is down.
        pass
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.middleware("http")
async def prepare_indexes(request, call_next):
    if request.url.path != "/health":
        ensure_indexes()
    return await call_next(request)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_date(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be a real calendar date") from exc
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    punch_out: Optional[int] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def resolve_punched_at(raw: Optional[int]) -> datetime:
    if raw is None:
        return now_utc()
    return ms_to_utc(raw)


def require_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Aggregation building blocks
# --------------------------------------------------------------------------- #
def logs_lookup(start: str, end: str) -> dict:
    """Per-employee logs in [start, end]. The first $match is an emp_code equality so the unique index is used."""
    return {
        "$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$emp_code", "$$code"]}}},
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {
                    "$project": {
                        "_id": 0,
                        "date": 1,
                        "status": 1,
                        "work_hours": 1,
                        "late_minutes": 1,
                        "overtime_minutes": 1,
                        "half_day": 1,
                    }
                },
            ],
            "as": "logs",
        }
    }


def _weekday(date_expr: str) -> dict:
    return {
        "$lte": [
            {
                "$isoDayOfWeek": {
                    "$dateFromString": {"dateString": date_expr, "timezone": "UTC"}
                }
            },
            5,
        ]
    }


def metrics_reduce() -> dict:
    """One pass over an employee's logs.

    present_half_units counts Mon-Fri presence only (half day = 1, full day = 2).
    Late, overtime, leave, on-duty and work-hour totals include every day of the
    month. Analytics read these stored fields; they do not recompute them.
    """
    present_units = {
        "$cond": [
            {"$and": [{"$in": ["$$this.status", list(PRESENCE)]}, _weekday("$$this.date")]},
            {"$cond": [{"$eq": [{"$ifNull": ["$$this.half_day", False]}, True]}, 1, 2]},
            0,
        ]
    }
    has_hours = {
        "$and": [
            {"$in": ["$$this.status", list(PRESENCE)]},
            {"$isNumber": "$$this.work_hours"},
        ]
    }
    return {
        "$reduce": {
            "input": {"$ifNull": ["$logs", []]},
            "initialValue": {
                "present_half_units": 0,
                "leave_days": 0,
                "late_count": 0,
                "total_late_minutes": 0,
                "total_overtime_minutes": 0,
                "on_duty_count": 0,
                "wh_cents": 0,
                "wh_count": 0,
            },
            "in": {
                "present_half_units": {"$add": ["$$value.present_half_units", present_units]},
                "leave_days": {
                    "$add": [
                        "$$value.leave_days",
                        {"$cond": [{"$eq": ["$$this.status", "LEAVE"]}, 1, 0]},
                    ]
                },
                "late_count": {
                    "$add": [
                        "$$value.late_count",
                        {"$cond": [{"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}, 1, 0]},
                    ]
                },
                "total_late_minutes": {
                    "$add": ["$$value.total_late_minutes", {"$ifNull": ["$$this.late_minutes", 0]}]
                },
                "total_overtime_minutes": {
                    "$add": [
                        "$$value.total_overtime_minutes",
                        {"$ifNull": ["$$this.overtime_minutes", 0]},
                    ]
                },
                "on_duty_count": {
                    "$add": [
                        "$$value.on_duty_count",
                        {"$cond": [{"$eq": ["$$this.status", "ON_DUTY"]}, 1, 0]},
                    ]
                },
                "wh_cents": {
                    "$add": [
                        "$$value.wh_cents",
                        {
                            "$cond": [
                                has_hours,
                                {"$round": [{"$multiply": ["$$this.work_hours", 100]}, 0]},
                                0,
                            ]
                        },
                    ]
                },
                "wh_count": {"$add": ["$$value.wh_count", {"$cond": [has_hours, 1, 0]}]},
            },
        }
    }


def working_days_expr(month_start: datetime, n_days: int) -> dict:
    """Mon-Fri from joined_on through the end of the month (R7)."""
    return {
        "$size": {
            "$filter": {
                "input": {"$range": [0, n_days]},
                "as": "i",
                "cond": {
                    "$let": {
                        "vars": {
                            "day": {
                                "$dateToString": {
                                    "format": "%Y-%m-%d",
                                    "date": {
                                        "$dateAdd": {
                                            "startDate": month_start,
                                            "unit": "day",
                                            "amount": "$$i",
                                        }
                                    },
                                    "timezone": "UTC",
                                }
                            }
                        },
                        "in": {
                            "$and": [
                                {"$gte": ["$$day", "$joined_on"]},
                                _weekday("$$day"),
                            ]
                        },
                    }
                },
            }
        }
    }


def agg_round_div(numerator: Any, denominator: Any) -> dict:
    """Integer division rounded half up. Inputs are non-negative."""
    return {
        "$let": {
            "vars": {"num": numerator, "den": denominator},
            "in": {
                "$let": {
                    "vars": {
                        "q": {"$toLong": {"$floor": {"$divide": ["$$num", "$$den"]}}},
                        "r": {"$toLong": {"$mod": ["$$num", "$$den"]}},
                    },
                    "in": {
                        "$cond": [
                            {"$gte": [{"$multiply": ["$$r", 2]}, "$$den"]},
                            {"$add": ["$$q", 1]},
                            "$$q",
                        ]
                    },
                }
            },
        }
    }


def run_aggregate(collection: str, pipeline: list, hint: str) -> list:
    ensure_indexes()
    return list(db[collection].aggregate(pipeline, hint=hint, allowDiskUse=True))


def explain_aggregate(collection: str, pipeline: list, hint: str) -> dict:
    ensure_indexes()
    raw = db.command(
        {
            "explain": {
                "aggregate": collection,
                "pipeline": pipeline,
                "allowDiskUse": True,
                "hint": hint,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
    )
    return json.loads(json_util.dumps(raw))


def explain_find(spec: dict) -> dict:
    ensure_indexes()
    raw = db.command(
        {
            "explain": {
                "find": "attendance_logs",
                "filter": spec["filter"],
                "sort": spec["sort"],
                "skip": spec["skip"],
                "limit": spec["limit"],
                "hint": spec["hint"],
            },
            "verbosity": "executionStats",
        }
    )
    return json.loads(json_util.dumps(raw))


def employee_monthly_query(emp_code: str, month: str) -> tuple[str, str, list]:
    start, end = month_bounds(month)
    n_days = calendar.monthrange(start.year, start.month)[1]
    start_dt = datetime(start.year, start.month, 1, tzinfo=UTC)
    pipeline = [
        {"$match": {"emp_code": emp_code}},
        logs_lookup(start.isoformat(), end.isoformat()),
        {
            "$addFields": {
                "metrics": metrics_reduce(),
                "working_days": working_days_expr(start_dt, n_days),
            }
        },
        {
            "$project": {
                "_id": 0,
                "present_half_units": "$metrics.present_half_units",
                "leave_days": "$metrics.leave_days",
                "late_count": "$metrics.late_count",
                "total_late_minutes": "$metrics.total_late_minutes",
                "total_overtime_minutes": "$metrics.total_overtime_minutes",
                "working_days": 1,
            }
        },
    ]
    return "employees", IDX_EMP_CODE, pipeline


def format_monthly(emp_code: str, month: str, row: dict) -> dict:
    working = as_int(row["working_days"])
    units = as_int(row["present_half_units"])
    pct = None if working == 0 else ratio_half_up(units * 50, working, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": half_units_to_number(units),
        "leave_days": as_int(row["leave_days"]),
        "late_count": as_int(row["late_count"]),
        "total_late_minutes": as_int(row["total_late_minutes"]),
        "total_overtime_minutes": as_int(row["total_overtime_minutes"]),
        "attendance_pct": pct,
    }


def department_summary_query(month: str, department: Optional[str]) -> tuple[str, str, list]:
    start, end = month_bounds(month)
    match: dict[str, Any] = {"joined_on": {"$lte": end.isoformat()}}
    hint = IDX_JOINED
    if department is not None:
        match["department"] = department
        hint = IDX_DEPT_JOINED
    pipeline = [
        {"$match": match},
        logs_lookup(start.isoformat(), end.isoformat()),
        {"$addFields": {"metrics": metrics_reduce()}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_half_units": {"$sum": "$metrics.present_half_units"},
                "leave_count": {"$sum": "$metrics.leave_days"},
                "late_count": {"$sum": "$metrics.late_count"},
                "total_late_minutes": {"$sum": "$metrics.total_late_minutes"},
                "on_duty_count": {"$sum": "$metrics.on_duty_count"},
                "wh_cents": {"$sum": "$metrics.wh_cents"},
                "wh_count": {"$sum": "$metrics.wh_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_half_units": 1,
                "leave_count": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "on_duty_count": 1,
                "wh_cents": 1,
                "wh_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]
    return "employees", hint, pipeline


def format_summary_item(row: dict) -> dict:
    count = as_int(row["wh_count"])
    if count == 0:
        avg = None
    else:
        # wh_cents is the sum of per-record cents. Round the mean back to 2 decimals.
        avg = scaled_int_to_float(div_round_half_up_scaled(as_int(row["wh_cents"]), count, 0), 2)
    return {
        "department": row["department"],
        "headcount": as_int(row["headcount"]),
        "present_days": half_units_to_number(as_int(row["present_half_units"])),
        "avg_work_hours": avg,
        "late_count": as_int(row["late_count"]),
        "total_late_minutes": as_int(row["total_late_minutes"]),
        "leave_count": as_int(row["leave_count"]),
        "on_duty_count": as_int(row["on_duty_count"]),
    }


def late_leaderboard_query(
    month: str, limit: int, department: Optional[str]
) -> tuple[str, str, list]:
    start, end = month_bounds(month)
    pipeline: list[dict] = [
        {
            "$match": {
                "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return "attendance_logs", IDX_DATE_LATE, pipeline


def format_leader_row(row: dict) -> dict:
    return {
        "rank": as_int(row["rank"]),
        "emp_code": row["emp_code"],
        "name": row["name"],
        "department": row["department"],
        "total_late_minutes": as_int(row["total_late_minutes"]),
        "late_count": as_int(row["late_count"]),
    }


def _trend_day_expr(start_dt: datetime) -> dict:
    return {
        "$let": {
            "vars": {
                "day": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": {
                            "$dateAdd": {
                                "startDate": start_dt,
                                "unit": "day",
                                "amount": "$$i",
                            }
                        },
                        "timezone": "UTC",
                    }
                }
            },
            "in": {
                "$let": {
                    "vars": {
                        "stat": {
                            "$arrayElemAt": [
                                {
                                    "$filter": {
                                        "input": "$daily",
                                        "as": "d",
                                        "cond": {"$eq": ["$$d._id", "$$day"]},
                                    }
                                },
                                0,
                            ]
                        }
                    },
                    "in": {
                        "date": "$$day",
                        "is_working_day": _weekday("$$day"),
                        "headcount": {
                            "$size": {
                                "$filter": {
                                    "input": "$emps",
                                    "as": "e",
                                    "cond": {"$lte": ["$$e.joined_on", "$$day"]},
                                }
                            }
                        },
                        "present_half_units": {"$ifNull": ["$$stat.present_half_units", 0]},
                        "late_count": {"$ifNull": ["$$stat.late_count", 0]},
                    },
                }
            },
        }
    }


def department_trend_query(department: str, start: date, end: date) -> tuple[str, str, list]:
    n_days = (end - start).days + 1
    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
    start_s, end_s = start.isoformat(), end.isoformat()
    pipeline = [
        {"$match": {"department": department}},
        logs_lookup(start_s, end_s),
        {
            "$facet": {
                "emps": [{"$project": {"_id": 0, "joined_on": 1}}],
                "daily": [
                    {"$unwind": "$logs"},
                    {
                        "$group": {
                            "_id": "$logs.date",
                            "present_half_units": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$logs.status", list(PRESENCE)]},
                                        {
                                            "$cond": [
                                                {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]},
                                                1,
                                                2,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
            }
        },
        {"$match": {"emps.0": {"$exists": True}}},
        {
            "$project": {
                "rows": {
                    "$map": {
                        "input": {"$range": [0, n_days]},
                        "as": "i",
                        "in": _trend_day_expr(start_dt),
                    }
                }
            }
        },
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {
            "$addFields": {
                "rate_scaled": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        agg_round_div(
                            {"$multiply": ["$present_half_units", 10000]},
                            {"$multiply": [2, "$headcount"]},
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "rate_sum": {
                        "$sum": "$rate_scaled",
                        "window": {"documents": [-6, 0]},
                    },
                    "rate_cnt": {
                        "$sum": {
                            "$cond": [{"$ne": ["$rate_scaled", None]}, 1, 0]
                        },
                        "window": {"documents": [-6, 0]},
                    },
                },
            }
        },
        {
            "$addFields": {
                "moving_scaled": {
                    "$cond": [
                        {"$gt": ["$rate_cnt", 0]},
                        agg_round_div("$rate_sum", "$rate_cnt"),
                        None,
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_half_units": 1,
                "late_count": 1,
                "rate_scaled": 1,
                "moving_scaled": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]
    return "employees", IDX_DEPT_JOINED, pipeline


def format_trend_row(row: dict) -> dict:
    return {
        "date": row["date"],
        "is_working_day": bool(row["is_working_day"]),
        "headcount": as_int(row["headcount"]),
        "present_count": half_units_to_number(as_int(row["present_half_units"])),
        "late_count": as_int(row["late_count"]),
        "attendance_rate": maybe_scaled(row.get("rate_scaled"), 4),
        "moving_avg_7d": maybe_scaled(row.get("moving_scaled"), 4),
    }


def parse_trend_range(raw_from: str, raw_to: str) -> tuple[date, date]:
    start_s = parse_date_str(raw_from, ["query", "from"])
    end_s = parse_date_str(raw_to, ["query", "to"])
    start, end = date.fromisoformat(start_s), date.fromisoformat(end_s)
    if end < start:
        fail_validation("to must be on or after from", ["query", "to"])
    if (end - start).days + 1 > 92:
        fail_validation("date range must not exceed 92 days", ["query", "to"])
    return start, end


def attendance_list_spec(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
    page: int,
    page_size: int,
) -> dict:
    start = parse_date_str(date_from, ["query", "date_from"]) if date_from else None
    end = parse_date_str(date_to, ["query", "date_to"]) if date_to else None
    if start and end and start > end:
        fail_validation("date_from must be on or before date_to", ["query", "date_from"])
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if start or end:
        query["date"] = {}
        if start:
            query["date"]["$gte"] = start
        if end:
            query["date"]["$lte"] = end
    if status is not None:
        query["status"] = status
    if emp_code is not None and status is not None:
        hint = IDX_EMP_STATUS_DATE
    elif status is not None:
        hint = IDX_STATUS_DATE_EMP
    elif emp_code is not None:
        hint = IDX_EMP_DATE
    else:
        hint = IDX_DATE_EMP
    return {
        "filter": query,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
        "hint": hint,
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": datetime.now(UTC),
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department
    total = db.employees.count_documents(query)
    docs = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = require_employee(body.emp_code)
    punched = resolve_punched_at(body.punched_at)
    day = attendance_date(punched, emp["shift_start"], emp["shift_end"])
    derived = derive_fields(
        body.status, punched, None, emp["shift_start"], emp["shift_end"], day
    )
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": derived["status"],
        "punch_in": derived["punch_in"],
        "punch_out": None,
        "work_hours": None,
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = require_employee(body.emp_code)
    punched = resolve_punched_at(body.punched_at)
    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched, "$type": "date"}},
        sort=[("punch_in", -1), ("date", -1)],
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")
    delta = (punched - doc["punch_in"]).total_seconds()
    if delta <= 0 or delta > DAY_SECONDS:
        fail_validation(
            "punched_at must be after punch_in and within 24 hours",
            ["body", "punched_at"],
        )
    derived = derive_fields(
        doc["status"],
        doc["punch_in"],
        punched,
        emp["shift_start"],
        emp["shift_end"],
        doc["date"],
    )
    updated = db.attendance_logs.find_one_and_update(
        {"_id": doc["_id"], "punch_out": None, "punch_in": doc["punch_in"]},
        {
            "$set": {
                "punch_out": derived["punch_out"],
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = attendance_list_spec(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(spec["filter"], hint=spec["hint"])
    docs = (
        db.attendance_logs.find(spec["filter"], {"_id": 0})
        .sort(list(spec["sort"].items()))
        .skip(spec["skip"])
        .limit(spec["limit"])
        .hint(spec["hint"])
    )
    return {
        "items": [serialize_attendance(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _logical_record(doc: dict) -> dict:
    return {
        "status": doc["status"],
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": as_int(doc.get("late_minutes")),
        "overtime_minutes": as_int(doc.get("overtime_minutes")),
        "half_day": bool(doc.get("half_day", False)),
    }


def _build_changes(before: dict, after: dict) -> dict:
    changes = {}
    for field in (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ):
        old, new = before[field], after[field]
        if field in ("punch_in", "punch_out"):
            changed = to_ms(old) != to_ms(new)
        elif field == "work_hours":
            changed = not same_work_hours(old, new)
        else:
            changed = old != new
        if changed:
            changes[field] = {"from": old, "to": new}
    return changes


@app.patch("/attendance/{emp_code}/{day}")
def regularize_attendance(emp_code: str, day: str, body: RegularizeRequest):
    day = parse_date_str(day, ["path", "date"])
    emp = require_employee(emp_code)
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if doc is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    provided = body.model_fields_set
    if "status" in provided and body.status is None:
        fail_validation("status cannot be null", ["body", "status"])
    if "punch_in" in provided and body.punch_in is None:
        fail_validation("punch_in cannot be null", ["body", "punch_in"])
    if "punch_out" in provided and body.punch_out is None:
        fail_validation("punch_out cannot be null", ["body", "punch_out"])

    before = _logical_record(doc)
    new_status = body.status if "status" in provided else before["status"]
    if new_status in ABSENCE:
        if "punch_in" in provided or "punch_out" in provided:
            fail_validation(
                "punch_in and punch_out cannot be set when status is ABSENT or LEAVE",
                ["body", "status"],
            )
        new_in = None
        new_out = None
    else:
        new_in = ms_to_utc(body.punch_in) if "punch_in" in provided else before["punch_in"]
        new_out = ms_to_utc(body.punch_out) if "punch_out" in provided else before["punch_out"]
        if new_in is None:
            fail_validation("a presence status requires punch_in", ["body", "punch_in"])
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != day:
            fail_validation(
                "punch_in must stay on the record's attendance date",
                ["body", "punch_in"],
            )
        if new_out is not None:
            delta = (new_out - new_in).total_seconds()
            if delta <= 0 or delta > DAY_SECONDS:
                fail_validation(
                    "punch_out must be after punch_in and within 24 hours",
                    ["body", "punch_out"],
                )

    after = derive_fields(
        new_status, new_in, new_out, emp["shift_start"], emp["shift_end"], day
    )
    changes = _build_changes(before, after)
    if not changes:
        fail_validation("request changes nothing", ["body"])

    history = doc.get("history")
    history_clause = (
        {"history": {"$exists": False}} if history is None else {"history": {"$size": len(history)}}
    )
    entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": day,
            "status": doc.get("status"),
            "punch_in": doc.get("punch_in"),
            "punch_out": doc.get("punch_out"),
            **history_clause,
        },
        {
            "$set": {
                "status": after["status"],
                "punch_in": after["punch_in"],
                "punch_out": after["punch_out"],
                "work_hours": after["work_hours"],
                "late_minutes": after["late_minutes"],
                "overtime_minutes": after["overtime_minutes"],
                "half_day": after["half_day"],
            },
            "$push": {"history": entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="concurrent modification")
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    month = parse_month(month)
    collection, hint, pipeline = employee_monthly_query(emp_code, month)
    rows = run_aggregate(collection, pipeline, hint)
    if not rows:
        raise HTTPException(status_code=404, detail="employee not found")
    return format_monthly(emp_code, month, rows[0])


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    month = parse_month(month)
    collection, hint, pipeline = department_summary_query(month, department)
    rows = run_aggregate(collection, pipeline, hint)
    return {"month": month, "items": [format_summary_item(row) for row in rows]}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month = parse_month(month)
    collection, hint, pipeline = late_leaderboard_query(month, limit, department)
    rows = run_aggregate(collection, pipeline, hint)
    return {"month": month, "items": [format_leader_row(row) for row in rows]}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    trend_from: str = Query(..., alias="from"),
    trend_to: str = Query(..., alias="to"),
):
    start, end = parse_trend_range(trend_from, trend_to)
    collection, hint, pipeline = department_trend_query(department, start, end)
    rows = run_aggregate(collection, pipeline, hint)
    if not rows:
        raise HTTPException(status_code=404, detail="department not found")
    return {"department": department, "items": [format_trend_row(row) for row in rows]}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    trend_from: Optional[str] = Query(None, alias="from"),
    trend_to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        spec = attendance_list_spec(emp_code, date_from, date_to, status, page, page_size)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_find(spec),
        }
    if endpoint == "employee_monthly":
        if not emp_code:
            fail_validation("emp_code is required", ["query", "emp_code"])
        month = parse_month(month)
        collection, hint, pipeline = employee_monthly_query(emp_code, month)
    elif endpoint == "department_summary":
        month = parse_month(month)
        collection, hint, pipeline = department_summary_query(month, department)
    elif endpoint == "late_leaderboard":
        month = parse_month(month)
        collection, hint, pipeline = late_leaderboard_query(month, limit, department)
    else:
        if not department:
            fail_validation("department is required", ["query", "department"])
        if not trend_from or not trend_to:
            fail_validation("from and to are required", ["query", "from"])
        start, end = parse_trend_range(trend_from, trend_to)
        collection, hint, pipeline = department_trend_query(department, start, end)
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain_aggregate(collection, pipeline, hint),
    }

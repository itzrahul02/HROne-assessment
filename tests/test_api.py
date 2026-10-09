"""Local checks for the attendance API. Not part of the submission."""
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ["MONGO_URI"] = os.environ.get("TEST_MONGO_URI", "mongodb://127.0.0.1:27017")
os.environ["MONGO_DB"] = "attendance_test"

from bson import json_util
from fastapi.testclient import TestClient

from app.main import (
    app,
    attendance_date,
    compute_late_minutes,
    compute_overtime,
    db,
    ensure_indexes,
    ms_to_utc,
    work_hours_and_half_day,
)

ROOT = Path(__file__).resolve().parents[1]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def epoch_ms(year, month, day, hour, minute, second=0, microsecond=0, ist=False):
    tz = IST if ist else UTC
    stamp = datetime(year, month, day, hour, minute, second, microsecond, tzinfo=tz)
    return int(stamp.timestamp() * 1000)


def approx(actual, expected):
    if expected is None:
        if actual is not None:
            raise AssertionError(actual)
        return
    got = Decimal(str(actual)).quantize(Decimal("0.0001"))
    want = Decimal(str(expected)).quantize(Decimal("0.0001"))
    if got != want:
        raise AssertionError(f"{actual} != {expected}")


class RuleTests(unittest.TestCase):
    def test_late_boundaries(self):
        day = "2026-07-06"
        cases = [
            (9, 40, 0, 0),
            (9, 40, 1, 10),
            (10, 15, 59, 45),
            (9, 30, 0, 0),
            (9, 20, 0, 0),
        ]
        for hour, minute, second, expected in cases:
            punch = datetime(2026, 7, 6, hour, minute, second, tzinfo=IST).astimezone(UTC)
            self.assertEqual(compute_late_minutes(punch, "09:30", day), expected)

    def test_truncation_before_late(self):
        raw = epoch_ms(2026, 7, 6, 9, 40, 0, 900000, ist=True)
        punch = ms_to_utc(raw)
        self.assertEqual(punch.astimezone(IST).second, 0)
        self.assertEqual(compute_late_minutes(punch, "09:30", "2026-07-06"), 0)
        raw = epoch_ms(2026, 7, 6, 9, 40, 1, 100000, ist=True)
        punch = ms_to_utc(raw)
        self.assertEqual(compute_late_minutes(punch, "09:30", "2026-07-06"), 10)

    def test_work_hours_half_up_and_half_day(self):
        start = datetime(2026, 7, 6, 9, 0, tzinfo=IST)
        hours, half = work_hours_and_half_day(start, start + timedelta(seconds=3618))
        self.assertEqual(hours, 1.01)
        self.assertTrue(half)
        hours, half = work_hours_and_half_day(start, start + timedelta(seconds=16200))
        self.assertEqual(hours, 4.5)
        self.assertFalse(half)
        hours, half = work_hours_and_half_day(start, start + timedelta(seconds=16182))
        self.assertEqual(hours, 4.5)
        self.assertFalse(half)
        hours, half = work_hours_and_half_day(start, start + timedelta(seconds=16181))
        self.assertEqual(hours, 4.49)
        self.assertTrue(half)

    def test_overtime_threshold_and_overnight(self):
        end = datetime(2026, 7, 6, 18, 59, tzinfo=IST).astimezone(UTC)
        self.assertEqual(compute_overtime(end, "09:30", "18:30", "2026-07-06"), 0)
        end = datetime(2026, 7, 6, 19, 0, tzinfo=IST).astimezone(UTC)
        self.assertEqual(compute_overtime(end, "09:30", "18:30", "2026-07-06"), 30)
        end = datetime(2026, 7, 7, 6, 20, tzinfo=IST).astimezone(UTC)
        self.assertEqual(compute_overtime(end, "22:00", "06:00", "2026-07-06"), 0)
        end = datetime(2026, 7, 7, 6, 40, tzinfo=IST).astimezone(UTC)
        self.assertEqual(compute_overtime(end, "22:00", "06:00", "2026-07-06"), 40)

    def test_overnight_attendance_date(self):
        early = datetime(2026, 7, 7, 5, 59, tzinfo=IST).astimezone(UTC)
        boundary = datetime(2026, 7, 7, 6, 0, tzinfo=IST).astimezone(UTC)
        evening = datetime(2026, 7, 6, 22, 15, tzinfo=IST).astimezone(UTC)
        self.assertEqual(attendance_date(early, "22:00", "06:00"), "2026-07-06")
        self.assertEqual(attendance_date(boundary, "22:00", "06:00"), "2026-07-07")
        self.assertEqual(attendance_date(evening, "22:00", "06:00"), "2026-07-06")
        day = datetime(2026, 7, 6, 23, 59, tzinfo=IST).astimezone(UTC)
        self.assertEqual(attendance_date(day, "09:30", "18:30"), "2026-07-06")
        nxt = datetime(2026, 7, 7, 0, 0, tzinfo=IST).astimezone(UTC)
        self.assertEqual(attendance_date(nxt, "09:30", "18:30"), "2026-07-07")


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_indexes()
        cls.client = TestClient(app)

    def setUp(self):
        db.employees.delete_many({})
        db.attendance_logs.delete_many({})

    def test_health(self):
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json(), {"status": "ok"})

    def test_create_employee_validation_and_duplicate(self):
        bad = self.client.post(
            "/employees",
            json={
                "emp_code": "EMP1",
                "name": "A",
                "email": "not-an-email",
                "department": "Engineering",
                "joined_on": "2026-02-31",
            },
        )
        self.assertEqual(bad.status_code, 422, bad.text)
        same_shift = self.client.post(
            "/employees",
            json={
                "emp_code": "EMP1001",
                "name": "A",
                "email": "a@example.com",
                "department": "Engineering",
                "shift_start": "09:30",
                "shift_end": "09:30",
                "joined_on": "2026-01-05",
            },
        )
        self.assertEqual(same_shift.status_code, 422, same_shift.text)
        created = self._employee("EMP1001")
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertNotIn("id", body)
        self.assertNotIn("_id", body)
        self.assertIsInstance(body["created_at"], int)
        self.assertGreater(body["created_at"], 100_000_000_000)
        again = self._employee("EMP1001")
        self.assertEqual(again.status_code, 409, again.text)

    def test_list_employees_filter_sort_and_page(self):
        for code, dept in (
            ("EMP0002", "Engineering"),
            ("EMP0001", "Sales"),
            ("EMP0003", "Engineering"),
        ):
            res = self._employee(code, department=dept)
            self.assertEqual(res.status_code, 201, res.text)
        page = self.client.get("/employees", params={"page": 1, "page_size": 2})
        self.assertEqual(page.status_code, 200, page.text)
        body = page.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual([item["emp_code"] for item in body["items"]], ["EMP0001", "EMP0002"])
        filtered = self.client.get("/employees", params={"department": "Engineering"})
        self.assertEqual(filtered.json()["total"], 2)
        self.assertEqual(
            [item["emp_code"] for item in filtered.json()["items"]],
            ["EMP0002", "EMP0003"],
        )
        self.assertEqual(self.client.get("/employees", params={"page": 0}).status_code, 422)
        self.assertEqual(self.client.get("/employees", params={"page_size": 101}).status_code, 422)

    def test_punch_in_rules_and_conflicts(self):
        self.assertEqual(self._employee("EMP2001").status_code, 201)
        missing = self.client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
        self.assertEqual(missing.status_code, 404, missing.text)
        seconds = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP2001", "punched_at": 1_700_000_000},
        )
        self.assertEqual(seconds.status_code, 422, seconds.text)
        on_time = epoch_ms(2026, 7, 6, 9, 40, 0, 900000, ist=True)
        res = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP2001", "punched_at": on_time, "status": "WFH"},
        )
        self.assertEqual(res.status_code, 201, res.text)
        body = res.json()
        self.assertNotIn("id", body)
        self.assertEqual(body["date"], "2026-07-06")
        self.assertEqual(body["status"], "WFH")
        self.assertEqual(body["late_minutes"], 0)
        self.assertIsNone(body["punch_out"])
        self.assertIsNone(body["work_hours"])
        self.assertEqual(body["history"], [])
        self.assertEqual(body["punch_in"], (on_time // 1000) * 1000)
        again = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP2001", "punched_at": on_time},
        )
        self.assertEqual(again.status_code, 409, again.text)

        self.assertEqual(self._employee("EMP2002").status_code, 201)
        late = self.client.post(
            "/attendance/punch-in",
            json={
                "emp_code": "EMP2002",
                "punched_at": epoch_ms(2026, 7, 6, 9, 40, 1, ist=True),
            },
        )
        self.assertEqual(late.status_code, 201, late.text)
        self.assertEqual(late.json()["late_minutes"], 10)

        self.assertEqual(
            self._employee("EMP2003", shift_start="22:00", shift_end="06:00").status_code,
            201,
        )
        overnight = self.client.post(
            "/attendance/punch-in",
            json={
                "emp_code": "EMP2003",
                "punched_at": epoch_ms(2026, 7, 7, 5, 30, ist=True),
            },
        )
        self.assertEqual(overnight.status_code, 201, overnight.text)
        self.assertEqual(overnight.json()["date"], "2026-07-06")
        self.assertEqual(overnight.json()["late_minutes"], 450)

    def test_punch_out_rules(self):
        self.assertEqual(self._employee("EMP3001").status_code, 201)
        punch_in = epoch_ms(2026, 7, 6, 9, 30, ist=True)
        created = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP3001", "punched_at": punch_in},
        )
        self.assertEqual(created.status_code, 201, created.text)
        too_soon = self.client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP3001", "punched_at": punch_in},
        )
        self.assertEqual(too_soon.status_code, 422, too_soon.text)
        short = self.client.post(
            "/attendance/punch-out",
            json={
                "emp_code": "EMP3001",
                "punched_at": epoch_ms(2026, 7, 6, 13, 0, ist=True),
            },
        )
        self.assertEqual(short.status_code, 200, short.text)
        self.assertEqual(short.json()["work_hours"], 3.5)
        self.assertTrue(short.json()["half_day"])
        self.assertEqual(short.json()["overtime_minutes"], 0)
        self.assertEqual(short.json()["history"], [])
        again = self.client.post(
            "/attendance/punch-out",
            json={
                "emp_code": "EMP3001",
                "punched_at": epoch_ms(2026, 7, 6, 18, 0, ist=True),
            },
        )
        self.assertEqual(again.status_code, 409, again.text)

        self.assertEqual(self._employee("EMP3002").status_code, 201)
        self.client.post(
            "/attendance/punch-in",
            json={
                "emp_code": "EMP3002",
                "punched_at": epoch_ms(2026, 7, 7, 9, 30, ist=True),
            },
        )
        overtime = self.client.post(
            "/attendance/punch-out",
            json={
                "emp_code": "EMP3002",
                "punched_at": epoch_ms(2026, 7, 7, 19, 0, ist=True),
            },
        )
        self.assertEqual(overtime.status_code, 200, overtime.text)
        self.assertEqual(overtime.json()["overtime_minutes"], 30)
        self.assertEqual(overtime.json()["work_hours"], 9.5)
        self.assertFalse(overtime.json()["half_day"])

        self.assertEqual(self._employee("EMP3003").status_code, 201)
        self.client.post(
            "/attendance/punch-in",
            json={
                "emp_code": "EMP3003",
                "punched_at": epoch_ms(2026, 7, 8, 9, 30, ist=True),
            },
        )
        exact = self.client.post(
            "/attendance/punch-out",
            json={
                "emp_code": "EMP3003",
                "punched_at": epoch_ms(2026, 7, 9, 9, 30, ist=True),
            },
        )
        self.assertEqual(exact.status_code, 200, exact.text)
        too_long = self.client.post(
            "/attendance/punch-out",
            json={
                "emp_code": "EMP3004",
                "punched_at": epoch_ms(2026, 7, 9, 9, 30, 1, ist=True),
            },
        )
        self.assertEqual(self._employee("EMP3004").status_code, 201)
        self.client.post(
            "/attendance/punch-in",
            json={
                "emp_code": "EMP3004",
                "punched_at": epoch_ms(2026, 7, 10, 9, 30, ist=True),
            },
        )
        too_long = self.client.post(
            "/attendance/punch-out",
            json={
                "emp_code": "EMP3004",
                "punched_at": epoch_ms(2026, 7, 11, 9, 30, 1, ist=True),
            },
        )
        self.assertEqual(too_long.status_code, 422, too_long.text)

        self.assertEqual(
            self._employee("EMP3005", shift_start="22:00", shift_end="06:00").status_code,
            201,
        )
        self.client.post(
            "/attendance/punch-in",
            json={
                "emp_code": "EMP3005",
                "punched_at": epoch_ms(2026, 7, 6, 21, 55, ist=True),
            },
        )
        closed = self.client.post(
            "/attendance/punch-out",
            json={
                "emp_code": "EMP3005",
                "punched_at": epoch_ms(2026, 7, 7, 6, 40, ist=True),
            },
        )
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertEqual(closed.json()["date"], "2026-07-06")
        self.assertEqual(closed.json()["work_hours"], 8.75)
        self.assertEqual(closed.json()["overtime_minutes"], 40)
        self.assertEqual(closed.json()["late_minutes"], 0)

    def test_regularize(self):
        self.assertEqual(self._employee("EMP4001").status_code, 201)
        punch_in = epoch_ms(2026, 7, 7, 10, 5, ist=True)
        punch_out = epoch_ms(2026, 7, 7, 18, 30, ist=True)
        self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP4001", "punched_at": punch_in},
        )
        self.client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP4001", "punched_at": punch_out},
        )
        nothing = self.client.patch(
            "/attendance/EMP4001/2026-07-07",
            json={"reason": "no change here", "regularized_by": "hr.admin"},
        )
        self.assertEqual(nothing.status_code, 422, nothing.text)
        corrected = epoch_ms(2026, 7, 7, 9, 28, ist=True)
        res = self.client.patch(
            "/attendance/EMP4001/2026-07-07",
            json={
                "punch_in": corrected,
                "reason": "biometric glitch",
                "regularized_by": "hr.admin",
            },
        )
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["late_minutes"], 0)
        self.assertEqual(body["work_hours"], 9.03)
        self.assertEqual(len(body["history"]), 1)
        self.assertEqual(body["history"][0]["by"], "hr.admin")
        self.assertEqual(body["history"][0]["changes"]["late_minutes"], {"from": 35, "to": 0})
        self.assertEqual(body["history"][0]["changes"]["punch_in"]["from"], (punch_in // 1000) * 1000)
        self.assertNotIn("status", body["history"][0]["changes"])

        absent = self.client.patch(
            "/attendance/EMP4001/2026-07-07",
            json={
                "status": "ABSENT",
                "punch_in": corrected,
                "reason": "should reject punches",
                "regularized_by": "hr.admin",
            },
        )
        self.assertEqual(absent.status_code, 422, absent.text)
        cleared = self.client.patch(
            "/attendance/EMP4001/2026-07-07",
            json={
                "status": "LEAVE",
                "reason": "approved after the fact",
                "regularized_by": "hr.admin",
            },
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertIsNone(cleared.json()["punch_in"])
        self.assertIsNone(cleared.json()["punch_out"])
        self.assertEqual(cleared.json()["late_minutes"], 0)
        self.assertIsNone(cleared.json()["work_hours"])
        self.assertEqual(len(cleared.json()["history"]), 2)

        wrong_day = self.client.patch(
            "/attendance/EMP4001/2026-07-07",
            json={
                "status": "PRESENT",
                "punch_in": epoch_ms(2026, 7, 8, 9, 30, ist=True),
                "reason": "wrong attendance date",
                "regularized_by": "hr.admin",
            },
        )
        self.assertEqual(wrong_day.status_code, 422, wrong_day.text)
        self.assertEqual(self.client.patch("/attendance/EMP4001/2026-02-31", json={
            "reason": "bad date value",
            "regularized_by": "hr",
        }).status_code, 422)
        self.assertEqual(
            self.client.patch(
                "/attendance/EMP4040/2026-07-07",
                json={"reason": "missing employee", "regularized_by": "hr"},
            ).status_code,
            404,
        )

    def test_sample_list_and_monthly(self):
        self._load_samples()
        listed = self.client.get("/attendance", params={"page": 1, "page_size": 20})
        self.assertEqual(listed.status_code, 200, listed.text)
        items = listed.json()["items"]
        self.assertEqual(listed.json()["total"], 9)
        self.assertEqual(
            [(item["date"], item["emp_code"]) for item in items],
            [
                ("2026-07-14", "EMP0002"),
                ("2026-07-08", "EMP0003"),
                ("2026-07-08", "EMP0004"),
                ("2026-07-07", "EMP0001"),
                ("2026-07-07", "EMP0004"),
                ("2026-07-07", "EMP0006"),
                ("2026-07-06", "EMP0001"),
                ("2026-07-06", "EMP0003"),
                ("2026-07-06", "EMP0005"),
            ],
        )
        legacy = items[0]
        self.assertFalse(legacy["half_day"])
        self.assertEqual(legacy["history"], [])
        self.assertNotIn("id", legacy)
        self.assertIsInstance(legacy["punch_in"], int)
        history = next(item for item in items if item["emp_code"] == "EMP0001" and item["date"] == "2026-07-07")
        self.assertIsInstance(history["history"][0]["at"], int)
        self.assertIsInstance(history["history"][0]["changes"]["punch_in"]["from"], int)
        bad_range = self.client.get(
            "/attendance", params={"date_from": "2026-07-10", "date_to": "2026-07-01"}
        )
        self.assertEqual(bad_range.status_code, 422, bad_range.text)

        monthly = self.client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
        self.assertEqual(monthly.status_code, 200, monthly.text)
        body = monthly.json()
        self.assertEqual(body["working_days"], 23)
        self.assertEqual(body["present_days"], 2)
        self.assertEqual(body["leave_days"], 0)
        approx(body["attendance_pct"], "8.70")
        joiner = self.client.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"})
        self.assertEqual(joiner.json()["working_days"], 15)
        self.assertEqual(joiner.json()["present_days"], 1)
        approx(joiner.json()["attendance_pct"], "6.67")
        half = self.client.get("/analytics/employees/EMP0004/monthly", params={"month": "2026-07"})
        self.assertEqual(half.json()["present_days"], 0.5)
        approx(half.json()["attendance_pct"], "2.17")
        late = self.client.get("/analytics/employees/EMP0003/monthly", params={"month": "2026-07"})
        self.assertEqual(late.json()["late_count"], 1)
        self.assertEqual(late.json()["total_late_minutes"], 35)
        self.assertEqual(late.json()["total_overtime_minutes"], 40)
        self.assertEqual(late.json()["leave_days"], 1)
        self.assertEqual(
            self.client.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code,
            404,
        )

    def test_department_summary_reads_stored_values(self):
        self._load_samples()
        self._insert_employee("EMP0099", "Engineering", "2026-01-01")
        self._insert_employee("EMP0098", "Engineering", "2026-08-01")
        # Weekend late must count toward late totals and not toward present_days.
        self._insert_log("EMP0001", "2026-07-11", late_minutes=15, overtime_minutes=40)
        # Stored half_day wins over work_hours. This record is a Wednesday.
        self._insert_log("EMP0099", "2026-07-01", work_hours=8, half_day=True, late_minutes=99)
        res = self.client.get("/analytics/departments/summary", params={"month": "2026-07"})
        self.assertEqual(res.status_code, 200, res.text)
        items = {item["department"]: item for item in res.json()["items"]}
        engineering = items["Engineering"]
        self.assertEqual(engineering["headcount"], 3)
        self.assertEqual(engineering["present_days"], 3.5)
        self.assertEqual(engineering["late_count"], 2)
        self.assertEqual(engineering["total_late_minutes"], 114)
        # 9.12, 9.03, 8.98, the Saturday 8.00 and the stored 8.00. Mean of records, not of people.
        approx(engineering["avg_work_hours"], "8.63")
        sales = items["Sales"]
        self.assertEqual(sales["headcount"], 2)
        self.assertEqual(sales["present_days"], 1.5)
        self.assertEqual(sales["leave_count"], 1)
        self.assertEqual(sales["late_count"], 1)
        self.assertEqual(sales["total_late_minutes"], 35)
        approx(sales["avg_work_hours"], "6.29")
        support = items["Support"]
        self.assertEqual(support["present_days"], 2)
        approx(support["avg_work_hours"], "8.75")
        self.assertEqual(support["on_duty_count"], 0)
        only = self.client.get(
            "/analytics/departments/summary",
            params={"month": "2026-07", "department": "Sales"},
        )
        self.assertEqual([item["department"] for item in only.json()["items"]], ["Sales"])
        june = self.client.get(
            "/analytics/departments/summary",
            params={"month": "2026-06", "department": "Engineering"},
        )
        # EMP0002 joined 13 Jul and EMP0098 joined 1 Aug, so June headcount is EMP0001 + EMP0099.
        self.assertEqual(june.json()["items"][0]["headcount"], 2)

    def test_leaderboard_ties_and_department_scope(self):
        self._insert_employee("EMP0101", "Engineering", "2026-01-01", name="Ana")
        self._insert_employee("EMP0103", "Engineering", "2026-01-01", name="Cara")
        self._insert_employee("EMP0102", "Engineering", "2026-01-01", name="Bea")
        self._insert_employee("EMP0104", "Sales", "2026-01-01", name="Dee")
        self._insert_log("EMP0101", "2026-08-03", late_minutes=100)
        self._insert_log("EMP0102", "2026-08-03", late_minutes=80)
        self._insert_log("EMP0103", "2026-08-04", late_minutes=80)
        self._insert_log("EMP0104", "2026-08-03", late_minutes=200)
        self._insert_log("EMP0101", "2026-08-08", late_minutes=5)  # Saturday
        self._insert_log("NOPE01", "2026-08-03", late_minutes=999)
        res = self.client.get(
            "/analytics/leaderboard/late",
            params={"month": "2026-08", "limit": 2, "department": "Engineering"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        rows = res.json()["items"]
        self.assertEqual(
            [(row["rank"], row["emp_code"], row["total_late_minutes"]) for row in rows],
            [(1, "EMP0101", 105), (2, "EMP0102", 80), (2, "EMP0103", 80)],
        )
        self.assertEqual(rows[1]["name"], "Bea")
        wide = self.client.get(
            "/analytics/leaderboard/late", params={"month": "2026-08", "limit": 1}
        )
        codes = [(row["rank"], row["emp_code"]) for row in wide.json()["items"]]
        self.assertEqual(codes, [(1, "EMP0104")])

    def test_trend_gaps_weekend_and_moving_average(self):
        self._insert_employee("EMP0201", "QA", "2026-07-01")
        self._insert_employee("EMP0202", "QA", "2026-07-08")
        # Mon 6 full, Tue 7 none, Wed 8 half, Thu 9 full, Fri 10 none, Sat 11 full + late.
        self._insert_log("EMP0201", "2026-07-06", late_minutes=0)
        self._insert_log("EMP0201", "2026-07-08", half_day=True, work_hours=3.5)
        self._insert_log("EMP0201", "2026-07-09")
        self._insert_log("EMP0201", "2026-07-11", late_minutes=12)
        res = self.client.get(
            "/analytics/departments/QA/trend",
            params={"from": "2026-07-06", "to": "2026-07-12"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        rows = res.json()["items"]
        self.assertEqual([row["date"] for row in rows], [
            "2026-07-06",
            "2026-07-07",
            "2026-07-08",
            "2026-07-09",
            "2026-07-10",
            "2026-07-11",
            "2026-07-12",
        ])
        self.assertEqual(rows[0]["headcount"], 1)
        self.assertEqual(rows[2]["headcount"], 2)
        self.assertEqual(rows[0]["present_count"], 1)
        self.assertEqual(rows[1]["present_count"], 0)
        self.assertEqual(rows[2]["present_count"], 0.5)
        self.assertEqual(rows[5]["present_count"], 1)
        self.assertFalse(rows[5]["is_working_day"])
        self.assertEqual(rows[5]["late_count"], 1)
        self.assertIsNone(rows[5]["attendance_rate"])
        approx(rows[0]["attendance_rate"], "1.0000")
        approx(rows[1]["attendance_rate"], "0.0000")
        approx(rows[2]["attendance_rate"], "0.2500")
        approx(rows[3]["attendance_rate"], "0.5000")
        approx(rows[4]["attendance_rate"], "0.0000")
        approx(rows[0]["moving_avg_7d"], "1.0000")
        approx(rows[1]["moving_avg_7d"], "0.5000")
        approx(rows[2]["moving_avg_7d"], "0.4167")
        approx(rows[4]["moving_avg_7d"], "0.3500")
        # Saturday is inside the window but its null rate is skipped, so Friday's window
        # and Saturday's window contain the same five rates.
        self.assertEqual(rows[4]["moving_avg_7d"], rows[5]["moving_avg_7d"])
        self.assertEqual(
            self.client.get(
                "/analytics/departments/Missing/trend",
                params={"from": "2026-07-06", "to": "2026-07-06"},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                "/analytics/departments/QA/trend",
                params={"from": "2026-07-10", "to": "2026-07-01"},
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.get(
                "/analytics/departments/QA/trend",
                params={"from": "2026-07-01", "to": "2026-10-01"},
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.get(
                "/analytics/departments/QA/trend",
                params={"from": "2026-07-01", "to": "2026-09-30"},
            ).status_code,
            200,
        )

    def test_average_is_over_records(self):
        self._insert_employee("EMP0301", "Ops", "2026-01-01")
        self._insert_employee("EMP0302", "Ops", "2026-01-01")
        self._insert_log("EMP0301", "2026-09-01", work_hours=10)
        self._insert_log("EMP0301", "2026-09-02", work_hours=10)
        self._insert_log("EMP0302", "2026-09-01", work_hours=4)
        res = self.client.get(
            "/analytics/departments/summary",
            params={"month": "2026-09", "department": "Ops"},
        )
        approx(res.json()["items"][0]["avg_work_hours"], "8.00")

    def test_future_joiner_has_null_percentage(self):
        self._insert_employee("EMP0401", "Ops", "2026-12-01")
        res = self.client.get("/analytics/employees/EMP0401/monthly", params={"month": "2026-07"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["working_days"], 0)
        self.assertIsNone(res.json()["attendance_pct"])

    def test_explain_uses_index_scans(self):
        self._insert_employee("EMP0501", "Engineering", "2026-01-01")
        cases = [
            ("/admin/explain/attendance_list", {}),
            ("/admin/explain/attendance_list", {"emp_code": "EMP0501", "status": "PRESENT"}),
            ("/admin/explain/attendance_list", {"status": "LEAVE", "date_from": "2026-07-01", "date_to": "2026-07-31"}),
            ("/admin/explain/employee_monthly", {"emp_code": "EMP0501", "month": "2026-07"}),
            ("/admin/explain/department_summary", {"month": "2026-07"}),
            ("/admin/explain/department_summary", {"month": "2026-07", "department": "Engineering"}),
            ("/admin/explain/late_leaderboard", {"month": "2026-07", "limit": 5}),
            (
                "/admin/explain/department_trend",
                {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"},
            ),
        ]
        for path, params in cases:
            res = self.client.get(path, params=params)
            self.assertEqual(res.status_code, 200, f"{path} {params} {res.text[:500]}")
            text = json.dumps(res.json()["explain"])
            self.assertIn("IXSCAN", text, path)
            self.assertNotIn("COLLSCAN", text, path)
        self.assertEqual(self.client.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code, 422)
        self.assertEqual(self.client.get("/admin/explain/not_a_query").status_code, 422)

    def _employee(self, code, department="Engineering", shift_start="09:30", shift_end="18:30"):
        return self.client.post(
            "/employees",
            json={
                "emp_code": code,
                "name": code,
                "email": f"{code.lower()}@example.com",
                "department": department,
                "shift_start": shift_start,
                "shift_end": shift_end,
                "joined_on": "2026-01-05",
            },
        )

    def _insert_employee(self, code, department, joined_on, name=None, shift_start="09:30", shift_end="18:30"):
        db.employees.insert_one(
            {
                "emp_code": code,
                "name": name or code,
                "email": f"{code.lower()}@example.com",
                "department": department,
                "shift_start": shift_start,
                "shift_end": shift_end,
                "joined_on": joined_on,
                "created_at": datetime.now(UTC),
            }
        )

    def _insert_log(
        self,
        code,
        day,
        status="PRESENT",
        late_minutes=0,
        overtime_minutes=0,
        work_hours=8,
        half_day=False,
    ):
        db.attendance_logs.insert_one(
            {
                "emp_code": code,
                "date": day,
                "status": status,
                "punch_in": datetime(2026, 1, 1, tzinfo=UTC),
                "punch_out": datetime(2026, 1, 1, 8, tzinfo=UTC),
                "work_hours": work_hours,
                "late_minutes": late_minutes,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
                "history": [],
            }
        )

    def _load_samples(self):
        opts = json_util.JSONOptions(tz_aware=True)
        for name in ("employees", "attendance_logs"):
            docs = json_util.loads((ROOT / "sample_data" / f"{name}.json").read_text(), json_options=opts)
            db[name].insert_many(docs)


class ConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = os.environ.copy()
        env["MONGO_URI"] = os.environ["MONGO_URI"]
        env["MONGO_DB"] = "attendance_test"
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8765", "--log-level", "warning"],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 20
        ready = False
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                break
            try:
                import urllib.request

                with urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=1) as res:
                    if res.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.2)
        if not ready:
            output = cls.proc.stdout.read().decode() if cls.proc.stdout else ""
            raise RuntimeError(f"server did not start\n{output}")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def setUp(self):
        db.employees.delete_many({})
        db.attendance_logs.delete_many({})

    def test_parallel_creates_punches_and_edits(self):
        import urllib.error
        import urllib.request

        def call(method, path, payload):
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:8765{path}",
                data=data,
                headers={"Content-Type": "application/json"},
                method=method,
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as res:
                    return res.status
            except urllib.error.HTTPError as exc:
                return exc.code

        employee = {
            "emp_code": "EMP7001",
            "name": "Race",
            "email": "race@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-05",
        }
        codes = []

        def create():
            codes.append(call("POST", "/employees", employee))

        threads = [threading.Thread(target=create) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(codes.count(201), 1, codes)
        self.assertEqual(codes.count(409), 7, codes)

        punch = {
            "emp_code": "EMP7001",
            "punched_at": epoch_ms(2026, 7, 6, 9, 30, ist=True),
        }
        punch_codes = []

        def punch_in():
            punch_codes.append(call("POST", "/attendance/punch-in", punch))

        threads = [threading.Thread(target=punch_in) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(punch_codes.count(201), 1, punch_codes)
        self.assertEqual(punch_codes.count(409), 7, punch_codes)
        self.assertEqual(db.attendance_logs.count_documents({"emp_code": "EMP7001"}), 1)

        out_codes = []
        out = {
            "emp_code": "EMP7001",
            "punched_at": epoch_ms(2026, 7, 6, 18, 30, ist=True),
        }

        def punch_out():
            out_codes.append(call("POST", "/attendance/punch-out", out))

        threads = [threading.Thread(target=punch_out) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(out_codes.count(200), 1, out_codes)
        self.assertEqual(out_codes.count(409), 7, out_codes)

        edit_codes = []
        edit = {
            "status": "WFH",
            "reason": "parallel regularization",
            "regularized_by": "hr.admin",
        }

        def edit_row():
            edit_codes.append(call("PATCH", "/attendance/EMP7001/2026-07-06", edit))

        threads = [threading.Thread(target=edit_row) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(edit_codes.count(200), 1, edit_codes)
        self.assertTrue(all(code in (200, 409, 422) for code in edit_codes), edit_codes)
        saved = db.attendance_logs.find_one({"emp_code": "EMP7001", "date": "2026-07-06"})
        self.assertEqual(saved["status"], "WFH")
        self.assertEqual(len(saved["history"]), 1)


if __name__ == "__main__":
    unittest.main()

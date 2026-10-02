"""
Smoke test for POST /hall-allocations/auto (automatic hall allocation).

Scenario: three departments (ND2) write at the same time, 80 students in
total, and three halls (50 / 30 / 5 seats). Checks packing + mixing, the
no-mixing option, overflow reporting, dry-run, re-runs, and that every
student really ends up with a unique hall + seat.
"""
import os
import sys

os.environ["DATABASE_URL"] = "sqlite:///./smoke_test_auto.db"
if os.path.exists("smoke_test_auto.db"):
    os.remove("smoke_test_auto.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def check(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"OK: {msg}")


client.post("/auth/register", json={"username": "admin", "email": "a@x.ng", "password": "SuperSecret1"})
tok = client.post("/auth/login", data={"username": "admin", "password": "SuperSecret1"}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}

dept = {}
for code in ("CSD", "STA", "MAC"):
    dept[code] = client.post("/departments", json={"name": code + " dept", "code": code}, headers=H).json()["id"]

sess = client.post("/sessions", json={"name": "2026/2027"}, headers=H).json()["id"]
sem = client.post("/semesters", json={"session_id": sess, "name": "First"}, headers=H).json()["id"]
client.post(f"/semesters/{sem}/start", headers=H)

hall = {}
for name, seats in (("Big", 50), ("Small", 30), ("Tiny", 5)):
    hall[name] = client.post("/halls", json={"name": name, "code": name[:3].upper(), "total_seats": seats}, headers=H).json()["id"]

counts = {"CSD": 40, "STA": 30, "MAC": 10}
exam = {}
for code, n in counts.items():
    off = client.post("/course-offerings", json={
        "course_code": f"{code}201", "course_title": code, "department_id": dept[code],
        "level": "ND2", "semester_id": sem}, headers=H).json()["id"]
    exam[code] = client.post("/exams", json={
        "course_offering_id": off, "exam_date": "2026-11-10",
        "start_time": "09:00:00", "end_time": "11:00:00"}, headers=H).json()["id"]

# A later, non-overlapping sitting for CSD-like reuse of the same halls.
off = client.post("/course-offerings", json={
    "course_code": "CSD202", "course_title": "CSD2", "department_id": dept["CSD"],
    "level": "ND2", "semester_id": sem}, headers=H).json()["id"]
exam_pm = client.post("/exams", json={
    "course_offering_id": off, "exam_date": "2026-11-10",
    "start_time": "13:00:00", "end_time": "15:00:00"}, headers=H).json()["id"]

tokens = {}
for code, n in counts.items():
    for i in range(1, n + 1):
        m = f"{code}/ND/26/{i:03d}"
        r = client.post("/students/register", json={
            "full_name": f"Student {m}", "matric_no": m, "department_id": dept[code],
            "level": "ND2", "semester_id": sem})
        tokens[m] = r.json()["access_token"]

# -- needs a scope ----------------------------------------------------------
r = client.post("/hall-allocations/auto", json={}, headers=H)
check(r.status_code == 400, "auto-allocate without semester/exam ids is rejected")
r = client.post("/hall-allocations/auto", json={"semester_id": sem})
check(r.status_code == 401, "auto-allocate requires an admin token")

# -- dry run saves nothing --------------------------------------------------
r = client.post("/hall-allocations/auto", json={"semester_id": sem, "dry_run": True}, headers=H)
check(r.status_code == 200 and r.json()["dry_run"], f"dry run works ({r.status_code})")
check(client.get("/hall-allocations", headers=H).json() == [], "dry run left no hall allocations behind")
seats = client.get("/students/me/seats", headers={"Authorization": f"Bearer {tokens['CSD/ND/26/001']}"}).json()
check(seats == [], "dry run assigned no student seats")

# -- real run, mixing on ----------------------------------------------------
r = client.post("/hall-allocations/auto", json={"semester_id": sem}, headers=H)
body = r.json()
check(r.status_code == 200, f"auto-allocate ({r.status_code}: {r.text[:200]})")
by_code = {e["course_code"]: e for e in body["exams"]}
check(by_code["CSD201"]["seated"] == 40 and by_code["STA201"]["seated"] == 30 and by_code["MAC201"]["seated"] == 10,
      "all 80 AM students seated")
check(body["warnings"] == [] or all("CSD202" in w for w in body["warnings"]), f"no capacity warnings for AM sitting ({body['warnings']})")
check([b["hall_name"] for b in by_code["CSD201"]["blocks"]] == ["Big"] and by_code["CSD201"]["blocks"][0]["seat_start_no"] == 1,
      "largest exam placed in the big hall from seat 1")
check(by_code["STA201"]["blocks"][0]["hall_name"] == "Small", "30 students fit the 30-seat hall exactly")
mac = by_code["MAC201"]["blocks"][0]
check(mac["hall_name"] == "Big" and (mac["seat_start_no"], mac["seat_end_no"]) == (41, 50),
      "10-student exam fills the leftover seats 41-50 of the big hall (mixed departments)")

# every student has exactly one seat; no seat is used twice in a hall at that time
taken = set()
for m, t in tokens.items():
    s = client.get("/students/me/seats", headers={"Authorization": f"Bearer {t}"}).json()
    am = [x for x in s if x["start_time"].startswith("09")]
    check(len(am) == 1, f"{m} has a seat") if m.endswith("/001") else None
    if len(am) != 1:
        check(False, f"{m} has exactly one AM seat")
    key = (am[0]["hall_name"], am[0]["seat_number"])
    if key in taken:
        check(False, f"seat {key} given twice")
    taken.add(key)
check(len(taken) == 80, "80 students -> 80 distinct (hall, seat) pairs")

# CSD202 (afternoon) is seeded too: CSD students sit it. Hall reuse in a later sitting works.
pm = {e["course_code"]: e for e in body["exams"]}["CSD202"]
check(pm["seated"] == 40 and pm["blocks"][0]["hall_name"] == "Big" and pm["blocks"][0]["seat_start_no"] == 1,
      "afternoon sitting reuses the big hall from seat 1")

# -- re-run is stable, replace_existing=false skips -------------------------
r2 = client.post("/hall-allocations/auto", json={"semester_id": sem}, headers=H).json()
check([(e["course_code"], [(b["hall_name"], b["seat_start_no"]) for b in e["blocks"]]) for e in r2["exams"]]
      == [(e["course_code"], [(b["hall_name"], b["seat_start_no"]) for b in e["blocks"]]) for e in body["exams"]],
      "re-running gives the same layout")
r3 = client.post("/hall-allocations/auto", json={"semester_id": sem, "replace_existing": False}, headers=H).json()
check(r3["exams"] == [] and len(r3["skipped"]) == 4, f"replace_existing=false skips allocated exams ({r3['skipped']})")

# -- no mixing: one exam per hall, overflow reported ------------------------
r4 = client.post("/hall-allocations/auto", json={"exam_ids": [exam["CSD"], exam["STA"], exam["MAC"]], "allow_mixing": False}, headers=H).json()
b4 = {e["course_code"]: e for e in r4["exams"]}
check(b4["CSD201"]["blocks"][0]["hall_name"] == "Big" and b4["STA201"]["blocks"][0]["hall_name"] == "Small",
      "no-mixing: CSD in Big, STA in Small")
check(b4["MAC201"]["seated"] == 5 and len(b4["MAC201"]["unseated"]) == 5, "no-mixing: MAC only gets the 5-seat hall; 5 reported unseated")
check(any("MAC201" in w for w in r4["warnings"]), "overflow warning produced")
sa = client.get("/students/me/seats", headers={"Authorization": f"Bearer {tokens['MAC/ND/26/010']}"}).json()
check(sa == [] or all(x["course_code"] != "MAC201" for x in sa), "unseated student has no seat for that exam")

# -- restricting halls -------------------------------------------------------
r5 = client.post("/hall-allocations/auto", json={"exam_ids": [exam["MAC"]], "hall_ids": [hall["Tiny"]]}, headers=H).json()
e5 = r5["exams"][0]
check([b["hall_name"] for b in e5["blocks"]] == ["Tiny"] and e5["seated"] == 5 and len(e5["unseated"]) == 5,
      "hall_ids restricts the halls used (only the 5-seat hall -> 5 seated, 5 reported)")
r6 = client.post("/hall-allocations/auto", json={"exam_ids": [exam["MAC"]]}, headers=H).json()
check(r6["exams"][0]["blocks"][0]["hall_name"] == "Big" and r6["exams"][0]["seated"] == 10,
      "without the restriction the same exam goes back to the leftover seats in Big")

# -- closed semester --------------------------------------------------------
client.post(f"/semesters/{sem}/submit", headers=H)
r = client.post("/hall-allocations/auto", json={"semester_id": sem}, headers=H)
check(r.status_code == 400, "closed semester is rejected")

print("\nALL AUTO-ALLOCATION TESTS PASSED")

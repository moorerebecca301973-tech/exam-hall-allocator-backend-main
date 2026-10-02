"""
Core seat-allocation logic.

For a given ExamSchedule (one course, one date/time), we find every
HallDepartmentRange that points at it (rng.exam_schedule_id == exam.id) --
each range is a block of seats, in some hall, reserved for a
department+level whose matric numbers fall in [matric_start, matric_end].
Several such ranges (in the same or different halls) can exist for one
exam, and a single hall's sitting (HallAllocation) can itself hold ranges
for several different exams/departments happening simultaneously in that
room.

For every Student whose department+level matches a range, and whose
matric_no falls lexicographically within [matric_start, matric_end], we
rank all matching, registered students by matric_no and assign them
consecutive seat numbers starting at seat_start_no. If the number of
matching students exceeds the seats reserved in that range, the overflow
students are left unallocated and reported back to the admin (so they can
widen the range or add another block) rather than silently overwriting
someone else's seat.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.models import (
    ExamSchedule,
    ExamStatus,
    Hall,
    HallAllocation,
    HallDepartmentRange,
    SeatAllocation,
    Student,
)


@dataclass
class AllocationResult:
    allocated: int = 0
    overflow_students: List[str] = field(default_factory=list)  # matric numbers
    errors: List[str] = field(default_factory=list)


def _matric_in_range(matric_no: str, start: str, end: str) -> bool:
    """Case-insensitive lexicographic containment check."""
    m = matric_no.strip().upper()
    return start.strip().upper() <= m <= end.strip().upper()


def generate_seat_allocations_for_exam(
    db: Session, exam: ExamSchedule
) -> AllocationResult:
    """
    (Re)computes SeatAllocation rows for every HallDepartmentRange that
    points at this exam (across any hall/sitting it has been placed in).
    Existing seat allocations for this exam are cleared and recomputed, so
    this is safe to call again after an admin edits the ranges or more
    students register.
    """
    result = AllocationResult()

    db.query(SeatAllocation).filter(
        SeatAllocation.exam_schedule_id == exam.id
    ).delete()

    ranges: List[HallDepartmentRange] = (
        db.query(HallDepartmentRange)
        .filter(HallDepartmentRange.exam_schedule_id == exam.id)
        .all()
    )

    for rng in ranges:
        capacity = rng.seat_end_no - rng.seat_start_no + 1

        candidates: List[Student] = (
            db.query(Student)
            .filter(
                Student.department_id == rng.department_id,
                Student.level == rng.level,
            )
            .all()
        )
        matching = sorted(
            (
                s
                for s in candidates
                if _matric_in_range(s.matric_no, rng.matric_start, rng.matric_end)
            ),
            key=lambda s: s.matric_no.upper(),
        )

        if len(matching) > capacity:
            result.overflow_students.extend(s.matric_no for s in matching[capacity:])
            result.errors.append(
                f"Range {rng.matric_start}-{rng.matric_end} (hall_allocation_id="
                f"{rng.hall_allocation_id}) has {len(matching)} student(s) but "
                f"only {capacity} seat(s) reserved."
            )
            matching = matching[:capacity]

        for idx, student in enumerate(matching):
            seat_no = rng.seat_start_no + idx
            db.add(
                SeatAllocation(
                    student_id=student.id,
                    exam_schedule_id=exam.id,
                    hall_id=rng.hall_allocation.hall_id,
                    hall_department_range_id=rng.id,
                    seat_number=seat_no,
                )
            )
            result.allocated += 1

    db.commit()
    return result


def validate_no_seat_overlap(
    existing_ranges: List[HallDepartmentRange], new_range: dict
) -> None:
    """Raises ValueError if new_range's seat block overlaps an existing one
    within the same hall allocation (same hall + same sitting)."""
    new_start, new_end = new_range["seat_start_no"], new_range["seat_end_no"]
    for r in existing_ranges:
        if new_start <= r.seat_end_no and r.seat_start_no <= new_end:
            raise ValueError(
                f"Seat range {new_start}-{new_end} overlaps existing range "
                f"{r.seat_start_no}-{r.seat_end_no} in this hall allocation."
            )


# ---------------------------------------------------------------------------
# Automatic hall allocation
# ---------------------------------------------------------------------------
@dataclass
class AutoBlock:
    hall_id: int
    hall_name: str
    seat_start_no: int
    seat_end_no: int
    matric_start: str
    matric_end: str
    students: int


@dataclass
class AutoExamSummary:
    exam_schedule_id: int
    course_code: str
    department_code: str
    level: str
    exam_date: str
    start_time: str
    end_time: str
    total_students: int = 0
    seated: int = 0
    unseated: List[str] = field(default_factory=list)  # matric numbers
    blocks: List[AutoBlock] = field(default_factory=list)


@dataclass
class AutoAllocationResult:
    dry_run: bool = False
    exams: List[AutoExamSummary] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class _HallState:
    hall: Hall
    order: int
    segments: List[List[int]]  # free [start, end] seat blocks, inclusive
    owners: Set[int]  # exam ids already seated in this hall for this sitting
    alloc: Optional[HallAllocation] = None


def _free_segments(total_seats: int, used: List[Tuple[int, int]]) -> List[List[int]]:
    segments: List[List[int]] = []
    cursor = 1
    for start, end in sorted(used):
        if start > cursor:
            segments.append([cursor, start - 1])
        cursor = max(cursor, end + 1)
    if cursor <= total_seats:
        segments.append([cursor, total_seats])
    return segments


def _seg_len(seg: List[int]) -> int:
    return seg[1] - seg[0] + 1


def _pick_segment(
    states: List[_HallState], exam_id: int, remaining: int, allow_mixing: bool
) -> Optional[Tuple[_HallState, List[int]]]:
    """Best-fit: if some free block can hold everyone still waiting, use the
    smallest such block (keeps big halls free and fills leftover gaps).
    Otherwise take the biggest block available and carry on in the next."""
    candidates = []
    for st in states:
        if not allow_mixing and (st.owners - {exam_id}):
            continue
        for seg in st.segments:
            candidates.append((st, seg))
    if not candidates:
        return None
    fits = [c for c in candidates if _seg_len(c[1]) >= remaining]
    if fits:
        return min(fits, key=lambda c: (_seg_len(c[1]), c[0].order))
    return max(candidates, key=lambda c: (_seg_len(c[1]), -c[0].order))


def auto_allocate_halls(
    db: Session,
    exams: List[ExamSchedule],
    hall_ids: Optional[List[int]] = None,
    allow_mixing: bool = True,
    replace_existing: bool = True,
    dry_run: bool = False,
) -> AutoAllocationResult:
    """
    Automatically picks halls, matric ranges and seat blocks for `exams`.

    Exams are grouped into sittings (same date + start + end). Within a
    sitting, the exams with the most students are placed first. Each exam's
    registered students (department + level, sorted by matric number) are
    poured into free seat blocks: a single block when everyone fits,
    otherwise the largest blocks first, splitting the matric order into
    consecutive ranges. When `allow_mixing` is true, left-over seats in a
    partly used hall go to other departments' exams sitting at the same
    time; when false, every hall holds one exam only.

    Halls booked for a different, overlapping sitting are never used, and
    seats already held by exams outside `exams` are left untouched.
    Ends by running the normal seat computation, so students see their hall
    and seat straight away. With `dry_run` nothing is saved.
    """
    result = AutoAllocationResult(dry_run=dry_run)

    eligible: List[ExamSchedule] = []
    for exam in exams:
        code = exam.course_offering.course.code
        if exam.status != ExamStatus.SCHEDULED:
            result.skipped.append(f"{code}: exam is {exam.status.value}")
        elif not replace_existing and exam.hall_department_ranges:
            result.skipped.append(f"{code}: already has a hall allocation")
        else:
            eligible.append(exam)
    if not eligible:
        return result

    # Capture display data now; the ORM objects expire after a rollback.
    summaries: Dict[int, AutoExamSummary] = {}
    for exam in eligible:
        off = exam.course_offering
        summaries[exam.id] = AutoExamSummary(
            exam_schedule_id=exam.id,
            course_code=off.course.code,
            department_code=off.department.code,
            level=off.level.value,
            exam_date=exam.exam_date.isoformat(),
            start_time=exam.start_time.strftime("%H:%M"),
            end_time=exam.end_time.strftime("%H:%M"),
        )

    if replace_existing:
        ids = [e.id for e in eligible]
        db.query(SeatAllocation).filter(
            SeatAllocation.exam_schedule_id.in_(ids)
        ).delete(synchronize_session=False)
        db.query(HallDepartmentRange).filter(
            HallDepartmentRange.exam_schedule_id.in_(ids)
        ).delete(synchronize_session=False)
        db.flush()
        db.expire_all()
        for empty in (
            db.query(HallAllocation)
            .filter(~HallAllocation.department_ranges.any())
            .all()
        ):
            db.delete(empty)
        db.flush()

    all_halls_q = db.query(Hall)
    if hall_ids:
        all_halls_q = all_halls_q.filter(Hall.id.in_(hall_ids))
    all_halls = sorted(all_halls_q.all(), key=lambda h: (-h.total_seats, h.name))
    if not all_halls:
        result.warnings.append("No halls available - create a hall first.")
        db.rollback() if dry_run else db.commit()
        return result

    # Group into sittings.
    sittings: Dict[tuple, List[ExamSchedule]] = {}
    for exam in eligible:
        sittings.setdefault(
            (exam.exam_date, exam.start_time, exam.end_time), []
        ).append(exam)

    for (exam_date, start_time, end_time), group in sorted(
        sittings.items(), key=lambda kv: (kv[0][0], kv[0][1])
    ):
        # Build per-hall state for this sitting.
        states: List[_HallState] = []
        for hall in all_halls:
            clash = (
                db.query(HallAllocation)
                .filter(
                    HallAllocation.hall_id == hall.id,
                    HallAllocation.exam_date == exam_date,
                    HallAllocation.start_time < end_time,
                    HallAllocation.end_time > start_time,
                    ~and_(
                        HallAllocation.start_time == start_time,
                        HallAllocation.end_time == end_time,
                    ),
                )
                .first()
            )
            if clash:
                continue
            alloc = (
                db.query(HallAllocation)
                .filter(
                    HallAllocation.hall_id == hall.id,
                    HallAllocation.exam_date == exam_date,
                    HallAllocation.start_time == start_time,
                    HallAllocation.end_time == end_time,
                )
                .first()
            )
            used = [(r.seat_start_no, r.seat_end_no) for r in alloc.department_ranges] if alloc else []
            owners = {r.exam_schedule_id for r in alloc.department_ranges} if alloc else set()
            states.append(
                _HallState(
                    hall=hall,
                    order=len(states),
                    segments=_free_segments(hall.total_seats, used),
                    owners=owners,
                    alloc=alloc,
                )
            )

        # Students per exam (same selection rule the seat computation uses).
        roster: Dict[int, List[Student]] = {}
        for exam in group:
            off = exam.course_offering
            roster[exam.id] = sorted(
                db.query(Student)
                .filter(
                    Student.department_id == off.department_id,
                    Student.level == off.level,
                )
                .all(),
                key=lambda s: s.matric_no.upper(),
            )

        for exam in sorted(group, key=lambda e: -len(roster[e.id])):
            students = roster[exam.id]
            summary = summaries[exam.id]
            summary.total_students = len(students)
            if not students:
                result.warnings.append(
                    f"{summary.course_code}: no registered students yet - "
                    "nothing to allocate. Run auto-allocation again after registration."
                )
                continue

            pos = 0
            while pos < len(students):
                picked = _pick_segment(states, exam.id, len(students) - pos, allow_mixing)
                if picked is None:
                    break
                st, seg = picked
                take = min(_seg_len(seg), len(students) - pos)
                chunk = students[pos : pos + take]
                seat_start, seat_end = seg[0], seg[0] + take - 1

                if st.alloc is None:
                    st.alloc = HallAllocation(
                        hall_id=st.hall.id,
                        exam_date=exam_date,
                        start_time=start_time,
                        end_time=end_time,
                    )
                    db.add(st.alloc)
                    db.flush()
                off = exam.course_offering
                db.add(
                    HallDepartmentRange(
                        hall_allocation_id=st.alloc.id,
                        exam_schedule_id=exam.id,
                        department_id=off.department_id,
                        level=off.level,
                        matric_start=chunk[0].matric_no,
                        matric_end=chunk[-1].matric_no,
                        seat_start_no=seat_start,
                        seat_end_no=seat_end,
                    )
                )
                summary.blocks.append(
                    AutoBlock(
                        hall_id=st.hall.id,
                        hall_name=st.hall.name,
                        seat_start_no=seat_start,
                        seat_end_no=seat_end,
                        matric_start=chunk[0].matric_no,
                        matric_end=chunk[-1].matric_no,
                        students=take,
                    )
                )
                seg[0] += take
                if seg[0] > seg[1]:
                    st.segments.remove(seg)
                st.owners.add(exam.id)
                pos += take

            summary.seated = pos
            if pos < len(students):
                summary.unseated = [s.matric_no for s in students[pos:]]
                result.warnings.append(
                    f"{summary.course_code}: {len(summary.unseated)} student(s) could not "
                    f"be seated - not enough free seats in the available halls for "
                    f"{summary.exam_date} {summary.start_time}-{summary.end_time}."
                )

    result.exams = [summaries[e.id] for e in eligible]

    if dry_run:
        db.rollback()
        return result

    db.commit()
    # Compute the real per-student seat rows (also confirms the counts).
    for exam in eligible:
        computed = generate_seat_allocations_for_exam(db, exam)
        summaries[exam.id].seated = computed.allocated
    return result

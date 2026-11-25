"""
ccm_core.py - College Complaint Manager (GreenFuture Hack, Project #23)

Standard library only. Holds everything except the web pages:
  * defensive validation (one validator per field)
  * business logic: log complaints, assign categories, track resolution status
  * JSON persistence that survives missing / corrupted files
  * keyword + category search, executive summary, warnings
  * Markdown / plain-text / CSV exports and bulk CSV import with an error log
  * an optional terminal menu:  python ccm_core.py
"""
from __future__ import annotations

import csv
import io
import json
import os
import random
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("CCM_DATA_DIR", "data"))
DB_FILE = DATA_DIR / "complaints.json"
ERROR_LOG = DATA_DIR / "import_errors.log"

CATEGORIES = ["Academics", "Hostel", "Canteen", "Transport", "Library",
              "Infrastructure", "Faculty Conduct", "Fees & Admin",
              "IT & Wi-Fi", "Safety"]
PRIORITIES = ["Low", "Medium", "High", "Critical"]
STATUSES = ["Open", "In Progress", "Resolved", "Closed"]
DEPARTMENTS = ["CSE", "ECE", "EEE", "Mechanical", "Civil", "IT", "MBA", "Admin Office"]
CLOSED_STATES = {"Resolved", "Closed"}

# Days allowed to resolve a complaint, by priority. Drives the due date.
SLA_DAYS = {"Low": 14, "Medium": 7, "High": 3, "Critical": 1}


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class Complaint:
    id: int
    title: str
    description: str
    category: str
    priority: str
    status: str
    submitted_by: str
    department: str
    email: str
    created: str          # ISO date
    due: str              # ISO date
    resolved: str = ""    # ISO date, empty until resolved/closed

    @property
    def is_overdue(self) -> bool:
        return self.status not in CLOSED_STATES and date.fromisoformat(self.due) < date.today()

    @property
    def days_late(self) -> int:
        return max(0, (date.today() - date.fromisoformat(self.due)).days)

    @property
    def days_open(self) -> int:
        end = date.fromisoformat(self.resolved) if self.resolved else date.today()
        return (end - date.fromisoformat(self.created)).days


# --------------------------------------------------------------------------
# Validation: each validator returns (clean_value, error_or_None)
# --------------------------------------------------------------------------
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{1,59}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _text(label: str, lo: int, hi: int):
    def check(v):
        v = " ".join((v or "").split())
        if not lo <= len(v) <= hi:
            return v, f"{label} must be {lo}-{hi} characters (currently {len(v)})."
        return v, None
    return check


def _choice(label: str, options: list[str]):
    def check(v):
        v = (v or "").strip()
        hit = next((o for o in options if o.lower() == v.lower()), None)
        return (hit, None) if hit else (v, f"Choose a valid {label}.")
    return check


def _name(v):
    v = " ".join((v or "").split())
    if NAME_RE.match(v):
        return v, None
    return v, "Enter a name of 2-60 letters (spaces, . ' - are fine)."


def _email(v):
    v = (v or "").strip()
    if v and (len(v) > 120 or not EMAIL_RE.match(v)):
        return v, "Enter a valid email address or leave this blank."
    return v, None


def _created(v):
    v = (v or "").strip()
    if not v:
        return date.today().isoformat(), None
    try:
        d = date.fromisoformat(v)
    except ValueError:
        return v, "Use the date format YYYY-MM-DD."
    if d > date.today() or d.year < 2000:
        return v, "Date cannot be in the future or before the year 2000."
    return d.isoformat(), None


FIELDS = {
    "title": _text("Title", 5, 100),
    "description": _text("Details", 10, 500),
    "category": _choice("category", CATEGORIES),
    "priority": _choice("priority", PRIORITIES),
    "department": _choice("department", DEPARTMENTS),
    "submitted_by": _name,
    "email": _email,
    "created": _created,
}


def validate(raw: dict) -> tuple[dict, dict]:
    """Return (clean_values, errors). Never raises on bad input."""
    clean, errors = {}, {}
    for key, check in FIELDS.items():
        value, err = check(raw.get(key, ""))
        if err:
            errors[key] = err
        else:
            clean[key] = value
    return clean, errors


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def last_months(k: int = 6) -> list[str]:
    y, m = date.today().year, date.today().month
    out = []
    for _ in range(k):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


SAMPLES = [
    ("Hostel", "Water heater broken in Block B", "Geyser on the third floor of Block B has not worked for a week and students are bathing in cold water."),
    ("Hostel", "Mess food quality has dropped", "Rice and curries are served undercooked and cold at dinner; several students reported stomach upset this week."),
    ("Canteen", "Overpricing at the main canteen", "Snack prices were raised without notice and no price list is displayed at the counter."),
    ("Canteen", "Drinking water unavailable near canteen", "The water purifier beside the canteen has been out of order for days."),
    ("Transport", "College bus arrives late every morning", "Route 4 bus reaches campus after 9:15 am, so students miss the first lecture almost daily."),
    ("Transport", "Bus stop shelter damaged", "The shelter at the main gate has collapsed and students wait in the sun and rain."),
    ("Library", "Reference books missing from shelves", "Several core textbooks listed for this semester are unavailable and the waiting list is over a month long."),
    ("Library", "Library closes too early before exams", "The reading hall closes at 6 pm even during exam week; extended hours are needed."),
    ("Infrastructure", "Leaking roof in Lab 3", "Rainwater drips onto workstations in Computer Lab 3, creating an electrical hazard."),
    ("Infrastructure", "Broken benches in Room 204", "Five benches in Room 204 are broken and students stand or sit on the floor during lectures."),
    ("IT & Wi-Fi", "Wi-Fi not working in academic block", "The network drops every few minutes, so online submissions and portal access keep failing."),
    ("IT & Wi-Fi", "Student portal login failing", "The portal shows a server error when logging in to download hall tickets and view internal marks."),
    ("Academics", "Timetable clash for elective subjects", "Two electives are scheduled in the same slot on Thursdays and affected students cannot attend both."),
    ("Faculty Conduct", "Lectures regularly ending early", "Classes for a core subject end 20 minutes early and the syllabus is far behind schedule."),
    ("Fees & Admin", "Scholarship amount not credited", "The scholarship for this semester has not reached my account even though verification was completed."),
    ("Fees & Admin", "Wrong fee receipt issued", "The fee receipt shows an incorrect amount and the accounts office has not corrected it after two visits."),
    ("Safety", "Poor lighting near the parking lot", "The path between the library and the parking lot is unlit after sunset, which makes walking there unsafe."),
    ("Safety", "Fire extinguishers expired in Block A", "Safety tags on fire extinguishers on the first floor of Block A show last year's expiry date."),
]
NAMES = ["Anika Reddy", "Rahul Verma", "Sneha Patel", "Karthik Rao", "Fatima Sheikh",
         "Arjun Nair", "Priya Sharma", "Imran Khan", "Divya Menon", "Sai Teja",
         "Meera Iyer", "Vikram Singh"]


# --------------------------------------------------------------------------
# Store: persistence + business logic
# --------------------------------------------------------------------------
class Store:
    def __init__(self, path: Path = DB_FILE):
        self.path = Path(path)
        self.items: list[Complaint] = []
        self.recovered = False      # True if a corrupted file was set aside
        self.load()

    # ---- persistence ------------------------------------------------------
    def load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("expected a JSON list")
            self.items = [Complaint(**r) for r in raw]
        except FileNotFoundError:
            self.items = []
            self.save()
        except (json.JSONDecodeError, ValueError, TypeError, KeyError):
            # Keep the broken file for inspection, then start clean.
            os.replace(self.path, self.path.with_suffix(".corrupt.json"))
            self.items, self.recovered = [], True
            self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(c) for c in self.items], indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: never leaves a half-written file

    # ---- core operations --------------------------------------------------
    def get(self, cid: int) -> Complaint | None:
        return next((c for c in self.items if c.id == cid), None)

    def add(self, clean: dict, status: str = "Open", resolved: str = "", save: bool = True) -> Complaint:
        created = date.fromisoformat(clean["created"])
        due = created + timedelta(days=SLA_DAYS[clean["priority"]])
        if status in CLOSED_STATES:
            resolved = resolved or date.today().isoformat()
        else:
            resolved = ""
        c = Complaint(
            id=max((i.id for i in self.items), default=0) + 1,
            title=clean["title"], description=clean["description"],
            category=clean["category"], priority=clean["priority"],
            status=status, submitted_by=clean["submitted_by"],
            department=clean["department"], email=clean.get("email", ""),
            created=created.isoformat(), due=due.isoformat(), resolved=resolved,
        )
        self.items.append(c)
        if save:
            self.save()
        return c

    def update_status(self, cid: int, status: str) -> bool:
        c = self.get(cid)
        if c is None or status not in STATUSES:
            return False
        c.status = status
        c.resolved = (c.resolved or date.today().isoformat()) if status in CLOSED_STATES else ""
        self.save()
        return True

    def delete(self, cid: int) -> bool:
        c = self.get(cid)
        if c is None:
            return False
        self.items.remove(c)
        self.save()
        return True

    # ---- query & search ---------------------------------------------------
    def search(self, q: str = "", category: str = "", status: str = "", priority: str = "") -> list[Complaint]:
        q = q.strip().lstrip("#").lower()
        out = []
        for c in self.items:
            if category and c.category != category:
                continue
            if status and c.status != status:
                continue
            if priority and c.priority != priority:
                continue
            haystack = " ".join([str(c.id), c.title, c.description, c.submitted_by,
                                 c.category, c.department]).lower()
            if q and q not in haystack:
                continue
            out.append(c)
        return sorted(out, key=lambda c: c.id, reverse=True)

    # ---- analytics --------------------------------------------------------
    def summary(self) -> dict:
        items = self.items
        total = len(items)
        done = [c for c in items if c.status in CLOSED_STATES]
        dated = [c for c in done if c.resolved]
        avg_days = round(sum(c.days_open for c in dated) / len(dated), 1) if dated else 0.0
        on_time = sum(1 for c in dated if c.resolved <= c.due)

        by_category = Counter(c.category for c in items).most_common()
        monthly_counts = Counter(c.created[:7] for c in items)
        monthly = [(m, monthly_counts.get(m, 0)) for m in last_months(6)]
        prev, last = monthly[-2][1], monthly[-1][1]
        if prev == 0 and last == 0:
            trend = "No complaints logged in the last two months."
        else:
            word = "up from" if last > prev else "down from" if last < prev else "level with"
            trend = f"{last} logged this month so far, {word} {prev} last month."

        return {
            "total": total,
            "active": total - len(done),
            "overdue": sum(1 for c in items if c.is_overdue),
            "resolved": len(done),
            "resolution_rate": round(100 * len(done) / total, 1) if total else 0.0,
            "avg_days": avg_days,
            "sla_rate": round(100 * on_time / len(dated), 1) if dated else 0.0,
            "by_status": [(s, sum(1 for c in items if c.status == s)) for s in STATUSES],
            "by_category": by_category,
            "by_priority": [(p, sum(1 for c in items if c.priority == p)) for p in PRIORITIES],
            "monthly": monthly,
            "top_category": by_category[0][0] if by_category else "-",
            "trend": trend,
        }

    def warnings(self) -> list[tuple[str, str]]:
        """Threshold flags: overdue, critical-and-unassigned, possible duplicates."""
        overdue, critical, dupes = [], [], []
        for c in self.items:
            if c.status in CLOSED_STATES:
                continue
            if c.is_overdue:
                overdue.append(("overdue", f"#{c.id} {c.title} is {c.days_late} day(s) past due ({c.priority})."))
            elif c.priority == "Critical" and c.status == "Open":
                critical.append(("critical", f"#{c.id} {c.title} is critical and still unassigned."))
        seen = defaultdict(list)
        for c in self.items:
            seen[(c.title.lower(), c.submitted_by.lower())].append(c.id)
        for ids in seen.values():
            if len(ids) > 1:
                dupes.append(("duplicate", "Possible duplicate entries: " + ", ".join(f"#{i}" for i in ids) + "."))
        return critical + overdue + dupes

    # ---- exports ----------------------------------------------------------
    def to_markdown(self) -> str:
        s = self.summary()
        L = ["# College Complaint Manager: summary report",
             f"Generated {date.today().isoformat()}", "",
             "## Headline numbers", "", "| Metric | Value |", "|---|---|",
             f"| Total complaints | {s['total']} |",
             f"| Still active | {s['active']} |",
             f"| Overdue | {s['overdue']} |",
             f"| Resolution rate | {s['resolution_rate']}% |",
             f"| Average days to resolve | {s['avg_days']} |",
             f"| Resolved within due date | {s['sla_rate']}% |",
             f"| Busiest category | {s['top_category']} |",
             f"| Trend | {s['trend']} |"]
        for title, rows in (("By status", s["by_status"]), ("By category", s["by_category"]),
                            ("By priority", s["by_priority"]), ("Complaints per month", s["monthly"])):
            L += ["", f"## {title}", "", "| Item | Count |", "|---|---|"]
            L += [f"| {a} | {b} |" for a, b in rows]
        L += ["", "## Warnings", ""] + ([f"- {t}" for _, t in self.warnings()] or ["- None"])
        return "\n".join(L) + "\n"

    def to_text(self) -> str:
        L = [f"COMPLAINT AUDIT LOG   generated {datetime.now():%Y-%m-%d %H:%M}", "=" * 72]
        for c in sorted(self.items, key=lambda c: c.id):
            L.append(f"#{c.id:<4} {c.created}  {c.status:<12} {c.priority:<8} {c.category:<15} {c.title}")
            note = f"      by {c.submitted_by} ({c.department}); due {c.due}"
            if c.resolved:
                note += f"; resolved {c.resolved}"
            if c.is_overdue:
                note += "; OVERDUE"
            L.append(note)
        return "\n".join(L) + "\n"

    def to_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=[f.name for f in fields(Complaint)])
        writer.writeheader()
        for c in sorted(self.items, key=lambda c: c.id):
            writer.writerow(asdict(c))
        return buf.getvalue()

    # ---- bulk import ------------------------------------------------------
    def import_csv(self, text: str) -> tuple[int, list[str]]:
        """Import legacy CSV rows. Returns (rows_added, row_error_messages)."""
        reader = csv.DictReader(io.StringIO(text))
        headers = {(h or "").strip().lower() for h in (reader.fieldnames or [])}
        needed = {"title", "description", "category", "priority", "submitted_by", "department"}
        if needed - headers:
            return 0, ["Header: missing column(s) " + ", ".join(sorted(needed - headers)) + "."]

        added, errors = 0, []
        for n, row in enumerate(reader, start=2):  # row 1 is the header
            row = {(k or "").strip().lower(): (v if isinstance(v, str) else "") for k, v in row.items()}
            clean, errs = validate(row)
            wanted = (row.get("status") or "Open").strip()
            status = next((s for s in STATUSES if s.lower() == wanted.lower()), None)
            if status is None:
                errs["status"] = "Status must be one of " + ", ".join(STATUSES) + "."
            if errs:
                errors.append(f"Row {n}: " + "; ".join(f"{k}: {v}" for k, v in errs.items()))
                continue
            self.add(clean, status=status, save=False)
            added += 1
        if added:
            self.save()
        if errors:
            with ERROR_LOG.open("a", encoding="utf-8") as fh:
                fh.write(f"--- import {datetime.now():%Y-%m-%d %H:%M:%S}: {added} added, {len(errors)} rejected\n")
                fh.write("\n".join(errors) + "\n")
        return added, errors

    # ---- sample data ------------------------------------------------------
    def seed(self, n: int = 40) -> int:
        """Add n realistic complaints plus two boundary-value edge cases."""
        rng, today = random.Random(23), date.today()
        for i in range(n):
            cat, title, desc = SAMPLES[i % len(SAMPLES)]
            if i >= len(SAMPLES):
                title += f" ({rng.choice(['follow-up', 'repeat report', 'still pending'])})"
            created = today - timedelta(days=rng.randint(1, 150))
            clean = {"title": title, "description": desc, "category": cat,
                     "priority": rng.choices(PRIORITIES, weights=[3, 5, 3, 1])[0],
                     "department": rng.choice(DEPARTMENTS), "submitted_by": rng.choice(NAMES),
                     "email": "", "created": created.isoformat()}
            status = rng.choice(STATUSES)
            resolved = ""
            if status in CLOSED_STATES:
                span = rng.randint(0, SLA_DAYS[clean["priority"]] + 8)
                resolved = min(created + timedelta(days=span), today).isoformat()
            self.add(clean, status=status, resolved=resolved, save=False)

        edge = [  # minimum-length title, maximum-length description
            {"title": "No AC", "description": "Air conditioning is off in the seminar hall.",
             "category": "Infrastructure", "priority": "High", "department": "Admin Office",
             "submitted_by": "Meera Iyer", "email": "", "created": today.isoformat()},
            {"title": "Projector keeps failing in seminar hall",
             "description": ("The projector in the seminar hall shuts down mid-presentation. " * 9)[:500].strip(),
             "category": "Infrastructure", "priority": "Medium", "department": "CSE",
             "submitted_by": "Rahul Verma", "email": "", "created": (today - timedelta(days=2)).isoformat()},
        ]
        for e in edge:
            self.add(e, save=False)
        self.save()
        return n + len(edge)


# --------------------------------------------------------------------------
# Optional terminal interface
# --------------------------------------------------------------------------
YELLOW, RED, DIM, BOLD, RESET = "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"

MENU = f"""
{BOLD}  College Complaint Manager{RESET}
  1  Log a complaint          5  Executive summary
  2  List / search            6  Export report
  3  Update status            7  Bulk import CSV
  4  Delete a complaint       8  Load sample data
  0  Exit
"""


def _ask(key: str, prompt: str) -> str:
    while True:
        value, err = FIELDS[key](input(prompt))
        if not err:
            return value
        print(f"  {RED}! {err}{RESET}")


def _pick(label: str, options: list[str]) -> str:
    for i, o in enumerate(options, 1):
        print(f"   {i}. {o}")
    while True:
        raw = input(f"{label} (number or name): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        value, err = _choice(label.lower(), options)(raw)
        if not err:
            return value
        print(f"  {RED}! {err}{RESET}")


def _table(rows: list[Complaint]) -> None:
    if not rows:
        print("  No complaints match.")
        return
    print(f"{'ID':>4}  {'Title':<38} {'Category':<15} {'Priority':<9} {'Status':<12} Due")
    for i, c in enumerate(rows, 1):
        flag = f" {YELLOW}OVERDUE{RESET}" if c.is_overdue else ""
        print(f"{c.id:>4}  {c.title[:37]:<38} {c.category[:14]:<15} {c.priority:<9} {c.status:<12} {c.due}{flag}")
        if i % 10 == 0 and i < len(rows):
            if input(f"  {DIM}-- Enter for more, q to stop: {RESET}").strip().lower() == "q":
                break


def _log(store: Store) -> None:
    raw = {"title": _ask("title", "Title: "), "description": _ask("description", "Details: ")}
    raw["category"] = _pick("Category", CATEGORIES)
    raw["priority"] = _pick("Priority", PRIORITIES)
    raw["submitted_by"] = _ask("submitted_by", "Your name: ")
    raw["department"] = _pick("Department", DEPARTMENTS)
    raw["email"] = _ask("email", "Email (optional): ")
    clean, errors = validate(raw)
    if errors:  # should not happen; fields were validated one by one
        print(errors)
        return
    c = store.add(clean)
    print(f"  Logged complaint #{c.id}. Due {c.due}.")


def _list(store: Store) -> None:
    q = input("Keyword (blank for all): ")
    cat = input("Category filter (blank for all): ").strip()
    cat = next((o for o in CATEGORIES if o.lower() == cat.lower()), "")
    _table(store.search(q, category=cat))


def _update(store: Store) -> None:
    raw = input("Complaint ID: ").strip()
    if not raw.isdigit() or store.get(int(raw)) is None:
        print(f"  {RED}! No complaint with that ID.{RESET}")
        return
    store.update_status(int(raw), _pick("Status", STATUSES))
    print("  Status updated.")


def _delete(store: Store) -> None:
    raw = input("Complaint ID to delete: ").strip()
    if raw.isdigit() and input("  Type YES to confirm: ") == "YES" and store.delete(int(raw)):
        print("  Deleted.")
    else:
        print("  Nothing deleted.")


def _summary(store: Store) -> None:
    s = store.summary()
    print(f"\n  Total {s['total']} | Active {s['active']} | Overdue {s['overdue']} | "
          f"Resolved {s['resolution_rate']}% | Avg {s['avg_days']} days | On time {s['sla_rate']}%")
    for title, rows in (("Status", s["by_status"]), ("Category", s["by_category"]),
                        ("Priority", s["by_priority"]), ("Monthly", s["monthly"])):
        print(f"\n  {BOLD}{title}{RESET}")
        top = max([n for _, n in rows] + [1])
        for label, n in rows:
            print(f"   {label:<16} {'#' * round(n / top * 30):<30} {n}")
    print(f"\n  {s['trend']}")


def _export(store: Store) -> None:
    out = DATA_DIR / "exports"
    out.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    for ext, text in (("md", store.to_markdown()), ("txt", store.to_text()), ("csv", store.to_csv())):
        (out / f"report_{stamp}.{ext}").write_text(text, encoding="utf-8")
    print(f"  Wrote Markdown, text and CSV reports to {out}/")


def _import(store: Store) -> None:
    path = Path(input("CSV path: ").strip().strip('"'))
    try:
        added, errors = store.import_csv(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        print(f"  {RED}! Could not read file: {exc}{RESET}")
        return
    print(f"  Imported {added} row(s); {len(errors)} rejected.")
    for e in errors[:10]:
        print("   ", e)
    if errors:
        print(f"  Full log: {ERROR_LOG}")


def _seed(store: Store) -> None:
    print(f"  Added {store.seed()} sample complaints.")


def cli() -> None:
    store = Store()
    if store.recovered:
        print(f"{YELLOW}Saved data was unreadable and has been set aside as .corrupt.json.{RESET}")
    for _, text in store.warnings()[:5]:
        print(f"{YELLOW}! {text}{RESET}")
    actions = {"1": _log, "2": _list, "3": _update, "4": _delete,
               "5": _summary, "6": _export, "7": _import, "8": _seed}
    while True:
        print(MENU)
        choice = input("Select: ").strip()
        if choice == "0":
            print("Goodbye.")
            return
        action = actions.get(choice)
        if not action:
            print("  Pick a number from the menu.")
            continue
        try:
            action(store)
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")


if __name__ == "__main__":
    cli()
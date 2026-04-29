import re
import csv
import logging
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_creds(path: str):
    """
    Finds pairs like:
      Username: Foo
      Password: Bar
    (order can be Username then Password, possibly on adjacent lines)
    """
    text = Path(path).read_text(encoding="utf-8")
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    pat = re.compile(
        r"Username:\s*(\S+)[^\S\r\n]*\n?[\s\S]*?Password:\s*(\S+)",
        re.IGNORECASE
    )
    pairs = pat.findall(text)
    return [(u.strip(), p.strip()) for (u, p) in pairs]


def guess_name_from_row(row, delim):
    parts = []
    for k, v in row.items():
        if k and v and k.lower() in ("student", "name", "first", "last"):
            parts.append(v.strip())
    return " ".join(parts)


def extract_students(path: str):
    raw = Path(path).read_text(encoding="utf-8")

    header_line = raw.splitlines()[0] if raw.splitlines() else ""
    header_l = header_line.lower()
    has_student_col = "student" in header_l
    has_email_col = "email" in header_l

    records = []

    if has_student_col and has_email_col:
        delim = "\t" if "\t" in header_line else ","
        reader = csv.DictReader(raw.splitlines(), delimiter=delim)
        for row in reader:
            email = (row.get("Email") or row.get("email") or "").strip()
            name = (row.get("Student") or row.get("student") or "").strip()
            if email:
                if not name:
                    name = guess_name_from_row(row, delim)
                records.append({"name": name or "(no name)", "email": email})
    else:
        for line in raw.splitlines():
            m = re.search(
                r"([A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+)", line)
            if m:
                email = m.group(1)
                before = line[:m.start()].strip().strip("|,\t;")
                if "\t" in before:
                    name = before.split("\t")[1].strip() if len(before.split("\t")) > 1 else before
                elif "," in before:
                    name = before.split(",")[-1].strip()
                else:
                    name = before.strip()
                records.append({"name": name or "(no name)", "email": email})

    return records


def load_mapping_csv(path: str):
    file_path = Path(path)
    if not file_path.exists():
        return []

    rows = []
    with file_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row:
                continue
            normalized = {
                (k or "").strip().lower(): (v or "").strip()
                for k, v in row.items()
                if k is not None
            }
            if normalized:
                rows.append(normalized)
    return rows


def build_credential_set(rows):
    return {
        (row["username"], row["password"])
        for row in rows
        if row.get("username") and row.get("password")
    }


def merge_sent_rows(existing_rows, new_rows):
    merged = {}
    for row in existing_rows + new_rows:
        name = (row.get("name") or "").strip()
        email = (row.get("email") or "").strip()
        username = (row.get("username") or "").strip()
        password = (row.get("password") or "").strip()
        if not (name or email or username or password):
            continue
        key = (name.lower(), email.lower(), username, password)
        merged[key] = {
            "name": name,
            "email": email,
            "username": username,
            "password": password,
        }
    return list(merged.values())


def prepare_distribution(students, creds, sent_rows, unused_rows, creds_path="creds.txt"):
    sent_creds = build_credential_set(sent_rows)
    current_creds = set(creds)

    sent_in_current = sent_creds & current_creds
    sent_not_current = sent_creds - current_creds

    if sent_rows:
        logger.info(
            f"Loaded {len(sent_rows)} previously sent mappings from sent_mapping.csv.")
        if sent_in_current:
            logger.info(
                f"{len(sent_in_current)} previously sent credentials still appear in {creds_path}.")
        if sent_not_current:
            logger.warning(
                f"{len(sent_not_current)} previously sent credentials are not present in {creds_path}. "
                "Verify that sent_mapping.csv matches this class and creds.txt.")
        if not sent_in_current:
            logger.warning(
                "No previously sent credentials overlap with the current creds file. "
                "This may indicate sent_mapping.csv is from a prior class or creds.txt has changed.")

    already_sent_pairs = {
        (
            (row.get("name") or "").strip().lower(),
            (row.get("email") or "").strip().lower()
        )
        for row in sent_rows
    }
    already_sent_emails = {
        (row.get("email") or "").strip().lower()
        for row in sent_rows
        if row.get("email")
    }
    already_sent_names = {
        (row.get("name") or "").strip().lower()
        for row in sent_rows
        if row.get("name")
    }

    email_counts = Counter(
        (s["email"] or "").strip().lower()
        for s in students
        if s.get("email")
    )
    unique_emails = all(count == 1 for count in email_counts.values())
    roster_names = [
        (s["name"] or "").strip().lower()
        for s in students
        if s.get("name")
    ]
    unique_names = len(roster_names) == len(set(roster_names))

    if already_sent_pairs or already_sent_emails or already_sent_names:
        remaining_students = []
        removed_count = 0
        for s in students:
            email = (s["email"] or "").strip().lower()
            name = (s["name"] or "").strip().lower()
            if (name, email) in already_sent_pairs:
                removed_count += 1
                continue
            if unique_emails and email in already_sent_emails:
                removed_count += 1
                continue
            if unique_names and name in already_sent_names:
                removed_count += 1
                continue
            remaining_students.append(s)
        if removed_count:
            logger.info(
                f"Removed {removed_count} already-sent student(s) from distribution options.")
        students = remaining_students

    unused_creds = [
        (row["username"], row["password"])
        for row in unused_rows
        if row.get("username") and row.get("password")
    ]
    valid_unused = [c for c in unused_creds if c in current_creds]
    stale_unused = [c for c in unused_creds if c not in current_creds]

    if valid_unused:
        logger.info(f"Loaded {len(valid_unused)} reusable credentials from unused_mapping.csv.")
    if stale_unused:
        logger.warning(
            f"{len(stale_unused)} credentials in unused_mapping.csv do not match {creds_path} and will be ignored.")

    used_creds = sent_creds & current_creds
    available_creds = valid_unused + [c for c in creds if c not in used_creds and c not in valid_unused]

    if not students:
        raise SystemExit(
            "All roster students have already been sent credentials according to sent_mapping.csv.")
    if not available_creds:
        raise SystemExit(
            "No available credentials remain. Check creds.txt and unused_mapping.csv.")

    logger.info(
        f"Prepared distribution with {len(students)} students and {len(available_creds)} available credentials.")
    return students, available_creds

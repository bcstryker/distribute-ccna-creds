#!/usr/bin/env python3
import os
import re
import csv
import smtplib
import argparse
import logging
from collections import Counter
from pathlib import Path
from email.message import EmailMessage
from dotenv import load_dotenv

# --- Setup logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('distribute_creds.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")  # required
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")  # required
FROM = SMTP_USERNAME

SUBJECT = os.getenv("MAIL_SUBJECT", "Your Cisco Lab Credentials")
BODY_TEMPLATE = os.getenv("MAIL_BODY", """Hello {name},

Here are your credentials for the Cisco Lab Portal:

Username: {username}
Password: {password}

Access URL: https://htdlab.cisco.com

Please click "Exit" when finishing lab exercises to stop the timer.

- Brandon Stryker
""")

# Default file paths (can be overridden by CLI args or env vars)
DEFAULT_CREDS_FILE = "./setup/creds.txt"
DEFAULT_ROSTER_FILE = "./setup/roster.txt"
CREDS_FILE = os.getenv("CREDS_FILE", DEFAULT_CREDS_FILE)
ROSTER_FILE = os.getenv("ROSTER_FILE", DEFAULT_ROSTER_FILE)

# ---------------- Parsing ----------------


def extract_creds(path: str):
    """
    Finds pairs like:
      Username: Foo
      Password: Bar
    (order can be Username then Password, possibly on adjacent lines)
    """
    text = Path(path).read_text(encoding="utf-8")
    # collapse multiple spaces/newlines between fields
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    # robust: allow optional newline between key/value lines
    pat = re.compile(
        r"Username:\s*(\S+)[^\S\r\n]*\n?[\s\S]*?Password:\s*(\S+)",
        re.IGNORECASE
    )
    pairs = pat.findall(text)
    return [(u.strip(), p.strip()) for (u, p) in pairs]


def extract_students(path: str):
    """
    Returns list of dicts: [{"name": "First Last", "email": "x@y"}...]
    Works for TSV/CSV-ish roster or freeform lines; prefers 'Student' column if present.
    """
    raw = Path(path).read_text(encoding="utf-8")

    # Try TSV/CSV header detection first
    header_line = raw.splitlines()[0] if raw.splitlines() else ""
    header_l = header_line.lower()
    has_student_col = "student" in header_l
    has_email_col = "email" in header_l

    records = []

    if has_student_col and has_email_col:
        # Decide delimiter
        delim = "\t" if "\t" in header_line else ","
        reader = csv.DictReader(raw.splitlines(), delimiter=delim)
        for row in reader:
            email = (row.get("Email") or row.get("email") or "").strip()
            name = (row.get("Student") or row.get("student") or "").strip()
            if email:
                # fallback: if name empty, try to grab the token before email in the row string
                if not name:
                    name = guess_name_from_row(row, delim)
                records.append({"name": name or "(no name)", "email": email})
    else:
        # Freeform: find lines with an email; name = preceding field tokens before email if available
        for line in raw.splitlines():
            m = re.search(
                r"([A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+)", line)
            if m:
                email = m.group(1)
                # naive name guess: take text before email, strip separators
                before = line[:m.start()].strip().strip("|,\t;")
                # if before has tabs/commas, take the last chunk (likely "First Last")
                if "\t" in before:
                    name = before.split("\t")[1].strip() if len(
                        before.split("\t")) > 1 else before
                elif "," in before:
                    # "1, John Doe, email" -> take the piece right before email
                    name = before.split(",")[-1].strip()
                else:
                    name = before.strip()
                records.append({"name": name or "(no name)", "email": email})

    return records


def guess_name_from_row(row, delim):
    # fall back: concatenate likely name-ish fields
    parts = []
    for k, v in row.items():
        if k and v and k.lower() in ("student", "name", "first", "last"):
            parts.append(v.strip())
    return " ".join(parts)


def load_mapping_csv(path: str):
    """Read a mapping CSV file into normalized rows."""
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


from collections import Counter


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


def prepare_distribution(students, creds, sent_rows, unused_rows):
    sent_creds = build_credential_set(sent_rows)
    current_creds = set(creds)

    # Determine whether the previous sent mapping overlaps with this creds file.
    sent_in_current = sent_creds & current_creds
    sent_not_current = sent_creds - current_creds

    if sent_rows:
        logger.info(
            f"Loaded {len(sent_rows)} previously sent mappings from sent_mapping.csv.")
        if sent_in_current:
            logger.info(
                f"{len(sent_in_current)} previously sent credentials still appear in {CREDS_FILE}.")
        if sent_not_current:
            logger.warning(
                f"{len(sent_not_current)} previously sent credentials are not present in {CREDS_FILE}. "
                "Verify that sent_mapping.csv matches this class and creds.txt.")
        if not sent_in_current:
            logger.warning(
                "No previously sent credentials overlap with the current creds file. "
                "This may indicate sent_mapping.csv is from a prior class or creds.txt has changed.")

    # Remove roster students who have already been sent credentials.
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

    # Reuse available unused credentials first, but only if they still exist in creds.txt.
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
            f"{len(stale_unused)} credentials in unused_mapping.csv do not match creds.txt and will be ignored.")

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

# ---------------- Email sending ----------------


def send_one(to_addr: str, subject: str, body: str):
    """Send an email via SMTP."""
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        raise ValueError(
            "SMTP credentials not configured. Set SMTP_USERNAME and SMTP_PASSWORD.")

    msg = EmailMessage()
    msg["From"] = FROM
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as smtp:
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)
        logger.info(f"Email sent to {to_addr}")
    except Exception as e:
        logger.error(f"Failed to send email to {to_addr}: {e}")
        raise

# ---------------- GUI ----------------


def run_gui_and_send(students, creds, existing_sent_rows):
    """
    students: list of {"name","email"}
    creds:    list of (username,password)
    existing_sent_rows: previous sent mapping history for merge/preservation.
    Shows a tabbed GUI with distribution and resend tabs.
    """
    import tkinter as tk
    from tkinter import ttk, messagebox

    root = tk.Tk()
    root.title("Cisco Lab Credentials — Distribution and Resend")

    bg_color = "#1e2128"
    fg_color = "#e8eaed"
    selected_tab = "#2e323d"
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure("TFrame", background=bg_color)
    style.configure("Main.TFrame", background=bg_color)
    style.configure("Main.TLabel", background=bg_color, foreground=fg_color)
    style.configure("Main.TCheckbutton", background=bg_color, foreground=fg_color)
    style.configure("TCheckbutton", background=bg_color, foreground=fg_color)
    style.map("TCheckbutton",
              background=[("selected", bg_color), ("active", bg_color), ("!selected", bg_color)])
    style.configure("Main.TEntry", fieldbackground="#2b2f38", foreground=fg_color)
    style.configure("TButton", background="#2b2f38", foreground=fg_color)
    style.configure("TScrollbar", background=bg_color, troughcolor="#2b2f38", arrowcolor=fg_color)
    style.configure("TNotebook", background=bg_color, bordercolor=bg_color)
    style.configure("TNotebook.Tab", padding=(12, 8), background=selected_tab, foreground=fg_color)
    style.map("TNotebook.Tab",
              padding=[("selected", (18, 10)), ("!selected", (12, 8))],
              background=[("selected", selected_tab), ("active", selected_tab), ("!selected", bg_color)],
              foreground=[("selected", fg_color), ("!selected", fg_color)])

    root.configure(background=bg_color)

    notebook = ttk.Notebook(root)
    tab_distribute = ttk.Frame(notebook, style="Main.TFrame")
    tab_resend = ttk.Frame(notebook, style="Main.TFrame")
    notebook.add(tab_distribute, text="Distribute")
    notebook.add(tab_resend, text="Already Distributed")
    notebook.pack(fill="both", expand=True, padx=12, pady=12)

    tab_distribute_inner = ttk.Frame(tab_distribute, style="Main.TFrame", padding=12)
    tab_distribute_inner.pack(fill="both", expand=True)
    tab_resend_inner = ttk.Frame(tab_resend, style="Main.TFrame", padding=12)
    tab_resend_inner.pack(fill="both", expand=True)

    info = ttk.Label(
        root, text=f"Students: {len(students)}   Credentials: {len(creds)}", style="Main.TLabel")
    info.pack(padx=12, pady=(0, 6), anchor="w")

    status = tk.StringVar(value="Ready.")
    status_lbl = ttk.Label(root, textvariable=status, style="Main.TLabel")
    status_lbl.pack(padx=12, pady=(0, 12), anchor="w")

    # --- Distribution tab widgets ---
    vars_ = []

    canvas = tk.Canvas(tab_distribute_inner, borderwidth=0, height=360, background=bg_color, highlightthickness=0)
    frame = ttk.Frame(canvas, style="Main.TFrame")
    vsb = ttk.Scrollbar(tab_distribute_inner, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True, padx=(0, 8))
    canvas.create_window((0, 0), window=frame, anchor="nw")

    def on_frame_config(event):
        canvas.configure(scrollregion=canvas.bbox("all"))
    frame.bind("<Configure>", on_frame_config)

    def enforce_checklist_width(canvas_widget, content_frame):
        content_frame.update_idletasks()
        max_width = max((child.winfo_reqwidth() for child in content_frame.winfo_children()), default=0)
        if max_width:
            required_width = max_width + 32
            canvas_widget.configure(width=required_width)
            content_frame.configure(width=required_width)
        root.update_idletasks()
        root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())

    def refresh_attendance_list():
        nonlocal vars_
        for child in frame.winfo_children():
            child.destroy()
        vars_ = []
        for idx, s in enumerate(students, start=1):
            var = tk.BooleanVar(value=True)
            cb = ttk.Checkbutton(
                frame,
                text=f"{idx}. {s['name']}  <{s['email']}>",
                variable=var,
                style="Main.TCheckbutton"
            )
            cb.pack(anchor="w", padx=6, pady=3, fill="x")
            vars_.append(var)
        info.config(text=f"Students: {len(students)}   Credentials: {len(creds)}")
        enforce_checklist_width(canvas, frame)

    def remove_students(sent_students):
        nonlocal students, creds
        sent_counter = Counter((s["name"], s["email"]) for s in sent_students)
        remaining_students = []
        for s in students:
            key = (s["name"], s["email"])
            if sent_counter[key] > 0:
                sent_counter[key] -= 1
                continue
            remaining_students.append(s)
        students = remaining_students
        creds = creds[len(sent_students):]
        refresh_attendance_list()

    def do_send():
        chosen = [s for s, v in zip(students, vars_) if v.get()]
        absent = [s for s, v in zip(students, vars_) if not v.get()]
        if not chosen:
            messagebox.showwarning("Nothing selected", "No attendees checked.")
            return

        if not SMTP_USERNAME or not SMTP_PASSWORD:
            messagebox.showerror("Missing SMTP credentials",
                                 "Set SMTP_USERNAME and SMTP_PASSWORD in your .env.")
            return

        if len(chosen) > len(creds):
            messagebox.showwarning(
                "More attendees than credentials",
                f"{len(chosen)} attendees selected but only {len(creds)} credentials.\n"
                f"Only the first {len(creds)} attendees will receive credentials."
            )

        to_process = min(len(chosen), len(creds))
        send_list = chosen[:to_process]
        sent_rows = []

        try:
            for i, student in enumerate(send_list):
                username, password = creds[i]
                body = BODY_TEMPLATE.format(
                    name=student["name"].split()[0], username=username, password=password)
                status.set(f"Sending to {student['email']} …")
                root.update_idletasks()
                send_one(student["email"], SUBJECT, body)
                sent_rows.append({
                    "name": student["name"],
                    "email": student["email"],
                    "username": username,
                    "password": password
                })
                logger.info(f"Sent credentials to {student['email']}")
        except Exception as e:
            logger.error(f"Error while sending emails: {e}")
            messagebox.showerror("Send error", f"Error while sending: {e}")
            return

        all_sent_rows = merge_sent_rows(existing_sent_rows, sent_rows)
        existing_sent_rows[:] = all_sent_rows
        out_csv = Path("sent_mapping.csv")
        with out_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f, fieldnames=["name", "email", "username", "password"])
            w.writeheader()
            w.writerows(all_sent_rows)

        remaining_creds = creds[to_process:]
        max_len = max(len(absent), len(remaining_creds))
        rows = []
        for i in range(max_len):
            s = absent[i] if i < len(absent) else {"name": "", "email": ""}
            c = remaining_creds[i] if i < len(remaining_creds) else ("", "")
            rows.append({
                "name": s["name"],
                "email": s["email"],
                "username": c[0],
                "password": c[1],
            })

        if rows:
            unused_csv = Path("unused_mapping.csv")
            with unused_csv.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f, fieldnames=["name", "email", "username", "password"])
                w.writeheader()
                w.writerows(rows)
            logger.info(f"Unused mappings saved to unused_mapping.csv")

        remove_students(send_list)
        refresh_sent_list()

        status.set("Done.")
        msg = f"Sent {to_process} emails.\n"
        msg += f"Absent: {len(absent)}. Unused creds: {len(remaining_creds)}.\n"
        if rows:
            msg += "Mapped to unused_mapping.csv"
        logger.info(
            f"Distribution complete: {to_process} sent, {len(absent)} absent, {len(remaining_creds)} unused")
        messagebox.showinfo("Completed", msg)

    refresh_attendance_list()
    ttk.Button(tab_distribute_inner, text="Send Credentials",
               command=do_send).pack(pady=12, padx=(0, 12), anchor="e")

    # --- Already distributed tab widgets ---
    sent_vars = []
    canvas_sent = tk.Canvas(tab_resend_inner, borderwidth=0, height=360, background=bg_color, highlightthickness=0)
    frame_sent = ttk.Frame(canvas_sent, style="Main.TFrame")
    vsb_sent = ttk.Scrollbar(tab_resend_inner, orient="vertical", command=canvas_sent.yview)
    canvas_sent.configure(yscrollcommand=vsb_sent.set)
    vsb_sent.pack(side="right", fill="y")
    canvas_sent.pack(side="left", fill="both", expand=True, padx=(0, 8))
    canvas_sent.create_window((0, 0), window=frame_sent, anchor="nw")

    def on_frame_sent_config(event):
        canvas_sent.configure(scrollregion=canvas_sent.bbox("all"))
    frame_sent.bind("<Configure>", on_frame_sent_config)

    def refresh_sent_list():
        nonlocal sent_vars
        for child in frame_sent.winfo_children():
            child.destroy()
        sent_vars = []
        for idx, row in enumerate(existing_sent_rows, start=1):
            label = f"{idx}. {row['name']} <{row['email']}>  [{row['username']}]"
            var = tk.BooleanVar(value=False)
            cb = ttk.Checkbutton(frame_sent, text=label, variable=var, style="Main.TCheckbutton")
            cb.pack(anchor="w", padx=6, pady=3, fill="x")
            sent_vars.append(var)
        enforce_checklist_width(canvas_sent, frame_sent)

    def resend_send():
        chosen = [r for r, v in zip(existing_sent_rows, sent_vars) if v.get()]
        if not chosen:
            messagebox.showwarning("Nothing selected", "No learners selected for resend.")
            return
        if not SMTP_USERNAME or not SMTP_PASSWORD:
            messagebox.showerror("Missing SMTP credentials",
                                 "Set SMTP_USERNAME and SMTP_PASSWORD in your .env.")
            return

        try:
            for row in chosen:
                body = BODY_TEMPLATE.format(
                    name=row["name"].split()[0],
                    username=row["username"],
                    password=row["password"])
                status.set(f"Resending to {row['email']} …")
                root.update_idletasks()
                send_one(row["email"], SUBJECT, body)
                logger.info(f"Resent credentials to {row['email']}")
        except Exception as e:
            logger.error(f"Error while resending emails: {e}")
            messagebox.showerror("Resend error", f"Error while resending: {e}")
            return

        all_sent_rows = merge_sent_rows(existing_sent_rows, chosen)
        existing_sent_rows[:] = all_sent_rows
        out_csv = Path("sent_mapping.csv")
        with out_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f, fieldnames=["name", "email", "username", "password"])
            w.writeheader()
            w.writerows(all_sent_rows)

        status.set("Done.")
        messagebox.showinfo("Completed", f"Resent {len(chosen)} emails.")

    refresh_sent_list()
    ttk.Button(tab_resend_inner, text="Resend Credentials",
               command=resend_send).pack(pady=12, padx=(0, 12), anchor="e")

    root.mainloop()

# ---------------- Main ----------------


def main():
    parser = argparse.ArgumentParser(
        description="Distribute CCNA lab credentials via email"
    )
    parser.add_argument(
        "--roster",
        default=ROSTER_FILE,
        help=f"Path to roster file (default: {DEFAULT_ROSTER_FILE})"
    )
    parser.add_argument(
        "--creds",
        default=CREDS_FILE,
        help=f"Path to credentials file (default: {DEFAULT_CREDS_FILE})"
    )
    args = parser.parse_args()

    # Validate SMTP configuration
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        logger.error("SMTP credentials not configured")
        raise SystemExit(
            "Error: SMTP_USERNAME and SMTP_PASSWORD must be set.\n"
            "Set them in a .env file or as environment variables."
        )

    logger.info("Starting credential distribution tool")

    if not Path(args.roster).exists():
        logger.error(f"Roster file not found: {args.roster}")
        raise SystemExit(f"Roster file not found: {args.roster}")
    if not Path(args.creds).exists():
        logger.error(f"Creds file not found: {args.creds}")
        raise SystemExit(f"Creds file not found: {args.creds}")

    students = extract_students(args.roster)
    creds = extract_creds(args.creds)
    sent_rows = load_mapping_csv("sent_mapping.csv")
    unused_rows = load_mapping_csv("unused_mapping.csv")

    if not students:
        logger.error("No students/emails found in roster")
        raise SystemExit("No students/emails found in roster.")
    if not creds:
        logger.error("No credentials found in creds file")
        raise SystemExit("No credentials found in creds file.")

    students, creds = prepare_distribution(students, creds, sent_rows, unused_rows)

    logger.info(
        f"Loaded {len(students)} students and {len(creds)} credentials")
    run_gui_and_send(students, creds, sent_rows)


if __name__ == "__main__":
    main()

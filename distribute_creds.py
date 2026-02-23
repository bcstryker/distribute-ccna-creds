#!/usr/bin/env python3
import os
import re
import csv
import smtplib
import argparse
import logging
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

    # de-dup by email, keep first occurrence
    seen = set()
    uniq = []
    for r in records:
        if r["email"] not in seen:
            uniq.append(r)
            seen.add(r["email"])
    return uniq


def guess_name_from_row(row, delim):
    # fall back: concatenate likely name-ish fields
    parts = []
    for k, v in row.items():
        if k and v and k.lower() in ("student", "name", "first", "last"):
            parts.append(v.strip())
    return " ".join(parts)

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


def run_gui_and_send(students, creds):
    """
    students: list of {"name","email"}
    creds:    list of (username,password)
    Shows a checkbox UI to mark attendance.
    """
    import tkinter as tk
    from tkinter import ttk, messagebox

    root = tk.Tk()
    root.title("Mark Attendance — Send Cisco Lab Credentials")

    # top info
    info = ttk.Label(
        root, text=f"Students: {len(students)}   Credentials: {len(creds)}")
    info.pack(padx=12, pady=(12, 6), anchor="w")

    # Select all / none buttons
    btn_frame = ttk.Frame(root)
    btn_frame.pack(padx=12, pady=(0, 6), anchor="w")
    # container for vars
    vars_ = []

    # scrollable list
    canvas = tk.Canvas(root, borderwidth=0, height=420)
    frame = ttk.Frame(canvas)
    vsb = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True, padx=12)
    canvas.create_window((0, 0), window=frame, anchor="nw")

    def on_frame_config(event):
        canvas.configure(scrollregion=canvas.bbox("all"))
    frame.bind("<Configure>", on_frame_config)

    # Populate checkboxes
    for idx, s in enumerate(students, start=1):
        var = tk.BooleanVar(value=True)  # default to checked
        cb = ttk.Checkbutton(
            frame, text=f"{idx}. {s['name']}  <{s['email']}>", variable=var)
        cb.pack(anchor="w", padx=6, pady=3)
        vars_.append(var)

    def select_all():
        for v in vars_:
            v.set(True)

    def select_none():
        for v in vars_:
            v.set(False)

    ttk.Button(btn_frame, text="Select All",
               command=select_all).pack(side="left")
    ttk.Button(btn_frame, text="Select None", command=select_none).pack(
        side="left", padx=(8, 0))

    # status
    status = tk.StringVar(value="Ready.")
    status_lbl = ttk.Label(root, textvariable=status)
    status_lbl.pack(padx=12, pady=(6, 0), anchor="w")

    def do_send():
        chosen = [s for s, v in zip(students, vars_)
                  if v.get()]          # attendees
        # not in attendance
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
        sent_rows = []

        try:
            for i in range(to_process):
                student = chosen[i]
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

        # Save receipts of what was sent
        out_csv = Path("sent_mapping.csv")
        with out_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f, fieldnames=["name", "email", "username", "password"])
            w.writeheader()
            w.writerows(sent_rows)

        # ---- Map leftover creds to ABSENT students ----
        remaining_creds = creds[to_process:]  # unused creds
        # pair them in order
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

        status.set("Done.")
        msg = f"Sent {to_process} emails.\n"
        msg += f"Absent: {len(absent)}. Unused creds: {len(remaining_creds)}.\n"
        if rows:
            msg += "Mapped to unused_mapping.csv"
        logger.info(
            f"Distribution complete: {to_process} sent, {len(absent)} absent, {len(remaining_creds)} unused")
        messagebox.showinfo("Completed", msg)

    ttk.Button(root, text="Send Emails to Checked",
               command=do_send).pack(pady=12)
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

    if not students:
        logger.error("No students/emails found in roster")
        raise SystemExit("No students/emails found in roster.")
    if not creds:
        logger.error("No credentials found in creds file")
        raise SystemExit("No credentials found in creds file.")

    logger.info(
        f"Loaded {len(students)} students and {len(creds)} credentials")
    run_gui_and_send(students, creds)


if __name__ == "__main__":
    main()

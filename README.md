# Distribute CCNA Credentials

A tool for securely distributing Cisco CCNA lab credentials to students via email.

## Features

- **Interactive GUI**: Mark attendance with checkboxes before sending credentials
- **Flexible roster parsing**: Supports CSV, TSV, or freeform email lists
- **Robust credential extraction**: Handles various formatting of username/password pairs
- **Email tracking**: Saves logs of sent and unsent credentials
- **Audit logging**: All actions logged to `distribute_creds.log`

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Create `.env` File

Create a `.env` file in the project directory with your SMTP credentials:

```env
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=465
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password
MAIL_SUBJECT=Your Cisco Lab Credentials
MAIL_BODY=Hello {name},

Here are your credentials for the Cisco Lab Portal:

Username: {username}
Password: {password}

Access URL: https://htdlab.cisco.com

Please click "Exit" when finishing lab exercises to stop the timer.

- Your Name
```

**Important**: For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833), not your regular password. Add `SMTP_PASSWORD` to `.gitignore` to prevent accidental commits.

### 3. Prepare Input Files

#### Roster File (`roster.txt`)

Can be any of these formats:

**CSV with headers (email + student columns):**

```
Student,Email
John Doe,john@example.com
Jane Smith,jane@example.com
```

**TSV with headers:**

```
Student	Email
John Doe	john@example.com
```

**Freeform (one line per student):**

```
John Doe <john@example.com>
Jane Smith | jane@example.com
jane@example.com
```

#### Credentials File (`creds.txt`)

```
Username: user001
Password: pass123

Username: user002
Password: pass456
```

## Usage

### Basic Usage (default files)

```bash
python distribute_creds.py
```

### Custom File Paths

```bash
python distribute_creds.py --roster path/to/roster.txt --creds path/to/creds.txt
```

### CLI Arguments

- `--roster`: Path to roster file (default: `roster.txt`)
- `--creds`: Path to credentials file (default: `creds.txt`)

## Output Files

After running the tool, the following files are created:

- **`sent_mapping.csv`**: Records of credentials sent to attending students
- **`unused_mapping.csv`**: Mapping of unused credentials to absent students (if applicable)
- **`distribute_creds.log`**: Complete audit log of all operations

## Security Notes

- **Never commit sensitive files**: Ensure `.env`, `creds.txt`, `roster.txt`, `sent_mapping.csv`, and `unused_mapping.csv` are in `.gitignore`
- **Use app passwords**: For Gmail, generate an app-specific password rather than using your main account password
- **Audit logs**: Review `distribute_creds.log` to verify all operations
- **Future enhancement**: Consider encrypting stored credentials with `python-cryptography`

## Troubleshooting

### "SMTP credentials not configured"

Ensure `SMTP_USERNAME` and `SMTP_PASSWORD` are set in your `.env` file or environment variables.

### "Roster file not found"

Verify the path to `roster.txt` exists and is passed correctly via `--roster`.

### Email send failures

Check `distribute_creds.log` for detailed error messages. Common issues:

- Incorrect SMTP credentials
- Network connectivity
- Gmail app password not set up
- Incorrect SMTP server/port for your provider

## License

Internal use only.

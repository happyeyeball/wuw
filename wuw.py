#!/usr/bin/env python3
"""WUW - Website Up/Down Monitor"""

import sqlite3
import smtplib
import configparser
import sys
import argparse
import logging
from datetime import datetime
from email.mime.text import MIMEText

try:
    import requests
    from requests.exceptions import RequestException
except ImportError:
    sys.exit("requests not installed. Run: pip3 install requests")

CONFIG_PATH = "/var/wuwdev/config.ini"
LOG_PATH    = "/var/wuwdev/wuw.log"

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("wuw")


# ---------------------------------------------------------------------------
# Config / DB
# ---------------------------------------------------------------------------

def load_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return cfg


def get_db(cfg):
    path = cfg.get("database", "path", fallback="/var/wuwdev/wuw.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS websites (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            url          TEXT NOT NULL UNIQUE,
            active       INTEGER NOT NULL DEFAULT 1,
            status       TEXT,
            last_checked TEXT,
            last_down    TEXT,
            last_up      TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            website_id  INTEGER NOT NULL,
            checked_at  TEXT NOT NULL,
            status      TEXT NOT NULL,
            http_code   INTEGER,
            response_ms INTEGER,
            error       TEXT,
            FOREIGN KEY (website_id) REFERENCES websites(id)
        );
        CREATE TABLE IF NOT EXISTS recipients (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL,
            email     TEXT NOT NULL UNIQUE,
            sms_email TEXT,
            active    INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS website_recipients (
            website_id   INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            PRIMARY KEY (website_id, recipient_id),
            FOREIGN KEY (website_id)   REFERENCES websites(id),
            FOREIGN KEY (recipient_id) REFERENCES recipients(id)
        );
    """)
    conn.commit()


def migrate(conn):
    """Migrate from old recipients schema (with website_id) to normalised schema."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(recipients)").fetchall()]
    if "website_id" not in cols:
        print("Already on current schema, nothing to migrate.")
        return

    print("Migrating recipients schema...")
    conn.executescript("""
        ALTER TABLE recipients RENAME TO recipients_old;

        CREATE TABLE recipients (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            name   TEXT NOT NULL,
            email  TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE website_recipients (
            website_id   INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            PRIMARY KEY (website_id, recipient_id),
            FOREIGN KEY (website_id)   REFERENCES websites(id),
            FOREIGN KEY (recipient_id) REFERENCES recipients(id)
        );

        INSERT INTO recipients (name, email, active)
            SELECT DISTINCT name, email, active FROM recipients_old;

        INSERT INTO website_recipients (website_id, recipient_id)
            SELECT ro.website_id, r.id
            FROM recipients_old ro
            JOIN recipients r ON r.email = ro.email;

        DROP TABLE recipients_old;
    """)
    conn.commit()
    print("Migration complete.")


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------

def check_site(url, timeout):
    """Return (status, http_code, response_ms, error_str)."""
    t0 = datetime.utcnow()
    try:
        r = requests.get(
            url, timeout=timeout, allow_redirects=True,
            headers={"User-Agent": "WUW/1.0"},
        )
        ms = int((datetime.utcnow() - t0).total_seconds() * 1000)
        status = "up" if r.status_code < 400 else "down"
        return status, r.status_code, ms, None
    except RequestException as e:
        ms = int((datetime.utcnow() - t0).total_seconds() * 1000)
        return "down", None, ms, str(e)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(cfg, subject, body, to_address=None):
    try:
        to = to_address or cfg.get("email", "to_address")
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = cfg.get("email", "from_address")
        msg["To"]      = to
        host = cfg.get("email", "smtp_host")
        port = cfg.getint("email", "smtp_port")
        user = cfg.get("email", "smtp_user")
        pw   = cfg.get("email", "smtp_password")
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls()
            smtp.login(user, pw)
            smtp.send_message(msg)
        log.info("Email sent to %s: %s", to, subject)
    except Exception as e:
        log.error("Email failed to %s: %s", to_address or "default", e)


def notify_recipients(cfg, conn, website_id, subject, body):
    """Send to all active recipients for a site, fall back to config to_address."""
    rows = conn.execute(
        "SELECT r.email, r.sms_email FROM recipients r"
        " JOIN website_recipients wr ON wr.recipient_id = r.id"
        " WHERE wr.website_id = ? AND r.active = 1",
        (website_id,),
    ).fetchall()
    if rows:
        for r in rows:
            send_email(cfg, subject, body, r["email"])
            if r["sms_email"]:
                send_email(cfg, subject, body, r["sms_email"])
    else:
        send_email(cfg, subject, body)


# ---------------------------------------------------------------------------
# Monitoring run
# ---------------------------------------------------------------------------

def run_checks(cfg, conn):
    timeout = cfg.getint("monitoring", "timeout_seconds", fallback=10)
    now     = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    sites = conn.execute("SELECT * FROM websites WHERE active = 1").fetchall()
    if not sites:
        log.info("No active sites to check.")
        return

    for site in sites:
        status, code, ms, error = check_site(site["url"], timeout)
        prev   = site["status"]
        name   = site["name"]
        url    = site["url"]
        code_s = str(code) if code else "N/A"

        conn.execute(
            "INSERT INTO audit_log (website_id, checked_at, status, http_code, response_ms, error)"
            " VALUES (?,?,?,?,?,?)",
            (site["id"], now, status, code, ms, error),
        )

        if status == "down":
            conn.execute(
                "UPDATE websites SET status=?, last_checked=?, last_down=? WHERE id=?",
                (status, now, now, site["id"]),
            )
        else:
            conn.execute(
                "UPDATE websites SET status=?, last_checked=?, last_up=? WHERE id=?",
                (status, now, now, site["id"]),
            )
        conn.commit()

        if status == "down" and prev != "down":
            subject = f"WUW ALERT: {name} is DOWN"
            body = (
                f"Site:      {name}\n"
                f"URL:       {url}\n"
                f"Status:    DOWN\n"
                f"HTTP Code: {code_s}\n"
                f"Error:     {error or 'N/A'}\n"
                f"Detected:  {now} UTC\n"
            )
            notify_recipients(cfg, conn, site["id"], subject, body)
            log.warning("DOWN: %s (%s) code=%s", name, url, code_s)

        elif status == "up" and prev == "down":
            subject = f"WUW RECOVERY: {name} is back UP"
            body = (
                f"Site:      {name}\n"
                f"URL:       {url}\n"
                f"Status:    UP\n"
                f"HTTP Code: {code_s}\n"
                f"Response:  {ms}ms\n"
                f"Recovered: {now} UTC\n"
            )
            notify_recipients(cfg, conn, site["id"], subject, body)
            log.info("RECOVERED: %s (%s) code=%s %dms", name, url, code_s, ms)

        else:
            log.info("%s: %s (%s) code=%s %dms",
                     "UP" if status == "up" else "DOWN", name, url, code_s, ms)


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_test_email(cfg):
    print(f"Sending test email to {cfg.get('email', 'to_address')}...")
    send_email(cfg, "WUW Test Email", "WUW email is configured correctly.")
    print("Done — check your inbox. Any SMTP errors appear above.")


def cmd_check(args, cfg):
    timeout = cfg.getint("monitoring", "timeout_seconds", fallback=10)
    status, code, ms, error = check_site(args.url, timeout)
    print(f"URL:    {args.url}")
    print(f"Status: {status.upper()}")
    print(f"Code:   {code or 'N/A'}")
    print(f"Time:   {ms}ms")
    if error:
        print(f"Error:  {error}")


def cmd_add(args, conn):
    try:
        conn.execute("INSERT INTO websites (name, url) VALUES (?,?)", (args.name, args.url))
        conn.commit()
        print(f"Added: {args.name} -> {args.url}")
    except sqlite3.IntegrityError:
        print(f"URL already exists: {args.url}")


def cmd_remove(args, conn):
    row = conn.execute("SELECT id, name FROM websites WHERE url=?", (args.url,)).fetchone()
    if not row:
        print(f"Not found: {args.url}")
        return
    conn.execute("UPDATE websites SET active=0 WHERE id=?", (row["id"],))
    conn.commit()
    print(f"Deactivated: {row['name']}")


def cmd_list(conn):
    sites = conn.execute(
        "SELECT id, name, url, active, status, last_checked FROM websites ORDER BY id"
    ).fetchall()
    if not sites:
        print("No sites in database.")
        return
    print(f"{'ID':<4} {'Active':<7} {'Status':<6} {'Name':<25} {'Last Checked':<22} URL")
    print("-" * 110)
    for s in sites:
        print(f"{s['id']:<4} {'yes' if s['active'] else 'no':<7} {s['status'] or '?':<6} "
              f"{s['name']:<25} {(s['last_checked'] or 'never'):<22} {s['url']}")


def cmd_add_recipient(args, conn):
    """Add a recipient (find or create), then link to a site."""
    row = conn.execute("SELECT id FROM recipients WHERE email=?", (args.email,)).fetchone()
    if row:
        rec_id = row["id"]
        print(f"Recipient already exists: {args.email}")
    else:
        cur = conn.execute(
            "INSERT INTO recipients (name, email) VALUES (?,?)", (args.name, args.email)
        )
        rec_id = cur.lastrowid
        conn.commit()
        print(f"Created recipient: {args.name} <{args.email}>")

    if args.site_id:
        site = conn.execute("SELECT name FROM websites WHERE id=?", (args.site_id,)).fetchone()
        if not site:
            print(f"No website with id {args.site_id}")
            return
        try:
            conn.execute(
                "INSERT INTO website_recipients (website_id, recipient_id) VALUES (?,?)",
                (args.site_id, rec_id),
            )
            conn.commit()
            print(f"Linked to: {site['name']}")
        except sqlite3.IntegrityError:
            print(f"Already linked to: {site['name']}")


def cmd_remove_recipient(args, conn):
    row = conn.execute("SELECT id, name, email FROM recipients WHERE id=?", (args.id,)).fetchone()
    if not row:
        print(f"No recipient with id {args.id}")
        return
    conn.execute("UPDATE recipients SET active=0 WHERE id=?", (args.id,))
    conn.commit()
    print(f"Deactivated: {row['name']} <{row['email']}>")


def cmd_link(args, conn):
    site = conn.execute("SELECT name FROM websites WHERE id=?", (args.site_id,)).fetchone()
    rec  = conn.execute("SELECT name, email FROM recipients WHERE id=?", (args.recipient_id,)).fetchone()
    if not site:
        print(f"No website with id {args.site_id}")
        return
    if not rec:
        print(f"No recipient with id {args.recipient_id}")
        return
    try:
        conn.execute(
            "INSERT INTO website_recipients (website_id, recipient_id) VALUES (?,?)",
            (args.site_id, args.recipient_id),
        )
        conn.commit()
        print(f"Linked: {rec['name']} <{rec['email']}> -> {site['name']}")
    except sqlite3.IntegrityError:
        print(f"Already linked.")


def cmd_unlink(args, conn):
    conn.execute(
        "DELETE FROM website_recipients WHERE website_id=? AND recipient_id=?",
        (args.site_id, args.recipient_id),
    )
    conn.commit()
    print("Unlinked.")


def cmd_list_recipients(args, conn):
    rows = conn.execute(
        "SELECT r.id, r.name, r.email, r.active,"
        " GROUP_CONCAT(w.name, ', ') as sites"
        " FROM recipients r"
        " LEFT JOIN website_recipients wr ON wr.recipient_id = r.id"
        " LEFT JOIN websites w ON w.id = wr.website_id"
        " GROUP BY r.id ORDER BY r.id"
    ).fetchall()
    if not rows:
        print("No recipients found.")
        return
    print(f"{'ID':<4} {'Active':<7} {'Name':<20} {'Email':<35} {'SMS':<30} Sites")
    print("-" * 115)
    for r in rows:
        print(f"{r['id']:<4} {'yes' if r['active'] else 'no':<7} {r['name']:<20} "
              f"{r['email']:<35} {(r['sms_email'] or ''):<30} {r['sites'] or '(none)'}")


def cmd_history(args, conn):
    row = conn.execute("SELECT id, name FROM websites WHERE url=?", (args.url,)).fetchone()
    if not row:
        print(f"Not found: {args.url}")
        return
    rows = conn.execute(
        "SELECT checked_at, status, http_code, response_ms, error FROM audit_log"
        " WHERE website_id=? ORDER BY id DESC LIMIT ?",
        (row["id"], args.limit),
    ).fetchall()
    print(f"History for {row['name']} ({args.url})  — last {args.limit} checks")
    print(f"{'Checked At':<22} {'Status':<6} {'Code':<6} {'Ms':<6} Error")
    print("-" * 80)
    for r in rows:
        print(f"{r['checked_at']:<22} {r['status']:<6} {str(r['http_code'] or ''):<6} "
              f"{str(r['response_ms'] or ''):<6} {r['error'] or ''}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="WUW - Website Up/Down Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  wuw.py check https://example.com\n"
            "  wuw.py add https://example.com 'Example Site'\n"
            "  wuw.py add-recipient jeffrey@example.com 'Jeffrey' --site 1\n"
            "  wuw.py link 1 1\n"
            "  wuw.py list\n"
            "  wuw.py list-recipients\n"
            "  wuw.py run\n"
            "  wuw.py migrate\n"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="Quick check a single URL (no DB required)")
    p_check.add_argument("url")

    p_add = sub.add_parser("add", help="Add a site to monitor")
    p_add.add_argument("url")
    p_add.add_argument("name")

    p_rm = sub.add_parser("remove", help="Deactivate a monitored site")
    p_rm.add_argument("url")

    sub.add_parser("list", help="List all monitored sites and current status")

    p_hist = sub.add_parser("history", help="Show audit log for a site")
    p_hist.add_argument("url")
    p_hist.add_argument("--limit", type=int, default=20)

    sub.add_parser("init-db", help="Initialize the database")
    sub.add_parser("migrate", help="Migrate recipients to normalised schema")
    sub.add_parser("run", help="Run all checks — cron entry point")
    sub.add_parser("test-email", help="Send a test email to verify SMTP settings")

    p_addr = sub.add_parser("add-recipient", help="Add a recipient (and optionally link to a site)")
    p_addr.add_argument("email")
    p_addr.add_argument("name")
    p_addr.add_argument("--site", dest="site_id", type=int, default=None,
                        help="Website ID to link to")

    p_rmr = sub.add_parser("remove-recipient", help="Deactivate a recipient")
    p_rmr.add_argument("id", type=int)

    p_link = sub.add_parser("link", help="Link a recipient to a site")
    p_link.add_argument("site_id", type=int)
    p_link.add_argument("recipient_id", type=int)

    p_unlink = sub.add_parser("unlink", help="Unlink a recipient from a site")
    p_unlink.add_argument("site_id", type=int)
    p_unlink.add_argument("recipient_id", type=int)

    sub.add_parser("list-recipients", help="List all recipients and their linked sites")

    args = parser.parse_args()
    cfg  = load_config()

    if args.command == "test-email":
        cmd_test_email(cfg)
        return

    if args.command == "check":
        cmd_check(args, cfg)
        return

    conn = get_db(cfg)
    init_db(conn)

    if args.command == "init-db":
        print("Database initialized.")
    elif args.command == "migrate":
        migrate(conn)
    elif args.command == "add":
        cmd_add(args, conn)
    elif args.command == "remove":
        cmd_remove(args, conn)
    elif args.command == "list":
        cmd_list(conn)
    elif args.command == "history":
        cmd_history(args, conn)
    elif args.command == "add-recipient":
        cmd_add_recipient(args, conn)
    elif args.command == "remove-recipient":
        cmd_remove_recipient(args, conn)
    elif args.command == "link":
        cmd_link(args, conn)
    elif args.command == "unlink":
        cmd_unlink(args, conn)
    elif args.command == "list-recipients":
        cmd_list_recipients(args, conn)
    elif args.command in ("run", None):
        run_checks(cfg, conn)

    conn.close()


if __name__ == "__main__":
    main()

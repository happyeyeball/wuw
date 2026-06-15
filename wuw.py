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
    """)
    conn.commit()


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

def send_email(cfg, subject, body):
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = cfg.get("email", "from_address")
        msg["To"]      = cfg.get("email", "to_address")
        host = cfg.get("email", "smtp_host")
        port = cfg.getint("email", "smtp_port")
        user = cfg.get("email", "smtp_user")
        pw   = cfg.get("email", "smtp_password")
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls()
            smtp.login(user, pw)
            smtp.send_message(msg)
        log.info("Email sent: %s", subject)
    except Exception as e:
        log.error("Email failed: %s", e)


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
            send_email(cfg, subject, body)
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
            send_email(cfg, subject, body)
            log.info("RECOVERED: %s (%s) code=%s %dms", name, url, code_s, ms)

        else:
            log.info("%s: %s (%s) code=%s %dms",
                     "UP" if status == "up" else "DOWN", name, url, code_s, ms)


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_check(args, cfg):
    """Quick single-URL check — no database needed."""
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
            "  wuw.py list\n"
            "  wuw.py run\n"
            "  wuw.py history https://example.com\n"
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

    sub.add_parser("run", help="Run all checks — cron entry point")

    args = parser.parse_args()
    cfg  = load_config()

    if args.command == "check":
        cmd_check(args, cfg)
        return

    conn = get_db(cfg)
    init_db(conn)

    if args.command == "init-db":
        print("Database initialized.")
    elif args.command == "add":
        cmd_add(args, conn)
    elif args.command == "remove":
        cmd_remove(args, conn)
    elif args.command == "list":
        cmd_list(conn)
    elif args.command == "history":
        cmd_history(args, conn)
    elif args.command in ("run", None):
        run_checks(cfg, conn)

    conn.close()


if __name__ == "__main__":
    main()

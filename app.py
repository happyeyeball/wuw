#!/usr/bin/env python3
"""WUW - Web Dashboard"""

from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3

DB_PATH     = '/var/wuwdev/wuw.db'
app         = Flask(__name__)
app.secret_key = 'wuw-s3cr3t-k3y'


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    conn  = get_db()
    sites = conn.execute('''
        SELECT w.*,
               GROUP_CONCAT(r.name || ' <' || r.email || '>', ', ') AS recipients
        FROM websites w
        LEFT JOIN website_recipients wr ON wr.website_id = w.id
        LEFT JOIN recipients r ON r.id = wr.recipient_id AND r.active = 1
        WHERE w.active = 1
        GROUP BY w.id
        ORDER BY w.name
    ''').fetchall()
    conn.close()
    return render_template('index.html', sites=sites)


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------

@app.route('/sites/add', methods=['GET', 'POST'])
def site_add():
    if request.method == 'POST':
        name = request.form['name'].strip()
        url  = request.form['url'].strip()
        if not name or not url:
            flash('Name and URL are required.', 'danger')
        else:
            conn = get_db()
            try:
                conn.execute('INSERT INTO websites (name, url) VALUES (?,?)', (name, url))
                conn.commit()
                flash(f'Added: {name}', 'success')
                return redirect(url_for('index'))
            except sqlite3.IntegrityError:
                flash('URL already exists.', 'danger')
            finally:
                conn.close()
    return render_template('site_form.html', site=None)


@app.route('/sites/<int:id>/edit', methods=['GET', 'POST'])
def site_edit(id):
    conn = get_db()
    site = conn.execute('SELECT * FROM websites WHERE id=?', (id,)).fetchone()
    if not site:
        flash('Site not found.', 'danger')
        return redirect(url_for('index'))
    if request.method == 'POST':
        name = request.form['name'].strip()
        url  = request.form['url'].strip()
        if not name or not url:
            flash('Name and URL are required.', 'danger')
        else:
            try:
                conn.execute('UPDATE websites SET name=?, url=? WHERE id=?', (name, url, id))
                conn.commit()
                flash(f'Updated: {name}', 'success')
                return redirect(url_for('index'))
            except sqlite3.IntegrityError:
                flash('URL already exists.', 'danger')
            finally:
                conn.close()
    return render_template('site_form.html', site=site)


@app.route('/sites/<int:id>/delete', methods=['POST'])
def site_delete(id):
    conn = get_db()
    site = conn.execute('SELECT name FROM websites WHERE id=?', (id,)).fetchone()
    if site:
        conn.execute('UPDATE websites SET active=0 WHERE id=?', (id,))
        conn.commit()
        flash(f'Removed: {site["name"]}', 'success')
    conn.close()
    return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------

@app.route('/recipients')
def recipients():
    conn = get_db()
    recs = conn.execute('''
        SELECT r.*,
               GROUP_CONCAT(w.name, ', ') AS sites
        FROM recipients r
        LEFT JOIN website_recipients wr ON wr.recipient_id = r.id
        LEFT JOIN websites w ON w.id = wr.website_id AND w.active = 1
        WHERE r.active = 1
        GROUP BY r.id
        ORDER BY r.name
    ''').fetchall()
    conn.close()
    return render_template('recipients.html', recipients=recs)


@app.route('/recipients/add', methods=['GET', 'POST'])
def recipient_add():
    conn  = get_db()
    sites = conn.execute('SELECT * FROM websites WHERE active=1 ORDER BY name').fetchall()
    if request.method == 'POST':
        name     = request.form['name'].strip()
        email    = request.form['email'].strip()
        site_ids = request.form.getlist('site_ids')
        sms_email = request.form.get('sms_email', '').strip() or None
        if not name or not email:
            flash('Name and email are required.', 'danger')
        else:
            try:
                cur    = conn.execute('INSERT INTO recipients (name, email, sms_email) VALUES (?,?,?)', (name, email, sms_email))
                rec_id = cur.lastrowid
                for sid in site_ids:
                    conn.execute(
                        'INSERT INTO website_recipients (website_id, recipient_id) VALUES (?,?)',
                        (int(sid), rec_id),
                    )
                conn.commit()
                flash(f'Added: {name}', 'success')
                return redirect(url_for('recipients'))
            except sqlite3.IntegrityError:
                flash('Email already exists.', 'danger')
            finally:
                conn.close()
    return render_template('recipient_form.html', recipient=None, sites=sites, linked_ids=set())


@app.route('/recipients/<int:id>/edit', methods=['GET', 'POST'])
def recipient_edit(id):
    conn   = get_db()
    rec    = conn.execute('SELECT * FROM recipients WHERE id=?', (id,)).fetchone()
    if not rec:
        flash('Recipient not found.', 'danger')
        return redirect(url_for('recipients'))
    sites  = conn.execute('SELECT * FROM websites WHERE active=1 ORDER BY name').fetchall()
    linked = {r[0] for r in conn.execute(
        'SELECT website_id FROM website_recipients WHERE recipient_id=?', (id,)
    ).fetchall()}
    if request.method == 'POST':
        name     = request.form['name'].strip()
        email    = request.form['email'].strip()
        site_ids  = {int(s) for s in request.form.getlist('site_ids')}
        sms_email = request.form.get('sms_email', '').strip() or None
        if not name or not email:
            flash('Name and email are required.', 'danger')
        else:
            try:
                conn.execute('UPDATE recipients SET name=?, email=?, sms_email=? WHERE id=?', (name, email, sms_email, id))
                for sid in site_ids - linked:
                    conn.execute(
                        'INSERT OR IGNORE INTO website_recipients (website_id, recipient_id) VALUES (?,?)',
                        (sid, id),
                    )
                for sid in linked - site_ids:
                    conn.execute(
                        'DELETE FROM website_recipients WHERE website_id=? AND recipient_id=?',
                        (sid, id),
                    )
                conn.commit()
                flash(f'Updated: {name}', 'success')
                return redirect(url_for('recipients'))
            except sqlite3.IntegrityError:
                flash('Email already in use.', 'danger')
            finally:
                conn.close()
    return render_template('recipient_form.html', recipient=rec, sites=sites, linked_ids=linked)


@app.route('/recipients/<int:id>/delete', methods=['POST'])
def recipient_delete(id):
    conn = get_db()
    rec  = conn.execute('SELECT name FROM recipients WHERE id=?', (id,)).fetchone()
    if rec:
        conn.execute('UPDATE recipients SET active=0 WHERE id=?', (id,))
        conn.commit()
        flash(f'Removed: {rec["name"]}', 'success')
    conn.close()
    return redirect(url_for('recipients'))


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)

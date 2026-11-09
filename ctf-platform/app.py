import csv
import hmac
import io
import os
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse

from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, send_from_directory, session,
                   url_for)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import and_, func, inspect as sa_inspect, or_, text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
CHALLENGE_UPLOAD_DIR = os.path.join(UPLOAD_DIR, "challenges")
os.makedirs(INSTANCE_DIR, exist_ok=True)
os.makedirs(CHALLENGE_UPLOAD_DIR, exist_ok=True)

MAX_BG_BYTES = 6 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_TEAM_SIZE = 8
TEAM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{2,39}$")


def load_secret_key():
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    path = os.path.join(INSTANCE_DIR, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret_key(),
    SQLALCHEMY_DATABASE_URI="sqlite:///" + os.path.join(INSTANCE_DIR, "ctf.db"),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    MAX_CONTENT_LENGTH=32 * 1024 * 1024,  # 32 MB (forms + uploads)
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("HTTPS") == "1",
)
db = SQLAlchemy(app)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(40), unique=True, nullable=False)
    invite_code = db.Column(db.String(16), unique=True, nullable=False, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created = db.Column(db.DateTime, default=now)
    captain = db.relationship("User", foreign_keys=[created_by])
    members = db.relationship("User", back_populates="team", foreign_keys="User.team_id")

    @property
    def member_count(self):
        return len(self.members)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(24), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=True)
    created = db.Column(db.DateTime, default=now)
    team = db.relationship("Team", foreign_keys=[team_id], back_populates="members")


class Challenge(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(40), nullable=False, default="General")
    description = db.Column(db.Text, nullable=False, default="")
    hints = db.Column(db.Text)                 # one hint per line
    points = db.Column(db.Integer, nullable=False, default=100)
    flag = db.Column(db.String(255), nullable=False)
    url = db.Column(db.String(500))
    file_name = db.Column(db.String(255))      # randomised name on disk
    file_original = db.Column(db.String(160))  # name shown to players
    file_size = db.Column(db.Integer)
    visible = db.Column(db.Boolean, default=True, nullable=False)
    start_at = db.Column(db.DateTime)          # UTC; empty = always open
    end_at = db.Column(db.DateTime)            # UTC; empty = no deadline
    created = db.Column(db.DateTime, default=now)

    @property
    def hint_list(self):
        """Hints split into clean lines for templates and the player dialog."""
        return [line.strip() for line in (self.hints or "").splitlines() if line.strip()]

    def state_at(self, at=None):
        """Return live, scheduled or ended for the given UTC time."""
        at = at or now()
        if self.start_at and self.start_at > at:
            return "scheduled"
        if self.end_at and self.end_at <= at:
            return "ended"
        return "live"

    @property
    def state(self):
        return self.state_at()


class Solve(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenge.id"), nullable=False)
    solved_at = db.Column(db.DateTime, default=now)
    user = db.relationship("User")
    challenge = db.relationship("Challenge")
    __table_args__ = (db.UniqueConstraint("user_id", "challenge_id"),)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    message = db.Column(db.Text, nullable=False, default="")
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created = db.Column(db.DateTime, default=now)
    author = db.relationship("User", foreign_keys=[created_by])


class NotificationRead(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    notification_id = db.Column(db.Integer, db.ForeignKey("notification.id"), nullable=False)
    read_at = db.Column(db.DateTime, default=now)
    __table_args__ = (db.UniqueConstraint("user_id", "notification_id"),)


class Setting(db.Model):
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")


THEMES = {
    "arena": "Hulk",
    "violet": "Thor",
    "ember": "Iron Man",
    "frost": "Captain America",
}

# The six Infinity Stones double as the six challenge categories. Clearing
# every challenge inside a stone's category earns that stone; all six unlock
# the Snap.
STONES = [
    {"key": "power",  "cat": "Power Stone",  "name": "Power",  "color": "#a855f7", "rgb": "168, 85, 247"},
    {"key": "space",  "cat": "Space Stone",  "name": "Space",  "color": "#3b82f6", "rgb": "59, 130, 246"},
    {"key": "reality", "cat": "Reality Stone", "name": "Reality", "color": "#ef4444", "rgb": "239, 68, 68"},
    {"key": "soul",   "cat": "Soul Stone",   "name": "Soul",   "color": "#f97316", "rgb": "249, 115, 22"},
    {"key": "mind",   "cat": "Mind Stone",   "name": "Mind",   "color": "#eab308", "rgb": "234, 179, 8"},
    {"key": "time",   "cat": "Time Stone",   "name": "Time",   "color": "#22c55e", "rgb": "34, 197, 94"},
]
STONE_CATS = [s["cat"] for s in STONES]
STONE_BY_CAT = {s["cat"]: s for s in STONES}
# A stone is claimed after this many solves in its category. Extra challenges
# beyond this count are optional — useful when an admin adds more targets.
STONE_SOLVES_REQUIRED = 3

DEFAULTS = {
    "platform_name": "CTF Platform",
    "tagline": "Hack, solve, capture the flag.",
    "registration_open": "1",
    "bg_image": "",  # empty = bundled banner
    "scoreboard_visible": "1",
    "accent_theme": "arena",
    "platform_mode": "user",  # user = individual scores, team = team scores
}


def get_setting(key):
    row = db.session.get(Setting, key)
    return row.value if row else DEFAULTS[key]


def set_setting(key, value):
    row = db.session.get(Setting, key)
    if row:
        row.value = value
    else:
        db.session.add(Setting(key=key, value=value))


def get_theme():
    theme = get_setting("accent_theme")
    return theme if theme in THEMES else "arena"


def platform_mode():
    mode = get_setting("platform_mode")
    return mode if mode in ("user", "team") else "user"


def is_team_mode():
    return platform_mode() == "team"


def new_invite_code():
    while True:
        code = secrets.token_hex(4)
        if not Team.query.filter_by(invite_code=code).first():
            return code


def parse_datetime_field(value):
    """Parse a datetime-local form value. Blank means no schedule bound."""
    value = (value or "").strip()
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def challenge_started_clause(at=None):
    at = at or now()
    return or_(Challenge.start_at.is_(None), Challenge.start_at <= at)


def challenge_live_clause(at=None):
    at = at or now()
    return and_(
        challenge_started_clause(at),
        or_(Challenge.end_at.is_(None), Challenge.end_at > at),
    )


def team_solved_ids(team):
    """Challenge ids already captured by a team (admins do not count)."""
    if not team:
        return set()
    rows = (db.session.query(Solve.challenge_id)
            .join(User, User.id == Solve.user_id)
            .filter(User.team_id == team.id, User.is_admin.is_(False))
            .distinct().all())
    return {row[0] for row in rows}


def team_score(team):
    """Sum each team challenge once, regardless of how many members solved it."""
    if not team:
        return 0
    solved = (db.session.query(Solve.challenge_id, Challenge.points)
              .join(User, User.id == Solve.user_id)
              .join(Challenge, Challenge.id == Solve.challenge_id)
              .filter(User.team_id == team.id, User.is_admin.is_(False))
              .group_by(Solve.challenge_id, Challenge.points)
              .subquery())
    return int(db.session.query(func.coalesce(func.sum(solved.c.points), 0)).scalar() or 0)


def remove_user_from_team(user):
    """Remove one member, transferring captaincy or deleting an empty team."""
    team = user.team
    if not team:
        return None
    if team.created_by == user.id:
        replacement = (User.query
                       .filter(User.team_id == team.id, User.id != user.id)
                       .order_by(User.created, User.id).first())
        team.created_by = replacement.id if replacement else None
    user.team_id = None
    db.session.flush()
    if not User.query.filter_by(team_id=team.id).count():
        db.session.delete(team)
    return team


def disband_team(team):
    User.query.filter_by(team_id=team.id).update({User.team_id: None})
    db.session.delete(team)


# --------------------------------------------------------------------------
# Helpers: rate limiting, CSRF, auth decorators
# --------------------------------------------------------------------------
_hits = defaultdict(deque)


def allow(key, limit, window):
    t = time.time()
    q = _hits[key]
    while q and t - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(t)
    return True


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not g.user:
            flash("Log in to continue.", "error")
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not g.user:
            return redirect(url_for("login"))
        if not g.user.is_admin:
            abort(403)
        return fn(*a, **kw)
    return wrapper


@app.before_request
def load_user():
    uid = session.get("uid")
    g.user = db.session.get(User, uid) if uid else None
    if request.method == "POST":
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRFToken") or ""
        if not hmac.compare_digest(sent.encode(), session.get("csrf", "").encode()):
            abort(400, "Your session expired. Reload the page and try again.")


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; script-src 'self'; frame-ancestors 'none'"
    )
    return resp


def stone_progress(user):
    """Per-stone completion for one player or their team. Returns six dicts."""
    totals = dict(db.session.query(Challenge.category, func.count(Challenge.id))
                  .filter(Challenge.visible.is_(True),
                          Challenge.category.in_(STONE_CATS),
                          challenge_started_clause())
                  .group_by(Challenge.category).all())
    done = {}
    if user and is_team_mode():
        if user.team_id:
            rows = (db.session.query(Challenge.category,
                                     func.count(func.distinct(Solve.challenge_id)))
                    .join(Solve, Solve.challenge_id == Challenge.id)
                    .join(User, User.id == Solve.user_id)
                    .filter(User.team_id == user.team_id,
                            User.is_admin.is_(False),
                            Challenge.visible.is_(True),
                            Challenge.category.in_(STONE_CATS),
                            challenge_started_clause())
                    .group_by(Challenge.category).all())
            done = dict(rows)
    elif user:
        rows = (db.session.query(Challenge.category,
                                 func.count(func.distinct(Solve.challenge_id)))
                .join(Solve, Solve.challenge_id == Challenge.id)
                .filter(Solve.user_id == user.id,
                        Challenge.visible.is_(True),
                        Challenge.category.in_(STONE_CATS),
                        challenge_started_clause())
                .group_by(Challenge.category).all())
        done = dict(rows)
    out = []
    for stone in STONES:
        total = int(totals.get(stone["cat"], 0))
        solved = min(int(done.get(stone["cat"], 0)), total)
        required = min(STONE_SOLVES_REQUIRED, total)
        out.append(dict(stone, total=total, solved=solved, required=required,
                        unlocked=bool(required) and solved >= required))
    return out


def stone_payload(user):
    """JSON-friendly snapshot of the Stone Hunt for the flag-submission reply."""
    stones = stone_progress(user)
    collected = sum(1 for s in stones if s["unlocked"])
    return dict(stones=stones, stones_collected=collected,
                stones_complete=collected == len(STONES))


@app.context_processor
def inject_globals():
    bg = get_setting("bg_image")
    bg_url = url_for("static", filename=("uploads/" + bg) if bg else "img/banner.png")
    try:
        static_version = int(max(
            os.path.getmtime(os.path.join(BASE_DIR, "static", "style.css")),
            os.path.getmtime(os.path.join(BASE_DIR, "static", "app.js")),
        ))
    except OSError:
        static_version = 0
    stones = stone_progress(g.get("user"))
    collected = sum(1 for s in stones if s["unlocked"])
    unread_notifications = 0
    if g.get("user"):
        unread_notifications = (db.session.query(func.count(Notification.id))
                                .outerjoin(NotificationRead, and_(
                                    NotificationRead.notification_id == Notification.id,
                                    NotificationRead.user_id == g.user.id))
                                .filter(Notification.active.is_(True),
                                        NotificationRead.id.is_(None))
                                .scalar() or 0)
    return dict(
        csrf_token=csrf_token,
        platform_name=get_setting("platform_name"),
        tagline=get_setting("tagline"),
        registration_open=get_setting("registration_open") == "1",
        scoreboard_visible=get_setting("scoreboard_visible") == "1",
        accent_theme=get_theme(),
        platform_mode=platform_mode(),
        team_mode=is_team_mode(),
        bg_url=bg_url,
        static_version=static_version,
        me=g.get("user"),
        my_team=(g.user.team if g.get("user") else None),
        unread_notifications=unread_notifications,
        max_team_size=MAX_TEAM_SIZE,
        stones=stones,
        stones_by_cat={s["cat"]: s for s in stones},
        stones_collected=collected,
        stones_complete=collected == len(STONES),
        stone_required=STONE_SOLVES_REQUIRED,
    )


# --------------------------------------------------------------------------
# Public + user routes
# --------------------------------------------------------------------------
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,24}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DUMMY_HASH = generate_password_hash("not-a-real-password")


def leaderboard_rows(user=None):
    """Scoreboard rows as dicts, aggregated per user or per team."""
    if is_team_mode():
        solved = (db.session.query(
                      User.team_id.label("team_id"),
                      Solve.challenge_id.label("challenge_id"),
                      func.min(Solve.solved_at).label("first_solve"),
                      Challenge.points.label("points"))
                  .join(Solve, Solve.user_id == User.id)
                  .join(Challenge, Challenge.id == Solve.challenge_id)
                  .filter(User.team_id.isnot(None), User.is_admin.is_(False))
                  .group_by(User.team_id, Solve.challenge_id, Challenge.points)
                  .subquery())
        rows = (db.session.query(Team.id, Team.name,
                                 func.coalesce(func.sum(solved.c.points), 0),
                                 func.count(solved.c.challenge_id),
                                 func.max(solved.c.first_solve))
                .outerjoin(solved, solved.c.team_id == Team.id)
                .group_by(Team.id).all())
        rows.sort(key=lambda r: (-int(r[2] or 0), r[4] or datetime.max, r[1].lower()))
        my_id = user.team_id if user else None
        return [dict(id=r[0], name=r[1], score=int(r[2] or 0),
                     solves=int(r[3] or 0), last=r[4],
                     is_me=bool(my_id and r[0] == my_id)) for r in rows]

    rows = (db.session.query(User.id, User.username,
                             func.coalesce(func.sum(Challenge.points), 0),
                             func.count(Solve.id), func.max(Solve.solved_at))
            .outerjoin(Solve, Solve.user_id == User.id)
            .outerjoin(Challenge, Challenge.id == Solve.challenge_id)
            .filter(User.is_admin.is_(False))
            .group_by(User.id).all())
    rows.sort(key=lambda r: (-r[2], r[4] or datetime.max))
    my_id = user.id if user else None
    return [dict(id=r[0], name=r[1], score=int(r[2] or 0), solves=int(r[3] or 0),
                 last=r[4], is_me=bool(my_id and r[0] == my_id)) for r in rows]


@app.route("/")
def index():
    at = now()
    stats = dict(
        challenges=Challenge.query.filter(Challenge.visible.is_(True),
                                          challenge_live_clause(at)).count(),
        upcoming=Challenge.query.filter(Challenge.visible.is_(True),
                                        Challenge.start_at > at).count(),
        players=User.query.filter_by(is_admin=False).count(),
        teams=Team.query.count(),
        solves=Solve.query.count(),
    )
    categories = (db.session.query(Challenge.category, func.count(Challenge.id))
                  .filter(Challenge.visible.is_(True), challenge_live_clause(at))
                  .group_by(Challenge.category)
                  .order_by(func.count(Challenge.id).desc(), Challenge.category)
                  .all())
    return render_template("index.html", stats=stats, categories=categories)


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("challenges"))
    if get_setting("registration_open") != "1":
        flash("Registration is closed.", "error")
        return redirect(url_for("login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        error = None
        if not USERNAME_RE.match(username):
            error = "Username must be 3–24 characters: letters, numbers, _ or -."
        elif not EMAIL_RE.match(email) or len(email) > 120:
            error = "Enter a valid email address."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != request.form.get("confirm", ""):
            error = "Passwords don't match."
        elif User.query.filter(func.lower(User.username) == username.lower()).first():
            error = "That username is taken."
        elif User.query.filter_by(email=email).first():
            error = "That email is already registered."
        if error:
            flash(error, "error")
            return render_template("register.html", form=request.form), 400
        user = User(username=username, email=email,
                    password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        session.clear()
        session["uid"] = user.id
        flash("Welcome aboard. Pick a challenge to start.", "ok")
        return redirect(url_for("challenges"))
    return render_template("register.html", form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("challenges"))
    if request.method == "POST":
        if not allow("login:" + (request.remote_addr or "?"), 10, 300):
            flash("Too many attempts. Wait a few minutes and try again.", "error")
            return render_template("login.html"), 429
        name = request.form.get("username", "").strip().lower()
        pw = request.form.get("password", "")
        user = User.query.filter(func.lower(User.username) == name).first()
        ok = check_password_hash(user.password_hash if user else DUMMY_HASH, pw)
        if user and ok:
            session.clear()
            session["uid"] = user.id
            return redirect(url_for("admin_dashboard" if user.is_admin else "challenges"))
        flash("Wrong username or password.", "error")
        return render_template("login.html"), 401
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/challenges")
@login_required
def challenges():
    items = (Challenge.query.filter_by(visible=True)
             .order_by(Challenge.category, Challenge.points, Challenge.id).all())
    if is_team_mode():
        solved = team_solved_ids(g.user.team)
        score = team_score(g.user.team)
    else:
        solved = {s.challenge_id for s in Solve.query.filter_by(user_id=g.user.id)}
        score = sum(c.points for c in items if c.id in solved)
    groups = {}
    for c in items:
        groups.setdefault(c.category, []).append(c)
    order = {cat: i for i, cat in enumerate(STONE_CATS)}
    groups = dict(sorted(groups.items(),
                         key=lambda kv: (order.get(kv[0], len(order)), kv[0])))
    return render_template("challenges.html", groups=groups, solved=solved, score=score)


@app.post("/challenges/<int:cid>/submit")
@login_required
def submit(cid):
    ch = db.session.get(Challenge, cid)
    if not ch or not ch.visible:
        return jsonify(status="error", message="Challenge not found."), 404
    state = ch.state
    if state != "live" and not g.user.is_admin:
        if state == "scheduled":
            return jsonify(status="scheduled",
                           message="This challenge has not started yet."), 409
        return jsonify(status="ended",
                       message="This challenge has ended."), 410
    if is_team_mode() and not g.user.is_admin and not g.user.team_id:
        return jsonify(status="team",
                       message="Join or create a team before submitting flags."), 400
    if not allow("flag:%d" % g.user.id, 10, 60):
        return jsonify(status="ratelimit",
                       message="Slow down. Max 10 attempts per minute."), 429
    data = request.get_json(silent=True) or {}
    submitted = str(data.get("flag", "")).strip()
    if not submitted:
        return jsonify(status="error", message="Enter a flag."), 400

    team = g.user.team if is_team_mode() and g.user.team_id else None
    if team:
        already = (db.session.query(Solve.id)
                   .join(User, User.id == Solve.user_id)
                   .filter(Solve.challenge_id == ch.id,
                           User.team_id == team.id,
                           User.is_admin.is_(False)).first())
    else:
        already = Solve.query.filter_by(user_id=g.user.id, challenge_id=ch.id).first()
    if already:
        message = ("Your team already solved this one." if team
                   else "You already solved this one.")
        return jsonify(status="already", message=message)

    if not hmac.compare_digest(submitted.encode(), ch.flag.encode()):
        return jsonify(status="wrong", message="Incorrect flag.")
    try:
        db.session.add(Solve(user_id=g.user.id, challenge_id=ch.id))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        message = ("Your team already solved this one." if team
                   else "You already solved this one.")
        return jsonify(status="already", message=message)
    message = "Correct! +%d points." % ch.points
    if g.user.is_admin:
        message = "Correct! Admin solves don't count on the scoreboard."
    elif team:
        message = "Correct! +%d points for %s." % (ch.points, team.name)
    payload = dict(status="correct", message=message, points=ch.points)
    payload.update(stone_payload(g.user))
    return jsonify(**payload)


@app.get("/challenges/<int:cid>/download")
@login_required
def challenge_download(cid):
    """Serve challenge attachments as a forced download under a safe name."""
    ch = db.session.get(Challenge, cid)
    if not ch or not ch.file_name or (not ch.visible and not g.user.is_admin):
        abort(404)
    if ch.state != "live" and not g.user.is_admin:
        abort(404)
    if is_team_mode() and not g.user.is_admin and not g.user.team_id:
        abort(404)
    stored = os.path.basename(ch.file_name)
    path = os.path.join(CHALLENGE_UPLOAD_DIR, stored)
    if not os.path.isfile(path):
        abort(404)
    return send_from_directory(
        CHALLENGE_UPLOAD_DIR, stored, as_attachment=True,
        download_name=ch.file_original or stored)


@app.route("/scoreboard")
def scoreboard():
    visible = get_setting("scoreboard_visible") == "1"
    if not visible and not (g.user and g.user.is_admin):
        abort(404)
    return render_template("scoreboard.html", rows=leaderboard_rows(g.user),
                           scoreboard_visible=visible)


# --------------------------------------------------------------------------
# Teams
# --------------------------------------------------------------------------
@app.get("/team")
@login_required
def team_home():
    members = []
    if g.user.team_id:
        members = (User.query.filter_by(team_id=g.user.team_id)
                   .order_by(User.created, User.id).all())
    teams = (db.session.query(Team, func.count(User.id))
             .outerjoin(User, User.team_id == Team.id)
             .group_by(Team.id)
             .order_by(Team.created).all())
    return render_template("team.html", team=g.user.team, members=members,
                           teams=teams)


@app.post("/team/create")
@login_required
def team_create():
    if not is_team_mode():
        flash("Team mode is off. Ask an admin to enable it first.", "error")
        return redirect(url_for("team_home"))
    if g.user.team_id:
        flash("Leave your current team before creating another.", "error")
        return redirect(url_for("team_home"))
    name = request.form.get("name", "").strip()
    if not TEAM_NAME_RE.match(name):
        flash("Team name must be 3–40 characters: letters, numbers, spaces, . _ or -.", "error")
    elif Team.query.filter(func.lower(Team.name) == name.lower()).first():
        flash("That team name is already taken.", "error")
    else:
        team = Team(name=name, invite_code=new_invite_code(), created_by=g.user.id)
        db.session.add(team)
        db.session.flush()
        g.user.team_id = team.id
        db.session.commit()
        flash("Team created. Share the invite code with your crew.", "ok")
        return redirect(url_for("team_home"))
    return redirect(url_for("team_home"))


@app.post("/team/join")
@login_required
def team_join():
    if not is_team_mode():
        flash("Team mode is off. Ask an admin to enable it first.", "error")
        return redirect(url_for("team_home"))
    if g.user.team_id:
        flash("Leave your current team before joining another.", "error")
        return redirect(url_for("team_home"))
    code = request.form.get("invite_code", "").strip().lower()
    team = Team.query.filter(func.lower(Team.invite_code) == code).first() if code else None
    if not team:
        flash("No team matches that invite code.", "error")
    elif team.member_count >= MAX_TEAM_SIZE:
        flash("That team is full (max %d members)." % MAX_TEAM_SIZE, "error")
    else:
        g.user.team_id = team.id
        db.session.commit()
        flash("Joined %s." % team.name, "ok")
        return redirect(url_for("team_home"))
    return redirect(url_for("team_home"))


@app.post("/team/leave")
@login_required
def team_leave():
    team = g.user.team
    if not team:
        flash("You are not on a team.", "error")
        return redirect(url_for("team_home"))
    name = team.name
    remove_user_from_team(g.user)
    db.session.commit()
    flash("You left %s." % name, "ok")
    return redirect(url_for("team_home"))


@app.post("/team/rename")
@login_required
def team_rename():
    team = g.user.team
    if not team or team.created_by != g.user.id:
        abort(403)
    name = request.form.get("name", "").strip()
    if not TEAM_NAME_RE.match(name):
        flash("Team name must be 3–40 characters: letters, numbers, spaces, . _ or -.", "error")
    elif Team.query.filter(func.lower(Team.name) == name.lower(),
                           Team.id != team.id).first():
        flash("That team name is already taken.", "error")
    else:
        team.name = name
        db.session.commit()
        flash("Team renamed.", "ok")
    return redirect(url_for("team_home"))


@app.post("/team/invite")
@login_required
def team_invite():
    team = g.user.team
    if not team or team.created_by != g.user.id:
        abort(403)
    team.invite_code = new_invite_code()
    db.session.commit()
    flash("Invite code regenerated. The old code no longer works.", "ok")
    return redirect(url_for("team_home"))


@app.post("/team/members/<int:uid>/remove")
@login_required
def team_remove_member(uid):
    team = g.user.team
    if not team or team.created_by != g.user.id:
        abort(403)
    member = db.session.get(User, uid) or abort(404)
    if member.team_id != team.id:
        abort(404)
    if member.id == g.user.id:
        flash("Use Leave team if you want to leave yourself.", "error")
        return redirect(url_for("team_home"))
    remove_user_from_team(member)
    db.session.commit()
    flash("%s was removed from the team." % member.username, "ok")
    return redirect(url_for("team_home"))


@app.post("/team/disband")
@login_required
def team_disband():
    team = g.user.team
    if not team or team.created_by != g.user.id:
        abort(403)
    name = team.name
    disband_team(team)
    db.session.commit()
    flash("%s was disbanded." % name, "ok")
    return redirect(url_for("team_home"))


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
@app.get("/notifications")
@login_required
def notifications():
    items = (Notification.query.filter_by(active=True)
             .order_by(Notification.created.desc()).all())
    read_ids = {row.notification_id for row in
                NotificationRead.query.filter_by(user_id=g.user.id).all()}
    unread_ids = {n.id for n in items if n.id not in read_ids}
    for nid in unread_ids:
        db.session.add(NotificationRead(user_id=g.user.id, notification_id=nid))
    if unread_ids:
        db.session.commit()
    return render_template("notifications.html", items=items, unread_ids=unread_ids)


# --------------------------------------------------------------------------
# Admin panel
# --------------------------------------------------------------------------
@app.route("/admin")
@admin_required
def admin_dashboard():
    at = now()
    stats = dict(
        users=User.query.filter_by(is_admin=False).count(),
        teams=Team.query.count(),
        challenges=Challenge.query.count(),
        visible=Challenge.query.filter_by(visible=True).count(),
        live=Challenge.query.filter(Challenge.visible.is_(True),
                                    challenge_live_clause(at)).count(),
        upcoming=Challenge.query.filter(Challenge.visible.is_(True),
                                        Challenge.start_at > at).count(),
        ended=Challenge.query.filter(Challenge.visible.is_(True),
                                      Challenge.end_at <= at).count(),
        files=Challenge.query.filter(Challenge.file_name.isnot(None)).count(),
        solves=Solve.query.count(),
        notifications=Notification.query.filter_by(active=True).count(),
    )
    recent = Solve.query.order_by(Solve.solved_at.desc()).limit(10).all()
    return render_template("admin/dashboard.html", stats=stats, recent=recent)


@app.route("/admin/challenges")
@admin_required
def admin_challenges():
    items = Challenge.query.order_by(Challenge.category, Challenge.points).all()
    counts = dict(db.session.query(Solve.challenge_id, func.count(Solve.id))
                  .group_by(Solve.challenge_id).all())
    return render_template("admin/challenges.html", items=items, counts=counts)


def fill_challenge(ch):
    """Validate the form and copy text fields onto ch. Returns an error or None."""
    f = request.form
    title = f.get("title", "").strip()
    category = f.get("category", "").strip() or "General"
    flag = f.get("flag", "").strip()
    url = f.get("url", "").strip()
    try:
        points = int(f.get("points", ""))
    except ValueError:
        return "Points must be a whole number."
    if not title or len(title) > 120:
        return "Title is required (max 120 characters)."
    if len(category) > 40:
        return "Category is too long (max 40 characters)."
    if not 1 <= points <= 10000:
        return "Points must be between 1 and 10000."
    if not flag or len(flag) > 255:
        return "Flag is required (max 255 characters)."
    if url and (urlparse(url).scheme not in ("http", "https") or len(url) > 500):
        return "The resource link must start with http:// or https://."
    hints = f.get("hints", "").strip()
    if len(hints) > 2000:
        return "Hints are too long (max 2000 characters)."
    try:
        start_at = parse_datetime_field(f.get("start_at"))
        end_at = parse_datetime_field(f.get("end_at"))
    except ValueError:
        return "Start and end times must be valid dates."
    if start_at and end_at and end_at <= start_at:
        return "The end time must be after the start time."
    ch.title, ch.category, ch.points, ch.flag = title, category, points, flag
    ch.description = f.get("description", "").strip()
    ch.hints = hints or None
    ch.url = url or None
    ch.start_at = start_at
    ch.end_at = end_at
    ch.visible = f.get("visible") == "1"
    return None


def remove_challenge_file(ch):
    """Delete an attachment from disk and clear it from the record."""
    if ch.file_name:
        path = os.path.join(CHALLENGE_UPLOAD_DIR, os.path.basename(ch.file_name))
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
    ch.file_name = ch.file_original = None
    ch.file_size = None


def save_challenge_file(ch):
    """Apply an uploaded attachment (or removal). Returns an error or None."""
    upload = request.files.get("attachment")
    if upload and upload.filename:
        data = upload.read()
        if not data:
            return "The selected file is empty. Choose another file."
        if len(data) > MAX_ATTACHMENT_BYTES:
            return "Attachment is too large (max 25 MB)."
        original = secure_filename(upload.filename) or "attachment"
        root, ext = os.path.splitext(original)
        ext = ext[:16]
        original = (root[:120] or "attachment") + ext
        stored = secrets.token_hex(16) + ext.lower()
        try:
            with open(os.path.join(CHALLENGE_UPLOAD_DIR, stored), "xb") as f:
                f.write(data)
        except FileExistsError:
            stored = secrets.token_hex(16) + ext.lower()
            with open(os.path.join(CHALLENGE_UPLOAD_DIR, stored), "xb") as f:
                f.write(data)
        except OSError:
            return "The file could not be saved. Try again."
        remove_challenge_file(ch)
        ch.file_name = stored
        ch.file_original = original
        ch.file_size = len(data)
    elif request.form.get("remove_file") == "1":
        remove_challenge_file(ch)
    return None


@app.route("/admin/challenges/new", methods=["GET", "POST"])
@admin_required
def admin_challenge_new():
    ch = Challenge(visible=True, points=100, flag="")
    if request.method == "POST":
        err = fill_challenge(ch)
        if not err:
            err = save_challenge_file(ch)
        if err:
            flash(err, "error")
        else:
            db.session.add(ch)
            db.session.commit()
            flash("Challenge created.", "ok")
            return redirect(url_for("admin_challenges"))
    return render_template("admin/challenge_form.html", ch=ch, is_new=True,
                           categories=categories())


@app.route("/admin/challenges/<int:cid>/edit", methods=["GET", "POST"])
@admin_required
def admin_challenge_edit(cid):
    ch = db.session.get(Challenge, cid) or abort(404)
    if request.method == "POST":
        err = fill_challenge(ch)
        if not err:
            err = save_challenge_file(ch)
        if err:
            flash(err, "error")
            db.session.rollback()
        else:
            db.session.commit()
            flash("Challenge saved.", "ok")
            return redirect(url_for("admin_challenges"))
    return render_template("admin/challenge_form.html", ch=ch, is_new=False,
                           categories=categories())


@app.post("/admin/challenges/<int:cid>/toggle")
@admin_required
def admin_challenge_toggle(cid):
    ch = db.session.get(Challenge, cid) or abort(404)
    ch.visible = not ch.visible
    db.session.commit()
    flash("'%s' is now %s." % (ch.title, "visible" if ch.visible else "hidden"), "ok")
    return redirect(url_for("admin_challenges"))


@app.post("/admin/challenges/<int:cid>/delete")
@admin_required
def admin_challenge_delete(cid):
    ch = db.session.get(Challenge, cid) or abort(404)
    remove_challenge_file(ch)
    Solve.query.filter_by(challenge_id=ch.id).delete()
    db.session.delete(ch)
    db.session.commit()
    flash("Challenge deleted.", "ok")
    return redirect(url_for("admin_challenges"))


def categories():
    """Stone categories first (kept in gauntlet order), then any others."""
    found = {r[0] for r in db.session.query(Challenge.category).distinct().all()}
    return STONE_CATS + sorted(found - set(STONE_CATS))


@app.route("/admin/users")
@admin_required
def admin_users():
    rows = (db.session.query(User, func.count(Solve.id))
            .outerjoin(Solve, Solve.user_id == User.id)
            .group_by(User.id).order_by(User.created).all())
    return render_template("admin/users.html", rows=rows)


@app.post("/admin/users/<int:uid>/role")
@admin_required
def admin_user_role(uid):
    u = db.session.get(User, uid) or abort(404)
    if u.id == g.user.id:
        flash("You can't change your own role.", "error")
    elif u.is_admin and User.query.filter_by(is_admin=True).count() <= 1:
        flash("Keep at least one admin account.", "error")
    else:
        u.is_admin = not u.is_admin
        db.session.commit()
        flash("%s is now %s." % (u.username, "an admin" if u.is_admin else "a player"), "ok")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<int:uid>/team")
@admin_required
def admin_user_remove_team(uid):
    u = db.session.get(User, uid) or abort(404)
    if not u.team_id:
        flash("%s is not on a team." % u.username, "error")
    else:
        name = u.team.name
        remove_user_from_team(u)
        db.session.commit()
        flash("%s was removed from %s." % (u.username, name), "ok")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<int:uid>/delete")
@admin_required
def admin_user_delete(uid):
    u = db.session.get(User, uid) or abort(404)
    if u.id == g.user.id:
        flash("You can't delete your own account.", "error")
    elif u.is_admin and User.query.filter_by(is_admin=True).count() <= 1:
        flash("Keep at least one admin account.", "error")
    else:
        if u.team_id:
            remove_user_from_team(u)
        Solve.query.filter_by(user_id=u.id).delete()
        NotificationRead.query.filter_by(user_id=u.id).delete()
        Notification.query.filter_by(created_by=u.id).update({Notification.created_by: None})
        db.session.delete(u)
        db.session.commit()
        flash("User deleted.", "ok")
    return redirect(url_for("admin_users"))


@app.post("/admin/mode")
@admin_required
def admin_mode():
    mode = request.form.get("mode", "")
    if mode not in ("user", "team"):
        abort(400)
    set_setting("platform_mode", mode)
    db.session.commit()
    flash("Platform switched to %s mode." % ("team" if mode == "team" else "user"), "ok")
    return redirect(url_for("admin_dashboard"))


# --------------------------------------------------------------------------
# Admin teams
# --------------------------------------------------------------------------
@app.get("/admin/teams")
@admin_required
def admin_teams():
    rows = (db.session.query(Team, func.count(User.id))
            .outerjoin(User, User.team_id == Team.id)
            .group_by(Team.id)
            .order_by(Team.name).all())
    scores = {team.id: team_score(team) for team, _ in rows}
    return render_template("admin/teams.html", rows=rows, scores=scores)


@app.post("/admin/teams/<int:tid>/delete")
@admin_required
def admin_team_delete(tid):
    team = db.session.get(Team, tid) or abort(404)
    name = team.name
    disband_team(team)
    db.session.commit()
    flash("%s was deleted. Its members can join another team." % name, "ok")
    return redirect(url_for("admin_teams"))


@app.post("/admin/teams/<int:tid>/members/<int:uid>/remove")
@admin_required
def admin_team_remove_member(tid, uid):
    team = db.session.get(Team, tid) or abort(404)
    member = db.session.get(User, uid) or abort(404)
    if member.team_id != team.id:
        abort(404)
    team_name = team.name
    username = member.username
    remove_user_from_team(member)
    db.session.commit()
    flash("%s was removed from %s." % (username, team_name), "ok")
    return redirect(url_for("admin_teams"))


# --------------------------------------------------------------------------
# Admin notifications
# --------------------------------------------------------------------------
@app.route("/admin/notifications", methods=["GET", "POST"])
@admin_required
def admin_notifications():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        message = request.form.get("message", "").strip()
        if not title or len(title) > 120:
            flash("Notification title is required (max 120 characters).", "error")
        elif not message or len(message) > 4000:
            flash("Notification message is required (max 4000 characters).", "error")
        else:
            db.session.add(Notification(title=title, message=message,
                                        created_by=g.user.id, active=True))
            db.session.commit()
            flash("Notification pushed to all players.", "ok")
            return redirect(url_for("admin_notifications"))
    items = Notification.query.order_by(Notification.created.desc()).all()
    read_counts = dict(db.session.query(NotificationRead.notification_id,
                                       func.count(NotificationRead.id))
                       .group_by(NotificationRead.notification_id).all())
    return render_template("admin/notifications.html", items=items,
                           read_counts=read_counts)


@app.post("/admin/notifications/<int:nid>/toggle")
@admin_required
def admin_notification_toggle(nid):
    item = db.session.get(Notification, nid) or abort(404)
    item.active = not item.active
    db.session.commit()
    flash("Notification is now %s." % ("active" if item.active else "inactive"), "ok")
    return redirect(url_for("admin_notifications"))


@app.post("/admin/notifications/<int:nid>/delete")
@admin_required
def admin_notification_delete(nid):
    item = db.session.get(Notification, nid) or abort(404)
    NotificationRead.query.filter_by(notification_id=item.id).delete()
    db.session.delete(item)
    db.session.commit()
    flash("Notification deleted.", "ok")
    return redirect(url_for("admin_notifications"))


# --------------------------------------------------------------------------
# Admin exports
# --------------------------------------------------------------------------
@app.get("/admin/export/solves.csv")
@admin_required
def admin_export_solves():
    rows = (db.session.query(Solve, User, Challenge)
            .join(User, User.id == Solve.user_id)
            .join(Challenge, Challenge.id == Solve.challenge_id)
            .order_by(Solve.solved_at).all())
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["solved_at_utc", "username", "team", "challenge",
                     "category", "points", "admin_solve"])
    for solve, user, challenge in rows:
        writer.writerow([
            solve.solved_at.strftime("%Y-%m-%d %H:%M:%S") if solve.solved_at else "",
            user.username,
            user.team.name if user.team else "",
            challenge.title,
            challenge.category,
            challenge.points,
            "yes" if user.is_admin else "no",
        ])
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=ctf-solves.csv"})


@app.get("/admin/export/users.csv")
@admin_required
def admin_export_users():
    rows = User.query.order_by(User.created).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["username", "email", "role", "team", "created_utc"])
    for user in rows:
        writer.writerow([
            user.username,
            user.email,
            "admin" if user.is_admin else "player",
            user.team.name if user.team else "",
            user.created.strftime("%Y-%m-%d %H:%M:%S") if user.created else "",
        ])
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=ctf-users.csv"})


def sniff_image(head):
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def remove_custom_bg():
    old = get_setting("bg_image")
    if old:
        path = os.path.join(UPLOAD_DIR, os.path.basename(old))
        if os.path.exists(path):
            os.remove(path)
    set_setting("bg_image", "")


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        name = request.form.get("platform_name", "").strip()
        tagline = request.form.get("tagline", "").strip()
        if not name or len(name) > 40 or len(tagline) > 140:
            flash("Name is required (max 40). Tagline max 140 characters.", "error")
            return redirect(url_for("admin_settings"))
        set_setting("platform_name", name)
        set_setting("tagline", tagline)
        set_setting("registration_open", "1" if request.form.get("registration_open") == "1" else "0")
        set_setting("scoreboard_visible", "1" if request.form.get("scoreboard_visible") == "1" else "0")
        mode = request.form.get("platform_mode", "user")
        set_setting("platform_mode", mode if mode in ("user", "team") else "user")
        theme = request.form.get("accent_theme", "arena")
        set_setting("accent_theme", theme if theme in THEMES else "arena")

        upload = request.files.get("bg_file")
        if request.form.get("reset_bg") == "1":
            remove_custom_bg()
        elif upload and upload.filename:
            data = upload.read()
            if len(data) > MAX_BG_BYTES:
                flash("Background must be 6 MB or smaller.", "error")
                db.session.rollback()
                return redirect(url_for("admin_settings"))
            ext = sniff_image(data[:16])
            if not ext:
                flash("Background must be a PNG, JPG or WebP image.", "error")
                db.session.rollback()
                return redirect(url_for("admin_settings"))
            remove_custom_bg()
            fname = "bg_%s.%s" % (secrets.token_hex(6), ext)
            with open(os.path.join(UPLOAD_DIR, fname), "wb") as f:
                f.write(data)
            set_setting("bg_image", fname)
        db.session.commit()
        flash("Settings saved.", "ok")
        return redirect(url_for("admin_settings"))
    return render_template("admin/settings.html",
                           has_custom_bg=bool(get_setting("bg_image")),
                           themes=THEMES,
                           accent_theme=get_theme())


# --------------------------------------------------------------------------
# Stone Hunt seed content
# --------------------------------------------------------------------------
# (category, title, points, description, hint, flag)
SEED_CHALLENGES = [
    ("Power Stone", "Load the Core", 200,
     "The Power Stone answers only to a host that can take the surge. Warm-up target while the real vault is wired up.",
     "Sample target — submit flag{power_load_the_core}.", "flag{power_load_the_core}"),
    ("Power Stone", "Overcharge", 300,
     "Push the reactor past its rated ceiling and the readout flips to something worth submitting.",
     "Sample target — submit flag{power_overcharge}.", "flag{power_overcharge}"),

    ("Space Stone", "Portal Coordinates", 200,
     "Six jump coordinates, one doorway. Plot them in order and the gate hands back a destination tag.",
     "Sample target — submit flag{space_portal_coordinates}.", "flag{space_portal_coordinates}"),
    ("Space Stone", "Jump Point", 300,
     "Dial the jump point and the gate logs where it thinks you are heading.",
     "Sample target — submit flag{space_jump_point}.", "flag{space_jump_point}"),

    ("Reality Stone", "Wrong Reflection", 200,
     "The mirror replays a moment that never happened. Name what changed and the illusion folds.",
     "Sample target — submit flag{reality_wrong_reflection}.", "flag{reality_wrong_reflection}"),
    ("Reality Stone", "Shape Shifter", 300,
     "Reality is only a suggestion here: the sample file keeps rewriting its own bytes.",
     "Sample target — submit flag{reality_shape_shifter}.", "flag{reality_shape_shifter}"),

    ("Soul Stone", "Price of Admission", 200,
     "The gate asks for something you did not plan to give up, and keeps a receipt.",
     "Sample target — submit flag{soul_price_of_admission}.", "flag{soul_price_of_admission}"),
    ("Soul Stone", "Lost and Found", 300,
     "A name that was erased is still cached in the ledger, one row above the void.",
     "Sample target — submit flag{soul_lost_and_found}.", "flag{soul_lost_and_found}"),
    ("Soul Stone", "Keeper's Toll", 400,
     "The keeper of the valley charges every soul that passes through, and never forgets a balance.",
     "Sample target — submit flag{soul_keepers_toll}.", "flag{soul_keepers_toll}"),

    ("Mind Stone", "Logic Loop", 200,
     "The construct traps anyone who answers too quickly. Read it twice before you reply.",
     "Sample target — submit flag{mind_logic_loop}.", "flag{mind_logic_loop}"),
    ("Mind Stone", "Purple Puzzle", 300,
     "A cipher that only makes sense once you stop looking at it directly.",
     "Sample target — submit flag{mind_purple_puzzle}.", "flag{mind_purple_puzzle}"),

    ("Time Stone", "Loop de Loop", 200,
     "The target resets every time you look away. Catch it mid-loop.",
     "Sample target — submit flag{time_loop_de_loop}.", "flag{time_loop_de_loop}"),
    ("Time Stone", "Rewind", 300,
     "The log runs backwards. Read it that way and it reads like an instruction.",
     "Sample target — submit flag{time_rewind}.", "flag{time_rewind}"),
    ("Time Stone", "Temporal Buffer", 400,
     "A frame from ten seconds in the future was written early, and nobody noticed.",
     "Sample target — submit flag{time_temporal_buffer}.", "flag{time_temporal_buffer}"),
]

# Original challenges are folded into stones so their solves keep counting.
SEED_LEGACY = {"weee": "Power Stone", "main": "Space Stone",
               "eves me": "Reality Stone", "nihal;": "Mind Stone"}
SEED_PER_STONE = 3


def seed_stone_challenges():
    """Give every stone at least three targets. Idempotent; never touches solves."""
    if Challenge.query.filter(Challenge.category.in_(STONE_CATS)).count():
        return
    for title, cat in SEED_LEGACY.items():
        ch = Challenge.query.filter_by(title=title).first()
        if ch and ch.category not in STONE_BY_CAT:
            ch.category = cat
    added = 0
    for cat, title, points, desc, hint, flag in SEED_CHALLENGES:
        if Challenge.query.filter_by(title=title).first():
            continue
        if Challenge.query.filter_by(category=cat).count() >= SEED_PER_STONE:
            continue
        db.session.add(Challenge(title=title, category=cat, description=desc,
                                 hints=hint, points=points, flag=flag,
                                 visible=True))
        added += 1
    db.session.commit()
    if added:
        print("\n  Stone Hunt seeded: %d sample challenge(s)" % added)
        print("  6 stone categories x %d+ challenges ready\n" % SEED_PER_STONE)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def http_error(e):
    msgs = {403: "You don't have access to this page.",
            404: "That page doesn't exist.",
            413: "That upload is too large. Challenge files can be up to 25 MB."}
    msg = msgs.get(e.code) or getattr(e, "description", "Bad request.")
    return render_template("error.html", code=e.code, message=msg), e.code


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------
def ensure_schema():
    """Add columns introduced after the first release (SQLite-friendly)."""
    insp = sa_inspect(db.engine)
    tables = insp.get_table_names()
    changed = False

    if "challenge" in tables:
        cols = {c["name"] for c in insp.get_columns("challenge")}
        additions = {
            "hints": "TEXT",
            "file_name": "VARCHAR(255)",
            "file_original": "VARCHAR(160)",
            "file_size": "INTEGER",
            "start_at": "DATETIME",
            "end_at": "DATETIME",
        }
        for name, ddl in additions.items():
            if name not in cols:
                db.session.execute(text("ALTER TABLE challenge ADD COLUMN %s %s" % (name, ddl)))
                changed = True

    if "user" in tables:
        user_cols = {c["name"] for c in insp.get_columns("user")}
        if "team_id" not in user_cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN team_id INTEGER"))
            changed = True

    if changed:
        db.session.commit()


def bootstrap():
    db.create_all()
    ensure_schema()
    seed_stone_challenges()
    if not User.query.filter_by(is_admin=True).first():
        username = os.environ.get("ADMIN_USER", "admin")
        password = os.environ.get("ADMIN_PASS") or secrets.token_urlsafe(12)
        email = os.environ.get("ADMIN_EMAIL", "admin@localhost.local")
        user = User.query.filter(func.lower(User.username) == username.lower()).first()
        if not user:
            user = User(username=username, email=email,
                        password_hash=generate_password_hash(password))
            db.session.add(user)
        user.is_admin = True
        db.session.commit()
        print("\n  First run: admin account created")
        print("  username: %s\n  password: %s\n  (change ADMIN_USER / ADMIN_PASS env vars to choose your own)\n"
              % (username, password))


with app.app_context():
    bootstrap()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", 4002)), debug=False)

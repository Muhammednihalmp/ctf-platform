# CTF Platform (Flask)

A small CTFd-style platform: user registration/login, user or team scoring, scheduled challenges, broadcast notifications, a scoreboard, and an admin panel.

## Tech stack

- **Language:** Python 3
- **Web framework:** Flask 3.x
- **ORM / database layer:** Flask-SQLAlchemy 3.1+ on SQLAlchemy
- **Database:** SQLite (`instance/ctf.db`)
- **Templating:** Jinja2 (server-rendered pages)
- **Frontend:** hand-written CSS and vanilla JavaScript (`static/style.css`, `static/app.js`); self-hosted IBM Plex Mono and Instrument Sans fonts; no build step or Node toolchain
- **Auth:** Werkzeug password hashing, Flask signed-cookie sessions, per-session CSRF tokens, and in-memory rate limiting
- **File storage:** local disk under `static/uploads/` (custom backgrounds) and `static/uploads/challenges/` (challenge attachments)

## Project structure

```text
app.py                          Flask app, models, routes, bootstrap/seed logic
requirements.txt                Python dependencies
templates/                      Jinja2 templates (public, auth, admin)
static/style.css                All styles and theme variables
static/app.js                   Client-side interactions
static/fonts/                   Self-hosted webfonts
static/img/banner.png           Default background banner
static/uploads/                 Custom backgrounds uploaded in admin settings
static/uploads/challenges/      Challenge attachments (randomised filenames)
instance/ctf.db                 SQLite database (created on first run)
instance/secret.key             Generated Flask secret key (if SECRET_KEY is unset)
```

## Run

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:4001 (the port can be changed with `PORT`).

On first run an admin account is created and its password is printed in the terminal.
Pick your own with environment variables:

```bash
ADMIN_USER=nihal ADMIN_PASS='a-strong-password' python app.py
```

## User mode and team mode

The platform supports two scoring modes, switchable by an admin from **Platform settings** or the dashboard quick switch:

- **User mode (default)** — every player has an individual score and appears on the player scoreboard.
- **Team mode** — players create or join a team from `/team` using an invite code. Each challenge counts once per team, no matter how many members solve it, and the scoreboard ranks teams by combined points. Players must be on a team to submit flags. Admin solves never count toward team scores.

Team details:

- Team names are unique and limited to 40 characters; each team holds up to 8 members.
- The captain can rename the team, regenerate the invite code, remove members, or disband the team. Members can leave at any time; captaincy transfers automatically if the captain leaves.
- Switching back to user mode keeps teams, membership and solves intact. Scores revert to the individual view without deleting anything.
- Admins can manage teams from **Admin → Teams** and remove a player from a team from **Admin → Users**.

## Notifications

Admins can broadcast messages to every registered player from **Admin → Notifications**.

- Each notification has a title, message, active/inactive state and author.
- Players see an **Alerts** link in the navigation with an unread count. Opening `/notifications` marks the active messages as read.
- Admins can see how many players have read each notification, deactivate a message without deleting it, or delete it for everyone.

## Challenge scheduling

Each challenge can have an optional **starts at** and **ends at** time (UTC), configured in the admin challenge form.

- Before the start time the challenge card is locked and its description, hints, links and files stay hidden. The card shows a live countdown.
- After the end time submissions and attachment downloads close, and the card is marked ended.
- Challenges with no schedule stay open permanently. Admins can always preview and submit, even outside the window.
- Stone progress includes challenges that have started; scheduled challenges do not count toward stone totals until they open.

## Admin panel (`/admin`)

- **Challenges**: create, edit, hide/show, delete, and set a **start/end schedule**. Set title, category, points, description, **hints (one per line)**, flag, an optional resource link **and an optional attachment file (up to 25 MB)**. Players reveal hints one at a time in the challenge dialog; cards show how many hints are available. Uploading a new file replaces the old one; files are stored with randomised names under `static/uploads/challenges/` and served only through a login-protected, forced-download route.
- **Users**: promote/demote admins, remove players from teams, delete users. The last remaining admin cannot be demoted or deleted.
- **Teams**: view every team, its captain, members and team score; remove members or delete a team.
- **Notifications**: push a broadcast, view read counts, deactivate or delete past messages.
- **Exports**: download solve history and user lists as CSV from the dashboard.
- **Platform settings**: platform name, tagline, open/close registration, **user/team scoring mode**, scoreboard visibility, accent theme, and a custom background image (default is the Offenso banner in `static/img/banner.png`).
  - **Scoreboard visibility**: when hidden, players no longer see the scoreboard link and `/scoreboard` returns 404. Admins still get a preview with a notice, and can re-enable it from settings.
  - **Accent themes**: pick **Arena green** (default dark arena look), **Neon violet**, **Ember**, or **Frost**. The theme recolours buttons, highlights, score cells, navigation, dialogs and the page glow.

## Interface

The home page includes a live terminal panel, mission steps, category tracks and arena totals. Login and registration use a split auth layout with an operator terminal, and the scoreboard has a top-three podium above the full standings table. Team pages, alerts, scheduled challenge countdowns and admin tables are responsive and degrade to a single column on mobile.

## Database

The app uses a single SQLite file at `instance/ctf.db`. It is created automatically on first run by `db.create_all()`, and `ensure_schema()` adds newer columns to existing databases (hints, attachments and challenge scheduling, plus `user.team_id`). There is no migration framework (no Alembic), so back up the database before changing models or schema.

### Tables

| Table | Purpose | Columns |
| --- | --- | --- |
| `user` | Player and admin accounts | `id` (PK), `username` (unique, max 24), `email` (unique, max 120), `password_hash`, `is_admin`, `team_id` (FK `team.id`), `created` |
| `team` | Team mode crews | `id` (PK), `name` (unique, max 40), `invite_code` (unique), `created_by` (FK `user.id`, the captain), `created` |
| `challenge` | Challenge definitions, schedule and attachment metadata | `id` (PK), `title`, `category`, `description`, `points`, `flag`, `hints`, `url`, `file_name`, `file_original`, `file_size`, `visible`, `start_at`, `end_at`, `created` |
| `solve` | One row per player/challenge solve | `id` (PK), `user_id` (FK `user.id`), `challenge_id` (FK `challenge.id`), `solved_at`; unique on (`user_id`, `challenge_id`) |
| `notification` | Admin broadcast messages | `id` (PK), `title`, `message`, `active`, `created_by` (FK `user.id`), `created` |
| `notification_read` | Per-user read receipts | `id` (PK), `user_id` (FK `user.id`), `notification_id` (FK `notification.id`), `read_at`; unique on (`user_id`, `notification_id`) |
| `setting` | Key/value platform configuration | `key` (PK), `value` |

### Relationships and data notes

- `solve` links users to challenges; a player can solve each challenge once (enforced by the unique constraint). Deleting a user or challenge removes its solves in application code.
- In team mode, a solve still belongs to the player who submitted it. Team scores aggregate distinct solved challenges across current members, so a challenge only scores once per team; admin solves are excluded.
- Challenge `flag` and `hints` are stored in plain text so admins can edit them. Keep `instance/` private.
- `challenge.start_at` and `challenge.end_at` are UTC datetimes; `NULL` means no bound. Scheduling only controls player access — existing solves remain on the scoreboard.
- Attachments are stored on disk under randomised names; `challenge.file_name` points to the stored file, `file_original` is the download name, and `file_size` is in bytes.
- The `setting` table stores platform name, tagline, registration/scoreboard visibility, accent theme, custom background filename and `platform_mode` (`user` or `team`). Missing keys fall back to the defaults in `app.py`.
- The scoreboard is calculated live: users from `solve` joined to `challenge`, or teams from distinct team/challenge pairs. Nothing is cached.
- Existing databases are upgraded on startup: new tables are created and `ensure_schema()` adds the `team_id`, `start_at` and `end_at` columns. Back up `instance/ctf.db` before upgrading.

## Notes

- Host on your network: `HOST=0.0.0.0 PORT=8000 python app.py`. For real events put it behind gunicorn + HTTPS (nginx/Caddy) and set `HTTPS=1` and `SECRET_KEY`.
- Back up `instance/ctf.db` and the `static/uploads/` folder before an event.
- Flag submissions are limited to 10 per minute per user; login to 10 attempts per 5 minutes per IP (in memory, per process).

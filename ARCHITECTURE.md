# Job Hunter — Architecture

> A Telegram bot (**@openings_radar_bot**) that watches LinkedIn for new jobs and sends each user the
> ones that fit **their** resume and filters, with a match %.
> Visual version: [`docs/architecture.html`](docs/architecture.html) (open it in a browser).

---

## 1. What it does

1. A user opens the bot and sends `/start`. Depending on `ACCESS_MODE`, they're either let in straight away or wait for an admin to approve them.
2. They send their **resume** (PDF / DOCX / TXT). The bot pulls out their skills, years of experience and job titles, and suggests search keywords.
3. They set **filters** from the ⚙️ menu: keywords, locations, date posted, experience level, job type, on-site/remote/hybrid, Easy Apply, **match threshold**, max years a job may ask for, title words / companies to skip, max applicants, and quiet hours.
4. Every `SCAN_EVERY_MINUTES` (default **3**), the bot searches LinkedIn for every user's keywords × locations, newest first. It scores each new job against that user's resume and sends the ones at or above their threshold (default 0 = everything).
5. Each alert shows the match %, the skills they have, the skills the job also wants, and buttons: 🔗/⚡ Open · ✅ Applied · ⭐ Save · 🙈 Hide · 🤝 Referral message.

---

## 2. Components

```
                ┌──────────────── Docker container "job-hunter" (restart: unless-stopped) ────────────────┐
 Telegram  ◄──► │  app/bot.py        handlers, menus, approval flow, Clear chat, background jobs          │
 users          │     │  app/ui.py   texts + keyboards (pure, testable)                                     │
                │     ▼                                                                                   │
                │  app/scanner.py    the scan cycle: plan searches → fetch → match → queue                │
                │     │        │                                                                          │
                │     ▼        ▼                                                                          │
 LinkedIn  ◄─── │  app/linkedin.py   public jobs-guest endpoints, polite delays, 429 back-off             │
 (public,       │  app/matcher.py    hard filters + score 0-100                                           │
  no login)     │  app/resume.py     PDF/DOCX/TXT → text → skills, years, titles, keyword ideas           │
                │  app/skills.py     shared skill taxonomy (~110 skills with aliases)                     │
                │  app/filters.py    per-user Filters + LinkedIn Query (URL params, shared key)           │
                │  app/db.py         SQLite (WAL)          app/storage.py  per-user resume folders        │
                │  app/admin.py      CLI used by the `jobhunter` command                                  │
                └───────────────────────────── ./data (volume) ───────────────────────────────────────────┘
                                                   jobhunter.db · users/<id>/ · logs/ · heartbeat
```

| File | Responsibility |
|---|---|
| `main.py` | Logging (stdout + `data/logs/bot.log`, rotated **hourly**, kept **12 h**), DB init, starts polling |
| `app/config.py` | Server-wide settings from `.env` (token, admins, access mode, scan interval, delays) |
| `app/bot.py` | All Telegram handlers, access control, filter menu, resume upload, delivery, Clear chat, jobs |
| `app/ui.py` | Every text and keyboard the user sees (no I/O → unit-tested directly) |
| `app/scanner.py` | One scan cycle; cooldown after rate limits; re-scoring after filter/resume changes |
| `app/linkedin.py` | HTTP client + HTML parsers for search results and job details |
| `app/matcher.py` | `evaluate()` → `Match(ok, score, reason, matched, missing, years)` |
| `app/resume.py` | Text extraction, experience years, titles, keyword suggestions |
| `app/skills.py` | Skill taxonomy used by **both** resume parsing and job matching |
| `app/filters.py` | `Filters` dataclass (stored as JSON per user), `Query` (one LinkedIn search), parsers |
| `app/db.py` | SQLite schema + queries |
| `app/storage.py` | `data/users/<telegram_id>/` folders |
| `app/referral.py` | Job-specific referral request messages (template-based) + LinkedIn people-search links |
| `app/admin.py` | `python -m app.admin users / approve / block / make-admin / stats / reset-cooldown` |

---

## 3. LinkedIn access

* It uses LinkedIn's **public, logged-out** endpoints, the same ones linkedin.com/jobs uses for visitors who aren't signed in:
  * search: `https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=…&location=…&f_TPR=r86400&sortBy=DD&start=0` (10 jobs per page)
  * detail: `https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/<id>`
* **No account or cookies are used, so no account can be blocked.**
* Filter codes are LinkedIn's own:

| Filter | Param | Values |
|---|---|---|
| Date posted | `f_TPR` | **`live` (default) → searches `r3600`, and the bot keeps only jobs ≤ `LIVE_MAX_AGE_MINUTES` (30) old** · `r3600` 1 h · `r21600` 6 h · `r86400` 24 h · `r604800` week · `r2592000` month |
| Experience level | `f_E` | 1 Internship · 2 Entry · 3 Associate · 4 Mid-Senior · 5 Director · 6 Executive |
| Job type | `f_JT` | F Full-time · P Part-time · C Contract · T Temporary · I Internship · V Volunteer · O Other |
| Work type | `f_WT` | 1 On-site · 2 Remote · 3 Hybrid |
| Easy Apply | `f_AL` | `true` (**on by default**) |
| Sort | `sortBy` | `DD` (newest first, always) |

* **Being polite** (LinkedIn returns HTTP 429 to clients that are too fast):
  * a random 2–5 s pause before every request, with rotating browser user agents;
  * on 429, it waits 30 s and then 60 s (plus jitter) before retrying; after the third 429 it stops the cycle;
  * the whole scanner then **cools down** for 10 → 20 → 40 → 60 min, and resets after a clean cycle;
  * `SCAN_EVERY_MINUTES` can't go below 2. **"Every second" isn't possible:** LinkedIn would block the server's IP within minutes. Every 5 min, newest first, gets alerts out a few minutes after posting.

---

## 4. The scan cycle (`app/scanner.py`)

```
every SCAN_EVERY_MINUTES ──► plan ──► search ──► queue per user ──► details ──► match ──► deliver
```

1. **Plan.** For each active, unpaused user, `queries_for(filters)` gives one `Query` per keyword × location. Queries with the same `key` (a hash of keyword, location and LinkedIn filters, case-insensitive) are **merged**, so 10 users searching "AI Engineer / India / 24 h" cost one request.
   * If a user joins a search that has already run for others, the jobs that search found in its date window are back-filled to them.
2. **Search.** Least-recently-run searches go first. A page is fetched (newest first), and job IDs the search hasn't seen before (`query_seen`) are "new". Paging stops at the first page with an already-seen job, or a short last page. A brand-new search reads up to `PAGES_FIRST_SCAN` pages (30 jobs).
3. **Queue.** Every new job ID becomes a `deliveries` row with status `waiting` for every user of that search.
4. **Details.**
   * For each `waiting` row, excluded title words are checked first (no request wasted).
   * Otherwise the full description is fetched **once** and cached in `jobs`, so other users reuse it.
   * A cycle fetches at most 60 descriptions; the rest stay `waiting` for the next cycle.
   * After 3 failed fetches, the job is scored on its title only.
5. **Match.** A duplicate check runs first (same title + company sent in the last 7 days, i.e. reposted in another city). Then `matcher.evaluate()` runs, and the row becomes `pending` (with score) or `rejected` (with a reason code).
6. **Deliver.** Every 60 s (also *while* a scan is still running, with its own lock so nothing is sent twice) and right after a cycle, the bot sends `pending` rows best score first, up to `MAX_PER_CYCLE` per user. It skips paused users and users in their quiet hours.
   * If a user has blocked the bot (Forbidden), they're auto-paused.

If a user changes their filters or resume, `reevaluate_user()` re-scores their recent `pending` and `rejected` rows **from the cache, with no new requests**. For example, lowering the threshold sends the newly qualifying jobs right away.

---

### 📍 Location: Delhi NCR (default) and location names
* LinkedIn geocodes free text, and sometimes gets it wrong: **"Delhi NCR" returns jobs in Lansing, Michigan (USA)**. "New Delhi, Delhi, India" returns Delhi, Gurugram, Noida and Gurgaon.
* `filters.DEFAULT_LOCATION = "New Delhi, Delhi, India"`, shown to users as "Delhi NCR (Delhi, Gurugram, Noida)".
* `normalize_location()` turns what users type (Delhi NCR, NCR, Gurgaon, Bangalore, Mumbai…) into LinkedIn's exact strings via `LOCATION_ALIASES`. Unknown text is passed through unchanged. **When you add a city, add it to the aliases and test it.**
* A one-time migration (`migrate_location_default`) moved users from the old "India" default to Delhi NCR.

### ⚡ Easy Apply only (default)
* `Filters.easy_apply` defaults to **True**, so searches send `f_AL=true`.
* The job page is also checked: `linkedin.detect_easy_apply()` looks for the `public_jobs_apply-link-simple` marker (Easy Apply) or `…-offsite` (company site), stored as `jobs.easy_apply`.
* Alerts show **⚡ Easy Apply** with an "⚡ Easy Apply on LinkedIn" button, or **🌐 Applies on the company's website**.
* Users turn it off in ⚙️ Filters → ⚡ Easy Apply only.
* A one-time migration (`migrate_easy_apply_default`) turned it on for existing users.

### ⚡ Just posted (live) — the default
The bot's main job is to send jobs **the moment they're posted**:

* **Date posted defaults to `live`.** LinkedIn is searched for the past hour, newest first, every 3 minutes, and only jobs whose LinkedIn age is ≤ 30 min are sent. In practice they arrive minutes after posting, limited by LinkedIn's indexing and the 3-minute interval.
* **A brand-new search doesn't flood you with old jobs.** Anything older than 30 min is rejected (`old`) without fetching its description.
* **Users can widen it** in ⚙️ Filters → 🕒 Date posted (past hour / 6 h / 24 h / week / month), and then the age cut-off is off.
* **One-time migration on start** (`migrate_live_default`): users still on the old 24 h default are moved to live, and their queued older jobs are dropped.
* **Quiet at night** is expected, because few jobs are posted then.

## 5. Matching & score (`app/matcher.py`)

**Hard filters**: a job is skipped, with the reason stored, if any of these apply:

| Code | Rule |
|---|---|
| `not_easy_apply` | Easy Apply only is on (the default) but the job page's apply button goes to the company site (`public_jobs_apply-link-offsite`) |
| `old` | ⚡ live mode (the default): the job was posted more than `LIVE_MAX_AGE_MINUTES` ago, judged from LinkedIn's "x minutes ago" and stored as `jobs.posted_at`. This is checked **before** the description is fetched. Jobs of unknown age are kept |
| `title` | title contains an excluded word (whole word; default: senior, sr, lead, principal, staff, architect, manager, director, head, vp) |
| `company` | company matches an excluded company |
| `applicants` | more applicants than the user's max |
| `years` | the minimum years the job asks for > allowed (auto = your years + 2, or a fixed number, or "any") |
| `fresher` | the job's upper bound + 1 < your years (e.g. "0-1 years" when you have 3) |
| `unrelated` | the title shares nothing with your keywords **and** the description covers < 40% skills |
| `duplicate` | same title + company already sent in the last 7 days |
| `threshold` | score < your match threshold |

**Score (0–100)**

```
score = 65 × skill coverage + 20 × title relevance + 15 × experience fit
```
* **Skill coverage**: the taxonomy skills found in the job title and description. A skill named in the title, or mentioned twice, counts ×2. Coverage = the weight of those skills that are on your resume ÷ the total weight.
  * Short descriptions (total weight < 4) are pulled toward 40% so that one keyword can't look like a perfect match.
  * A description that names no tech skills gets 25%.
* **Title relevance**: the best overlap between the title and any of your keywords, after normalising (AI = Artificial Intelligence, GenAI = Generative AI, back-end = backend, node.js = node, SDE = software…). Generic words like engineer/developer are ignored. Implications count, e.g. LLM → AI.
* **Experience fit**: 1.0 if the job asks for ≤ your years, otherwise −0.3 per extra year.
* **No resume yet**: no score. Jobs are still sent if their title fits your keywords.

Required years come from the description. It recognises "2-4 years", "3+ yrs", "minimum 5 years", "two years of experience", and takes the **largest minimum** ("5+ years overall, 2 years of Kafka" → 5+).

---

## 6. Resume handling & the per-user folder

**Parsing** (`app/resume.py`, all local, nothing is sent anywhere):
* **Text**: `pypdf` (first 10 pages), `python-docx` (paragraphs + tables), or UTF-8 text. Scanned or empty files are rejected with a hint.
* **Skills**: `skills.find_skills()`, the same taxonomy the matcher uses. Ordered by number of mentions.
* **Experience years**:
  * Date ranges such as `Jan 2024 – Present`, `03/2021 - 02/2023` and `Feb '24 – Nov 2024` are collected. Lines that look like education (university, B.Tech, CGPA…) are skipped.
  * Overlapping ranges are merged, so two jobs at once aren't double-counted, and the months are summed.
  * Fallback: "X+ years of experience". The user can override it with `/years`.
* **Keyword suggestions**: from skill categories (3+ AI skills → AI Engineer, Generative AI Engineer, LLM Engineer; backend → Backend Developer, Software Engineer; Node.js → Node.js Developer…) plus the job titles found in the resume, up to 6. They're applied automatically on the first upload if the user has no keywords yet.

**Storage** (`app/storage.py`):
```
data/users/<telegram_user_id>/      0700
    resume.pdf | resume.docx | resume.txt   the file the user sent (only the latest; a new upload replaces it)
    resume.txt.extracted                    the extracted plain text
    profile.json                            the parsed profile (skills, years, titles, keyword ideas)
```
* The folder name is the **numeric Telegram user ID**. Telegram authenticates it and it never changes, so a handler can only ever reach the sender's own folder. `user_dir()` refuses anything that isn't an int, so no path tricks are possible.
* Files are written atomically (temp file + rename) with mode 0600.
* The parsed profile is also stored in `users.resume` (JSON) so scans don't read files.
* 🗑 Delete my resume (button) removes the folder. `/deleteme confirm` removes the folder, the DB rows and the chat messages.

---

## 7. Data model (`data/jobhunter.db`, SQLite WAL)

| Table | Key | What |
|---|---|---|
| `users` | `id` (Telegram user ID) | chat_id, name, `status` pending/active/blocked, `is_admin`, `filters` JSON, `resume` JSON, `paused`, `last_manual_scan` |
| `jobs` | `id` (LinkedIn job ID) | title, company, location, url, posted, `posted_at` (UTC, from "x min ago"), description, criteria, applicants, seniority, `detail_status` none/ok/failed |
| `query_seen` | (`qkey`, `job_id`) | which jobs each search has returned (for paging) |
| `query_state` | `qkey` | last run, run count |
| `user_queries` | (`user_id`, `qkey`) | searches a user is attached to (for back-fill) |
| `deliveries` | (`user_id`, `job_id`) | `status` waiting → pending → sent → applied/saved/hidden, or rejected · `score`, `reason`, `reason_code`, `details` JSON (match breakdown), `message_id` |
| `messages` | (`chat_id`, `message_id`) | every message in each chat with `kind` ui/job/user, for Clear chat |
| `meta` | `key` | cooldown, last cycle summary, `alert_open` (watchdog) |
| `cycles` | — | one row per scheduled scan: ts, searches, listings, new_jobs, matched, error (kept 2 days) |

**Pruning** (every 12 h): deliveries older than 30 days are removed, except applied and saved ones. So are unreferenced jobs older than 30 days, search history older than 35 days, and message IDs older than 3 days.

---

## 8. Telegram UX

**Main menu** (persistent keyboard): 🟢 Is it working? · 🔍 Find jobs now · ⚙️ Filters · 📄 My resume · 📊 Status · ⭐ Saved · 💡 Insights · ⏯ Pause / Resume · 🧹 Clear chat · ❓ Help

**Commands**: `/start /intro (/tour /guide) /help /scan /filters /resume (/cv) /titles /addtitle … /keywords … /location … /threshold N /years N /saved (/applied) /insights /status /pause /clear /deleteme`. Admin only: `/users /approve <id> /block <id>`.

* **Job titles menu** (⚙️ Filters → 🔎 Job titles / keywords, or `/titles`): it lists the current titles with ❌ to remove
  each one, ➕ Add titles (appends, case-insensitive dedupe), ✏️ Replace all, and 📄 Add resume suggestions. Up to
  `MAX_KEYWORDS` = 10 titles. `/addtitle A, B` adds and `/keywords A, B` replaces.
* **Filters menu** (inline):
  * multi-select toggles for experience level, job type and work type;
  * single-select for date posted;
  * presets plus a custom value for the threshold and max years;
  * free-text prompts for keywords, locations, excluded words and companies, max applicants, quiet hours and your years.
* **Network blips** (Telegram unreachable for a moment) are logged as one WARNING line, not a traceback.
* **Access**: `ACCESS_MODE=approval` (default). A new user is `pending`, and admins get ✅ Approve / 🚫 Block buttons; the user is told when they're approved. The first user ever becomes admin when `ADMIN_IDS` is empty. `open` mode lets anyone in, up to `MAX_USERS`.
* **Clear chat**: every message the bot sends, and every message the user sends, is recorded in `messages`. 🧹 offers *Clear everything* or *Clear but keep job alerts*. It deletes in batches of 100 with `deleteMessages`, then sends a fresh menu. Telegram only lets bots delete messages from the **last 48 h**; older ones stay, and the user can use Telegram's own "Clear history" for those.
* **Guided tour** (`/intro`, also offered as a 📖 Take the tour button after /start, approval and /help):
  7 pages with ◀️ / ▶️ buttons: what the bot is · sending your resume · filters · how the match % works ·
  job alerts & buttons · every menu button · privacy & tips. Pages live in `ui.INTRO_PAGES`. It works even
  for users still waiting for approval. A test checks every feature and menu button is explained, so **when you add
  a feature, add it to the tour**.
* **🤝 Referral message** (a button on every alert, `ref:<job_id>`). `referral.build()` fills templates with the job (title, cleaned company name, job ID, link, the job's skills that the user has) and the resume (name from the first lines, current role, years). It produces:
  * a **connection note**: the richest variant that fits LinkedIn's 200-character limit;
  * a **full message**, which doubles as a DM or email, plus an email subject;
  * a **follow-up**;
  * buttons to open LinkedIn people search for employees and for recruiters at the company.
  Messages come in `<pre>` blocks, so they're tap-to-copy. It's template-based: no AI, no cost, nothing leaves the server. A user can only generate messages for their own jobs.
* **🟢 Is it working?** (`/alive`, `/working`, `/check`, or the top menu button). `ui.health_verdict()` decides the state from the last `cycles` row:
  * 🟢 hunting: the last scan was ≤ 3 × interval ago;
  * 🟡 slowed down: rate-limit cooldown, or the last scan errored;
  * 🔴 stopped: no scan for > 3 × interval;
  * "just restarted": no scan yet and the bot started recently.
  It also shows the last and next scan, today's numbers for the user (checked / matched / sent / best %, via `db.user_window`), what's being searched, and uptime. The user's own blockers (paused, no titles, no resume) each get a fix button.
* **🌅 Daily report**: 09:00 (TIMEZONE), last 24 h per user: jobs checked, matched, sent, best match, and whether scans ran all day. `Filters.daily_report` (default on) is toggled in Filters.
* **In-bot watchdog** (every 5 min): if there's been no scan for > max(15 min, 4 × interval), a cooldown, or 3 failed scans in a row, every admin gets **one** 🔴 alert. When it's healthy again they get ✅ "back to normal".
* **Insights**: from the last 30 days of the user's matches: the average and best match, the top "missing" skills (what to learn next), their most-wanted skills, and the most active companies.
* **Manual scan**: runs only that user's searches, at most once every `MANUAL_SCAN_COOLDOWN_SECONDS`. It shares a lock with the scheduled cycle.

---

## 9. Deployment

* **Docker** (`Dockerfile`, multi-stage):
  * `base`: dependencies.
  * `test`: copies the code and tests and runs **pytest**. If any test fails, the image doesn't build.
  * `runtime`: depends on `test`, so tests always run.
  * Healthcheck: `data/heartbeat` must have been touched in the last 150 s (the bot touches it every 30 s).
* **docker-compose.yml**:
  * `restart: unless-stopped`, and Docker itself is enabled at boot, so **the bot starts on boot and after crashes**;
  * runs as your host UID/GID, so `./data` stays yours;
  * mounts `./data` and `./.env` (read-only);
  * Docker's own copy of the output is capped at 2 × 1 MB.

### Disk usage / log retention
| What | Limit |
|---|---|
| `data/logs/bot.log*` | rotated every hour, `LOG_RETENTION_HOURS` (12) files kept; an hourly job and each start delete anything older |
| Docker container logs | `max-size 1m`, `max-file 2` → at most ~2 MB |
| Old images / build cache | removed by `jobhunter rebuild` / `jobhunter update` / `jobhunter clean` |
| Database | prune every 6 h: deliveries & jobs > 30 days (applied/saved kept), search history > 35 days, message IDs > 3 days |
| Resumes | one file per user (a new upload replaces the old one) |
* **Server setup** (once): `./scripts/install.sh`. It creates the venv and runs the tests, installs the git hooks, links the `jobhunter` command, and starts Docker.

### Server watchdog (cron)
`scripts/watchdog.sh` runs every 10 min from the user's crontab (`jobhunter watchdog install`, also done by `install.sh`). It covers the case the bot can't report itself: the container stopped, crashed or is unhealthy, or the heartbeat is older than 5 min.
* It runs `docker compose up -d` and sends **one** Telegram alert to `ADMIN_IDS`, or to the admins in the DB, using curl with `--form-string`.
* When things recover, it sends one ✅ message. The open alert is remembered in `data/watchdog.alerted`.

### `jobhunter` command (like `wnc`)
`~/.local/bin/jobhunter → <repo>/scripts/jobhunter`, works from any directory and over SSH:

```
jobhunter health            container, heartbeat, stats, disk, last problems
jobhunter logs | log | errors | scans
jobhunter users | approve <id> | block <id> | make-admin <id> | stats | reset-cooldown
jobhunter start | stop | restart
jobhunter rebuild           rebuild (tests run) + restart     ← after you change code
jobhunter update            git pull + rebuild                ← after pushing from another machine
jobhunter watchdog install  cron check every 10 min (restart + Telegram alert)
jobhunter clean             free space: logs older than 12 h, old images, build cache
jobhunter test | env | backup | shell
```

---

## 10. Tests & quality gates

`tests/` contains about 175 tests. They make no network calls; LinkedIn and Telegram are faked in `tests/fakes.py`.

| File | Covers |
|---|---|
| `test_skills_filters.py` | skill aliases (Java≠JavaScript, Go≠"go to"…), Filters JSON, LinkedIn params, query sharing, quiet hours, input parsers |
| `test_resume_storage.py` | PDF/DOCX/TXT extraction, errors, years (merge, education skip, formats), titles, suggestions, per-user folders (perms, replace, isolation, path safety) |
| `test_matcher.py` | required years, title relevance, exclusions, years/fresher rules, threshold, score monotonicity, no-resume mode |
| `test_linkedin.py` | HTML parsing on saved real pages, params sent, 429 back-off / give-up, last page |
| `test_db.py` | users, deliveries, duplicates, 48 h message window, pruning |
| `test_scanner.py` | full cycles, shared searches, single detail fetch, paging, back-fill, duplicates, pause, rate-limit cooldown & recovery, re-evaluation |
| `test_referral.py` | every message is job-specific, the note stays ≤ 200 chars even with huge titles, no-resume fallback, name parsing, people-search links, handler + isolation |
| (locations) | `test_skills_filters.py::test_normalize_location`: Delhi NCR → New Delhi string, aliases, pass-through |
| `test_health.py` | health verdicts, `/alive` (healthy / down / slow / cooldown / blockers / restart), cycles table + pruning, user_window, in-bot watchdog (alert once, recover), daily report + toggle, **watchdog.sh run against fake docker/curl** (restart, single alert, recovery, frozen bot, admins from DB) |
| `test_bot.py` | approval flow, admin-only, resume upload, filter buttons & text, job buttons, user isolation, delivery cap/quiet/pause/blocked, Clear chat, /deleteme, manual scan cooldown |

**The tests run automatically:**
1. **pre-commit** and **pre-push** git hooks (`scripts/hooks`, enabled by `git config core.hooksPath scripts/hooks`);
2. **GitHub Actions** (`.github/workflows/tests.yml`) on every push and PR, plus a Docker build;
3. **every Docker build** (`jobhunter rebuild`), so a broken bot is never deployed.

---

## 11. Making changes (checklist)

1. Change the code and **add or adjust tests** next to the feature (see the table above).
2. `jobhunter test`
3. If you changed the behaviour, update this file and `docs/architecture.html`.
4. `git commit` (the hook runs the tests), then `git push` (the hook and CI run them again).
5. `jobhunter rebuild` on the server (the build runs the tests again, then restarts the bot).

**Common extensions**
* *New skill* → `app/skills.py` (+ a case in `test_skills_filters.py::test_find_skills`).
* *New filter*:
  1. add a field to `Filters`, and to `summary()` and `filters_kb()`;
  2. add a handler branch in `on_filter_button` / `apply_text_setting`;
  3. if LinkedIn supports it, add it to `Query` and `Query.params()`;
  4. if it's applied after fetching, add it to `matcher.evaluate()`.
* *Any user-facing feature* → also explain it in `ui.INTRO_PAGES` (the tour) and `ui.HELP`.
* *New menu button* → `ui.MENU_BUTTONS` / `MAIN_KB` plus the `buttons` dict in `bot.build()` (a test checks every button is wired).

## 12. Configuration (`.env`)

| Var | Default | |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | from @BotFather (**never commit `.env`**) |
| `ADMIN_IDS` | empty | comma-separated Telegram IDs; empty → the first /start becomes admin |
| `ACCESS_MODE` | approval | `approval` or `open` |
| `MAX_USERS` | 50 | cap for open mode |
| `SCAN_EVERY_MINUTES` | 3 | min 2 |
| `LIVE_MAX_AGE_MINUTES` | 30 | ⚡ Just posted: max job age (min 5) |
| `PAGES_FIRST_SCAN` | 3 | backlog pages for a new search |
| `MAX_PER_CYCLE` | 10 | alerts per user per cycle |
| `MANUAL_SCAN_COOLDOWN_SECONDS` | 120 | |
| `REQUEST_DELAY_MIN/MAX` | 2 / 5 | seconds between LinkedIn requests |
| `MAX_COOLDOWN_MINUTES` | 60 | cap for the rate-limit back-off |
| `MAX_RESUME_MB` | 5 | |
| `LOG_RETENTION_HOURS` | 12 | hours of log files kept in `data/logs` |
| `TIMEZONE` | Asia/Kolkata | for quiet hours and display |

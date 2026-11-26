<p align="center"><img src="assets/logo-640.jpg" width="160" alt="Job Hunter logo"></p>

# 🎯 Job Hunter

A Telegram bot ([@openings_radar_bot](https://t.me/openings_radar_bot)) that watches LinkedIn for new jobs and sends
each user the ones that match **their resume**, with a match %, the skills they have, and the skills the job also wants.

* 📄 Send your resume (PDF/DOCX/TXT). It reads your skills, experience and titles, and suggests keywords.
* ⚙️ LinkedIn's own filters: keywords, location, date posted, experience level, job type, on-site/remote/hybrid, Easy Apply.
* 🎯 Bot-side filters: **match threshold** (default 0 = everything), max years asked, skip title words / companies,
  max applicants, quiet hours.
* ⚡ Scans every 5 minutes, newest first, so jobs arrive minutes after they're posted.
* ✅ Applied / ⭐ Save / 🙈 Hide buttons, 💡 skill-gap insights, 🧹 clear chat, approval-based multi-user access.

**How it works:** [ARCHITECTURE.md](ARCHITECTURE.md) · visual: [docs/architecture.html](docs/architecture.html)

## Quick start (server)

```bash
git clone git@github.com:Harsh-1000/job-hunter.git && cd job-hunter
cp .env.example .env && nano .env        # TELEGRAM_BOT_TOKEN=...
./scripts/install.sh                     # venv + tests, git hooks, `jobhunter` command, Docker (auto-starts on boot)
```
Then open the bot in Telegram and send **/start**. The first user becomes the admin and approves everyone else.

## Day to day

```
jobhunter health        is it running? last problems
jobhunter logs          live output
jobhunter users         who's using it · jobhunter approve <id>
jobhunter rebuild       after changing code (tests run during the build)
jobhunter update        git pull + rebuild
jobhunter help          everything else
```

## Development

```bash
.venv/bin/python -m pytest -q            # or: jobhunter test
```
The tests run before every commit and push (git hooks), on GitHub Actions, and during every Docker build.

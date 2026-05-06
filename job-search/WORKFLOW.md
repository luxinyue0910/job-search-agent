# Local Job Search Workflow

This workspace is designed for human-in-the-loop job applications. It can find jobs, score them, prepare materials, fill known ATS fields, and notify you by email. It must not submit applications, send recruiter outreach, or automate LinkedIn.

## Setup

1. Keep private data outside the public code repo. Pick a private directory:

```bash
export JOB_SEARCH_PRIVATE_DIR="$HOME/Documents/job-search-private"
```

Add that line to `~/.zshrc` on each computer if you want it to persist.

2. Initialize private files from examples:

```bash
python3 job-search/scripts/job_search.py init-person
```

3. Edit private files under `$JOB_SEARCH_PRIVATE_DIR`:

- `$JOB_SEARCH_PRIVATE_DIR/profile.json`
- `$JOB_SEARCH_PRIVATE_DIR/resume/master_resume.md`
- `$JOB_SEARCH_PRIVATE_DIR/data/sources.json`
- `$JOB_SEARCH_PRIVATE_DIR/data/applications.json`

4. Add your PDF resume under `$JOB_SEARCH_PRIVATE_DIR/resume/` and set `profile.json` `resume_file`.
5. Optional browser setup:

```bash
cd job-search
npm install
```

## Multiple People

The root workspace is the backward-compatible default person. For another person, create an isolated partition:

```bash
export JOB_SEARCH_PRIVATE_DIR="$HOME/Documents/job-search-private"
python3 job-search/scripts/job_search.py --person alice init-person
```

Then edit:

- `$JOB_SEARCH_PRIVATE_DIR/profiles/alice/profile.json`
- `$JOB_SEARCH_PRIVATE_DIR/profiles/alice/resume/master_resume.md`
- `$JOB_SEARCH_PRIVATE_DIR/profiles/alice/data/sources.json`

Run commands with `--person alice`:

```bash
python3 job-search/scripts/job_search.py --person alice run
python3 job-search/scripts/job_search.py --person alice notify --send-email
node job-search/scripts/fill_form.js --person alice --id <application-id>
```

Each person gets separate profile data, resume, tracker, output, email browser session, and ATS browser session.

## Two-Repo Sync Model

Use two repositories when this code is public:

- Public code repo: scripts, templates, examples, and docs.
- Private data repo: real `profile.json`, resumes, trackers, sources, outputs, and local browser sessions.

Clone both on every computer, then set `JOB_SEARCH_PRIVATE_DIR` to the private data repo path.

## Core Commands

Run commands from the repository root.

```bash
python3 job-search/scripts/job_search.py sync-csv
python3 job-search/scripts/job_search.py find-jobs
python3 job-search/scripts/job_search.py discover-jobs --since-days 7 --score
python3 job-search/scripts/job_search.py score-job --id <application-id>
python3 job-search/scripts/job_search.py prepare-application --id <application-id>
python3 job-search/scripts/job_search.py notify
```

Use `discover-jobs` when freshness matters. It uses ATS APIs for Greenhouse, Lever, and Ashby where possible, records `posted_at`, `updated_at`, `first_seen`, and `last_seen`, and only adds jobs whose posted date is inside the cutoff. Jobs with no posted date are skipped by default. It also applies a title filter for software, backend, AI, new grad, junior, DevOps, platform, and related roles; pass `--no-role-filter` to review every fresh posting.

Examples:

```bash
python3 job-search/scripts/job_search.py discover-jobs --since-days 7 --score
python3 job-search/scripts/job_search.py discover-jobs --since-hours 24 --score
python3 job-search/scripts/job_search.py discover-jobs --since-hours 24 --include-unknown-posted-date
python3 job-search/scripts/job_search.py discover-jobs --since-hours 24 --no-role-filter
```

`data/sources.json` can include ATS identifiers to avoid brittle HTML scraping:

```json
{
  "sources": [
    {
      "company": "Example",
      "platform": "greenhouse",
      "board": "example",
      "url": "https://job-boards.greenhouse.io/example"
    },
    {
      "company": "Example",
      "platform": "lever",
      "site": "example",
      "url": "https://jobs.lever.co/example"
    },
    {
      "company": "Example",
      "platform": "ashby",
      "board": "example",
      "url": "https://jobs.ashbyhq.com/example"
    }
  ]
}
```

End-to-end local run:

```bash
python3 job-search/scripts/job_search.py run
```

Send the run summary to yourself after generating it. The provider comes from `profile.json`:

```bash
python3 job-search/scripts/job_search.py run --send-email
```

## Browser Commands

Fill a supported ATS form up to the submit/review step:

```bash
node job-search/scripts/fill_form.js --id <application-id>
```

Send the latest notification markdown to yourself through Outlook or Gmail:

```bash
node job-search/scripts/outlook_notify.js --summary job-search/output/notifications/latest.md
node job-search/scripts/gmail_notify.js --summary job-search/output/notifications/latest.md
```

## Safety Rules

- Never click final application submit buttons.
- Never send recruiter emails or LinkedIn connection requests.
- Email notifications may only be sent to `profile.personal.email`.
- Stop for CAPTCHA, login walls, account creation, e-signature, or unclear form questions.
- Never fabricate skills, metrics, companies, education, or authorization status.
- Do not commit real `profile.json`, resume PDFs, trackers, browser profiles, or generated outputs to a public repository.

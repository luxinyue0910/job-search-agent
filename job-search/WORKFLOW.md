# Local Job Search Workflow

This workspace is designed for human-in-the-loop job applications. It can find jobs, score them, prepare materials, fill known ATS fields, and notify you by email. It must not submit applications, send recruiter outreach, or automate LinkedIn.

## Setup

1. Copy the example private files:

```bash
cp examples/profile.example.json profile.json
cp examples/master_resume.example.md resume/master_resume.md
cp examples/sources.example.json data/sources.json
cp examples/applications.example.json data/applications.json
cp examples/applications.example.csv data/applications.csv
```

2. Edit `profile.json`.
3. Replace `resume/master_resume.md` with your full truthful resume.
4. Add your PDF resume under `resume/` and set `profile.json` `resume_file`.
5. Edit `data/sources.json` with target company career pages.
6. Optional browser setup:

```bash
cd job-search
npm install
```

## Multiple People

The root workspace is the backward-compatible default person. For another person, create an isolated partition:

```bash
python3 job-search/scripts/job_search.py --person alice init-person
```

Then edit:

- `job-search/profiles/alice/profile.json`
- `job-search/profiles/alice/resume/master_resume.md`
- `job-search/profiles/alice/data/sources.json`

Run commands with `--person alice`:

```bash
python3 job-search/scripts/job_search.py --person alice run
python3 job-search/scripts/job_search.py --person alice notify --send-email
node job-search/scripts/fill_form.js --person alice --id <application-id>
```

Each person gets separate profile data, resume, tracker, output, email browser session, and ATS browser session.

## Core Commands

Run commands from the repository root.

```bash
python3 job-search/scripts/job_search.py sync-csv
python3 job-search/scripts/job_search.py find-jobs
python3 job-search/scripts/job_search.py score-job --id <application-id>
python3 job-search/scripts/job_search.py prepare-application --id <application-id>
python3 job-search/scripts/job_search.py notify
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

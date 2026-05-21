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
5. Keep the PDF and Markdown resume in sync:

- `profile.json` `resume_file` is the file uploaded to ATS forms.
- `resume/master_resume.md` is used for scoring, keyword matching, and generating tailored application materials.

6. Optional browser setup:

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

## Tracks

Use tracks when the same person is applying with different resume/search/scoring strategies, such as SDE, QA/SDET, or mobile. Tracks share the same private profile, `sources.json`, `company_watchlist.json`, `applications.json`, and `seen_jobs.json`; each application can record `target_track`, `matched_tracks`, and `resume_file`.

Track files live under `$JOB_SEARCH_PRIVATE_DIR/tracks/<track-id>/`:

- `track.json`: roles, search terms, scoring keywords, and resume paths.
- `master_resume.md`: the Markdown resume used for scoring and tailored materials.
- `resume.pdf`: the PDF uploaded to ATS forms for that track.

Examples:

```bash
python3 job-search/scripts/job_search.py discover-jobs --track qa_engineer --since-days 7 --score
python3 job-search/scripts/job_search.py discover-web-jobs --track qa_engineer --since-days 7 --score --update-sources
python3 job-search/scripts/job_search.py discover-watchlist-jobs --provider bing --track qa_engineer --since-days 7 --score
python3 job-search/scripts/job_search.py score-job --id <application-id> --track qa_engineer
python3 job-search/scripts/job_search.py prepare-application --id <application-id> --track qa_engineer
```

If a job is found by both QA and SDE searches, the tracker keeps one application by URL and appends both values to `matched_tracks`. `prepare-application` uses the application `target_track` unless you override it with `--track`.

Use `discover-jobs` when freshness matters. It uses ATS APIs for Greenhouse, Lever, and Ashby where possible, records `posted_at`, `updated_at`, `first_seen`, and `last_seen`, and only adds jobs whose posted date is inside the cutoff. Jobs with no posted date are skipped by default. It also applies a title filter for software, backend, AI, new grad, junior, DevOps, platform, and related roles; pass `--no-role-filter` to review every fresh posting.

Use `classify-sources --custom-only` before maintaining large company lists. It inspects configured career pages and reports whether a `custom` source can be upgraded to a direct platform such as Greenhouse, Lever, Ashby, Gem, or Workday. Use `--apply` after reviewing the output. Workday sources use the public CXS API and parse relative posting dates such as `Posted Today` and `Posted 3 Days Ago`. Phenom pages are detected but still need a dedicated adapter.

Use `discover-web-jobs` to expand beyond known company boards. It calls a search API, looks for public Greenhouse, Lever, and Ashby job URLs, parses the original ATS posting, then applies the same posted-date, title, location, seen-job, and tracker de-dupe rules. This avoids logging into LinkedIn, Indeed, Jobright, or Wellfound and treats those sites as discovery hints rather than scrape targets.

Use `discover-watchlist-jobs` for large companies and Seattle target companies that use custom career sites. The private `company_watchlist.json` is configured for the Bing Web Search API route by default, so daily watchlist discovery does not spend SerpAPI quota.

Configure one provider:

```bash
export SERPAPI_API_KEY="..."
export BING_SEARCH_API_KEY="..."
```

Examples:

```bash
python3 job-search/scripts/job_search.py discover-jobs --since-days 7 --score
python3 job-search/scripts/job_search.py classify-sources --custom-only
python3 job-search/scripts/job_search.py classify-sources --custom-only --apply
python3 job-search/scripts/job_search.py discover-web-jobs --provider serpapi --since-days 7 --score --update-sources
python3 job-search/scripts/job_search.py discover-web-jobs --provider serpapi --since-days 7 --pages-per-query 3 --score --update-sources
python3 job-search/scripts/job_search.py discover-web-jobs --provider bing --since-days 7 --score --update-sources
python3 job-search/scripts/job_search.py discover-watchlist-jobs --provider bing --track general_sde --since-days 7 --score
python3 job-search/scripts/job_search.py discover-watchlist-jobs --provider bing --track qa_engineer --since-days 7 --score
python3 job-search/scripts/job_search.py discover-jobs --since-hours 24 --score
python3 job-search/scripts/job_search.py discover-jobs --since-hours 24 --include-unknown-posted-date
python3 job-search/scripts/job_search.py discover-jobs --since-hours 24 --no-role-filter
```

Use `--pages-per-query` with SerpAPI when the first Google result page is too shallow. Each extra page costs another SerpAPI request per query, so keep `--max-queries` and `--pages-per-query` balanced.

Aggregator discovery is intentionally human-in-the-loop. Use Jobright or Wellfound as lead sources, then resolve companies back to official ATS boards before applying:

```bash
JOB_SEARCH_PRIVATE_DIR=/path/to/private node job-search/scripts/collect_aggregator_leads.js --provider jobright --resolve-sources
JOB_SEARCH_PRIVATE_DIR=/path/to/private node job-search/scripts/collect_aggregator_leads.js --provider wellfound --resolve-sources
python3 job-search/scripts/job_search.py discover-jobs --since-days 7 --score
```

The collector opens a real browser profile, waits for you to log in and set filters, scrolls the visible result list, writes `data/aggregator_leads.json`, and optionally searches for official Greenhouse, Lever, or Ashby boards to add to `sources.json`.

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

The form filler launches a dedicated ATS Chrome profile with extensions disabled by default, so tools like Jobright do not inject sidebars or slow down DOM automation. It keeps Chrome open by default after filling so you can manually review, edit, and submit. Use `--close-when-done` only for dry runs where the browser should close automatically. Use `--allow-extensions` only when a specific application flow needs an extension.

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

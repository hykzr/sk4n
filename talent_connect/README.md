# NUS TalentConnect CLI

A deterministic CLI for fetching and persisting jobs from the Kinobi-based
[NUS TalentConnect](https://nus-talentconnect.app.kinobi.asia/).

It discovers job and company records,
keeps a local copy current, and makes that data easy to query.

## Run it

From the repository root:

```bash
uv run talent-connect --help
# or
uv run python -m talent_connect --help
```

The default SQLite database is `data/talent_connect/talent_connect.sqlite3`.
Override it globally with `--data-path PATH` or
`TALENT_CONNECT_DATA_PATH=PATH`.

## Authentication

```bash
uv run talent-connect auth status
uv run talent-connect auth login
uv run talent-connect auth login --refresh
uv run talent-connect auth logout
```

`auth login` opens a visible browser and detects a completed NUS SSO login
automatically; The Playwright storage state is
saved as `nus_talent_connect` and reused for status checks.

`auth logout` removes only **talent connect's** saved session. 

Job commands authenticate by default. If the saved session is missing or
expired, the CLI opens a browser and continues automatically after NUS SSO
finishes. Authenticated job-list records include `user_has_applied`,
`is_bookmarked`, `is_unqualified_student`, and other job details

Use `--no-login` on `fetch`, `job`, `company`, or `search job` to use the public
job endpoint instead. Company records themselves are public and have the same
fields with or without login; `company` authenticates only because it also
fetches that company's jobs. The CLI never extracts or prints Kinobi's browser
token.

## Low-level authenticated API requests

Use `api` to call a Kinobi endpoint directly through the same authenticated
in-page Axios client:

```bash
# GET is the default
uv run talent-connect api '/api/auth/'
uv run talent-connect api '/api/job?entries_per_page=1'

# Send a JSON body with any HTTP method
uv run talent-connect api '/api/example' -X POST -d '{"key":"value"}'
```

The path must begin with `/api/`; full URLs are rejected so Kinobi
authentication cannot be forwarded to another host. Successful response data
is written as JSON to stdout. Request and validation errors are written to
stderr and return exit code 2, making the command suitable for shell pipelines
and endpoint exploration. This command does not read or update the local job
database.

## Fetch and persist jobs

```bash
# Fetch every current job, upsert changed records, and print all matches
uv run talent-connect fetch

# Common filters
uv run talent-connect fetch --query engineer --employment-type internship
uv run talent-connect fetch --company "Google"
uv run talent-connect fetch --country SG --work-arrangement "work from office"
uv run talent-connect fetch --applied --include-expired-if-applied
uv run talent-connect fetch --qualified --posted-after 2026-07-01
uv run talent-connect fetch --application-type "easy apply"
uv run talent-connect fetch --saved --special-needs
uv run talent-connect fetch --status interviewing
uv run talent-connect fetch --status declined --status declined-offer
uv run talent-connect fetch --max-jobs 50

# Public records only; do not validate or open a login
uv run talent-connect fetch --no-login --query engineer

# Still upsert every match, but print only new or changed jobs
uv run talent-connect fetch --updated-only

# Query only stored data
uv run talent-connect fetch --cached --query python

# Structured and agent-friendly output
uv run talent-connect fetch --max-jobs 10 --format json
uv run talent-connect fetch --max-jobs 10 --format jsonl
uv run talent-connect fetch --max-jobs 10 --format plain
```

Repeat a filter flag to send multiple values. Supported filters are:

- `--company`, `--company-type`, `--exclude-company-type`
- `--employment-type`, `--exclude-employment-type`
- `--work-arrangement`, `--country`, `--city`
- `--industry`, `--role`
- `--work-term`, `--related-work-term`
- `--programme`, `--internship-programme`
- `--application-type` (`easy apply` or `external job`)
- `--hard-skill`, `--soft-skill`
- `--applied` / `--not-applied`
- `--drafted` / `--not-drafted`
- `--saved` / `--not-saved`
- `--recommended` / `--not-recommended`
- `--special-needs` / `--not-special-needs`
- `--qualified`
- `--status STATUS` (repeatable)
- `--posted-after DATE_OR_DATETIME`
- `--include-expired-if-applied`

Applied, drafted, saved, recommended, qualified, and status filters require
authentication and cannot be combined with `--no-login`. `--recommended` uses
the separate endpoint used by the web UI. `--qualified` checks
`is_unqualified_student` after the authenticated search because the API ignores
an attempted qualification query parameter.

`--status` mirrors the authenticated profile's job-workflow tabs. Its values
are `withdrawn`, `interviewing`, `declined`, `offered`, `accepted-offer`,
`job-history`, and `declined-offer`. Repeating it returns the union of those
states. These are deliberately distinct from the main job search's `--applied`
flag:

| CLI status | Kinobi workflow query |
| --- | --- |
| `withdrawn` | application `statuses=withdrawn` |
| `interviewing` | application `statuses=interviewing` |
| `declined` | application `statuses=rejected` |
| `offered` | offer `responses=pending`, statuses `sent,terminated,expired` |
| `accepted-offer` | offer `responses=accepted`, statuses `sent,expired`, excluding past work |
| `job-history` | offer `responses=accepted`, statuses `sent,expired`, past work only |
| `declined-offer` | offer `responses=rejected`, statuses `sent,expired` |

Offer requests always include the authenticated user's applicant ID. Kinobi's
offer endpoint is institution-wide without that parameter, so omitting it
would return other students' records. Empty status tabs are returned as an
accurate empty result. The workflow endpoints return application/offer records
with nested jobs; the CLI fetches each matching job's authenticated detail,
attaches `job_application` or `job_offer` metadata, and persists the combined
record. Other job filters are then applied to those complete job records.

Kinobi also ignores posted-date and sort query parameters, and its default
ordering is not reliably chronological. Therefore `--posted-after` fetches ALL
remote matches, compares `published_at` locally, and only then applies
`--max-jobs`. It cannot safely stop at an earlier page so is very slow

Kinobi's authenticated job-list endpoint  does not, return every
job-detail field. The dedicated detail endpoint adds 17 fields, including
start/end dates, role, work term, application ID/status, additional-information
responses, and applicant count. Therefore the detail controls remain useful:

- Default `fetch` requests dedicated details only for jobs with no stored
  detail or whose list `updated_at` differs from the `updated_at` recorded when
  detail was last fetched.
- `--no-details` skips all dedicated detail requests and stores list records.
- `--refresh-details` forces a dedicated detail request for every match.

Workflow `--status` searches always fetch their matching job details because
the application and offer endpoints expose only partial nested job records;
`--no-details` therefore applies only to ordinary job-list searches.

Kinobi's `updated_at` is the job record's last-modified timestamp. It is the
same on the list and detail endpoints in the tested tenant, even though some
other fields (including `published_at` on imported jobs) can differ between
those endpoints. The CLI stores a separate detail timestamp, so a normal fetch
can avoid hundreds of unchanged detail requests without assuming that a job's
local fingerprint alone proves the detail record is current.

Interactive fetches show separate progress bars for list pages and detail
requests. Progress is disabled for `--format` output so stdout remains valid.

Each remote query upserts its matches. Unchanged rows only get a new
`last_seen_at`; changed payloads update `changed_at`; new rows retain
`first_seen_at`. A dedicated `job` refresh can enrich a list record without a
later list fetch discarding fields unique to the detail endpoint.

`--updated-only` affects output, not persistence: every matched job is checked
and upserted, while only new or changed jobs are printed. It cannot be combined
with `--cached`.

## Jobs and companies

```bash
# _id or slug; remote refresh is the default
uv run talent-connect job 6a228d6d028283001dd60467
uv run talent-connect job JOB_SLUG --no-refresh

# _id, company_id, or slug; also fetches and upserts current jobs
uv run talent-connect company "google"
uv run talent-connect company COMPANY_ID --max-jobs 20
uv run talent-connect company COMPANY_SLUG --no-refresh
```

`job --no-refresh` and `company --no-refresh` are strictly offline and fail
clearly when the requested record has not been stored.

## Lightweight ID search

```bash
uv run talent-connect search job "machine learning"
uv run talent-connect search job internship --employment-type internship
uv run talent-connect search company "Google"
uv run talent-connect search company "Example Corp" --cached
```

Search prints Kinobi IDs and slugs. The job API inevitably returns full list
records, but `search job` deliberately persists only summary fields. Use `job
ID` to fetch the dedicated detail record.

## Output formats

With no `--format`, commands use human-friendly Rich tables and detail views.

- `--format json` writes one JSON value.
- `--format jsonl` writes one job or company record per line.
- `--format plain` writes each top-level field as `field: value`, with records
  separated by a dashed line. Nested values stay as compact JSON on that same
  line, which is convenient for agents and shell pipelines.

## Eligibility IDs

`restriction.academic_majors` contains opaque IDs from NUS's tenant-specific
academic directory. The same IDs appear as `field_value` entries in
`eligible_internship_programme_versions[].student_eligibility_criteria` when a
programme requires particular majors. A criterion also carries
`field_variable`, `field_comparator`, and `operator`, which describe what the
IDs constrain.

These are not the values returned by Kinobi's public `/api/major-list/all`
endpoint: that endpoint is a generic text list and cannot resolve the
NUS-specific IDs. The authenticated user record supplies the student's own
academic-major IDs and a human-readable academic profile, while the job API
uses those IDs server-side to produce `is_unqualified_student`. The current
web application exposes no general ID-to-name lookup, so the CLI preserves the
raw restrictions and treats Kinobi's qualification result as authoritative
instead of inventing labels.

## Kinobi endpoints

The implementation was verified against the NUS tenant on 30 July 2026:

| Purpose | Endpoint | Login |
| --- | --- | --- |
| Authenticated job search/list | `GET /api/job` through Kinobi's in-page client | Yes |
| Authenticated recommended jobs | `GET /api/job/recommendation` through Kinobi's in-page client | Yes |
| Authenticated job detail | `GET /api/job/{id-or-slug}` through Kinobi's in-page client | Yes |
| Profile application status | `GET /api/job-application/by-user-and-job-paginated?statuses=...` | Yes |
| Profile offer status | `GET /api/job-offer/all-paginated?applicant_ids=...&statuses=...&responses=...` | Yes |
| Public job search/list | `GET /api/job/public` | No |
| Public job detail | `GET /api/job/{id-or-slug}/public` | No |
| Company search/list | `GET /api/company` | No |
| Company by database ID | `GET /api/company/by-id/{id}` | No |
| Company by company ID | `GET /api/company/company-id/{company_id}` | No |
| Company by slug | `GET /api/company/{slug}` | No |
| Current user/status | `GET /api/auth/` through Kinobi's in-page client | Yes |
| Generic public major names | `GET /api/major-list/all` | No |

The API origin is `https://nus-talentconnect.server.kinobi.asia`; the web app
origin is `https://nus-talentconnect.app.kinobi.asia`.

# NUS TalentConnect

Search [TalentConnect](https://nus-talentconnect.app.kinobi.asia/) jobs and companies, and keep
local copies of the results.

## Sign in

```bash
talent-connect auth login # Complete NUS SSO in the browser
talent-connect auth status # Check the saved sign-in
talent-connect auth logout # Forget this tool's sign-in
```

Login remembers your sign-in; add `--refresh` to `auth login` to start a fresh one.

## Find and save jobs

```bash
talent-connect fetch --query engineer --employment-type internship --max-jobs 20 # Find and save matches
```

Filters include company, location, skills, saved jobs (`--saved`), qualification (`--qualified`),
and application/offer workflow (`--status interviewing`, for example). Saved jobs, applications,
qualification, recommendations, and offers need a login. Workflow tabs are separate from the general
`--applied` filter.

For public listings without signing in:

```bash
talent-connect fetch --no-login --query engineer --max-jobs 20 # Browse public jobs
```

A posted-date filter such as `--posted-after 2026-07-01` can be slow because all matching listings
must be checked.

## Search IDs and read details

```bash
talent-connect search job "machine learning" # Find job IDs without requesting full details
talent-connect job JOB_ID # Read a job's description and requirements
talent-connect search company Google # Find company IDs
talent-connect company COMPANY_ID --max-jobs 20 # Read a company and its current jobs
```

Replace `JOB_ID` or `COMPANY_ID` with an ID or slug from the search results. Qualification reflects
TalentConnect's assessment of your profile. Some eligibility restrictions use internal NUS IDs that
the tool cannot translate into major names.

## Saved results and more options

Normal reads check remote listings and reuse unchanged job details. Use `fetch --cached` or
`search ... --cached` for local results; use `job ... --no-refresh` or `company ... --no-refresh`
for a saved record. `--refresh-details` forces job details to be downloaded again. `--updated-only`
prints only new or changed matches while still saving every match.

Run `sk4n paths` to locate the saved database. Put `--data-path PATH` before the subcommand to use
another file or folder. See `talent-connect --help` and `talent-connect fetch --help` for filters
and output options. Advanced API/browser procedures are in the
[low-level guide](../skills/nus-talent-connect/references/low-level.md).

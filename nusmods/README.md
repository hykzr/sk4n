# NUSMods CLI

Search NUSMods' public course API and manage a local timetable that imports
and exports native NUSMods share URLs. No NUS login is required.

```bash
uv run nusmods --help
uv run python -m nusmods --help
```

The default schedule and one-day API/review cache are stored in the `nusmods/`
directory below the platform-specific Agent for NUS user-data root. Use
`--data-path PATH` for a command-specific override, `AGENT_FOR_NUS_HOME` to
relocate all persistent application data, or `--cache-ttl SECONDS` to change
cache expiry. The active academic year is inferred the same way as the
timetable site (the upcoming year becomes active in July); override it with
`--academic-year 2025/2026`.

Cache behavior can be selected globally or after commands that read remote
data:

```bash
# Ignore cached reads, fetch now, and replace the affected cache entries
uv run nusmods course CG2028 --comments --refresh

# Accept cached entries of any age and never contact NUSMods or Disqus
uv run nusmods search security --no-refresh
uv run nusmods schedule today --no-refresh
```

With neither flag, fresh entries are used for `--cache-ttl` seconds. Expired
entries are refreshed, with stale entries retained as an offline fallback if
the request fails. `--no-refresh` returns a clear cache-miss error when a
required resource has never been cached.

## Search courses

```bash
uv run nusmods search "machine learning"
uv run nusmods search circuits --sem s1 --level 2000 --min-units 4
uv run nusmods search programming --faculty Computing --department Computer
uv run nusmods search design --grading graded --attribute su
uv run nusmods search data --no-exam
uv run nusmods search algorithms --no-exam-clash s1
uv run nusmods search security --format json
```

The filters cover the NUSMods course finder's Web UI facets:

- offered semester (`--sem`, repeatable);
- no exam and no clash with a stored semester;
- course level;
- exact, minimum, and maximum units;
- faculty, department, and grading basis;
- all NUSMods attribute flags (`year`, `su`, `grsu`, `ssgf`, `sfs`, `lab`,
  `ism`, `urop`, `fyp`, `mpes1`, and `mpes2`).

Repeated semester, level, unit, faculty, department, grading, and attribute
filters are OR conditions, matching the Web UI. Different filter categories
combine with AND. Faculty, department, and grading values are
case-insensitive substrings, so the exact dropdown label is not required.

## Course details and reviews

```bash
uv run nusmods course CG2028
uv run nusmods course CG2028 --sem s1
uv run nusmods course CG2028 --comments
uv run nusmods course CG2028 --comments --format json
```

Course output includes requisites, workload, grading, exam data, and every
lecture/tutorial/lab/other timetable slot, including class number, venue,
weeks, and the API's class `size` when supplied. `--comments` reads the public
reviews payload embedded by NUSMods' Disqus thread; no Disqus login is used.
The normalized review payload is cached with the course/API data and obeys
`--refresh`, `--no-refresh`, and `--cache-ttl`. If Disqus paginates a very
large thread, JSON output marks `hasMore: true`.

## Timetable management

```bash
# Import/export the exact share-link representation
uv run nusmods schedule import 'https://nusmods.com/timetable/sem-1/share?...'
uv run nusmods schedule export --sem s1

# Add, view, and remove
uv run nusmods schedule add CG2028
uv run nusmods schedule add EG2401A --ta --sem s1
uv run nusmods schedule --sem s1
uv run nusmods schedule delete CG2028 --sem s1

# Daily and summary views
uv run nusmods schedule today
uv run nusmods schedule today --date 2026-08-11
uv run nusmods schedule status
```

`schedule add` selects the first available class group for each lesson type.
`--ta` expands those groups into individual lessons, matching NUSMods' TA
timetable representation. Untimetabled courses remain in the schedule with no
slots.

Today's view follows NUSMods' academic-calendar start dates and public-holiday
list. On the actual current date it displays only lessons that have not ended,
as NUSMods' Today page does. A supplied non-current date displays the whole
day.

## Editing slots, role, and semester

Run edit without mutations to list stable `TYPE@N` selectors and current
selections:

```bash
uv run nusmods schedule edit CG2028 --sem s1
```

Student courses select one class group for every lesson type:

```bash
uv run nusmods schedule edit CG2028 --set LAB=02 --set TUT=01
```

TA courses can select zero through all individual lessons for each type:

```bash
# Convert the current student groups to TA slots
uv run nusmods schedule edit CG2028 --ta

# Replace, add, remove, select all, or select none
uv run nusmods schedule edit CG2028 --set LAB=@1,@3
uv run nusmods schedule edit CG2028 --add-slot LEC=@2
uv run nusmods schedule edit CG2028 --remove-slot LEC=@1
uv run nusmods schedule edit CG2028 --set TUT=all
uv run nusmods schedule edit CG2028 --set LAB=none
# Equivalent zero-selection spelling
uv run nusmods schedule edit CG2028 --clear LAB
```

A class number can be used instead of `@N`; in TA mode it expands to every
individual meeting in that class group. Full NUSMods lesson IDs are also
accepted for lossless automation.

```bash
uv run nusmods schedule edit CG2028 --student
uv run nusmods schedule edit CG2028 --move-to s2
uv run nusmods schedule edit CG2028 --hidden
uv run nusmods schedule edit CG2028 --visible --list-slots
```

Converting TA to student mode chooses the class group containing the most
currently selected TA lessons, which is NUSMods' own conversion rule.
Moving semesters keeps the student/TA role and initializes valid groups from
the destination semester.

## Stored shape

`data/nusmods/schedule.json` uses a small, readable schema:

```json
{
  "schemaVersion": 1,
  "academicYear": "2026/2027",
  "semesters": {
    "1": {
      "courses": {
        "CG2028": {
          "isTa": false,
          "hidden": false,
          "selections": {
            "Laboratory": ["02"],
            "Lecture": ["01"],
            "Tutorial": ["01"]
          }
        }
      }
    }
  }
}
```

Student selections are class numbers. TA selections are NUSMods' serialized
full lesson IDs, so multiple meetings with the same class number can still be
selected independently and exported without loss.

All read commands support `--format json`; searches additionally work well as
`jsonl`. Structured output writes only data to stdout, while errors go to
stderr with exit code 2.

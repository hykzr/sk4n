# Canvas

Check your NUS Canvas courses and save course materials for later. These commands read Canvas; they
do not submit work or change course content.

## Sign in

```bash
canvas auth login # Complete NUS SSO in the browser
canvas auth status # Check the saved sign-in
canvas auth logout # Forget this tool's sign-in; other browsers stay signed in
```

Login remembers your sign-in. Add `--refresh` to `auth login` to start a fresh sign-in.

## Find a course

```bash
canvas student # Show your student profile
canvas list --semester latest # List courses in the latest academic semester
canvas course CG2028 # Show course details and available sections
canvas course CG2028 home # Read the course's Home view
canvas course CG2028 path # Find the saved course folder
```

A course code can match several enrollments. Add a semester such as `-s 2526S1`, or select an exact
numeric Canvas course ID such as `62224`. Retired courses remain available from saved copies after
online access ends. Home can be empty when the lecturer has not published content there.

## Read course materials

Choose an available section shown by `course`:

```bash
canvas course CG2028 announcements list # Read lecturer announcements
canvas course CG2028 assignments list # Check assignments and deadlines
canvas course CG2028 discussions list # Read discussion topics
canvas course CG2028 files list # Find course files and downloaded copies
canvas course CG2028 groups list # Check course groups and membership
canvas course CG2028 modules list # Browse the course's content outline
canvas course CG2028 pages list # Read published course pages
canvas course CG2028 people list # View the course roster
canvas course CG2028 quizzes list # Check available quiz information
canvas course CG2028 syllabus list # Read the syllabus
```

Use an item ID from a list to read its details, or `path` to locate a section's saved material:

```bash
canvas course CG2028 assignments 118925 # Read one assignment
canvas course CG2028 files path # Locate the saved file index
```

Unstarted quizzes and staff-only quiz content are skipped.

## Check upcoming work

```bash
canvas upcoming # Show upcoming Canvas activity
canvas todo # Show your Canvas To-Do items
canvas calendar-events # Read your Canvas calendar
sk4n calendar --date 2026-08-14 # Map a date to the NUS academic week
```

Canvas's feeds may omit exams, class schedules, or deadlines mentioned only in announcements and
attachments. Check those materials too. The shared `sk4n calendar` command needs no login.

## Download multiple courses

```bash
canvas sync --course CG2028 --course CS1010 # Save materials for the selected courses
```

Run `canvas sync` without course filters to save all accessible courses.

## Saved copies and more options

Normal course reads check for updates and reuse unchanged downloads. Use `--no-refresh` for saved
copies only; use `--refresh` to force another download when something appears incomplete.

Compact JSON converts HTML to text. When formatting matters or text appears incomplete, preserve the
source HTML while keeping the compact fields:

```bash
canvas course CG2028 announcements list --format json --original-html
```

Bodies stored in HTML files remain file paths; open those files to inspect or render their HTML.

Run `sk4n paths` to find your data. Put `--data-path PATH` before the subcommand to choose another
folder. See `canvas --help` and, for example, `canvas course --help` for all options and structured
output formats. Advanced API/browser procedures are in the
[low-level guide](../skills/nus-canvas/references/low-level.md).

# NUSMods

Find courses, read reviews, and keep a local timetable that you can share through NUSMods. No NUS
login is needed. Timetable edits do not register for or drop official NUS courses.

## Explore courses

```bash
nusmods search circuits --sem s1 --level 2000 --min-units 4 # Find courses matching your interests
nusmods course CG2028 --comments # Read course details, lesson slots, and public reviews
```

Course details include prerequisites, workload, and exams. Reviews come from public Disqus comments;
large threads may be incomplete. Repeating a search filter includes any of its values; different
filter categories combine. `--no-exam-clash s1` compares exams with your saved timetable.

The academic year switches to the upcoming year in July. To choose another year, put
`--academic-year 2025/2026` before the subcommand.

## Keep a timetable

```bash
nusmods schedule import 'https://nusmods.com/timetable/sem-1/share?...' # Replace one semester from a share link
nusmods schedule --sem s1 # Show your saved timetable
nusmods schedule add CG2028 --sem s1 # Add a course with its first available class groups
nusmods schedule edit CG2028 --sem s1 --set LAB=02 --set TUT=01 # Choose class groups
nusmods schedule delete CG2028 --sem s1 # Remove a course from the local timetable
nusmods schedule export --sem s1 # Get a portable NUSMods share link
nusmods schedule today # Show today's lessons that have not ended
nusmods schedule status # Show the date, semester, courses, and non-TA units
```

Replace the example URL with your NUSMods share link. Import replaces that semester's local
timetable. Run `schedule edit COURSE --sem s1` without changes to see available choices before
selecting your own. Today's view uses the NUS academic calendar and public holidays; add
`--date YYYY-MM-DD` to inspect another day.

For TA timetables, add `--ta` when adding or editing a course. TA mode allows individual lessons:
for example, `schedule edit CG2028 --ta --set LAB=@1,@3`. Use selectors printed by the edit view;
`all`, `none`, `--add-slot`, and `--remove-slot` support broader changes. Edit also supports moving
between semesters (`--move-to s2`), switching back to student mode (`--student`), and hiding/showing
a course (`--hidden`/`--visible`).

## Saved data and more options

Course data is normally cached for one day. Expired data is refreshed, with older saved data as a
fallback if the network fails. Use `--refresh` to force an update or `--no-refresh` for saved data
only; missing saved data produces an error.

Run `sk4n paths` to find your timetable and cached data. Put `--data-path PATH` before the
subcommand to use another folder. See `nusmods --help` and `nusmods schedule edit --help` for all
filters, slot options, and output formats.

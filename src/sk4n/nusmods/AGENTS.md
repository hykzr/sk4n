# NUSMods maintenance

Start with `client.py` for public API/review caching, `schedule.py` for share-link compatibility,
and shared `sk4n/academic_calendar.py` for Singapore dates and academic-week calculations. No login
or saved browser session is needed.

- Match NUSMods semantics, including its July academic-year rollover, filter combinations, and
  student/TA timetable conversion. Do not infer teaching weeks from fixed Gregorian offsets when
  public calendar data is available.
- Student selections identify class groups; TA selections identify individual sessions. Preserve
  full serialized NUSMods lesson IDs for lossless share-link round trips; class numbers alone lose
  meetings. Timetable changes are local and never perform official course registration.
- Reviews come from the public embedded Disqus payload, not an authenticated review API. Preserve
  pagination/coverage warnings rather than presenting a partial thread as complete.
- Default caching deliberately permits stale fallback after network failure. Forced refresh must
  bypass cached reads; cache-only mode must never contact NUSMods or Disqus.

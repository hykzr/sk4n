# Canvas maintenance

Start with `fetcher.py` for targeted reads and `sync.py` for bulk sync. Content-specific modules
handle their own artifacts; `quiz_content.py` handles Classic/New Quizzes review access.

- Keep normal reads incremental and scoped to the requested course/resource.
- Read-only unless approved: following a quiz-taking redirect can start an attempt. Never open
  unstarted quizzes; skip quiz content for TA/staff roles to avoid elevated-access behavior.
- Navigation visibility is not a complete access boundary: Home may resolve to a hidden tab, and
  module/HTML references may expose files while Files is closed. Ignore explicit cross-course embeds
  rather than fetching them under the current course; retain inaccessible same-course links/errors.
- Canvas has no cheap syllabus-body change signal; existing syllabi deliberately stay lazy until a
  forced refresh. Account creation time is only an enrollment-year inference, not matriculation
  data.
- A vanished course is archived as retired, not deleted. Preserve its cache and avoid live content
  requests. A course code may identify several enrollments; never silently select one.
- Keep persisted content paths relative for portability and emitted paths absolute for consumers.
  Preserve cache locking and atomic publication for concurrent CLI processes. Formatting must not
  modify stored records or expose internal `_canvas*` metadata.
- Empty Home content is valid. Calendar/To-Do/upcoming feeds omit deadlines embedded in course
  content and are insufficient on their own for deadline audits.

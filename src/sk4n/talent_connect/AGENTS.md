# TalentConnect maintenance

Use `client.py` for public data, `authenticated_client.py` for Kinobi's in-page client, and
`storage.py` for persistence. Keep authenticated requests in the saved browser context; do not
extract or print its token. Company data is public; company commands also fetch jobs, which explains
their default authentication.

- Offer queries must include the signed-in user's `applicant_ids`. Without it, Kinobi returns
  institution-wide offers. Fail closed if the user's ID is unavailable.
- Job-list `applied` state and profile application/offer workflow tabs are different sources. Keep
  their semantics separate; workflow endpoints contain incomplete nested jobs and require detail
  enrichment, even when ordinary list searches skip details.
- Kinobi ignores attempted qualification, posted-date, and sort parameters. Filter qualification
  from `is_unqualified_student`. For posted dates, scan all remote matches before local filtering
  and result limits; remote ordering cannot justify an early stop.
- List/detail fields differ even when `updated_at` agrees. Track freshness against the timestamp
  saved with the last detail fetch, and retain detail-only fields during later list upserts. A local
  payload fingerprint alone cannot prove detail freshness.
- NUS academic-major restriction IDs are tenant-specific. `/api/major-list/all` supplies generic
  names and cannot resolve them; no general tenant lookup is known. Preserve opaque restrictions and
  Kinobi's qualification result rather than inventing labels.
- Empty workflow tabs are valid results. Output filters such as `--updated-only` must not reduce
  persistence coverage; compact output must not discard stored detail.

# Outlook web inspection

Run `nus-outlook auth status` first. When needed, the user completes NUS SSO and MFA via `nus-outlook auth login`; existing authorization to sign in remains valid.

Prefer `nus-outlook api '/owa/service.svc?action=GetOwaUserConfiguration' -X POST` for endpoint checks. Supported actions are `GetOwaUserConfiguration`, `FindFolder`, `GetFolder`, `FindItem`, `GetItem`, `GetAttachment`, `FindConversation`, `GetConversationItems`, `GetCalendarFoldersV2`, and `GetCalendarView`. POST carries the Outlook JSON read request, using the `Header`/`Body` envelope; it does not imply a mailbox write. Request types must match the selected action.

The following contracts were checked with read-only requests on 2026-11-03:

- `FindItem` returns `ItemId.Id` and `ItemId.ChangeKey`. `GetItem` with
  `ItemShape.BaseShape: IdOnly` also returns that revision without the body.
  `AllProperties`, `BodyType: Text`, and `BlockExternalImages: true` return the
  full text body, `LastModifiedTime`, attachment metadata, and reply/conversation
  fields. Request item IDs as `{"__type": "ItemId:#Exchange", "Id": ID}`;
  search's complete ID object is not a valid request ID shape.
- Search exposes `DateTimeLastModified` with fractional seconds while full
  `GetItem` may round `LastModifiedTime` to whole seconds. Its `ItemId` may lack a
  change key. Use the lightweight revision check before reusing full content.
  Attempts to request `LastModifiedTime` or `DateTimeLastModified` as ordinary
  `PropertyUri` fields failed; they are not used by incremental checks.
- Full messages expose `Attachments[]` with `AttachmentId.Id`, `Name`, `Size`,
  `ContentType`, `LastModifiedTime`, and `__type` when available. `GetAttachment`
  takes `Body.AttachmentIds: [{"__type": "AttachmentId:#Exchange", "Id": ID}]`
  and returns `Body.ResponseMessages.Items[].Attachments[]` with base64 `Content`
  for files. Omit `AttachmentShape`: the tested shape variants returned HTTP
  400. Metadata and downloaded response fields are retained in the cache,
  storing decoded bytes separately from JSON. File downloads were checked live;
  item/MIME and reference handling also have deterministic unit coverage.
- A missing/malformed folder can produce an Exchange `ResponseCode` such as
  `ErrorFolderNotFound` or `ErrorInvalidIdMalformed`. Search can wrap it in HTTP
  400; `GetFolder` with `BaseShape: IdOnly` distinguishes a stale folder mapping
  from other search failures before retrying. Retries refresh the mapping once.

Read-only checks on 2026-11-04 confirmed that a single `FindItem` page returns
metadata without `Body`, while a single search returns metadata and `Preview`
without `Body`. Default high-level lists/searches cache those returned records
without any per-result `GetItem` calls. Only `message ID`, `--body`, or
`--attachments` opts into detail requests and body-revision validation.
The cache stores emails by ID rather than query strings or result snapshots.
Offline text searches use local JSON with `jq`/`grep`; `messages --no-refresh`
can filter and page cached metadata without a matching prior query.

Link contracts checked on 2026-11-05:

- Full `GetItem` includes `WebClientReadFormQueryString`, an Outlook web URL.
  Lists/searches do not need another request to generate their normalized
  message `url`: `/mail/deeplink/read/{encoded-message-id}`. IDs are encoded as
  individual path segments, including `/`, `+`, and `=` characters.
- `GetAttachment` returned an empty `AttachmentOriginalUrl` for the file checked.
  Outlook's loaded `previewAttachmentInPopOut` implementation constructs
  `/mail/deeplink/attachment/{encoded-message-id}/{encoded-attachment-id}/false/`.
  This authenticated viewer route opened the correct attachment in a live
  browser check. Message deep links were also checked, preserving read state.
  These viewer links carry IDs rather than expiring download credentials.
- High-level records save mail `url`, `Attachments[].url`, and downloaded-file
  manifest `url` fields. `_outlook.attachments[]` exposes `url` together with
  `path` and download status, including metadata-only attachments. Normal reads
  enrich older cached records locally; cache-only reads derive links without
  writing. Agent replies cite mail URLs and attachment URL/local-file pairs.

Mailbox search and unread filtering use `POST /searchservice/api/v2/query`,
with `EntityRequests[].EntityType` set to `Message`, `Query.QueryString`, and
`Filter.Term.DistinguishedFolderName` or `Filter.Term.FolderId`. Results retain
the `Source` fields, but normalize `ItemId.Id` from `ImmutableId` to the stable
Exchange ID. The folder-dependent search ID remains in `ItemRestId` and `Id`.
Outlook's `ImmutableId` alphabet maps `-` to `/` and `_` to `+`. The older `FindItem`
query/restriction shapes did not produce valid search results in live checks.

Directory lookup uses `POST /search/api/v2/query` with
`EntityRequests[].EntityType` set to `People`, `ContentSources: ["Exchange"]`,
`Provenances: ["Directory"]`, `Query.QueryString`, and `Size`. The filter selects
`PeopleType: Person` and organization users/contacts. This is the endpoint
observed in Outlook's People search; the legacy `ResolveNames` OWA action failed
in live checks and is not supported by this CLI. Results retain each `Source`
record, including `DisplayName`, `EmailAddresses`, `JobTitle`, `Department`, and
`CompanyName` where available. Addresses can include aliases, and different
people can share a name. Return candidates and their directory-provided context
instead of inventing addresses or guessing course roles. Refine a query when
the requested limit or server cap may omit the intended match.

For page or network investigation:

```bash
nus-outlook playwright-cli --session nus-outlook
playwright-cli -s=nus-outlook snapshot
playwright-cli -s=nus-outlook requests
playwright-cli -s=nus-outlook close
```

The command restores private cookies, local storage, and IndexedDB in a named `@playwright/cli` session. Use named high-level commands for authorized mailbox changes; use this session to inspect page behavior or investigate a changed endpoint. Use `--headed` when a visible window helps. Do not reuse an existing unrelated session or print authorization headers, token responses, or storage contents. Avoid opening messages through the page when read-state changes would be unwanted; use `message` instead.

Outlook currently redirects older `outlook.office.com` links to `outlook.cloud.microsoft`. NUS login can involve Microsoft account discovery followed by NUS federation and MFA. A realm-forced link can cause an extra account-discovery round trip; the CLI uses the standard mailbox URL and remembers the sign-in hint. A renewed session is saved only after the server returns a signed-in mailbox profile. If silent renewal fails, use explicit interactive login.

These web endpoints are private Outlook implementation details and may change. Inspect the smallest read response and update the package when their contract changes. Always close the named inspection browser when finished.

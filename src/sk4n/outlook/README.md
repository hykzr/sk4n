# NUS Outlook

Sign in once in the browser and complete NUS SSO and MFA:

```bash
nus-outlook auth login
# Optional: preselect the Microsoft sign-in account.
nus-outlook auth login --login-hint e1234567@u.nus.edu
nus-outlook auth status --format json
```

The CLI saves private browser state outside the checkout, including IndexedDB, and uses direct
Outlook web requests for mailbox reads. When a token expires, it tries a fresh headless browser to
renew it from that state. NUS/Microsoft can still require another interactive login or MFA.
`auth login --refresh` starts a fresh sign-in, and `auth logout` removes only the CLI's saved
session.

Use `NUS-ID@u.nus.edu` for student sign-in addresses and `NUS-ID@nus.edu.sg` for staff sign-in
addresses.

```bash
nus-outlook folders --format json
nus-outlook messages --limit 10
nus-outlook messages --unread --limit 10 --format json
nus-outlook messages --folder sentitems --offset 20 --limit 10
nus-outlook messages --folder 'Sent Items' --limit 10
nus-outlook messages --folder-id 'FOLDER_ID' --limit 10
nus-outlook search 'subject:internship' --limit 10 --format json
nus-outlook search 'subject:internship' --limit 5 --body --format json
nus-outlook message 'MESSAGE_ID' --format json
nus-outlook message 'MESSAGE_ID' --attachments
nus-outlook messages --limit 10 --no-refresh
nus-outlook people 'INSTRUCTOR NAME' --limit 10 --format json
```

When the user explicitly requests a mailbox change, the named commands act on one message and then
refresh its cached record:

```bash
nus-outlook flag 'MESSAGE_ID'
nus-outlook unflag 'MESSAGE_ID'
nus-outlook mark-read 'MESSAGE_ID'
nus-outlook mark-unread 'MESSAGE_ID'
nus-outlook move 'MESSAGE_ID' --folder Archive
nus-outlook draft create --to person@example.com --subject 'Draft subject' --body 'Unsent text'
nus-outlook draft reply 'MESSAGE_ID' --body 'Unsent reply text'
nus-outlook draft edit 'DRAFT_ID' --body-file reply.txt --attach preview.png
nus-outlook preview 'DRAFT_ID' --output preview.png --format json
# After the agent reviews the draft and the user approves (or explicitly waives their review):
nus-outlook draft send 'DRAFT_ID'
nus-outlook draft remove 'DRAFT_ID'
```

`draft create`, `reply`, and `edit` accept repeatable `--attach PATH` options. Create/edit accept
repeatable `--to`, `--cc`, and `--bcc`; edit replaces only specified recipient lists, with
`--clear-to`, `--clear-cc`, and `--clear-bcc` to empty them. `--body` or UTF-8 `--body-file`
replaces the entire plain-text body when editing, including any quoted original. Unspecified fields
and existing attachments are retained. Attachment files are validated before any mailbox changes; an
upload failure can still leave a saved/partially edited draft. `draft remove` moves the draft to
Deleted Items.

Sending requires an existing, unsubmitted draft ID and user authorization. Always draft first and
personally review recipients (including Cc/Bcc), subject, full body, attachments, and a fresh
`preview`. Show the draft's Outlook link and preview and wait for approval unless the user
explicitly waived their review. After edits, repeat the applicable review/approval steps. A waiver
never skips the agent's own review. `draft send` reports `status: submitted` when Outlook accepts
the submission; this does not confirm delivery. A briefly pending item can still have
`IsDraft: true` and `IsSubmitted: true`; it cannot be sent again. The cache keeps the actual
returned folder until Outlook moves it to Sent Items. If the follow-up read fails, submission
success is returned with a warning and the stale cache is invalidated. Check Sent Items before
retrying an uncertain send.

`preview ID` captures the actual Outlook web UI for drafts and received/sent mail in a temporary
authenticated headless browser. It blocks mailbox write requests, including autosave and read-state
changes, and closes that browser. Drafts open in the editor to show recipients and attachment tiles.
The command returns `_outlook.preview_path`, the message `url`, full body, and metadata. The PNG is
always cached as `preview.png` beside that email's `message.json`. Default reads validate the
message revision and reuse an unchanged, intact preview without opening a browser;
`_outlook.preview_reused` reports reuse. `--refresh` forces recapture; `--no-refresh` requires a
valid local message and preview and never contacts Outlook. `--output PATH.png` exports a copy while
retaining the canonical cached PNG. `_outlook.preview_path` always names that cached PNG;
`_outlook.preview_output_path` names the optional export. Every named mailbox write invalidates its
cached preview, even if the server leaves the change key unchanged. Draft edits refresh the message
body/metadata; uncertain writes or failed follow-up reads invalidate the stale message too.
Screenshots show the current viewport, so review the returned full body and all
recipients/attachments as well. A changed revision during capture fails without replacing the
previous screenshot.

Read `IsRead` and `Flag.FlagStatus` in JSON to check message state. `messages` requests `Flag`
explicitly; older cached records without it need a refresh.

Use `ItemId.Id` from message results and `FolderId.Id` from folder results. Message reads preserve
the read flag. Search defaults to the inbox; select another folder with `--folder`. Names are
case-insensitive. Duplicate folder names require a full path such as `Projects/Reports` or
`--folder-id ID`. `--folder` selects names/paths or distinguished names; `--folder-id` selects an
exact ID and bypasses name lookup. These options are mutually exclusive. With neither option, the
inbox is selected. Common distinguished names (`inbox`, `sentitems`, `drafts`, `archive`,
`deleteditems`) can be passed directly without listing folders first. Search covers the selected
folder only.

`--format json` returns compact Outlook fields; JSONL uses the same detail, one record per line.
Mail retains `ItemId.Id`, subject, URL, sender/recipients, dates, read/flag/draft state, body or
preview, attachment metadata, and action results. Exchange transport headers, duplicate identifiers,
permissions, and rendering metadata are omitted. HTML bodies become text with link targets
preserved. Folders retain names, IDs, types, and message counts; directory results retain
identity/contact and affiliation fields. `--format json-full` returns full Outlook records,
including `ItemId.ChangeKey` and `InternetMessageHeaders`. The low-level `api` command outputs the
returned response data without field filtering and does not accept `--format`. Cache files remain
complete regardless of output format.

Message output adds `_outlook.path` and `_outlook.attachments` with local paths and download status.
Search normalizes `ItemId.Id` to Outlook's stable immutable ID; the raw search ID remains in
`ItemRestId` and `Id` in json-full. `_outlook.body_downloaded` reports whether a complete body has
been cached; `_outlook.body_current` becomes false if later metadata shows that body is stale.

`_outlook.preview_path`, `preview_reused`, `preview_viewport_only`, and `preview_output_path`
describe the cached screenshot, whether it was reused, its viewport limitation, and any exported
copy.

Every email has a `url` for opening it in Outlook. Attachment metadata includes `Attachments[].url`,
and `_outlook.attachments[]` pairs each `url` with its local `path` when downloaded. These URLs are
saved in the cache as well; obtaining them does not require downloading attachment bytes. Cite mail
as `[subject](URL)`. Cite attachments as `[filename](URL) ([local](<ABSOLUTE_PATH>))` when the local
file exists, or `[filename](URL)` otherwise, matching Canvas's reference convention. Use the
returned `url` fields for references.

By default, `messages` makes one list request and `search` makes one search request (the result
limit defaults to 20). The live list API supplies metadata; search also supplies previews. Neither
supplies full bodies. These commands cache the returned records without making per-message requests,
including when the body was already cached. Use `message ID` to read one full body, or add `--body`
to `messages`/`search` to fetch bodies for the selected results. `--attachments` also opts into
fetching message details to discover attachments. Online full-message reads also fetch the folder
mapping if it is missing and the message has an opaque parent-folder ID. This keeps the message in
the same offline folder listing after its body is downloaded and repairs existing cached folder IDs
on the next online message read. Later reads reuse the saved mapping.

Folders, messages, searches, and individual messages use the same three refresh modes as Canvas:

| Mode           | Folder mapping                                                                                                                                      | Messages and attachments                                                                                                                                                                                                       |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Default        | `folders` always refreshes. Named filters reuse the mapping, refreshing once on a missing name or missing-folder response, then fail if unresolved. | Lists/searches cache returned metadata only. `message ID`, `--body`, or `--attachments` fetch details as needed and reuse complete unchanged bodies. Download attachment bytes only with `--attachments`.                      |
| `--no-refresh` | Use only the saved mapping; missing names fail.                                                                                                     | Read only cached data, without authentication or network requests. Listing applies local folder/unread/paging filters. Requested bodies/files must be cached and have no known revision change. Offline search is unsupported. |
| `--refresh`    | Refresh the mapping before resolving names; unresolved names fail. `--folder-id` skips name lookup.                                                 | Lists/searches refresh metadata. Force body fetching for `message ID` or with `--body`/`--attachments`. With `--attachments`, force attachment downloads too.                                                                  |

The default cache root is `<sk4n-home>/outlook`, shown by `sk4n paths`. Override it with
`nus-outlook --data-path PATH ...`. The layout is:

```text
outlook/
  folders.json
  emails/
    index.json
    <normalized-subject>--<message-id-hash>/
      message.json
      preview.png
      attachments/<normalized-name>--<attachment-id-hash>.<extension>
```

`message.json` stores the latest returned `metadata`, the last fetched `message` with its body when
downloaded, a `complete` flag, a `body_current` flag, folder context, downloaded-attachment
manifests, and a preview fingerprint/checksum. Message results combine the cached body with the
latest observed `IsRead` and `Flag` state, including offline reads. Offline reads do not rewrite the
cache. New metadata does not relabel an old body with a newer revision. New email directories and
attachment filenames use at most 40 normalized ASCII title characters and the first 8 bytes of the
SHA-256 ID hash (16 hex characters). On a name collision, only the new entry's hash grows, one byte
at a time, until unique. Existing indexed paths stay stable, including when a subject or filename
changes. Attachment extensions are kept separately from the bounded title. These shorter components
reduce Windows path lengths. Cache files are written atomically, with private permissions on POSIX
systems, and CLI writers share a cache lock.

Lists/searches cache records returned for the requested page, not the whole mailbox. Query strings
and result-page snapshots are not cached. Previously seen emails and downloaded bodies/files remain
cached when a later query has different results. `messages --no-refresh` lists known cached records,
sorting by received time and applying `--folder`, `--unread`, `--limit`, and `--offset` locally;
these need not match an earlier request. Its contents reflect only mail seen by the CLI, not a
complete or freshly reconciled mailbox. A cached direct message needs known folder context to appear
in a filtered local listing. Use JSON with `jq` or `grep` for offline text filtering:

```bash
nus-outlook messages --no-refresh --limit 100 --format json | jq '.[] | select((.Subject // "") | test("internship"; "i"))'
nus-outlook message 'MESSAGE_ID' --no-refresh --format json
```

Freshness uses Outlook's `ItemId.ChangeKey`. A cached individual-message read checks it with a
lightweight `GetItem` request; explicitly requesting bodies for search results lacking a change key
uses the same check. Default lists and searches never make these per-message revision requests.
`LastModifiedTime` is a fallback when comparable revision data exists. Received/sent dates alone
never establish freshness. Search dates can include fractional seconds that full-message dates omit.
Replies are separate items with their own `ItemId`, linked through fields such as `ConversationId`,
`InReplyTo`, and `References`; they are cached separately.

Attachment metadata is saved with the full message without downloading bytes. `--attachments` on
`messages`, `search`, or `message` downloads file attachments, including inline files. Attachment
timestamps and sizes determine reuse; when there is no attachment timestamp, reuse follows the
parent message revision. Missing or truncated local files are fetched again. Attached Outlook items
are saved as `.eml` when MIME is supplied, otherwise as JSON. Linked cloud/reference attachments
retain metadata and status `reference`; their external URLs are not downloaded. Nested attachment
bytes inside an attached item's JSON are not recursively fetched.

`people NAME` searches the NUS directory for a name or email address, including people you have not
emailed. Results include `DisplayName`, `EmailAddresses`, and available job title, department,
company, and office details. Matching names can refer to multiple people, so check those details or
refine the query before selecting an address. The directory may not identify someone's
course-specific role, and the requested result limit or a server cap can omit matches.

Advanced options precede the subcommand: `--data-path PATH`, `--timeout 60`, `--site-name NAME`, and
`--base-url URL`. Environment overrides are `OUTLOOK_TIMEOUT`, `OUTLOOK_SITE_NAME`,
`OUTLOOK_BASE_URL`, `SK4N_HOME`, and `SK4N_SESSION_DIR`.

```bash
nus-outlook api '/owa/service.svc?action=GetOwaUserConfiguration' -X POST
nus-outlook playwright-cli --session nus-outlook
playwright-cli -s=nus-outlook snapshot
playwright-cli -s=nus-outlook close
sk4n skills install --agents codex --skills nus-outlook --scope user
```

The low-level `api` command accepts supported read actions only. Named write commands use
item-scoped Outlook actions. They reread the changed item after a successful write, update its
folder and metadata in the cache, and return the current ID. If that reread fails, the stale cached
record is invalidated. Changes made outside these commands become visible to offline reads only
after the affected message is refreshed. Outlook web endpoints can change independently of this
package.

## Outlook action contracts observed in November 2026

- `FindItem` needs `AdditionalProperties: [{"FieldURI":"Flag", "__type":"PropertyUri:#Exchange"}]`
  to expose `Flag.FlagStatus`; full `GetItem` and search results include it. `IsRead` is already
  returned.
- Search `ItemId.Id`/`ItemRestId` is folder-dependent (`AQMk…`); `ImmutableId` is the stable `AAk…`
  ID in Outlook's alphabet (`-` means `/`, `_` means `+`). Normalize it before cache keys or item
  actions. Search omits `Flag` on unflagged mail. `GetItem` accepts REST IDs only with
  `Prefer: IdType="ImmutableId"`; folder lists return stable IDs without that header.
- Item-scoped `UpdateItem` with `SetItemField` paths `Flag` or `IsRead` updates one email. Outlook's
  list UI instead uses `ApplyConversationAction`, which can affect a whole conversation. Read state
  can change without a new `ChangeKey`, so reread the full item after every successful write.
- `MoveItem` returns the moved item in `Items[]`; in the tested mailbox `ItemId.Id` remained stable
  while `ChangeKey` and `ParentFolderId` changed. Handle a changed ID anyway. The UI move response
  uses `MovedItemIds[]`.
- `CreateItem` with `MessageDisposition: SaveOnly` and a `Message` creates an unsent draft; a
  `ReplyToItem` with `ReferenceItemId` and `NewBodyContent` creates a reply draft with conversation
  linkage and quoted original text. `DeleteItem` with `DeleteType: MoveToDeletedItems` discards a
  draft and returns `MovedItemId`.
- On 2026-11-08, draft edits used `UpdateItem`, `SetItemField` paths `Subject`, `Body`, and
  recipient lists, with `MessageDisposition: SaveOnly`. `CreateAttachment` accepts `ParentItemId`
  and `FileAttachment` entries with `Name`, `ContentType`, `IsInline: false`, and base64 `Content`;
  its response includes `AttachmentId.RootItemChangeKey`. The UI's binary
  `CreateAttachmentFromLocalFile` route carries the same JSON contract in `x-owa-urlpostdata`; the
  CLI's JSON upload was checked live with PNG bytes.
- The UI sends with `UpdateItem` and `MessageDisposition: SendAndSaveCopy`; its success response has
  `Items: null` and `ResponseCode: NoError`. A no-op `Subject` update with the draft ID/change key,
  `ConflictResolution: NeverOverwrite`, and `SendOnNotFoundError: false` was verified live,
  preserving the body and attachment bytes. The immutable ID remained stable. Immediately afterward
  `GetItem` can return `IsSubmitted: true` while `IsDraft` and folder still reflect Drafts; a later
  read confirms `IsDraft: false` in Sent Items. Never treat a post-submission read failure as a
  failed send.
- `/mail/deeplink/read/{id}` opens a saved draft with `Continue editing`; that button exposes the
  composer, recipient wells and attachment options. These mount asynchronously after the editor.
  Preview waits for them, blocks service writes, and verifies the same message revision afterward.
  Sent mail uses the reading view.
- Trace request bodies may be stored by SHA-1 under `.playwright-cli/traces/resources/` rather than
  printed by `request-body`; inspect only the needed JSON payload. Trace network logs also contain
  credentials, so never print whole logs or request headers.

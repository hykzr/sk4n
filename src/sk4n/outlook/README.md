# NUS Outlook

Sign in once in the browser and complete NUS SSO and MFA:

```bash
nus-outlook auth login
# Optional: preselect the Microsoft sign-in account.
nus-outlook auth login --login-hint e1234567@u.nus.edu
nus-outlook auth status --format json
```

The CLI saves private browser state outside the checkout, including IndexedDB,
and uses direct Outlook web requests for mailbox reads. When a token expires,
it tries a fresh headless browser to renew it from that state. NUS/Microsoft can
still require another interactive login or MFA. `auth login --refresh` starts
a fresh sign-in, and `auth logout` removes only the CLI's saved session.

Use `NUS-ID@u.nus.edu` for student sign-in addresses and `NUS-ID@nus.edu.sg`
for staff sign-in addresses.

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

When the user explicitly requests a mailbox change, the named commands act on
one message and then refresh its cached record:

```bash
nus-outlook flag 'MESSAGE_ID'
nus-outlook unflag 'MESSAGE_ID'
nus-outlook mark-read 'MESSAGE_ID'
nus-outlook mark-unread 'MESSAGE_ID'
nus-outlook move 'MESSAGE_ID' --folder Archive
nus-outlook draft create --to person@example.com --subject 'Draft subject' --body 'Unsent text'
nus-outlook draft reply 'MESSAGE_ID' --body 'Unsent reply text'
nus-outlook draft remove 'DRAFT_ID'
```

`draft remove` moves the draft to Deleted Items. There is no send command.
Read `IsRead` and `Flag.FlagStatus` in JSON to check message state. `messages`
requests `Flag` explicitly; older cached records without it need a refresh.

Use `ItemId.Id` from message results and `FolderId.Id` from folder results.
Message reads preserve the read flag. Search defaults to the inbox; select
another folder with `--folder`. Names are case-insensitive. Duplicate folder
names require a full path such as `Projects/Reports` or `--folder-id ID`.
`--folder` selects names/paths or distinguished names; `--folder-id` selects an
exact ID and bypasses name lookup. These options are mutually exclusive. With
neither option, the inbox is selected. JSON output retains Outlook fields and adds
`_outlook.path` and `_outlook.attachments` with local paths and download status.
Search normalizes `ItemId.Id` to Outlook's stable immutable ID; the raw search
ID remains in `ItemRestId` and `Id`.
`_outlook.body_downloaded` reports whether a complete body has been cached;
`_outlook.body_current` becomes false if later metadata shows that body is stale.

Every email has a `url` for opening it in Outlook. Attachment metadata includes
`Attachments[].url`, and `_outlook.attachments[]` pairs each `url` with its local
`path` when downloaded. These URLs are saved in the cache as well; obtaining
them does not require downloading attachment bytes. Cite mail as
`[subject](URL)`. Cite attachments as `[filename](URL) ([local](<ABSOLUTE_PATH>))`
when the local file exists, or `[filename](URL)` otherwise, matching Canvas's
reference convention. Use the returned `url` fields for references.

By default, `messages` makes one list request and `search` makes one search
request (the result limit defaults to 20). The live list API supplies metadata;
search also supplies previews. Neither supplies full bodies. These commands
cache the returned records without making per-message requests, including when
the body was already cached. Use `message ID` to read one full body, or add
`--body` to `messages`/`search` to fetch bodies for the selected results.
`--attachments` also opts into fetching message details to discover attachments.

Folders, messages, searches, and individual messages use the same three refresh
modes as Canvas:

| Mode | Folder mapping | Messages and attachments |
| --- | --- | --- |
| Default | `folders` always refreshes. Named filters reuse the mapping, refreshing once on a missing name or missing-folder response, then fail if unresolved. | Lists/searches cache returned metadata only. `message ID`, `--body`, or `--attachments` fetch details as needed and reuse complete unchanged bodies. Download attachment bytes only with `--attachments`. |
| `--no-refresh` | Use only the saved mapping; missing names fail. | Read only cached data, without authentication or network requests. Listing applies local folder/unread/paging filters. Requested bodies/files must be cached and have no known revision change. Offline search is unsupported. |
| `--refresh` | Refresh the mapping before resolving names; unresolved names fail. `--folder-id` bypasses mapping requests. | Lists/searches refresh metadata. Force body fetching for `message ID` or with `--body`/`--attachments`. With `--attachments`, force attachment downloads too. |

The default cache root is `<sk4n-home>/outlook`, shown by `sk4n paths`.
Override it with `nus-outlook --data-path PATH ...`. The layout is:

```text
outlook/
  folders.json
  emails/
    index.json
    <normalized-subject>--<message-id-hash>/
      message.json
      attachments/<normalized-name>--<attachment-id-hash>.<extension>
```

`message.json` stores the latest returned `metadata`, the last fetched `message`
with its body when downloaded, a `complete` flag, a `body_current` flag, folder
context, and the downloaded-attachment manifest. New metadata does not relabel
an old body with a newer revision. New email directories and attachment
filenames use at most 40 normalized ASCII title characters and the first
8 bytes of the SHA-256 ID hash (16 hex
characters). On a name collision, only the new entry's hash grows, one byte at
a time, until unique. Existing indexed paths stay stable, including when a
subject or filename changes. Attachment extensions are kept separately from
the bounded title. These shorter components reduce Windows path lengths.
Cache files are written atomically, with private permissions on POSIX systems,
and CLI writers share a cache lock.

Lists/searches cache records returned for the requested page, not the whole
mailbox. Query strings and result-page snapshots are not cached. Previously
seen emails and downloaded bodies/files remain cached when a later query has
different results. `messages --no-refresh` lists known cached records, sorting
by received time and applying `--folder`, `--unread`, `--limit`, and `--offset`
locally; these need not match an earlier request. Its contents reflect only mail
seen by the CLI, not a complete or freshly reconciled mailbox. A cached direct
message needs known folder context to appear in a filtered local listing.
Use JSON with `jq` or `grep` for offline text filtering:

```bash
nus-outlook messages --no-refresh --limit 100 --format json | jq '.[] | select((.Subject // "") | test("internship"; "i"))'
nus-outlook message 'MESSAGE_ID' --no-refresh --format json
```

Freshness uses Outlook's `ItemId.ChangeKey`. A cached individual-message read
checks it with a lightweight `GetItem` request; explicitly requesting bodies
for search results lacking a change key uses the same check. Default lists and
searches never make these per-message revision requests. `LastModifiedTime` is
a fallback when comparable revision
data exists. Received/sent dates alone never establish freshness. Search dates
can include fractional seconds that full-message dates omit. Replies are separate
items with their own `ItemId`, linked through fields such as `ConversationId`,
`InReplyTo`, and `References`; they are cached separately.

Attachment metadata is saved with the full message without downloading bytes.
`--attachments` on `messages`, `search`, or `message` downloads file attachments,
including inline files. Attachment timestamps and sizes determine reuse; when
there is no attachment timestamp, reuse follows the parent message revision.
Missing or truncated local files are fetched again. Attached Outlook items are
saved as `.eml` when MIME is supplied, otherwise as JSON. Linked cloud/reference
attachments retain metadata and status `reference`; their external URLs are not
downloaded. Nested attachment bytes inside an attached item's JSON are not
recursively fetched.

`people NAME` searches the NUS directory for a name or email address, including
people you have not emailed. Results include `DisplayName`, `EmailAddresses`,
and available job title, department, company, and office details. Matching names
can refer to multiple people, so check those details or refine the query before
selecting an address. The directory may not identify someone's course-specific
role, and the requested result limit or a server cap can omit matches.

Advanced options precede the subcommand: `--data-path PATH`, `--timeout 60`, `--site-name NAME`,
and `--base-url URL`. Environment overrides are `OUTLOOK_TIMEOUT`,
`OUTLOOK_SITE_NAME`, `OUTLOOK_BASE_URL`, `SK4N_HOME`, and `SK4N_SESSION_DIR`.

```bash
nus-outlook api '/owa/service.svc?action=GetOwaUserConfiguration' -X POST
nus-outlook playwright-cli --session nus-outlook
playwright-cli -s=nus-outlook snapshot
playwright-cli -s=nus-outlook close
sk4n skills install --agents codex --skills nus-outlook --scope user
```

The low-level `api` command accepts supported read actions only. Named write
commands use item-scoped Outlook actions. They reread the changed item after a
successful write, update its folder and metadata in the cache, and return the
current ID. If that reread fails, the stale cached record is invalidated.
Changes made outside these commands become visible to offline reads only after
the affected message is refreshed.
Outlook web endpoints can change independently of this package.


## Outlook action contracts observed in November 2026

- `FindItem` needs `AdditionalProperties: [{"FieldURI":"Flag", "__type":"PropertyUri:#Exchange"}]` to expose `Flag.FlagStatus`; full `GetItem` and search results include it. `IsRead` is already returned.
- Search `ItemId.Id`/`ItemRestId` is folder-dependent (`AQMk…`); `ImmutableId` is the stable `AAk…` ID in Outlook's alphabet (`-` means `/`, `_` means `+`). Normalize it before cache keys or item actions. Search omits `Flag` on unflagged mail. `GetItem` accepts REST IDs only with `Prefer: IdType="ImmutableId"`; folder lists return stable IDs without that header.
- Item-scoped `UpdateItem` with `SetItemField` paths `Flag` or `IsRead` updates one email. Outlook's list UI instead uses `ApplyConversationAction`, which can affect a whole conversation. Read state can change without a new `ChangeKey`, so reread the full item after every successful write.
- `MoveItem` returns the moved item in `Items[]`; in the tested mailbox `ItemId.Id` remained stable while `ChangeKey` and `ParentFolderId` changed. Handle a changed ID anyway. The UI move response uses `MovedItemIds[]`.
- `CreateItem` with `MessageDisposition: SaveOnly` and a `Message` creates an unsent draft; a `ReplyToItem` with `ReferenceItemId` and `NewBodyContent` creates a reply draft with conversation linkage and quoted original text. `DeleteItem` with `DeleteType: MoveToDeletedItems` discards a draft and returns `MovedItemId`. Sending has no high-level support.
- Trace request bodies may be stored by SHA-1 under `.playwright-cli/traces/resources/` rather than printed by `request-body`; inspect only the needed JSON payload. Trace network logs also contain credentials, so never print whole logs or request headers.


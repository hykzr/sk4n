# Outlook maintenance

Outlook web endpoints are private and can change independently of this package. Start with
`client.py` for reads, `actions.py` for item writes, `fetcher.py`/`storage.py` for cache semantics,
and `preview.py` for guarded UI capture. When updating write access commands, capture the exact
shape from playwright before constructing api calls

## Remote contracts and reasons

- Mail search and directory lookup use different search endpoints. Legacy `FindItem`
  query/restriction shapes did not work for mail search; `ResolveNames` did not work for directory
  lookup. Do not replace the search routes with those actions without live validation.
- Search REST IDs vary by folder. Normalize `ImmutableId` before cache keys/actions; Outlook's
  alphabet maps `-` to `/` and `_` to `+`, unlike standard base64url. `GetItem` needs
  `Prefer: IdType="ImmutableId"` for those IDs. Request IDs must use the action's ID shape, not a
  whole search result object.
- `FindItem` needs an explicit `Flag` additional property; search can omit it for unflagged mail.
  Use item-scoped `UpdateItem`, not the UI's conversation-wide action, for individual flags/read
  state. Successful writes can leave `ChangeKey` unchanged, so reread the item and invalidate
  previews.
- Prefer `ChangeKey` for revision checks. Search timestamps can have fractions that `GetItem` rounds
  away; received/sent dates cannot prove freshness. Attempts to request modified-time fields as
  ordinary `PropertyUri` additions failed.
- `GetAttachment` shape variants returned HTTP 400; omit `AttachmentShape`. Linked/reference
  attachments do not imply permission to download external cloud resources.
- Sending uses a saved draft with `UpdateItem`/`SendAndSaveCopy`, including a no-op subject update,
  `NeverOverwrite`, and `SendOnNotFoundError: false`. Acceptance can return no item; a subsequent
  read may still show Drafts/`IsDraft` with `IsSubmitted`. Submission is not delivery. Never report
  an accepted send as failed because cache reconciliation failed, or blindly retry it.
- Draft deep links initially show a reading view. The editor's recipients/attachment controls mount
  asynchronously after “Continue editing”. Preview must wait for them, block mailbox writes
  (including autosave/read-state changes), and verify the revision after capture.

## Persistence and investigation

- Preserve the distinction between latest metadata and the revision of a downloaded body. Default
  lists/searches deliberately avoid per-result body/revision requests. Offline coverage is only
  previously observed mail, not a mailbox snapshot or saved search.
- Keep indexed cache paths stable after subject/filename changes. Bounded names address Windows path
  limits; do not rebuild existing paths from current titles. Preserve private atomic writes and the
  shared writer lock.
- Saved authentication includes IndexedDB. Publish renewed state only after verifying a signed-in
  mailbox profile; forced federation URLs can add account-discovery loops. NUS/Microsoft may still
  require interactive MFA.
- Browser traces may keep request bodies under `.playwright-cli/traces/resources/` rather than in
  `request-body` output. Inspect only required payloads; full traces/headers can contain
  credentials. Normal UI mail views can mark messages read; prefer the CLI or guarded preview for
  passive reads.

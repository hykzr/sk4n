# Changelog

## Unreleased

### NUS Outlook (`nus-outlook`)

- Add the `nus-outlook` CLI and installable `nus-outlook` agent skill for reading, caching, and
  managing NUS Outlook mail.
- Persist NUS SSO/MFA browser authentication (including IndexedDB and student `@u.nus.edu` / staff
  `@nus.edu.sg` login hints) with silent token renewal.
- Incrementally cache folder mappings and returned mail metadata, with on-demand full bodies and
  attachments, single-request default lists/searches, folder-name/path and `--folder-id` selectors,
  stable Outlook web deep links, and `--refresh` / `--no-refresh` modes.
- Look up people's names, email addresses, and affiliation details in the NUS directory with
  `nus-outlook people`.
- Support authorized item-scoped mailbox management (`flag`, `unflag`, `mark-read`, `mark-unread`,
  `move`) and draft workflows (`draft create`, `reply`, `edit`, `send`, `remove`) with repeatable
  `--attach` file uploads and immediate cache reconciliation.
- Capture write-blocked Outlook web screenshots of drafts and messages with `nus-outlook preview`,
  caching revision-verified `preview.png` artifacts locally with optional `--output` export.
- Provide read-only `nus-outlook api` endpoint access and authenticated `nus-outlook playwright-cli`
  inspection sessions.

## [0.1.0]

- Initial public release.
- Provide Canvas, NUSMods, TalentConnect, and shared `sk4n` command-line tools.
- Bundle agent skills for the supported NUS services. Provide cli commands to skill management

[0.1.0]: https://github.com/hykzr/sk4n/releases/tag/v0.1.0

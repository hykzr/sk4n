# Changelog

## Unreleased

- Make `--format json` and `jsonl` more compact across all four services. Use `json-full` for full
  records; saved data is unchanged. Direct API output remains unfiltered.
- Add NUS Outlook tools and an assistant skill: sign in with NUS SSO/MFA, read/search mail without
  marking it read, download attachments, and look up NUS email addresses.
- Remember Outlook sign-in and renew it automatically when Microsoft permits; keep local mail copies
  for faster repeated reads and offline access.
- Support Outlook flags, read/unread changes, message moves, and drafts with attachments. Preview
  drafts before sending; sending requires an existing draft and user authorization.
- Add Outlook screenshot previews, direct read-only API access, and signed-in browser inspection.

## [0.1.0]

- Initial release with Canvas, NUSMods, TalentConnect, and shared `sk4n` tools.
- Include assistant skills and commands to install and manage them.

[0.1.0]: https://github.com/hykzr/sk4n/releases/tag/v0.1.0

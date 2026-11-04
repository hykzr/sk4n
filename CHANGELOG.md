# Changelog

## Unreleased

- Incrementally cache Outlook folder mappings and returned mail metadata, with
  on-demand bodies and attachments, single-request default lists/searches,
  folder-name filters, and consistent `--refresh` /
  `--no-refresh` modes.
- Add `nus-outlook` mail commands and an installable NUS Outlook agent skill.
- Persist Outlook browser authentication, including IndexedDB, with silent
  token renewal and explicit NUS SSO/MFA login.
- Look up people's names and email addresses in the NUS directory with
  `nus-outlook people`; clarify student/staff sign-in addresses and require
  browser UI for authorized writes.

## 0.1.0

- Initial public release.
- Provide Canvas, NUSMods, TalentConnect, and shared `sk4n` command-line tools.
- Bundle agent skills for the supported NUS services. Provide cli commands to skill management

[0.1.0]: https://github.com/hykzr/sk4n/releases/tag/v0.1.0

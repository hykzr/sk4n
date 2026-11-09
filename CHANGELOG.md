# Changelog

## [Unreleased]

- Add `-v`/`--version` to all five commands, showing the package version and Python executable.
- Fix `sk4n doctor` false errors for pipx on MacOS for Python version check

## [0.1.1] - 2026-11-10

### NUS Outlook

- Add NUS Outlook tools and an assistant skill to read/search mail without marking it read, download
  attachments, find NUS email addresses, and manage messages and drafts. Preview drafts with
  attachments before authorized sending.
- Remember NUS SSO/MFA sign-in and save mail for faster reads and offline access. Advanced tools
  provide screenshot previews, read-only API access, and signed-in browser inspection.

### Other improvements

- Update dependencies to fix reported security vulnerabilities.
- Make `--format json` and `jsonl` more compact across all four services. Use `json-full` for full
  records; saved data is unchanged. Direct API output remains unfiltered.
- Convert HTML to readable text in compact output. Add Canvas `--original-html` to preserve source
  HTML when needed.

## [0.1.0]

- Initial release with Canvas, NUSMods, TalentConnect, and shared `sk4n` tools.
- Include assistant skills and commands to install and manage them.

[0.1.1]: https://github.com/hykzr/sk4n/releases/tag/v0.1.1
[0.1.0]: https://github.com/hykzr/sk4n/releases/tag/v0.1.0

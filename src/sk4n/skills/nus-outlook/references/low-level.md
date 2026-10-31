# Outlook web inspection

Run `nus-outlook auth status` first. When needed, the user completes NUS SSO and MFA via `nus-outlook auth login`; existing authorization to sign in remains valid.

Prefer `nus-outlook api '/owa/service.svc?action=GetOwaUserConfiguration' -X POST` for endpoint checks. Supported actions are `GetOwaUserConfiguration`, `FindFolder`, `GetFolder`, `FindItem`, `GetItem`, `FindConversation`, `GetConversationItems`, `GetCalendarFoldersV2`, and `GetCalendarView`. POST carries the Outlook JSON read request, using the `Header`/`Body` envelope; it does not imply a mailbox write. Request types must match the selected action.

Mailbox search and unread filtering use `POST /searchservice/api/v2/query`,
with `EntityRequests[].EntityType` set to `Message`, `Query.QueryString`, and
`Filter.Term.DistinguishedFolderName` or `Filter.Term.FolderId`. Results retain
the original `Source` record, including `ItemId.Id`. The older `FindItem`
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

The command restores private cookies, local storage, and IndexedDB in a named `@playwright/cli` session. Use `--headed` for authorized UI writes so the action can be reviewed in the browser; for read-only inspection, use it when a visible window helps. Do not reuse an existing unrelated session or print authorization headers, token responses, or storage contents. Avoid opening messages through the page when read-state changes would be unwanted; use `message` instead.

Outlook currently redirects older `outlook.office.com` links to `outlook.cloud.microsoft`. NUS login can involve Microsoft account discovery followed by NUS federation and MFA. A realm-forced link can cause an extra account-discovery round trip; the CLI uses the standard mailbox URL and remembers the sign-in hint. A renewed session is saved only after the server returns a signed-in mailbox profile. If silent renewal fails, use explicit interactive login.

These web endpoints are private Outlook implementation details and may change. Inspect the smallest read response and update the package when their contract changes. Always close the named inspection browser when finished.

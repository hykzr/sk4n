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
nus-outlook search 'subject:internship' --limit 10 --format json
nus-outlook message 'MESSAGE_ID' --format json
nus-outlook people 'INSTRUCTOR NAME' --limit 10 --format json
```

Use `ItemId.Id` from message results and `FolderId.Id` from folder results.
Message reads preserve the read flag. Search defaults to the inbox; select
another folder with `--folder`. JSON output retains original Outlook fields.

`people NAME` searches the NUS directory for a name or email address, including
people you have not emailed. Results include `DisplayName`, `EmailAddresses`,
and available job title, department, company, and office details. Matching names
can refer to multiple people, so check those details or refine the query before
selecting an address. The directory may not identify someone's course-specific
role, and the requested result limit or a server cap can omit matches.

Advanced options precede the subcommand: `--timeout 60`, `--site-name NAME`,
and `--base-url URL`. Environment overrides are `OUTLOOK_TIMEOUT`,
`OUTLOOK_SITE_NAME`, `OUTLOOK_BASE_URL`, `SK4N_HOME`, and `SK4N_SESSION_DIR`.

```bash
nus-outlook api '/owa/service.svc?action=GetOwaUserConfiguration' -X POST
nus-outlook playwright-cli --session nus-outlook
playwright-cli -s=nus-outlook snapshot
playwright-cli -s=nus-outlook close
sk4n skills install --agents codex --skills nus-outlook --scope user
```

The low-level API accepts supported read actions only. The integration does not
send, delete, move, or mark mail. It uses Outlook web endpoints, which can change
independently of this package.

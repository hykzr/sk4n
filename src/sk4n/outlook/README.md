# NUS Outlook

Read NUS mail, download attachments, find NUS email addresses, and manage messages or drafts.
Listing and reading messages leave them unread if they were unread before.

## Sign in

```bash
nus-outlook auth login # Complete NUS SSO and MFA in the browser
nus-outlook auth status # Check the saved sign-in
nus-outlook auth logout # Forget this tool's sign-in
```

If account selection needs help, add `--login-hint NUS-ID@u.nus.edu` for a student account or
`NUS-ID@nus.edu.sg` for staff. The saved sign-in renews automatically when Microsoft permits; fresh
MFA may still be required. `auth login --refresh` starts a fresh sign-in.

## Read mail and find people

```bash
nus-outlook folders # List mailbox folders and their IDs
nus-outlook messages --unread --limit 10 # List unread messages
nus-outlook search 'subject:internship' --limit 10 # Search the selected mail folder
nus-outlook message 'MESSAGE_ID' # Read one full message
nus-outlook people 'INSTRUCTOR NAME' --limit 10 # Look up NUS directory email addresses
```

Copy message IDs from the results and keep the quotes. Lists/searches show summaries; add `--body`
for full text or `--attachments` to download attachments. Returned links open the email or
attachment in Outlook.

Mail defaults to the inbox, and search covers one selected folder. Use `--folder 'Sent Items'` for a
name or full path; `--folder-id ID` selects an exact folder when names are ambiguous. Directory
matches may share a name, so check their affiliation before choosing an address.

## Manage messages

These commands change one message in your mailbox:

```bash
nus-outlook flag 'MESSAGE_ID' # Add a follow-up flag
nus-outlook unflag 'MESSAGE_ID' # Remove the flag
nus-outlook mark-read 'MESSAGE_ID' # Mark the message read
nus-outlook mark-unread 'MESSAGE_ID' # Mark it unread
nus-outlook move 'MESSAGE_ID' --folder Archive # Move it to another folder
```

## Prepare and send drafts

Drafts stay unsent until you use `draft send`. Review recipients, subject, full text, and
attachments first. When an assistant prepares mail, it should show you the draft link and preview
for approval.

```bash
nus-outlook draft create --to person@example.com --subject 'Hello' --body 'Draft text' # Save a new draft
nus-outlook draft reply 'MESSAGE_ID' --body 'Reply text' # Save a reply draft
nus-outlook draft edit 'DRAFT_ID' --body-file reply.txt --attach report.pdf # Change a saved draft
nus-outlook preview 'DRAFT_ID' --output preview.png # Preview a draft; also works for received/sent mail
nus-outlook draft send 'DRAFT_ID' # Send the reviewed, approved draft
nus-outlook draft remove 'DRAFT_ID' # Move a draft to Deleted Items
```

Previews show only the visible part of long mail. Editing the body replaces all existing text,
including quoted replies. Unspecified fields and existing attachments remain; repeat `--attach` to
add several files. If an upload fails, inspect the saved draft before retrying. If sending has an
uncertain outcome, check Sent Items before trying again. Submission does not confirm delivery.

## Saved mail and more options

Normal reads check for updates and reuse unchanged bodies, attachments, and previews.
`messages --no-refresh` and `message ID --no-refresh` read saved mail only; this covers previously
seen messages, not your whole mailbox. Offline search is unavailable. Use `--refresh` when you need
to force another download or preview.

Run `sk4n paths` to locate saved mail and private sign-in files. Put `--data-path PATH` before the
subcommand to choose another cache folder. See `nus-outlook --help` and each command's `--help` for
all options, including JSON output. Advanced API/browser procedures are in the
[low-level guide](../skills/nus-outlook/references/low-level.md).

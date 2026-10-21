# Email alerts

After every hourly run, IhaveaJob sends **one email** summarising what changed since your last email. Nothing is sent when nothing changed.

Each email contains, per company:

- **New**: title, location, the employer's posting date when available, and the link.
- **Updated**: what changed, as old → new (title, location, department, employment type, apply link). For descriptions, the added (`+`) and removed (`−`) lines are shown.
- **No longer listed**: the job disappeared from the page you monitor, confirmed over several runs. It may have been filled or closed, or just moved.
- **Page text changed**: for pages where individual jobs can't be identified, the lines that appeared or disappeared.
- **Needs attention**: a page that has failed three runs in a row, for example because it now blocks automated access or moved. You're told once per failure streak.

**The first email** is a "monitoring started" message listing what's being watched. Jobs that existed before that are treated as a baseline and never emailed.

**Delivery guarantees:**
- Each change is emailed to each recipient at most once.
- If sending fails, nothing is marked as sent, so the next run retries.
- Only jobs that pass your filters (`keywords`, `locations` and so on) are included.

## Set up Gmail (free)

1. Turn on 2-Step Verification for the Google account that will *send* the alerts.
2. Create an App Password: **Google Account → Security → 2-Step Verification → App passwords**. Name it "IhaveaJob" and copy the 16-character password. Your normal password won't work, and should never be used here.
3. In the GitHub repository, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USERNAME` | the sending Gmail address |
| `SMTP_PASSWORD` | the App Password |
| `ALERT_EMAIL_TO` | where alerts go (comma-separate several addresses) |
| `SMTP_FROM` | optional, e.g. `IhaveaJob <sender@gmail.com>` |

4. Optional: under **Variables** (not secrets), add `IHAVEAJOB_TIMEZONE` = `America/Los_Angeles` so email times use your time zone.
5. Run **Actions → monitor → Run workflow**. You should receive the "monitoring started" email within a few minutes.

Gmail limits how much a personal account can send per day. Hourly digests to a few people are far below that, but don't use this to mass-mail.

Other providers work the same way. Set `SMTP_HOST` and `SMTP_PORT` to the provider's values: port 587 uses STARTTLS, and port 465 uses SSL.

## Local commands

```bash
export SMTP_HOST=smtp.gmail.com SMTP_USERNAME=... SMTP_PASSWORD=... ALERT_EMAIL_TO=...
ihaveajob test-email          # sends a short test message
ihaveajob notify --dry-run    # prints the next digest without sending or marking anything
ihaveajob notify              # sends it
```

## Privacy

Email addresses are only read from secrets or environment variables at send time. The database stores a one-way hash of each recipient, which is used to avoid duplicates, and never the address itself. That matters because the default database lives on a public branch.

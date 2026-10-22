# Web app: manage your careers pages

`dashboard/app.py` is a small Streamlit app. At the top, a headline sums up what's new since yesterday, and a 14-day activity strip shows which companies have been posting. Below that, you can:

- **Browse jobs.** Every job currently on watch, newest first. Search by title, location or team, filter by company, or show only new jobs.
- **Add a page.** Paste any careers link, optionally add keywords, locations or exclusions, and **Preview** what it finds before adding.
- **See all your pages**: how many jobs each one tracks, when it was last checked, how it's read, and any errors. You can **pause**, **resume** or **remove** a page.
- **Check all pages now**, without waiting for the hourly run.
- **See changes over time**: a daily chart plus a list of new, updated and removed jobs for the last day, week or month.

The app doesn't monitor anything itself. It edits `config/companies.yaml` in your GitHub repository, and the hourly GitHub workflow does the checking and emailing. The app reads results from the same database.

## Deploy it (free, about 10 minutes)

### 1. Create a GitHub token, limited to this one repository

1. Open **https://github.com/settings/personal-access-tokens/new** (fine-grained token).
2. **Token name:** `IhaveaJob web app`. **Expiration:** your choice. You'll need to make a new token when it expires.
3. **Repository access:** *Only select repositories* → `ankitdixit23/IhaveaJob`.
4. **Permissions → Repository permissions:**
   - **Contents:** Read and write. This lets the app add and remove pages.
   - **Actions:** Read and write. This powers "Check all pages now".
   - Metadata: Read-only, which GitHub adds automatically.
5. Click **Generate token** and copy it. You'll only see it once, so don't paste it anywhere except step 2 below.

### 2. Deploy on Streamlit Community Cloud

1. Go to **https://share.streamlit.io** and sign in with GitHub.
2. Click **Create app** → **Deploy a public app from GitHub**, then fill in:
   - **Repository:** `ankitdixit23/IhaveaJob`
   - **Branch:** `main`
   - **Main file path:** `dashboard/app.py`
   - **App URL:** pick a name, e.g. `ihaveajob-ankit`
3. Open **Advanced settings → Secrets** and paste (with your values):

   ```toml
   GITHUB_TOKEN = "github_pat_..."
   GITHUB_REPO = "ankitdixit23/IhaveaJob"
   APP_PASSWORD = "choose-a-password"
   ```

   Optionally choose Python 3.12 there too.
4. Click **Deploy**. The first start takes a minute or two.

### 3. Make it private

The app can change your repository, so lock it down:

1. In the app's **Settings → Sharing**, choose **Only specific people can view this app**, and add your email.
2. Keep `APP_PASSWORD` set as a second lock.

Streamlit Community Cloud allows **one private app** per account. The repository itself can stay public: the token and password live only in Streamlit's secret settings.

## Notes

- **Previews read the page HTML only**, because Streamlit's servers don't have a headless browser. Pages built with JavaScript (Microsoft, Meta, Amazon and so on) may show "no individual jobs found" in the preview. The hourly GitHub check does use a real browser and finds them there, so add the page anyway.
- **Data refreshes** every 5 minutes, or when you click **Refresh data**.
- **Each change is a commit** to `config/companies.yaml`, marked "(via web app)", so you can always see or undo what changed in the repo's history.
- **Free apps go to sleep** after a period without visitors. Opening the link wakes the app up within a minute. This has no effect on monitoring or emails.
- **If the token expires,** the app shows "GitHub rejected the token". Create a new one (step 1) and replace `GITHUB_TOKEN` in the app's Settings → Secrets.

## Run it locally

```bash
pip install -e ".[dashboard]"
streamlit run dashboard/app.py
```

Without `GITHUB_TOKEN`, the app works on your local `config/companies.yaml` and `data/ihaveajob.db`.

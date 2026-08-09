# Setting up the three accounts

Every credential the pipeline needs, in the order that gets you posting soonest.

One of the three works today. The other two need approval from the platform, and
those reviews take weeks — **submit them first, then do the rest while you
wait.**

| Platform | Time to first post | Blocker |
|---|---|---|
| YouTube Shorts | ~15 min | none |
| TikTok | 2–4 weeks | app audit |
| Instagram Reels | 2–6 weeks | Meta App Review |

**Reddit is not supported.** It closed self-service API registration in
November 2025 — see the appendix for the details and what it would take to
bring it back.

Check progress at any point with:

```powershell
.\.venv\Scripts\shorts publish --check
```

It names exactly which value is missing per platform.

---

## Before you start

All secrets go in `.env` at the repo root. It is gitignored — never commit it.

Non-secret settings (account ids, your handle, cadence) go in
`shorts.yaml` instead. Each step below says which file.

After editing `shorts.yaml` by hand, nothing needs restarting; the next command
picks it up.

---

## 1. YouTube Shorts

**Free, no approval. Do this one first.**

You already have `credentials.json` in the repo from the influencer tooling. You
are reusing that same Google Cloud project.

### 1.1 Enable the API

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Top-left project picker → select the **same project** `credentials.json` came
   from. If unsure, open `credentials.json` and look at `project_id`.
3. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**

### 1.2 Check the OAuth client type

`credentials.json` must be a **Desktop app** client, not a Web application.

1. **APIs & Services → Credentials**
2. Look at your OAuth 2.0 Client ID. The Type column must say **Desktop**.
3. If it says Web application, click **Create Credentials → OAuth client ID →
   Desktop app**, download the JSON, and replace `credentials.json`.

A Web client fails with a redirect-URI error when the browser opens.

### 1.3 Add yourself as a test user

If your OAuth consent screen is in *Testing* mode (it will be, unless you
published it):

1. **APIs & Services → OAuth consent screen → Audience**
2. Under **Test users**, add the Google account that owns the YouTube channel

Skip this and sign-in fails with "app has not completed verification".

### 1.4 Authorise

Nothing goes in `.env`. Run any publish and a browser opens once:

```powershell
.\.venv\Scripts\shorts publish
```

Approve, and `youtube_token.json` is written. It refreshes itself after that.

### 1.5 Settings

In `shorts.yaml`:

```yaml
publish:
  youtube_privacy: unlisted     # switch to "public" once you are happy
  youtube_category_id: '20'     # 20 = Gaming
```

**Leave it on `unlisted` for the first few posts.** You can flip a video to
public from YouTube Studio afterwards.

> **Quota:** an upload costs 1600 units against a 10,000/day default — about
> **6 uploads a day**. The pipeline posts every 2 days, so this never binds.

---

## 2. TikTok

**Free, but posts stay private until your app passes audit. Submit early.**

### 2.1 Register

1. [developers.tiktok.com](https://developers.tiktok.com) → log in → **Manage apps**
2. **Connect an app** → fill in the details
3. Add these products to the app:
   - **Login Kit**
   - **Content Posting API**

### 2.2 Credentials

From the app's page, into `.env`:

```
TIKTOK_CLIENT_KEY=your_client_key
TIKTOK_CLIENT_SECRET=your_client_secret
```

### 2.3 Get the OAuth tokens

You need to complete the OAuth flow once, with scope `video.publish`, and save
the result. Following TikTok's Login Kit docs, you will end up with an
`access_token` and a `refresh_token`.

Save them to `tiktok_token.json` next to `.env`:

```json
{
  "access_token": "act...",
  "refresh_token": "rft..."
}
```

The pipeline refreshes and rewrites this file automatically from then on.

### 2.4 Submit for audit

In the developer portal, submit the app for review. Expect **2–4 weeks** and
likely more than one round of feedback — they check your integration's UX
against their guidelines closely.

Until it passes, every post is forced to `SELF_ONLY` (visible only to you). The
publisher detects this and downgrades rather than failing, so **you can test the
entire pipeline while you wait** — the videos just will not be public.

Once audited, set:

```yaml
publish:
  tiktok_privacy: PUBLIC_TO_EVERYONE
```

---

## 3. Instagram Reels

**Free, but the most setup of the four. Submit the review early.**

### 3.1 Account prerequisites

Instagram will not allow API posting to a personal account.

1. In the Instagram app: **Settings → Account type and tools → Switch to
   professional account** → choose **Creator** or **Business**
2. Create a Facebook Page if you do not have one
   ([facebook.com/pages/create](https://facebook.com/pages/create))
3. Link them: Instagram **Settings → Sharing to other apps → Facebook**, or from
   the Page's **Linked accounts** settings

### 3.2 Create the Meta app

1. [developers.facebook.com/apps](https://developers.facebook.com/apps) →
   **Create App**
2. Use case: **Other** → type: **Business**
3. Add the **Instagram** product

### 3.3 Get a long-lived token

1. Open [Graph API Explorer](https://developers.facebook.com/tools/explorer/)
2. Select your app
3. Add permissions: `instagram_basic`, `instagram_content_publish`,
   `pages_show_list`, `pages_read_engagement`
4. **Generate Access Token** and approve
5. That token is short-lived. Exchange it for a long-lived Page token following
   Meta's token documentation

Into `.env`:

```
IG_ACCESS_TOKEN=your_long_lived_page_token
```

> **This expires roughly every 60 days.** Instagram will silently stop posting
> and it will look like a pipeline bug. Put a recurring calendar reminder on it
> now — this is the single most likely thing to break months from today.

### 3.4 Find your account id

In Graph API Explorer, run:

```
me/accounts?fields=instagram_business_account
```

Copy the numeric `id` into `shorts.yaml`:

```yaml
publish:
  instagram_user_id: '17841400000000000'
```

### 3.5 Set up video hosting

Instagram does not accept uploads — it fetches the video from a public URL. The
default backend puts it in a GitHub release, which is free.

1. [github.com/settings/personal-access-tokens](https://github.com/settings/personal-access-tokens)
   → **Generate new token** → fine-grained
2. Repository access: select your `Marketor` repo
3. Repository permissions → **Contents: Read and write**
4. Generate, and copy into `.env`:

```
GITHUB_TOKEN=github_pat_...
```

In `shorts.yaml`:

```yaml
publish:
  hosting:
    backend: github_release
    github_repo: DimitriKouliche/Marketor
```

If you already serve files from your own domain, use `backend: public_url` and
set `public_base_url` instead.

### 3.6 Submit for App Review

Request **Advanced Access** for `instagram_content_publish`. Expect **2–6
weeks** and probably several submissions.

> **API limit:** Reels via the API are capped at **90 seconds** even though the
> app allows three minutes. Clips are 18–20s, so this never binds.

---

## 4. Posting schedule

Set in `shorts.yaml` and enforced automatically — the queue skips a platform
until its window is up:

```yaml
publish:
  cadence_days:
    youtube: 2.0
    tiktok: 2.0
    instagram: 2.0
```

So: **one clip every 2 days** to each of the three.

Running `shorts publish` more often is harmless — platforms not yet due are
skipped with a message saying how long is left.

---

## 5. First run

```powershell
.\.venv\Scripts\shorts publish --check     # what is configured
.\.venv\Scripts\shorts list ready          # what is queued
.\.venv\Scripts\shorts approve 3 7         # nothing posts without this
.\.venv\Scripts\shorts publish --dry-run   # what would go out
.\.venv\Scripts\shorts publish
```

**Nothing posts without `approve`.** Keep it that way for the first few weeks —
automated posting from day one trains each platform's algorithm on your worst
output during exactly the period it is deciding what your account is.

---

## 6. The two LLM keys

Separate models do separate jobs, so both keys go in `.env`:

```
OPENAI_API_KEY=sk-...            # the planner: what to post, where, what angle
DEEPSEEK_API_KEY=sk-...          # the writer: captions, titles, post text
```

- **OpenAI** — https://platform.openai.com/api-keys. Only used by
  `shorts orchestrate`, a handful of times a week, so the spend is negligible.
- **DeepSeek** — https://platform.deepseek.com/api_keys. Used by `shorts write`
  for every clip; roughly a tenth of a cent each.

Either can be left unset. Without the DeepSeek key the pipeline falls back to
deterministic templates and says so loudly. Without the OpenAI key
`shorts orchestrate` refuses to run and you post manually with `shorts publish`.

---

## 7. Running it unattended

Once your reject rate settles below about 10%, hand the deciding over:

```yaml
orchestrator:
  autopost: true
```

```powershell
schtasks /create /tn "Marketor orchestrate" /sc daily /st 10:00 `
  /tr "Q:\Development\Marketor\.venv\Scripts\shorts.exe orchestrate"
```

Daily, even though nothing posts daily — the cadence table above is what spaces
the posts out, and a daily run means a missed slot is picked up the next morning
instead of four days later.

The planner keeps its notes in `strategy.md`. Read it occasionally; it is the
clearest picture of what it thinks is working, and you can edit it to give it
standing direction.

Before turning `autopost` on, watch a few runs:

```powershell
.\.venv\Scripts\shorts orchestrate --dry-run
```

If you want to keep a platform for yourself, list it under
`orchestrator.manual_platforms`. The planner is then never told it exists, while
`shorts handoff <platform>` still tells you what to post there and
`shorts record` keeps the cadence and the learning loop in step.

That is also the answer while TikTok and Instagram are still in review: their
apps cannot post yet, but `handoff` will hand you the file, the caption and the
hashtags to upload by hand in the meantime.

---

## Troubleshooting

**`ffprobe not found`** — open a new terminal. Winget only updates PATH for
shells started after the install.

**YouTube: "app has not completed verification"** — you are not in the test
users list (step 1.3).

**YouTube: redirect URI mismatch** — `credentials.json` is a Web client, not a
Desktop client (step 1.2).

**TikTok: posts are private** — expected until the audit passes.

**Instagram: worked for weeks, then stopped** — the Page token expired (step
3.3).

**Instagram: "media not found"** — `GITHUB_TOKEN` lacks `Contents: Read and
write`, or `github_repo` is wrong.

---

## Appendix: why Reddit is not here

Reddit would be the best-fitting platform in this list. A clip that lands on
r/IndieGaming reaches more genuinely interested players than any amount of
TikTok reach. It is absent for a reason outside the project.

**In November 2025 Reddit closed self-service API registration.** Going to
`reddit.com/prefs/apps` and choosing "create another app" no longer creates
anything; it returns a pointer to the
[Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy).
Every new OAuth token now needs prior approval through a support ticket.
Credentials issued before that date were grandfathered and still work.

Applying is free, via the
[Developer Support form](https://support.reddithelp.com/hc/en-us/requests/new?ticket_form_id=14868593862164)
under category **developer**. But a weekly self-promotion upload is close to the
exact case the review exists to refuse, and reports are consistent that personal
and hobbyist posting bots are rarely approved: multi-week waits, generic
rejections.

The Reddit publisher, the compilation builder and the subreddit rotation were
deleted rather than left dormant — roughly 780 lines. Keeping code for a
platform the project does not post to makes every later change more work than
it needs to be, and the deleted version is a `git show` away if it is ever
wanted back.

You can still post there by hand whenever you feel like it. The clips are
ordinary files in `shorts_out/`.

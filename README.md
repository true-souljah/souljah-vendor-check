# souljah-vendor-check

Read-only Reddit Data API client for editorial vendor checks: before a vendor
is listed or rated on our review sites, an editor searches a few public
subreddits (r/Peptides, r/Nootropics) for user reports about that vendor.
A few dozen searches and post reads a week; it stores links and short notes
only. This tool is the source link for our Reddit Data API access request —
it does exactly what that request says and nothing more.

Read-only: no posting, voting, messaging or moderation. Python 3.9+
standard library only; runs as `/usr/bin/python3 vendor_check.py …` on macOS.

## What it reads

- **macOS Keychain** (at runtime, via `security find-generic-password -s <service> -w`):
  `souljah-vendor-check-client-id`, `souljah-vendor-check-client-secret`,
  `souljah-vendor-check-password`. Nothing is ever written to the Keychain
  except by `setup-keychain.sh`.
- **`config.json`**: Reddit username, subreddit allowlist, User-Agent parts,
  notes path. Copy `config.example.json` to `config.json` (next to
  `vendor_check.py`) to start.
- **Reddit Data API** (read-only, three endpoints and no others):
  `POST /api/v1/access_token` (OAuth2 script app, password grant — the token
  is kept in memory for that run only and never written to disk or printed),
  `GET /r/<sub>/search`, `GET /api/info`.

Every request carries the User-Agent
`macos:souljah-vendor-check:1.0 (by /u/true_souljah)`.

## What it stores and deletes

`note` appends one record to `notes.json` (default
`~/.souljah-vendor-check/notes.json`, mode `600`) with exactly these fields:
permalink, post id, subreddit, post created_utc, vendor, note text (max 280
characters), saved_at. It never stores post bodies, comment text, titles or
usernames. The permalink is stored in canonical form (no query string), and
`note` refuses posts outside the subreddit allowlist and posts that are
already deleted, removed or not found. `purge` looks every stored post up via `GET /api/info?id=t3_<id>`
(batched up to 100 ids per call) and deletes the record when the post is
deleted or removed (author `[deleted]`, `removed_by_category` set, or the
post is no longer returned); it prints counts only.

## Rate limits

At most one request per second (well under the 100-per-minute free tier).
Reddit's `X-Ratelimit-Remaining` / `X-Ratelimit-Reset` headers are honoured:
when remaining drops below 5, the tool sleeps until the window resets. On
HTTP 429 it waits for the reset and retries once, then stops with an error.

## Setup

1. `bash setup-keychain.sh` — prompts silently (`read -rs`) for the client
   id, client secret and account password, rejects empty values, and stores
   them with `security add-generic-password -U`. Values are never echoed.
2. `cp config.example.json config.json` and edit the allowlist if needed.
3. Run the tests: `python3 -m unittest discover -v`.

## Commands

```
/usr/bin/python3 vendor_check.py search --subreddit Peptides --query "vendor name" --limit 25
/usr/bin/python3 vendor_check.py note \
    --url https://www.reddit.com/r/Peptides/comments/<id>/... \
    --vendor "Vendor Name" --text "two shipping reports, both positive"
/usr/bin/python3 vendor_check.py purge
```

`search` prints, per post: permalink URL, subreddit, created date (UTC),
score, title. `--limit` defaults to 25 and is capped at 100. Only subreddits
listed in `config.json` `"subreddits"` can be searched; anything else is
refused. No streaming, no background service, no data export.

## Data-use commitments

No AI or model training on Reddit data; no resale or sharing; links only on
the sites, never post text or usernames.

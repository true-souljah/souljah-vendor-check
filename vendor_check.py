#!/usr/bin/env python3
"""souljah-vendor-check - a small read-only Reddit Data API client.

Editorial vendor checks: search a few public subreddits for user reports
about a vendor, save links with short notes, and purge notes whose posts
were deleted or removed. Read-only: no posting, voting, messaging or
moderation. Python 3.9+ standard library only.

Credentials live in the macOS Keychain and are read at runtime; the OAuth2
token is kept in memory for the run only and is never written to disk or
printed.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH_ROOT = "https://oauth.reddit.com"
DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.json"
)

KEYCHAIN_SERVICES = {
    "client_id": "souljah-vendor-check-client-id",
    "client_secret": "souljah-vendor-check-client-secret",
    "password": "souljah-vendor-check-password",
}

MIN_REQUEST_INTERVAL = 1.0  # at most one request per second
REMAINING_FLOOR = 5.0       # sleep until reset when remaining drops below this
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
NOTE_MAX_CHARS = 280
INFO_BATCH = 100            # /api/info accepts up to 100 ids per call


POST_ID_RE = re.compile(r"\A[A-Za-z0-9]{1,12}\Z")  # base36 post id


class VendorCheckError(Exception):
    """Expected error; the message is safe to print (never a secret)."""


class RateLimitError(VendorCheckError):
    """Reddit kept returning HTTP 429 after one retry."""


# ---------------------------------------------------------------- config


def load_config(path="config.json"):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        raise VendorCheckError(
            "config.json not found; copy config.example.json to config.json"
        )
    except ValueError as err:
        raise VendorCheckError("config.json is not valid JSON: %s" % err)
    for key in ("username", "subreddits"):
        if key not in cfg:
            raise VendorCheckError("config.json is missing %r" % key)
    if not isinstance(cfg["subreddits"], list) or not cfg["subreddits"]:
        raise VendorCheckError('config.json "subreddits" must be a non-empty list')
    return cfg


def build_user_agent(cfg):
    """macos:souljah-vendor-check:1.0 (by /u/<username>) - from config."""
    return "{}:{}:{} (by /u/{})".format(
        cfg.get("platform", "macos"),
        cfg.get("app_id", "souljah-vendor-check"),
        cfg.get("version", "1.0"),
        cfg["username"],
    )


def notes_path(cfg):
    return os.path.expanduser(
        cfg.get("notes_path", "~/.souljah-vendor-check/notes.json")
    )


# ------------------------------------------------------------ keychain


def read_keychain(service):
    """Read one secret from the macOS Keychain (`security find-generic-password -s <service> -w`)."""
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        raise VendorCheckError("macOS `security` tool not found; this runs on macOS only")
    if result.returncode != 0:
        raise VendorCheckError(
            "keychain lookup failed for %s (run: bash setup-keychain.sh)" % service
        )
    # Only drop the trailing newline `security` adds; spaces may be part of a secret.
    value = result.stdout.rstrip("\r\n")
    if not value:
        raise VendorCheckError("keychain entry for %s is empty" % service)
    return value


def get_credentials(cfg, keychain):
    creds = {name: keychain(service) for name, service in KEYCHAIN_SERVICES.items()}
    creds["username"] = cfg["username"]
    return creds


# ---------------------------------------------------------------- http


def default_opener(method, url, headers, data):
    """Real transport: returns (status, lowercase-header dict, body bytes)."""
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as err:
        body = err.read()
        return err.code, {k.lower(): v for k, v in err.headers.items()}, body
    except urllib.error.URLError as err:
        raise VendorCheckError("request failed: %s" % err.reason)


def parse_json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise VendorCheckError("Reddit returned a response that is not JSON")


def _header_float(headers, name):
    value = headers.get(name)
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _reset_seconds(headers):
    for name in ("x-ratelimit-reset", "retry-after"):
        value = _header_float(headers, name)
        if value is not None and value > 0:
            return value
    return MIN_REQUEST_INTERVAL


class RedditClient:
    """One HTTP client with pacing, ratelimit-header handling and one 429 retry."""

    def __init__(self, user_agent, opener=None, sleep_fn=time.sleep, time_fn=time.time):
        self.user_agent = user_agent
        self.opener = opener or default_opener
        self.sleep = sleep_fn
        self.now = time_fn
        self.token = None  # in memory for this run only
        self._last_request_at = None
        self._hold_until = None

    def _pace(self):
        if self._last_request_at is not None:
            elapsed = self.now() - self._last_request_at
            if elapsed < MIN_REQUEST_INTERVAL:
                self.sleep(MIN_REQUEST_INTERVAL - elapsed)
        if self._hold_until is not None:
            wait = self._hold_until - self.now()
            if wait > 0:
                self.sleep(wait)

    def _note_rate_headers(self, headers):
        remaining = _header_float(headers, "x-ratelimit-remaining")
        reset = _header_float(headers, "x-ratelimit-reset")
        if remaining is not None and remaining < REMAINING_FLOOR:
            delay = reset if (reset is not None and reset > 0) else MIN_REQUEST_INTERVAL
            self._hold_until = self.now() + delay

    def request(self, method, url, extra_headers=None, data=None):
        headers = {"User-Agent": self.user_agent}
        if self.token is not None:
            headers["Authorization"] = "bearer " + self.token
        if extra_headers:
            headers.update(extra_headers)
        for attempt in (1, 2):
            self._pace()
            self._last_request_at = self.now()
            status, resp_headers, body = self.opener(method, url, headers, data)
            if status == 429 and attempt == 1:
                self.sleep(_reset_seconds(resp_headers))
                continue
            if status == 429:
                raise RateLimitError(
                    "still rate limited after waiting for the reset window; stopping"
                )
            break
        if status != 200:
            path = urllib.parse.urlsplit(url).path
            raise VendorCheckError("HTTP %s from %s" % (status, path))
        self._note_rate_headers(resp_headers)
        return resp_headers, body


def fetch_token(client, creds):
    """OAuth2 script-app password grant; the token stays in memory."""
    basic = base64.b64encode(
        (creds["client_id"] + ":" + creds["client_secret"]).encode("utf-8")
    ).decode("ascii")
    body = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "username": creds["username"],
            "password": creds["password"],
        }
    ).encode("utf-8")
    _, raw = client.request(
        "POST",
        TOKEN_URL,
        extra_headers={
            "Authorization": "Basic " + basic,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=body,
    )
    payload = parse_json(raw)
    token = payload.get("access_token")
    if not token:
        raise VendorCheckError("token response did not include an access token")
    client.token = token
    return token


# ---------------------------------------------------------------- notes


def load_notes(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        try:
            notes = json.load(fh)
        except ValueError:
            raise VendorCheckError("%s is not valid JSON; fix or remove it" % path)
    if not isinstance(notes, list):
        raise VendorCheckError("%s must contain a JSON list of records" % path)
    return notes


def save_notes(path, notes):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(notes, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        # Do not leave a half-written temp file behind; the original error propagates.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def parse_permalink(url):
    """Return (subreddit, post_id) from a /r/<sub>/comments/<id>/... permalink."""
    split = urllib.parse.urlsplit(url)
    host = (split.hostname or "").lower()
    parts = [p for p in split.path.split("/") if p]
    if (
        split.scheme in ("http", "https")
        and (host == "reddit.com" or host.endswith(".reddit.com"))
        and len(parts) >= 4
        and parts[0].lower() == "r"
        and parts[2].lower() == "comments"
        and POST_ID_RE.match(parts[3])
    ):
        return parts[1], parts[3].lower()
    raise VendorCheckError(
        "not a post permalink (expected /r/<subreddit>/comments/<post id>/...)"
    )


# ------------------------------------------------------------- reddit


def _invalid_info(reason):
    return VendorCheckError(
        "/api/info response is not a valid post listing (%s); nothing was changed"
        % reason
    )


def _invalid_search(reason):
    return VendorCheckError("search response is not a valid post listing (%s)" % reason)


def _listing_posts(payload, invalid):
    """Return the post dicts of a Listing of t3 children with string ids.

    Raises invalid(reason) for anything else, so an error body is never read
    as an empty result.
    """
    if not isinstance(payload, dict):
        raise invalid("not a JSON object")
    if "error" in payload:
        raise invalid("it has an error field")
    if payload.get("kind") != "Listing":
        raise invalid("kind is not Listing")
    data = payload.get("data")
    children = data.get("children") if isinstance(data, dict) else None
    if not isinstance(children, list):
        raise invalid("data.children is missing")
    posts = []
    for child in children:
        if not isinstance(child, dict) or child.get("kind") != "t3":
            raise invalid("a child is not a t3 post")
        post = child.get("data")
        if not isinstance(post, dict) or not isinstance(post.get("id"), str):
            raise invalid("a post has no id")
        posts.append(post)
    return posts


def fetch_posts(client, post_ids):
    """GET /api/info for up to 100 t3_ ids; return {post_id: post data}.

    Fails closed: unless the body is a Listing of t3 posts drawn only from the
    requested ids, raise instead of returning, so an error body or an
    unrelated listing is never read as "these posts are gone".
    """
    if not post_ids or len(post_ids) > INFO_BATCH:
        raise ValueError("expected 1..%d post ids" % INFO_BATCH)
    url = OAUTH_ROOT + "/api/info?" + urllib.parse.urlencode(
        {"id": ",".join("t3_" + pid for pid in post_ids)}
    )
    _, raw = client.request("GET", url)
    requested = set(post_ids)
    found = {}
    for post in _listing_posts(parse_json(raw), _invalid_info):
        if post["id"] not in requested:
            raise _invalid_info("it lists a post that was not requested")
        found[post["id"]] = post
    return found


def post_is_dead(post):
    if post is None:
        return True
    if post.get("author") == "[deleted]":
        return True
    if post.get("removed_by_category"):
        return True
    return False


# ---------------------------------------------------------------- app


class App:
    def __init__(self, cfg, opener=None, keychain=None, sleep_fn=time.sleep, time_fn=time.time):
        self.cfg = cfg
        self.keychain = keychain or read_keychain
        self.client = RedditClient(
            build_user_agent(cfg),
            opener=opener,
            sleep_fn=sleep_fn,
            time_fn=time_fn,
        )

    def authorize(self):
        creds = get_credentials(self.cfg, self.keychain)
        fetch_token(self.client, creds)


def utc_timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_allowlisted(cfg, subreddit):
    allowed = [s.lower() for s in cfg["subreddits"]]
    if subreddit.lower() not in allowed:
        raise VendorCheckError(
            "subreddit r/%s is not in the config.json allowlist" % subreddit
        )


def cmd_search(app, args):
    check_allowlisted(app.cfg, args.subreddit)
    if not 1 <= args.limit <= MAX_LIMIT:
        raise VendorCheckError("limit must be between 1 and %d" % MAX_LIMIT)
    app.authorize()
    url = OAUTH_ROOT + "/r/{}/search?{}".format(
        urllib.parse.quote(args.subreddit),
        urllib.parse.urlencode(
            {
                "q": args.query,
                "restrict_sr": "1",
                "sort": "new",
                "limit": str(args.limit),
            }
        ),
    )
    _, raw = app.client.request("GET", url)
    for post in _listing_posts(parse_json(raw), _invalid_search):
        permalink = post.get("permalink", "")
        if permalink:
            print("https://www.reddit.com" + permalink)
        created = datetime.fromtimestamp(
            post.get("created_utc", 0) or 0, tz=timezone.utc
        )
        print(
            "r/{} | {} | score {}".format(
                post.get("subreddit", "?"),
                created.strftime("%Y-%m-%d %H:%M UTC"),
                post.get("score", 0),
            )
        )
        print(post.get("title", ""))
        print()


def cmd_note(app, args):
    if len(args.text) > NOTE_MAX_CHARS:
        raise VendorCheckError(
            "note text is %d characters; the maximum is %d"
            % (len(args.text), NOTE_MAX_CHARS)
        )
    if not args.text.strip():
        raise VendorCheckError("note text is empty")
    if not args.vendor.strip():
        raise VendorCheckError("vendor name is empty")
    subreddit, post_id = parse_permalink(args.url)
    check_allowlisted(app.cfg, subreddit)
    app.authorize()
    posts = fetch_posts(app.client, [post_id])
    post = posts.get(post_id)
    if post_is_dead(post):
        raise VendorCheckError(
            "post t3_%s is deleted, removed or not found; nothing saved" % post_id
        )
    subreddit = post.get("subreddit") or subreddit
    check_allowlisted(app.cfg, subreddit)
    permalink = post.get("permalink")
    record = {
        # Canonical permalink without query string or tracking parameters.
        "permalink": ("https://www.reddit.com" + permalink) if permalink
        else "https://www.reddit.com/r/%s/comments/%s/" % (subreddit, post_id),
        "post_id": post_id,
        "subreddit": subreddit,
        "created_utc": post.get("created_utc"),
        "vendor": args.vendor,
        "note": args.text,
        "saved_at": utc_timestamp(),
    }
    path = notes_path(app.cfg)
    notes = load_notes(path)
    notes.append(record)
    save_notes(path, notes)
    print("saved note for t3_%s" % post_id)


def cmd_purge(app, args):
    path = notes_path(app.cfg)
    notes = load_notes(path)
    if not notes:
        print("checked=0 deleted=0 kept=0")
        return 0
    # Only a valid listing showing a post dead or absent deletes its record.
    # A record without a valid post id cannot be checked, so it is kept as is.
    checkable, unchecked = [], []  # checkable holds (index in notes, record)
    for index, record in enumerate(notes):
        post_id = record.get("post_id") if isinstance(record, dict) else None
        if isinstance(post_id, str) and POST_ID_RE.match(post_id):
            checkable.append((index, record))
        else:
            unchecked.append(record)
    dead = set()  # indices in notes of records to delete
    if checkable:
        app.authorize()
    for start in range(0, len(checkable), INFO_BATCH):
        batch = checkable[start:start + INFO_BATCH]
        posts = fetch_posts(app.client, [n["post_id"] for _, n in batch])
        dead.update(i for i, n in batch if post_is_dead(posts.get(n["post_id"])))
    kept = [n for i, n in enumerate(notes) if i not in dead]
    if dead:
        save_notes(path, kept)
    print("checked=%d deleted=%d kept=%d" % (len(notes), len(dead), len(kept)))
    if unchecked:
        print(
            "warning: kept %d record%s with no valid post id (not checked):"
            % (len(unchecked), "" if len(unchecked) == 1 else "s"),
            file=sys.stderr,
        )
        for record in unchecked:
            # Permalink only, never the note text; skip anything unprintable.
            link = record.get("permalink") if isinstance(record, dict) else None
            ok = isinstance(link, str) and link != "" and link.isprintable()
            print("  " + (link if ok else "(no printable permalink)"), file=sys.stderr)
    return 0


COMMANDS = {
    "search": cmd_search,
    "note": cmd_note,
    "purge": cmd_purge,
}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="vendor_check.py",
        description="Read-only Reddit vendor checks for editorial review sites.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_search = sub.add_parser("search", help="search an allowlisted subreddit")
    p_search.add_argument("--subreddit", required=True, help="e.g. Peptides")
    p_search.add_argument("--query", required=True, help="search text")
    p_search.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                          help="results to show (default %d, max %d)" % (DEFAULT_LIMIT, MAX_LIMIT))
    p_note = sub.add_parser("note", help="save a short note about one post")
    p_note.add_argument("--url", required=True, help="post permalink")
    p_note.add_argument("--vendor", required=True, help="vendor name")
    p_note.add_argument("--text", required=True, help="note text (max %d chars)" % NOTE_MAX_CHARS)
    sub.add_parser("purge", help="drop notes whose posts are deleted or removed")
    return parser


def main(argv=None, *, opener=None, keychain=None, sleep_fn=time.sleep,
         time_fn=time.time, config_path=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    try:
        cfg = load_config(config_path)
        app = App(cfg, opener=opener, keychain=keychain, sleep_fn=sleep_fn, time_fn=time_fn)
        return COMMANDS[args.command](app, args) or 0
    except VendorCheckError as err:
        print("error: %s" % err, file=sys.stderr)
        return 1
    except OSError as err:
        # Filesystem or system failures (permissions, a directory where a file
        # should be, a full disk): one line with the reason and path, no traceback.
        detail = err.strerror or str(err) or err.__class__.__name__
        if err.filename:
            detail = "%s: %s" % (detail, err.filename)
        print("error: %s" % detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

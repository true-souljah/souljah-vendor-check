"""Tests for vendor_check.py - no network, fake opener and fake keychain.

All secret-shaped values are obvious letters-only fakes.
"""

import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vendor_check as vc  # noqa: E402

FAKE_CLIENT_ID = "fakeclientid"
FAKE_CLIENT_SECRET = "fakesecret"
FAKE_PASSWORD = "fakepassword"
FAKE_TOKEN = "faketoken"
FAKE_KEYCHAIN = {
    "souljah-vendor-check-client-id": FAKE_CLIENT_ID,
    "souljah-vendor-check-client-secret": FAKE_CLIENT_SECRET,
    "souljah-vendor-check-password": FAKE_PASSWORD,
}
USER_AGENT = "macos:souljah-vendor-check:1.0 (by /u/true_souljah)"


def fake_keychain(service):
    return FAKE_KEYCHAIN[service]


class FakeClock:
    """time.time / time.time.sleep pair; sleep advances the clock."""

    def __init__(self, start=1000.0):
        self.current = start
        self.sleeps = []

    def time_fn(self):
        return self.current

    def sleep_fn(self, seconds):
        self.sleeps.append(seconds)
        self.current += seconds


def make_post(post_id, subreddit="Peptides", title="Vendor thread report",
              score=7, created=1704186240.0, author="someuser", removed=None):
    return {
        "id": post_id,
        "subreddit": subreddit,
        "title": title,
        "score": score,
        "created_utc": created,
        "author": author,
        "permalink": "/r/%s/comments/%s/vendor_thread/" % (subreddit, post_id),
        "removed_by_category": removed,
        "selftext": "post body that must never be stored",
    }


def listing_body(children):
    return json.dumps(
        {"data": {"children": [{"kind": "t3", "data": post} for post in children]}}
    ).encode("utf-8")


class FakeReddit:
    """Routes requests to canned responses and records every request."""

    def __init__(self):
        self.requests = []
        self.token_statuses = []
        self.search_statuses = []
        self.info_statuses = []
        self.token_headers = {"x-ratelimit-remaining": "98", "x-ratelimit-reset": "600"}
        self.data_headers = {"x-ratelimit-remaining": "98", "x-ratelimit-reset": "600"}
        self.search_children = [make_post("live1")]
        self.posts = {}  # id -> post dict returned by /api/info

    # -- helpers -----------------------------------------------------
    @staticmethod
    def _pop(statuses, default=200):
        return statuses.pop(0) if statuses else default

    def _ids(self, url):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        return [value[3:] for value in query["id"][0].split(",")]

    # -- opener ------------------------------------------------------
    def __call__(self, method, url, headers, data):
        self.requests.append(
            {"method": method, "url": url, "headers": dict(headers), "data": data}
        )
        if "access_token" in url:
            status = self._pop(self.token_statuses)
            return status, dict(self.token_headers), json.dumps(
                {"access_token": FAKE_TOKEN, "expires_in": 3600}
            ).encode("utf-8")
        if "/api/info" in url:
            status = self._pop(self.info_statuses)
            children = [self.posts[pid] for pid in self._ids(url) if pid in self.posts]
            return status, dict(self.data_headers), listing_body(children)
        if "/search" in url:
            status = self._pop(self.search_statuses)
            return status, dict(self.data_headers), listing_body(self.search_children)
        raise AssertionError("unexpected url: %s" % url)

    def urls(self):
        return [r["url"] for r in self.requests]


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notes_file = os.path.join(self.tmp.name, "notes.json")
        self.config_file = self.write_config()
        self.fake = FakeReddit()
        self.clock = FakeClock()

    def write_config(self):
        cfg = {
            "username": "true_souljah",
            "platform": "macos",
            "app_id": "souljah-vendor-check",
            "version": "1.0",
            "subreddits": ["Peptides", "Nootropics"],
            "notes_path": self.notes_file,
        }
        path = os.path.join(self.tmp.name, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return path

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = vc.main(
                argv,
                opener=self.fake,
                keychain=fake_keychain,
                sleep_fn=self.clock.sleep_fn,
                time_fn=self.clock.time_fn,
                config_path=self.config_file,
            )
        return rc, out.getvalue(), err.getvalue()

    def read_notes(self):
        with open(self.notes_file, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def write_notes(self, records):
        with open(self.notes_file, "w", encoding="utf-8") as fh:
            json.dump(records, fh)


class TestTokenRequest(BaseCase):
    def test_token_request_shape(self):
        self.fake.search_children = []
        rc, _, _ = self.run_main(
            ["search", "--subreddit", "Peptides", "--query", "vendorname"]
        )
        self.assertEqual(rc, 0)
        first = self.fake.requests[0]
        self.assertEqual(first["method"], "POST")
        self.assertEqual(first["url"], "https://www.reddit.com/api/v1/access_token")
        expected_basic = "Basic " + base64.b64encode(
            (FAKE_CLIENT_ID + ":" + FAKE_CLIENT_SECRET).encode("utf-8")
        ).decode("ascii")
        self.assertEqual(first["headers"]["Authorization"], expected_basic)
        self.assertEqual(
            first["headers"]["Content-Type"], "application/x-www-form-urlencoded"
        )
        form = urllib.parse.parse_qs(first["data"].decode("utf-8"))
        self.assertEqual(form["grant_type"], ["password"])
        self.assertEqual(form["username"], ["true_souljah"])
        self.assertEqual(form["password"], [FAKE_PASSWORD])

    def test_user_agent_on_every_request(self):
        self.fake.search_children = []
        rc, _, _ = self.run_main(
            ["search", "--subreddit", "Peptides", "--query", "vendorname"]
        )
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(len(self.fake.requests), 2)
        for request in self.fake.requests:
            self.assertEqual(request["headers"]["User-Agent"], USER_AGENT)

    def test_data_request_uses_bearer_token(self):
        self.fake.search_children = []
        self.run_main(["search", "--subreddit", "Peptides", "--query", "vendorname"])
        data_request = self.fake.requests[1]
        self.assertTrue(data_request["url"].startswith("https://oauth.reddit.com/"))
        self.assertEqual(
            data_request["headers"]["Authorization"], "bearer " + FAKE_TOKEN
        )


class TestSearch(BaseCase):
    def test_search_url_and_params(self):
        self.fake.search_children = []
        self.run_main(
            ["search", "--subreddit", "Peptides", "--query", "best vendor",
             "--limit", "50"]
        )
        search_url = self.fake.urls()[1]
        split = urllib.parse.urlsplit(search_url)
        self.assertEqual(split.netloc, "oauth.reddit.com")
        self.assertEqual(split.path, "/r/Peptides/search")
        query = urllib.parse.parse_qs(split.query)
        self.assertEqual(query["restrict_sr"], ["1"])
        self.assertEqual(query["sort"], ["new"])
        self.assertEqual(query["limit"], ["50"])
        self.assertEqual(query["q"], ["best vendor"])

    def test_default_limit_is_25(self):
        self.fake.search_children = []
        self.run_main(["search", "--subreddit", "Nootropics", "--query", "x"])
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.fake.urls()[1]).query)
        self.assertEqual(query["limit"], ["25"])

    def test_limit_over_100_refused(self):
        rc, _, err = self.run_main(
            ["search", "--subreddit", "Peptides", "--query", "x", "--limit", "101"]
        )
        self.assertEqual(rc, 1)
        self.assertIn("limit must be between 1 and 100", err)
        self.assertEqual(self.fake.requests, [])

    def test_refuses_subreddit_not_in_allowlist(self):
        rc, _, err = self.run_main(
            ["search", "--subreddit", "WallStreetBets", "--query", "x"]
        )
        self.assertEqual(rc, 1)
        self.assertIn("not in the config.json allowlist", err)
        self.assertEqual(self.fake.requests, [])

    def test_printed_fields(self):
        post = make_post("abc12", subreddit="Peptides", title="Peptide vendor report",
                         score=42, created=1704186240.0)
        self.fake.search_children = [post]
        rc, out, _ = self.run_main(
            ["search", "--subreddit", "Peptides", "--query", "vendor"]
        )
        self.assertEqual(rc, 0)
        self.assertIn("https://www.reddit.com/r/Peptides/comments/abc12/vendor_thread/", out)
        self.assertIn("r/Peptides", out)
        self.assertIn("2024-01-02 09:04 UTC", out)
        self.assertIn("score 42", out)
        self.assertIn("Peptide vendor report", out)


class TestNote(BaseCase):
    PERMALINK = "https://www.reddit.com/r/Peptides/comments/abc12/vendor_thread/"

    def setUp(self):
        super().setUp()
        self.fake.posts["abc12"] = make_post("abc12")

    def test_note_stores_only_allowed_fields(self):
        rc, out, _ = self.run_main(
            ["note", "--url", self.PERMALINK, "--vendor", "ExampleVendor",
             "--text", "two shipping reports, both positive"]
        )
        self.assertEqual(rc, 0)
        records = self.read_notes()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(
            sorted(record),
            ["created_utc", "note", "permalink", "post_id", "saved_at",
             "subreddit", "vendor"],
        )
        self.assertEqual(record["permalink"], self.PERMALINK)
        self.assertEqual(record["post_id"], "abc12")
        self.assertEqual(record["subreddit"], "Peptides")
        self.assertEqual(record["created_utc"], 1704186240.0)
        self.assertEqual(record["vendor"], "ExampleVendor")
        self.assertEqual(record["note"], "two shipping reports, both positive")
        self.assertTrue(record["saved_at"])

    def test_note_never_stores_post_content_or_usernames(self):
        post = make_post("abc12", title="SECRETTITLE", author="SECRETUSER",
                         removed=None)
        post["selftext"] = "SECRETBODY"
        self.fake.posts["abc12"] = post
        self.run_main(
            ["note", "--url", self.PERMALINK, "--vendor", "ExampleVendor",
             "--text", "short note"]
        )
        with open(self.notes_file, "r", encoding="utf-8") as fh:
            raw = fh.read()
        for forbidden in ("SECRETTITLE", "SECRETUSER", "SECRETBODY"):
            self.assertNotIn(forbidden, raw)

    def test_note_file_mode_600(self):
        self.run_main(
            ["note", "--url", self.PERMALINK, "--vendor", "ExampleVendor",
             "--text", "short note"]
        )
        mode = os.stat(self.notes_file).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_note_over_280_chars_rejected(self):
        rc, _, err = self.run_main(
            ["note", "--url", self.PERMALINK, "--vendor", "ExampleVendor",
             "--text", "x" * 281]
        )
        self.assertEqual(rc, 1)
        self.assertIn("the maximum is 280", err)
        self.assertFalse(os.path.exists(self.notes_file))
        self.assertEqual(self.fake.requests, [])

    def test_note_exactly_280_chars_accepted(self):
        rc, _, _ = self.run_main(
            ["note", "--url", self.PERMALINK, "--vendor", "ExampleVendor",
             "--text", "x" * 280]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.read_notes()[0]["note"], "x" * 280)

    def test_note_appends_to_existing_records(self):
        self.write_notes(
            [{"permalink": self.PERMALINK, "post_id": "old1", "subreddit": "Peptides",
              "created_utc": 1.0, "vendor": "OtherVendor", "note": "old",
              "saved_at": "2026-01-01T00:00:00Z"}]
        )
        rc, _, _ = self.run_main(
            ["note", "--url", self.PERMALINK, "--vendor", "ExampleVendor",
             "--text", "second note"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.read_notes()), 2)

    def test_note_bad_permalink_rejected(self):
        rc, _, err = self.run_main(
            ["note", "--url", "https://example.com/not-a-permalink",
             "--vendor", "ExampleVendor", "--text", "note"]
        )
        self.assertEqual(rc, 1)
        self.assertIn("not a post permalink", err)
        self.assertEqual(self.fake.requests, [])

    def test_note_refuses_subreddit_not_in_allowlist(self):
        rc, _, err = self.run_main(
            ["note", "--url", "https://www.reddit.com/r/Other/comments/abc12/x/",
             "--vendor", "ExampleVendor", "--text", "note"]
        )
        self.assertEqual(rc, 1)
        self.assertIn("not in the config.json allowlist", err)
        self.assertEqual(self.fake.requests, [])

    def test_note_refuses_deleted_or_missing_post(self):
        self.fake.posts["abc12"] = make_post("abc12", author="[deleted]")
        rc, _, err = self.run_main(
            ["note", "--url", self.PERMALINK, "--vendor", "ExampleVendor",
             "--text", "note"]
        )
        self.assertEqual(rc, 1)
        self.assertIn("nothing saved", err)
        self.assertFalse(os.path.exists(self.notes_file))
        del self.fake.posts["abc12"]
        rc, _, _ = self.run_main(
            ["note", "--url", self.PERMALINK, "--vendor", "ExampleVendor",
             "--text", "note"]
        )
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.notes_file))

    def test_note_stores_canonical_permalink(self):
        rc, _, _ = self.run_main(
            ["note", "--url", self.PERMALINK + "?utm_source=share&context=3",
             "--vendor", "ExampleVendor", "--text", "note"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.read_notes()[0]["permalink"], self.PERMALINK)


class TestPurge(BaseCase):
    def record(self, post_id):
        return {
            "permalink": "https://www.reddit.com/r/Peptides/comments/%s/x/" % post_id,
            "post_id": post_id,
            "subreddit": "Peptides",
            "created_utc": 1704186240.0,
            "vendor": "ExampleVendor",
            "note": "note",
            "saved_at": "2026-01-01T00:00:00Z",
        }

    def test_purge_deletes_dead_and_keeps_live(self):
        self.fake.posts["live1"] = make_post("live1", author="someuser")
        self.fake.posts["gone1"] = make_post("gone1", author="[deleted]")
        self.fake.posts["gone2"] = make_post("gone2", removed="deleted")
        # gone3 is not in self.fake.posts at all -> not returned by /api/info
        self.write_notes([self.record("live1"), self.record("gone1"),
                          self.record("gone2"), self.record("gone3")])
        rc, out, _ = self.run_main(["purge"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "checked=4 deleted=3 kept=1")
        remaining = self.read_notes()
        self.assertEqual([r["post_id"] for r in remaining], ["live1"])

    def test_purge_no_notes_file(self):
        rc, out, _ = self.run_main(["purge"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "checked=0 deleted=0 kept=0")
        self.assertEqual(self.fake.requests, [])

    def test_purge_drops_records_without_valid_post_id(self):
        self.fake.posts["live1"] = make_post("live1")
        bad = self.record("x")
        bad["post_id"] = "bad,id"
        no_id = self.record("y")
        del no_id["post_id"]
        self.write_notes([self.record("live1"), bad, no_id])
        rc, out, _ = self.run_main(["purge"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "checked=3 deleted=2 kept=1")
        self.assertEqual([r["post_id"] for r in self.read_notes()], ["live1"])
        info_calls = [r for r in self.fake.requests if "/api/info" in r["url"]]
        self.assertEqual(len(info_calls), 1)

    def test_purge_batches_at_100_ids(self):
        records = [self.record("dead%03d" % i) for i in range(150)]
        self.write_notes(records)
        rc, out, _ = self.run_main(["purge"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "checked=150 deleted=150 kept=0")
        info_calls = [r for r in self.fake.requests if "/api/info" in r["url"]]
        self.assertEqual(len(info_calls), 2)
        first_ids = urllib.parse.parse_qs(
            urllib.parse.urlsplit(info_calls[0].get("url")).query
        )["id"][0].split(",")
        second_ids = urllib.parse.parse_qs(
            urllib.parse.urlsplit(info_calls[1].get("url")).query
        )["id"][0].split(",")
        self.assertEqual(len(first_ids), 100)
        self.assertEqual(len(second_ids), 50)
        self.assertTrue(all(value.startswith("t3_") for value in first_ids + second_ids))


class TestRateLimiting(BaseCase):
    def two_request_run(self):
        self.fake.search_children = []
        self.run_main(["search", "--subreddit", "Peptides", "--query", "x"])

    def test_one_second_spacing_between_requests(self):
        self.two_request_run()
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual(self.clock.sleeps, [1.0])

    def test_sleeps_until_reset_when_remaining_below_five(self):
        self.fake.token_headers = {"x-ratelimit-remaining": "3",
                                   "x-ratelimit-reset": "7"}
        self.two_request_run()
        # token request at t=1000; hold until t=1007; next request sleeps
        # 1.0 for spacing (t=1001) then 6.0 to reach the reset (t=1007).
        self.assertEqual(self.clock.sleeps, [1.0, 6.0])
        self.assertEqual(len(self.fake.requests), 2)

    def test_http_429_waits_for_reset_and_retries_once(self):
        self.fake.search_statuses = [429, 200]
        self.fake.data_headers = {"x-ratelimit-remaining": "3",
                                  "x-ratelimit-reset": "12"}
        self.fake.search_children = [make_post("abc12")]
        rc, out, _ = self.run_main(["search", "--subreddit", "Peptides", "--query", "x"])
        self.assertEqual(rc, 0)
        self.assertIn("r/Peptides", out)
        self.assertIn(12.0, self.clock.sleeps)
        self.assertEqual(len(self.fake.requests), 3)  # token + search twice

    def test_http_429_twice_stops_with_error(self):
        self.fake.search_statuses = [429, 429]
        rc, _, err = self.run_main(["search", "--subreddit", "Peptides", "--query", "x"])
        self.assertEqual(rc, 1)
        self.assertIn("rate limited", err)
        self.assertEqual(len(self.fake.requests), 3)  # token + search twice, no third

    def test_no_sleep_when_remaining_healthy(self):
        self.two_request_run()
        self.assertEqual(self.clock.sleeps, [1.0])  # spacing only, no reset hold


class TestNoSecrets(BaseCase):
    def test_secrets_never_printed(self):
        secret_candidates = [
            FAKE_CLIENT_ID,
            FAKE_CLIENT_SECRET,
            FAKE_PASSWORD,
            FAKE_TOKEN,
            base64.b64encode(
                (FAKE_CLIENT_ID + ":" + FAKE_CLIENT_SECRET).encode("utf-8")
            ).decode("ascii"),
        ]
        scenarios = [
            ["search", "--subreddit", "Peptides", "--query", "vendor"],
            ["note", "--url", "https://www.reddit.com/r/Peptides/comments/abc12/x/",
             "--vendor", "ExampleVendor", "--text", "note text"],
            ["search", "--subreddit", "NotAllowed", "--query", "x"],
            ["purge"],
        ]
        for argv in scenarios:
            with self.subTest(argv=argv[0]):
                rc, out, err = self.run_main(argv)
                if rc != 0:
                    self.assertIn("error:", err)
                for secret in secret_candidates:
                    self.assertNotIn(secret, out)
                    self.assertNotIn(secret, err)

    def test_secrets_never_written_to_notes(self):
        self.fake.posts["abc12"] = make_post("abc12")
        self.run_main(
            ["note", "--url", "https://www.reddit.com/r/Peptides/comments/abc12/x/",
             "--vendor", "ExampleVendor", "--text", "note text"]
        )
        with open(self.notes_file, "r", encoding="utf-8") as fh:
            raw = fh.read()
        for secret in (FAKE_CLIENT_SECRET, FAKE_PASSWORD, FAKE_TOKEN):
            self.assertNotIn(secret, raw)


class TestConfig(unittest.TestCase):
    def test_default_notes_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = os.environ.get("HOME")
            os.environ["HOME"] = tmp
            try:
                path = vc.notes_path({"username": "true_souljah"})
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
            self.assertEqual(
                path,
                os.path.join(tmp, ".souljah-vendor-check", "notes.json"),
            )

    def test_user_agent_built_from_config(self):
        cfg = {"platform": "macos", "app_id": "souljah-vendor-check",
               "version": "1.0", "username": "true_souljah"}
        self.assertEqual(vc.build_user_agent(cfg), USER_AGENT)

    def test_parse_permalink(self):
        sub, post_id = vc.parse_permalink(
            "https://www.reddit.com/r/Nootropics/comments/z9y8x7/some_title/?ctx=1"
        )
        self.assertEqual(sub, "Nootropics")
        self.assertEqual(post_id, "z9y8x7")

    def test_parse_permalink_rejects_other_hosts_and_bad_ids(self):
        for url in (
            "https://example.com/r/Peptides/comments/abc12/x/",
            "https://www.reddit.com/r/Peptides/comments/abc,12/x/",
            "ftp://www.reddit.com/r/Peptides/comments/abc12/x/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(vc.VendorCheckError):
                    vc.parse_permalink(url)

    def test_default_config_path_is_next_to_script(self):
        self.assertEqual(
            vc.DEFAULT_CONFIG_PATH,
            os.path.join(os.path.dirname(os.path.abspath(vc.__file__)), "config.json"),
        )


class TestKeychainReader(unittest.TestCase):
    def test_reads_with_security_find_generic_password(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="fake value\n", stderr="")

        with mock.patch.object(vc.subprocess, "run", fake_run):
            value = vc.read_keychain("souljah-vendor-check-client-id")
        self.assertEqual(value, "fake value")  # inner space kept, newline dropped
        self.assertEqual(
            calls,
            [["/usr/bin/security", "find-generic-password", "-s",
              "souljah-vendor-check-client-id", "-w"]],
        )

    def test_missing_entry_error_does_not_leak(self):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 44, stdout="", stderr="not found")

        with mock.patch.object(vc.subprocess, "run", fake_run):
            with self.assertRaises(vc.VendorCheckError) as ctx:
                vc.read_keychain("souljah-vendor-check-password")
        self.assertIn("setup-keychain.sh", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

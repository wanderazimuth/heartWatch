import json
import os
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from heartwatch import db, dropbox, store
from heartwatch.config import Config

from fitwriter import write

START = datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc)


class FakeDropbox(dropbox.Client):
    """Answers like Dropbox does, from a dict of path -> file contents."""

    def __init__(self, files: dict[str, bytes]):
        super().__init__("app-key", "refresh-token")
        self.files = files
        self.listed: set[str] = set()
        self.calls: list[str] = []
        self.page_size = 2

    def _request(self, url, body, headers):
        self.calls.append(url.rsplit("/2/", 1)[-1])
        if url == dropbox.TOKEN_URL:
            form = urllib.parse.parse_qs(body.decode())
            assert form["client_id"] == ["app-key"]
            return json.dumps({"access_token": "access", "refresh_token": "refresh-token"}).encode()
        assert headers["Authorization"] == "Bearer access"
        if url.endswith("/files/download"):
            return self.files[json.loads(headers["Dropbox-API-Arg"])["path"]]
        # list_folder and list_folder/continue: hand out unseen files a page at a time.
        new = [p for p in sorted(self.files) if p not in self.listed][: self.page_size]
        self.listed |= set(new)
        entries = [{".tag": "file", "name": p.rsplit("/", 1)[-1], "path_lower": p} for p in new]
        entries.append({".tag": "folder", "name": "sub", "path_lower": "/apps/wahoofitness/sub"})
        has_more = any(p not in self.listed for p in self.files) and len(new) == self.page_size
        return json.dumps({"entries": entries, "cursor": f"c{len(self.listed)}", "has_more": has_more}).encode()


class PollTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(archive_dir=self.tmp.name)
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_imports_new_files_and_remembers_the_cursor(self):
        box = FakeDropbox({
            "/apps/wahoofitness/a.fit": write(START, [130] * 60),
            "/apps/wahoofitness/b.fit": write(START.replace(day=18), [140] * 60),
            "/apps/wahoofitness/c.fit": write(START.replace(day=19), [120] * 60),
            "/apps/wahoofitness/notes.txt": b"not a workout",
        })
        result = dropbox.poll(self.conn, self.cfg, box)
        self.assertEqual(sorted(result.added), ["a.fit", "b.fit", "c.fit"])
        self.assertEqual(len(store.workouts(self.conn)), 3)
        self.assertEqual(dropbox.get_meta(self.conn, "dropbox_cursor"), "c4")
        self.assertEqual(box.calls[0], "https://api.dropboxapi.com/oauth2/token")  # one token for the whole poll
        self.assertEqual(box.calls.count("https://api.dropboxapi.com/oauth2/token"), 1)

        # Next poll continues from the cursor and only sees the new file.
        box.files["/apps/wahoofitness/d.fit"] = write(START.replace(day=20), [125] * 60)
        result = dropbox.poll(self.conn, self.cfg, box)
        self.assertEqual(result.added, ["d.fit"])
        self.assertIn("files/list_folder/continue", box.calls[-2])

    def test_reshared_file_is_not_imported_twice(self):
        data = write(START, [130] * 60)
        dropbox.poll(self.conn, self.cfg, FakeDropbox({"/apps/wahoofitness/a.fit": data}))
        result = dropbox.poll(self.conn, self.cfg, FakeDropbox({"/apps/wahoofitness/a (1).fit": data}))
        self.assertEqual(result.added, [])
        self.assertEqual(len(store.workouts(self.conn)), 1)

    def test_bad_file_is_reported(self):
        result = dropbox.poll(self.conn, self.cfg, FakeDropbox({"/apps/wahoofitness/bad.fit": b"junk"}))
        self.assertEqual(len(result.failed), 1)


class LoginTest(unittest.TestCase):
    def test_login_url_uses_pkce(self):
        url, verifier = dropbox.login_url("app-key")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["token_access_type"], ["offline"])
        self.assertNotIn(verifier, url)

    def test_token_file_is_private(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "token"
            dropbox.save_token(path, "secret\n")
            self.assertEqual(path.read_text(), "secret\n")
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

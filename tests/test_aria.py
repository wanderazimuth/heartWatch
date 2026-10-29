import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from heartwatch import aria, db, web
from heartwatch.config import Config

MAC = bytes.fromhex("001dc9d20f79")
NOW = 1793170000


def upload(readings: list[tuple[int, int, int, int]], clock: int = NOW, mac: bytes = MAC, version: int = 3) -> bytes:
    """An Aria upload: readings are (grams, scale time, user id, body fat in thousandths of a percent)."""
    body = struct.pack("<LL6s16sLLLL", version, 87, mac, b"\x00" * 16, 39, 33, clock, len(readings))
    for grams, when, user, fat in readings:
        body += struct.pack("<LLLLLLLL", 2, 500, grams, when, user, fat, 0, fat)
    return body + struct.pack("<H", aria.crc16(body))


class ParseTest(unittest.TestCase):
    def test_crc16_xmodem(self):
        self.assertEqual(aria.crc16(b"123456789"), 0x31C3)

    def test_readings(self):
        got = aria.parse(upload([(88450, NOW - 60, 12345, 22500), (90000, NOW - 30, 0, 0)]), now=NOW)
        self.assertEqual((got.mac, got.battery, got.firmware), ("001dc9d20f79", 87, 39))
        self.assertEqual(got.readings, [aria.Reading(NOW - 60, 88450, 22.5, 12345), aria.Reading(NOW - 30, 90000, None, 0)])

    def test_the_scale_clock_is_corrected(self):
        # The scale thinks it's an hour earlier: its readings are an hour later than they say.
        got = aria.parse(upload([(88450, NOW - 3600 - 60, 1, 0)], clock=NOW - 3600), now=NOW)
        self.assertEqual(got.readings[0].taken, NOW - 60)

    def test_a_resend_is_not_stored_twice(self):
        conn = db.connect(":memory:")
        body = upload([(88450, NOW - 60, 12345, 22500), (61000, NOW - 30, 0, 0)])
        self.assertEqual(aria.store_readings(conn, aria.parse(body, now=NOW).readings), 2)
        # The reply didn't reach the scale; it sends the same upload again a few minutes later.
        self.assertEqual(aria.store_readings(conn, aria.parse(body, now=NOW + 300).readings), 0)
        # A different weight, or the same one the next day, is a new weigh-in.
        later = upload([(88400, NOW + 600, 12345, 22400), (88450, NOW + 86400, 12345, 22500)], clock=NOW + 86400)
        self.assertEqual(aria.store_readings(conn, aria.parse(later, now=NOW + 86400).readings), 2)

    def test_refused(self):
        good = upload([(88450, NOW, 1, 0)])
        for bad in (good[:-1] + bytes([good[-1] ^ 1]), good[:20], upload([], version=2)):
            with self.assertRaises(ValueError):
                aria.parse(bad)

    def test_reply(self):
        cfg = Config(aria_mac="00:1d:c9:d2:0f:79", aria_name="Roy", aria_height_mm=1753, aria_birth_year=1970,
                     aria_sex="male", aria_units="lb")
        r = aria.reply(cfg, 12345, 88450, now=NOW)
        self.assertEqual(len(r), 104)  # what helvetic sends for one person
        body, (crc, tail) = r[:-4], struct.unpack("<HH", r[-4:])
        self.assertEqual((crc, tail), (aria.crc16(body), 0x19 + 0x4D))
        ts, units, status, _, users = struct.unpack_from("<LBBBL", body)
        self.assertEqual((ts, units, status, users), (NOW, 0, 0x32, 1))
        uid, name, lo, hi, age, sex, height = struct.unpack_from("<L16x20sLLLBL", body, 11)
        self.assertEqual((uid, name.rstrip(), lo, hi, age, sex, height), (12345, b"ROY", 84450, 92450, 56, 2, 1753))
        self.assertEqual(struct.unpack_from("<LLL", body, 88), (0, 3, 0))  # 3: no firmware update


class ScaleRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(db_path=str(Path(self.tmp.name) / "hw.db"), archive_dir=self.tmp.name,
                          aria_mac="00:1D:C9:D2:0F:79", aria_height_mm=1753, aria_birth_year=1970, aria_sex="male")
        self.start(self.cfg)

    def start(self, cfg):
        server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(cfg))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(self, body: bytes) -> tuple[int, bytes]:
        req = urllib.request.Request(self.base + "/scale/upload", body, {"Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def rows(self):
        conn = db.connect(self.cfg.db_path)
        try:
            return [tuple(r) for r in conn.execute("SELECT weight_g, fat_pct, person FROM body ORDER BY taken")]
        finally:
            conn.close()

    def test_upload(self):
        import time
        now = int(time.time())
        body = upload([(88450, now - 120, 12345, 22500), (61000, now - 60, 0, 0)], clock=now)
        status, answer = self.post(body)
        self.assertEqual(status, 200)
        # It recognises you by the number it already had, around the weight it just measured.
        self.assertEqual(struct.unpack_from("<L16x20sLL", answer, 11)[0::2], (12345, 84450))
        self.assertEqual(self.rows(), [(88450, 22.5, "12345"), (61000, None, "guest")])
        # A resend after a failed reply isn't stored twice.
        self.assertEqual(self.post(body)[0], 200)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(list(Path(self.tmp.name, "aria").rglob("*.bin"))), 2)

    def test_refused(self):
        self.assertEqual(self.post(upload([(88450, NOW, 1, 0)], mac=bytes(6)))[0], 403)
        self.assertEqual(self.post(b"\x03" + bytes(60))[0], 400)
        self.assertEqual(self.rows(), [])

    def test_off_without_a_mac(self):
        self.start(Config(db_path=self.cfg.db_path, archive_dir=self.tmp.name))
        self.assertEqual(self.post(upload([(88450, NOW, 1, 0)]))[0], 404)

    def test_register_and_validate(self):
        with urllib.request.urlopen(self.base + "/scale/validate") as r:
            self.assertEqual(r.read(), b"T")
        with urllib.request.urlopen(self.base + "/scale/register?serialNumber=001DC9D20F79&token=x&ssid=palantir") as r:
            self.assertEqual(r.status, 200)

    def test_rebuild_reads_the_archive_again(self):
        import time
        now = int(time.time())
        self.post(upload([(88450, now - 60, 12345, 22500)], clock=now))
        conn = db.connect(self.cfg.db_path)
        self.addCleanup(conn.close)
        with conn:
            conn.execute("DELETE FROM body")
        self.assertEqual(aria.reload(conn, self.cfg), 1)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""The board row's `type=` is a claim; the bytes are the measurement.

A voice note from Mr Salam arrived on 2026-09-19 tagged `type=image` and began
with `OggS`. Had the fetcher trusted the row it would have written a .jpg that
no transcriber would touch, and the message would have stayed unread twice over
- once for never being fetched, once for being fetched under the wrong name.

Run: python tests/test_wa_media.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import wa_media as wm  # noqa: E402


class Sniffing(unittest.TestCase):
    def test_the_voice_note_that_was_labelled_an_image(self):
        """The exact case: row said image, bytes said Ogg."""
        ext, kind = wm.sniff(b"OggS\x00\x02" + b"\x00" * 40, "image/jpeg")
        self.assertEqual((ext, kind), (".ogg", "audio"),
                         "the declared mime must not beat the magic bytes")

    def test_real_images_are_still_images(self):
        for blob, ext in ((b"\xff\xd8\xff\xe0" + b"\x00" * 20, ".jpg"),
                          (b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, ".png")):
            with self.subTest(ext=ext):
                self.assertEqual(wm.sniff(blob, "image/jpeg"), (ext, "image"))

    def test_unrecognised_bytes_fall_back_to_the_declared_mime(self):
        self.assertEqual(wm.sniff(b"\x00\x01\x02\x03", "audio/ogg"), (".ogg", "audio"))
        self.assertEqual(wm.sniff(b"\x00\x01\x02\x03", "image/webp"), (".jpg", "image"))

    def test_unknown_is_reported_as_unknown_not_guessed(self):
        """An honest .bin beats a confident wrong extension."""
        self.assertEqual(wm.sniff(b"\x00\x01\x02\x03", ""), (".bin", "unknown"))


class Parsing(unittest.TestCase):
    """The payload shape the inbound workflow actually writes."""

    def test_a_row_without_a_media_id_is_not_silently_dropped(self):
        """One of the 47 rows carried `type=unsupported` and no id at all.
        It must be reported, because 'nothing to fetch' and 'nothing sent' are
        different facts about the world."""
        import re
        payload = "WA-MEDIA|type=unsupported|media="
        self.assertIsNone(re.search(r"media=([0-9]+)", payload))
        self.assertEqual(re.search(r"type=([a-z]+)", payload).group(1), "unsupported")

    def test_a_normal_media_row_parses(self):
        import re
        payload = "WA-MEDIA|type=image|media=2203134563581011"
        self.assertEqual(re.search(r"media=([0-9]+)", payload).group(1),
                         "2203134563581011")
        self.assertEqual(re.search(r"type=([a-z]+)", payload).group(1), "image")


class Credentials(unittest.TestCase):
    def test_no_secret_is_hardcoded(self):
        src = open(wm.__file__, encoding="utf-8").read()
        for shape in ("EAA", "gsk_", "xai-", "sk-"):
            self.assertNotIn(shape + "A", src)
        self.assertIn("WA_TOKEN", src)
        self.assertIn("GROQ_API_KEY", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)

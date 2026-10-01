"""Wording-only normalisation applied before Drain3."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from logai.config import Drain3Config
from logai.models import RawLog
from logai.parsing.drain3_parser import Drain3Parser
from logai.parsing.preprocessor import find_level, normalize


class TestFindLevel(unittest.TestCase):
    def test_level_found_at_positions_one_to_five(self):
        cases = [
            ("INFO started", "INFO"),
            ("2026-09-30T07:00:32Z ERROR boom", "ERROR"),
            ("27/07/2026 00:00:00.024 info [t] x", "INFO"),
            ("2026-09-30 14:00:31,123 [main] WARN x", "WARN"),
            ("2026-09-30 14:00:31,123 12345 [main] [DEBUG] x", "DEBUG"),
        ]
        for message, expected in cases:
            found = find_level(message)
            self.assertIsNotNone(found, message)
            self.assertEqual(found[0], expected, message)

    def test_level_after_five_tokens_is_ignored(self):
        self.assertIsNone(find_level("1 2 3 4 5 ERROR late"))

    def test_level_word_after_plain_word_is_not_a_prefix(self):
        self.assertIsNone(find_level("Connection ERROR happened"))

    def test_no_level(self):
        self.assertIsNone(find_level("just a plain message"))
        self.assertIsNone(find_level(""))


class TestNormalize(unittest.TestCase):
    def test_prefix_level_and_thread_removed(self):
        self.assertEqual(
            normalize("27/07/2026 00:00:02.152  INFO [send-5] con-1-Sender: check is connected first"),
            "con-<*>-Sender: check is connected first",
        )

    def test_timestamps_removed_not_masked(self):
        self.assertEqual(
            normalize("RequestHandler: status = 000, active time: Thu Aug 13 18:52:11 GMT+07:00 2020"),
            "RequestHandler: status <*> active time:",
        )
        self.assertEqual(normalize("done at 2026-09-30T07:00:32.036Z ok"), "done at ok")
        self.assertEqual(normalize("tick 18:52:11.120 ok"), "tick ok")

    def test_plain_numbers_are_not_mistaken_for_timestamps(self):
        self.assertEqual(normalize("port 8685 mirror 50010"), "port <*> mirror <*>")

    def test_xml_reduced_to_element_names_and_words(self):
        message = (
            '<S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body>'
            "<ns2:gwOperationResponse><error>0</error><description>success</description>"
            "</ns2:gwOperationResponse></S:Body></S:Envelope>"
        )
        self.assertEqual(normalize(message), "gwOperationResponse error <*> description success")

    def test_ids_masked_words_kept(self):
        self.assertEqual(
            normalize("request 6419a71e-ccf8-46e9-af77-c7cf036aa215 ptr 0x7f3a9b2c from 10.0.0.1:80 by a.b@c.com"),
            "request <*> ptr <*> from <*> by <*>",
        )
        self.assertEqual(
            normalize("encryptedPass=MlebjTtuWaLRN5ek0Rkwn3MuLNc=, product=TOM690"),
            "encryptedPass <*> product TOM<*>",
        )

    def test_url_host_masked_path_words_kept(self):
        self.assertEqual(
            normalize("call ====http://10.60.129.245:8685/BCCSGateway?wsdl"),
            "call <*>/BCCSGateway?wsdl",
        )

    def test_placeholder_runs_collapse(self):
        self.assertEqual(
            normalize("replicate to datanode(s) 10.0.0.1:1 10.0.0.2:2 10.0.0.3:3"),
            "replicate to datanode s <*>",
        )
        self.assertEqual(normalize("history: *136#"), "history: <*>")

    def test_token_cap(self):
        self.assertEqual(len(normalize(" ".join(f"w{chr(97 + i % 26)}" for i in range(100)), max_tokens=40).split()), 40)


class TestDrain3ParserUsesPreprocessor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.parser = Drain3Parser(
            Drain3Config(persistence_path=os.path.join(self.tmp, "state.bin"), sim_threshold=0.5)
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _parse(self, message: str):
        return self.parser.parse(RawLog(timestamp=1000.0, service="svc", level="INFO", message=message))

    def test_sender_and_receiver_stay_distinct(self):
        sender = self._parse("27/07/2026 00:00:23.284  INFO [send-1] con-0-Sender: send message")
        receiver = self._parse("27/07/2026 00:00:26.165  INFO [NioProcessor-40] con-1-Receiver: receive message")
        self.assertNotEqual(sender.template_id, receiver.template_id)

    def test_same_wording_different_values_share_template(self):
        a = self._parse("27/07/2026 00:00:17.876  INFO [worker-1] LixiHandler: 84396081821 is valid to transfer")
        b = self._parse("28/07/2026 11:02:03.001  INFO [worker-9] LixiHandler: 84387836245 is valid to transfer")
        self.assertEqual(a.template_id, b.template_id)
        self.assertEqual(b.template, "LixiHandler: <*> is valid to transfer")

    def test_placeholder_heavy_messages_reuse_their_cluster(self):
        """Drain3 ignores its own wildcard when scoring similarity, so a message
        that is mostly placeholders must still match its cluster instead of
        creating a new one on every occurrence."""
        ids = {
            self._parse(message).template_id
            for message in [
                "x 1 y 2 3 z 4",
                "x 5 y 6 7 z 8",
                "x 9 y 10 11 z 12",
            ]
        }
        self.assertEqual(len(ids), 1)
        for message in ("84396081821", "84387836245", "10.0.0.1:80 12"):
            self._parse(message)
        self.assertEqual(self.parser.cluster_count(), 2)
        self.assertEqual(self._parse("12345").template, "<*>")

    def test_preprocess_can_be_disabled(self):
        cfg = Drain3Config(persistence_path=os.path.join(self.tmp, "raw.bin"), preprocess=False)
        self.assertEqual(Drain3Parser(cfg).preprocess("27/07/2026 INFO x"), "27/07/2026 INFO x")


if __name__ == "__main__":
    unittest.main()

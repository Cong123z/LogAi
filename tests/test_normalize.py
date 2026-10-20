"""Wording-only normalisation applied before Drain3."""
from __future__ import annotations

import unittest

from logai2.reader.normalize import find_level, normalize


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


class TestNormalizeMissingParts(unittest.TestCase):
    """Rules that don't match are skipped; nothing raises."""

    def test_production_gossip_line(self):
        message = (
            "30/09/2026 14:00:31 DEBUG [gossip-handlers-321] "
            "jgroups:name=NewGossipRouterWARNING_EXTEND_DATA_MI_HT2: "
            "ConnectionHandler[peer: /10.240.175.140, logical_addrs: "
            "warning_extend_data_mi_ht2_node2] responded to GOSSIP_GET with []"
        )
        self.assertEqual(find_level(message)[0], "DEBUG")
        self.assertEqual(
            normalize(message),
            "jgroups:name NewGossipRouterWARNING_EXTEND_DATA_MI_HT<*>: ConnectionHandler "
            "peer: <*> logical_addrs: warning_extend_data_mi_ht<*>_node<*> "
            "responded to GOSSIP_GET with",
        )

    def test_timestamp_and_thread_without_level(self):
        self.assertEqual(normalize("30/09/2026 14:00:31 [main] something failed"), "something failed")
        self.assertEqual(normalize("2026-09-30T07:00:32.036Z - started ok"), "started ok")

    def test_no_timestamp_no_level(self):
        self.assertEqual(
            normalize("ConnectionHandler responded to GOSSIP_GET with []"),
            "ConnectionHandler responded to GOSSIP_GET with",
        )
        self.assertEqual(normalize("[main] Connection ERROR here"), "main Connection ERROR here")

    def test_level_without_timestamp(self):
        self.assertEqual(normalize("ERROR something failed"), "something failed")

    def test_non_string_input(self):
        self.assertEqual(normalize(None), "")
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize("   "), "")
        self.assertEqual(normalize(123), "<*>")


if __name__ == "__main__":
    unittest.main()

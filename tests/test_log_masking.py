"""Masking rules of the wording-only normalizer (logai2/reader/normalize.py).

Copied from logai-engine's test_log_masking.py. The originals parsed through
Drain3 only to read the mined template of a single message, which is the
normalize output, so they call normalize directly here. Tests that need Drain3
clustering (template convergence) drive normalize then
``logai2.owner.miners.Miners.match``.
"""
from __future__ import annotations

import unittest

from logai2.config import Drain3Config
from logai2.owner.miners import Miners
from logai2.reader.normalize import normalize


def _match(miners: Miners, message: str):
    return miners.match("svc", normalize(message))


class TestLogMasking(unittest.TestCase):
    def test_t00027_preserves_exception_keyword(self):
        """T00027 does NOT lose the 'Exception' keyword when IP:port varies."""
        miners = Miners(Drain3Config(), {})
        id1, _, _ = _match(miners, "10.250.10.1:50010:Exception writing block blk_111 to mirror 10.250.10.2:50010")
        id2, template, _ = _match(miners, "10.250.10.3:50010:Exception writing block blk_222 to mirror 10.250.10.4:50010")
        self.assertEqual(id1, id2)
        self.assertIn("Exception", template)
        self.assertIn("writing block", template)
        self.assertIn("to mirror", template)
        self.assertNotIn("10.250.10.1", template)
        self.assertNotIn("blk_111", template)

    def test_keywords_preserved_across_critical_messages(self):
        """Verify Transmitted, Failed, and Got exception keywords are preserved."""
        samples = [
            ("10.250.10.2:50010:Transmitted block blk_123 to /10.250.10.3:50010", "Transmitted"),
            ("10.250.10.1:50010:Failed to transfer blk_123 to 10.250.10.2:50010 got java.io.IOException: Connection reset by peer", "Failed"),
            ("10.250.10.1:50010:Got exception while serving blk_123 to /10.250.10.2:34567:", "Got exception"),
            ("10.250.10.1:50010:Exception writing block blk_111 to mirror 10.250.10.2:50010", "Exception writing block"),
        ]
        for msg, expected_kw in samples:
            self.assertIn(expected_kw, normalize(msg))


class TestUniversalMaskingRules(unittest.TestCase):
    """The date/time/LEVEL/[thread] prefix and every timestamp are removed,
    ids/numbers become <*>, words are kept. The samples are production-shaped;
    they verify the strategy, they are not what the rules were derived from."""

    def test_uuid_and_timestamp_shapes_masked(self):
        """UUIDs are masked and ISO timestamps removed even standing alone in
        free text, independent of any key=value context."""
        a = normalize("request 6419a71e-ccf8-46e9-af77-c7cf036aa215 completed at 2026-09-30T07:00:32.036Z")
        b = normalize("request 19383617-5be0-48c9-8f85-25bc71ee3c20 completed at 2026-10-01T11:22:03.100Z")
        self.assertEqual(a, b)
        self.assertEqual(b, "request <*> completed at")

    def test_multi_token_dates_are_removed(self):
        """Multi-token timestamps are removed entirely (@timestamp already
        carries the time), not fragmented by the number rule into pieces."""
        self.assertEqual(
            normalize("x activeTime=Sat Sep 21 14:27:33 GMT+07:00 2019, at 27/07/2026 00:00:00.024 done"),
            "x activeTime at done",
        )

    def test_xml_payload_reduced_to_element_names_and_words(self):
        """SOAP wrappers, namespaces and attributes are dropped; element names
        and text words are kept, so success and failure stay distinguishable."""
        message = (
            "27/07/2026 00:00:01.100 INFO [worker-1] BCCSUtil: Response of getInfoSub request for 84379383220: "
            "<S:Envelope xmlns:S=\"http://schemas.xmlsoap.org/soap/envelope/\">\n<S:Body>\n"
            "<ns2:gwOperationResponse><Result><error>0</error><description>success</description></Result>"
            "</ns2:gwOperationResponse>\n</S:Body>\n</S:Envelope>"
        )
        self.assertEqual(
            normalize(message),
            "BCCSUtil: Response of getInfoSub request for <*> gwOperationResponse Result "
            "error <*> description success",
        )

    def test_free_text_values_keep_their_words(self):
        """Free-text values are wording, so they are kept: two different
        descriptions are two different templates. Key names stay visible and
        numeric values are masked."""
        a = normalize("27/07/2026 00:00:00.723 INFO [TM] Transaction timeout: Info{reqID=1, description=Receive incorrect, sentType=6}")
        b = normalize("27/07/2026 00:00:00.724 INFO [TM] Transaction timeout: Info{reqID=2, description=Customer havent created password yet, sentType=4}")
        self.assertEqual(a, "Transaction timeout: Info reqID <*> description Receive incorrect sentType <*>")
        self.assertEqual(
            b,
            "Transaction timeout: Info reqID <*> description Customer havent created password yet sentType <*>",
        )

    def test_trailing_value_does_not_swallow_free_text(self):
        """Outside a key=value list, a value is one token: the action words
        after it must survive so distinct actions stay distinct templates."""
        a = normalize("27/07/2026 00:00:01.026 INFO [w-1] UssdAppInf:type=Gw,name=Ussd1 connector for send: con-0")
        b = normalize("27/07/2026 00:00:01.027 INFO [w-1] UssdAppInf:type=Gw,name=Ussd1 sending ...")
        self.assertNotEqual(a, b)
        self.assertIn("connector for send", a)
        self.assertIn("sending", b)

    def test_leading_prefix_stripped_case_insensitive(self):
        self.assertEqual(normalize("30/09/2026 14:00:31 info [t-1] hello world"), "hello world")

    def test_transaction_blob_values_masked_to_wildcard(self):
        """Real TransactionManager sample: key names stay literal, ids and
        secrets are masked, the embedded activeTime timestamp and the leading
        date/time/LEVEL/[thread] prefix are removed, words are kept."""
        message = (
            "27/07/2026 00:00:00.024  INFO [TransactionManager] DBAdapter: "
            "Log success request his TransactionInfo{reqID=1384223044, "
            "transID=W12-89082-1785086988853, msisdn=84344872426, "
            "encryptedPass=MlebjTtuWaLRN5ek0Rkwn3MuLNc=, "
            "activeTime=Sat Sep 21 14:27:33 GMT+07:00 2019, "
            "description=Receive incorrect, numHisTrans=0}"
        )
        self.assertEqual(
            normalize(message),
            "DBAdapter: Log success request his TransactionInfo reqID <*> transID <*> "
            "msisdn <*> encryptedPass <*> activeTime description Receive incorrect numHisTrans <*>",
        )

    def test_transaction_blob_normalizes_identically(self):
        """Two transaction logs differing in every variable value (including
        timestamp and thread name) normalize to the same text."""
        def make(req_id, trans_id, enc_pass, ts, thread):
            return (
                f"{ts}  INFO [{thread}] DBAdapter: Log success request his "
                f"TransactionInfo{{reqID={req_id}, transID={trans_id}, "
                f"encryptedPass={enc_pass}, description=Receive incorrect}}"
            )

        msg_a = make("1384223044", "W12-89082-1785086988853", "MlebjTtuWaLRN5ek0Rkwn3MuLNc=", "27/07/2026 00:00:00.024", "TransactionManager")
        msg_b = make("999999999", "W99-11111-9999999999999", "ZZZZ9999AAAA1111bbbb2222=", "30/07/2026 11:22:33.999", "TransactionManagerXYZ")
        self.assertEqual(normalize(msg_a), normalize(msg_b))

    def test_logfmt_style_space_separated_converges(self):
        """A space-separated logfmt-style message (no commas/braces, no
        [thread]) still converges when only the values differ."""
        a = normalize("level=info ts=2026-09-30T07:00:32Z component=router req_id=abc123")
        b = normalize("level=info ts=2026-10-01T09:11:03Z component=router req_id=xyz999")
        miners = Miners(Drain3Config(), {})
        self.assertEqual(miners.match("svc", a)[0], miners.match("svc", b)[0])

    def test_leading_prefix_stripped_for_any_thread_name(self):
        """The leading date/time/LEVEL/[thread] prefix is stripped regardless
        of what the thread name actually is."""
        a = normalize("30/09/2026 14:00:31 DEBUG [gossip-handlers-321] jgroups:name=Router responded to GET with []")
        b = normalize("01/10/2026 09:12:45 DEBUG [gossip-handlers-999] jgroups:name=Router responded to GET with []")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("jgroups:"), a)


if __name__ == "__main__":
    unittest.main()

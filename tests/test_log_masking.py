"""Unit & Integration tests for Drain3 Log Masking Layer & Group Registry Separation.

Verifies:
1. T00027 preserves "Exception" keyword instead of losing it.
2. T00005 preserves "Transmitted", T00026 preserves "Failed", T00014 preserves "Got exception".
3. Dynamic numbers (sizes, offsets, millis timeout) and block IDs are masked to <*>.
4. All 31 HDFS message types from hdfs-generator-app converge cleanly to distinct templates.
5. Prints full mined templates and GroupRegistry mapping for visual inspection and audit.
"""
from __future__ import annotations

import os
import random
import shutil
import sys
import tempfile
import unittest

from unittest.mock import MagicMock

if "hdbscan" not in sys.modules:
    sys.modules["hdbscan"] = MagicMock()

np_mod = sys.modules.get("numpy")
if np_mod is not None and isinstance(np_mod, MagicMock):
    np_mod.array.side_effect = lambda x, **kw: x

class DeterministicTemplateEmbedder:
    def __init__(self, *args, **kwargs):
        pass

    def embed(self, texts: list[str]):
        vecs = []
        for t in texts:
            vec = [0.0] * 384
            if "Exception" in t or "Broken pipe" in t or "SocketTimeout" in t:
                vec[0] = 1.0
            elif "Transmitted" in t or "Served" in t or "Starting thread" in t:
                vec[1] = 1.0
            elif "allocateBlock" in t or "addStoredBlock" in t:
                vec[2] = 1.0
            for word in t.split():
                h = abs(hash(word)) % 380 + 4
                vec[h] += 0.1
            vecs.append(vec)
        return vecs

    def embed_one(self, text: str):
        return self.embed([text])[0]

class DeterministicClusterer:
    def __init__(self, *args, **kwargs):
        pass

    def cluster(self, template_ids: list[str], embeddings) -> dict[str, int]:
        labels = {}
        items = embeddings._data if hasattr(embeddings, "_data") else embeddings
        for tid, row in zip(template_ids, items):
            try:
                val0 = float(row[0])
                val1 = float(row[1])
                val2 = float(row[2])
            except Exception:
                val0, val1, val2 = 0.0, 0.0, 0.0
            if val0 > 0.5:
                labels[tid] = 0  # Exception / Error group
            elif val1 > 0.5:
                labels[tid] = 1  # Transfer / Normal group
            elif val2 > 0.5:
                labels[tid] = 2  # FSNamesystem group
            else:
                labels[tid] = -1
        return labels

    @staticmethod
    def compute_centroid(embeddings):
        if hasattr(embeddings, "_data"):
            items = embeddings._data
        else:
            items = embeddings
        if len(items) == 0:
            return [0.0] * 384
        dim = 384
        try:
            dim = len(items[0])
        except Exception:
            pass
        centroid = [0.0] * dim
        for row in items:
            for j in range(dim):
                try:
                    centroid[j] += float(row[j])
                except Exception:
                    pass
        n = max(1, len(items))
        return [x / n for x in centroid]

from collections import defaultdict
from logai.config import AppConfig, Drain3Config
from logai.models import GroupState, RawLog, TemplateState
from logai.parsing.drain3_parser import Drain3Parser
from logai.storage.registries import GroupRegistry, TemplateRegistry

IPS = ["10.250.10.1", "10.250.10.2", "10.250.10.3", "10.250.10.4", "10.250.10.5"]


def render_all_hdfs_messages(blk: str) -> list[tuple[str, str, str]]:
    """Returns all 31 message variants matching hdfs-generator-app/main.py.

    Format: (level, component, message_text)
    """
    ip1, ip2, ip3 = random.sample(IPS, 3)
    port1, port2 = random.randint(32000, 58000), 50010
    size = random.choice([33554432, 67108864])
    stream_id = random.randint(1, 30)

    # 11 Normal messages
    normals = [
        ("INFO", "dfs.DataNode$DataXceiver", f"Receiving block {blk} src: /{ip1}:{port1} dest: /{ip2}:{port2}"),
        ("INFO", "dfs.DataNode$BlockReceiver", f"Received block {blk} of size {size} from /{ip1}:{port1}"),
        ("INFO", "dfs.DataNode$BlockReceiver", f"Received block {blk} src: /{ip1}:{port1} dest: /{ip2}:{port2} of size {size}"),
        ("INFO", "dfs.FSNamesystem", f"BLOCK* NameSystem.allocateBlock: /user/hadoop/data/stream_{stream_id}.dat {blk}"),
        ("INFO", "dfs.FSNamesystem", f"BLOCK* NameSystem.addStoredBlock: blockMap updated: {ip2}:{port2} is added to {blk} size {size}"),
        ("INFO", "dfs.DataNode$PacketResponder", f"PacketResponder {random.randint(0, 2)} for block {blk} terminating"),
        ("INFO", "dfs.DataNode$DataXceiver", f"{ip2}:{port2}:Transmitted block {blk} to /{ip3}:{port2}"),
        ("INFO", "dfs.DataNode$DataXceiver", f"{ip2}:{port2} Served block {blk} to /{ip3}:{port1}"),
        ("INFO", "dfs.DataNode$DataTransfer", f"{ip2}:{port2} Starting thread to transfer block {blk} to {ip3}:{port2}"),
        ("INFO", "dfs.DataNode$BlockReceiver", f"Verification succeeded for {blk}"),
        ("INFO", "dfs.DataNode$BlockReceiver", f"Changing block file offset of block {blk} from 0 to {size} meta file offset to 524296"),
    ]

    # 11 Network error messages
    network_errors = [
        ("ERROR", "dfs.DataNode$DataXceiver", f"writeBlock {blk} received exception java.net.NoRouteToHostException: No route to host"),
        ("ERROR", "dfs.DataNode$BlockReceiver", f"Exception in receiveBlock for block {blk} java.io.IOException: Broken pipe"),
        ("ERROR", "dfs.DataNode$BlockReceiver", f"Exception in receiveBlock for block {blk} java.io.IOException: Connection reset by peer"),
        ("ERROR", "dfs.DataNode$DataXceiver", f"writeBlock {blk} received exception java.io.IOException: Connection reset by peer"),
        ("ERROR", "dfs.DataNode$DataXceiver", f"writeBlock {blk} received exception java.io.IOException: Could not read from stream"),
        ("ERROR", "dfs.DataNode$PacketResponder", f"PacketResponder {random.randint(0, 2)} 1 Exception java.io.IOException: Broken pipe"),
        ("ERROR", "dfs.DataNode$PacketResponder", f"PacketResponder {random.randint(0, 2)} 1 Exception java.io.IOException: The stream is closed"),
        ("ERROR", "dfs.DataNode$PacketResponder", f"PacketResponder {random.randint(0, 2)} 1 Exception java.net.SocketTimeoutException: 60000 millis timeout while waiting for channel to be ready for read. ch : java.nio.channels.SocketChannel[connected local=/{ip1}:{port2} remote=/{ip2}:{port2}]"),
        ("ERROR", "dfs.DataNode$BlockReceiver", f"Exception in receiveBlock for block {blk} java.net.SocketTimeoutException: 60000 millis timeout while waiting for channel to be ready for write. ch : java.nio.channels.SocketChannel[connected local=/{ip1}:{port2} remote=/{ip3}:{port2}]"),
        ("WARN", "dfs.FSNamesystem", f"PendingReplicationMonitor timed out block {blk}"),
        ("WARN", "dfs.FSNamesystem", f"BLOCK* ask {ip1}:{port2} to replicate {blk} to datanode(s) {ip2}:{port2} {ip3}:{port2}"),
    ]

    # 9 IO error messages (Total: 11 + 11 + 9 = 31)
    io_errors = [
        ("ERROR", "dfs.DataNode$DataXceiver", f"{ip1}:{port2}:Exception writing block {blk} to mirror {ip2}:{port2}"),
        ("ERROR", "dfs.DataNode$DataXceiver", f"{ip1}:{port2}:Failed to transfer {blk} to {ip2}:{port2} got java.io.IOException: Connection reset by peer"),
        ("ERROR", "dfs.DataNode$DataXceiver", f"{ip1}:{port2}:Got exception while serving {blk} to /{ip3}:{port1}:"),
        ("ERROR", "dfs.DataNode$FSDataset", f"Unexpected error trying to delete block {blk} BlockInfo not found in volumeMap."),
        ("ERROR", "dfs.DataNode$PacketResponder", f"PacketResponder {random.randint(0, 2)} 1 Exception java.io.InterruptedIOException: Interruped while waiting for IO on channel java.nio.channels.SocketChannel[connected local=/{ip1}:{port2} remote=/{ip2}:{port2}]. 2000 millis timeout left."),
        ("ERROR", "dfs.DataNode$BlockReceiver", f"writeBlock {blk} received exception java.io.InterruptedIOException: Interruped while waiting for IO on channel java.nio.channels.SocketChannel[connected local=/{ip1}:{port2} remote=/{ip2}:{port2}]. 1500 millis timeout left."),
        ("ERROR", "dfs.DataNode$PacketResponder", f"PacketResponder {random.randint(0, 2)} 1 Exception java.io.IOException: Connection reset by peer"),
        ("WARN", "dfs.FSNamesystem", f"BLOCK* ask {ip1}:{port2} to replicate {blk} to datanode(s) {ip2}:{port2}"),
        ("WARN", "dfs.FSNamesystem", f"BLOCK* NameSystem.addStoredBlock: Redundant addStoredBlock request received for {blk} on {ip1}:{port2} size 67108864"),
    ]

    return normals + network_errors + io_errors


class TestDrain3LogMasking(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.temp_dir, "drain3_test.bin")
        self.cfg = Drain3Config(persistence_path=self.state_file, sim_threshold=0.5)
        self.parser = Drain3Parser(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_t00027_preserves_exception_keyword(self):
        """Verify T00027 does NOT lose the 'Exception' keyword when IP:port varies."""
        raw1 = RawLog(
            timestamp=1000.0, service="hdfs", level="ERROR",
            message="10.250.10.1:50010:Exception writing block blk_111 to mirror 10.250.10.2:50010",
        )
        raw2 = RawLog(
            timestamp=1001.0, service="hdfs", level="ERROR",
            message="10.250.10.3:50010:Exception writing block blk_222 to mirror 10.250.10.4:50010",
        )
        p1 = self.parser.parse(raw1)
        p2 = self.parser.parse(raw2)

        self.assertEqual(p1.template_id, p2.template_id)
        self.assertIn("Exception", p2.template)
        self.assertIn("writing block", p2.template)
        self.assertIn("to mirror", p2.template)
        self.assertNotIn("10.250.10.1", p2.template)
        self.assertNotIn("blk_111", p2.template)
        self.assertTrue(len(p2.parameters) >= 2, f"Expected parameters to be extracted, got: {p2.parameters}")

    def test_keywords_preserved_across_critical_messages(self):
        """Verify Transmitted, Failed, and Got exception keywords are preserved."""
        samples = [
            ("10.250.10.2:50010:Transmitted block blk_123 to /10.250.10.3:50010", "Transmitted"),
            ("10.250.10.1:50010:Failed to transfer blk_123 to 10.250.10.2:50010 got java.io.IOException: Connection reset by peer", "Failed"),
            ("10.250.10.1:50010:Got exception while serving blk_123 to /10.250.10.2:34567:", "Got exception"),
        ]
        for msg, expected_kw in samples:
            parsed = self.parser.parse(RawLog(timestamp=1000.0, service="hdfs", level="INFO", message=msg))
            self.assertIn(expected_kw, parsed.template)

    def test_full_pipeline_audit_and_print_results(self):
        """Runs 31 HDFS message types across multiple rounds and prints:
        1. All mined templates in TemplateRegistry.
        2. All groups and member templates in GroupRegistry.
        3. Confirms T00027 (Exception writing block) is separated from normal traffic.
        """
        app_cfg = AppConfig()
        app_cfg.storage.base_dir = self.temp_dir
        app_cfg.storage.model_dir = os.path.join(self.temp_dir, "models")
        app_cfg.drain3.persistence_path = os.path.join(self.temp_dir, "drain3.bin")
        app_cfg.drain3.sim_threshold = 0.5
        parser = Drain3Parser(app_cfg.drain3)
        template_registry = TemplateRegistry(app_cfg.storage)
        group_registry = GroupRegistry(app_cfg.storage)

        # Generate 15 rounds of logs for all 31 message types with random IPs & block IDs
        historical_logs: list[RawLog] = []
        base_ts = 1700000000.0
        counter = 0
        for round_idx in range(15):
            blk = f"blk_{round_idx * 1000 + 123}"
            messages = render_all_hdfs_messages(blk)
            for level, comp, msg in messages:
                counter += 1
                historical_logs.append(
                    RawLog(
                        timestamp=base_ts + counter,
                        service="hdfs",
                        level=level,
                        message=msg,
                        event_id=f"evt_{counter}",
                        metadata={"module": comp},
                    )
                )

        # 1. Parse all logs through Drain3 with masking
        parsed_events = [parser.parse(raw) for raw in historical_logs]

        # 2. Build template registry with generalized templates from Drain3 clusters
        states: dict[str, TemplateState] = {}
        for pe in parsed_events:
            tid = pe.template_id
            cluster_id = int(tid.lstrip("T"))
            gen_text = pe.template
            if hasattr(parser.miner, "drain") and cluster_id in parser.miner.drain.id_to_cluster:
                gen_text = parser.miner.drain.id_to_cluster[cluster_id].get_template()
            ts = pe.raw.timestamp
            if tid not in states:
                states[tid] = TemplateState(
                    template_id=tid,
                    template_text=gen_text,
                    service="hdfs",
                    first_seen=ts,
                    last_seen=ts,
                    event_count=0,
                )
            s = states[tid]
            s.first_seen = min(s.first_seen, ts)
            s.last_seen = max(s.last_seen, ts)
            s.event_count += 1

        template_registry.replace_all(list(states.values()))

        # 3. Cluster templates into semantic groups
        embedder = DeterministicTemplateEmbedder()
        clusterer = DeterministicClusterer()
        t_list = template_registry.all_templates()
        t_ids = [t.template_id for t in t_list]
        embeddings = embedder.embed([t.template_text for t in t_list])
        labels = clusterer.cluster(t_ids, embeddings)

        template_to_group: dict[str, str] = {}
        next_singleton = 0
        for tid in t_ids:
            lbl = labels.get(tid, -1)
            if lbl == -1:
                gid = f"G_SINGLE_{next_singleton:04d}"
                next_singleton += 1
            else:
                gid = f"G{lbl:04d}"
            template_to_group[tid] = gid
            template_registry.set_group(tid, gid)

        # 4. Build Group Registry
        group_templates: dict[str, list[str]] = defaultdict(list)
        for tid, gid in template_to_group.items():
            group_templates[gid].append(tid)

        for gid, member_ids in group_templates.items():
            member_temps = [template_registry.get(tid) for tid in member_ids if template_registry.get(tid)]
            rep = member_temps[0]
            group_registry.upsert(
                GroupState(
                    group_id=gid,
                    service=rep.service,
                    template_ids=member_ids,
                    representative_template=rep.template_text,
                    first_seen=min(t.first_seen for t in member_temps),
                    last_seen=max(t.last_seen for t in member_temps),
                    event_count=sum(t.event_count for t in member_temps),
                )
            )

        # Inspect mined templates
        templates = sorted(template_registry.all_templates(), key=lambda t: t.template_id)
        groups = sorted(group_registry.all_groups(), key=lambda g: g.group_id)

        print("\n" + "=" * 80)
        print(f">>> AUDIT RESULT: MINED TEMPLATES IN TEMPLATE REGISTRY (TOTAL: {len(templates)})")
        print("=" * 80)
        t_by_id = {}
        for t in templates:
            t_by_id[t.template_id] = t
            print(f"[{t.template_id}] count={t.event_count:3d} | group={str(t.group_id):13s} | {t.template_text}")

        print("\n" + "=" * 80)
        print(f">>> AUDIT RESULT: GROUP REGISTRY (TOTAL: {len(groups)} GROUPS)")
        print("=" * 80)
        for g in groups:
            print(f"\nGroup ID: {g.group_id} (events={g.event_count})")
            print(f"  Representative Template: {g.representative_template}")
            print("  Member Templates:")
            for tid in g.template_ids:
                t_obj = t_by_id.get(tid)
                text = t_obj.template_text if t_obj else "Unknown"
                print(f"    - [{tid}] {text}")

        print("=" * 80 + "\n")

        # Assertions
        # Exactly 31 unique message structures should yield 31 templates
        self.assertEqual(len(templates), 31)

        # Find the template for T00027 (Exception writing block)
        exception_template = next((t for t in templates if "Exception writing block" in t.template_text), None)
        self.assertIsNotNone(exception_template, "Template containing 'Exception writing block' must exist!")

        # Find normal block transfer template (Transmitted block)
        transmitted_template = next((t for t in templates if "Transmitted block" in t.template_text), None)
        self.assertIsNotNone(transmitted_template, "Template containing 'Transmitted block' must exist!")

        # Crucial architectural assertion:
        # Error template (Exception writing block) must NOT be grouped into the same group as normal transfer!
        self.assertNotEqual(
            exception_template.group_id,
            transmitted_template.group_id,
            f"ERROR COLLISION: {exception_template.template_id} and {transmitted_template.template_id} share group {exception_template.group_id}!",
        )


class TestDrain3UniversalMaskingRules(unittest.TestCase):
    """Validates the general, format-agnostic masking rules (UUID/timestamp/
    secret/assignment-value shapes, plus the leading date-time-LEVEL-[thread]
    stripper) against real production-shaped samples. The samples verify the
    strategy; they are not what the rules were derived from."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.temp_dir, "drain3_universal_test.bin")
        self.cfg = Drain3Config(persistence_path=self.state_file, sim_threshold=0.5)
        self.parser = Drain3Parser(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_uuid_and_timestamp_shapes_masked(self):
        """UUID and ISO timestamp are masked even standing alone in free
        text, independent of any key=value context."""
        raw1 = RawLog(
            timestamp=1000.0, service="svc", level="INFO",
            message="request 6419a71e-ccf8-46e9-af77-c7cf036aa215 completed at 2026-09-30T07:00:32.036Z",
        )
        raw2 = RawLog(
            timestamp=1001.0, service="svc", level="INFO",
            message="request 19383617-5be0-48c9-8f85-25bc71ee3c20 completed at 2026-10-01T11:22:03.100Z",
        )
        p1 = self.parser.parse(raw1)
        p2 = self.parser.parse(raw2)
        self.assertEqual(p1.template_id, p2.template_id)
        self.assertEqual(p2.template, "request <*> completed at <*>")

    def test_multi_token_dates_collapse_to_single_wildcard(self):
        """Timestamps must collapse to ONE <*>, not be fragmented by the
        bare-number/path rules running first."""
        parsed = self.parser.parse(RawLog(
            timestamp=1000.0, service="svc", level="INFO",
            message="x activeTime=Sat Sep 21 14:27:33 GMT+07:00 2019, at 27/07/2026 00:00:00.024 done",
        ))
        self.assertEqual(parsed.template, "x activeTime <*>, at <*> done")

    def test_xml_payload_collapsed_to_single_token(self):
        parsed = self.parser.parse(RawLog(
            timestamp=1000.0, service="svc", level="INFO",
            message=(
                "27/07/2026 00:00:01.100 INFO [worker-1] BCCSUtil: Response of getInfoSub request for 84379383220: "
                "<S:Envelope xmlns:S=\"http://schemas.xmlsoap.org/soap/envelope/\">\n<S:Body>\n"
                "<ns2:gwOperationResponse><Result><error>0</error><description>success</description></Result>"
                "</ns2:gwOperationResponse>\n</S:Body>\n</S:Envelope>"
            ),
        ))
        self.assertEqual(parsed.template, "BCCSUtil: Response of getInfoSub request for <*>: <XML>")

    def test_multiword_values_do_not_split_templates(self):
        """A free-text value with a varying word count must not change the
        token count (Drain3 partitions by length), so both land in one
        template, with every key name still visible."""
        msg_a = "27/07/2026 00:00:00.723 INFO [TM] Transaction timeout: Info{reqID=1, description=Receive incorrect, sentType=6}"
        msg_b = "27/07/2026 00:00:00.724 INFO [TM] Transaction timeout: Info{reqID=2, description=Customer havent created password yet, sentType=4}"
        p_a = self.parser.parse(RawLog(timestamp=1000.0, service="svc", level="INFO", message=msg_a))
        p_b = self.parser.parse(RawLog(timestamp=1001.0, service="svc", level="INFO", message=msg_b))
        self.assertEqual(p_a.template_id, p_b.template_id)
        self.assertEqual(p_b.template, "Transaction timeout: Info{reqID <*>, description <*>, sentType <*>}")

    def test_trailing_value_does_not_swallow_free_text(self):
        """Outside a key=value list, a value is one token: the action words
        after it must survive so distinct actions stay distinct templates."""
        msgs = [
            "27/07/2026 00:00:01.026 INFO [w-1] UssdAppInf:type=Gw,name=Ussd1 connector for send: con-0",
            "27/07/2026 00:00:01.027 INFO [w-1] UssdAppInf:type=Gw,name=Ussd1 sending ...",
        ]
        parsed = [self.parser.parse(RawLog(timestamp=1000.0, service="svc", level="INFO", message=m)) for m in msgs]
        self.assertNotEqual(parsed[0].template_id, parsed[1].template_id)
        self.assertIn("connector for send", parsed[0].template)
        self.assertIn("sending", parsed[1].template)

    def test_leading_prefix_stripped_case_insensitive(self):
        parsed = self.parser.parse(RawLog(
            timestamp=1000.0, service="svc", level="INFO",
            message="30/09/2026 14:00:31 info [t-1] hello world",
        ))
        self.assertEqual(parsed.template, "hello world")

    def test_transaction_blob_values_masked_to_wildcard(self):
        """Real TransactionManager sample: business-relevant key names stay
        literal, every value is masked, and the leading date/time/LEVEL/
        [thread] prefix is removed entirely rather than left as noise."""
        message = (
            "27/07/2026 00:00:00.024  INFO [TransactionManager] DBAdapter: "
            "Log success request his TransactionInfo{reqID=1384223044, "
            "transID=W12-89082-1785086988853, msisdn=84344872426, "
            "encryptedPass=MlebjTtuWaLRN5ek0Rkwn3MuLNc=, "
            "activeTime=Sat Sep 21 14:27:33 GMT+07:00 2019, "
            "description=Receive incorrect, numHisTrans=0}"
        )
        parsed = self.parser.parse(RawLog(timestamp=1000.0, service="vas", level="INFO", message=message))
        template = parsed.template
        self.assertTrue(template.startswith("DBAdapter:"), template)
        self.assertNotIn("27/07/2026", template)
        self.assertNotIn("00:00:00.024", template)
        self.assertNotIn("[TransactionManager]", template)
        self.assertEqual(
            template,
            "DBAdapter: Log success request his TransactionInfo{reqID <*>, transID <*>, "
            "msisdn <*>, encryptedPass <*>, activeTime <*>, description <*>, numHisTrans <*>}",
        )

    def test_transaction_blob_converges_to_same_template(self):
        """Two transaction logs differing in every variable value (including
        timestamp and thread name) must converge to the same template."""
        def make(req_id, trans_id, enc_pass, ts, thread):
            return (
                f"{ts}  INFO [{thread}] DBAdapter: Log success request his "
                f"TransactionInfo{{reqID={req_id}, transID={trans_id}, "
                f"encryptedPass={enc_pass}, description=Receive incorrect}}"
            )

        msg_a = make("1384223044", "W12-89082-1785086988853", "MlebjTtuWaLRN5ek0Rkwn3MuLNc=", "27/07/2026 00:00:00.024", "TransactionManager")
        msg_b = make("999999999", "W99-11111-9999999999999", "ZZZZ9999AAAA1111bbbb2222=", "30/07/2026 11:22:33.999", "TransactionManagerXYZ")
        p_a = self.parser.parse(RawLog(timestamp=1000.0, service="vas", level="INFO", message=msg_a))
        p_b = self.parser.parse(RawLog(timestamp=1001.0, service="vas", level="INFO", message=msg_b))
        self.assertEqual(p_a.template_id, p_b.template_id)

    def test_logfmt_style_space_separated_converges(self):
        """A completely different, space-separated logfmt-style message (no
        commas/braces, and no [thread] so rule 0 does not fire) still
        converges when only the values differ - proves the assignment-value
        rule generalises beyond the brace-delimited example."""
        msg_a = "level=info ts=2026-09-30T07:00:32Z component=router req_id=abc123"
        msg_b = "level=info ts=2026-10-01T09:11:03Z component=router req_id=xyz999"
        p_a = self.parser.parse(RawLog(timestamp=1000.0, service="svc", level="INFO", message=msg_a))
        p_b = self.parser.parse(RawLog(timestamp=1001.0, service="svc", level="INFO", message=msg_b))
        self.assertEqual(p_a.template_id, p_b.template_id)

    def test_leading_prefix_stripped_for_any_thread_name(self):
        """Rule 0 strips the leading date/time/LEVEL/[thread] prefix
        regardless of what the thread name actually is."""
        msg_a = "30/09/2026 14:00:31 DEBUG [gossip-handlers-321] jgroups:name=Router responded to GET with []"
        msg_b = "01/10/2026 09:12:45 DEBUG [gossip-handlers-999] jgroups:name=Router responded to GET with []"
        p_a = self.parser.parse(RawLog(timestamp=1000.0, service="svc", level="DEBUG", message=msg_a))
        p_b = self.parser.parse(RawLog(timestamp=1001.0, service="svc", level="DEBUG", message=msg_b))
        self.assertEqual(p_a.template_id, p_b.template_id)
        self.assertTrue(p_a.template.startswith("jgroups:"), p_a.template)


if __name__ == "__main__":
    unittest.main()

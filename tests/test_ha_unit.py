"""E10-S6 unit: the app's multi-host database URL, the object store's move to the next MinIO node,
the cluster's command lines, the replication / node collectors, fencing in the bootstrap and the
operator CLI. The real failover drill is ``tests/test_ha_integration.py``; the MinIO node-loss
drill needs the Linux MinIO build (Colab / the pod)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from prometheus_client import CollectorRegistry, generate_latest

from cdi_adapter import metrics, secretstore
from cdi_adapter.config import Settings
from cdi_adapter.db import engine_url
from cdi_adapter.ha import __main__ as cli
from cdi_adapter.ha import minio as mc
from cdi_adapter.ha import pg as ha
from cdi_adapter.storage import ObjectStore

ROOT = Path(__file__).resolve().parents[1]
BOOT = (ROOT / "infra" / "runpod" / "bootstrap_pod.sh").read_text(encoding="utf-8")


# ------------------------------------------------------------------ the app's database URL


def test_one_host_is_the_url_as_it_is():
    s = Settings(database_url="postgresql+psycopg://cdi:pw@127.0.0.1:5432/cdi")
    assert engine_url(s).render_as_string(hide_password=False) == s.database_url


def test_listed_hosts_replace_the_urls_host_and_ask_for_the_writable_one():
    s = Settings(database_url="postgresql+psycopg://cdi:pw@127.0.0.1:5432/cdi",
                 database_hosts=" 127.0.0.1:5432 , 127.0.0.1:5433 ")  # fmt: skip
    u = engine_url(s)
    assert u.host is None and u.port is None
    assert u.query["host"] == ("127.0.0.1:5432", "127.0.0.1:5433")
    assert u.query["target_session_attrs"] == "read-write"
    assert (u.username, u.password, u.database) == ("cdi", "pw", "cdi")
    assert u.query["connect_timeout"] == "3"


# ------------------------------------------------------------------ the object store's failover


class FakeS3:
    def __init__(self, name: str, down: bool = False, status: int | None = None) -> None:
        self.name, self.down, self.status, self.calls = name, down, status, 0

    def _hit(self) -> None:
        self.calls += 1
        if self.down:
            raise EndpointConnectionError(endpoint_url=f"http://{self.name}")
        if self.status:
            raise ClientError({"Error": {"Code": "X"}, "ResponseMetadata":
                               {"HTTPStatusCode": self.status}}, "GetObject")  # fmt: skip

    def get_object(self, **kw):
        self._hit()

        class Body:
            def read(self_inner) -> bytes:
                return self.name.encode()

        return {"Body": Body()}

    def head_object(self, **kw):
        self._hit()
        raise ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                          "HeadObject")  # fmt: skip


def _store(*clients: FakeS3) -> ObjectStore:
    s = ObjectStore(Settings(s3_endpoint_url=",".join(f"http://{c.name}" for c in clients)))
    s._clients = list(clients)
    return s


def test_several_endpoints_mean_one_client_each_with_a_fast_give_up():
    s = ObjectStore(Settings(s3_endpoint_url="http://a:9101, http://b:9102,http://c:9103"))
    assert s.endpoints == ["http://a:9101", "http://b:9102", "http://c:9103"]
    assert len(s._clients) == 3
    cfg = s.client.meta.config
    assert cfg.connect_timeout == 2 and cfg.retries["total_max_attempts"] == 1
    one = ObjectStore(Settings(s3_endpoint_url="http://a:9000"))
    assert len(one._clients) == 1
    assert one.client.meta.config.retries["total_max_attempts"] == 4  # 3 retries, as before


def test_a_dead_node_is_skipped_and_the_next_one_stays_in_use():
    a, b = FakeS3("a", down=True), FakeS3("b")
    s = _store(a, b)
    assert s.get_bytes("k") == b"b"
    assert s.client is b
    assert s.get_bytes("k") == b"b" and a.calls == 1  # not asked again


def test_a_node_without_quorum_is_skipped_too():
    s = _store(FakeS3("a", status=503), FakeS3("b"))
    assert s.get_bytes("k") == b"b"


def test_an_answer_is_not_a_failover():
    a, b = FakeS3("a"), FakeS3("b")
    s = _store(a, b)
    assert s.exists("missing") is False  # a 404 from a is the answer
    assert b.calls == 0


def test_every_node_down_raises_the_last_error():
    s = _store(FakeS3("a", down=True), FakeS3("b", down=True))
    with pytest.raises(EndpointConnectionError):
        s.get_bytes("k")


# ------------------------------------------------------------------ the MinIO cluster


def test_one_host_is_one_loopback_process_over_every_drive():
    c = mc.Cluster.one_host(Path("/ws/tools/minio"), Path("/ws/minio-erasure"))
    assert c.addresses == ("127.0.0.1:9000",) and c.urls() == ["http://127.0.0.1:9000"]
    assert c.drives == tuple(f"/ws/minio-erasure/d{i}" for i in range(1, 5))
    cmd = c.command()
    assert cmd[1:6] == ["server", "--address", "127.0.0.1:9000", "--console-address",
                        "127.0.0.1:10000"]  # fmt: skip
    assert cmd[-4:] == list(c.drives)
    assert [d.as_posix() for d in c.local_drives()] == list(c.drives)


def test_several_hosts_each_run_the_full_drive_list_on_their_own_address():
    addrs = ("10.0.0.1:9000", "10.0.0.2:9000", "10.0.0.3:9000", "10.0.0.4:9000")
    drives = tuple(f"http://{a}/data" for a in addrs)
    c = mc.Cluster(Path("minio"), addrs, drives, Path("run"))
    for i in c.nodes():
        cmd = c.command(i)
        assert cmd[3] == addrs[i - 1] and cmd[-4:] == list(drives)
    assert c.local_drives() == []


def test_node_credentials_come_in_the_environment_never_the_command_line():
    env = mc.node_env("cdi-abc", "s3cr3t-value")
    assert env["MINIO_ROOT_USER"] == "cdi-abc" and env["MINIO_ROOT_PASSWORD"] == "s3cr3t-value"
    assert env["MINIO_PROMETHEUS_AUTH_TYPE"] == "public"  # loopback only; the collector reads it
    assert "MINIO_ROOTDISK_THRESHOLD_SIZE" not in env
    assert mc.node_env("a", "b", root_disk_ok=True)["MINIO_ROOTDISK_THRESHOLD_SIZE"] == "1KiB"
    c = mc.Cluster.one_host(Path("minio"), Path("base"))
    assert "s3cr3t-value" not in " ".join(c.command())


def test_the_cli_refuses_a_cluster_of_fewer_than_four_hosts(monkeypatch):
    monkeypatch.setenv("CDI_MINIO_MODE", "cluster")
    monkeypatch.setenv("CDI_MINIO_ADDRESSES", "10.0.0.1:9000,10.0.0.2:9000")
    with pytest.raises(ha.HaError, match="at least 4"):
        cli.cluster()
    monkeypatch.setenv("CDI_MINIO_MODE", "erasure")
    monkeypatch.setenv("CDI_MINIO_ADDRESSES", "127.0.0.1:9000")
    assert cli.cluster().addresses == ("127.0.0.1:9000",) and len(cli.cluster().drives) == 4


# ------------------------------------------------------------------ collectors


def _scrape(collector) -> dict[str, float]:
    reg = CollectorRegistry()
    reg.register(collector)
    from prometheus_client.parser import text_string_to_metric_families

    text = generate_latest(reg).decode()
    return {s.name: s.value for f in text_string_to_metric_families(text) for s in f.samples}


def test_replication_is_reported_by_the_primary_only():
    got = _scrape(metrics.PgReplicationCollector(lambda: (False, 1, 0.4, 0)))
    assert got == {"cdi_pg_standbys_streaming": 1, "cdi_pg_replication_lag_seconds": 0.4,
                   "cdi_pg_replication_slots_inactive": 0}  # fmt: skip
    assert _scrape(metrics.PgReplicationCollector(lambda: (True, 0, 0.0, 0))) == {}

    def down():
        raise OSError("db down")

    assert _scrape(metrics.PgReplicationCollector(down)) == {}


def test_minio_drives_come_from_minios_own_gauges():
    text = "\n".join([
        "# HELP x",
        'minio_cluster_drive_total{server="127.0.0.1:9000"} 4',
        "minio_cluster_drive_offline_total 1",
        "minio_cluster_drive_online_total 3",
    ])  # fmt: skip
    got = _scrape(metrics.MinioDrivesCollector(lambda: text))
    assert got == {"cdi_minio_drives": 4, "cdi_minio_drives_offline": 1}
    assert _scrape(metrics.MinioDrivesCollector(lambda: "nothing here")) == {}

    def down():
        raise OSError("minio down")

    assert _scrape(metrics.MinioDrivesCollector(down)) == {}


def test_minio_nodes_are_counted_only_for_a_cluster():
    got = _scrape(metrics.MinioNodesCollector(["http://a", "http://b", "http://c"], lambda u: 2))
    assert got == {"cdi_minio_nodes": 3, "cdi_minio_nodes_online": 2}
    assert _scrape(metrics.MinioNodesCollector(["http://a"], lambda u: 1)) == {}


# ------------------------------------------------------------------ Postgres tooling


def test_as_root_the_tools_run_as_postgres():
    pg = ha.Pg("/usr/lib/postgresql/16/bin", os_user="postgres")
    cmd = pg.tool("pg_ctl", "-D", "/d", "start")
    assert cmd[:4] == ["runuser", "-u", "postgres", "--"] and cmd[-3:] == ["-D", "/d", "start"]
    assert ha.Pg("bin").tool("psql")[0].endswith("psql.exe" if sys.platform == "win32" else "psql")


def test_the_replication_password_goes_to_a_private_password_file(tmp_path):
    pg = ha.Pg("bin")
    f = ha.write_pgpass(tmp_path / "pgpass", [("127.0.0.1", 5433, "cdi_repl", "a:b\\c")], pg)
    assert f.read_text() == "127.0.0.1:5433:*:cdi_repl:a\\:b\\\\c\n"
    if os.name == "posix":
        assert (f.stat().st_mode & 0o777) == 0o600


def test_the_replication_password_is_a_store_secret(tmp_path):
    assert "cdi_replication_password" in secretstore.NAMES
    assert len(secretstore.generate("cdi_replication_password")) >= 32
    assert "cdi_replication_password" in secretstore.init(tmp_path)


def test_a_fenced_data_folder_is_never_started_as_a_primary():
    fence_at = BOOT.index('if [ -f "$PGDATA/cdi.fenced" ]')
    start_at = BOOT.index("pg_ctl -D $PGDATA -l")
    assert fence_at < start_at and "exit 1" in BOOT[fence_at:start_at]
    assert ha.FENCE == "cdi.fenced"


def test_standby_and_cluster_are_opt_in_in_the_bootstrap():
    assert 'if [ -n "${CDI_PG_STANDBY_PORT:-}" ]' in BOOT
    assert "cdi_adapter.ha pg setup-standby" in BOOT
    assert '"${CDI_MINIO_MODE:-single}" != "single"' in BOOT
    assert "cdi_adapter.ha minio start" in BOOT


def test_every_command_parses():
    p = cli.build_parser()
    for argv in (["pg", "setup-standby"], ["pg", "status"], ["pg", "failover", "--to", "standby"],
                 ["pg", "rejoin", "--old", "primary"], ["pg", "drill", "--seconds", "3"],
                 ["minio", "start"], ["minio", "status"], ["minio", "stop-node", "2"],
                 ["minio", "start-node", "2"], ["minio", "migrate", "--from", "http://x:9000"],
                 ["minio", "drill"]):  # fmt: skip
        a = p.parse_args(argv)
        assert (a.area, a.cmd) == tuple(argv[:2])

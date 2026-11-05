"""agmen record y agmen info (RFC 0010): grabar topics en MCAP sin frenar a nadie.

Corre con los dos núcleos (AGMEN_BACKEND=native|pure): lo grabado son los
mismos bytes que se publicaron (lo de memoria compartida, con su payload de
verdad), las pérdidas se cuentan y quedan escritas en el archivo, y una
grabación cortada se repara.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

import agmen
from agmen import _mcap, _shm, pubsub, record, wire
from agmen import hub as hubmod

ROOT = Path(__file__).resolve().parents[1]
BIG = bytes(range(256)) * 4000       # 1 MB: va por memoria compartida


@dataclass
class Img(wire.Message):
    SCHEMA = "Img{u32 w;u32 h;bytes data}"
    w: int
    h: int
    data: bytes


def imu(i: int) -> wire.ImuSample:
    return wire.ImuSample(accel=(float(i), 0.0, 9.8), gyro=(0.0, 0.0, 0.0))


class HubCase(unittest.TestCase):
    def setUp(self):
        self.hub = hubmod.Hub("127.0.0.1:0", log=lambda t: None)
        self.old = os.environ.get("AGMEN_HUB")
        os.environ["AGMEN_HUB"] = f"127.0.0.1:{self.hub.sock.getsockname()[1]}"
        self.stop_hub = threading.Event()
        threading.Thread(target=self.hub.serve, args=(self.stop_hub,), daemon=True).start()
        self.dir = tempfile.mkdtemp()
        self.closers = []

    def tearDown(self):
        for c in reversed(self.closers):
            c()
        self.stop_hub.set()
        if self.old is None:
            os.environ.pop("AGMEN_HUB", None)
        else:
            os.environ["AGMEN_HUB"] = self.old

    def pub(self, cls, topic, pure=False):
        p = pubsub._PurePublisher(cls, topic) if pure else agmen.Publisher(cls, topic)
        self.closers.append(p.close)
        return p

    def wait_subscribers(self, pub, n=1, timeout=3.0):
        end = time.monotonic() + timeout
        while pub.subscribers < n and time.monotonic() < end:
            time.sleep(0.02)
            if hasattr(pub, "_drain_locked"):
                pub._drain_locked()
        self.assertGreaterEqual(pub.subscribers, n)

    def read(self, path):
        out = []
        with _mcap.Reader(path) as r:
            for m in r.messages():
                env, payload = wire.parse(bytes(m.data))
                out.append((m.channel.topic, env, bytes(payload), m))
            return out, dict(r.metadata), r


class TestGrabar(HubCase):
    def test_los_mismos_bytes_tambien_por_memoria_compartida(self):
        path = os.path.join(self.dir, "a.mcap")
        rec = record.Recorder(path, ["*"], quiet=True).start()
        a = self.pub(wire.ImuSample, "/imu")
        cam = self.pub(Img, "/camara")
        self.wait_subscribers(a)
        self.wait_subscribers(cam)
        for i in range(50):
            a.publish(imu(i))
            if i % 10 == 0:
                cam.publish(Img(i, 7, BIG))
            time.sleep(0.005)
        time.sleep(0.4)
        shm = cam.shm_subscribers
        s = rec.stop()
        self.assertEqual((s.messages, s.lost), (55, 0))
        got, meta, _ = self.read(path)
        imus = [wire.ImuSample.decode_payload(p).accel[0] for t, _, p, _ in got if t == "/imu"]
        imgs = [Img.decode_payload(p) for t, _, p, _ in got if t == "/camara"]
        self.assertEqual(imus, [float(i) for i in range(50)])
        self.assertEqual([i.w for i in imgs], [0, 10, 20, 30, 40])
        self.assertTrue(all(bytes(i.data) == BIG for i in imgs))
        for _, env, _, m in got:
            self.assertFalse(env.flags & wire.FLAG_SHM, "se guarda el payload, no el descriptor")
            self.assertEqual(m.publish_time, env.t_pub)
            self.assertGreaterEqual(m.log_time, env.t_pub)
        if _shm.enabled():
            self.assertEqual(shm, 1, "el grabador recibe por memoria compartida")
        self.assertEqual(meta["agmen.perdidos"], {"total": "0"})
        self.assertEqual(meta["agmen.grabacion"]["version"], agmen.__version__)

    def test_solo_los_topics_pedidos(self):
        path = os.path.join(self.dir, "b.mcap")
        rec = record.Recorder(path, ["/imu"], quiet=True).start()
        a = self.pub(wire.ImuSample, "/imu")
        b = self.pub(wire.ImuSample, "/otro")
        self.wait_subscribers(a)
        for i in range(10):
            a.publish(imu(i))
            b.publish(imu(i))
        time.sleep(0.3)
        self.assertEqual(b.subscribers, 0, "nadie le pidió /otro")
        rec.stop()
        got, _, _ = self.read(path)
        self.assertEqual({t for t, *_ in got}, {"/imu"})
        self.assertEqual(len(got), 10)

    def test_disco_lento_nunca_frena_y_las_perdidas_quedan_escritas(self):
        path = os.path.join(self.dir, "c.mcap")
        err = io.StringIO()
        rec = record.Recorder(path, ["*"], quiet=True, queue_bytes=3 << 20, out=err)
        real_write = rec._write

        def slow(item):                       # un disco que escribe ~10 imágenes por segundo
            time.sleep(0.1)
            real_write(item)

        rec._write = slow
        rec.start()
        cam = self.pub(Img, "/camara")
        self.wait_subscribers(cam)
        t0 = time.perf_counter()
        for i in range(60):
            cam.publish(Img(i, 0, BIG))
            time.sleep(0.005)
        took = time.perf_counter() - t0
        time.sleep(0.5)
        s = rec.stop()
        st = s.topics["/camara"]
        self.assertLess(took, 2.0, "publicar no espera al grabador")
        self.assertGreater(st.lost_disk + st.lost_before, 0, "el disco no alcanzó: hubo pérdidas")
        self.assertEqual(st.messages + st.lost_disk + st.lost_before, 60, "cada mensaje se grabó o se contó")
        self.assertIn("el disco no alcanza", err.getvalue())
        _, meta, _ = self.read(path)
        lost = meta["agmen.perdidos"]
        self.assertEqual(int(lost["total"]), st.lost_disk + st.lost_before)

    def test_huecos_de_seq(self):
        path = os.path.join(self.dir, "d.mcap")
        rec = record.Recorder(path, ["/imu"], quiet=True).start()
        a = self.pub(wire.ImuSample, "/imu", pure=True)
        self.wait_subscribers(a)
        for i in range(10):
            if i == 5:
                a.seq += 3                    # como si la red se hubiera comido 3
            a.publish(imu(i))
        time.sleep(0.3)
        s = rec.stop()
        self.assertEqual(s.topics["/imu"].lost_before, 3)
        _, meta, _ = self.read(path)
        self.assertEqual(meta["agmen.perdidos"]["/imu antes de llegar"], "3")

    def test_dividir_por_tamano(self):
        base = os.path.join(self.dir, "e.mcap")
        rec = record.Recorder(base, ["*"], quiet=True, split="2MiB", chunk_size=256 << 10).start()
        cam = self.pub(Img, "/camara")
        self.wait_subscribers(cam)
        for i in range(8):
            cam.publish(Img(i, 0, BIG))
            time.sleep(0.05)
        time.sleep(0.5)
        s = rec.stop()
        self.assertGreater(len(s.files), 1)
        self.assertTrue(all(os.path.basename(f).startswith("e_0") for f in s.files))
        seen = []
        for f in s.files:
            got, meta, r = self.read(f)
            self.assertTrue(r.complete)
            self.assertIn("agmen.perdidos", meta)
            seen += [Img.decode_payload(p).w for _, _, p, _ in got]
        self.assertEqual(seen, list(range(8)), "nada se repite ni se pierde entre archivos")


class TestInfo(HubCase):
    def test_info_y_reparar(self):
        path = os.path.join(self.dir, "f.mcap")
        rec = record.Recorder(path, ["*"], quiet=True, chunk_size=20_000).start()
        a = self.pub(wire.ImuSample, "/imu")
        self.wait_subscribers(a)
        for i in range(300):                  # una IMU a 1 kHz
            a.publish(imu(i))
            time.sleep(0.001)
        time.sleep(0.3)
        rec.stop()
        d = record.describe(path)
        self.assertTrue(d["complete"])
        self.assertEqual([(t["topic"], t["type"], t["messages"]) for t in d["topics"]], [("/imu", "ImuSample", 300)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(record.cmd_info(type("A", (), {"archivo": path, "reparar": False})()), 0)
        self.assertIn("ImuSample", out.getvalue())
        self.assertIn("sin pérdidas", out.getvalue())
        # Cortado (el grabador se cayó): se lee igual, y --reparar le devuelve el índice.
        with open(path, "rb") as f:
            full = f.read()
        with open(path, "wb") as f:
            f.write(full[: len(full) // 2])
        self.assertFalse(record.describe(path)["complete"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            record.cmd_info(type("A", (), {"archivo": path, "reparar": False})())
        self.assertIn("--reparar", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(record.cmd_info(type("A", (), {"archivo": path, "reparar": True})()), 0)
        d = record.describe(path)
        self.assertTrue(d["complete"])
        self.assertGreater(d["topics"][0]["messages"], 50)


class TestComando(HubCase):
    def test_agmen_record_y_agmen_info(self):
        path = os.path.join(self.dir, "g.mcap")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8"}
        p = subprocess.Popen([sys.executable, "-m", "agmen", "record", "-o", path, "--duration", "2s", "/imu"],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        a = self.pub(wire.ImuSample, "/imu")
        self.wait_subscribers(a, timeout=5)
        for i in range(20):
            a.publish(imu(i))
            time.sleep(0.01)
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("⏺ grabando /imu", err)
        self.assertIn("20 mensajes", err)
        r = subprocess.run([sys.executable, "-m", "agmen", "info", path], env=env, capture_output=True,
                           text=True, encoding="utf-8", timeout=30, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("/imu", r.stdout)
        self.assertIn("20", r.stdout)

    def test_sin_hub_lo_dice(self):
        os.environ["AGMEN_HUB"] = "127.0.0.1:9"        # nadie escucha ahí
        env = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8"}
        r = subprocess.run([sys.executable, "-m", "agmen", "record", "-o", os.path.join(self.dir, "h.mcap"),
                            "--duration", "1s"], env=env, capture_output=True, text=True, encoding="utf-8",
                           timeout=30, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("agmen run", r.stderr)


class TestTamanos(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(record.parse_size("1GiB"), 1 << 30)
        self.assertEqual(record.parse_size("500MB"), 500 * 1024 ** 2)
        self.assertEqual(record.parse_split("10min"), (0, 600.0))
        self.assertEqual(record.parse_split("2MiB"), (2 << 20, 0.0))
        with self.assertRaises(ValueError):
            record.parse_size("mucho")


if __name__ == "__main__":
    unittest.main()

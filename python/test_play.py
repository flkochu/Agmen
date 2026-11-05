"""agmen play (RFC 0010 D6): reproducir una grabación con su tiempo original.

Corre con los dos núcleos. Se arma una grabación con tiempos conocidos y se
revisa lo que reciben los suscriptores: los mismos bytes salvo `clock` =
replay (también una imagen que va por memoria compartida), a su tiempo, con
`/clock` en la hora de la grabación, y en bucle sin que el tiempo regrese.
"""

from __future__ import annotations

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
from agmen import _mcap, clock, play, wire
from agmen import hub as hubmod

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_700_000_000_000_000_000          # la grabación es "de 2023"
STEP = 50_000_000                        # un mensaje cada 50 ms
BIG = bytes(range(256)) * 4000           # 1 MB: va por memoria compartida
SRC_IMU, SRC_CAM = 0x1111, 0x2222


@dataclass
class Img(wire.Message):
    SCHEMA = "Img{u32 w;u32 h;bytes data}"
    w: int
    h: int
    data: bytes


def imu(i: int) -> wire.ImuSample:
    return wire.ImuSample(accel=(float(i), 0.0, 9.8), gyro=(0.0, 0.0, 0.0))


def make_recording(path: str, n: int = 20) -> list[bytes]:
    """Una grabación como la que hace agmen record: /imu cada 50 ms, /camara cada 200 ms."""
    w = _mcap.Writer(path, profile="agmen", chunk_size=200_000)
    s_imu = w.add_schema("ImuSample", "agmen", wire.ImuSample.SCHEMA_TEXT.encode())
    s_img = w.add_schema("Img", "agmen", Img.SCHEMA_TEXT.encode())
    c_imu = w.add_channel("/imu", "agmen", s_imu)
    c_img = w.add_channel("/camara", "agmen", s_img)
    c_clk = w.add_channel("/clock", "agmen", w.add_schema("Clock", "agmen", clock.Clock.SCHEMA_TEXT.encode()))
    packets = []
    for i in range(n):
        t = T0 + i * STEP
        env = wire.Envelope(wire.ENC_COMPACT, 0, wire.ImuSample.SCHEMA_ID, wire.topic_id("/imu"), SRC_IMU, i,
                            t - 2_000_000, t - 1_000_000, 24, wire.CLOCK_REAL)
        pkt = env.pack() + imu(i).encode_payload()
        w.add_message(c_imu, t, t - 1_000_000, i, pkt)
        packets.append(pkt)
        if i % 4 == 0:
            body = Img(i, 1, BIG).encode_payload()
            n_body = body.nbytes if isinstance(body, memoryview) else len(body)
            env = wire.Envelope(wire.ENC_ARROW, 0, Img.SCHEMA_ID, wire.topic_id("/camara"), SRC_CAM, i // 4,
                                t - 3_000_000, t - 1_000_000, n_body, wire.CLOCK_REAL)
            w.add_message(c_img, t + 1, t - 1_000_000, i // 4, env.pack(), body)
        if i % 5 == 0:   # un /clock grabado: no se reproduce
            w.add_message(c_clk, t, t, i, wire.encode(clock.Clock(t=0), i, t, topic="/clock"))
    w.close()
    return packets


class PlayCase(unittest.TestCase):
    def setUp(self):
        self.hub = hubmod.Hub("127.0.0.1:0", log=lambda t: None)
        self.old = os.environ.get("AGMEN_HUB")
        os.environ["AGMEN_HUB"] = f"127.0.0.1:{self.hub.sock.getsockname()[1]}"
        self.stop_hub = threading.Event()
        threading.Thread(target=self.hub.serve, args=(self.stop_hub,), daemon=True).start()
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "vuelta.mcap")
        self.packets = make_recording(self.path)
        self.closers = []

    def tearDown(self):
        for c in reversed(self.closers):
            c()
        self.stop_hub.set()
        clock.configure(clock.REAL)
        if self.old is None:
            os.environ.pop("AGMEN_HUB", None)
        else:
            os.environ["AGMEN_HUB"] = self.old

    def sub(self, cls, topic):
        s = agmen.Subscriber(cls, topic)
        self.closers.append(s.close)
        return s

    def collect(self, sub, until: float, out: list) -> None:
        while time.monotonic() < until:
            s = sub.receive(0.05)
            if s is not None:
                out.append((time.monotonic(), s))


class TestReproducir(PlayCase):
    def test_los_mismos_bytes_a_su_tiempo_con_reloj_replay(self):
        imu_sub = self.sub(wire.ImuSample, "/imu")
        cam_sub = self.sub(Img, "/camara")
        clk_sub = self.sub(clock.Clock, "/clock")
        p = play.Player(self.path, quiet=True)
        self.assertEqual(p.topics, ["/camara", "/imu"], "el /clock grabado no se reproduce")
        imus, cams, clks = [], [], []
        until = time.monotonic() + 2.5
        threads = [threading.Thread(target=self.collect, args=(s, until, out))
                   for s, out in ((imu_sub, imus), (cam_sub, cams), (clk_sub, clks))]
        for t in threads:
            t.start()
        time.sleep(0.2)
        p.start()
        self.assertTrue(p.wait(5))
        for t in threads:
            t.join()
        stats = p.stop()
        self.assertEqual(stats.messages, 25)
        self.assertEqual([s.message.accel[0] for _, s in imus], [float(i) for i in range(20)])
        for i, (_, s) in enumerate(imus):
            e = s.envelope
            self.assertEqual(e.clock, wire.CLOCK_REPLAY)
            self.assertEqual((e.source_id, e.seq, e.t_data, e.t_pub),
                             (SRC_IMU, i, T0 + i * STEP - 2_000_000, T0 + i * STEP - 1_000_000))
        # A su tiempo: 19 intervalos de 50 ms ≈ 0.95 s (con margen para el CI).
        span = imus[-1][0] - imus[0][0]
        self.assertGreater(span, 0.8)
        self.assertLess(span, 1.6)
        self.assertEqual([s.message.w for _, s in cams], [0, 4, 8, 12, 16])
        self.assertTrue(all(bytes(s.message.data) == BIG for _, s in cams))
        self.assertEqual({s.envelope.source_id for _, s in cams}, {SRC_CAM})
        # /clock: la hora de la grabación, y siempre hacia adelante.
        ts = [s.message.t for _, s in clks]
        self.assertTrue(ts)
        self.assertGreaterEqual(min(ts), T0)
        self.assertLess(max(ts), T0 + 25 * STEP)
        self.assertEqual(ts, sorted(ts))

    def test_al_doble_y_en_bucle(self):
        imu_sub = self.sub(wire.ImuSample, "/imu")
        p = play.Player(self.path, ["/imu"], rate=4.0, loop=True, quiet=True)
        got = []
        until = time.monotonic() + 1.6
        th = threading.Thread(target=self.collect, args=(imu_sub, until, got))
        th.start()
        time.sleep(0.1)
        p.start()
        th.join()
        p.stop()
        self.assertGreaterEqual(p.stats.passes, 2, "a ×4 una vuelta dura ~0.25 s")
        t_data = [s.envelope.t_data for _, s in got]
        self.assertEqual(t_data, sorted(t_data), "el tiempo nunca regresa entre vueltas")
        self.assertGreater(len(got), 20)
        self.assertEqual(got[20][1].message.accel[0], 0.0, "la segunda vuelta empieza otra vez")

    def test_un_nodo_en_reloj_real_lo_avisa(self):
        # Un nodo aparte (otro proceso), con reloj real: el de este proceso ya es el de la grabación.
        code = ("import agmen\nfrom agmen import wire\nsub = agmen.Subscriber(wire.ImuSample, '/imu')\n"
                "n = 0\nwhile n < 5:\n    n += sub.receive(10.0) is not None\nprint('recibidos', n)\n")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8"}
        env.pop("AGMEN_CLOCK", None)
        node = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8")
        end = time.monotonic() + 10
        while time.monotonic() < end and not any(op == "topic" and f.get("name") == "/imu" and int(f.get("subs", 0))
                                                 for op, f in hubmod.query(os.environ["AGMEN_HUB"], timeout=0.5)):
            time.sleep(0.05)
        p = play.Player(self.path, ["/imu"], rate=4.0, loop=True, quiet=True)
        p.start()
        try:
            out, err = node.communicate(timeout=20)
        finally:
            p.stop()
        self.assertIn("recibidos 5", out, "el dato llega igual")
        self.assertEqual(err.count("'/imu' llega con reloj reproducción"), 1, err)
        self.assertIn("AGMEN_CLOCK=replay", err)

    def test_errores_claros(self):
        with self.assertRaisesRegex(ValueError, "no tiene el topic /nada"):
            play.Player(self.path, ["/nada"])
        other = os.path.join(self.dir, "otro.mcap")
        w = _mcap.Writer(other, profile="x")
        cid = w.add_channel("/a", "json", w.add_schema("S", "jsonschema", b"{}"))
        w.add_message(cid, 1, 1, 0, b"{}")
        w.close()
        with self.assertRaisesRegex(ValueError, "no es una grabación de Agmen"):
            play.Player(other)


class TestComando(PlayCase):
    def env(self):
        return {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8"}

    def agmen(self, *args):
        return subprocess.run([sys.executable, "-m", "agmen", *args], env=self.env(), capture_output=True,
                              text=True, encoding="utf-8", timeout=60, check=False)

    def test_agmen_play(self):
        imu_sub = self.sub(wire.ImuSample, "/imu")
        p = subprocess.Popen([sys.executable, "-m", "agmen", "play", self.path, "/imu", "--rate", "2"],
                             env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding="utf-8")
        got = []
        until = time.monotonic() + 20
        while time.monotonic() < until and p.poll() is None:
            s = imu_sub.receive(0.05)
            if s is not None:
                got.append(s)
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("▶ reproduciendo", err)
        self.assertIn("AGMEN_CLOCK=replay", err, "le dice a la gente cómo correr sus nodos")
        self.assertIn("⏹ 20 mensajes", err)
        self.assertGreater(len(got), 10)
        self.assertTrue(all(s.envelope.clock == wire.CLOCK_REPLAY for s in got))

    def test_errores_del_comando(self):
        r = self.agmen("play", os.path.join(self.dir, "no-existe.mcap"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("no existe", r.stderr)
        r = self.agmen("play", self.path, "/nada")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no tiene el topic /nada (tiene: /camara, /imu)", r.stderr)
        os.environ["AGMEN_HUB"] = "127.0.0.1:9"        # nadie escucha ahí
        r = self.agmen("play", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("agmen run", r.stderr)

    def test_agmen_run_revisa_play_y_skip(self):
        toml = os.path.join(self.dir, "robot.toml")
        with open(toml, "w", encoding="utf-8") as f:
            f.write('[robot]\nname = "r"\n\n[[node]]\nname = "imu"\ncmd = "python -c pass"\n\n[hub]\nenabled = false\n'
                    '\n[web]\nenabled = false\n')
        r = self.agmen("run", toml, "--skip", "camara")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--skip camara: no hay un nodo con ese nombre (hay: imu)", r.stderr)
        r = self.agmen("run", toml, "--play", os.path.join(self.dir, "no-existe.mcap"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("error: --play: no existe", r.stderr)


class TestRelojReplay(unittest.TestCase):
    def tearDown(self):
        clock.configure(clock.REAL)

    def test_modo_replay(self):
        clock.configure(clock.REPLAY, stepped=False, listen=False)
        self.assertEqual(clock.mode(), "replay")
        self.assertEqual(clock.domain(), wire.CLOCK_REPLAY)
        self.assertEqual(clock.mode_name(), "reproducción")
        clock.update(clock.Clock(t=T0, speed=2.0))
        time.sleep(0.05)
        t = clock.now_ns()
        self.assertGreater(t, T0 + 60_000_000)
        self.assertLess(t, T0 + 1_000_000_000)
        clock.update(clock.Clock(t=T0, speed=1.0, paused=True))
        self.assertGreaterEqual(clock.now_ns(), t, "nunca hacia atrás")

    def test_el_servidor_de_reloj_solo_salta_hacia_adelante(self):
        srv = clock.ClockServer(mode=clock.REPLAY, t=T0, publish=False)
        try:
            srv.seek(T0 + 5_000_000_000)
            self.assertGreaterEqual(srv.now(), T0 + 5_000_000_000)
            srv.seek(T0)
            self.assertGreaterEqual(srv.now(), T0 + 5_000_000_000)
            self.assertEqual(srv.info()["mode"], "replay")
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()

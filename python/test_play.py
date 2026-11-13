"""agmen play (RFC 0010 D6): reproducir una grabación con su tiempo original.

Corre con los dos núcleos. Se arma una grabación con tiempos conocidos y se
revisa lo que reciben los suscriptores: los mismos bytes salvo `clock` =
replay (también una imagen que va por memoria compartida), a su tiempo, con
`/clock` en la hora de la grabación, y en bucle sin que el tiempo regrese.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

import agmen
from agmen import _mcap, _shm, _tcp, clock, play, wire
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
        cam_pub = p._pubs[("/camara", SRC_CAM)]
        if _shm.enabled():   # RFC 0009: los bloques son del source_id grabado, y el suscriptor nunca cae a TCP
            self.assertEqual(cam_pub._tcp.pool.owner, SRC_CAM)
            self.assertEqual(cam_pub.shm_subscribers, 1)
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

    def test_el_source_id_grabado_desde_el_principio(self):
        # Si se cambiara después de crear el publicador, un suscriptor que se conecte en
        # ese momento recibiría bloques de memoria compartida de otro dueño (pasó en Windows).
        seen = []
        orig = _tcp.Fanout.__init__

        def spy(fan, topic="", source_id=0, topic_id=0):
            seen.append(source_id)
            orig(fan, topic, source_id, topic_id)

        _tcp.Fanout.__init__ = spy
        try:
            pub = play._ReplayPublisher(play._RecordedType(Img.SCHEMA_TEXT), "/camara", SRC_CAM)
        finally:
            _tcp.Fanout.__init__ = orig
        self.closers.append(pub.close)
        self.assertEqual(seen, [SRC_CAM])
        self.assertEqual((pub.source_id, pub._tcp.source_id), (SRC_CAM, SRC_CAM))

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


NODO = """\
import os, sys, time
print(f"ARRANCO {sys.argv[1]} reloj={os.environ.get('AGMEN_CLOCK', '-')}", flush=True)
time.sleep(120)
"""


class TestPerfilesEnLaLineaDeComandos(PlayCase):
    """agmen run --profile / --sim / --play con perfiles (RFC 0011 §3.2): códigos de salida,
    avisos y qué nodos corren de verdad (subprocesos, sin hub ni panel)."""

    def setUp(self):
        super().setUp()
        self.toml = os.path.join(self.dir, "robot.toml")
        with open(os.path.join(self.dir, "nodo.py"), "w", encoding="utf-8") as f:
            f.write(NODO)
        self.write(self.robot())

    def robot(self, extra="", hub=False):
        nodo = os.path.join(self.dir, "nodo.py")

        def node(name, profile=None):
            prof = f"profile = \"{profile}\"\n" if profile else ""
            return f"[[node]]\nname = \"{name}\"\n{prof}cmd = ['python', '{nodo}', '{name}']\n\n"

        return ('[robot]\nname = "r"\n\n' + node("imu", "real") + node("motor", "real") + node("mundo", "sim")
                + node("cerebro") + f"[hub]\nenabled = {str(hub).lower()}\n\n[web]\nenabled = false\n" + extra)

    def write(self, text):
        with open(self.toml, "w", encoding="utf-8") as f:
            f.write(text)

    def env(self):
        return {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}

    def agmen(self, *args):
        return subprocess.run([sys.executable, "-m", "agmen", *args], env=self.env(), capture_output=True,
                              text=True, encoding="utf-8", timeout=60, check=False)

    def corre(self, *args, esperar=("ARRANCO cerebro",), seg=30.0):
        """agmen run en vivo hasta que aparezcan los textos de `esperar`; Ctrl+C. Devuelve (código, salida)."""
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        p = subprocess.Popen([sys.executable, "-m", "agmen", "run", self.toml, "--no-browser", *args],
                             env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", **kw)
        lines = []
        reader = threading.Thread(target=lambda: lines.extend(iter(p.stdout.readline, "")), daemon=True)
        reader.start()
        end = time.monotonic() + seg
        while time.monotonic() < end and p.poll() is None:
            text = "".join(lines)
            if all(w in text for w in esperar) and "todo arrancado" in text:
                break
            time.sleep(0.1)
        time.sleep(0.7)   # que se vea si arranca algo que no debía
        if p.poll() is None:
            p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        reader.join(5)
        p.stdout.close()
        return p.returncode, "".join(lines)

    # --- errores: código 2 y qué hacer
    def test_sim_con_otro_profile_es_error(self):
        r = self.agmen("run", self.toml, "--sim", "--profile", "real")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("--sim", r.stderr)
        self.assertIn("--profile real", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_sim_con_profile_sim_es_lo_mismo_y_no_es_error(self):
        code, out = self.corre("--sim", "--profile", "sim")
        self.assertEqual(code, 0, out)
        self.assertIn("ARRANCO mundo", out)

    def test_play_con_sim_es_error(self):
        for flags in (["--sim"], ["--profile", "sim"]):
            r = self.agmen("run", self.toml, "--play", self.path, *flags)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("los dos quieren mandar el reloj", r.stderr)
            self.assertIn("Elige uno", r.stderr)

    def test_perfil_replay_sin_play_es_error(self):
        r = self.agmen("run", self.toml, "--profile", "replay")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("--play", r.stderr)
        self.assertNotIn("ARRANCO", r.stdout)
        # y también si lo dice el robot.toml
        self.write(self.robot().replace('name = "r"\n', 'name = "r"\nprofile = "replay"\n', 1))
        r = self.agmen("run", self.toml)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("--play", r.stderr)

    def test_perfil_desconocido_sugiere(self):
        r = self.agmen("run", self.toml, "--profile", "siim")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("error en", r.stderr)
        self.assertIn("el perfil 'siim' no existe", r.stderr)
        self.assertIn("¿Quisiste decir 'sim'?", r.stderr)
        r = self.agmen("run", self.toml, "--profile", "")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_perfil_desconocido_en_el_robot_toml(self):
        self.write(self.robot().replace('name = "r"\n', 'name = "r"\nprofile = "siim"\n', 1))
        r = self.agmen("run", self.toml)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("[robot] profile = 'siim'", r.stderr)
        self.assertIn("¿Quisiste decir 'sim'?", r.stderr)

    def test_skip_de_un_nodo_que_no_existe_sigue_siendo_error(self):
        r = self.agmen("run", self.toml, "--sim", "--skip", "camara")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("--skip camara: no hay un nodo con ese nombre", r.stderr)

    def test_nodo_hub_en_otro_perfil_es_error(self):
        nodo = os.path.join(self.dir, "nodo.py")
        self.write(self.robot(hub=True) + f"\n[[node]]\nname = \"hub\"\nprofile = \"real\"\n"
                   f"cmd = ['python', '{nodo}', 'hub']\n")
        for flags in (("--sim",), ("--play", self.path)):   # el nodo 'hub' es del perfil real
            r = self.agmen("run", self.toml, *flags)
            self.assertEqual(r.returncode, 2, f"{flags}: {r.stdout}{r.stderr}")
            self.assertIn("'hub'", r.stderr)

    # --- avisos y qué corre
    def test_sin_banderas_corre_real_y_no_dice_nada_del_perfil_si_no_falta_nadie(self):
        code, out = self.corre(esperar=("ARRANCO imu", "ARRANCO motor", "ARRANCO cerebro"))
        self.assertEqual(code, 0, out)
        self.assertNotIn("ARRANCO mundo", out)
        self.assertIn("perfil real", out, "hay un nodo de otro perfil: sí se dice")
        self.assertIn("no corren: mundo (perfil sim)", out)
        self.assertIn("reloj real", out)

    def test_sin_perfiles_la_salida_es_la_de_siempre(self):
        nodo = os.path.join(self.dir, "nodo.py")
        self.write(f'[robot]\nname = "r"\n\n[[node]]\nname = "cerebro"\ncmd = [\'python\', \'{nodo}\', \'cerebro\']\n\n'
                   f'[hub]\nenabled = false\n\n[web]\nenabled = false\n')
        code, out = self.corre()
        self.assertEqual(code, 0, out)
        self.assertNotIn("perfil", out, "sin perfiles y sin banderas no sale la línea del perfil")
        self.assertIn("ARRANCO cerebro reloj=real", out)

    def test_sim_corre_mundo_y_nunca_el_hardware(self):
        code, out = self.corre("--sim", esperar=("ARRANCO mundo", "ARRANCO cerebro"))
        self.assertEqual(code, 0, out)
        self.assertNotIn("ARRANCO imu", out)
        self.assertNotIn("ARRANCO motor", out)
        self.assertRegex(out, r"agmen\s+│ perfil sim · reloj de simulación \(×1\) · no corren: imu, motor \(perfil real\)")
        self.assertIn("ARRANCO mundo reloj=sim", out, "el reloj de simulación sin escribir [clock]")
        self.assertIn("ARRANCO cerebro reloj=sim", out)

    def test_profile_sim_es_igual_que_sim(self):
        code, out = self.corre("--profile", "sim", esperar=("ARRANCO mundo",))
        self.assertEqual(code, 0, out)
        self.assertNotIn("ARRANCO imu", out)
        self.assertIn("perfil sim · reloj de simulación", out)

    def test_skip_de_otro_perfil_no_es_error_y_avisa(self):
        code, out = self.corre("--sim", "--skip", "imu", esperar=("ARRANCO mundo",))
        self.assertEqual(code, 0, out)
        self.assertIn("--skip imu: no hace falta, 'imu' es del perfil real y no corre en el perfil 'sim'", out)
        self.assertNotIn("ARRANCO imu", out)

    def test_skip_de_un_nodo_que_corre_sigue_como_siempre(self):
        code, out = self.corre("--sim", "--skip", "mundo", esperar=("ARRANCO cerebro",))
        self.assertEqual(code, 0, out)
        self.assertNotIn("ARRANCO mundo", out)
        self.assertNotIn("no hace falta", out)

    def test_play_sin_profile_usa_replay(self):
        code, out = self.corre("--play", self.path, esperar=("ARRANCO cerebro",))
        self.assertEqual(code, 0, out)
        self.assertIn("perfil replay · reloj de la grabación", out)
        self.assertIn("no corren: imu, motor (perfil real), mundo (perfil sim)", out)
        self.assertNotIn("ARRANCO imu", out)
        self.assertNotIn("ARRANCO motor", out)
        self.assertNotIn("ARRANCO mundo", out)
        self.assertIn("ARRANCO cerebro reloj=replay", out)

    def test_play_con_profile_real_corre_los_real_con_el_reloj_de_la_grabacion(self):
        code, out = self.corre("--play", self.path, "--profile", "real",
                               esperar=("ARRANCO imu", "ARRANCO motor", "ARRANCO cerebro"))
        self.assertEqual(code, 0, out)
        self.assertNotIn("ARRANCO mundo", out)
        self.assertIn("ARRANCO imu reloj=replay", out)
        self.assertIn("ARRANCO motor reloj=replay", out)
        self.assertIn("perfil real · reloj de la grabación", out)


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

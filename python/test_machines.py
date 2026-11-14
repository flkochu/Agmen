"""agmen.machines y [[machine]] en agmen run (RFC 0011 §3.4, parte 3a).

Sin dos computadoras reales: "otra computadora" es 192.0.2.10 (TEST-NET-1, RFC 5737)
y los nombres `.invalid` (RFC 6761), que nunca existen. Nada manda paquetes a la red.

    python -m unittest discover -s python
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agmen import machines
from agmen.machines import DEFAULT_PORT, lan_addresses, placement, split_host, these_are_here
from agmen.run import load_config

ROOT = Path(__file__).resolve().parents[1]
AJENA = "192.0.2.10"


class TestSplitHost(unittest.TestCase):
    def test_formatos(self):
        casos = {
            "192.168.1.20": ("192.168.1.20", DEFAULT_PORT),
            "192.168.1.20:7400": ("192.168.1.20", 7400),
            "robot": ("robot", DEFAULT_PORT),
            "robot.local": ("robot.local", DEFAULT_PORT),
            "robot.local:1": ("robot.local", 1),
            "robot.local:65535": ("robot.local", 65535),
            "[fe80::1]:7400": ("fe80::1", 7400),
            "[fe80::1]": ("fe80::1", DEFAULT_PORT),
            "fe80::1": ("fe80::1", DEFAULT_PORT),
            "::1": ("::1", DEFAULT_PORT),
            "  192.168.1.20  ": ("192.168.1.20", DEFAULT_PORT),
        }
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(split_host(texto), esperado)

    def test_puerto_por_defecto_es_7391(self):
        self.assertEqual(DEFAULT_PORT, 7391)

    def test_errores(self):
        casos = {
            "": "vacío",
            "   ": "vacío",
            "http://robot": "http://",
            "robot/ruta": "sin http://",
            "mi robot": "espacios",
            "1.2.3.4 :7391": "espacios",
            "1.2.3.4:0": "1 y 65535",
            "1.2.3.4:65536": "1 y 65535",
            "1.2.3.4:abc": "1 y 65535",
            "1.2.3.4:": "1 y 65535",
            "1.2.3.4:-5": "1 y 65535",
            "1.2.3.4:+5": "1 y 65535",
            ":7391": "falta la IP",
            "[fe80::1]:0": "1 y 65535",
            "[fe80::1]x": "[fe80::1]:7391",
            "[fe80::1": "[fe80::1]:7391",
            "[robot]:7391": "IPv6",
            "fe80::zz": "IPv6",
            "1:2:3": "IPv6",
        }
        for texto, frag in casos.items():
            with self.subTest(texto=texto):
                with self.assertRaises(ValueError) as ctx:
                    split_host(texto)
                self.assertIn(frag, str(ctx.exception))

    def test_no_consulta_el_dns(self):
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS")), \
                mock.patch("socket.gethostbyname", side_effect=AssertionError("DNS")), \
                mock.patch("socket.gethostbyname_ex", side_effect=AssertionError("DNS")):
            self.assertEqual(split_host("no-existe.invalid:7400"), ("no-existe.invalid", 7400))


class TestTheseAreHere(unittest.TestCase):
    def test_loopback_y_localhost(self):
        r = these_are_here(["127.0.0.1", "localhost", "LOCALHOST", "::1", "127.0.0.5"])
        self.assertEqual(r, {h: True for h in ["127.0.0.1", "localhost", "LOCALHOST", "::1", "127.0.0.5"]})

    def test_una_ip_de_esta_computadora(self):
        aqui = lan_addresses()
        if not aqui:
            self.skipTest("esta computadora no tiene IP en una red local")
        self.assertEqual(these_are_here(aqui), {ip: True for ip in aqui})

    def test_ip_de_otra_computadora(self):
        self.assertEqual(these_are_here([AJENA]), {AJENA: False})
        self.assertEqual(these_are_here(["198.51.100.7", "203.0.113.9"]),
                         {"198.51.100.7": False, "203.0.113.9": False})

    def test_nombre_que_no_existe(self):
        t0 = time.monotonic()
        r = these_are_here(["no-existe-agmen.invalid"], timeout=1.5)
        self.assertEqual(r, {"no-existe-agmen.invalid": False})
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_nombre_de_esta_computadora(self):
        nombre = socket.gethostname()
        try:
            ips = {i[4][0] for i in socket.getaddrinfo(nombre, None)}
        except OSError:
            self.skipTest("el nombre de esta computadora no resuelve")
        if not ips:
            self.skipTest("sin direcciones")
        self.assertTrue(these_are_here([nombre])[nombre])

    def test_mezcla_y_orden(self):
        r = these_are_here(["localhost", AJENA, "x.invalid", "127.0.0.1"])
        self.assertEqual(r, {"localhost": True, AJENA: False, "x.invalid": False, "127.0.0.1": True})

    def test_lista_vacia_y_repetidos(self):
        self.assertEqual(these_are_here([]), {})
        self.assertEqual(these_are_here(["localhost", "localhost"]), {"localhost": True})

    def test_dns_colgado_no_cuelga_y_es_otra_computadora(self):
        liberar = threading.Event()

        def colgado(*a, **k):
            liberar.wait(30)
            raise OSError("tarde")

        try:
            with mock.patch("socket.getaddrinfo", colgado):
                t0 = time.monotonic()
                r = these_are_here(["robot-colgado.local", AJENA], timeout=0.5)
                dt = time.monotonic() - t0
        finally:
            liberar.set()
        self.assertEqual(r, {"robot-colgado.local": False, AJENA: False})
        self.assertLess(dt, 2.5, f"tardó {dt:.1f} s con un DNS colgado y timeout=0.5")

    def test_dns_colgado_con_varios_nombres_comparten_el_limite(self):
        liberar = threading.Event()

        def colgado(*a, **k):
            liberar.wait(30)
            raise OSError("tarde")

        try:
            with mock.patch("socket.getaddrinfo", colgado):
                t0 = time.monotonic()
                r = these_are_here([f"n{i}.local" for i in range(6)])   # límite por defecto (~1.5 s)
                dt = time.monotonic() - t0
        finally:
            liberar.set()
        self.assertEqual(set(r.values()), {False})
        self.assertLess(dt, 3.5, f"6 nombres colgados tardaron {dt:.1f} s: el límite debe ser para todos juntos")

    def test_nombre_que_resuelve_a_loopback_es_de_aqui(self):
        with mock.patch("socket.getaddrinfo", return_value=[(socket.AF_INET, 2, 17, "", ("127.0.0.1", 0))]):
            self.assertEqual(these_are_here(["mi-robot.local"]), {"mi-robot.local": True})

    def test_nombre_que_resuelve_a_ip_ajena_es_otra(self):
        with mock.patch("socket.getaddrinfo", return_value=[(socket.AF_INET, 2, 17, "", (AJENA, 0))]):
            self.assertEqual(these_are_here(["otro.local"]), {"otro.local": False})

    def test_unicode_raro_no_revienta(self):
        r = these_are_here(["é" * 300 + ".local", "a..b"], timeout=1.0)
        self.assertEqual(set(r.values()), {False})


class TestLanAddresses(unittest.TestCase):
    def test_sigue_existiendo_en_el_panel(self):
        from agmen.panel import server
        self.assertTrue(callable(server.lan_addresses))
        self.assertIs(server.lan_addresses, machines.lan_addresses)

    def test_no_incluye_loopback(self):
        for ip in lan_addresses():
            self.assertFalse(ip.startswith("127."), ip)


def robot_toml(maquinas="", nodos=(("motor", None, "robot"), ("imu", None, "robot"), ("ia", None, None)),
               extra=""):
    """maquinas: [(name, host)]. nodos: [(name, profile, machine)]."""
    t = '[robot]\nname = "r"\n'
    for name, host in maquinas:
        t += f'\n[[machine]]\nname = "{name}"\nhost = "{host}"\n'
    for name, profile, machine in nodos:
        t += f'\n[[node]]\nname = "{name}"\ncmd = "x"\n'
        if profile:
            t += f'profile = "{profile}"\n'
        if machine:
            t += f'machine = "{machine}"\n'
    return t + extra


class TestPlacement(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def cfg(self, text, profile=None):
        p = Path(self.dir) / "robot.toml"
        p.write_text(text, encoding="utf-8")
        return load_config(p, profile)

    def test_aqui_y_afuera(self):
        c = self.cfg(robot_toml([("robot", "127.0.0.1"), ("laptop", AJENA)],
                                [("motor", None, "robot"), ("ia", None, "laptop"), ("x", None, None)]))
        self.assertEqual(placement(c), {"robot": True, "laptop": False})

    def test_solo_las_que_usan_los_nodos(self):
        c = self.cfg(robot_toml([("robot", "127.0.0.1"), ("sobra", AJENA)], [("motor", None, "robot")]))
        self.assertEqual(placement(c), {"robot": True})

    def test_sin_nodos_que_usen_la_maquina_no_se_resuelve_nada(self):
        c = self.cfg(robot_toml([("robot", "no-existe.invalid")], [("motor", None, None)]))
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS")):
            self.assertEqual(placement(c), {})

    def test_sin_machines(self):
        self.assertEqual(placement(self.cfg(robot_toml(nodos=[("motor", None, None)]))), {})

    def test_local_todas_son_de_aqui_sin_consultar_nada(self):
        c = self.cfg(robot_toml([("robot", AJENA), ("laptop", "x.invalid")],
                                [("motor", None, "robot"), ("ia", None, "laptop")],
                                '\n[profile.sim]\nlocal = true\n'), "sim")
        self.assertTrue(c.local)
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS")):
            self.assertEqual(placement(c), {"robot": True, "laptop": True})

    def test_por_perfil(self):
        text = robot_toml([("robot", AJENA), ("laptop", "127.0.0.1")],
                          [("motor", "real", "robot"), ("mundo", "sim", "laptop"), ("ia", None, None)])
        self.assertEqual(placement(self.cfg(text, "real")), {"robot": False})
        self.assertEqual(placement(self.cfg(text, "sim")), {"laptop": True})

    def test_mux_cuenta_como_nodo(self):
        mux = ('\n[[mux]]\nname = "ruedas"\noutput = "127.0.0.1:7500"\nmachine = "robot"\n'
               '[[mux.input]]\nname = "a"\nlisten = "127.0.0.1:7501"\npriority = 1\n')
        c = self.cfg(robot_toml([("robot", AJENA)], [("ia", None, None)]) + mux)
        self.assertEqual(placement(c), {"robot": False})

    def test_dns_colgado_en_placement(self):
        liberar = threading.Event()

        def colgado(*a, **k):
            liberar.wait(30)
            raise OSError

        c = self.cfg(robot_toml([("robot", "colgado.local")], [("motor", None, "robot")]))
        try:
            with mock.patch("socket.getaddrinfo", colgado):
                t0 = time.monotonic()
                r = placement(c, timeout=0.5)
                dt = time.monotonic() - t0
        finally:
            liberar.set()
        self.assertEqual(r, {"robot": False})
        self.assertLess(dt, 2.5)


NODO = """\
import os, sys, time
print(f"ARRANCO {sys.argv[1]}", flush=True)
time.sleep(120)
"""

PUERTO_HUB = 7478


class TestMaquinasEnLaLineaDeComandos(unittest.TestCase):
    """agmen run con [[machine]] (RFC 0011 §3.4), como subproceso: sin panel; el hub, apagado salvo
    donde se revisa que no arranque."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.toml = os.path.join(self.dir, "robot.toml")
        self.nodo = os.path.join(self.dir, "nodo.py")
        with open(self.nodo, "w", encoding="utf-8") as f:
            f.write(NODO)

    def robot(self, hosts, nodos, extra="", hub=False):
        """hosts: {name: host}. nodos: [(name, profile, machine)]."""
        t = '[robot]\nname = "r"\n'
        for name, host in hosts.items():
            t += f'\n[[machine]]\nname = "{name}"\nhost = "{host}"\n'
        for name, profile, machine in nodos:
            t += f"\n[[node]]\nname = \"{name}\"\ncmd = ['python', '{self.nodo}', '{name}']\n"
            if profile:
                t += f'profile = "{profile}"\n'
            if machine:
                t += f'machine = "{machine}"\n'
        t += f"\n[hub]\nenabled = {str(hub).lower()}\n"
        t += "\n[web]\nenabled = false\n" + extra
        with open(self.toml, "w", encoding="utf-8") as f:
            f.write(t)

    def env(self):
        return {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1",
                "AGMEN_HUB": f"127.0.0.1:{PUERTO_HUB}"}

    def falla(self, *args, seg=60):
        t0 = time.monotonic()
        r = subprocess.run([sys.executable, "-m", "agmen", "run", self.toml, "--no-browser", *args],
                           env=self.env(), capture_output=True, text=True, encoding="utf-8", timeout=seg,
                           check=False)
        return r, time.monotonic() - t0

    def corre(self, *args, esperar=("ARRANCO motor",), seg=30.0):
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
        time.sleep(0.7)
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

    NODOS = [("motor", None, "robot"), ("imu", None, "robot"), ("ia", None, None)]

    def test_host_de_aqui_corre_y_lo_dice(self):
        self.robot({"robot": "127.0.0.1"}, self.NODOS)
        code, out = self.corre(esperar=("ARRANCO motor", "ARRANCO imu", "ARRANCO ia"))
        self.assertEqual(code, 0, out)
        self.assertIn("robot (127.0.0.1): es esta computadora; sus nodos corren aquí (motor, imu)", out)
        for n in ("motor", "imu", "ia"):
            self.assertIn(f"ARRANCO {n}", out)
        # después de la cabecera
        self.assertLess(out.index("agmen"), out.index("es esta computadora"))

    def test_puerto_distinto_se_muestra(self):
        self.robot({"robot": "127.0.0.1:7400"}, self.NODOS)
        code, out = self.corre()
        self.assertEqual(code, 0, out)
        self.assertIn("robot (127.0.0.1:7400): es esta computadora", out)

    def test_otra_computadora_no_arranca_nada_y_sale_con_2(self):
        self.robot({"robot": AJENA}, self.NODOS, hub=True)
        r, _ = self.falla()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(f"error: 'motor' e 'imu' corren en 'robot' ({AJENA}) y esta versión todavía no arranca "
                      f"nodos en otra computadora (llega con agmen agent). Mientras tanto: corre agmen run en "
                      f"esa computadora, o usa [profile.real] local = true para correr todo aquí", r.stderr)
        self.assertNotIn("ARRANCO", r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        salida = r.stdout + r.stderr
        self.assertNotIn("iniciado", salida)       # ni el hub ni ningún nodo
        self.assertNotIn("agmen hub en", salida)
        self.assertNotIn("directorio de topics", salida)

    def test_un_solo_nodo_remoto_en_singular(self):
        self.robot({"robot": AJENA}, [("motor", None, "robot"), ("ia", None, None)])
        r, _ = self.falla()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(f"'motor' corre en 'robot' ({AJENA}) y esta versión", r.stderr)
        self.assertNotIn("ARRANCO", r.stdout)

    def test_dos_computadoras_ajenas_y_una_de_aqui_sigue_siendo_error(self):
        self.robot({"robot": AJENA, "laptop": "127.0.0.1"},
                   [("motor", None, "robot"), ("ia", None, "laptop"), ("imu", None, None)])
        r, _ = self.falla()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("'motor' corre en 'robot'", r.stderr)
        self.assertNotIn("ARRANCO", r.stdout)

    def test_skip_del_nodo_remoto_sigue_siendo_error(self):
        self.robot({"robot": AJENA}, self.NODOS)
        r, _ = self.falla("--skip", "motor")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("corren en 'robot'", r.stderr)
        self.assertNotIn("ARRANCO", r.stdout)

    def test_machine_sin_nodos_en_el_perfil_activo_no_estorba(self):
        self.robot({"robot": AJENA},
                   [("motor", "real", "robot"), ("mundo", "sim", None), ("ia", None, None)])
        code, out = self.corre("--sim", esperar=("ARRANCO mundo",))
        self.assertEqual(code, 0, out)
        self.assertIn("ARRANCO mundo", out)
        self.assertNotIn("es esta computadora", out)
        self.assertNotIn(AJENA, out)

    def test_local_corre_todo_aqui_con_su_linea(self):
        self.robot({"robot": AJENA}, [("motor", "real", "robot"), ("mundo", "sim", "robot"), ("imu", "sim", "robot")],
                   '\n[profile.sim]\nlocal = true\n')
        code, out = self.corre("--sim", esperar=("ARRANCO mundo", "ARRANCO imu"))
        self.assertEqual(code, 0, out)
        self.assertIn("local = true: todo corre en esta computadora (se ignora machine: mundo, imu)", out)
        self.assertNotIn("es esta computadora", out)

    def test_local_sin_machine_en_los_nodos(self):
        self.robot({"robot": AJENA}, [("motor", None, None)], '\n[profile.sim]\nlocal = true\n')
        code, out = self.corre("--sim", esperar=("ARRANCO motor",))
        self.assertEqual(code, 0, out)
        self.assertIn("local = true: todo corre en esta computadora", out)
        self.assertNotIn("se ignora machine", out)

    def test_el_mismo_robot_sin_local_falla(self):
        self.robot({"robot": AJENA}, [("motor", "real", "robot"), ("mundo", "sim", "robot")],
                   '\n[profile.sim]\nlocal = true\n')
        r, _ = self.falla()   # perfil real: sin local
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("[profile.real] local = true", r.stderr)

    def test_nombre_que_no_resuelve_es_error_sin_colgarse(self):
        self.robot({"robot": "agmen-no-existe.invalid"}, self.NODOS)
        r, dt = self.falla(seg=60)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("corren en 'robot' (agmen-no-existe.invalid)", r.stderr)
        self.assertNotIn("ARRANCO", r.stdout)
        self.assertLess(dt, 25.0, f"agmen run tardó {dt:.1f} s con un nombre que no resuelve")

    def test_machine_mal_escrita_es_error_de_configuracion(self):
        self.robot({"robot": "127.0.0.1"}, [("motor", None, "robto")])
        r, _ = self.falla()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("¿Quisiste decir 'robot'?", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_sin_machine_no_imprime_ninguna_linea_nueva(self):
        self.robot({}, [("motor", None, None), ("ia", None, None)])
        code, out = self.corre(esperar=("ARRANCO motor", "ARRANCO ia"))
        self.assertEqual(code, 0, out)
        for frag in ("computadora", "local = true", "machine"):
            self.assertNotIn(frag, out)

    def test_local_sin_machine_declarada_no_imprime_nada_nuevo(self):
        self.robot({}, [("motor", None, None)], '\n[profile.sim]\nlocal = true\n')
        code, out = self.corre("--sim", esperar=("ARRANCO motor",))
        self.assertEqual(code, 0, out)
        self.assertNotIn("local = true", out)
        self.assertNotIn("computadora", out)


if __name__ == "__main__":
    unittest.main()

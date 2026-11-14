"""agmen.machines y [[machine]] en agmen run (RFC 0011 §3.4, parte 3a).

Sin dos computadoras reales: "otra computadora" es 192.0.2.10 (TEST-NET-1, RFC 5737)
y los nombres `.invalid` (RFC 6761), que nunca existen. Nada manda paquetes a la red.

    python -m unittest discover -s python
"""

from __future__ import annotations

import io
import json
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
from agmen.machines import Place, and_names, check_hosts, place_machines, placement_lines, remote_error
from agmen.run import ConfigError, Launcher, load_config

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
                      f"esa computadora. Para probar aquí en simulación: [profile.sim] local = true y "
                      f"agmen run --sim", r.stderr)
        self.assertNotIn("[profile.real] local", r.stderr)   # I4: en el perfil real no se sugiere
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
        # I4: en el perfil real no se sugiere local = true (serían los drivers del robot aquí)
        self.assertNotIn("[profile.real] local", r.stderr)
        self.assertIn("Para probar aquí en simulación: [profile.sim] local = true y agmen run --sim", r.stderr)

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

    def test_remota_en_sim_sin_local_sugiere_local_del_perfil_sim(self):
        self.robot({"robot": AJENA}, [("mundo", "sim", "robot"), ("ia", None, None)])
        r, _ = self.falla("--sim")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("Mientras tanto: corre agmen run en esa computadora, o usa [profile.sim] local = true "
                      "para correr todo aquí", r.stderr)
        self.assertNotIn("Para probar aquí en simulación", r.stderr)
        self.assertNotIn("ARRANCO", r.stdout)

    def test_varias_computadoras_no_dice_corre_en_cada_una(self):
        self.robot({"laptop": "127.0.0.1", "robot": AJENA},
                   [("ia", None, "laptop"), ("motor", None, "robot")])
        r, _ = self.falla()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("Con nodos en varias computadoras ('laptop', 'robot') hace falta agmen agent, que llega en "
                      "una versión próxima. Mientras tanto: quita machine de los nodos para correr todo en una "
                      "sola computadora", r.stderr)
        self.assertNotIn("cada computadora", r.stderr)
        self.assertNotIn("ARRANCO", r.stdout)

    def test_nombre_sin_resolver_dice_que_pongas_la_ip(self):
        self.robot({"robot": "agmen-no-existe.invalid"}, self.NODOS)
        r, _ = self.falla()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("No pude resolver 'agmen-no-existe.invalid'", r.stderr)
        self.assertIn("si esta es esa computadora, pon su IP en host.", r.stderr)

    def test_ipv6_link_local_sin_zona_dice_que_falta_la_interfaz(self):
        self.robot({"robot": "fe80::1"}, self.NODOS)
        r, _ = self.falla()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("%interfaz", r.stderr)
        self.assertNotIn("ARRANCO", r.stdout)

    def test_difusion_como_host_es_error_al_leer(self):
        self.robot({"robot": "255.255.255.255"}, self.NODOS)
        r, _ = self.falla()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("no es la dirección de una computadora", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_linea_de_nodos_sin_machine(self):
        self.robot({"robot": "127.0.0.1"}, self.NODOS)
        code, out = self.corre(esperar=("ARRANCO motor", "ARRANCO imu", "ARRANCO ia"))
        self.assertEqual(code, 0, out)
        self.assertIn("sin machine, corren en esta computadora: ia", out)

    def test_con_local_no_sale_la_linea_de_sin_machine(self):
        self.robot({"robot": AJENA}, [("motor", "sim", "robot"), ("ia", "sim", None)], '\n[profile.sim]\nlocal = true\n')
        code, out = self.corre("--sim", esperar=("ARRANCO motor", "ARRANCO ia"))
        self.assertEqual(code, 0, out)
        self.assertNotIn("sin machine", out)

    def test_sin_machine_declaradas_no_sale_la_linea_de_sin_machine(self):
        self.robot({}, [("motor", None, None)])
        code, out = self.corre(esperar=("ARRANCO motor",))
        self.assertEqual(code, 0, out)
        self.assertNotIn("sin machine", out)


# ---------------------------------------------------------------------------
# Pruebas del bloque 3a tras la revisión (B1, I1-I4, M1-M6)
# ---------------------------------------------------------------------------

def _config(text, profile=None):
    """Carga un robot.toml escrito en texto (en un directorio temporal)."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "robot.toml"
        path.write_text(text, encoding="utf-8")
        return load_config(path, profile)


def _falso_socket(propia, bind_ok=True, connect_error=None):
    """Un socket.socket falso: getsockname() devuelve `propia`; bind() funciona siempre (o falla)."""
    sock = mock.MagicMock()
    sock.__enter__.return_value = sock
    sock.getsockname.return_value = (propia, 4242)
    sock.bind.side_effect = None if bind_ok else OSError("no se puede")
    if connect_error is not None:
        sock.connect.side_effect = connect_error
    fabrica = mock.MagicMock(return_value=sock)
    return fabrica, sock


class TestEsEstaComputadoraSeguridad(unittest.TestCase):
    """B1: una IP es de aquí solo por loopback o porque el sistema la elige como origen
    (connect UDP). Nunca por un bind. Ante la duda, es otra computadora."""

    def test_getsockname_con_otra_ip_no_es_de_aqui(self):
        fabrica, sock = _falso_socket("10.0.0.7")
        with mock.patch("socket.socket", fabrica):
            r = check_hosts(["192.168.1.20"])
        self.assertEqual(r["192.168.1.20"].here, False)
        self.assertEqual(r["192.168.1.20"].why, "other")
        sock.connect.assert_called_once_with(("192.168.1.20", 1))

    def test_bind_que_siempre_funciona_no_cambia_el_resultado_y_ni_se_llama(self):
        fabrica, sock = _falso_socket("10.0.0.7", bind_ok=True)
        with mock.patch("socket.socket", fabrica):
            r = check_hosts(["192.168.1.20", "8.8.8.8", AJENA])
        self.assertEqual({h: p.here for h, p in r.items()}, {"192.168.1.20": False, "8.8.8.8": False, AJENA: False})
        sock.bind.assert_not_called()

    def test_getsockname_con_la_misma_ip_si_es_de_aqui(self):
        fabrica, sock = _falso_socket("192.168.1.20")
        with mock.patch("socket.socket", fabrica):
            r = check_hosts(["192.168.1.20"])
        self.assertEqual(r["192.168.1.20"], Place(True, "mine"))
        sock.bind.assert_not_called()

    def test_misma_ipv6_escrita_distinto_es_de_aqui(self):
        fabrica, _ = _falso_socket("2001:db8:0:0:0:0:0:5")
        with mock.patch("socket.socket", fabrica):
            self.assertTrue(check_hosts(["2001:db8::5"])["2001:db8::5"].here)

    def test_connect_que_falla_es_otra_computadora(self):
        for error in (OSError(101, "Network is unreachable"), PermissionError(13, "EACCES")):
            with self.subTest(error=type(error).__name__):
                fabrica, _ = _falso_socket("192.168.1.20", connect_error=error)
                with mock.patch("socket.socket", fabrica):
                    self.assertEqual(check_hosts(["192.168.1.20"])["192.168.1.20"].here, False)

    def test_no_se_pueden_crear_sockets_es_otra_computadora(self):
        with mock.patch("socket.socket", side_effect=OSError("sin sockets")):
            self.assertEqual(check_hosts(["192.168.1.20"])["192.168.1.20"], Place(False, "other"))

    def test_difusion_multicast_y_cualquiera_son_special_sin_tocar_sockets(self):
        # aunque el socket dijera "es mía" y el bind funcionara, estas nunca son una computadora
        fabrica, sock = _falso_socket("255.255.255.255")
        casos = ["255.255.255.255", "0.0.0.0", "224.0.0.1", "239.255.255.250", "::", "ff02::1"]
        with mock.patch("socket.socket", fabrica):
            r = check_hosts(casos)
        for h in casos:
            with self.subTest(host=h):
                self.assertFalse(r[h].here)
                self.assertEqual(r[h].why, "special")
                self.assertTrue(r[h].note)
        sock.bind.assert_not_called()
        sock.connect.assert_not_called()

    def test_la_difusion_de_la_subred_no_es_de_aqui(self):
        # 192.168.1.255 con connect real: o EACCES o sin ruta; con un socket que "acepta todo" tampoco
        fabrica, _ = _falso_socket("10.0.0.7")
        with mock.patch("socket.socket", fabrica):
            self.assertFalse(check_hosts(["192.168.1.255"])["192.168.1.255"].here)
        fabrica, _ = _falso_socket("192.168.1.5", connect_error=PermissionError(13, "EACCES"))
        with mock.patch("socket.socket", fabrica):
            self.assertFalse(check_hosts(["192.168.1.255"])["192.168.1.255"].here)

    def test_loopback_es_de_aqui_sin_sockets(self):
        with mock.patch("socket.socket", side_effect=AssertionError("no debe abrir sockets")):
            r = check_hosts(["127.0.0.1", "127.9.9.9", "::1", "localhost", "LocalHost"])
        for h, p in r.items():
            with self.subTest(host=h):
                self.assertTrue(p.here)
                self.assertEqual(p.why, "loopback")

    def test_ipv6_link_local_sin_zona(self):
        fabrica, sock = _falso_socket("fe80::1")
        with mock.patch("socket.socket", fabrica):
            p = check_hosts(["fe80::1"])["fe80::1"]
        self.assertFalse(p.here)
        self.assertEqual(p.why, "zone")
        self.assertIn("%interfaz", p.note)
        sock.connect.assert_not_called()

    def test_ipv6_link_local_con_zona_se_revisa_por_connect(self):
        fabrica, _ = _falso_socket("fe80::1%eth0")
        with mock.patch("socket.socket", fabrica):
            p = check_hosts(["fe80::1%eth0"])["fe80::1%eth0"]
        self.assertEqual(p, Place(True, "mine"))
        fabrica, _ = _falso_socket("fe80::99%eth0")
        with mock.patch("socket.socket", fabrica):
            self.assertFalse(check_hosts(["fe80::1%eth0"])["fe80::1%eth0"].here)

    def test_nombre_cuyo_ip_es_solo_otra_no_es_de_aqui_aunque_bind_funcione(self):
        fabrica, sock = _falso_socket("10.0.0.7")
        info = [(socket.AF_INET, 2, 17, "", (AJENA, 0))]
        with mock.patch("socket.socket", fabrica), mock.patch("socket.getaddrinfo", return_value=info):
            p = check_hosts(["robot.local"])["robot.local"]
        self.assertFalse(p.here)
        self.assertEqual(p.why, "other")
        self.assertIn(AJENA, p.note)
        sock.bind.assert_not_called()

    def test_nombre_con_varias_ips_basta_una_de_aqui(self):
        info = [(socket.AF_INET, 2, 17, "", (AJENA, 0)), (socket.AF_INET, 2, 17, "", ("127.0.0.1", 0))]
        with mock.patch("socket.getaddrinfo", return_value=info):
            self.assertEqual(check_hosts(["dos.local"])["dos.local"], Place(True, "mine"))

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("unshare"),
                         "necesita Linux con el comando unshare (espacio de red propio)")
    def test_en_un_espacio_de_red_con_ip_nonlocal_bind_nada_ajeno_es_de_aqui(self):
        """La prueba real de B1: en un netns con net.ipv4.ip_nonlocal_bind=1 un bind() funciona con
        CUALQUIER IP (y con la difusión). these_are_here no debe creerlo. Se arma la red sin el comando ip."""
        with tempfile.TemporaryDirectory() as d:
            guion = Path(d) / "dentro.py"
            guion.write_text(NETNS_GUION, encoding="utf-8")
            sonda = subprocess.run(["unshare", "-rn", sys.executable, "-I", "-c", "print('ok')"],
                                   capture_output=True, text=True, timeout=30, check=False)
            if sonda.returncode != 0 or "ok" not in sonda.stdout:
                self.skipTest(f"unshare -rn no está disponible aquí: {sonda.stderr.strip()[:120]}")
            r = subprocess.run(["unshare", "-rn", sys.executable, str(guion)],
                               env={**os.environ, "PYTHONPATH": str(ROOT / "python")},
                               capture_output=True, text=True, timeout=60, check=False)
        if r.returncode == 77:
            self.skipTest(f"no se pudo armar la red de prueba: {r.stdout.strip()[-200:]}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        datos = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(datos["bind_ajeno_funciona"], "el netns no tiene ip_nonlocal_bind: la prueba no prueba nada")
        self.assertEqual(datos["ajenas"], {"192.168.1.20": False, "8.8.8.8": False, "255.255.255.255": False,
                                           "192.168.1.255": False, "10.9.9.255": False})
        # y las de la propia red sí (cualquier interfaz, no solo la de la ruta por defecto)
        self.assertEqual(datos["propias"], {"192.168.1.5": True, "10.9.9.9": True, "127.0.0.1": True})


# Corre dentro de `unshare -rn`: arma lo, 192.168.1.5 (ruta por defecto) y 10.9.9.9, sin la herramienta ip.
NETNS_GUION = r"""
import ctypes, fcntl, json, socket, struct, sys

SIOCSIFADDR, SIOCSIFNETMASK, SIOCSIFFLAGS, SIOCGIFFLAGS, SIOCADDRT = 0x8916, 0x891C, 0x8914, 0x8913, 0x890B
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sa = lambda ip: struct.pack("HH4s8x", socket.AF_INET, 0, socket.inet_aton(ip))
ifreq = lambda name, data: struct.pack("16s16s", name.encode(), data)
try:
    flags = struct.unpack("16sH14x", fcntl.ioctl(s, SIOCGIFFLAGS, ifreq("lo", b"")))[1]
    fcntl.ioctl(s, SIOCSIFFLAGS, struct.pack("16sH14x", b"lo", flags | 1))
    for name, ip in (("lo:1", "192.168.1.5"), ("lo:2", "10.9.9.9")):
        fcntl.ioctl(s, SIOCSIFADDR, ifreq(name, sa(ip)))
        fcntl.ioctl(s, SIOCSIFNETMASK, ifreq(name, sa("255.255.255.0")))

    class SA(ctypes.Structure):
        _fields_ = [("data", ctypes.c_char * 16)]

    class Rt(ctypes.Structure):
        _fields_ = [("p1", ctypes.c_ulong), ("dst", SA), ("gw", SA), ("mask", SA), ("flags", ctypes.c_ushort),
                    ("p2", ctypes.c_short), ("p3", ctypes.c_ulong), ("p4", ctypes.c_void_p),
                    ("metric", ctypes.c_short), ("dev", ctypes.c_char_p), ("window", ctypes.c_ulong),
                    ("irtt", ctypes.c_ushort)]

    rt = Rt()
    rt.dst.data, rt.mask.data, rt.dev, rt.flags = sa("0.0.0.0"), sa("0.0.0.0"), b"lo", 1
    fcntl.ioctl(s, SIOCADDRT, bytes(rt))
    with open("/proc/sys/net/ipv4/ip_nonlocal_bind", "w") as f:
        f.write("1")
except OSError as e:
    print(f"sin red de prueba: {e}")
    sys.exit(77)

from agmen.machines import these_are_here

try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as t:
        t.bind(("8.8.8.8", 0))
    bind_ajeno = True
except OSError:
    bind_ajeno = False
print(json.dumps({
    "bind_ajeno_funciona": bind_ajeno,
    "ajenas": these_are_here(["192.168.1.20", "8.8.8.8", "255.255.255.255", "192.168.1.255", "10.9.9.255"]),
    "propias": these_are_here(["192.168.1.5", "10.9.9.9", "127.0.0.1"]),
}))
"""


class TestCheckHosts(unittest.TestCase):
    def test_nombre_que_la_red_no_conoce_es_unresolved(self):
        p = check_hosts(["agmen-no-existe.invalid"], timeout=2.0)["agmen-no-existe.invalid"]
        self.assertEqual((p.here, p.why), (False, "unresolved"))
        self.assertIn("agmen-no-existe.invalid", p.note)
        self.assertIn("la red no lo conoce", p.note)

    def test_getaddrinfo_que_falla_o_no_devuelve_nada_es_unresolved(self):
        for efecto in ({"side_effect": socket.gaierror(-2, "Name or service not known")},
                       {"side_effect": UnicodeError("label")}, {"return_value": []}):
            with self.subTest(efecto=list(efecto)[0]), mock.patch("socket.getaddrinfo", **efecto):
                self.assertEqual(check_hosts(["x.local"])["x.local"].why, "unresolved")

    def test_dns_colgado_es_timeout_con_el_limite_en_la_nota(self):
        liberar = threading.Event()

        def colgado(*a, **k):
            liberar.wait(30)
            raise OSError("tarde")

        try:
            with mock.patch("socket.getaddrinfo", colgado):
                t0 = time.monotonic()
                p = check_hosts(["robot.local"], timeout=0.5)["robot.local"]
                dt = time.monotonic() - t0
        finally:
            liberar.set()
        self.assertEqual((p.here, p.why), (False, "timeout"))
        self.assertIn("0.5 s", p.note)
        self.assertIn("robot.local", p.note)
        self.assertLess(dt, 2.5)

    def test_timeout_por_defecto_es_1_5_s(self):
        self.assertEqual(machines.RESOLVE_TIMEOUT, 1.5)
        liberar = threading.Event()

        def colgado(*a, **k):
            liberar.wait(30)
            raise OSError

        try:
            with mock.patch("socket.getaddrinfo", colgado):
                p = check_hosts(["robot.local"])["robot.local"]
        finally:
            liberar.set()
        self.assertIn("en 1.5 s", p.note)

    def test_ipv6_link_local_sin_zona_es_zone(self):
        p = check_hosts(["fe80::1"])["fe80::1"]
        self.assertEqual((p.here, p.why), (False, "zone"))
        self.assertIn("%interfaz", p.note)

    def test_special(self):
        r = check_hosts(["255.255.255.255", "0.0.0.0", "224.0.0.1", "::"])
        self.assertEqual({p.why for p in r.values()}, {"special"})
        self.assertFalse(any(p.here for p in r.values()))

    def test_loopback(self):
        p = check_hosts(["127.0.0.1"])["127.0.0.1"]
        self.assertEqual((p.here, p.why), (True, "loopback"))

    def test_place_es_inmutable_y_note_vacia_por_defecto(self):
        p = Place(True, "mine")
        self.assertEqual(p.note, "")
        with self.assertRaises(Exception):
            p.here = False

    def test_these_are_here_es_check_hosts_en_bool(self):
        hosts = ["127.0.0.1", AJENA, "x.invalid", "0.0.0.0", "fe80::1"]
        self.assertEqual(these_are_here(hosts, 1.0), {h: p.here for h, p in check_hosts(hosts, 1.0).items()})
        self.assertTrue(all(isinstance(v, bool) for v in these_are_here(hosts, 1.0).values()))

    def test_place_machines_y_placement(self):
        c = _config(robot_toml([("robot", "127.0.0.1"), ("laptop", AJENA)],
                               [("motor", None, "robot"), ("ia", None, "laptop")]))
        pm = place_machines(c)
        self.assertEqual(pm["robot"], Place(True, "loopback"))
        self.assertEqual((pm["laptop"].here, pm["laptop"].why), (False, "other"))
        self.assertEqual(placement(c), {"robot": True, "laptop": False})

    def test_place_machines_local_no_consulta_nada(self):
        c = _config(robot_toml([("robot", AJENA)], [("motor", "sim", "robot")], "\n[profile.sim]\nlocal = true\n"),
                    "sim")
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS")), \
                mock.patch("socket.socket", side_effect=AssertionError("socket")):
            self.assertEqual(place_machines(c), {"robot": Place(True, "local")})


class TestAndNames(unittest.TestCase):
    def test_une_nombres(self):
        self.assertEqual(and_names(["motor"]), "'motor'")
        self.assertEqual(and_names(["motor", "imu"]), "'motor' e 'imu'")
        self.assertEqual(and_names(["imu", "motor"]), "'imu' y 'motor'")
        self.assertEqual(and_names(["a", "b", "imu"]), "'a', 'b' e 'imu'")
        self.assertEqual(and_names(["a", "b", "c"]), "'a', 'b' y 'c'")


class TestRemoteError(unittest.TestCase):
    """Los mensajes de error cuando hay nodos en otra computadora (I1, I2, I4)."""

    OTRA = Place(False, "other")
    AQUI = Place(True, "loopback")

    def cfg(self, maquinas, nodos, extra="", profile=None):
        return _config(robot_toml(maquinas, nodos, extra), profile)

    def test_una_remota_en_real(self):
        c = self.cfg([("robot", AJENA)], [("motor", None, "robot"), ("imu", None, "robot"), ("ia", None, None)])
        self.assertEqual(remote_error(c, {"robot": self.OTRA}),
                         f"'motor' e 'imu' corren en 'robot' ({AJENA}) y esta versión todavía no arranca nodos "
                         f"en otra computadora (llega con agmen agent). Mientras tanto: corre agmen run en esa "
                         f"computadora. Para probar aquí en simulación: [profile.sim] local = true y "
                         f"agmen run --sim")

    def test_una_remota_en_real_no_sugiere_local_en_real(self):
        c = self.cfg([("robot", AJENA)], [("motor", None, "robot")])
        self.assertNotIn("[profile.real] local", remote_error(c, {"robot": self.OTRA}))

    def test_un_solo_nodo_va_en_singular(self):
        c = self.cfg([("robot", AJENA)], [("motor", None, "robot")])
        self.assertTrue(remote_error(c, {"robot": self.OTRA}).startswith(f"'motor' corre en 'robot' ({AJENA}) y"))

    def test_remota_en_sim_sin_local_sugiere_local_del_perfil(self):
        c = self.cfg([("robot", AJENA)], [("mundo", "sim", "robot")], profile="sim")
        e = remote_error(c, {"robot": self.OTRA})
        self.assertTrue(e.endswith("Mientras tanto: corre agmen run en esa computadora, o usa "
                                   "[profile.sim] local = true para correr todo aquí"), e)
        self.assertNotIn("Para probar aquí en simulación", e)

    def test_otro_perfil_con_su_nombre(self):
        c = self.cfg([("robot", AJENA)], [("n", "pruebas", "robot")], '\n[profile.pruebas]\n', profile="pruebas")
        self.assertTrue(remote_error(c, {"robot": self.OTRA}).endswith(
            ", o usa [profile.pruebas] local = true para correr todo aquí"))

    def test_varias_computadoras_una_aqui_y_una_remota(self):
        c = self.cfg([("laptop", "127.0.0.1"), ("robot", AJENA)],
                     [("ia", None, "laptop"), ("motor", None, "robot")])
        e = remote_error(c, {"laptop": self.AQUI, "robot": self.OTRA})
        self.assertIn(f"'motor' corre en 'robot' ({AJENA}).", e)
        self.assertIn("Con nodos en varias computadoras ('laptop', 'robot') hace falta agmen agent, que llega "
                      "en una versión próxima.", e)
        self.assertIn("Mientras tanto: quita machine de los nodos para correr todo en una sola computadora", e)
        self.assertNotIn("cada computadora", e)
        self.assertNotIn("corre agmen run en", e)
        self.assertNotIn("'ia' corre", e)         # solo se nombra lo que corre allá

    def test_varias_computadoras_dos_remotas(self):
        c = self.cfg([("robot", AJENA), ("brazo", "198.51.100.7:7400")],
                     [("motor", None, "robot"), ("pinza", None, "brazo")])
        e = remote_error(c, {"robot": self.OTRA, "brazo": self.OTRA})
        self.assertIn(f"'motor' corre en 'robot' ({AJENA}); 'pinza' corre en 'brazo' (198.51.100.7:7400).", e)
        self.assertIn("('robot', 'brazo')", e)

    def test_varias_en_sim_sugiere_ademas_local(self):
        c = self.cfg([("a", AJENA), ("b", "198.51.100.7")], [("x", "sim", "a"), ("y", "sim", "b")], profile="sim")
        e = remote_error(c, {"a": self.OTRA, "b": self.OTRA})
        self.assertIn("quita machine de los nodos para correr todo en una sola computadora, o usa "
                      "[profile.sim] local = true para correr todo aquí", e)

    def test_nombre_que_no_resolvio(self):
        c = self.cfg([("robot", "robot.local")], [("motor", None, "robot")])
        nota = "no pude resolver 'robot.local' (la red no lo conoce): si esta es esa computadora, pon su IP en host"
        e = remote_error(c, {"robot": Place(False, "unresolved", nota)})
        self.assertIn("No pude resolver 'robot.local' (la red no lo conoce): si esta es esa computadora, "
                      "pon su IP en host.", e)

    def test_nombre_que_vencio_el_limite(self):
        c = self.cfg([("robot", "robot.local")], [("motor", None, "robot")])
        nota = "no pude resolver 'robot.local' en 1.5 s: si esta es esa computadora, pon su IP en host"
        e = remote_error(c, {"robot": Place(False, "timeout", nota)})
        self.assertIn("No pude resolver 'robot.local' en 1.5 s: si esta es esa computadora, pon su IP en host.", e)

    def test_ipv6_link_local_sin_zona_dice_que_falta_la_interfaz(self):
        c = self.cfg([("robot", "fe80::1")], [("motor", None, "robot")])
        e = remote_error(c, check_hosts_para(c))
        self.assertIn("%interfaz", e)
        self.assertIn("fe80::1", e)

    def test_nombre_que_no_resuelve_de_punta_a_punta(self):
        c = self.cfg([("robot", "agmen-no-existe.invalid")], [("motor", None, "robot")])
        e = remote_error(c, place_machines(c, 2.0))
        self.assertIn("No pude resolver 'agmen-no-existe.invalid' (la red no lo conoce)", e)
        self.assertIn("pon su IP en host", e)

    def test_dns_colgado_de_punta_a_punta(self):
        c = self.cfg([("robot", "robot.local")], [("motor", None, "robot")])
        liberar = threading.Event()

        def colgado(*a, **k):
            liberar.wait(30)
            raise OSError

        try:
            with mock.patch("socket.getaddrinfo", colgado):
                e = remote_error(c, place_machines(c, 0.5))
        finally:
            liberar.set()
        self.assertIn("No pude resolver 'robot.local' en 0.5 s:", e)

    def test_todo_aqui_o_sin_computadoras_no_hay_error(self):
        c = self.cfg([("robot", "127.0.0.1")], [("motor", None, "robot")])
        self.assertEqual(remote_error(c, {"robot": self.AQUI}), "")
        self.assertEqual(remote_error(c, {}), "")
        self.assertEqual(remote_error(_config(robot_toml(nodos=[("m", None, None)])), {}), "")

    def test_las_machine_que_no_estan_en_places_no_cuentan(self):
        c = self.cfg([("robot", AJENA), ("laptop", "127.0.0.1")], [("ia", None, "laptop"), ("motor", None, "robot")])
        self.assertEqual(remote_error(c, {"laptop": self.AQUI}), "")


def check_hosts_para(config):
    return place_machines(config, 1.0)


class TestPlacementLines(unittest.TestCase):
    def test_sin_machine_declaradas_no_hay_lineas(self):
        c = _config(robot_toml(nodos=[("motor", None, None), ("ia", None, None)]))
        self.assertEqual(placement_lines(c, {}), [])

    def test_linea_de_los_nodos_sin_machine(self):
        c = _config(robot_toml([("robot", "127.0.0.1")],
                               [("hub", None, None), ("motor", None, "robot"), ("cerebro", None, None)]))
        lineas = placement_lines(c, place_machines(c))
        self.assertEqual(lineas, ["robot (127.0.0.1): es esta computadora; sus nodos corren aquí (motor)",
                                  "sin machine, corren en esta computadora: hub, cerebro"])

    def test_no_hay_linea_si_todos_los_nodos_tienen_machine(self):
        c = _config(robot_toml([("robot", "127.0.0.1")], [("motor", None, "robot")]))
        self.assertEqual(placement_lines(c, place_machines(c)),
                         ["robot (127.0.0.1): es esta computadora; sus nodos corren aquí (motor)"])

    def test_con_local_no_sale_la_linea_de_sin_machine(self):
        c = _config(robot_toml([("robot", AJENA)], [("motor", "sim", "robot"), ("ia", "sim", None)],
                               "\n[profile.sim]\nlocal = true\n"), "sim")
        lineas = placement_lines(c, place_machines(c))
        self.assertEqual(lineas, ["local = true: todo corre en esta computadora (se ignora machine: motor)"])
        self.assertFalse(any("sin machine" in x for x in lineas))

    def test_con_local_y_sin_machine_declarada_no_hay_nada(self):
        c = _config(robot_toml(nodos=[("motor", "sim", None)], extra="\n[profile.sim]\nlocal = true\n"), "sim")
        self.assertEqual(placement_lines(c, {}), [])

    def test_puerto_distinto_y_ipv6_en_la_linea(self):
        c = _config(robot_toml([("robot", "127.0.0.1:7400"), ("v6", "[::1]:7400")],
                               [("a", None, "robot"), ("b", None, "v6")]))
        lineas = placement_lines(c, place_machines(c))
        self.assertIn("robot (127.0.0.1:7400): es esta computadora; sus nodos corren aquí (a)", lineas)
        self.assertIn("v6 ([::1]:7400): es esta computadora; sus nodos corren aquí (b)", lineas)


class TestLauncherYMaquinas(unittest.TestCase):
    """Launcher(config, stream=None, *, places=None) (I3)."""

    def cfg(self, maquinas, nodos, extra="", profile=None):
        return _config(robot_toml(maquinas, nodos, extra), profile)

    def nuevo(self, c, **kw):
        return Launcher(c, stream=io.StringIO(), **kw)

    def test_nodo_de_otra_computadora_es_configerror_con_el_mensaje_de_run(self):
        c = self.cfg([("robot", AJENA)], [("motor", None, "robot"), ("imu", None, "robot")])
        with self.assertRaises(ConfigError) as ctx:
            self.nuevo(c)           # places=None: lo decide él
        self.assertEqual(str(ctx.exception), remote_error(c, place_machines(c)))
        self.assertIn(f"'motor' e 'imu' corren en 'robot' ({AJENA})", str(ctx.exception))

    def test_places_dado_con_una_remota_tambien_rechaza_sin_resolver(self):
        c = self.cfg([("robot", "robot.local")], [("motor", None, "robot")])
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS")), \
                mock.patch("socket.socket", side_effect=AssertionError("socket")):
            with self.assertRaises(ConfigError):
                self.nuevo(c, places={"robot": Place(False, "other")})

    def test_antes_de_crear_nada(self):
        c = self.cfg([("robot", AJENA)], [("motor", None, "robot")])
        with mock.patch("agmen.run.LogBook", side_effect=AssertionError("ya creó el LogBook")), \
                mock.patch("agmen.run.Printer", side_effect=AssertionError("ya creó el Printer")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("ya lanzó un proceso")):
            with self.assertRaises(ConfigError):
                self.nuevo(c)

    def test_places_incompleto_es_valueerror(self):
        c = self.cfg([("robot", AJENA), ("laptop", "127.0.0.1")], [("motor", None, "robot"), ("ia", None, "laptop")])
        with self.assertRaises(ValueError) as ctx:
            self.nuevo(c, places={"laptop": Place(True, "loopback")})
        self.assertIn("robot", str(ctx.exception))
        with self.assertRaises(ValueError):
            self.nuevo(c, places={})

    def test_places_sobrante_no_estorba(self):
        c = self.cfg([("robot", "127.0.0.1")], [("motor", None, "robot")])
        lan = self.nuevo(c, places={"robot": Place(True, "loopback"), "otra": Place(False, "other")})
        self.assertEqual(lan.places["robot"], Place(True, "loopback"))

    def test_todo_aqui_se_construye_y_conserva_places(self):
        c = self.cfg([("robot", "127.0.0.1")], [("motor", None, "robot"), ("ia", None, None)])
        lan = self.nuevo(c)
        self.assertEqual(lan.places, {"robot": Place(True, "loopback")})

    def test_con_places_dado_no_vuelve_a_resolver(self):
        c = self.cfg([("robot", "robot.local")], [("motor", None, "robot")])
        with mock.patch("agmen.machines.place_machines", side_effect=AssertionError("resolvió de nuevo")), \
                mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS")), \
                mock.patch("socket.socket", side_effect=AssertionError("socket")):
            lan = self.nuevo(c, places={"robot": Place(True, "mine")})
        self.assertTrue(lan.places["robot"].here)

    def test_sin_machine_no_consulta_la_red_ni_el_dns(self):
        # como lo construye scripts/paralelo_check.py: load_config + Launcher(config, stream=...)
        c = self.cfg([], [("a", None, None), ("b", None, None)])
        with mock.patch("agmen.machines.place_machines", side_effect=AssertionError("consultó")), \
                mock.patch("agmen.machines.check_hosts", side_effect=AssertionError("consultó")), \
                mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS")), \
                mock.patch("socket.gethostbyname", side_effect=AssertionError("DNS")), \
                mock.patch("socket.socket", side_effect=AssertionError("socket")):
            lan = self.nuevo(c)
            lan2 = Launcher(c, io.StringIO())      # stream posicional, como paralelo_check
        self.assertEqual(lan.places, {})
        self.assertEqual(lan2.places, {})

    def test_places_es_solo_por_nombre(self):
        with self.assertRaises(TypeError):
            Launcher(self.cfg([], [("a", None, None)]), io.StringIO(), {})

    def test_con_local_no_consulta_y_construye(self):
        c = self.cfg([("robot", AJENA)], [("mundo", "sim", "robot")], "\n[profile.sim]\nlocal = true\n", "sim")
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS")), \
                mock.patch("socket.socket", side_effect=AssertionError("socket")):
            lan = self.nuevo(c)
        self.assertEqual(lan.places, {"robot": Place(True, "local")})

    def test_paralelo_check_usa_launcher_sin_machine(self):
        """scripts/paralelo_check.py construye Launcher(load_config(path), stream=...) con un robot.toml
        sin [[machine]]: no cambia de comportamiento (ni consulta la red)."""
        fuente = (ROOT / "scripts" / "paralelo_check.py").read_text(encoding="utf-8")
        self.assertIn("Launcher(load_config(path), stream=", fuente)
        self.assertNotIn("[[machine]]", fuente)
        self.assertNotIn("places", fuente)


class TestSplitHostBloque3a(unittest.TestCase):
    """Validación al leer (sin DNS): M1-M6."""

    def malo(self, texto, *frag):
        with self.assertRaises(ValueError) as ctx:
            split_host(texto)
        for f in frag:
            self.assertIn(f, str(ctx.exception))
        return str(ctx.exception)

    def test_puerto_solo_digitos_ascii(self):
        for texto in ("1.2.3.4:²", "1.2.3.4:７４００", "1.2.3.4:٧٤٠٠", "robot.local:¹", "[fe80::1]:７４００"):
            with self.subTest(texto=texto):
                self.malo(texto, "puerto", "1 y 65535")

    def test_ejemplo_del_error_de_puerto_ipv6_lleva_corchetes(self):
        self.malo("[::1]:abc", 'host = "[::1]:7391"')
        self.malo("[fe80::1]:0", 'host = "[fe80::1]:7391"')
        self.malo("1.2.3.4:abc", 'host = "1.2.3.4:7391"')

    def test_ipv6_con_zona(self):
        self.assertEqual(split_host("[fe80::1%eth0]:7400"), ("fe80::1%eth0", 7400))
        self.assertEqual(split_host("fe80::1%eth0"), ("fe80::1%eth0", DEFAULT_PORT))
        self.assertEqual(split_host("[fe80::1%eth0]"), ("fe80::1%eth0", DEFAULT_PORT))

    def test_ipv4_mal_escrita(self):
        for texto in ("999.1.1.1", "1.2.3", "1.2.3.4.5", "256.0.0.1", "1..2.3", "1.2.3.", "..", "300.300.300.300:7400"):
            with self.subTest(texto=texto):
                self.malo(texto, "IPv4 válida")

    def test_ipv4_valida_con_ceros_a_la_izquierda_no_revienta(self):
        try:
            split_host("010.1.1.1")
        except ValueError as e:
            self.assertIn("IPv4", str(e))      # válido o error claro; nunca otra excepción

    def test_nombres_invalidos(self):
        for texto in ("robot_!!", "a..b", ".robot", "-a.b", "a-.b", "robo t", "róbot.local", "robot$", "a.b!.c",
                      "ro*bot", "*.local"):
            with self.subTest(texto=texto):
                self.malo(texto)

    def test_nombres_validos(self):
        for texto in ("robot", "Robot.Local", "robot.local.", "mi_robot-2.lan", "a", "r2d2", "1robot", "robot-1.local",
                      "_dmarc.local", "x.y.z.w"):
            with self.subTest(texto=texto):
                self.assertEqual(split_host(texto)[1], DEFAULT_PORT)

    def test_etiquetas_de_1_a_63(self):
        self.assertEqual(split_host("a" * 63 + ".local")[0], "a" * 63 + ".local")
        self.malo("a" * 64 + ".local")
        self.malo("a" * 64)

    def test_nombre_de_253_como_maximo(self):
        ok = ".".join(["a" * 63] * 3 + ["a" * 61])
        self.assertEqual(len(ok), 253)
        self.assertEqual(split_host(ok)[0], ok)
        malo = ".".join(["a" * 63] * 3 + ["a" * 62])
        self.assertEqual(len(malo), 254)
        self.malo(malo)

    def test_direcciones_que_no_son_una_computadora(self):
        for texto in ("0.0.0.0", "0.0.0.0:7400", "224.0.0.1", "239.255.255.250", "255.255.255.255",
                      "::", "[::]:7391", "ff02::1", "[ff02::1]:7400"):
            with self.subTest(texto=texto):
                self.malo(texto, "no es la dirección de una computadora")

    def test_las_direcciones_normales_siguen_valiendo(self):
        for texto in ("127.0.0.1", "10.0.0.1", "192.168.255.254", "255.255.255.254", "223.255.255.255", "::1",
                      "2001:db8::1"):
            with self.subTest(texto=texto):
                split_host(texto)

    def test_sigue_sin_consultar_el_dns(self):
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS")), \
                mock.patch("socket.socket", side_effect=AssertionError("socket")):
            for texto in ("robot.local", "1.2.3.4", "[fe80::1%eth0]:7400", "no-existe.invalid"):
                split_host(texto)


class TestLoadConfigMachinesBloque3a(unittest.TestCase):
    def malo(self, text, *frag):
        with self.assertRaises(ConfigError) as ctx:
            _config(text)
        for f in frag:
            self.assertIn(f, str(ctx.exception))
        return str(ctx.exception)

    @staticmethod
    def m(name, host):
        return f'\n[[machine]]\nname = "{name}"\nhost = "{host}"\n'

    def test_host_invalido_dice_la_machine(self):
        for host in ("999.1.1.1", "robot_!!", "a..b", "0.0.0.0", "224.0.0.1", "255.255.255.255", "1.2.3.4:²",
                     "1.2.3.4:７４００", "::", "[ff02::1]"):
            with self.subTest(host=host):
                self.malo(self.m("robot", host), "[[machine]] 'robot'", "host")

    def test_puerto_con_digitos_no_ascii_dice_el_puerto(self):
        self.malo(self.m("robot", "1.2.3.4:²"), "puerto", "1 y 65535")
        self.malo(self.m("robot", "1.2.3.4:７４００"), "puerto", "1 y 65535")

    def test_hosts_con_zona_se_cargan(self):
        c = _config(self.m("a", "[fe80::1%eth0]:7400") + self.m("b", "fe80::2%eth0") + robot_toml(nodos=[("n", None, None)]).split("\n", 2)[2])
        self.assertEqual([(x.host, x.port) for x in c.machines], [("fe80::1%eth0", 7400), ("fe80::2%eth0", 7391)])

    def test_nombres_que_solo_cambian_en_mayusculas(self):
        e = self.malo(self.m("robot", "1.2.3.4") + self.m("Robot", "1.2.3.5"), "mayúsculas", "'robot'", "'Robot'")
        self.assertNotIn("Traceback", e)

    def test_nombres_exactos_repetidos_siguen_dando_el_error_de_unicos(self):
        self.malo(self.m("robot", "1.2.3.4") + self.m("robot", "1.2.3.5"), "'robot'", "únicos")

    def test_mismo_host_y_puerto_son_la_misma_computadora(self):
        for a, b in (("1.2.3.4", "1.2.3.4"), ("1.2.3.4", "1.2.3.4:7391"), ("Robot.local", "robot.local."),
                     ("ROBOT.LOCAL:7400", "robot.local:7400"), ("::1", "0:0:0:0:0:0:0:1"),
                     ("[fe80::1%eth0]:7400", "[FE80::1%eth0]:7400")):
            with self.subTest(a=a, b=b):
                self.malo(self.m("a", a) + self.m("b", b), "'a'", "'b'", "son la misma computadora")

    def test_mismo_host_con_otro_puerto_se_permite(self):
        c = _config(self.m("a", "1.2.3.4:7400") + self.m("b", "1.2.3.4:7401") + self.m("c", "robot.local")
                    + self.m("d", "robot.local:7392") + robot_toml(nodos=[("n", None, None)]).split("\n", 2)[2])
        self.assertEqual([m.port for m in c.machines], [7400, 7401, 7391, 7392])

    def test_hosts_distintos_se_permiten(self):
        c = _config(self.m("a", "1.2.3.4") + self.m("b", "1.2.3.5") + robot_toml(nodos=[("n", None, None)]).split("\n", 2)[2])
        self.assertEqual(len(c.machines), 2)


if __name__ == "__main__":
    unittest.main()

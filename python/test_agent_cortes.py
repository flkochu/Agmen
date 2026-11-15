"""Pruebas de la parte 3c, primera mitad (RFC 0011 §3.4, D6, D9): la gracia de 10 s del agente, reanudar una
sesión (`reanudar`) y la reconexión automática de `agmen run`.

Comprueban lo que dice la especificación (secciones 1, 2, 3 y 8 de especificacion_3c.md), no lo que hace el código.

Tres bloques:
  1. TestProtocolo...: un AgentServer real en un hilo y una laptop de mentira (Cli) que habla directo con él.
     Los tiempos de gracia se acortan parchando agent.GRACE donde lo que importa no es el reloj.
  2. TestCortesDeRed: agente y agmen run como procesos de verdad, con un intermediario TCP (Intermediario) en
     medio que congela y corta. Aquí van los tiempos REALES (6 s de silencio, 10 s de gracia).
  3. TestSinCambios: lo que la 3c NO cambia (la conexión inicial, la pérdida antes de arrancar los nodos).

Corre en Windows y en Linux: el intermediario es solo `socket` e hilos. Lo que usa /proc (hilos y descriptores) se
salta fuera de Linux, con el motivo escrito.
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import test_agent as ta   # solo se usan sus ayudantes y clases base (ninguna trae pruebas)
from agmen import agent
from agmen.agent import ChannelClosed, ChannelError, ChannelTimeout, HandshakeError, client_handshake, derive_key


def setUpModule():
    warnings.simplefilter("ignore", ResourceWarning)   # los nodos dejan tuberías sin cerrar (ruido, no un fallo)


LINUX = sys.platform.startswith("linux")
HEX32 = re.compile(r"[0-9a-f]{32}")

# Un nodo de prueba que numera sus líneas (una cada 0.2 s) para poder comprobar "una sola vez y en orden". Si el
# agente lo reiniciara, la cuenta volvería a 0 (y el pid cambiaría). Al aparecer marcas/rafaga_<nombre> escribe
# 800 líneas (RAF) lo más rápido que deja run.py sin que recorte: LOG_BURST = 400 de golpe y luego menos de
# LOG_RATE = 200 por segundo; así 4 nodos pasan de las 2000 líneas que guarda el agente para reanudar. Al acabar
# deja marcas/rafaga_fin_<nombre>.
NODO_CUENTA = '''\
import os, signal, sys, time
signal.signal(signal.SIGINT, signal.default_int_handler)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)
name, marks = sys.argv[1], sys.argv[2]
with open(os.path.join(marks, "arranque_" + name), "a") as f:
    f.write(str(os.getpid()) + "\\n")
print("HOLA", name, os.getpid(), flush=True)
rafaga = os.path.join(marks, "rafaga_" + name)
hecha = False
i = 0
fin = time.time() + 120          # nunca se queda para siempre si algo sale mal
try:
    while time.time() < fin:
        with open(os.path.join(marks, "hb_" + name), "w") as f:
            f.write(str(time.time()))
        if not hecha and os.path.exists(rafaga):
            hecha = True
            for j in range(800):
                print("RAF", name, j, flush=True)
                if j >= 400:
                    time.sleep(0.006)
            with open(os.path.join(marks, "rafaga_fin_" + name), "w") as f:
                f.write("x")
        print("N", name, i, flush=True)
        i += 1
        time.sleep(0.2)
except KeyboardInterrupt:
    pass
'''


# ===========================================================================
# El intermediario TCP de prueba
# ===========================================================================

class Intermediario:
    """Pasa bytes entre agmen run y el agente y puede congelar y cortar la red, sin nada de POSIX (solo sockets e
    hilos: corre igual en Windows).

    congelar(): deja de pasar bytes SIN cerrar nada (los que llegan se tiran, como una WiFi caída; tampoco se
      propaga el cierre de una punta), en los dos sentidos o en uno; y las conexiones nuevas se cierran en cuanto se
      aceptan (nadie contesta).
    cortar(): cierra todas las conexiones de golpe (reinicio de la WiFi) y las nuevas se cierran en cuanto se aceptan.
    levantar(): cierra lo que hubiera quedado congelado (sus bytes ya se perdieron: no se puede seguir) y vuelve a
      dejar pasar conexiones nuevas.
    ultimo_c2a / ultimo_a2c: la hora (monotonic) del último byte que SÍ se entregó de la laptop al agente / al revés."""

    def __init__(self, destino: int) -> None:
        self.destino = destino
        self.puerto = ta.free_tcp_port()
        self.caido = False
        self.congelado = {"c2a": False, "a2c": False}
        self.ultimo_c2a = 0.0
        self.ultimo_a2c = 0.0
        self.conexiones = 0
        self._pares: list[tuple[socket.socket, socket.socket]] = []
        self._lock = threading.Lock()
        self._cerrado = False
        self._escucha = socket.socket()
        self._escucha.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._escucha.bind(("127.0.0.1", self.puerto))
        self._escucha.listen(16)
        threading.Thread(target=self._aceptar, daemon=True, name="intermediario-acepta").start()

    def _aceptar(self) -> None:
        while not self._cerrado:
            try:
                c, _ = self._escucha.accept()
            except OSError:
                return
            if self.caido:
                self._cerrar(c)
                continue
            try:
                u = socket.create_connection(("127.0.0.1", self.destino), timeout=3)
                u.settimeout(None)
            except OSError:
                self._cerrar(c)
                continue
            with self._lock:
                self.conexiones += 1
                self._pares.append((c, u))
            threading.Thread(target=self._bombear, args=(c, u, "c2a"), daemon=True).start()
            threading.Thread(target=self._bombear, args=(u, c, "a2c"), daemon=True).start()

    @staticmethod
    def _cerrar(*socks: socket.socket) -> None:
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)    # despierta al hilo que esté leyendo (close solo no basta en Linux)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def _bombear(self, origen: socket.socket, destino: socket.socket, sentido: str) -> None:
        try:
            while True:
                datos = origen.recv(65536)
                if not datos:
                    if self.congelado[sentido]:
                        return                     # el cierre de una punta tampoco llega: la red está muda
                    break
                if self.congelado[sentido]:
                    continue                       # se pierde (la red no entrega), pero la conexión sigue "abierta"
                destino.sendall(datos)
                if sentido == "c2a":
                    self.ultimo_c2a = time.monotonic()
                else:
                    self.ultimo_a2c = time.monotonic()
        except OSError:
            if self.congelado[sentido]:
                return
        self._cerrar(origen, destino)

    def congelar(self, c2a: bool = True, a2c: bool = True) -> None:
        self.congelado["c2a"], self.congelado["a2c"] = c2a, a2c
        self.caido = True

    def _cerrar_todo(self) -> None:
        with self._lock:
            pares, self._pares = self._pares, []
        for c, u in pares:
            self._cerrar(c, u)

    def cortar(self) -> None:
        self.caido = True
        self._cerrar_todo()

    def levantar(self) -> None:
        self._cerrar_todo()
        self.congelado["c2a"] = self.congelado["a2c"] = False
        self.caido = False

    def close(self) -> None:
        self._cerrado = True
        self._cerrar(self._escucha)
        self._cerrar_todo()


class TestIntermediario(unittest.TestCase):
    """El propio intermediario: si él falla, las demás pruebas no prueban nada."""

    def setUp(self):
        self.eco = socket.socket()
        self.eco.bind(("127.0.0.1", 0))
        self.eco.listen(4)
        self.addCleanup(self.eco.close)
        self.conns: list[socket.socket] = []

        def servir():
            while True:
                try:
                    c, _ = self.eco.accept()
                except OSError:
                    return
                self.conns.append(c)
                threading.Thread(target=self._eco, args=(c,), daemon=True).start()

        threading.Thread(target=servir, daemon=True).start()
        self.addCleanup(lambda: [c.close() for c in self.conns])
        self.px = Intermediario(self.eco.getsockname()[1])
        self.addCleanup(self.px.close)

    @staticmethod
    def _eco(c):
        try:
            while True:
                d = c.recv(4096)
                if not d:
                    return
                c.sendall(d)
        except OSError:
            pass

    def conectar(self) -> socket.socket:
        s = socket.create_connection(("127.0.0.1", self.px.puerto), timeout=5)
        self.addCleanup(s.close)
        return s

    def test_pasa_bytes_congela_sin_cerrar_y_corta(self):
        s = self.conectar()
        s.sendall(b"hola")
        self.assertEqual(s.recv(10), b"hola")
        self.px.congelar()
        s.sendall(b"perdido")
        s.settimeout(0.7)
        with self.assertRaises(socket.timeout, msg="congelado debe dejar la conexión abierta y muda, no cerrada"):
            s.recv(10)
        s2 = self.conectar()           # una conexión nueva con la red caída se cierra sola
        s2.settimeout(5)
        self.assertEqual(s2.recv(10), b"")
        self.px.levantar()             # la congelada se cierra: sus bytes ya se perdieron
        s.settimeout(5)
        self.assertEqual(s.recv(10), b"")
        s3 = self.conectar()
        s3.sendall(b"otra vez")
        self.assertEqual(s3.recv(10), b"otra vez")
        self.px.cortar()
        s3.settimeout(5)
        self.assertEqual(s3.recv(10), b"")

    def test_congelado_el_cierre_de_una_punta_no_cruza(self):
        s = self.conectar()
        s.sendall(b"antes")
        self.assertEqual(s.recv(10), b"antes")
        self.px.congelar()
        for c in self.conns:          # el "agente" cierra su lado
            c.close()
        s.settimeout(0.7)
        with self.assertRaises(socket.timeout, msg="con la red muda el cierre del otro lado no se entera"):
            s.recv(10)

    def test_congelar_en_un_solo_sentido(self):
        s = self.conectar()
        s.sendall(b"antes")
        self.assertEqual(s.recv(10), b"antes")     # (ya aceptada y emparejada: congelar no la cierra)
        self.px.congelar(c2a=False, a2c=True)
        s.sendall(b"ida")                       # la ida pasa (el eco la contesta) pero la vuelta se pierde
        s.settimeout(0.7)
        with self.assertRaises(socket.timeout):
            s.recv(10)
        self.assertGreater(self.px.ultimo_c2a, 0.0)


# ===========================================================================
# 1. El protocolo, con una laptop de mentira hablando directo con un agente real
# ===========================================================================

class Dicho(list):
    """Lo que dijo el agente, (texto, tipo), con la hora (monotonic) de cada cosa: medir tiempos con lo que el
    agente DICE y no con "¿vive el nodo?", que en Windows solo ve un latido con una ventana de 3 s."""

    def __init__(self) -> None:
        super().__init__()
        self.horas: list[float] = []

    def append(self, item) -> None:
        self.horas.append(time.monotonic())
        super().append(item)

    def clear(self) -> None:
        super().clear()
        self.horas.clear()


class ProtocoloCase(ta.ServerCase):
    """Un agente real en un hilo, con nodos que numeran sus líneas, y ayudantes para perder y reanudar."""

    def setUp(self):
        super().setUp()
        self.said = Dicho()
        (self.dir / "nodo.py").write_text(NODO_CUENTA, encoding="utf-8")

    def parchea(self, modulo, nombre, valor):
        p = mock.patch.object(modulo, nombre, valor)
        p.start()
        self.addCleanup(p.stop)

    def abrir(self, ok_nodos=("motor", "imu")):
        """Agente + sesión abierta con los nodos corriendo. Devuelve (laptop, sesion_ok)."""
        self.start_server()
        cli, ok = self.sesion()
        self.assertEqual(ok["op"], "sesion_ok", ok)
        for n in ok_nodos:
            self.assertTrue(cli.wait(cli.hay("estado", nodo=n, estado="corriendo")), f"{n} no arrancó: {cli.msgs}")
        self.assertTrue(self.espera(lambda: all(self.vivo(n) for n in ok_nodos)))
        return cli, ok

    def perder(self, cli):
        """La conexión se cae sin despedirse (la laptop se va de la red)."""
        try:
            cli.ch.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        cli.close()

    def dijo(self, fragmento: str) -> bool:
        return any(fragmento in t for t, _ in self.said)

    def hora_dicho(self, fragmento: str) -> float | None:
        for h, (t, _) in zip(self.said.horas, self.said):
            if fragmento in t:
                return h
        return None

    def esperar_gracia(self):
        self.assertTrue(self.espera(lambda: self.dijo("dejó de contestar")), f"el agente no entró en gracia: {self.said}")

    def lista_dicha(self) -> str:
        return "\n".join(f"[{k}] {t}" for t, k in self.said)

    def reanudar(self, sid, logs=None, **extra):
        cli = self.conectar()
        msg = {"op": "reanudar", "sesion": sid, "logs": logs if logs is not None else {}}
        msg.update(extra)
        cli.send(msg)
        return cli

    def respuesta(self, cli, ops=("reanudar_ok", "sesion_error")) -> dict:
        self.assertTrue(cli.wait(lambda ms: any(m["op"] in ops for m in ms), 30), f"sin respuesta: {cli.msgs}")
        for m in cli.msgs:
            if m["op"] in ops:
                return m
        raise AssertionError("imposible")

    def ns(self, cli, nodo):
        return [m["n"] for m in cli.all("log", nodo=nodo)]

    def vivos(self, *nodos) -> bool:
        return all(self.vivo(n) for n in nodos)

    def todos_muertos(self, timeout: float = 30.0) -> bool:
        return self.espera(lambda: not self.vivo("motor") and not self.vivo("imu"), timeout)


class TestProtocoloGracia(ProtocoloCase):
    # --- sesion_ok
    def test_sesion_ok_trae_el_identificador_y_la_gracia(self):
        cli, ok = self.abrir()
        self.assertRegex(ok["sesion"], HEX32, "sesion: 32 caracteres hexadecimales (secrets.token_hex(16))")
        self.assertEqual(ok["gracia"], 10, "gracia: los segundos, para que el cliente diga la verdad")
        cli.send({"op": "adios"})
        self.assertTrue(self.espera(lambda: self.server.session is None))
        cli2, ok2 = self.sesion()
        self.assertRegex(ok2["sesion"], HEX32)
        self.assertNotEqual(ok["sesion"], ok2["sesion"], "el identificador es al azar: cada sesión el suyo")

    # --- la gracia: los nodos siguen
    def test_al_perderse_la_conexion_los_nodos_siguen_y_el_agente_lo_dice_una_vez(self):
        cli, ok = self.abrir()
        pids = {n: self.pids(n) for n in ("motor", "imu")}
        self.perder(cli)
        self.esperar_gracia()
        self.assertTrue(self.vivos("motor", "imu"), "en gracia los nodos siguen corriendo")
        self.assertEqual({n: self.pids(n) for n in ("motor", "imu")}, pids, "ningún nodo se reinicia")
        avisos = [t for t, _ in self.said if "dejó de contestar" in t]
        self.assertEqual(len(avisos), 1, f"el aviso va una sola vez: {avisos}")
        self.assertRegex(avisos[0], r"^la laptop \(127\.0\.0\.1\) dejó de contestar: sus nodos siguen \d+ s más por si "
                                    r"vuelve \(cerró la conexión sin despedirse\)$")
        n = int(re.search(r"siguen (\d+) s", avisos[0]).group(1))
        self.assertTrue(1 <= n <= 10, f"nunca promete más de 10 s: {avisos[0]}")
        self.assertFalse(self.dijo("detuve los nodos"), self.lista_dicha())

    def test_la_gracia_cuenta_desde_lo_ultimo_recibido_no_desde_el_silencio(self):
        """Sin laptop el tiempo total es GRACE (~10 s), no SILENCE_TIMEOUT + GRACE (16 s). Valores chicos: 1 s de
        silencio y 3 s de gracia: los nodos deben morir a ~3 s de lo último recibido, no a ~4."""
        self.parchea(agent, "SILENCE_TIMEOUT", 1.0)
        self.parchea(agent, "GRACE", 3.0)
        cli, ok = self.abrir()
        cli.send({"op": "ping", "t": 1})
        self.assertTrue(cli.wait(cli.hay("pong", t=1)))
        t0 = time.monotonic()          # lo último que el agente recibió (un instante antes que esto)
        self.assertTrue(self.espera(lambda: self.dijo("no dijo nada en 1 s"), 10), self.lista_dicha())
        t_silencio = time.monotonic() - t0
        self.assertTrue(self.espera(lambda: self.dijo("no volvió en 3 s"), 15), f"la gracia no se acabó\n{self.lista_dicha()}")
        # el tiempo se mide con lo que el agente DICE, no con "¿vive el nodo?": en Windows nodo_vivo solo ve un latido
        # con una ventana de 3 s y vería la muerte 3 s tarde (CI de Windows: 5.98 s)
        t_fin = self.hora_dicho("no volvió en 3 s") - t0
        self.assertTrue(self.espera(lambda: not self.vivo("motor"), 15), f"los nodos siguen vivos\n{self.lista_dicha()}")
        if os.name == "posix":   # aquí "murió" se ve al instante (el pid): también el tiempo real de la muerte
            self.assertLess(time.monotonic() - t0, 3.9, f"los nodos murieron tarde\n{self.lista_dicha()}")
        self.assertLess(t_silencio, 2.2, "el silencio se da por perdido a 1 s")
        self.assertGreater(t_fin, 2.5, "la gracia dura ~3 s desde lo último recibido")
        self.assertLess(t_fin, 3.9, f"3 s de gracia contados desde lo último recibido, no desde el silencio (+1 s): "
                                    f"{t_fin:.2f} s\n{self.lista_dicha()}")

    def test_fin_de_la_gracia_detiene_los_nodos_lo_dice_y_vuelve_a_esperar(self):
        self.parchea(agent, "GRACE", 1.5)
        cli, ok = self.abrir()
        sid = ok["sesion"]
        self.perder(cli)
        self.assertTrue(self.todos_muertos(), f"los nodos no se detuvieron al acabarse la gracia\n{self.lista_dicha()}")
        self.assertTrue(self.espera(lambda: self.dijo("no volvió en 1.5 s: detuve los nodos")), self.lista_dicha())
        self.assertTrue(any("(127.0.0.1)" in t and "no volvió" in t for t, _ in self.said), self.lista_dicha())
        self.assertTrue(self.espera(lambda: self.server.session is None), "la sesión sigue existiendo")
        self.assertTrue(self.espera(lambda: self.dijo("esperando") and self.dijo("(otra vez)")), self.lista_dicha())
        # la sesión ya no existe: reanudar -> sesion_error sesion; y una sesion nueva sí se acepta
        r = self.respuesta(self.reanudar(sid))
        self.assertEqual((r["op"], r.get("codigo")), ("sesion_error", "sesion"), r)
        cli2, ok2 = self.sesion()
        self.assertEqual(ok2["op"], "sesion_ok", ok2)

    # --- reanudar
    def test_reanudar_trae_estados_reales_y_los_log_con_n_mayor_que_k(self):
        cli, ok = self.abrir()
        sid = ok["sesion"]
        self.assertTrue(cli.wait(lambda ms: len([m for m in ms if m["op"] == "log" and m["nodo"] == "motor"]) >= 3))
        k_motor = max(self.ns(cli, "motor"))
        self.perder(cli)
        self.esperar_gracia()
        total = self.server.session._log_k    # cuántas líneas lleva cada nodo (para esperar a que haya habido corte)
        self.assertTrue(self.espera(lambda: total.get("motor", 0) >= k_motor + 4), f"el nodo no escribió: {total}")
        pid_motor, pid_imu = self.pids("motor")[-1], self.pids("imu")[-1]
        # un nodo muere durante el corte: el estado que se le diga a la laptop tiene que ser el REAL
        ta.matar(pid_imu)
        node_imu = self.server.session.launcher.nodes["imu"]
        self.assertTrue(self.espera(lambda: node_imu.state != "corriendo", 20), "el nodo imu no murió")
        cli2 = self.reanudar(sid, {"motor": k_motor})
        r = self.respuesta(cli2)
        self.assertEqual(r["op"], "reanudar_ok", r)
        self.assertEqual(cli2.msgs[0]["op"], "reanudar_ok", "reanudar_ok es lo primero que llega")
        self.assertEqual(r["gracia"], 10)
        self.assertEqual(r["nodos"]["motor"], {"estado": "corriendo", "pid": pid_motor, "codigo": None})
        self.assertNotEqual(r["nodos"]["imu"]["estado"], "corriendo", f"imu murió y reanudar_ok dice: {r['nodos']['imu']}")
        self.assertIsNone(r["nodos"]["imu"]["pid"])
        self.assertIsInstance(r["nodos"]["imu"]["codigo"], int)
        self.assertEqual(r["nodos"]["imu"]["estado"], node_imu.state)
        self.assertNotIn("perdidos", r, "no se perdió ninguna línea: 'perdidos' se omite")
        self.assertNotIn("ia", r["nodos"], "ia corre en la laptop")
        # los log: solo n > k, en orden y sin huecos (lo que no dijo, k = 0: desde la primera)
        self.assertTrue(cli2.wait(lambda ms: len([m for m in ms if m["op"] == "log" and m["nodo"] == "motor"]) >= 4))
        ns_motor = self.ns(cli2, "motor")
        self.assertEqual(ns_motor[0], k_motor + 1, f"el primer log recuperado de motor debe ser n=k+1: {ns_motor}")
        self.assertEqual(ns_motor, list(range(k_motor + 1, k_motor + 1 + len(ns_motor))), "en orden y sin huecos")
        for nodo in ("imu", "agmen"):
            ns_n = self.ns(cli2, nodo)
            if ns_n:
                self.assertEqual(ns_n[0], 1, f"sin k para '{nodo}' se manda desde n=1: {ns_n}")
                self.assertEqual(ns_n, list(range(1, 1 + len(ns_n))))
        # y sigue como una sesión normal
        cli2.send({"op": "ping", "t": 7})
        self.assertTrue(cli2.wait(cli2.hay("pong", t=7)), cli2.msgs)
        self.assertTrue(self.espera(lambda: self.server.session is not None and self.server.session.lost_at is None))
        self.assertEqual(self.pids("motor"), [pid_motor], "reanudar no reinicia nada")
        self.assertTrue(self.dijo("no se reinició nada"), self.lista_dicha())

    def test_se_puede_reanudar_varias_veces_con_el_mismo_identificador(self):
        cli, ok = self.abrir()
        sid = ok["sesion"]
        pid = self.pids("motor")
        actual = cli
        for vuelta in range(3):
            self.perder(actual)
            self.esperar_gracia()
            self.said.clear()
            actual = self.reanudar(sid)
            r = self.respuesta(actual)
            self.assertEqual(r["op"], "reanudar_ok", (vuelta, r, self.lista_dicha()))
            self.assertTrue(self.espera(lambda: self.server.session.lost_at is None))
        self.assertEqual(self.pids("motor"), pid)
        self.assertTrue(self.vivos("motor", "imu"))

    def test_reanudar_sustituye_a_la_conexion_que_aun_no_se_daba_por_perdida(self):
        """La laptop cambió de red y reconectó antes de que el agente notara que la vieja se perdió."""
        cli, ok = self.abrir()
        pid = self.pids("motor")
        nueva = self.reanudar(ok["sesion"])
        r = self.respuesta(nueva)
        self.assertEqual(r["op"], "reanudar_ok", f"{r}\n{self.lista_dicha()}")
        self.assertTrue(self.espera(lambda: cli.error is not None), "la conexión vieja debe cerrarse")
        self.assertEqual(self.pids("motor"), pid)
        self.assertTrue(self.vivos("motor", "imu"))
        nueva.send({"op": "ping", "t": 1})
        self.assertTrue(nueva.wait(nueva.hay("pong", t=1)))
        self.assertFalse(self.dijo("detuve"), self.lista_dicha())

    def test_reanudar_con_identificador_equivocado_es_sesion_error_sesion(self):
        cli, ok = self.abrir()
        self.perder(cli)
        self.esperar_gracia()
        for malo in ("0" * 32, ok["sesion"][:-1] + ("0" if ok["sesion"][-1] != "0" else "1"), "", "x" * 5000):
            with self.subTest(sesion=malo[:12]):
                r = self.respuesta(self.reanudar(malo))
                self.assertEqual((r["op"], r.get("codigo")), ("sesion_error", "sesion"), r)
                self.assertTrue(r.get("texto"), "un error siempre dice qué pasó")
        # (inferido de la seguridad, no escrito en la especificación) un identificador equivocado no mata la sesión
        self.assertTrue(self.vivos("motor", "imu"), f"un reanudar equivocado detuvo los nodos\n{self.lista_dicha()}")
        r = self.respuesta(self.reanudar(ok["sesion"]))
        self.assertEqual(r["op"], "reanudar_ok", f"tras los intentos malos, el bueno debe seguir sirviendo: {r}")

    def test_reanudar_sin_sesion_en_gracia(self):
        self.start_server()
        r = self.respuesta(self.reanudar("a" * 32))
        self.assertEqual((r["op"], r.get("codigo")), ("sesion_error", "sesion"), r)
        # con una sesión viva (con conexión) y un identificador que no es el suyo
        cli, ok = self.sesion()
        r = self.respuesta(self.reanudar("a" * 32))
        self.assertEqual((r["op"], r.get("codigo")), ("sesion_error", "sesion"), r)
        self.assertTrue(self.vivos("motor", "imu"), "un reanudar equivocado no toca a la sesión que sí tiene conexión")

    def test_sesion_de_otra_laptop_durante_la_gracia_es_ocupado_con_los_segundos(self):
        cli, ok = self.abrir()
        pid = self.pids("motor")
        self.perder(cli)
        self.esperar_gracia()
        otra = self.conectar()
        otra.send(self.mensaje())
        r = self.respuesta(otra)
        self.assertEqual((r["op"], r.get("codigo")), ("sesion_error", "ocupado"), r)
        texto = r["texto"]
        self.assertIn("127.0.0.1", texto, "dice de qué IP es la sesión que espera")
        segundos = [int(x) for x in re.findall(r"(\d+) s\b", texto)]
        self.assertTrue(any(1 <= s <= 10 for s in segundos), f"dice cuántos segundos le quedan (1 a 10): {texto!r}")
        self.assertTrue(self.vivos("motor", "imu"), "el rechazo no toca la sesión en gracia")
        self.assertEqual(self.pids("motor"), pid)
        # y la que esperaba sí puede volver
        r2 = self.respuesta(self.reanudar(ok["sesion"]))
        self.assertEqual(r2["op"], "reanudar_ok", r2)

    def test_reanudar_mal_formado_es_protocolo(self):
        cli, ok = self.abrir()
        sid = ok["sesion"]
        self.perder(cli)
        self.esperar_gracia()
        malos = {
            "sin sesion": {"op": "reanudar", "logs": {}},
            "sesion no es texto": {"op": "reanudar", "sesion": 123, "logs": {}},
            "sesion nula": {"op": "reanudar", "sesion": None, "logs": {}},
            "logs no es objeto": {"op": "reanudar", "sesion": sid, "logs": []},
            "logs con texto": {"op": "reanudar", "sesion": sid, "logs": {"motor": "3"}},
            "logs negativo": {"op": "reanudar", "sesion": sid, "logs": {"motor": -1}},
            "logs booleano": {"op": "reanudar", "sesion": sid, "logs": {"motor": True}},
            "logs decimal": {"op": "reanudar", "sesion": sid, "logs": {"motor": 1.5}},
        }
        for nombre, msg in malos.items():
            with self.subTest(caso=nombre):
                c = self.conectar()
                c.send(msg)
                r = self.respuesta(c)
                self.assertEqual((r["op"], r.get("codigo")), ("sesion_error", "protocolo"), (nombre, r))
        # (inferido, no escrito) los mal formados tampoco detienen la sesión que espera
        self.assertTrue(self.vivos("motor", "imu"), f"un reanudar mal formado detuvo los nodos\n{self.lista_dicha()}")
        r = self.respuesta(self.reanudar(sid))
        self.assertEqual(r["op"], "reanudar_ok", r)

    def test_sin_la_clave_no_se_llega_a_reanudar(self):
        cli, ok = self.abrir()
        sid = ok["sesion"]
        self.perder(cli)
        self.esperar_gracia()
        # (a) saludo con otra clave
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.peers.append(s)
        with self.assertRaises(HandshakeError):
            client_handshake(s, derive_key("zzzz-zzzz-zzzz"), timeout=15)
        # (b) sin saludo: directo el reanudar, crudo
        crudo = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.peers.append(crudo)
        crudo.settimeout(5)
        crudo.sendall(json.dumps({"op": "reanudar", "sesion": sid, "logs": {}}).encode() + b"\n")
        recibido = b""
        try:
            while True:
                d = crudo.recv(65536)
                if not d:
                    break
                recibido += d
        except (socket.timeout, OSError):
            pass
        self.assertNotIn(b"reanudar_ok", recibido, "sin saludo no se reanuda")
        self.assertNotIn(b"estado", recibido)
        self.assertTrue(self.vivos("motor", "imu"), "los intentos sin clave no deben tocar la sesión")
        # y con la clave buena sí
        r = self.respuesta(self.reanudar(sid))
        self.assertEqual(r["op"], "reanudar_ok", r)

    # --- sin gracia
    def test_mensaje_invalido_no_da_gracia(self):
        """Firma mala, número repetido: un error o un ataque, no una WiFi caída. Con GRACE=30 los nodos deben
        morir mucho antes de eso."""
        self.parchea(agent, "GRACE", 30.0)
        casos = {
            "firma mala": lambda c, n: f'{n} {{"op":"ping"}} {"0" * 64}\n'.encode(),
            "n repetido": lambda c, n: ta.ref_line(c.ch._key, "c", n - 1, '{"op":"ping"}'),
            "json malo": lambda c, n: ta.ref_line(c.ch._key, "c", n, "{no es json"),
        }
        for nombre, hacer in casos.items():
            with self.subTest(caso=nombre):
                self.said.clear()
                cli, ok = self.abrir() if self.server is None else self._otra_sesion()
                sid = ok["sesion"]
                cli.ch.sock.sendall(hacer(cli, cli.ch._sent + 1))
                t0 = time.monotonic()
                self.assertTrue(self.todos_muertos(15), f"{nombre}: los nodos siguen vivos (¿gracia?)\n{self.lista_dicha()}")
                self.assertLess(time.monotonic() - t0, 15)
                self.assertTrue(self.espera(lambda: self.dijo("mensaje inválido de la laptop")), self.lista_dicha())
                self.assertFalse(self.dijo("sus nodos siguen"), f"{nombre}: no debe haber gracia\n{self.lista_dicha()}")
                self.assertTrue(self.espera(lambda: self.server.session is None, 20), f"{nombre}: el agente sigue ocupado")
                r = self.respuesta(self.reanudar(sid))
                self.assertEqual((r["op"], r.get("codigo")), ("sesion_error", "sesion"), (nombre, r))

    def _otra_sesion(self):
        cli, ok = self.sesion()
        self.assertEqual(ok["op"], "sesion_ok", ok)
        self.assertTrue(cli.wait(cli.hay("estado", nodo="motor", estado="corriendo")))
        self.assertTrue(self.espera(lambda: self.vivos("motor", "imu")))
        return cli, ok

    def test_si_la_sesion_aun_no_esta_lista_perder_la_laptop_no_da_gracia(self):
        """El agente seguía preparando (corre su [robot] before) y la laptop se pierde: nadie tiene aún el
        identificador, así que no hay gracia (como en la 3b): se detiene lo que haya y se vuelve a esperar."""
        self.parchea(agent, "GRACE", 30.0)
        antes = json.dumps(["python", str(self.dir / "nodo.py"), "antes", str(self.marks)])
        self.start_server(write=False)
        self.write_toml(**{'[robot]\nname = "r"\n': f'[robot]\nname = "r"\nbefore = [{antes}]\n'})
        cli = self.conectar()
        cli.send(self.mensaje())
        self.assertTrue(self.espera(lambda: self.vivo("antes"), 30), "el [robot] before no arrancó")
        self.assertIsNone(cli.first("sesion_ok"), "la sesión no debía estar lista todavía")
        self.perder(cli)
        self.assertTrue(self.espera(lambda: self.dijo("cerró la conexión sin despedirse: detengo los nodos"), 15),
                        self.lista_dicha())
        self.assertTrue(self.espera(lambda: not self.vivo("antes"), 15), "el before debe detenerse")
        self.assertTrue(self.espera(lambda: self.server.session is None, 15), "el agente sigue ocupado")
        self.assertFalse(self.dijo("sus nodos siguen"), f"no hay gracia antes de estar lista\n{self.lista_dicha()}")
        self.assertFalse((self.marks / "arranque_motor").exists(), "arrancó un nodo")

    def test_adios_no_da_gracia(self):
        self.parchea(agent, "GRACE", 30.0)
        cli, ok = self.abrir()
        cli.send({"op": "adios"})
        t0 = time.monotonic()
        self.assertTrue(self.todos_muertos(15), f"adios: los nodos siguen vivos\n{self.lista_dicha()}")
        self.assertLess(time.monotonic() - t0, 15)
        self.assertFalse(self.dijo("sus nodos siguen"), self.lista_dicha())
        self.assertTrue(self.espera(lambda: self.server.session is None, 20))
        r = self.respuesta(self.reanudar(ok["sesion"]))
        self.assertEqual((r["op"], r.get("codigo")), ("sesion_error", "sesion"), r)

    def test_ctrl_c_en_el_agente_con_la_laptop_conectada_no_da_gracia(self):
        self.parchea(agent, "GRACE", 30.0)
        cli, ok = self.abrir()
        t0 = time.monotonic()
        self.server.close()
        self.assertTrue(self.todos_muertos(15), f"los nodos siguen vivos\n{self.lista_dicha()}")
        self.assertLess(time.monotonic() - t0, 15)
        self.assertFalse(self.dijo("sus nodos siguen"), self.lista_dicha())

    def test_ctrl_c_en_el_agente_durante_la_gracia_detiene_los_nodos(self):
        self.parchea(agent, "GRACE", 30.0)
        cli, ok = self.abrir()
        self.perder(cli)
        self.esperar_gracia()
        self.assertTrue(self.vivos("motor", "imu"))
        t0 = time.monotonic()
        self.server.close()        # lo que hace Ctrl+C en agmen agent
        self.assertTrue(self.todos_muertos(15), f"Ctrl+C durante la gracia no detuvo los nodos\n{self.lista_dicha()}")
        self.assertLess(time.monotonic() - t0, 15, "no debe esperar a que se acabe la gracia (30 s)")
        self.assertTrue(self.espera(lambda: self.server.session is None, 20))


class TestMemoriaAcotada(ProtocoloCase):
    """RESUME_LOG_LINES = 2000: lo que pase de ahí se pierde y reanudar_ok dice cuántas líneas (por nodo)."""

    TOML = ta.ServerCase.TOML.replace(
        '[[node]]\nname = "ia"',
        '[[node]]\nname = "x1"\ncmd = ["python", {nodo}, "x1", {marks}]\nmachine = "robot"\n\n'
        '[[node]]\nname = "x2"\ncmd = ["python", {nodo}, "x2", {marks}]\nmachine = "robot"\n\n'
        '[[node]]\nname = "ia"')

    def test_mas_de_2000_lineas_durante_el_corte_dan_perdidos_exactos(self):
        self.parchea(agent, "GRACE", 120.0)     # la ráfaga tarda unos segundos: aquí no se prueba el reloj de la gracia
        nodos = ("motor", "imu", "x1", "x2")
        cli, ok = self.abrir(nodos)
        sid = ok["sesion"]
        self.assertTrue(cli.wait(lambda ms: len(self.ns(cli, "motor")) >= 2))
        k = {"motor": max(self.ns(cli, "motor"))}      # (de los demás, nada: k = 0)
        self.perder(cli)
        self.esperar_gracia()
        for n in nodos:
            (self.marks / f"rafaga_{n}").write_text("x")
        self.assertTrue(self.espera(lambda: all((self.marks / f"rafaga_fin_{n}").exists() for n in nodos), 60),
                        "los nodos no terminaron la ráfaga")
        sesion = self.server.session

        def contadas() -> bool:   # el agente ya leyó (y guardó para reanudar) la última línea de cada ráfaga
            logs = sesion.launcher.logs
            with logs.lock:
                textos = {l["text"] for l in logs.lines}
            return all(f"RAF {n} 799" in textos for n in nodos) and sesion._log_idx == logs.next_idx

        self.assertTrue(self.espera(contadas, 30), f"el agente no contó las líneas: {sesion._log_k}")
        cli2 = self.reanudar(sid, k)
        r = self.respuesta(cli2)
        self.assertEqual(r["op"], "reanudar_ok", r)
        perdidos = r.get("perdidos", {})
        self.assertTrue(perdidos, f"pasaron 3200 líneas y solo caben 2000: debe decir cuántas perdió: {r}")
        self.assertGreaterEqual(sum(perdidos.values()), 1200, f"3200 líneas menos las 2000 que caben: {perdidos}")
        # los guardados llegan en orden de llegada: lo más reciente (RAF 799 de algún nodo) viene al final del reanudar
        self.assertTrue(cli2.wait(lambda ms: any(m["op"] == "log" and m["linea"] == f"RAF {m['nodo']} 799" for m in ms), 30),
                        "no llegaron los log guardados (o lo más reciente no se conservó)")
        self.assertTrue(cli2.wait(lambda ms: sum(1 for m in ms if m["op"] == "log") >= 1900), "faltan log guardados")
        replay = [m for m in cli2.msgs if m["op"] == "log"]
        # (hasta ~60 líneas pueden ser de lo que escriban los nodos después de reanudar: 4 nodos x 5 por segundo)
        self.assertLessEqual(len(replay), 2000 + 60, "RESUME_LOG_LINES = 2000: no se guardan más")
        comprobados = 0
        for n in nodos + ("agmen",):
            ns_n = self.ns(cli2, n)
            if not ns_n:
                continue
            comprobados += 1
            self.assertEqual(ns_n, list(range(ns_n[0], ns_n[0] + len(ns_n))), f"{n}: en orden y contiguas")
            esperado = ns_n[0] - 1 - k.get(n, 0)
            self.assertEqual(perdidos.get(n, 0), esperado,
                             f"{n}: perdidos debe ser las líneas con n > k que ya no están "
                             f"(k={k.get(n, 0)}, primera guardada n={ns_n[0]})\n{perdidos}")
        self.assertGreaterEqual(comprobados, 2)


# ===========================================================================
# 2. Agente y agmen run de verdad, con el intermediario en medio
# ===========================================================================

class Lineas(list):
    """Las líneas que sacó un proceso, con la hora (monotonic) a la que llegó cada una."""

    def __init__(self) -> None:
        super().__init__()
        self.horas: list[float] = []

    def hora_de(self, fragmento: str) -> float | None:
        for h, l in zip(self.horas, self):
            if fragmento in l:
                return h
        return None


class CortesBase(ta.E2EBase):
    """agente (proceso) <- intermediario <- agmen run (proceso). El agmen run habla con el puerto del intermediario."""

    web = False
    extra = ()      # nombres de nodos remotos de más (para las pruebas que necesitan muchas líneas de log)

    def setUp(self):
        super().setUp()
        (self.dir / "nodo.py").write_text(NODO_CUENTA, encoding="utf-8")
        if self.extra:
            nodo, marks = json.dumps(str(self.dir / "nodo.py")), json.dumps(str(self.marks))
            mas = "".join(f'\n[[node]]\nname = "{n}"\ncmd = ["python", {nodo}, "{n}", {marks}]\nmachine = "robot"\n'
                          for n in self.extra)
            self.toml.write_text(self.toml.read_text(encoding="utf-8") + mas, encoding="utf-8")
        self.px = Intermediario(self.port)
        self.addCleanup(self.px.close)
        self.web_port = ta.free_tcp_port()
        texto = self.toml.read_text(encoding="utf-8")
        texto = texto.replace(f"127.0.0.1:{self.port}", f"127.0.0.1:{self.px.puerto}")
        if self.web:
            texto = texto.replace("[web]\nenabled = false", f'[web]\nenabled = true\nhttp = "127.0.0.1:{self.web_port}"')
        self.toml_run = self.dir / "robot_run.toml"
        self.toml_run.write_text(texto, encoding="utf-8")

    def lanzar(self, args, env):
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        p = subprocess.Popen([sys.executable, "-m", "agmen", *args], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                             errors="replace", cwd=self.dir, **kw)
        lineas = Lineas()

        def leer():
            for l in iter(p.stdout.readline, ""):
                lineas.horas.append(time.monotonic())
                lineas.append(l)

        threading.Thread(target=leer, daemon=True).start()
        self.procs.append(p)
        return p, lineas

    def iniciar(self):
        """Agente y agmen run arrancados y con todo corriendo. Guarda self.pa/la/clave y self.pr/lr."""
        self.pa, self.la, self.clave = self.agente()
        args = ("--http", f"127.0.0.1:{self.web_port}") if self.web else ("--no-web",)
        self.pr, self.lr = self.correr(clave=self.clave, args=args, toml=self.toml_run)
        self.assertTrue(self.espera(lambda: any("todo arrancado" in l for l in self.lr) or self.pr.poll() is not None, 60),
                        self.diag("agmen run no terminó de arrancar"))
        self.assertIsNone(self.pr.poll(), self.diag("agmen run salió"))
        self.assertTrue(self.espera(lambda: all(self.vivo(n) for n in ("motor", "imu", "ia")), 30), self.diag("nodos"))
        self.pids0 = self.pids_de_marcas()
        for n in ("motor", "imu", "ia"):
            self.assertEqual(len(self.pids0[n]), 1)

    def latido(self, nodo: str) -> float:
        """La hora del último latido de un nodo de prueba (0 si nunca latió). Un nodo detenido deja de escribirlo:
        comparar este valor dice "nadie arrancó" sin la ventana de nodo_vivo (que en Windows no mira el pid)."""
        try:
            return (self.marks / f"hb_{nodo}").stat().st_mtime
        except OSError:
            return 0.0

    def diag(self, que: str = "") -> str:
        try:
            estado = self.estado_panel() if self.web and self.pr.poll() is None else "(sin panel)"
        except Exception as e:   # noqa: BLE001
            estado = f"(panel: {e!r})"
        return (f"{que}\n--- pids: {self.pids_de_marcas()}\n--- vivos: "
                f"{ {n: self.vivo(n) for n in ('motor', 'imu', 'ia')} }\n--- panel: {estado}\n"
                f"--- agmen agent (código {self.pa.poll()}):\n{self.texto(self.la)}"
                f"--- agmen run (código {self.pr.poll()}):\n{self.texto(self.lr)}")

    # --- el panel (para ver el estado de los nodos y mandar órdenes sin teclado)
    def estado_panel(self) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.web_port}/api/state", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def estados(self) -> dict:
        return {n["name"]: n["state"] for n in self.estado_panel()["nodes"]}

    def espera_estado(self, nodo: str, estado: str, timeout: float = 20.0) -> bool:
        def ok():
            try:
                return self.estados().get(nodo) == estado
            except (OSError, ValueError):
                return False
        return self.espera(ok, timeout)

    def orden(self, nodo: str, accion: str) -> int:
        req = urllib.request.Request(f"http://127.0.0.1:{self.web_port}/api/node/{nodo}/{accion}", data=b"{}",
                                     method="POST", headers={"Content-Type": "application/json", "X-Agmen": "1"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    # --- lo que imprimió agmen run
    def serie(self, nodo: str, prefijo: str = "N") -> list[int]:
        """Los números de las líneas '<prefijo> <nodo> <i>' que agmen run imprimió de ese nodo remoto, en orden."""
        rx = re.compile(rf"robot/{nodo}\s+│ {prefijo} {nodo} (\d+)\s*$")
        return [int(m.group(1)) for l in list(self.lr) if (m := rx.search(l))]

    def sin_huecos_ni_repetidas(self, nodo: str, minimo: int = 8) -> None:
        s = self.serie(nodo)
        self.assertGreaterEqual(len(s), minimo, self.diag(f"muy pocas líneas de {nodo}"))
        self.assertEqual(s, list(range(s[0], s[0] + len(s))),
                         self.diag(f"las líneas numeradas de {nodo} deben llegar una sola vez y en orden: {s}"))

    def cuenta(self, fragmento: str) -> int:
        return sum(1 for l in list(self.lr) if fragmento in l)

    def esperar_linea(self, fragmento: str, timeout: float = 30.0, lineas=None, que: str = "") -> bool:
        lineas = self.lr if lineas is None else lineas
        ok = self.espera(lambda: any(fragmento in l for l in list(lineas)), timeout)
        self.assertTrue(ok, self.diag(que or f"no apareció: {fragmento!r}"))
        return ok

    def detener_con_tiempo(self, p):
        t0 = time.monotonic()
        codigo = self.detener(p, 40)
        return codigo, time.monotonic() - t0

    def ctrl_c_a_run(self, timeout: float = 40):
        t0 = time.monotonic()
        codigo = self.detener(self.pr, timeout)
        return codigo, time.monotonic() - t0


class TestCortesDeRed(CortesBase):
    web = True

    # --- 2, 12: corte corto que vuelve; durante el corte los nodos remotos "sin conexión" y órdenes rechazadas
    def test_corte_corto_nada_se_reinicia_y_los_log_llegan_una_vez_y_en_orden(self):
        self.iniciar()
        self.assertTrue(self.espera(lambda: len(self.serie("motor")) >= 3 and len(self.serie("imu")) >= 3, 30), self.diag())
        t_corte = time.monotonic()
        self.px.cortar()
        self.esperar_linea("se perdió la conexión", 20, que="agmen run no notó el corte")
        self.esperar_linea("dejó de contestar", 20, self.la, "el agente no notó el corte")
        # el agente: gracia, nodos vivos
        self.assertTrue(any("sus nodos siguen" in l and "(cerró la conexión sin despedirse)" in l for l in self.la),
                        self.diag("el aviso del agente al entrar en gracia"))
        # (12) agmen run durante el corte: los nodos de esa computadora "sin conexión", los locales vivos
        self.assertTrue(self.espera_estado("motor", "sin conexión") and self.espera_estado("imu", "sin conexión"),
                        self.diag("los nodos remotos deben estar 'sin conexión' (no 'falló')"))
        est = self.estados()
        self.assertEqual(est["ia"], "corriendo", self.diag("el nodo local siguió"))
        self.assertTrue(self.vivo("ia"), self.diag("ia murió"))
        self.assertTrue(self.vivo("motor") and self.vivo("imu"), self.diag("los nodos del robot siguen en gracia"))
        # (12) una orden sobre un nodo sin conexión se rechaza con el texto de la sección 8
        self.assertEqual(self.orden("motor", "start"), 200)
        esperado = ("no hay conexión con robot: 'arrancar motor' no se hizo "
                    "(se perdió la conexión; reintento cada 1 s)")
        self.esperar_linea(esperado, 15, que="la orden sobre un nodo sin conexión no se rechazó con el texto de la spec")
        self.assertEqual(self.pids_de_marcas()["motor"], self.pids0["motor"], "la orden rechazada no arrancó nada")
        # el corte dura 2.5 s en total
        self.assertTrue(self.espera(lambda: time.monotonic() - t_corte >= 2.5, 5))
        self.px.levantar()
        self.esperar_linea("no se reinició nada", 30, que="agmen run no reconectó")
        # lo dicho
        texto = self.texto(self.lr)
        self.assertRegex(texto, rf"robot \(127\.0\.0\.1:{self.px.puerto}\): se perdió la conexión \(.*\)\. "
                                r"Sus nodos siguen ~\d+ s por si vuelve; reintento…")
        n = int(re.search(r"Sus nodos siguen ~(\d+) s", texto).group(1))
        self.assertTrue(1 <= n <= 10, f"nunca promete más de la gracia: ~{n} s")
        self.assertRegex(texto, rf"robot \(127\.0\.0\.1:{self.px.puerto}\): reconectado tras \d+ s sin conexión · no se reinició nada")
        self.assertNotIn("se perdieron", texto, "no se perdió ninguna línea de log")
        self.assertNotIn("ya había detenido", texto)
        self.assertEqual(self.cuenta("): se perdió la conexión ("), 1, "el aviso de pérdida va una sola vez")
        self.assertEqual(self.cuenta("ya pasaron"), 0, "en un corte corto no se dice que la gracia pasó")
        # (2) mismos PID: ningún nodo se reinició
        self.assertEqual(self.pids_de_marcas(), self.pids0, self.diag("los pids cambiaron: algo se reinició"))
        self.assertTrue(self.espera_estado("motor", "corriendo") and self.espera_estado("imu", "corriendo"), self.diag())
        # (2) los log del corte, una sola vez y en orden, y siguen llegando
        antes = len(self.serie("motor"))
        self.assertTrue(self.espera(lambda: len(self.serie("motor")) >= antes + 3, 15), self.diag("no siguen llegando log"))
        self.sin_huecos_ni_repetidas("motor")
        self.sin_huecos_ni_repetidas("imu")
        self.assertNotIn("Traceback", texto)
        for n in ("motor", "imu"):
            self.assertEqual(self.cuenta(f"HOLA {n} "), 1, f"el nodo {n} dijo HOLA más de una vez: se reinició")
        codigo, t = self.ctrl_c_a_run()
        self.assertEqual(codigo, 0, self.diag("Ctrl+C tras reconectar"))
        self.assertTrue(self.ninguno_vivo(("motor", "imu", "ia")), self.diag("quedaron nodos vivos"))
        self.assertNotIn(self.clave, self.texto(self.lr) + self.texto(self.la).replace(f"clave: {self.clave}", ""))

    def test_congelar_pierde_lo_que_iba_en_camino_y_al_volver_llega_todo_una_vez(self):
        """La red no corta: se queda muda 3 s (lo mandado se pierde) y luego se reinicia. Las líneas que el agente
        mandó durante el silencio nunca llegaron: tienen que venir en el reanudar, sin duplicar las que sí."""
        self.iniciar()
        self.assertTrue(self.espera(lambda: len(self.serie("motor")) >= 3, 30), self.diag())
        self.px.congelar(c2a=False, a2c=True)    # solo el agente -> laptop queda mudo; la laptop sigue mandando ping
        t0 = time.monotonic()
        self.assertTrue(self.espera(lambda: time.monotonic() - t0 >= 3.0, 5))
        vistas = self.serie("motor")[-1]
        self.px.levantar()                       # cierra lo congelado: los dos lados reconectan
        self.esperar_linea("no se reinició nada", 40, que="agmen run no reconectó tras el silencio")
        self.assertTrue(self.espera(lambda: self.serie("motor")[-1] >= vistas + 15 - 5, 20), self.diag())
        self.sin_huecos_ni_repetidas("motor")
        self.sin_huecos_ni_repetidas("imu")
        self.assertEqual(self.pids_de_marcas(), self.pids0, self.diag("los pids cambiaron"))

    # --- 3, 4: la laptop que no vuelve (tiempos reales) y la que vuelve tarde
    def test_laptop_que_no_vuelve_el_agente_detiene_a_los_10_s_y_si_vuelve_tarde_todo_detenido(self):
        self.iniciar()
        self.assertTrue(self.espera(lambda: len(self.serie("motor")) >= 3, 30), self.diag())
        self.px.congelar()                       # mudo en los dos sentidos, sin cerrar nada
        ultimo = self.px.ultimo_c2a              # lo último que el agente recibió de la laptop
        self.assertGreater(ultimo, 0.0)
        # el agente: a los 6 s de silencio dice que dejó de contestar, y los nodos siguen
        self.esperar_linea("dejó de contestar", 20, self.la, "el agente no entró en gracia")
        dijo = self.la.hora_de("dejó de contestar") - ultimo
        self.assertTrue(5.5 <= dijo <= 8.5, self.diag(f"el aviso de gracia llegó a {dijo:.1f} s de lo último recibido (6 s)"))
        linea = next(l for l in self.la if "dejó de contestar" in l)
        self.assertRegex(linea, r"la laptop \(127\.0\.0\.1\) dejó de contestar: sus nodos siguen \d+ s más por si vuelve "
                                r"\(no dijo nada en 6 s\)")
        self.assertTrue(self.vivo("motor") and self.vivo("imu"), self.diag("en gracia los nodos siguen"))
        # a los ~10 s (NO 16) desde lo último recibido, los detiene y lo dice. El tiempo se mide con lo que el agente
        # DICE (hora de su línea), no con "¿vive el nodo?": en Windows eso solo ve un latido con ventana y llega tarde
        t_muerte = [None]

        def murieron():
            if t_muerte[0] is None and not self.vivo("motor", 1.0) and not self.vivo("imu", 1.0):
                t_muerte[0] = time.monotonic()
            return t_muerte[0] is not None

        self.esperar_linea("la laptop (127.0.0.1) no volvió en 10 s: detuve los nodos", 25, self.la)
        dt = self.la.hora_de("no volvió en 10 s: detuve los nodos") - ultimo
        self.assertTrue(9.0 <= dt <= 12.5, self.diag(f"el agente dijo que detuvo a {dt:.1f} s de lo último recibido "
                                                     "(la gracia es 10 s desde lo último recibido; 16 s sería mal contado)"))
        self.assertTrue(self.espera(murieron, 25), self.diag("el agente no detuvo sus nodos"))
        # (la muerte vista: en POSIX al instante; en Windows el latido con ventana de 1 s agrega hasta ~1 s)
        self.assertLessEqual(t_muerte[0] - ultimo, 12.5 if os.name == "posix" else 14.0,
                             self.diag("los nodos murieron tarde"))
        self.esperar_linea("la laptop (127.0.0.1) no volvió en 10 s: detuve los nodos", 10, self.la)
        self.assertTrue(self.espera(lambda: sum("(otra vez)" in l for l in self.la) >= 1, 15), self.diag("no volvió a esperar"))
        self.assertIsNone(self.pa.poll(), "el agente debe seguir vivo, esperando")
        # agmen run: dijo la pérdida, y cuando pasó la gracia, lo dice una vez
        self.esperar_linea("robot: ya pasaron 10 s: robot habrá detenido sus nodos. Sigo intentando (Ctrl+C para salir)",
                           20, self.lr)
        self.assertEqual(self.cuenta("ya pasaron 10 s"), 1)
        self.assertEqual(self.cuenta("): se perdió la conexión ("), 1)
        perdio = next(l for l in self.lr if "): se perdió la conexión (" in l)
        self.assertIn("(dejó de contestar (6 s sin nada))", perdio)
        quedan = int(re.search(r"Sus nodos siguen ~(\d+) s por si vuelve", perdio).group(1))
        # la gracia que queda se cuenta desde lo último que agmen run RECIBIÓ (no desde lo que "mandó" al buffer con
        # la red caída): se enteró a los 6 s de silencio, así que quedan ~4 s, no ~9
        self.assertTrue(2 <= quedan <= 4, self.diag(f"agmen run prometió {quedan} s de gracia a los 6 s de silencio (~4)"))
        h_perdio, h_pasaron = self.lr.hora_de("): se perdió la conexión ("), self.lr.hora_de("ya pasaron 10 s")
        self.assertLess(abs((h_pasaron - h_perdio) - quedan), 1.8,
                        self.diag(f"'ya pasaron 10 s' debe salir cuando se acaba lo prometido ({quedan} s)"))
        # nunca una línea por intento: de agmen run, solo esas dos hasta aquí
        propias = [l for l in self.lr if re.match(r"agmen\s+│ robot", l) and "): conectado" not in l]
        self.assertEqual(len(propias), 2, self.diag(f"agmen run dijo más de la cuenta durante el corte: {propias}"))
        # (4) la laptop vuelve tarde
        self.assertTrue(self.espera_estado("motor", "sin conexión") and self.espera_estado("ia", "corriendo"), self.diag())
        self.px.levantar()
        self.esperar_linea("pero ya había detenido sus nodos. Arráncalos desde el panel (o reinicia agmen run)", 40,
                           que="agmen run no abrió una sesión nueva")
        self.assertRegex(self.texto(self.lr), rf"robot \(127\.0\.0\.1:{self.px.puerto}\): reconectado tras \d+ s sin conexión, pero ya "
                                              r"había detenido sus nodos")
        self.assertNotIn("no se reinició nada", self.texto(self.lr))
        self.assertTrue(self.espera_estado("motor", "detenido") and self.espera_estado("imu", "detenido"),
                        self.diag("los nodos del robot deben verse 'detenido'"))
        # ninguno arranca solo (un rato corto: lo que se vería si arrancaran de inmediato). NO con "¿vive?": en Windows
        # el latido viejo sigue "vivo" durante su ventana de 3 s (CI de Windows: "un nodo arrancó solo" con los
        # nodos detenidos). Se compara el latido: si nadie arranca, el archivo no vuelve a escribirse.
        self.assertTrue(self.ninguno_vivo(("motor", "imu"), 1.0), self.diag("los nodos del robot siguen vivos"))
        hb0 = {n: self.latido(n) for n in ("motor", "imu")}
        fin = time.monotonic() + 2.5
        while time.monotonic() < fin:
            self.assertEqual({n: self.latido(n) for n in ("motor", "imu")}, hb0, self.diag("un nodo arrancó solo"))
            time.sleep(0.1)
        self.assertEqual(self.pids_de_marcas(), self.pids0, self.diag("arrancó algún nodo del robot solo"))
        self.assertEqual(self.estados()["ia"], "corriendo")
        # y una orden `arrancar` sí lo arranca
        self.assertEqual(self.orden("motor", "start"), 200)
        self.assertTrue(self.espera(lambda: len(self.pids_de_marcas().get("motor", [])) == 2 and self.vivo("motor"), 20),
                        self.diag("la orden arrancar no arrancó motor"))
        self.assertTrue(self.espera_estado("motor", "corriendo"), self.diag())
        self.assertEqual(len(self.pids_de_marcas()["imu"]), 1, "imu no se pidió: no debe haber arrancado")
        self.assertEqual(self.latido("imu"), hb0["imu"], "imu no se pidió: no debe haber latido de nuevo")
        codigo, _ = self.ctrl_c_a_run()
        self.assertEqual(codigo, 0, self.diag())

    # --- 5, 9b: el agente se reinicia a medio corte (con Ctrl+C en el agente durante la gracia)
    def test_agente_que_se_reinicia_a_medio_corte_sesion_nueva_y_nodos_detenidos(self):
        self.iniciar()
        self.assertTrue(self.espera(lambda: len(self.serie("motor")) >= 2, 30), self.diag())
        self.px.cortar()
        self.esperar_linea("dejó de contestar", 20, self.la, "el agente no entró en gracia")
        self.assertTrue(self.vivo("motor") and self.vivo("imu"))
        # (9b) Ctrl+C en el agente DURANTE la gracia: detiene todo y sale (sin esperar a que se acabe)
        codigo, t = self.detener_con_tiempo(self.pa)
        self.assertEqual(codigo, 0, self.diag("el agente debe salir con 0 tras Ctrl+C en la gracia"))
        self.assertLess(t, 9.0, self.diag(f"el agente tardó {t:.1f} s en salir: ¿esperó a que se acabara la gracia?"))
        self.assertTrue(self.ninguno_vivo(("motor", "imu")), self.diag("Ctrl+C en el agente no detuvo los nodos"))
        self.assertTrue(self.vivo("ia"), "ia es de la laptop: no se toca")
        self.assertIn("listo. ¡Hasta luego!", self.texto(self.la))
        # el agente "se reinicia": otro proceso en el mismo puerto (misma clave: la guarda en su carpeta)
        self.pa, self.la, clave2 = self.agente()
        self.assertEqual(clave2, self.clave, "el agente reiniciado conserva su clave")
        self.px.levantar()
        self.esperar_linea("pero ya había detenido sus nodos", 40, que="agmen run no abrió sesión nueva con el agente nuevo")
        self.assertNotIn("no se reinició nada", self.texto(self.lr))
        self.assertTrue(self.espera_estado("motor", "detenido") and self.espera_estado("imu", "detenido"),
                        self.diag("con el agente nuevo los nodos deben verse 'detenido'"))
        self.assertTrue(self.espera(lambda: any("conectada" in l for l in self.la), 20), self.diag("el agente nuevo no vio la sesión"))
        time.sleep(1.5)     # (negativo) nada arranca solo
        self.assertEqual(self.pids_de_marcas(), self.pids0, self.diag("un nodo del robot arrancó solo"))
        self.assertFalse(self.vivo("motor") or self.vivo("imu"))
        self.assertEqual(self.estados()["ia"], "corriendo")
        codigo, _ = self.ctrl_c_a_run()
        self.assertEqual(codigo, 0, self.diag())

    # --- §3: cualquier otro rechazo al reconectar (aquí: el agente nuevo tiene otra clave)
    def test_rechazo_al_reconectar_se_dice_una_vez_sigue_intentando_y_no_pide_la_clave(self):
        self.iniciar()
        self.assertTrue(self.espera(lambda: len(self.serie("motor")) >= 2, 30), self.diag())
        self.px.cortar()
        self.esperar_linea("dejó de contestar", 20, self.la)
        self.assertEqual(self.detener(self.pa, 40), 0)
        # otro agente en el mismo puerto, con su propia carpeta de configuración: otra clave
        otro_cfg = str(self.dir / "cfg-otro-agente")
        self.pa, self.la, clave2 = self.agente(AGMEN_CONFIG_DIR=otro_cfg)
        self.assertNotEqual(clave2, self.clave)
        self.px.levantar()
        self.assertTrue(self.espera(lambda: any(l.startswith("✖") or "✖ " in l for l in self.lr), 30),
                        self.diag("agmen run no dijo que lo rechazaron"))
        rechazo = next(l for l in self.lr if "✖ " in l)
        self.assertIn("Sigo intentando cada 5 s (Ctrl+C para salir)", rechazo)
        self.assertNotIn(self.clave, rechazo)
        self.assertNotIn(clave2, rechazo)
        # (un intento cada 5 s: se deja pasar uno más; dicho una sola vez por código)
        intentos = self.px.conexiones
        self.assertTrue(self.espera(lambda: self.px.conexiones >= intentos + 1, 15), "agmen run dejó de reintentar")
        self.assertEqual(sum("✖ " in l for l in self.lr), 1, self.diag("el rechazo se dijo más de una vez"))
        self.assertIsNone(self.pr.poll(), self.diag("agmen run salió"))
        self.assertNotIn("clave de ", self.texto(self.lr).split("✖ ", 1)[0], "no debe pedir la clave a media corrida")
        codigo, _ = self.ctrl_c_a_run()
        self.assertEqual(codigo, 0, self.diag("Ctrl+C con el agente rechazando"))

    # --- 9a: Ctrl+C en la laptop durante un corte
    def test_ctrl_c_en_la_laptop_durante_el_corte_sale_rapido_con_0_y_dice_que_el_robot_no_se_entero(self):
        self.iniciar()
        self.assertTrue(self.espera(lambda: len(self.serie("motor")) >= 2, 30), self.diag())
        self.px.cortar()
        self.esperar_linea("se perdió la conexión", 20, que="agmen run no notó el corte")
        pid_agente = self.pa.pid
        codigo, t = self.ctrl_c_a_run()
        self.assertIsNotNone(codigo, self.diag("agmen run no salió tras Ctrl+C durante el corte"))
        self.assertEqual(codigo, 0, self.diag(f"código {codigo}"))
        self.assertLess(t, 4.0, self.diag(f"salir con una computadora sin conexión tardó {t:.1f} s (~1 s)"))
        texto = self.texto(self.lr)
        self.assertIn("robot no se enteró (sin conexión): detiene sus nodos solo, 10 s después de perder a la laptop", texto)
        self.assertIn("listo. ¡Hasta luego!", texto)
        self.assertNotIn("Traceback", texto)
        self.assertNotIn("KeyboardInterrupt", texto)
        self.assertTrue(self.ninguno_vivo(("ia",)), self.diag("el nodo local debe cerrarse"))
        # el robot no se enteró: sus nodos siguen (se detienen solos 10 s después de lo último recibido)
        self.assertTrue(self.vivo("motor") and self.vivo("imu"), self.diag("el agente no debe haberse enterado"))
        self.assertEqual(self.pa.pid, pid_agente)


class TestCortesSinPanel(CortesBase):
    web = False

    # --- 11: cortes repetidos, sin acumular hilos ni descriptores
    @unittest.skipUnless(LINUX, "cuenta hilos y descriptores con /proc (solo Linux); en Windows no hay un equivalente "
                                "en la biblioteca estándar")
    def test_cortes_repetidos_no_acumulan_hilos_ni_descriptores(self):
        self.iniciar()
        self.assertTrue(self.espera(lambda: len(self.serie("motor")) >= 2, 30), self.diag())

        def recursos(p):
            return len(os.listdir(f"/proc/{p.pid}/task")), len(os.listdir(f"/proc/{p.pid}/fd"))

        def un_corte(i):
            antes = self.cuenta("no se reinició nada")
            self.px.cortar()
            self.assertTrue(self.espera(lambda: self.px.conexiones >= 1 and self.cuenta("): se perdió la conexión (") >= i, 20),
                            self.diag(f"corte {i}: agmen run no lo notó"))
            self.px.levantar()
            self.assertTrue(self.espera(lambda: self.cuenta("no se reinició nada") > antes, 30),
                            self.diag(f"corte {i}: no reconectó"))

        un_corte(1)
        self.assertTrue(self.espera(lambda: self.estado_vivo_reanudado(), 10))
        base = {"agente": recursos(self.pa), "run": recursos(self.pr)}
        for i in (2, 3, 4):
            un_corte(i)
        self.assertEqual(self.pids_de_marcas(), self.pids0, self.diag("algún nodo se reinició en los cortes"))

        def estable():
            ahora = {"agente": recursos(self.pa), "run": recursos(self.pr)}
            return all(ahora[q][0] <= base[q][0] and ahora[q][1] <= base[q][1] for q in base)

        ok = self.espera(estable, 15)
        ahora = {"agente": recursos(self.pa), "run": recursos(self.pr)}
        self.assertTrue(ok, self.diag(f"tras 3 cortes más, (hilos, descriptores) = {ahora}; tras el primero: {base}"))
        self.assertEqual(self.cuenta("): se perdió la conexión ("), 4, "un aviso por corte, no uno por intento")
        # (los log recuperados salen unos instantes después del aviso de reconexión: se espera a que lleguen)
        self.assertTrue(self.espera(lambda: len(self.serie("motor")) >= 12, 20), self.diag("no llegaron los log"))
        self.sin_huecos_ni_repetidas("motor")

    def estado_vivo_reanudado(self) -> bool:
        return self.vivo("motor") and self.vivo("imu")


class TestMemoriaAcotadaDeRun(CortesBase):
    """Más de 2000 líneas durante el corte: agmen run dice cuántas se perdieron y recupera las últimas, una vez."""

    web = False
    extra = ("x1", "x2")

    def test_mas_de_2000_lineas_en_el_corte_agmen_run_dice_cuantas_se_perdieron(self):
        self.iniciar()
        nodos = ("motor", "imu", "x1", "x2")
        self.assertTrue(self.espera(lambda: all(len(self.serie(n)) >= 1 for n in ("motor", "imu")), 30), self.diag())
        self.px.cortar()
        self.esperar_linea("dejó de contestar", 20, self.la)
        for n in nodos:
            (self.marks / f"rafaga_{n}").write_text("x")
        self.assertTrue(self.espera(lambda: all((self.marks / f"rafaga_fin_{n}").exists() for n in nodos), 60),
                        self.diag("sin ráfaga"))
        self.px.levantar()
        self.esperar_linea("no se reinició nada", 40, que="no reconectó")
        linea = next(l for l in self.lr if "no se reinició nada" in l)
        m = re.search(r"· se perdieron (\d+) líneas de log", linea)
        self.assertIsNotNone(m, self.diag(f"agmen run no dijo cuántas líneas se perdieron: {linea!r}"))
        self.assertGreaterEqual(int(m.group(1)), 1000, f"3200 líneas menos las 2000 que caben: {linea!r}")
        self.assertTrue(self.espera(lambda: any(self.serie(n, "RAF") and self.serie(n, "RAF")[-1] == 799 for n in nodos), 30),
                        self.diag("lo más reciente de las ráfagas debe haberse recuperado"))
        for n in nodos:
            raf = self.serie(n, "RAF")
            # (la terminal descarta líneas si no da abasto con 2000 de golpe: "N líneas no cupieron en la terminal";
            # el contenido exacto de lo recuperado se comprueba en TestMemoriaAcotada. Aquí: en orden y una sola vez)
            self.assertEqual(raf, sorted(set(raf)), self.diag(f"{n}: lo recuperado va en orden y una vez"))
        self.assertEqual(self.pids_de_marcas().get("motor"), self.pids0["motor"], "nada se reinició")
        # lo que no se perdió no se duplica: ningún (nodo, número) se imprimió dos veces
        for n in nodos:
            for pref in ("N", "RAF"):
                s_n = self.serie(n, pref)
                self.assertEqual(len(s_n), len(set(s_n)), self.diag(f"{n} {pref}: líneas repetidas"))


# ===========================================================================
# 3. Lo que la 3c no cambia
# ===========================================================================

class TestSinCambios(ta.E2EBase):
    """La conexión inicial que falla y la pérdida antes de arrancar los nodos siguen como en la 3b."""

    def toml_un_robot(self, puerto: int, before: str = "") -> Path:
        nodo = json.dumps(str(self.dir / "nodo.py"))
        marks = json.dumps(str(self.marks))
        t = self.dir / "robot_uno.toml"
        t.write_text(f'[robot]\nname = "r"\n{before}\n[[machine]]\nname = "robot"\nhost = "127.0.0.1:{puerto}"\n\n'
                     f'[[node]]\nname = "motor"\ncmd = ["python", {nodo}, "motor", {marks}]\nmachine = "robot"\n\n'
                     f'[[node]]\nname = "ia"\ncmd = ["python", {nodo}, "ia", {marks}]\n\n'
                     f'[hub]\nenabled = false\n[web]\nenabled = false\n', encoding="utf-8")
        return t

    def setUp(self):
        super().setUp()
        (self.dir / "nodo.py").write_text(NODO_CUENTA, encoding="utf-8")

    def test_la_conexion_inicial_que_falla_no_arranca_nada(self):
        t = self.toml_un_robot(ta.free_tcp_port())          # nadie escucha ahí
        pr, lr = self.correr(clave=ta.KEY, toml=t)
        codigo = self.fin(pr, 40)
        texto = self.texto(lr)
        self.assertIsNotNone(codigo, f"agmen run no salió\n{texto}")
        self.assertNotEqual(codigo, 0, texto)
        self.assertNotEqual(codigo, 130, texto)
        self.assertIn("no contesta", texto)
        self.assertNotIn("Traceback", texto)
        self.assertNotIn("reintento", texto, "la conexión inicial no se reintenta")
        self.assertFalse(list(self.marks.glob("arranque_*")), f"arrancó algo: {self.pids_de_marcas()}\n{texto}")

    def test_una_perdida_antes_de_arrancar_los_nodos_sale_con_1_sin_arrancar_nada(self):
        """El agente contesta sesion_ok y se cae mientras corre el [robot] before local (todavía no arrancó ningún
        nodo): como en la 3b, no arranca nada y sale con 1."""
        (self.dir / "antes.py").write_text(
            "import os, sys, time\n"
            f"marks = {str(self.marks)!r}\n"
            "open(os.path.join(marks, 'before_corrio'), 'w').write('x')\n"
            "fin = time.time() + 30\n"
            "while not os.path.exists(os.path.join(marks, 'perdido')) and time.time() < fin:\n"
            "    time.sleep(0.05)\n"
            "time.sleep(1.5)\n", encoding="utf-8")
        marks = self.marks

        def script(ch, ses):
            ch.send({"op": "sesion_ok", "maquina": "robot", "agmen": ta.__version__, "nodos": list(ses["nodos"])})
            time.sleep(0.5)
            ch.close()
            (marks / "perdido").write_text("x")

        falso = ta.FakeAgent(ta.agente_que_saluda_bien(script))
        self.addCleanup(falso.close)
        antes = json.dumps("python " + str(self.dir / "antes.py"))
        t = self.toml_un_robot(falso.port, before=f"before = [{antes}]")
        pr, lr = self.correr(clave=ta.KEY, toml=t)
        codigo = self.fin(pr, 60)
        texto = self.texto(lr)
        self.assertIsNotNone(codigo, f"agmen run no salió\n{texto}")
        self.assertTrue((self.marks / "before_corrio").exists(), f"la prueba no llegó al before\n{texto}")
        self.assertEqual(codigo, 1, f"una pérdida antes de arrancar los nodos sale con 1\n{texto}")
        self.assertFalse(list(self.marks.glob("arranque_*")), f"arrancó un nodo: {self.pids_de_marcas()}\n{texto}")
        self.assertNotIn("todo arrancado", texto)
        self.assertNotIn("reintento", texto, "antes de arrancar los nodos no se reconecta")
        self.assertNotIn("Traceback", texto)


if __name__ == "__main__":
    unittest.main()

"""La prueba de red ("sonda", el firewall) entre agmen run y agmen agent (RFC 0011 §3.5, §6 reto 2; bloque 4c §1).

    python -m unittest discover -s python -p "test_sonda.py"

Con el agente de verdad en este proceso (como test_agent.py) y, donde hace falta, un agente de mentira. Un lado que
"no recibe" se simula haciendo que el otro mande a un puerto equivocado (nadie escucha ahí) o mande un dato que no
es. Esperas con tope; puertos elegidos por el sistema."""

from __future__ import annotations

import io
import os
import signal
import socket
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import test_agent as TA
from agmen import agent, cli, sonda
from agmen.agent import agent_handshake
from agmen.run import load_config

TOPE_S = 15.0


def until(cond, tope=TOPE_S, every=0.02) -> bool:
    end = time.monotonic() + tope
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(every)
    return cond()


def hay_ip(ip: str) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def puerto_cerrado() -> int:
    """Un puerto donde (ya) no escucha nadie, ni UDP ni TCP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Lados:
    """Anota cada sonda.Lado que se abre (de los dos lados), para ver que todos se cierran."""

    def __init__(self, case):
        self.todos: list[sonda.Lado] = []
        orig = sonda.Lado.__init__
        anota = self

        def init(self, *a, **kw):
            orig(self, *a, **kw)
            anota.todos.append(self)

        p = mock.patch.object(sonda.Lado, "__init__", init)
        p.start()
        case.addCleanup(p.stop)

    def cerrados(self) -> bool:
        return all(l.udp.fileno() == -1 and l.tcp.fileno() == -1 for l in self.todos)


def mal_manda(case, lado: str, que: str):
    """Hace que `lado` ("robot": el agente; "laptop": agmen run) mande `que` ("udp" o "tcp") a un puerto equivocado."""
    orig = sonda.Lado.send

    def send(self, peer, udp, tcp, dato):
        es_robot = threading.current_thread().name in ("agmen-agent-sonda", "falso-sonda")
        if es_robot == (lado == "robot"):
            if que == "udp":
                udp = puerto_cerrado()
            else:
                tcp = puerto_cerrado()
        return orig(self, peer, udp, tcp, dato)

    p = mock.patch.object(sonda.Lado, "send", send)
    p.start()
    case.addCleanup(p.stop)


class TestLado(unittest.TestCase):
    """El módulo que comparten los dos lados: abrir, mandar, esperar, validar, explicar."""

    def test_pasa_entre_dos_lados(self):
        with sonda.Lado() as a, sonda.Lado() as b:
            a.listen("127.0.0.1")
            b.listen("127.0.0.1")
            oa, ob = a.offer(), b.offer()
            b.send("127.0.0.1", oa["udp"], oa["tcp"], oa["dato"])
            a.send("127.0.0.1", ob["udp"], ob["tcp"], ob["dato"])
            self.assertTrue(a.wait(time.monotonic() + TOPE_S))
            self.assertTrue(b.wait(time.monotonic() + TOPE_S))
        self.assertEqual((a.udp.fileno(), a.tcp.fileno(), b.udp.fileno(), b.tcp.fileno()), (-1, -1, -1, -1))

    def test_dato_equivocado_no_cuenta(self):
        with sonda.Lado() as a, sonda.Lado() as b:
            a.listen("127.0.0.1")
            o = a.offer()
            b.send("127.0.0.1", o["udp"], o["tcp"], sonda.nuevo_dato())   # otro dato
            self.assertFalse(a.wait(time.monotonic() + 0.5))
            self.assertFalse(a.got_udp.is_set() or a.got_tcp.is_set())
            b.send("127.0.0.1", o["udp"], o["tcp"], o["dato"])            # el bueno, sí
            self.assertTrue(a.wait(time.monotonic() + TOPE_S))

    @unittest.skipUnless(hay_ip("127.0.0.2"), "hace falta 127.0.0.2 (en Linux siempre está)")
    def test_dato_de_otra_ip_no_cuenta(self):
        with sonda.Lado() as a, sonda.Lado("127.0.0.1") as de_otra, sonda.Lado("127.0.0.2") as del_otro:
            a.listen("127.0.0.2")   # el otro lado es 127.0.0.2
            o = a.offer()
            de_otra.send("127.0.0.1", o["udp"], o["tcp"], o["dato"])   # el dato bueno, desde 127.0.0.1
            self.assertFalse(a.wait(time.monotonic() + 0.5))
            self.assertFalse(a.got_udp.is_set() or a.got_tcp.is_set())
            del_otro.send("127.0.0.1", o["udp"], o["tcp"], o["dato"])
            self.assertTrue(a.wait(time.monotonic() + TOPE_S))

    def test_se_cierra_y_nada_queda_escuchando(self):
        a = sonda.Lado()
        a.listen("127.0.0.1")
        port = a.offer()["tcp"]
        a.close()
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()

    def test_si_no_puede_abrir_es_un_oserror_que_lo_dice(self):
        with self.assertRaises(sonda.SinSockets) as ctx:
            sonda.Lado("203.0.113.7")   # una IP que no es de esta computadora
        self.assertIsInstance(ctx.exception, OSError)
        self.assertIn("no pude abrir mis sockets en 203.0.113.7 (", str(ctx.exception))
        self.assertEqual(ctx.exception.ip, "203.0.113.7")
        self.assertTrue(ctx.exception.detalle)

    def test_mensajes_mal_formados(self):
        bueno = {"udp": 4000, "tcp": 4001, "dato": "a" * 32}
        self.assertEqual(sonda.oferta(bueno), (4000, 4001, "a" * 32))
        for malo in ({**bueno, "udp": 0}, {**bueno, "tcp": 65536}, {**bueno, "udp": True}, {**bueno, "tcp": "4001"},
                     {**bueno, "dato": "a" * 31}, {**bueno, "dato": "z" * 32}, {**bueno, "dato": None},
                     {"tcp": 4001, "dato": "a" * 32}):
            with self.assertRaises(ValueError, msg=malo):
                sonda.oferta(malo)
        self.assertEqual(sonda.fin({"udp": True, "tcp": False}), (True, False))
        for malo in ({"udp": 1, "tcp": True}, {"udp": True}, {"udp": "true", "tcp": True},
                     {"udp": False, "tcp": False, "error": ""}, {"udp": False, "tcp": False, "error": 3}):
            with self.assertRaises(ValueError, msg=malo):
                sonda.fin(malo)
        # §4.2: el "error" de sonda_fin, campo propio
        self.assertEqual(sonda.motivo({"udp": True, "tcp": True}), "")
        self.assertEqual(sonda.motivo({"udp": False, "tcp": False, "error": "no pudo"}), "no pudo")
        for malo in ({"error": ""}, {"error": "  "}, {"error": None}, {"error": ["x"]}):
            with self.assertRaises(ValueError, msg=malo):
                sonda.motivo(malo)
        with self.assertRaises(ValueError):
            sonda.ipv4("fe80::1")

    def test_explicar_dice_el_lado_por_donde_y_que_hacer(self):
        R = sonda.Resultado
        with mock.patch.object(sonda, "sistema", lambda: "linux"):
            self.assertEqual(sonda.explicar(R(True, True, False, True, "windows"), "robot", "10.0.0.2", "10.0.0.1"),
                             ["robot no recibe de esta computadora por UDP (mensajes chicos): ¿el firewall de Windows "
                              "bloquea a Python? Permítelo en redes privadas (Firewall de Windows → Permitir una "
                              "aplicación → Python, casilla Privada)"])
            self.assertEqual(sonda.explicar(R(True, False, True, True, "linux"), "robot", "10.0.0.2", "10.0.0.1"),
                             ["esta computadora no recibe de robot por TCP (mensajes grandes): ¿un firewall (ufw) "
                              "bloquea la entrada? En esta computadora: sudo ufw allow from 10.0.0.2"])
            dos = sonda.explicar(R(False, False, True, False, "darwin"), "robot", "10.0.0.2", "10.0.0.1")
        self.assertEqual(len(dos), 2)
        self.assertTrue(dos[0].startswith("robot no recibe de esta computadora por TCP (mensajes grandes): ¿el "
                                          "firewall de macOS"), dos)
        self.assertTrue(dos[1].startswith("esta computadora no recibe de robot ni por UDP (mensajes chicos) ni por TCP "
                                          "(mensajes grandes)"), dos)
        self.assertIn("En robot: sudo ufw allow from 10.0.0.1",
                      sonda.explicar(R(True, True, True, False, "linux"), "robot", "10.0.0.2", "10.0.0.1")[0])
        # §4.5: si esta computadora no pudo mandar, el porqué va en la línea de ese sentido (y solo en esa)
        con = sonda.explicar(R(True, True, False, True, "linux", envio={"UDP": "No route to host", "TCP": "x"}),
                             "robot", "10.0.0.2", "10.0.0.1")
        self.assertEqual(len(con), 1)
        self.assertTrue(con[0].startswith("robot no recibe de esta computadora por UDP (mensajes chicos) (esta "
                                          "computadora no pudo mandarle: UDP: No route to host): "), con)
        self.assertNotIn("TCP: x", con[0])
        self.assertFalse(R(True, True, True, True).ok is False)
        self.assertFalse(R(True, True, True, True, error="x").ok)
        self.assertFalse(R(True, True, None, None).ok)


class SondaCase(TA.LinkCase):
    """El agente de verdad, con hub (hace falta para el puente: sin puente no hay sonda)."""

    def setUp(self):
        super().setUp()
        self.start_server(write=False)
        self.write_toml(**{"[hub]\nenabled = false": "[hub]\nenabled = true"})
        self.notas: list[str] = []

    def abrir(self, sonda_tambien: bool = True):
        cfg, m = self.machine()
        link = agent.AgentLink(m, toml_hint=str(self.toml))
        link.attach(None, lambda name, text, kind, droppable=False: self.notas.append(text))
        self.addCleanup(link.close)
        link.open(TA.KEY, agent.session_message(cfg, "robot", "real", "laptop", sonda=sonda_tambien))
        return link


class TestConElAgente(SondaCase):
    def test_pasa_en_los_cuatro(self):
        lados = Lados(self)
        link = self.abrir()
        self.assertTrue(link._probe_cap)
        res = link.net_check()
        self.assertIsNotNone(res)
        self.assertTrue(res.ok, (res, self.said))
        self.assertEqual(res.so_alla, sonda.sistema())
        self.assertEqual(len(lados.todos), 2)
        self.assertTrue(lados.cerrados())
        self.assertIsNotNone(link.channel)   # la conexión sigue

    def falla(self, lado: str, que: str):
        Lados(self)
        mal_manda(self, lado, que)
        link = self.abrir()
        with mock.patch.object(sonda, "SONDA_S", 1.0):
            res = link.net_check()
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "")
        return res, link.probe_lines(res)

    def test_el_robot_no_recibe_udp(self):
        res, lines = self.falla("laptop", "udp")
        self.assertEqual((res.aqui_udp, res.aqui_tcp, res.alla_udp, res.alla_tcp), (True, True, False, True))
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("robot no recibe de esta computadora por UDP (mensajes chicos): "), lines)

    def test_el_robot_no_recibe_tcp(self):
        res, lines = self.falla("laptop", "tcp")
        self.assertEqual((res.aqui_udp, res.aqui_tcp, res.alla_udp, res.alla_tcp), (True, True, True, False))
        # (a un puerto cerrado, así que esta computadora además ve "Connection refused": §4.5, va en la línea)
        self.assertTrue(lines[0].startswith("robot no recibe de esta computadora por TCP (mensajes grandes)"), lines)
        self.assertIn("(esta computadora no pudo mandarle: TCP: ", lines[0])

    def test_la_laptop_no_recibe_udp(self):
        res, lines = self.falla("robot", "udp")
        self.assertEqual((res.aqui_udp, res.aqui_tcp, res.alla_udp, res.alla_tcp), (False, True, True, True))
        self.assertTrue(lines[0].startswith("esta computadora no recibe de robot por UDP (mensajes chicos): "), lines)

    def test_la_laptop_no_recibe_tcp(self):
        res, lines = self.falla("robot", "tcp")
        self.assertEqual((res.aqui_udp, res.aqui_tcp, res.alla_udp, res.alla_tcp), (True, False, True, True))
        self.assertTrue(lines[0].startswith("esta computadora no recibe de robot por TCP (mensajes grandes): "), lines)
        if sonda.sistema() == "linux":
            self.assertIn("En esta computadora: sudo ufw allow from 127.0.0.1", lines[0])

    def test_si_el_agente_no_puede_abrir_sus_sockets_falla_ya_con_ese_motivo(self):
        """§1.6/§4.2 con el agente de verdad: contesta sonda_fin con "error" y la laptop no espera SONDA_S + MARGEN_S;
        §4.5: una frase, sin repetirse, con qué hacer."""
        orig = sonda.Lado.__init__

        def init(self_, *a, **kw):
            if threading.current_thread().name == "agmen-agent-sonda":
                raise sonda.SinSockets("0.0.0.0", "prueba")
            orig(self_, *a, **kw)

        link = self.abrir()
        t0 = time.monotonic()
        with mock.patch.object(sonda.Lado, "__init__", init):
            res = link.net_check()
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertFalse(res.ok)
        self.assertRegex(res.error, r"^robot \(127\.0\.0\.1[^)]*\) no pudo hacer la prueba de red: no pudo abrir sus "
                                    r"sockets en 0\.0\.0\.0 \(prueba\)\. Revisa AGMEN_NET_IP y que la red de robot esté "
                                    r"arriba$")
        self.assertEqual(res.error.count("prueba de red"), 1)
        self.assertEqual(link.probe_lines(res), [res.error])
        self.assertIsNotNone(link.channel)
        self.assertTrue(link.net_check().ok)   # la siguiente no arrastra el error de antes

    def test_el_lado_que_no_pudo_mandar_lo_dice(self):
        """§4.5: la laptop no pudo mandar UDP (p. ej. "No route to host"): el motivo va en la línea de ese sentido;
        y el agente que no pudo mandar lo dice en su terminal."""
        def send_udp(self_, peer, port, data):   # ninguno de los dos lados logra mandar UDP
            self_.send_errors.setdefault("UDP", "No route to host")

        with mock.patch.object(sonda.Lado, "_send_udp", send_udp), mock.patch.object(sonda, "SONDA_S", 1.0):
            link = self.abrir()
            res = link.net_check()
        self.assertEqual((res.aqui_udp, res.alla_udp), (False, False), res)
        lines = link.probe_lines(res)
        self.assertTrue(lines[0].startswith("robot no recibe de esta computadora por UDP (mensajes chicos) (esta "
                                            "computadora no pudo mandarle: UDP: No route to host): "), lines)
        self.assertTrue(lines[1].startswith("esta computadora no recibe de robot por UDP (mensajes chicos): "), lines)
        self.assertTrue(until(lambda: any("no pude mandarle a la laptop (127.0.0.1): UDP: No route to host" in t
                                          for t, _ in self.said)), self.said)

    def test_sin_sonda_de_la_laptop_no_hay_prueba_y_el_agente_no_la_dice(self):
        link = self.abrir(sonda_tambien=False)
        self.assertTrue(link._tables)
        self.assertFalse(link._probe_cap)
        self.assertIsNone(link.net_check())
        self.assertFalse([n for n in self.notas if "red" in n], self.notas)   # (el de --no-net-check: agmen run)


class TestAgenteCrudo(TA.ServerCase):
    """El agente de verdad, hablándole directo (una laptop de mentira)."""

    def setUp(self):
        super().setUp()
        self.start_server(write=False)
        self.write_toml(**{"[hub]\nenabled = false": "[hub]\nenabled = true"})

    def ping(self, c, t="x"):
        c.send({"op": "ping", "t": t})
        self.assertTrue(c.wait(lambda ms: any(m["op"] == "pong" and m.get("t") == t for m in ms)), c.msgs)

    def test_dice_sonda_en_sesion_ok_y_reanudar_ok(self):
        c, ok = self.sesion(msg=self.mensaje(tablas=1, desde="laptop", sonda=1))
        self.assertEqual((ok.get("tablas"), ok.get("sonda")), (1, 1), ok)
        r = self.conectar()
        r.send({"op": "reanudar", "sesion": ok["sesion"], "logs": {}})
        self.assertTrue(r.wait(lambda ms: any(m["op"] == "reanudar_ok" for m in ms)), r.msgs)
        self.assertEqual(r.first("reanudar_ok").get("sonda"), 1)

    def test_sin_sonda_de_la_laptop_no_la_dice_e_ignora_un_sonda(self):
        c, ok = self.sesion(msg=self.mensaje(tablas=1, desde="laptop"))
        self.assertEqual(ok.get("tablas"), 1)
        self.assertNotIn("sonda", ok)
        c.send({"op": "sonda", "udp": 4000, "tcp": 4001, "dato": "a" * 32})
        self.ping(c)
        self.assertFalse([m for m in c.msgs if m["op"] in ("sonda_ok", "sonda_fin", "error")], c.msgs)

    def test_un_sonda_mal_formado_es_un_error_y_la_conexion_sigue(self):
        c, ok = self.sesion(msg=self.mensaje(tablas=1, desde="laptop", sonda=1))
        c.send({"op": "sonda", "udp": 0, "tcp": 4001, "dato": "a" * 32})
        self.assertTrue(c.wait(lambda ms: any(m["op"] == "sonda_fin" for m in ms)), c.msgs)
        malo = c.first("sonda_fin")   # §4.2: con su campo "error", no un `error` genérico
        self.assertEqual((malo["udp"], malo["tcp"]), (False, False))
        self.assertIn("la de la laptop llegó mal formada", malo["error"])
        self.assertIn("'udp' tiene que ser un puerto", malo["error"])
        self.ping(c)
        self.assertIsNone(c.first("sonda_ok"))
        self.assertIsNone(c.first("error"))
        c.msgs.remove(malo)
        # ... y la siguiente, bien formada, se hace (con un "laptop" que no contesta: sonda_fin dice que no llegó nada)
        with sonda.Lado() as lado, mock.patch.object(sonda, "SONDA_S", 0.5):
            lado.listen("127.0.0.1")
            c.send({"op": "sonda", **lado.offer()})
            self.assertTrue(c.wait(lambda ms: any(m["op"] == "sonda_fin" for m in ms)), c.msgs)
            self.assertTrue(lado.wait(time.monotonic() + TOPE_S))   # el agente sí mandó el dato de aquí
        self.assertEqual(c.first("sonda_fin"), {"op": "sonda_fin", "udp": False, "tcp": False})
        self.assertIn(c.first("sonda_ok")["so"], sonda.SISTEMAS)
        self.ping(c, "y")


# --- un agente de mentira ---
SID = "ab" * 16


class Falso:
    """Un agmen agent de mentira: saluda bien (con la clave), contesta sesion/reanudar con "tablas" (y "sonda" si
    `capaz`), contesta ping, y a cada `sonda` responde según el siguiente modo de `modos` (o "bien")."""

    def __init__(self, case, modos=(), capaz=True, barrera: threading.Barrier | None = None):
        self.modos = list(modos)
        self.capaz = capaz
        self.barrera = barrera
        self.sondas = 0
        self.chans: list = []
        self.agente = TA.FakeAgent(self._atender)
        case.addCleanup(self.agente.close)
        self.port = self.agente.port

    def _atender(self, c):
        ch = agent_handshake(c, TA.K())
        self.chans.append(ch)
        first = ch.recv(5)
        extra = {"tablas": 1, **({"sonda": 1} if self.capaz else {})}
        if first["op"] == "sesion":
            ch.send({"op": "sesion_ok", "maquina": "robot", "agmen": "0", "nodos": [], "sesion": SID, "gracia": 10,
                     **extra})
        else:
            ch.send({"op": "reanudar_ok", "nodos": {}, "gracia": 10, **extra})
        ip = c.getpeername()[0]
        while True:
            msg = ch.recv(30)
            if msg["op"] == "ping":
                ch.send({"op": "pong", "t": msg.get("t")})
            elif msg["op"] == "sonda":
                self.sondas += 1
                modo = self.modos.pop(0) if self.modos else "bien"
                threading.Thread(target=self._sonda, args=(ch, msg, ip, modo), daemon=True,
                                 name="falso-sonda").start()
            elif msg["op"] == "adios":
                return

    def _sonda(self, ch, msg, ip, modo):
        try:
            self._sonda_de_verdad(ch, msg, ip, modo)
        except agent.ChannelClosed:   # la laptop cerró a media prueba (fin de la prueba de la que sea): nada que hacer
            pass

    def _sonda_de_verdad(self, ch, msg, ip, modo):
        udp, tcp, dato = sonda.oferta(msg)
        if modo == "error":   # (como el agente de verdad cuando no puede: §4.2, campo propio)
            ch.send({"op": "sonda_fin", "udp": False, "tcp": False,
                     "error": "no pudo abrir sus sockets en 0.0.0.0 (prueba). Revisa AGMEN_NET_IP"})
            return
        if modo == "error_vacio":
            ch.send({"op": "sonda_fin", "udp": False, "tcp": False, "error": ""})
            return
        if modo == "corta":   # la conexión se pierde a media prueba
            ch.sock.shutdown(socket.SHUT_RDWR)
            return
        if modo == "mudo":   # no contesta nada (la laptop la deja por tiempo, o porque otra la sustituye)
            return
        if modo == "otro_error":   # un error que no es de la prueba: se dice como siempre y la prueba sigue
            ch.send({"op": "error", "texto": "robot: no conozco el nodo 'x'"})
        if modo == "error_que_habla_de_la_prueba":   # (antes se reconocía por el texto: ya no, §4.2)
            ch.send({"op": "error", "texto": "robot: algo que dice prueba de red pero no es de esta"})
        with sonda.Lado() as lado:
            lado.listen(ip)
            lado.send(ip, udp, tcp, sonda.nuevo_dato() if modo == "dato_malo" else dato)
            if modo == "no_ok":
                return
            ok = {"op": "sonda_ok", **lado.offer(), "so": "windows" if modo == "windows" else "linux"}
            if modo == "ok_malo":
                ok["tcp"] = 70000
            ch.send(ok)
            if modo == "error_tras_ok":
                ch.send({"op": "sonda_fin", "udp": False, "tcp": False, "error": "error inesperado (prueba)"})
                return
            if self.barrera is not None:
                self.barrera.wait(TOPE_S)   # las dos sondas a la vez: si fueran una tras otra, no pasa
            lado.wait(time.monotonic() + sonda.SONDA_S)
            if modo == "no_fin":
                return
            fin = {"op": "sonda_fin", "udp": lado.got_udp.is_set(), "tcp": lado.got_tcp.is_set()}
            if modo == "fin_malo":
                fin["udp"] = "sí"
            if modo == "dice_no_udp":
                fin["udp"] = False
            ch.send(fin)


class FalsoCase(TA.ConfigDirCase):
    def setUp(self):
        super().setUp()
        self.notas: list[str] = []

    def link(self, falso: Falso, name: str = "robot", sonda_tambien: bool = True):
        m = SimpleNamespace(name=name, host="127.0.0.1", port=falso.port)
        link = agent.AgentLink(m)
        link.attach(None, lambda who, text, kind, droppable=False: self.notas.append(text))
        self.addCleanup(link.close)
        msg = {"op": "sesion", "perfil": "real", "reloj": "real", "agmen": "0", "maquina": name, "nodos": {},
               "no_arrancar": [], "tablas": 1, "desde": "laptop", **({"sonda": 1} if sonda_tambien else {})}
        link.open(TA.KEY, msg)
        return link


class TestConAgenteDeMentira(FalsoCase):
    def test_agente_sin_sonda_avisa_una_vez_y_sigue(self):
        link = self.link(Falso(self, capaz=False))
        self.assertIsNone(link.net_check())
        self.assertIsNone(link.net_check())
        avisos = [n for n in self.notas if "no puedo probar la red con robot" in n]
        self.assertEqual(len(avisos), 1, self.notas)
        self.assertIn("Actualiza Agmen en robot (pip install -U agmen)", avisos[0])
        self.assertIsNotNone(link.channel)

    def probar(self, modo: str, sonda_s: float = 0.5):
        lados = Lados(self)
        falso = Falso(self, [modo])
        link = self.link(falso)
        t0 = time.monotonic()
        with mock.patch.object(sonda, "SONDA_S", sonda_s):
            res = link.net_check()
        self.assertLess(time.monotonic() - t0, sonda_s + sonda.MARGEN_S + 1.0)   # acaba aunque no contesten
        self.assertFalse(res.ok)
        self.assertTrue(until(lados.cerrados), "quedó un socket de la sonda abierto")
        self.assertIsNotNone(link.channel)   # la conexión no se cae por esto
        return res, link.probe_lines(res)

    def test_sonda_ok_que_no_llega(self):
        res, lines = self.probar("no_ok")
        self.assertIn("robot (127.0.0.1", res.error)
        self.assertIn("no contestó la prueba de red (no llegó su sonda_ok", res.error)

    def test_sonda_fin_que_no_llega(self):
        res, lines = self.probar("no_fin")
        self.assertIn("no contestó la prueba de red (no llegó su sonda_fin", res.error)
        self.assertEqual((res.aqui_udp, res.aqui_tcp), (True, True))

    def test_sonda_ok_mal_formado(self):
        res, _ = self.probar("ok_malo")
        self.assertIn("contestó mal la prueba de red ('tcp' tiene que ser un puerto", res.error)

    def test_sonda_fin_mal_formado(self):
        res, _ = self.probar("fin_malo")
        self.assertIn("contestó mal la prueba de red ('udp' y 'tcp' tienen que ser true o false)", res.error)

    def test_dato_equivocado_no_cuenta(self):
        res, lines = self.probar("dato_malo")
        self.assertEqual((res.aqui_udp, res.aqui_tcp, res.alla_udp, res.alla_tcp), (False, False, True, True))
        self.assertTrue(lines[0].startswith("esta computadora no recibe de robot ni por UDP (mensajes chicos) ni por "
                                            "TCP (mensajes grandes)"), lines)

    def test_a_medias_es_fallo(self):
        res, lines = self.probar("dice_no_udp")
        self.assertEqual((res.aqui_udp, res.aqui_tcp, res.alla_udp, res.alla_tcp), (True, True, False, True))
        self.assertEqual(len(lines), 1)
        self.assertIn("por UDP (mensajes chicos)", lines[0])

    def test_si_el_agente_contesta_error_falla_ya_con_ese_motivo(self):
        """§4.2: sonda_fin con "error" (aunque llegue antes que sonda_ok): sin esperar los SONDA_S + MARGEN_S; el
        motivo va en el error de la prueba (no se dice dos veces)."""
        lados = Lados(self)
        link = self.link(Falso(self, ["error"]))
        t0 = time.monotonic()
        res = link.net_check()   # SONDA_S de verdad (2 s): si esperara, tardaría 4
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertFalse(res.ok)
        self.assertRegex(res.error, r"^robot \(127\.0\.0\.1[^)]*\) no pudo hacer la prueba de red: no pudo abrir sus "
                                    r"sockets en 0\.0\.0\.0 \(prueba\)\. Revisa AGMEN_NET_IP$")
        self.assertEqual(link.probe_lines(res)[0], res.error)
        self.assertFalse(any("no pudo abrir sus sockets" in n for n in self.notas), self.notas)
        self.assertTrue(until(lados.cerrados))
        self.assertIsNotNone(link.channel)
        self.assertTrue(link.net_check().ok)   # la siguiente no arrastra el error de antes

    def test_un_error_tras_sonda_ok_tambien_corta_la_espera(self):
        lados = Lados(self)
        link = self.link(Falso(self, ["error_tras_ok"]))
        t0 = time.monotonic()
        with mock.patch.object(sonda, "SONDA_S", 5.0):
            res = link.net_check()
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertIn("no pudo hacer la prueba de red: error inesperado (prueba)", res.error)
        self.assertTrue(until(lados.cerrados))

    def test_un_error_vacio_en_sonda_fin_es_mal_formado(self):
        res, _ = self.probar("error_vacio")
        self.assertIn("contestó mal la prueba de red ('error' tiene que ser un texto", res.error)

    def test_un_error_que_no_es_de_la_prueba_se_dice_y_la_prueba_sigue(self):
        for modo, texto in (("otro_error", "robot: no conozco el nodo 'x'"),
                            ("error_que_habla_de_la_prueba", "robot: algo que dice prueba de red pero no es de esta")):
            with self.subTest(modo=modo):
                link = self.link(Falso(self, [modo]))
                res = link.net_check()
                self.assertTrue(res.ok, res)
                self.assertIn(texto, self.notas)

    def test_dos_pruebas_que_se_cruzan_no_se_pisan(self):
        """§4.3: la primera (el agente no le contesta) queda esperando; la segunda la sustituye: la vieja acaba ya
        como SinConexion (nunca un resultado), la nueva pasa, y lo que llegue por otra conexión no es de ninguna."""
        lados = Lados(self)
        falso = Falso(self, ["mudo", "bien"])
        link = self.link(falso)
        vieja: list = []
        t = threading.Thread(target=lambda: vieja.append(link.net_check()))
        t.start()
        self.assertTrue(until(lambda: falso.sondas == 1 and link._probing is not None))
        primera = link._probing
        t0 = time.monotonic()
        nueva = link.net_check()
        t.join(TOPE_S)
        self.assertTrue(nueva.ok, nueva)
        self.assertEqual(len(vieja), 1)
        self.assertIsInstance(vieja[0], agent.SinConexion)
        self.assertIn("otra prueba de red la sustituyó", vieja[0].motivo)
        self.assertTrue(primera.cancelled)
        self.assertLess(time.monotonic() - t0, sonda.SONDA_S + sonda.MARGEN_S)
        self.assertIsNone(link._probing)
        self.assertTrue(until(lados.cerrados))
        # Lo que llega se reparte por conexión: el de otra conexión no entra en la prueba.
        mia, otra = link.channel, object()
        prueba = agent._Prueba(mia)
        link._probing = prueba
        fin = {"op": "sonda_fin", "udp": False, "tcp": False, "error": "x"}
        link._dispatch(fin, otra)
        link._dispatch(fin)   # (sin conexión: antes de sesion_ok o al reanudar)
        self.assertTrue(prueba.box.empty())
        self.assertIsNone(prueba.error)
        link._dispatch(fin, mia)
        self.assertEqual(prueba.box.get_nowait(), fin)
        self.assertEqual(prueba.error, fin)
        link._probing = None

    def test_una_prueba_vieja_que_arranca_tarde_no_se_registra(self):
        """§4.3: la de una conexión que ya no es la actual no sustituye a nada: SinConexion, sin mandar nada."""
        falso = Falso(self)
        link = self.link(falso)
        actual = agent._Prueba(link.channel)
        link._probing = actual
        res = link._probe(link.channel, link._gen - 1, None)   # (una generación anterior)
        self.assertIsInstance(res, agent.SinConexion)
        self.assertIs(link._probing, actual)
        self.assertFalse(actual.cancelled)
        self.assertEqual(falso.sondas, 0)
        link._probing = None

    def test_sonda_ok_que_no_se_espera_se_ignora(self):
        falso = Falso(self)
        link = self.link(falso)
        falso.chans[0].send({"op": "sonda_ok", "udp": 1, "tcp": 1, "dato": "a" * 32, "so": "linux"})
        falso.chans[0].send({"op": "sonda_fin", "udp": True, "tcp": True})
        res = link.net_check()   # y la siguiente prueba no se confunde con esos
        self.assertTrue(res.ok, res)

    def test_al_reconectar_solo_avisa_una_vez_y_avisa_al_recuperarse(self):
        falso = Falso(self, ["dice_no_udp", "dice_no_udp", "bien", "bien"])
        link = self.link(falso)
        link.took_probe(True)   # al conectar pasó
        with mock.patch.object(sonda, "SONDA_S", 0.5):
            for _ in range(4):
                link._recheck(link.channel, link._gen)
        malas = [n for n in self.notas if "no pasó al reconectar" in n]
        self.assertEqual(len(malas), 1, self.notas)
        self.assertIn("sus nodos siguen corriendo; no detengo nada", malas[0])
        self.assertTrue(any("robot no recibe de esta computadora por UDP" in n for n in self.notas), self.notas)
        buenas = [n for n in self.notas if "la prueba de red ya pasa" in n]
        self.assertEqual(buenas, ["✔ red con 'robot': la prueba de red ya pasa (los topics pasan en los dos sentidos, "
                                  "UDP y TCP)"])
        self.assertIsNotNone(link.channel)
        self.assertEqual(falso.sondas, 4)

    def test_al_reanudar_se_repite_sola(self):
        """Tras un corte, la conexión reanudada repite la prueba (en su hilo) y, si falla, solo avisa."""
        falso = Falso(self, ["bien", "dice_no_udp"])
        link = self.link(falso)
        link.took_probe(link.net_check().ok)
        self.assertTrue(link.keep_trying())
        self.assertTrue(link.go())
        with mock.patch.object(sonda, "SONDA_S", 0.5):
            falso.chans[0].sock.close()   # el corte
            self.assertTrue(until(lambda: falso.sondas == 2 and any("no pasó al reconectar" in n
                                                                    for n in self.notas), 30), self.notas)
        self.assertTrue(until(lambda: link.channel is not None and link.down_since is None))


class TestAgmenRun(TA.LinkCase):
    """agmen run de verdad (en este proceso) contra el agente de verdad."""

    def setUp(self):
        super().setUp()
        self.start_server(write=False)
        # Un nodo de aquí (ia) y dos del robot, con hub (el puente: sin él no hay prueba de red).
        self.write_toml(**{"[hub]\nenabled = false": "[hub]\nenabled = true"})

    def run_aqui(self, *args):
        from agmen import cli as C

        self.addCleanup(signal.signal, signal.SIGINT, signal.getsignal(signal.SIGINT))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"AGMEN_REMOTE_HOSTS": "127.0.0.1", "NO_COLOR": "1", "AGMEN_AGENT_KEY": TA.KEY,
                                          "AGMEN_CONFIG_DIR": str(self.dir / "cfg-run")}), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", out), mock.patch("sys.stdin", io.StringIO()):
            codigo = C.main(["run", str(self.toml), "--no-browser", *args])
        return codigo, out.getvalue()

    def test_si_falla_al_conectar_no_arranca_nada_y_sale_como_si_no_contestara(self):
        lados = Lados(self)
        mal_manda(self, "laptop", "udp")
        with mock.patch.object(sonda, "SONDA_S", 1.0):
            codigo, salida = self.run_aqui()
        self.assertIn("la prueba de red con 'robot' no pasó, así que no arranqué nada:", salida)
        self.assertIn("robot no recibe de esta computadora por UDP (mensajes chicos)", salida)
        self.assertIn("--no-net-check", salida)
        if sonda.sistema() != "windows":   # §1.6: el límite de C++ solo si alguna computadora es Windows
            self.assertNotIn(sonda.LIMITE, salida)
        self.sin_arrancar_nada()   # ni los del robot ni el de aquí
        self.assertTrue(until(lados.cerrados))
        # El mismo código que cuando el agente no contesta.
        mudo = puerto_cerrado()
        self.write_toml(**{"[hub]\nenabled = false": "[hub]\nenabled = true",
                           f"127.0.0.1:{self.port}": f"127.0.0.1:{mudo}"})
        codigo_mudo, salida_mudo = self.run_aqui()
        self.assertIn("no contesta", salida_mudo)
        self.assertEqual(codigo, codigo_mudo)
        self.sin_arrancar_nada()

    def test_ctrl_c_durante_la_prueba_sale_limpio(self):
        lados = Lados(self)
        orig = sonda.Lado.listen

        def listen(self_, peer):   # el Ctrl+C llega a media prueba (del lado de la laptop)
            orig(self_, peer)
            if threading.current_thread().name.startswith("agmen-sonda-"):
                signal.raise_signal(signal.SIGINT)

        with mock.patch.object(sonda.Lado, "listen", listen):
            codigo, salida = self.run_aqui()
        self.assertEqual(codigo, 130, salida)
        self.assertIn("cancelado (Ctrl+C) mientras me conectaba con las otras computadoras", salida)
        self.sin_arrancar_nada()
        self.assertTrue(until(lados.cerrados))

    def test_no_net_check_avisa_y_no_pide_la_prueba(self):
        mudo = puerto_cerrado()
        self.write_toml(**{"[hub]\nenabled = false": "[hub]\nenabled = true",
                           f"127.0.0.1:{self.port}": f"127.0.0.1:{mudo}"})
        codigo, salida = self.run_aqui("--no-net-check")
        self.assertIn(sonda.SALTADA, salida)
        self.assertEqual(salida.count("me salté la prueba de red"), 1)
        cfg = load_config(self.toml)
        self.assertNotIn("sonda", agent.session_message(cfg, "robot", "real", "laptop", sonda=False))
        self.assertEqual(agent.session_message(cfg, "robot", "real", "laptop", sonda=True)["sonda"], 1)
        self.assertNotIn("sonda", agent.session_message(cfg, "robot", "real", None, sonda=True))   # sin puente, no


class TestVariosRobots(FalsoCase):
    def test_dos_robots_en_paralelo(self):
        barrera = threading.Barrier(2)
        a, b = Falso(self, barrera=barrera), Falso(self, barrera=barrera)
        links = [self.link(a, "robot"), self.link(b, "brazo")]
        said: list[str] = []
        launcher = SimpleNamespace(say=lambda t, k="agmen": said.append(t))
        error = cli._net_checks(launcher, links, threading.Event(), [])
        self.assertEqual(error, "", said)
        self.assertIn(sonda.PASA.format(robot="robot"), said)
        self.assertIn(sonda.PASA.format(robot="brazo"), said)
        self.assertEqual(said.count(sonda.LIMITE), 0)   # §1.6: ninguna es Windows
        self.assertTrue(all(l._net_ok for l in links))

    def test_si_uno_falla_el_error_lo_nombra_y_el_aviso_de_windows_sale_una_vez(self):
        a, b = Falso(self), Falso(self, ["dice_no_udp"])
        links = [self.link(a, "robot"), self.link(b, "brazo")]
        said: list[str] = []
        launcher = SimpleNamespace(say=lambda t, k="agmen": said.append(t))
        with mock.patch.object(sonda, "SONDA_S", 0.5), mock.patch.object(sonda, "sistema", lambda: "windows"):
            error = cli._net_checks(launcher, links, threading.Event(), [])
        self.assertEqual(said.count(f"⚠ {sonda.WINDOWS}"), 1, said)
        self.assertLess(said.index(f"⚠ {sonda.WINDOWS}"), said.index(sonda.PASA.format(robot="robot")))
        self.assertIn("la prueba de red con 'brazo' no pasó, así que no arranqué nada:", error)
        self.assertIn("brazo no recibe de esta computadora por UDP (mensajes chicos)", error)
        self.assertNotIn("'robot' no pasó", error)
        self.assertEqual(said.count(sonda.LIMITE), 1)   # esta es Windows: el límite, una vez
        self.assertTrue(error.endswith("(pero entonces, si un firewall bloquea los topics, Agmen no te lo va a "
                                       "decir)"), error)

    def net_checks(self, falsos, sistema="linux"):
        links = [self.link(f, n) for f, n in zip(falsos, ("robot", "brazo"))]
        said: list[str] = []
        launcher = SimpleNamespace(say=lambda t, k="agmen": said.append(t))
        with mock.patch.object(sonda, "SONDA_S", 0.5), mock.patch.object(sonda, "sistema", lambda: sistema):
            error = cli._net_checks(launcher, links, threading.Event(), [])
        return error, said

    def test_el_limite_de_cpp_sale_si_el_robot_es_windows(self):
        error, said = self.net_checks([Falso(self), Falso(self, ["windows"])])
        self.assertEqual(error, "", said)
        self.assertEqual(said.count(sonda.LIMITE), 1, said)

    def test_el_limite_de_cpp_no_sale_si_ninguna_es_windows(self):
        error, said = self.net_checks([Falso(self), Falso(self)])
        self.assertEqual(error, "", said)
        self.assertNotIn(sonda.LIMITE, said)

    def test_el_limite_de_cpp_no_sale_si_no_hubo_prueba(self):
        error, said = self.net_checks([Falso(self, capaz=False)], sistema="windows")
        self.assertEqual(error, "")
        self.assertNotIn(sonda.LIMITE, said)

    def test_si_la_conexion_se_pierde_a_media_prueba_no_dice_todo_bien(self):
        """§4.4: al conectar, la conexión perdida a media prueba es el caso de siempre: va a `lost` (una vez), no
        sale "✔" y cmd_run no arranca nada (sale con 1, como con cualquier conexión perdida al conectar)."""
        falso = Falso(self, ["corta"])
        link = self.link(falso)
        said: list[str] = []
        launcher = SimpleNamespace(say=lambda t, k="agmen": said.append(t))
        lost: list[str] = []
        stop = threading.Event()
        error = cli._net_checks(launcher, [link], stop, lost)
        self.assertEqual(error, "")
        self.assertEqual(len(lost), 1, said)
        self.assertIn("robot (127.0.0.1", lost[0])
        self.assertIn("vuelve a correr agmen run", lost[0])
        self.assertTrue(stop.is_set())
        self.assertNotIn(sonda.PASA.format(robot="robot"), said)
        self.assertEqual([t for t in said if t.startswith("✖")], [f"✖ {lost[0]}"])

    def test_net_check_sin_conexion_no_es_un_resultado(self):
        link = self.link(Falso(self))
        link.close()
        res = link.net_check()
        self.assertIsInstance(res, agent.SinConexion)

    def test_si_se_deja_de_esperar_cierra_todo(self):
        lados = Lados(self)
        falso = Falso(self, ["no_ok"])
        links = [self.link(falso)]
        launcher = SimpleNamespace(say=lambda t, k="agmen": None)
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()   # (como una computadora perdida: lo mismo que cancel)
        t0 = time.monotonic()
        self.assertEqual(cli._net_checks(launcher, links, stop, []), "")
        self.assertLess(time.monotonic() - t0, sonda.SONDA_S + sonda.MARGEN_S)
        self.assertTrue(until(lados.cerrados))


if __name__ == "__main__":
    unittest.main()

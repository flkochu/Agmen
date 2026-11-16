"""La diferencia entre los relojes de dos computadoras (RFC 0011 §3.6; bloque 5, parte A): la fórmula, la ventana,
"sin medir", las marcas en ping/pong (y los bytes de siempre con un Agmen anterior), el campo `relojes` de `red`, la
línea `maq` de `state` y los avisos de agmen run.

    python -m unittest discover -s python -p "test_relojes.py"

Nada depende de la hora real de esta máquina: las horas se inyectan (relojes de mentira). Esperas con tope."""

from __future__ import annotations

import socket
import time
import unittest
import warnings
from types import SimpleNamespace
from unittest import mock

import test_agent as TA
import test_topic_maquinas as TM
from agmen import agent as A
from agmen import hub as H
from agmen import hubproto as hp
from agmen import relojes as R
from agmen import tables as T

MS = 1_000_000
BASE = 1_700_000_000_000_000_000   # una hora de pared cualquiera (ns)


class Reloj:
    """Un reloj de mentira: hora de pared (ns) y monotonic (s) que solo avanzan a mano."""

    def __init__(self, pared: int = BASE, mono: float = 1000.0) -> None:
        self.pared, self.m = pared, mono

    def hora(self) -> int:
        return self.pared

    def mono(self) -> float:
        return self.m

    def avanza(self, s: float) -> None:
        self.pared += int(s * 1e9)
        self.m += s


def marcas(offset_ns: int, ida_ns: int, vuelta_ns: int, proceso_ns: int = 50_000, t1: int = BASE):
    """Las cuatro marcas de un ping/pong con el reloj del robot corrido `offset_ns`."""
    rx = t1 + ida_ns + offset_ns
    tx = rx + proceso_ns
    t4 = t1 + ida_ns + proceso_ns + vuelta_ns
    return t1, rx, tx, t4


# 1. La fórmula y la ventana
# ===========================================================================

class TestFormula(unittest.TestCase):
    def test_simetrica_da_el_offset_exacto(self):
        for off in (35 * MS, -35 * MS, 0, 86_400 * 10 ** 9):
            with self.subTest(off=off):
                m = R.medir(*marcas(off, 2 * MS, 2 * MS))
                self.assertEqual(m.dif_ns, off)
                self.assertEqual(m.ida_vuelta_ns, 4 * MS)
                self.assertEqual(m.err_ns, 2 * MS)

    def test_asimetrica_queda_dentro_de_la_incertidumbre(self):
        for off in (35 * MS, -35 * MS, 0):
            for ida, vuelta in ((1 * MS, 9 * MS), (9 * MS, 1 * MS), (0, 6 * MS)):
                with self.subTest(off=off, ida=ida, vuelta=vuelta):
                    m = R.medir(*marcas(off, ida, vuelta))
                    self.assertEqual(m.dif_ns, off + (ida - vuelta) // 2)
                    self.assertLessEqual(abs(m.dif_ns - off), m.err_ns)

    def test_absurdas_se_descartan(self):
        t1, rx, tx, t4 = marcas(0, 1 * MS, 1 * MS)
        self.assertIsNone(R.medir(t1, rx, tx, t1 - 1))                                  # ida y vuelta negativa
        self.assertIsNone(R.medir(*marcas(0, 1100 * MS, 1000 * MS)))                    # más de 2 s
        self.assertIsNotNone(R.medir(*marcas(0, 999 * MS, 999 * MS)))                   # justo debajo
        self.assertIsNone(R.medir(t1, tx, rx, t4))                                      # tx antes que rx


class Laptop:
    """Un Medidor con relojes de mentira y un robot de mentira con su propio reloj (offset)."""

    def __init__(self, offset_ns: int = 0) -> None:
        self.r = Reloj()
        self.med = R.Medidor(hora=self.r.hora, mono=self.r.mono)
        self.offset = offset_ns
        self.canal = object()
        self.t = 0.0
        self.cada = 2.0   # s entre un ping y el siguiente

    def ronda(self, ida_ns: int = 1 * MS, vuelta_ns: int = 1 * MS, reloj: dict | None | str = "bien") -> dict | None:
        """Un ping y su pong; devuelve lo que llevaba el ping en "reloj" (None: no lo llevaba)."""
        self.t += 1.0
        lleva = self.med.ping(self.canal, self.t)
        t1 = self.r.pared
        self.r.pared += ida_ns
        rx = self.r.pared + self.offset
        tx = rx + 10_000
        self.r.pared += 10_000 + vuelta_ns
        pong = {"op": "pong", "t": self.t}
        if reloj == "bien":
            pong["reloj"] = {"rx": rx, "tx": tx}
        elif reloj is not None:
            pong["reloj"] = reloj
        self.dicho = self.med.pong(self.canal, pong)
        self.r.avanza(self.cada - (self.r.pared - t1) / 1e9)
        return lleva


class TestVentana(unittest.TestCase):
    def test_se_usa_la_de_menor_ida_y_vuelta_de_las_ultimas_8(self):
        lap = Laptop(offset_ns=20 * MS)
        lap.cada = 1.0                                  # (todas vigentes: aquí solo cuenta la ventana de 8)
        lap.ronda(1 * MS, 1 * MS)                       # la mejor… pero va a salir de la ventana
        for _ in range(R.RELOJ_VENTANA - 1):
            lap.ronda(30 * MS, 10 * MS)                 # sesgadas: dif = 20 + 10 = 30 ms
        e = lap.med.estado()
        self.assertTrue(e.medido)
        self.assertEqual(e.dif_ns, 20 * MS)             # la de 1+1 ms sigue entre las últimas 8 (y vigente)
        lap.ronda(3 * MS, 3 * MS)                       # la novena: la primera sale
        e = lap.med.estado()
        self.assertEqual(e.dif_ns, 20 * MS)
        self.assertEqual(e.err_ns, 3 * MS)              # (lo que tardó el agente no cuenta)

    def test_solo_cuentan_las_vigentes(self):
        lap = Laptop(offset_ns=-35 * MS)
        lap.ronda(1 * MS, 1 * MS)
        lap.r.avanza(R.RELOJ_VIGENTE_S - 3)
        lap.ronda(20 * MS, 4 * MS)                      # peor, pero la de antes ya pasó de 10 s
        e = lap.med.estado()
        self.assertEqual(e.dif_ns, -35 * MS + 8 * MS)

    def test_el_ping_lleva_vacio_y_luego_la_mejor(self):
        lap = Laptop(offset_ns=35 * MS)
        self.assertEqual(lap.ronda(), {})
        lleva = lap.ronda()
        self.assertEqual(set(lleva), {"dif", "err"})
        self.assertEqual(lleva["dif"], 35 * MS)

    def test_sin_medir_tras_10_s_y_sin_conexion(self):
        lap = Laptop(offset_ns=5 * MS)
        e = lap.med.estado()
        self.assertEqual((e.medido, e.motivo, e.edad_s), (False, "esperando", None))
        lap.ronda()
        self.assertTrue(lap.med.estado().medido)
        e = lap.med.estado(conectado=False)
        self.assertEqual((e.medido, e.motivo), (False, "sin_conexion"))
        self.assertEqual(e.dif_ns, 0)                    # nunca un valor viejo como si fuera actual
        lap.r.avanza(R.RELOJ_VIGENTE_S + 0.5)
        e = lap.med.estado()
        self.assertEqual((e.medido, e.motivo), (False, "vieja"))
        self.assertGreater(e.edad_s, R.RELOJ_VIGENTE_S)
        self.assertEqual(R.linea_corta("robot", e), f"robot: reloj sin medir ({R.MOTIVOS['vieja']})")

    def test_marcas_mal_formadas_se_ignoran_y_se_dice_una_vez(self):
        lap = Laptop()
        for malo in ({"rx": "1", "tx": 2}, {"rx": 1.5, "tx": 2}, {"rx": True, "tx": 2}, {"rx": -1, "tx": 2},
                     {"rx": 2 ** 63, "tx": 2 ** 63}, {"rx": 5, "tx": 4}, [1, 2], "x", {}):
            with self.subTest(malo=malo):
                lap.ronda(reloj=malo)
        self.assertFalse(lap.med.estado().medido)
        lap2 = Laptop()
        lap2.ronda(reloj={"rx": "x"})
        self.assertEqual(lap2.dicho, ["mal_formado"])
        lap2.ronda(reloj={"rx": "x"})
        self.assertEqual(lap2.dicho, [])
        lap2.ronda()
        self.assertTrue(lap2.med.estado().medido)

    def test_pong_de_otro_ping_o_sin_t(self):
        lap = Laptop()
        self.assertEqual(lap.med.pong(lap.canal, {"op": "pong", "t": 123.0, "reloj": {"rx": 1, "tx": 2}}), [])
        self.assertEqual(lap.med.pong(lap.canal, {"op": "pong", "t": [1], "reloj": {"rx": 1, "tx": 2}}), [])
        self.assertEqual(lap.med.pong(lap.canal, {"op": "pong", "t": None}), [])
        self.assertFalse(lap.med.estado().medido)


# 2. Compatibilidad: agente anterior y laptop anterior
# ===========================================================================

def enlace() -> A.AgentLink:
    return A.AgentLink(SimpleNamespace(name="robot", host="127.0.0.1", port=7391))


class Canal:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, obj: dict) -> None:
        self.sent.append(obj)


class TestAgenteAnterior(unittest.TestCase):
    def test_tras_su_primer_pong_los_ping_son_los_de_siempre(self):
        link = enlace()
        dicho = []
        link._notice = lambda text, kind="agmen": dicho.append(text)
        canal = Canal()
        primero = link._ping_msg(canal, 10.5)
        self.assertEqual(primero, {"op": "ping", "t": 10.5, "reloj": {}})
        link._clock_pong({"op": "pong", "t": 10.5}, canal)   # el pong de un agente anterior: sin "reloj"
        self.assertEqual(dicho, [R.aviso_anterior("robot")])
        e = link._reloj.estado()
        self.assertEqual((e.medido, e.motivo), (False, "anterior"))
        for t in (12.5, 14.5):
            self.assertEqual(link._ping_msg(canal, t), {"op": "ping", "t": t})   # los bytes de siempre
            link._clock_pong({"op": "pong", "t": t}, canal)
        self.assertEqual(len(dicho), 1)                                         # se dijo una vez
        # una conexión nueva (quizá ya actualizaron Agmen allá): se vuelve a ofrecer
        self.assertEqual(link._ping_msg(Canal(), 16.5), {"op": "ping", "t": 16.5, "reloj": {}})

    def test_el_lector_viejo_de_la_laptop_no_se_fija_en_reloj(self):
        """Un pong con "reloj" sigue sirviendo para lo de siempre (el último contacto confirmado)."""
        t = time.monotonic() - 1
        self.assertEqual(A.AgentLink._confirmed({"op": "pong", "t": t, "reloj": {"rx": 1, "tx": 2}}, 0.0), t)


def sesion_agente(primero: dict | None = None):
    s = A._Session.__new__(A._Session)
    enviados, dicho = [], []
    r = Reloj(pared=BASE + 35 * MS)
    s.__dict__.update(_awaiting=False, send=lambda obj, droppable=False: enviados.append(obj) or True,
                      _reloj=R.Remoto(mono=r.mono), _hora=r.hora, _first=primero or {"desde": "laptop", "reloj": "real"},
                      ip="192.168.1.5", say=lambda text, kind="agmen": dicho.append((text, kind)))
    return s, enviados, dicho, r


class TestLaptopAnterior(unittest.TestCase):
    def test_el_pong_es_el_de_siempre_si_el_ping_no_trae_reloj(self):
        s, enviados, dicho, _ = sesion_agente()
        for t in (7, 1.5, "x"):
            s._order({"op": "ping", "t": t})
            self.assertEqual(enviados[-1], {"op": "pong", "t": t})
        self.assertEqual(len([d for d in dicho if "Agmen anterior" in d[0]]), 1)   # una vez
        e = s._reloj.estado()
        self.assertEqual((e.medido, e.motivo), (False, "anterior"))

    def test_con_reloj_el_pong_trae_rx_y_tx(self):
        s, enviados, dicho, r = sesion_agente()
        s._order({"op": "ping", "t": 3.0, "reloj": {}})
        self.assertEqual(enviados[-1], {"op": "pong", "t": 3.0, "reloj": {"rx": r.pared, "tx": r.pared}})
        self.assertEqual(dicho, [])
        self.assertEqual(s._reloj.estado().motivo, "esperando")
        s._order({"op": "ping", "t": 5.0, "reloj": {"dif": 35 * MS, "err": 1 * MS}})
        e = s._reloj.estado()
        self.assertEqual((e.medido, e.dif_ns, e.err_ns), (True, -35 * MS, 1 * MS))   # con el signo cambiado
        textos = [t for t, _ in dicho]
        self.assertTrue(textos[0].startswith("reloj de la laptop (laptop): -35.0 ms (±1.0 ms)"), textos)
        self.assertIn("va 35.0 ms atrasado", textos[1])
        s._order({"op": "ping", "t": 7.0, "reloj": {"dif": 2 * MS, "err": 1 * MS}})
        self.assertTrue(dicho[-1][0].startswith("✓ el reloj de la laptop (laptop) ya coincide"), dicho)
        n = len(dicho)
        s._order({"op": "ping", "t": 9.0, "reloj": {"dif": 3 * MS, "err": 1 * MS}})
        self.assertEqual(len(dicho), n)
        s._reloj.perdida()
        self.assertEqual(s._reloj.estado().motivo, "sin_conexion")
        r.avanza(R.RELOJ_VIGENTE_S + 1)
        s._order({"op": "ping", "t": 11.0, "reloj": {}})
        self.assertEqual(s._reloj.estado().motivo, "vieja")

    def test_en_sim_el_agente_no_avisa_del_umbral(self):
        s, _, dicho, _ = sesion_agente({"desde": "laptop", "reloj": "sim"})
        s._order({"op": "ping", "t": 5.0, "reloj": {"dif": 35 * MS, "err": 1 * MS}})
        self.assertEqual(len(dicho), 1)
        self.assertIn("(no afecta en simulación)", dicho[0][0])

    def test_medicion_mal_formada_se_ignora(self):
        s, enviados, dicho, _ = sesion_agente()
        for malo in ({"dif": "1", "err": 1}, {"dif": 1, "err": -1}, {"dif": 1.0, "err": 1}, "x", [1]):
            s._order({"op": "ping", "t": 1, "reloj": malo})
            self.assertIn("reloj", enviados[-1])
        self.assertFalse(s._reloj.estado().medido)
        self.assertEqual(len(dicho), 1)
        self.assertIn("mal formada", dicho[0][0])


class TestDeVerdad(TA.ServerCase):
    """Un agente de verdad y un AgentLink de verdad en esta computadora: el mismo reloj, la diferencia es chica."""

    def setUp(self):
        super().setUp()
        warnings.simplefilter("ignore", ResourceWarning)

    def test_la_laptop_mide_y_el_agente_se_entera(self):
        self.start_server()
        link = A.AgentLink(SimpleNamespace(name="robot", host="127.0.0.1", port=self.port))
        link._notice = lambda *a, **k: None
        link.open(TA.KEY, self.mensaje())
        self.addCleanup(link.close)
        self.assertTrue(self.espera(lambda: link.clock_state().medido, timeout=15), link.clock_state())
        e = link.clock_state()
        self.assertLess(abs(e.dif_ns), 50 * MS)
        self.assertLessEqual(e.edad_s, R.RELOJ_VIGENTE_S)
        # el agente lo sabe por el ping siguiente (y lo dice en su terminal)
        self.assertTrue(self.espera(lambda: any(t.startswith("reloj de la laptop") for t, _ in self.said), timeout=15),
                        self.said)


# 3. El hub: `red` con relojes y la línea `maq` de `state`
# ===========================================================================

def hub_de_prueba() -> H.Hub:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    hub = H.Hub(f"127.0.0.1:{port}", log=lambda t: None)
    hub._sent = []
    hub._send = lambda data, addr: hub._sent.append(data)
    return hub


LOCAL = ("127.0.0.1", 5555)


def maq(hub: H.Hub) -> list[dict]:
    return [f for op, f in hp.parse(hub.state_text().encode()) if op == "maq"]


class TestCampoRelojes(unittest.TestCase):
    def test_ida_y_vuelta(self):
        estados = [("robot", R.Estado(True, -35 * MS, 600_000, 1.5)), ("pi", R.Estado(False, motivo="anterior")),
                   ("cam", R.Estado(False, edad_s=12.25, motivo="vieja"))]
        texto = R.formato_red(estados)
        self.assertEqual(texto, "robot:-35000000:600000:1500,pi:-:anterior:-,cam:-:vieja:12250")
        self.assertEqual(hp.quote(texto), texto)   # nada se escapa en agmen-hub/1
        self.assertEqual(R.leer_red(texto), dict(estados))

    def test_mal_escritos(self):
        for malo in ("robot", "robot:1:2", "robot:x:1:1", "robot:1:-1:1", "robot:1:1:-", "r o:1:1:1",
                     "robot:-:MAL:-", "robot:1:1:1,robot:2:2:2", f"robot:{2 ** 63}:1:1", "robot:1:1:x"):
            with self.subTest(malo=malo), self.assertRaises(ValueError):
                R.leer_red(malo)


class TestHub(unittest.TestCase):
    def red(self, hub, **campos):
        hub.handle(hp.encode("red", peers="robot@192.168.1.20,pi@192.168.1.30", **campos), LOCAL, time.monotonic())

    def test_sin_modo_red_state_es_el_de_siempre(self):
        hub = hub_de_prueba()
        self.addCleanup(hub.sock.close)
        antes = hub.state_text()
        self.assertNotIn(" maq ", antes)
        self.red(hub, relojes="robot:1:1:1")
        self.assertIn(" maq ", hub.state_text())
        hub._leave_net("prueba")
        self.assertEqual(hub.state_text(), antes)
        self.assertEqual(hub.relojes, {})

    def test_state_con_y_sin_medicion(self):
        hub = hub_de_prueba()
        self.addCleanup(hub.sock.close)
        self.red(hub, relojes="robot:-35000000:600000:1500,pi:-:anterior:-")
        self.assertEqual(hub._sent[-1], hp.encode("red_ok", peers=2))
        lineas = maq(hub)
        self.assertEqual(lineas[0], {"name": "robot", "ip": "192.168.1.20", "reloj": "-35000000", "err": "600000",
                                     "edad": lineas[0]["edad"]})
        self.assertAlmostEqual(float(lineas[0]["edad"]), 1.5, delta=0.3)
        self.assertEqual(lineas[1], {"name": "pi", "ip": "192.168.1.30", "sin": "anterior"})
        # un `red` sin el campo (un puente anterior): "sin dato"
        self.red(hub)
        self.assertEqual([f.get("sin") for f in maq(hub)], ["sin_dato", "sin_dato"])

    def test_caduca_a_los_10_s(self):
        hub = hub_de_prueba()
        self.addCleanup(hub.sock.close)
        now = time.monotonic()
        hub.handle(hp.encode("red", peers="robot@192.168.1.20", relojes="robot:5:1:9000"), LOCAL, now)
        with mock.patch.object(H.time, "monotonic", return_value=now + 0.5):
            self.assertEqual(maq(hub)[0]["reloj"], "5")
        with mock.patch.object(H.time, "monotonic", return_value=now + 1.5):
            f = maq(hub)[0]
        self.assertEqual(f["sin"], "vieja")
        self.assertNotIn("reloj", f)
        self.assertAlmostEqual(float(f["edad"]), 10.5, delta=0.05)

    def test_relojes_mal_escritos_no_tumban_el_red(self):
        dicho = []
        hub = hub_de_prueba()
        hub.log = dicho.append
        self.addCleanup(hub.sock.close)
        self.red(hub, relojes="robot:basura")
        self.assertEqual(hub._sent[-1], hp.encode("red_ok", peers=2))
        self.assertTrue(hub.net_mode)
        self.assertEqual([f.get("sin") for f in maq(hub)], ["sin_dato", "sin_dato"])
        self.assertTrue(any("diferencia de relojes mal escrita" in d for d in dicho), dicho)

    def test_una_computadora_que_no_esta_en_peers_se_ignora(self):
        hub = hub_de_prueba()
        self.addCleanup(hub.sock.close)
        self.red(hub, relojes="otra:1:1:1,robot:2:1:1")
        self.assertEqual(set(hub.relojes), {"robot"})

    def test_un_hub_anterior_ignora_el_campo(self):
        """Un hub anterior solo lee `peers` de `red` (lo de hoy sin _set_relojes): contesta red_ok igual."""
        hub = hub_de_prueba()
        self.addCleanup(hub.sock.close)
        with mock.patch.object(H.Hub, "_set_relojes", lambda self, texto, now: None):
            self.red(hub, relojes="robot:1:1:1")
        self.assertEqual(hub._sent[-1], hp.encode("red_ok", peers=2))
        self.assertEqual(hub.peers, [("robot", "192.168.1.20"), ("pi", "192.168.1.30")])


class TestHubModo(unittest.TestCase):
    """§6.1/§6.4: el `modo` de `red` va en la línea `state`, solo en modo red, y caduca con él."""

    def red(self, hub, now=None, **campos):
        hub.handle(hp.encode("red", peers="robot@192.168.1.20", **campos), LOCAL,
                   time.monotonic() if now is None else now)

    def cabecera(self, hub) -> dict:
        return next(f for op, f in hp.parse(hub.state_text().encode()) if op == "state")

    def test_red_con_y_sin_modo(self):
        hub = hub_de_prueba()
        self.addCleanup(hub.sock.close)
        for modo in ("real", "sim", "replay"):
            with self.subTest(modo=modo):
                self.red(hub, modo=modo)
                self.assertEqual(hub._sent[-1], hp.encode("red_ok", peers=1))
                self.assertEqual(self.cabecera(hub)["modo"], modo)
                primera = hub.state_text().split("\n")[0]
                self.assertTrue(primera.endswith(f" robot={hp.quote(hub.robot)} modo={modo}"), primera)
        # un `red` sin `modo` (un puente anterior): se acepta igual y el hub ya no lo sabe
        self.red(hub)
        self.assertEqual(hub._sent[-1], hp.encode("red_ok", peers=1))
        self.assertTrue(hub.net_mode)
        self.assertNotIn("modo", self.cabecera(hub))

    def test_desconocido_se_ignora_y_se_dice(self):
        dicho = []
        hub = hub_de_prueba()
        hub.log = dicho.append
        self.addCleanup(hub.sock.close)
        self.red(hub, modo="real")
        self.red(hub, modo="marciano")
        self.assertEqual(hub._sent[-1], hp.encode("red_ok", peers=1))
        self.assertTrue(hub.net_mode)
        self.assertNotIn("modo", self.cabecera(hub))
        self.assertTrue(any("modo de reloj desconocido" in d and "marciano" in d for d in dicho), dicho)

    def test_sin_modo_red_state_identico(self):
        hub = hub_de_prueba()
        self.addCleanup(hub.sock.close)
        antes = hub.state_text()
        self.red(hub, modo="real")
        self.assertIn(" modo=real", hub.state_text())
        hub.handle(hp.encode("red", peers=""), LOCAL, time.monotonic())   # vuelta a modo local pedida
        self.assertEqual(hub.state_text(), antes)
        self.assertEqual(hub.modo, "")

    def test_caduca_con_el_modo_red(self):
        hub = hub_de_prueba()
        self.addCleanup(hub.sock.close)
        antes = hub.state_text()
        now = time.monotonic()
        self.red(hub, now, modo="sim")
        hub._check_net(now + hp.NET_EXPIRE_S / 2)
        self.assertEqual(self.cabecera(hub)["modo"], "sim")
        hub._check_net(now + hp.NET_EXPIRE_S + 0.1)
        self.assertFalse(hub.net_mode)
        self.assertEqual(hub.state_text(), antes)

    def test_un_hub_anterior_ignora_el_campo(self):
        """Un hub anterior (sin _set_modo): contesta red_ok igual y su `state` no cambia."""
        hub = hub_de_prueba()
        self.addCleanup(hub.sock.close)
        with mock.patch.object(H.Hub, "_set_modo", lambda self, modo: None):
            self.red(hub, modo="real")
        self.assertEqual(hub._sent[-1], hp.encode("red_ok", peers=1))
        self.assertEqual(hub.peers, [("robot", "192.168.1.20")])
        self.assertNotIn("modo", self.cabecera(hub))


class TestHerramientasIgnoranMaq(unittest.TestCase):
    """§2.2: agmen topic list/info ignoran la línea `maq`: su salida no cambia. (agmen node list sí la usa desde la
    parte B, §4.1: ver test_relojes_ver.py; aquí se comprueba que la tabla y las cuentas no cambian.)"""

    def test_salida_igual(self):
        extra = hp.parse("\n".join([hp.line("maq", name="robot", ip="192.168.1.20", reloj=-35000000, err=600000,
                                            edad="1.5"),
                                    hp.line("maq", name="pi", ip="192.168.1.30", sin="anterior")]).encode())
        for fn, kw in (("cmd_list", {}), ("cmd_info", {"topic": "/imu"})):
            with self.subTest(fn=fn):
                self.assertEqual(TM.corre(fn, TM.lineas() + extra, **kw), TM.corre(fn, TM.lineas(), **kw))
        with self.subTest(fn="cmd_list", machine=True):
            self.assertEqual(TM.corre("cmd_list", TM.lineas("robot") + extra), TM.corre("cmd_list", TM.lineas("robot")))
        for machine in (None, "robot"):
            with self.subTest(fn="cmd_node_list", machine=machine):
                con = TM.corre("cmd_node_list", TM.lineas(machine) + extra).split("\n")
                sin = TM.corre("cmd_node_list", TM.lineas(machine)).split("\n")
                i = con.index("# relojes de las otras computadoras (respecto al de esta)")
                self.assertEqual(con[:i - 1] + con[i + 3:], sin)   # (la línea en blanco, el título y las dos)
        # el puente (tablas.local_table) tampoco se fija en ella
        self.assertEqual(T.local_table(TM.lineas() + extra), T.local_table(TM.lineas()))


# 4. El puente pasa los relojes en `red`
# ===========================================================================

class TestPuente(unittest.TestCase):
    def puente(self, reloj=None, modo="", dicho=None):
        hub = hub_de_prueba()
        self.addCleanup(hub.sock.close)
        b = T.Bridge(hub.listen, (lambda t, k="agmen": dicho.append(t)) if dicho is not None else
                     (lambda *a, **k: None), modo=modo)
        b._sock = mock.Mock()
        sent = []
        b._sock.sendto = lambda data, addr: sent.append(data)
        b._sock.recvfrom = mock.Mock(side_effect=BlockingIOError)
        b._read_hub = lambda now, names=(): None
        p = b.add_peer("robot", lambda part: None, reloj=reloj)
        self.addCleanup(p.close, False)
        p.confirmed, p.ip = True, "192.168.1.20"
        b.tick()
        return [d for d in sent if d.startswith(b"agmen-hub/1 red ")]

    def test_sin_reloj_el_red_de_siempre(self):
        self.assertEqual(self.puente(), [b"agmen-hub/1 red peers=robot%40192.168.1.20"])

    def test_con_reloj(self):
        got = self.puente(lambda: R.Estado(True, 2 * MS, 600_000, 0.25))
        self.assertEqual(got, [b"agmen-hub/1 red peers=robot%40192.168.1.20 relojes=robot:2000000:600000:250"])
        got = self.puente(lambda: R.Estado(False, motivo="esperando"))
        self.assertEqual(got, [b"agmen-hub/1 red peers=robot%40192.168.1.20 relojes=robot:-:esperando:-"])

    def test_con_modo(self):
        for modo in ("real", "sim", "replay"):
            with self.subTest(modo=modo):
                self.assertEqual(self.puente(modo=modo), [f"agmen-hub/1 red peers=robot%40192.168.1.20 "
                                                          f"modo={modo}".encode()])
        got = self.puente(lambda: R.Estado(True, 2 * MS, 600_000, 0.25), modo="real")
        self.assertEqual(got, [b"agmen-hub/1 red peers=robot%40192.168.1.20 relojes=robot:2000000:600000:250 "
                               b"modo=real"])

    def test_modo_desconocido_no_va_y_se_dice(self):
        dicho = []
        self.assertEqual(self.puente(modo="marciano", dicho=dicho), [b"agmen-hub/1 red peers=robot%40192.168.1.20"])
        self.assertTrue(any("modo de reloj desconocido" in d for d in dicho), dicho)

    def test_un_reloj_que_falla_no_tumba_el_red(self):
        def roto():
            raise RuntimeError("x")
        self.assertEqual(self.puente(roto), [b"agmen-hub/1 red peers=robot%40192.168.1.20"])


# 5. Los avisos de agmen run
# ===========================================================================

def medido(dif_ms: float, err_ms: float = 0.6) -> R.Estado:
    return R.Estado(True, int(dif_ms * MS), int(err_ms * MS), 1.0)


class TestAvisos(unittest.TestCase):
    def vigia(self, modo="real", so=""):
        self.r = Reloj()
        return R.Vigia("robot", modo, so=lambda: so, mono=self.r.mono)

    def test_umbral_con_histeresis(self):
        v = self.vigia()
        self.assertEqual(v.revisar(medido(9.9)), [])
        [aviso] = v.revisar(medido(35))
        self.assertTrue(aviso.startswith("⚠ el reloj de robot va 35.0 ms adelantado respecto al de esta computadora "
                                         "(±0.6 ms)"), aviso)
        for frag in ("los t_data de sus topics no coinciden", "latencias y las grabaciones", "revisa los dos",
                     "sudo apt install chrony", "w32tm /resync"):
            self.assertIn(frag, aviso)
        self.assertEqual(v.revisar(medido(9)), [])         # entre 8 y 10: sigue mal, sin repetir
        self.assertEqual(v.revisar(medido(-12)), [])
        [bien] = v.revisar(medido(7.9))
        self.assertEqual(bien, "✓ el reloj de robot ya coincide con el de esta computadora: +7.9 ms (±0.6 ms)")
        self.assertEqual(v.revisar(medido(9.5)), [])       # por debajo de 10: no vuelve a avisar
        [otra] = v.revisar(medido(-10.5))
        self.assertIn("10.5 ms atrasado", otra)

    def test_primera_medicion_ya_mala_y_recordatorio_cada_10_min(self):
        v = self.vigia()
        self.assertEqual(len(v.revisar(medido(-500))), 1)
        self.r.avanza(R.RELOJ_RECORDATORIO_S - 1)
        self.assertEqual(v.revisar(medido(-500)), [])
        self.r.avanza(1)
        [rec] = v.revisar(medido(-500))
        self.assertTrue(rec.startswith("⚠ sigue igual: el reloj de robot va 500.0 ms atrasado"), rec)
        self.r.avanza(R.RELOJ_RECORDATORIO_S - 1)
        self.assertEqual(v.revisar(medido(-500)), [])
        self.r.avanza(1)
        self.assertEqual(len(v.revisar(medido(-500))), 1)

    def test_el_consejo_del_sistema_del_robot_primero(self):
        win = self.vigia(so="windows").revisar(medido(35))[0]
        self.assertLess(win.index("w32tm"), win.index("chrony"))
        lin = self.vigia(so="linux").revisar(medido(35))[0]
        self.assertLess(lin.index("chrony"), lin.index("w32tm"))

    def test_incertidumbre_grande_no_afirma_nada(self):
        v = self.vigia()
        [lenta] = v.revisar(medido(50, err_ms=25))
        self.assertEqual(lenta, "⚠ la red con robot está demasiado lenta para medir su reloj con precisión (±25.0 ms): "
                                "no puedo decir si va bien o corrido. Lo sigo midiendo; si la red mejora, aviso solo "
                                "si está corrido")
        self.assertEqual(v.revisar(medido(50, err_ms=25)), [])   # una vez
        self.assertFalse(v.mal)
        self.assertEqual(len(v.revisar(medido(50, err_ms=2))), 1)

    def test_sim_y_replay_sin_avisos(self):
        for modo in ("sim", "replay"):
            with self.subTest(modo=modo):
                v = self.vigia(modo)
                self.assertEqual(v.revisar(medido(500)), [])
                self.assertEqual(v.revisar(medido(50, err_ms=25)), [])

    def test_sin_medir_no_avisa(self):
        self.assertEqual(self.vigia().revisar(R.Estado(False, motivo="esperando")), [])

    def test_agmen_run_avisa_por_su_terminal(self):
        """AgentLink con watch_clock: el aviso sale con cada pong (y solo en real)."""
        for modo, n in (("real", 1), ("sim", 0)):
            with self.subTest(modo=modo):
                link = enlace()
                r = Reloj()
                link._reloj = R.Medidor(hora=r.hora, mono=r.mono)
                link.watch_clock(modo)
                link._vigia.mono = r.mono
                link.channel = canal = Canal()
                dicho = []
                link._notice = lambda text, kind="agmen", d=dicho: d.append((text, kind))
                link._ping_msg(canal, 1.0)
                r.pared += 2 * MS
                link._clock_pong({"op": "pong", "t": 1.0, "reloj": {"rx": BASE + 40 * MS, "tx": BASE + 40 * MS}},
                                 canal)
                self.assertEqual(len(dicho), n, dicho)
                if n:
                    self.assertEqual(dicho[0][1], "error")
                    self.assertIn("39.0 ms adelantado", dicho[0][0])

    def test_formatos(self):
        self.assertEqual(R.linea_corta("robot", medido(2.1)), "robot: reloj +2.1 ms (±0.6)")
        self.assertEqual(R.fmt_dif(-3_400_000_000), "-3.40 s")
        self.assertEqual(R.fmt_dif(86_400 * 10 ** 9), "+24.0 h")


if __name__ == "__main__":
    unittest.main()

"""Bloque 5, parte B (RFC 0011 §3.6; especificación §4): la diferencia de relojes en agmen node list, el panel
(/api/state y latencias) y agmen record.

    python -m unittest discover -s python -p "test_relojes_ver.py"

Sin otras computadoras todo sale igual que antes (node list byte a byte, /api/state sin claves nuevas, grabaciones
sin `relojes`). Nada depende de la hora real de la máquina: las diferencias vienen de líneas `maq` fijas o de un
`red` mandado a mano a un hub de verdad. Esperas con tope."""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import test_record as TREC
import test_topic_maquinas as TM
from agmen import _mcap, record, relojes as R, topic, wire
from agmen import hubproto as hp
from agmen.panel.server import Panel
from agmen.run import LogBook

MS = 1_000_000


def con_maq(*maqs: dict, machine: str | None = "robot") -> list:
    """Las líneas fijas de test_topic_maquinas más unas `maq` (como las da un hub en modo red)."""
    extra = hp.parse("\n".join(hp.line("maq", **m) for m in maqs).encode()) if maqs else []
    return TM.lineas(machine) + extra


def node_list(msgs, clock: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "AGMEN_CLOCK"}
    if clock is not None:
        env["AGMEN_CLOCK"] = clock
    with mock.patch.dict(os.environ, env, clear=True):
        return TM.corre("cmd_node_list", msgs)


def bloque_relojes(texto: str) -> list[str]:
    lineas = texto.splitlines()
    i = lineas.index("# relojes de las otras computadoras (respecto al de esta)")
    fin = lineas.index("# temporizadores y suscripciones de agmen.Node")
    return lineas[i + 1:fin - 1]


MEDIDO = dict(name="robot", ip="192.168.1.20", reloj=2_100_000, err=600_000, edad="1.5")
MAL = dict(name="robot", ip="192.168.1.20", reloj=-35 * MS, err=600_000, edad="0.5")
SIN = dict(name="pi", ip="192.168.1.30", sin="anterior")


class TestNodeList(unittest.TestCase):
    def test_sin_maquinas_identico(self):
        self.assertEqual(node_list(TM.lineas()), TM.ANTES_NODES)
        self.assertEqual(node_list(TM.lineas(), "real"), TM.ANTES_NODES)

    def test_medido_y_sin_medir(self):
        out = node_list(con_maq(MEDIDO, SIN))
        self.assertEqual(bloque_relojes(out), ["pi: reloj sin medir (el otro lado tiene un Agmen anterior, que no "
                                               "mide el reloj)", "robot: reloj +2.1 ms (±0.6)"])
        # la tabla de arriba y las cuentas de abajo, como antes (con la columna computadora)
        self.assertTrue(out.startswith("nodo   computadora  pid"))
        self.assertTrue(out.endswith("ia    principal  ciclo  50 veces  a tiempo\n"))

    def test_mal_en_sesion_real(self):
        (linea,) = bloque_relojes(node_list(con_maq(MAL), "real"))
        esperado = "⚠ robot: reloj -35.0 ms (±0.6) — más de 10 ms: revisa la hora de las dos"
        self.assertTrue(linea.startswith(esperado), linea)
        self.assertIn("chrony", linea)
        self.assertIn("w32tm /resync", linea)

    def test_sin_saber_el_modo_no_marca(self):
        (linea,) = bloque_relojes(node_list(con_maq(MAL)))
        self.assertFalse(linea.startswith("⚠"), linea)
        self.assertIn("si la sesión usa el reloj real", linea)
        self.assertNotIn("no afecta", linea)

    def test_sim_y_replay(self):
        for modo, nota in (("sim", "(no afecta en sim)"), ("replay", "(no afecta en reproducción)")):
            with self.subTest(modo=modo):
                (linea,) = bloque_relojes(node_list(con_maq(MAL), modo))
                self.assertEqual(linea, f"robot: reloj -35.0 ms (±0.6) {nota}")

    def test_red_lenta_no_afirma(self):
        lenta = dict(MAL, err=15 * MS)
        (linea,) = bloque_relojes(node_list(con_maq(lenta), "real"))
        self.assertFalse(linea.startswith("⚠"), linea)
        self.assertIn("demasiado lenta", linea)

    def test_maq_raras_quedan_sin_medir(self):
        rara = dict(name="robot", ip="192.168.1.20", reloj="x", err="1")
        (linea,) = bloque_relojes(node_list(con_maq(rara), "real"))
        self.assertEqual(linea, "robot: reloj sin medir (el hub no recibió la medición)")   # nunca 0 ms (C6)


class TestNodeListConUnHubDeVerdad(TM.TR.Base):
    """El hub en modo red recibe `relojes` del puente (aquí, a mano) y agmen node list lo muestra."""

    def test_muestra_la_linea_del_reloj(self):
        h = self.hub("127.0.0.6")
        h.keep(hp.encode("red", peers="robot@127.0.0.4", relojes="robot:2100000:600000:500"))
        self.assertTrue(TM.TR.until(lambda: h.hub.relojes))
        pub = self.open(h, TM.pubsub._PurePublisher, TM.TR.Chico, "/x")
        self.assertTrue(TM.TR.until(lambda: h.hub.endpoints))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"AGMEN_HUB": h.addr}), contextlib.redirect_stdout(out):
            topic.cmd_node_list(SimpleNamespace())
        self.assertIn("\n# relojes de las otras computadoras (respecto al de esta)\nrobot: reloj +2.1 ms (±0.6)\n",
                      out.getvalue())
        self.assertIsNotNone(pub)


def con_modo(msgs: list, modo: str) -> list:
    """Las mismas líneas, con `modo` en la línea `state` (como la da un hub en modo red al que el puente se lo dijo)."""
    return [(op, {**f, "modo": modo} if op == "state" else f) for op, f in msgs]


class TestNodeListConElModoDelHub(unittest.TestCase):
    """§6.2: el modo de la sesión lo dice el hub (`modo` en `state`); AGMEN_CLOCK ya no decide si el hub lo dice."""

    def test_los_cuatro_casos(self):
        bien = "robot: reloj +2.1 ms (±0.6)"
        # real y mal: ⚠ con el qué hacer (aunque AGMEN_CLOCK diga otra cosa: manda el hub)
        for clock in (None, "sim"):
            with self.subTest(caso="real mal", clock=clock):
                (linea,) = bloque_relojes(node_list(con_modo(con_maq(MAL), "real"), clock))
                self.assertTrue(linea.startswith("⚠ robot: reloj -35.0 ms (±0.6) — más de 10 ms: revisa la hora"),
                                linea)
                self.assertIn("chrony", linea)
        with self.subTest(caso="real bien"):
            self.assertEqual(bloque_relojes(node_list(con_modo(con_maq(MEDIDO), "real"), "sim")), [bien])
        for modo, nota in (("sim", "(no afecta en sim)"), ("replay", "(no afecta en reproducción)")):
            with self.subTest(caso=modo):
                (linea,) = bloque_relojes(node_list(con_modo(con_maq(MAL), modo), "real"))
                self.assertEqual(linea, f"robot: reloj -35.0 ms (±0.6) {nota}")
        with self.subTest(caso="sin modo"):   # hub anterior, sin agmen run o caducado: el texto condicional
            (linea,) = bloque_relojes(node_list(con_maq(MAL)))
            self.assertFalse(linea.startswith("⚠"), linea)
            self.assertIn("si la sesión usa el reloj real", linea)
            self.assertNotIn("no afecta", linea)

    def test_modo_desconocido_no_es_real_ni_sim(self):
        for clock in (None, "real", "sim"):
            with self.subTest(clock=clock):
                (linea,) = bloque_relojes(node_list(con_modo(con_maq(MAL), "marciano"), clock))
                self.assertFalse(linea.startswith("⚠"), linea)
                self.assertIn("si la sesión usa el reloj real", linea)
                self.assertNotIn("no afecta", linea)

    def test_sin_modo_del_hub_se_usa_agmen_clock(self):
        (linea,) = bloque_relojes(node_list(con_maq(MAL), "real"))
        self.assertTrue(linea.startswith("⚠"), linea)

    def test_sin_maquinas_identico_con_modo(self):
        self.assertEqual(node_list(con_modo(TM.lineas(), "real")), TM.ANTES_NODES)


class TestNodeListModoConUnHubDeVerdad(TM.TR.Base):
    """El puente (aquí, a mano) dice `modo=real` en `red`: el hub lo da en `state` y node list marca el ⚠."""

    def test_real_mal(self):
        h = self.hub("127.0.0.6")
        h.keep(hp.encode("red", peers="robot@127.0.0.4", relojes="robot:-35000000:600000:500", modo="real"))
        self.assertTrue(TM.TR.until(lambda: h.hub.relojes and h.hub.modo == "real"))
        pub = self.open(h, TM.pubsub._PurePublisher, TM.TR.Chico, "/x")
        self.assertTrue(TM.TR.until(lambda: h.hub.endpoints))
        env = {k: v for k, v in os.environ.items() if k != "AGMEN_CLOCK"}
        env["AGMEN_HUB"] = h.addr
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out):
            topic.cmd_node_list(SimpleNamespace())
        self.assertIn("\n⚠ robot: reloj -35.0 ms (±0.6) — más de 10 ms: revisa la hora", out.getvalue())
        self.assertIsNotNone(pub)


# ---------------------------------------------------------------------------
# El panel
# ---------------------------------------------------------------------------

class TestPanel(unittest.TestCase):
    EPS = [{"kind": "pub", "topic": "/imu", "node": "imu", "machine": "robot"},
           {"kind": "pub", "topic": "/aqui", "node": "a"},
           {"kind": "pub", "topic": "/dos", "node": "b"},
           {"kind": "pub", "topic": "/dos", "node": "c", "machine": "robot"},
           {"kind": "pub", "topic": "/pi", "node": "d", "machine": "pi"}]
    MACHINES = [{"name": "laptop", "here": True, "state": "aquí"},
                {"name": "robot", "here": False, "state": "conectado"},
                {"name": "pi", "here": False, "state": "conectado"}]

    def panel(self, machines=None, playing=False, clock=None):
        machines = self.MACHINES if machines is None else machines
        stub = SimpleNamespace(logs=LogBook(), nodes_info=lambda: [],
                               machines_info=lambda: [dict(m) for m in machines])
        p = Panel("127.0.0.1:0", None, "prueba", stub, None, topics=False, playing=playing, clock=clock)
        p.start()
        self.addCleanup(p.shutdown)
        p.endpoints = list(self.EPS)
        p.hub.update(topics={t: {"topic": t, "type": "X", "rate_hz": 10.0, "latency_ms": 40.0}
                             for t in ("/imu", "/aqui", "/dos", "/pi")})
        return p

    def state(self, p) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{p.port}/api/state", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_clock_de_cada_computadora(self):
        p = self.panel()
        p.clocks = {"robot": R.Estado(True, 2_100_000, 600_000, 1.5), "pi": R.Estado(False, motivo="anterior")}
        m = {x["name"]: x for x in self.state(p)["machines"]}
        self.assertNotIn("clock", m["laptop"])
        self.assertEqual(m["robot"]["clock"], {"offset_ms": 2.1, "error_ms": 0.6, "state": "ok", "why": "",
                                               "age_s": 1.5, "text": "reloj +2.1 ms (±0.6)"})
        self.assertEqual(m["pi"]["clock"]["state"], "sin_medir")
        self.assertIsNone(m["pi"]["clock"]["offset_ms"])        # nunca 0 ms (C6)
        self.assertEqual(m["pi"]["clock"]["why"], R.MOTIVOS["anterior"])
        self.assertEqual(m["robot"]["state"], "conectado")       # lo de antes sigue

    def test_sin_dato_del_hub_es_sin_medir(self):
        m = {x["name"]: x for x in self.state(self.panel())["machines"]}
        self.assertEqual(m["robot"]["clock"]["state"], "sin_medir")
        self.assertEqual(m["robot"]["clock"]["why"], R.MOTIVOS["sin_dato"])

    def test_mal_con_histeresis_y_que_hacer(self):
        p = self.panel()
        estados = []
        for dif in (5, 12, 9, 7.9, 9):
            p.clocks = {"robot": R.Estado(True, int(dif * MS), 500_000, 0.1)}
            estados.append(self.state(p)["machines"][1]["clock"]["state"])
        self.assertEqual(estados, ["ok", "mal", "mal", "ok", "ok"])
        p.clocks = {"robot": R.Estado(True, 40 * MS, 500_000, 0.1)}
        c = self.state(p)["machines"][1]["clock"]
        self.assertEqual(c["state"], "mal")
        self.assertIn("adelantado", c["why"])
        self.assertIn("chrony", c["why"])

    def test_en_sim_y_replay_no_esta_mal(self):
        from agmen import clock as ck
        for kw, nota in (({"playing": True}, "no afecta en reproducción"),
                         ({"clock": SimpleNamespace(info=lambda: {"mode": ck.SIM})}, "no afecta en sim")):
            with self.subTest(kw=list(kw)):
                p = self.panel(**kw)
                p.clocks = {"robot": R.Estado(True, 40 * MS, 500_000, 0.1)}
                c = self.state(p)["machines"][1]["clock"]
                self.assertEqual((c["state"], c["why"], c["offset_ms"]), ("ok", nota, 40.0))

    def test_latencias(self):
        p = self.panel()
        p.clocks = {"robot": R.Estado(True, 35 * MS, 500_000, 0.1), "pi": R.Estado(False, motivo="vieja")}
        t = self.state(p)["stats"]["topics"]
        self.assertEqual((t["/imu"]["latency_ms"], t["/imu"]["latency_raw_ms"], t["/imu"]["latency_fix"]),
                         (5.0, 40.0, "corregida"))
        self.assertIn("robot", t["/imu"]["latency_note"])
        self.assertEqual((t["/pi"]["latency_ms"], t["/pi"]["latency_fix"]), (40.0, "sin_corregir"))
        self.assertIn("sin medir", t["/pi"]["latency_note"])
        self.assertEqual((t["/dos"]["latency_ms"], t["/dos"]["latency_fix"]), (40.0, "sin_corregir"))
        self.assertIn("varias computadoras", t["/dos"]["latency_note"])
        self.assertEqual(t["/aqui"]["latency_ms"], 40.0)
        self.assertFalse({"latency_fix", "latency_note", "latency_raw_ms"} & set(t["/aqui"]))

    def test_latencias_en_replay_no_se_tocan(self):
        p = self.panel(playing=True)
        p.clocks = {"robot": R.Estado(True, 35 * MS, 500_000, 0.1)}
        t = self.state(p)["stats"]["topics"]
        self.assertEqual(t["/imu"]["latency_ms"], 40.0)
        self.assertNotIn("latency_fix", t["/imu"])

    def test_sin_maquinas_nada_nuevo(self):
        p = self.panel(machines=[])
        p.clocks = {"robot": R.Estado(True, 35 * MS, 500_000, 0.1)}   # (aunque el hub dijera algo)
        s = self.state(p)
        self.assertEqual(s["machines"], [])
        self.assertEqual(set(s["stats"]["topics"]["/imu"]), {"topic", "type", "rate_hz", "latency_ms",
                                                            "publishers_at", "subscribers_at"})
        self.assertEqual(s["stats"]["topics"]["/imu"]["latency_ms"], 40.0)

    def test_lee_las_maq_del_hub(self):
        p = self.panel()
        msgs = con_maq(MEDIDO, SIN)
        with mock.patch("agmen.hub.query", lambda *a, **k: msgs):
            p.stop.wait = lambda t, _n=[0]: _n.__setitem__(0, _n[0] + 1) or _n[0] > 1
            p._poll_rhythm()
        self.assertEqual(set(p.clocks), {"robot", "pi"})
        self.assertTrue(p.clocks["robot"].medido)

    def test_la_pagina_pone_el_reloj_como_texto(self):
        html = (Path(__file__).parent / "agmen" / "panel" / "index.html").read_text(encoding="utf-8")
        bloque = html[html.index("if (m.clock) {"):]
        bloque = bloque[:bloque.index("box.appendChild(wrap);")]
        self.assertIn("span.textContent = ", bloque)
        self.assertIn("why.textContent = ", bloque)
        self.assertNotIn("innerHTML", bloque)
        self.assertIn('s.latency_fix === "sin_corregir"', html)


# ---------------------------------------------------------------------------
# agmen record
# ---------------------------------------------------------------------------

class TestGrabacion(TREC.HubCase):
    def red(self, relojes_: str):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(hp.encode("red", peers="robot@192.168.1.20,pi@192.168.1.30", relojes=relojes_),
                     ("127.0.0.1", self.hub.sock.getsockname()[1]))
        finally:
            s.close()
        assert TM.TR.until(lambda: self.hub.relojes.get("robot", (None,))[0] is not None
                           and relojes_.split(":")[1] == str(self.hub.relojes["robot"][0].dif_ns))

    def grabar(self, nombre, entre=None):
        path = os.path.join(self.dir, nombre)
        rec = record.Recorder(path, ["*"], quiet=True).start()
        a = self.pub(wire.ImuSample, "/imu")
        self.wait_subscribers(a)
        if entre is not None:
            self.assertTrue(TM.TR.until(lambda: rec._clocks.por))   # la muestra del principio
            entre()
        for i in range(5):
            a.publish(TREC.imu(i))
        time.sleep(0.2)
        rec.stop()
        return path

    def metadatos(self, path):
        with _mcap.Reader(path) as r:
            return list(r.metadata)

    def test_sin_otras_computadoras_no_hay_relojes(self):
        path = self.grabar("a.mcap")
        meta = self.metadatos(path)
        self.assertEqual([n for n, _ in meta], ["agmen.grabacion", "agmen.perdidos"])   # como siempre
        self.assertNotIn("relojes", meta[0][1])
        self.assertNotIn("reloj de", TREC.TestPerfilEnLaGrabacion.info(self, path))

    def test_con_relojes(self):
        self.red("robot:2100000:600000:500,pi:-:anterior:-")
        path = self.grabar("b.mcap", entre=lambda: self.red("robot:3500000:400000:500,pi:-:anterior:-"))
        meta = self.metadatos(path)
        self.assertEqual([n for n, _ in meta], ["agmen.grabacion", "agmen.grabacion", "agmen.perdidos"])
        primero, ultimo = meta[0][1], meta[1][1]
        self.assertNotIn("relojes", primero)
        self.assertEqual({k: v for k, v in ultimo.items() if k != "relojes"}, primero)   # lo demás, igual
        d = json.loads(ultimo["relojes"])
        self.assertEqual(set(d), {"robot"})                     # "pi" sin medir no es una muestra
        r = d["robot"]
        self.assertEqual((r["inicio_ns"], r["fin_ns"], r["min_ns"], r["max_ns"], r["error_ns"]),
                         (2_100_000, 3_500_000, 2_100_000, 3_500_000, 600_000))
        self.assertGreaterEqual(r["muestras"], 2)
        self.assertEqual(json.loads(record.describe(path)["recording"]["relojes"]), d)
        info = TREC.TestPerfilEnLaGrabacion.info(self, path)
        self.assertIn("  reloj de robot: +2.1 ms al empezar, +3.5 ms al terminar (mín +2.1 ms, máx +3.5 ms, "
                      "±0.6 ms, ", info)

    def test_grabacion_de_antes_se_lee_igual(self):
        path = os.path.join(self.dir, "vieja.mcap")
        w = _mcap.Writer(path, profile=record.PROFILE, library="agmen 0.15.0")
        w.add_metadata("agmen.grabacion", {"version": "0.15.0", "computadora": "pc", "reloj": "real"})
        w.add_metadata("agmen.perdidos", {"total": "0"})
        w.close()
        self.assertEqual(record.describe(path)["recording"], {"version": "0.15.0", "computadora": "pc",
                                                             "reloj": "real"})
        self.assertNotIn("reloj de", TREC.TestPerfilEnLaGrabacion.info(self, path))


class TestTextosGrabacion(unittest.TestCase):
    def test_salto_se_delata(self):
        r = R.Resumen()
        for dif in (1 * MS, 30 * MS, 2 * MS):
            r.poner({"robot": R.Estado(True, dif, 500_000, 0.1)})
        lineas = R.lineas_grabacion(r.texto())
        self.assertEqual(lineas[0], "reloj de robot: +1.0 ms al empezar, +2.0 ms al terminar (mín +1.0 ms, "
                                    "máx +30.0 ms, ±0.5 ms, 3 muestras)")
        self.assertIn("⚠ cambió 29.0 ms durante la grabación", lineas[1])

    def test_vacio_y_mal_escrito(self):
        self.assertEqual(R.Resumen().texto(), "")
        self.assertEqual(R.Resumen().por, {})
        (linea,) = R.lineas_grabacion("{no es json")
        self.assertIn("no se puede leer", linea)
        (linea,) = R.lineas_grabacion('{"robot": {"inicio_ns": 1}}')
        self.assertIn("no se puede leer", linea)


if __name__ == "__main__":
    unittest.main()

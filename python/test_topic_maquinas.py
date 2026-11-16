"""De qué computadora es cada topic (RFC 0011 §3.5; bloque 4c §2): agmen topic list/info, agmen node list y el panel.

    python -m unittest discover -s python -p "test_topic_maquinas.py"

Sin entradas de otra computadora la salida es la de antes, byte a byte (§2.1): se compara con lo que imprimía el
código de antes del cambio para las mismas líneas del hub. Con entradas `machine=` se usa un hub de verdad en modo
red (como test_red.py). Esperas con tope; puertos elegidos por el sistema."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import test_red as TR
from agmen import hubproto as hp
from agmen import pubsub, topic
from agmen.panel.server import Panel
from agmen.run import LogBook
from agmen.tables import laptop_name


def lineas(machine: str | None = None, otro: str = "robot") -> list:
    """Unas líneas de `state` del hub, fijas (edades incluidas). Con `machine`, la segunda entrada de 'mando'
    (pid 7) es de esa computadora."""
    L = [hp.line("state", version="0.15.0", hub="127.0.0.1:7600", robot="r"),
         hp.line("topic", name="/imu", type="ImuSample", schema="00000000000000aa", text="ImuSample{f32 ax}", pubs=1,
                 subs=2, expected="required"),
         hp.line("topic", name="/cmd", type="Cmd", schema="00000000000000bb", text="Cmd{f32 v}", pubs=2, subs=0,
                 expected="no"),
         hp.line("topic", name="/vacio", type="", schema="", text="", pubs=0, subs=0, expected="required"),
         hp.line("ep", id="a", kind="pub", node="imu", pid=10, topic="/imu", type="ImuSample", addr="127.0.0.1:7001",
                 tcp="", age="0.1"),
         hp.line("ep", id="b", kind="sub", node="ia", pid=11, topic="/imu", type="ImuSample", addr="127.0.0.1:7002",
                 tcp="7702", age="0.2"),
         hp.line("ep", id="c", kind="sub", node="panel", pid=12, topic="*", type="", addr="127.0.0.1:7003", tcp="",
                 age="0.3"),
         hp.line("ep", id="d", kind="pub", node="mando", pid=13, topic="/cmd", type="Cmd", addr="127.0.0.1:7004", tcp="",
                 age="0.4"),
         hp.line("ep", id="e", kind="pub", node="mando", pid=7, topic="/cmd", type="Cmd", addr="127.0.0.1:7005", tcp="",
                 age="1.4", **({"machine": machine} if machine else {})),
         hp.line("stat", node="ia", pid=11, kind="timer", group="", name="ciclo", runs=50, skipped=0,
                 worst_late_ns=0)]
    msgs = hp.parse("\n".join(L).encode())
    if machine and machine != otro:   # un nombre que el hub no dejaría pasar: se pone a mano, tal cual
        for op, f in msgs:
            if f.get("machine") == otro:
                f["machine"] = machine
    return msgs


def corre(fn, msgs, **kw) -> str:
    out = io.StringIO()
    with mock.patch.object(topic.hubmod, "query", lambda *a, **k: msgs), contextlib.redirect_stdout(out):
        code = getattr(topic, fn)(SimpleNamespace(**kw))
    assert code == 0, code
    return out.getvalue()


# Lo que imprimía el código de antes de la 4c (commit e2faaf5) para lineas(): §2.1, byte a byte.
ANTES_LIST = ("topic   tipo       emisores  receptores\n"
              "──────  ─────────  ────────  ──────────  ────────────────────────────────────────\n"
              "/imu    ImuSample  1         2\n"
              "/cmd    Cmd        2         0           no está en robot.toml; ⚠ varios emisores\n"
              "/vacio  ?          0         0           ⚠ obligatorio y nadie lo publica\n")
ANTES_INFO_IMU = ("topic:   /imu\ntipo:    ImuSample  (schema_id 00000000000000aa)\nesquema: ImuSample{f32 ax}\n"
                  "robot.toml: declarado y obligatorio\nemisores (1):\n  imu  pid 10  127.0.0.1:7001  latió hace 0.1 s\n"
                  "receptores (2):\n  ia  pid 11  127.0.0.1:7002  latió hace 0.2 s\n"
                  "  panel  pid 12  127.0.0.1:7003  latió hace 0.3 s (escucha todos los topics)\n")
ANTES_INFO_CMD = ("topic:   /cmd\ntipo:    Cmd  (schema_id 00000000000000bb)\nesquema: Cmd{f32 v}\nemisores (2):\n"
                  "  mando  pid 13  127.0.0.1:7004  latió hace 0.4 s\n  mando  pid 7  127.0.0.1:7005  latió hace 1.4 s\n"
                  "receptores (1):\n  panel  pid 12  127.0.0.1:7003  latió hace 0.3 s (escucha todos los topics)\n")
ANTES_NODES = ("nodo   pid  publica     escucha  último latido\n"
               "─────  ───  ──────────  ───────  ─────────────\n"
               "ia     11   —           /imu     hace 0.2 s\n"
               "imu    10   /imu        —        hace 0.1 s\n"
               "mando  13   /cmd, /cmd  —        hace 0.4 s\n"
               "panel  12   —           (todos)  hace 0.3 s\n"
               "\n# temporizadores y suscripciones de agmen.Node\n"
               "nodo  grupo      qué    corrió\n"
               "────  ─────────  ─────  ────────  ────────\n"
               "ia    principal  ciclo  50 veces  a tiempo\n")


class TestSinOtrasComputadoras(unittest.TestCase):
    """§2.1: si ninguna línea ep dice `machine`, todo sale igual que antes."""

    def test_topic_list_igual_que_antes(self):
        self.assertEqual(corre("cmd_list", lineas()), ANTES_LIST)

    def test_topic_info_igual_que_antes(self):
        self.assertEqual(corre("cmd_info", lineas(), topic="/imu"), ANTES_INFO_IMU)
        self.assertEqual(corre("cmd_info", lineas(), topic="/cmd"), ANTES_INFO_CMD)

    def test_node_list_igual_que_antes(self):
        self.assertEqual(corre("cmd_node_list", lineas()), ANTES_NODES)


class TestConOtraComputadora(unittest.TestCase):
    """§2.2 y §2.3 con las líneas fijas (una entrada de 'robot')."""

    def test_topic_list_columna_publica(self):
        out = corre("cmd_list", lineas("robot")).splitlines()
        self.assertEqual(out[0].split(), ["topic", "tipo", "emisores", "receptores", "publica"])
        self.assertEqual(re.split(r"\s{2,}", out[2]), ["/imu", "ImuSample", "1", "2", "aquí"])
        self.assertEqual(re.split(r"\s{2,}", out[3]),
                         ["/cmd", "Cmd", "2", "0", "aquí, robot", "no está en robot.toml; ⚠ varios emisores"])
        self.assertEqual(re.split(r"\s{2,}", out[4]), ["/vacio", "?", "0", "0", "⚠ obligatorio y nadie lo publica"])
        self.assertEqual(out[4].index("⚠"), out[3].index("no está"))   # la columna vacía no corre los avisos

    def test_orden_aqui_primero_y_luego_alfabetico(self):
        self.assertEqual(topic._places(["robot", "brazo", "aquí", "robot", "Arriba"]), "aquí, Arriba, brazo, robot")
        self.assertEqual(topic._places(["robot"]), "robot")
        self.assertEqual(topic._places([]), "")

    def test_topic_info_dice_donde_corre_cada_uno(self):
        out = corre("cmd_info", lineas("robot"), topic="/cmd")
        self.assertIn("  mando  pid 13  127.0.0.1:7004  latió hace 0.4 s  · aquí\n", out)
        self.assertIn("  mando  pid 7  127.0.0.1:7005  latió hace 1.4 s  · en robot\n", out)
        self.assertIn("  panel  pid 12  127.0.0.1:7003  latió hace 0.3 s (escucha todos los topics)  · aquí\n", out)
        # Si el topic es solo de aquí pero hay alguna de fuera en la lista, también lo dice.
        self.assertIn("  imu  pid 10  127.0.0.1:7001  latió hace 0.1 s  · aquí\n",
                      corre("cmd_info", lineas("robot"), topic="/imu"))

    def test_node_list_columna_computadora(self):
        out = corre("cmd_node_list", lineas("robot")).splitlines()
        self.assertEqual(out[0].split(), ["nodo", "computadora", "pid", "publica", "escucha", "último", "latido"])
        filas = [re.split(r"\s{2,}", ln) for ln in out[2:7]]
        self.assertEqual(filas, [["ia", "aquí", "11", "—", "/imu", "hace 0.2 s"],
                                 ["imu", "aquí", "10", "/imu", "—", "hace 0.1 s"],
                                 ["mando", "aquí", "13", "/cmd", "—", "hace 0.4 s"],    # el mismo nombre en dos
                                 ["mando", "robot", "7", "/cmd", "—", "hace 1.4 s"],    # computadoras: dos nodos
                                 ["panel", "aquí", "12", "—", "(todos)", "hace 0.3 s"]])

    def test_un_nombre_raro_no_rompe_la_tabla(self):
        raro = "r<b>ó t|\"ñ\""
        out = corre("cmd_list", lineas(raro)).splitlines()
        self.assertIn(f"aquí, {raro}", out[3])
        self.assertEqual(out[1].split("  ")[4], "─" * len(f"aquí, {raro}"))   # la raya, del ancho de la columna
        self.assertIn(f"· en {raro}", corre("cmd_info", lineas(raro), topic="/cmd"))
        self.assertIn(raro, corre("cmd_node_list", lineas(raro)))


class TestConUnHubDeVerdad(TR.Base):
    """§2.6: un hub de verdad en modo red, con un Publisher de aquí y una entrada `machine=robot` (como el puente)."""

    def test_topic_list_info_y_node_list(self):
        h = self.hub("127.0.0.6")   # con AGMEN_NET_IP: acepta una 127.x como peer (como test_red.py)
        h.red("robot@127.0.0.4")
        sub = self.open(h, pubsub._PureSubscriber, TR.Chico, "/x")
        pub = self.open(h, pubsub._PurePublisher, TR.Chico, "/x")
        h.keep(hp.encode("pub", id="r1", node="motor", pid=7, topic="/x", schema=f"{TR.Chico.SCHEMA_ID:016x}",
                         type=TR.Chico.NAME, machine="robot"))
        self.assertTrue(TR.until(lambda: "r1" in h.hub.endpoints and len(h.hub.endpoints) >= 3), h.logs)
        with mock.patch.dict(os.environ, {"AGMEN_HUB": h.addr}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                topic.cmd_list(SimpleNamespace())
                topic.cmd_info(SimpleNamespace(topic="/x"))
                topic.cmd_node_list(SimpleNamespace())
        text = out.getvalue()
        lines = text.splitlines()
        self.assertEqual(lines[0].split(), ["topic", "tipo", "emisores", "receptores", "publica"])
        self.assertEqual(re.split(r"\s{2,}", lines[2])[:5], ["/x", TR.Chico.NAME, "2", "1", "aquí, robot"])
        self.assertRegex(text, r"\n  motor  pid 7  .*  · en robot\n")
        self.assertRegex(text, rf"\n  .+  pid {os.getpid()}  .*  · aquí\n")
        self.assertRegex(text, r"\nmotor\s+robot\s+7\s+/x\s+—")
        self.assertIsNotNone(pub)
        self.assertIsNotNone(sub)


class TestPanel(unittest.TestCase):
    """§2.4: publishers_at / subscribers_at en /api/state y la etiqueta en la página."""

    EPS = [{"kind": "pub", "topic": "/imu", "node": "imu"},
           {"kind": "pub", "topic": "/imu", "node": "imu2", "machine": "robot"},
           {"kind": "sub", "topic": "/imu", "node": "ia"},
           {"kind": "sub", "topic": "*", "node": "panel"},               # (escucha todo: no cuenta)
           {"kind": "pub", "topic": "/cmd", "node": "mando"},
           {"kind": "sub", "topic": "/cmd", "node": "motor", "machine": "robot"},
           {"kind": "sub", "topic": "/cmd", "node": "motor2", "machine": "robot"},
           {"kind": "sub", "topic": "/cmd", "node": "brazo", "machine": "Brazo"}]

    def panel(self, machines, **launcher):
        stub = SimpleNamespace(logs=LogBook(), nodes_info=lambda: [], machines_info=lambda: machines, **launcher)
        p = Panel("127.0.0.1:0", None, "prueba", stub, None, topics=False)
        p.start()
        self.addCleanup(p.shutdown)
        p.endpoints = list(self.EPS)
        p.hub.update(topics={"/imu": {"topic": "/imu", "type": "ImuSample", "rate_hz": 10.0},
                             "/cmd": {"topic": "/cmd", "type": "Cmd", "rate_hz": 5.0},
                             "/nuevo": {"topic": "/nuevo", "type": "X", "rate_hz": 1.0}})
        with urllib.request.urlopen(f"http://127.0.0.1:{p.port}/api/state", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))["stats"]["topics"]

    def test_con_machine_local(self):
        t = self.panel([{"name": "laptop", "here": True}, {"name": "robot", "here": False}])
        self.assertEqual((t["/imu"]["publishers_at"], t["/imu"]["subscribers_at"]), (["laptop", "robot"], ["laptop"]))
        self.assertEqual((t["/cmd"]["publishers_at"], t["/cmd"]["subscribers_at"]), (["laptop"], ["Brazo", "robot"]))
        self.assertEqual((t["/nuevo"]["publishers_at"], t["/nuevo"]["subscribers_at"]), ([], []))
        self.assertEqual(t["/imu"]["type"], "ImuSample")   # lo de antes sigue

    def test_sin_machine_local_usa_el_nombre_de_desde(self):
        config = SimpleNamespace(machines=[SimpleNamespace(name="robot")])
        places = {"robot": SimpleNamespace(here=False)}
        t = self.panel([{"name": "robot", "here": False}], config=config, places=places)
        aqui = laptop_name(config, places)
        self.assertEqual(t["/cmd"]["publishers_at"], [aqui])
        self.assertEqual(t["/imu"]["publishers_at"], [aqui, "robot"] if aqui != "robot" else ["robot"])

    def test_sin_machine_las_listas_van_vacias(self):
        t = self.panel([])
        for v in t.values():
            self.assertEqual((v["publishers_at"], v["subscribers_at"]), ([], []))

    def test_un_nombre_raro_llega_tal_cual(self):
        raro = "<img src=x onerror=alert(1)>"
        self.EPS = self.EPS + [{"kind": "pub", "topic": "/cmd", "node": "x", "machine": raro}]
        t = self.panel([{"name": "laptop", "here": True}])
        self.assertEqual(t["/cmd"]["publishers_at"], ["laptop", raro])

    def test_la_pagina_pone_los_nombres_como_texto(self):
        html = (Path(__file__).parent / "agmen" / "panel" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function placeOf(t)", html)
        bloque = html[html.index("const where = placeOf(topics[t]);"):]
        bloque = bloque[:bloque.index("b.appendChild(w);")]
        self.assertIn("w.textContent = ", bloque)
        self.assertNotIn("innerHTML", bloque)
        self.assertNotIn("placeOf", html[html.index("b.innerHTML = `${esc(t)}"):].split("\n")[0])


if __name__ == "__main__":
    unittest.main()

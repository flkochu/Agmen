"""Los nombres de las articulaciones declarados al hub (RFC 0013 §3.4, D6 = d).

    python -m unittest discover -s python -p "test_nombres.py"

Quien trae su propia lista (una política, un puente) la pasa con names=[...] al publicar o al suscribirse;
va al hub en el campo `names` de la línea de registro (`pub`/`sub`), y el hub la compara con la de
[[joints]] de robot.toml (que le pasa agmen run con --names) y con la de los demás endpoints del topic.

Con el núcleo de C++ (agmen._core) la lista llega igual: el registro lo arma C++ con el mismo campo (las pruebas
del núcleo de C++ están en TestNucleoCppValidaPrimero y TestNucleoCpp). Las pruebas del camino de Python puro
usan pubsub._PurePublisher y _PureSubscriber directo, así corren con los dos núcleos. Nada espera un tiempo fijo:
se espera cada condición, con tope.
"""

import contextlib
import functools
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import agmen
from agmen import _backend, pubsub, wire
from agmen import hub as H
from agmen import hubproto as hp
from agmen import tables as T
from agmen.msgs import Imagen, JointCommand, JointState
from agmen.run import ConfigError, load_config, prepare_hub

ROOT = Path(__file__).resolve().parent.parent
TOPE_S = 10.0
PATAS = ("FR_hip", "FR_thigh", "FR_calf", "FL_hip", "FL_thigh", "FL_calf",
         "RR_hip", "RR_thigh", "RR_calf", "RL_hip", "RL_thigh", "RL_calf")
CMD12 = JointCommand[12]
LOCAL = ("127.0.0.1", 50000)
T12 = CMD12.SCHEMA_TEXT
S12 = f"{CMD12.SCHEMA_ID:016x}"


def until(cond, tope=TOPE_S, every=None):
    end = time.monotonic() + tope
    while time.monotonic() < end:
        if cond():
            return True
        if every is not None:
            every()
        else:
            time.sleep(0.01)
    return cond()


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def joints_del_rfc(require=False, extra=()):
    """Lo que agmen run le pasa al hub para el [[joints]] del RFC."""
    names = [f"/patas/cmd={','.join(PATAS)}", f"/patas/estado={','.join(PATAS)}", *extra]
    return H.parse_names_args(names, ["/patas/cmd"] if require else [])


class HubEnMemoria(unittest.TestCase):
    """El Hub sin hilos ni red: lo que contesta se guarda en self.sent y lo que dice en self.logs."""

    def nuevo_hub(self, joints=None, cls=H.Hub):
        self.logs: list[str] = []
        self.hub = cls(f"127.0.0.1:{free_port()}", log=self.logs.append, grace_s=0.0, joints=joints) \
            if cls is H.Hub else cls(f"127.0.0.1:{free_port()}", log=self.logs.append, grace_s=0.0)
        self.addCleanup(self.hub.sock.close)
        self.sent: list[bytes] = []
        self.hub._send = lambda data, addr: self.sent.append(data)
        return self.hub

    def manda_bytes(self, data: bytes):
        self.sent.clear()
        self.hub.handle(data, LOCAL, 0.0)
        return [hp.parse(d)[0] for d in self.sent]

    def manda(self, op, **fields):
        return self.manda_bytes(hp.encode(op, **fields))

    def pub(self, eid, names=None, topic="/patas/cmd", text=T12, schema=S12, tipo="JointCommand", node=None):
        f = dict(id=eid, node=node or eid, pid=1, topic=topic, schema=schema, type=tipo, text=text)
        if names is not None:
            f["names"] = names if isinstance(names, str) else ",".join(names)
        return self.manda("pub", **f)

    def sub(self, eid, names=None, topic="/patas/cmd", schema=S12, tipo="JointCommand", node=None):
        f = dict(id=eid, node=node or eid, pid=1, topic=topic, schema=schema, type=tipo, port=6000)
        if names is not None:
            f["names"] = names if isinstance(names, str) else ",".join(names)
        return self.manda("sub", **f)

    def rechazo(self, respuestas) -> str:
        (op, f), = respuestas
        self.assertEqual(op, "error", respuestas)
        return f["msg"]

    def entra(self, respuestas):
        self.assertTrue(respuestas, "el hub no contestó")
        self.assertNotEqual(respuestas[0][0], "error", respuestas)

    def avisos(self, frag: str) -> list[str]:
        return [x for x in self.logs if frag in x]


# --- el formato: la línea de registro (es el contrato) -------------------------------------------------------------

class FalsoHub:
    """Un socket que hace de hub: guarda lo que le llega (para ver los bytes del registro)."""

    def __init__(self, case):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(TOPE_S)
        case.addCleanup(self.sock.close)
        patch = mock.patch.dict(os.environ, {"AGMEN_HUB": f"127.0.0.1:{self.sock.getsockname()[1]}"})
        patch.start()
        case.addCleanup(patch.stop)

    def vaciar(self) -> None:
        """Tira lo que ya llegó y nadie leyó (el registro o el latido de un endpoint de antes, que puede llamarse
        igual que el siguiente): lo que se lea después es de quien se cree después."""
        self.sock.setblocking(False)
        try:
            while True:
                self.sock.recvfrom(65536)
        except (BlockingIOError, ConnectionResetError):
            pass
        finally:
            self.sock.settimeout(TOPE_S)

    def registro(self, op: str, node: str | None = None) -> bytes:
        """La siguiente línea `op` que llegue; con `node`, la siguiente de ese nodo (las de otro nodo se saltan: el
        latido de un Subscriber de antes que alcanzó a salir antes de cerrarse)."""
        while True:
            data, _ = self.sock.recvfrom(65536)
            if not data.startswith(f"{hp.PREFIX} {op} ".encode()):
                continue
            if node is None or hp.parse(data)[0][1].get("node") == node:
                return data


class TestFormato(unittest.TestCase):
    def setUp(self):
        self.err = io.StringIO()
        ctx = contextlib.redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def abrir(self, cls, *args, **kw):
        o = cls(*args, **kw)
        self.addCleanup(o.close)
        return o

    def test_pub_sin_names_es_la_linea_de_siempre_byte_a_byte(self):
        falso = FalsoHub(self)
        pub = self.abrir(pubsub._PurePublisher, CMD12, "/patas/cmd")
        esperado = (f"agmen-hub/1 pub id={pub.source_id:016x} node={hp.quote(pubsub.node_name())} pid={os.getpid()} "
                    f"topic=/patas/cmd schema={S12} type=JointCommand text={hp.quote(T12)}").encode()
        self.assertEqual(falso.registro("pub"), esperado)

    def test_pub_con_names_lo_lleva_al_final_sin_escapar(self):
        falso = FalsoHub(self)
        pub = self.abrir(pubsub._PurePublisher, CMD12, "/patas/cmd", names=list(PATAS))
        antes = (f"agmen-hub/1 pub id={pub.source_id:016x} node={hp.quote(pubsub.node_name())} pid={os.getpid()} "
                 f"topic=/patas/cmd schema={S12} type=JointCommand text={hp.quote(T12)}")
        self.assertEqual(falso.registro("pub"), (antes + " names=" + ",".join(PATAS)).encode())
        self.assertEqual(pub.names, PATAS)

    def test_sub_sin_names_y_con_names(self):
        falso = FalsoHub(self)
        sub = self.abrir(pubsub._PureSubscriber, JointState[3], "/brazo/estado")
        linea = falso.registro("sub").decode()
        port = sub.listen.rsplit(":", 1)[1]
        self.assertEqual(linea, f"agmen-hub/1 sub id={sub._link.id} node={hp.quote(pubsub.node_name())} "
                                f"pid={os.getpid()} topic=/brazo/estado schema={JointState[3].SCHEMA_ID:016x} "
                                f"type=JointState port={port} tcp=")
        sub2 = self.abrir(pubsub._PureSubscriber, JointState[3], "/brazo/estado", names=["hombro", "codo", "mano.1"])
        linea = falso.registro("sub").decode()
        port = sub2.listen.rsplit(":", 1)[1]
        self.assertTrue(linea.endswith(f" port={port} tcp= names=hombro,codo,mano.1"), linea)

    def test_raw_sin_names_es_la_linea_de_siempre_byte_a_byte(self):
        """RawSubscriber y RawPublisher (los del [[mux]]): sin names, la línea de siempre; con names, al final."""
        falso = FalsoHub(self)
        rs = self.abrir(pubsub.RawSubscriber, "/patas/cmd")
        port = rs.listen.rsplit(":", 1)[1]
        siempre = (f"agmen-hub/1 sub id={rs._link.id} node={hp.quote(pubsub.node_name())} pid={os.getpid()} "
                   f"topic=/patas/cmd schema=* type= port={port} tcp={rs.tcp_port}")
        self.assertEqual(falso.registro("sub").decode(), siempre)
        rs.close()
        falso.vaciar()
        rs2 = self.abrir(pubsub.RawSubscriber, "/patas/cmd", names=list(PATAS))
        port = rs2.listen.rsplit(":", 1)[1]
        self.assertEqual(falso.registro("sub").decode(),
                         f"agmen-hub/1 sub id={rs2._link.id} node={hp.quote(pubsub.node_name())} pid={os.getpid()} "
                         f"topic=/patas/cmd schema=* type= port={port} tcp={rs2.tcp_port} names={','.join(PATAS)}")
        rs2.close()
        falso.vaciar()
        rp = self.abrir(pubsub.RawPublisher, "/patas/cmd")
        rp.set_schema(T12, "JointCommand", CMD12.SCHEMA_ID)
        siempre = (f"agmen-hub/1 pub id={rp.source_id:016x} node={hp.quote(pubsub.node_name())} pid={os.getpid()} "
                   f"topic=/patas/cmd schema={S12} type=JointCommand text={hp.quote(T12)}")
        self.assertEqual(falso.registro("pub").decode(), siempre)
        rp.close()
        falso.vaciar()
        rp2 = self.abrir(pubsub.RawPublisher, "/patas/cmd", names=list(PATAS))
        rp2.set_schema(T12, "JointCommand", CMD12.SCHEMA_ID)
        self.assertEqual(falso.registro("pub").decode(),
                         siempre.replace(f"id={rp.source_id:016x}", f"id={rp2.source_id:016x}")
                         + f" names={','.join(PATAS)}")
        self.assertEqual(rp2.names, PATAS)

    def test_raw_names_se_revisa_al_crear(self):
        for cls in (pubsub.RawSubscriber, pubsub.RawPublisher):
            with self.subTest(cls=cls.__name__):
                with self.assertRaisesRegex(ValueError, f"{cls.__name__} en '/patas/cmd': 'a' está dos veces"):
                    cls("/patas/cmd", names=["a", "a"])
                with self.assertRaisesRegex(TypeError, "no tiene orden fijo"):
                    cls("/patas/cmd", names={"a"})
        with self.assertRaisesRegex(ValueError, "RawSubscriber: names= va con un topic"):
            pubsub.RawSubscriber("127.0.0.1:0", names=["a"])

    def test_signos_validos_no_se_escapan(self):
        self.assertEqual(hp.encode("pub", names=hp.format_joint_names(["_a", "b/c.d-1", "9"])),
                         b"agmen-hub/1 pub names=_a,b/c.d-1,9")

    def test_tablas_sin_names_es_la_linea_de_siempre(self):
        d = T.check_endpoint("pubs", {"id": "a1", "node": "n", "pid": 7, "topic": "/t", "schema": S12,
                                      "type": "JointCommand", "text": "x"})
        self.assertNotIn("names", d)
        self.assertEqual(T.hub_line("robot", "127.0.0.2", "pubs", d),
                         f"agmen-hub/1 pub id=robot.a1 node=n pid=7 topic=/t schema={S12} type=JointCommand "
                         f"text=x machine=robot".encode())
        s = T.check_endpoint("subs", {"id": "b1", "node": "n", "pid": 7, "topic": "/t", "schema": S12,
                                      "type": "JointCommand", "port": 7000, "tcp": 0})
        self.assertEqual(T.hub_line("robot", "127.0.0.2", "subs", s, ["x"]),
                         f"agmen-hub/1 sub id=robot.b1 node=n pid=7 topic=/t schema={S12} type=JointCommand "
                         f"machine=robot addr=127.0.0.2:7000 solo=x".encode())

    def test_el_estado_sin_names_es_el_de_siempre(self):
        hub = H.Hub(f"127.0.0.1:{free_port()}", log=lambda _: None, joints=joints_del_rfc())
        self.addCleanup(hub.sock.close)
        hub._send = lambda *a: None
        hub.handle(hp.encode("pub", id="a", node="n", pid=1, topic="/patas/cmd", schema=S12, type="JointCommand",
                             text=T12), LOCAL, 0.0)
        ep = next(ln for ln in hub.state_text().split("\n") if " ep " in ln)
        self.assertRegex(ep, r"^agmen-hub/1 ep id=a kind=pub node=n pid=1 topic=/patas/cmd type=JointCommand "
                             r"addr=127\.0\.0\.1:50000 tcp= age=[0-9.]+$")
        hub.handle(hp.encode("pub", id="b", node="m", pid=1, topic="/patas/cmd", schema=S12, type="JointCommand",
                             text=T12, names=",".join(PATAS)), LOCAL, 0.0)
        ep = next(ln for ln in hub.state_text().split("\n") if " ep id=b " in ln)
        self.assertTrue(ep.endswith(" names=" + ",".join(PATAS)), ep)


# --- names= al crear: se revisa en el nodo, igual con los dos núcleos ---------------------------------------------

class TestNamesAlCrear(unittest.TestCase):
    CASOS = [
        (["a" * 65], ValueError, "mide 65 caracteres y el máximo es 64"),
        ([f"articulacion_{i:04d}" for i in range(500)], ValueError, "el máximo es 8192"),
        (["a", "b", "a"], ValueError, "'a' está dos veces en names (names[0] y names[2])"),
        ([], ValueError, "names está vacía"),
        (["a b"], ValueError, "tiene un espacio"),
        (["a,b"], ValueError, "tiene una coma"),
        (["ñ"], ValueError, "tiene 'ñ'"),
        (["-a"], ValueError, "empieza con '-'"),
        (["a\x1b[31m"], ValueError, "carácter de control"),
        ([""], ValueError, "está vacío"),
        ([3], ValueError, "names[0] = 3 no es un texto"),
        ("FR_hip", TypeError, "names es una lista de nombres"),
        (12, TypeError, "llegó int"),
    ]

    def clases(self):
        out = [("puro", functools.partial(pubsub._PurePublisher), functools.partial(pubsub._PureSubscriber))]
        if _backend.NATIVE:
            out.append(("nativo", pubsub._NativePublisher, pubsub._NativeSubscriber))
        return out

    def test_listas_que_no_valen(self):
        T3 = JointState[3]
        for nucleo, Pub, Sub in self.clases():
            for names, error, frag in self.CASOS:
                for nombre, cls in (("Publisher", Pub), ("Subscriber", Sub)):
                    with self.subTest(nucleo=nucleo, cls=nombre, names=repr(names)[:40]):
                        tipo = JointState[len(names)] if isinstance(names, list) and 1 <= len(names) else T3
                        with self.assertRaises(error) as ctx:
                            cls(tipo, "/patas/estado", names=names)
                        self.assertIn(frag, str(ctx.exception))
                        self.assertIn(f"{nombre} en '/patas/estado'", str(ctx.exception))
                        self.assertNotIn("\x1b", str(ctx.exception))

    def test_names_sin_orden_no_vale_y_con_orden_si(self):
        """Un set, un frozenset o un dict no tienen un orden que sirva, y el orden es justo lo que se compara:
        TypeError claro, con los dos núcleos y en Node.on. Lista, tupla y otras secuencias con orden siguen valiendo."""
        class Secuencia:   # una secuencia con orden que no es lista ni tupla
            def __iter__(self):
                return iter(["a", "b"])

        T2 = JointState[2]
        for nucleo, Pub, Sub in self.clases():
            for nombre, cls in (("Publisher", Pub), ("Subscriber", Sub)):
                for names in ({"a", "b"}, frozenset({"a", "b"}), {"a": 1, "b": 2}, {"a": 1, "b": 2}.keys()):
                    with self.subTest(nucleo=nucleo, cls=nombre, names=type(names).__name__):
                        with self.assertRaises(TypeError) as ctx:
                            cls(T2, "/patas/estado", names=names)
                        texto = str(ctx.exception)
                        self.assertIn(f"{nombre} en '/patas/estado': names debe ser una lista o una tupla, en el "
                                      f"orden de los arreglos: un {type(names).__name__} no tiene orden fijo y el "
                                      f"orden es justo lo que el hub compara", texto)
                with self.subTest(nucleo=nucleo, cls=nombre, names="str"):
                    with self.assertRaises(TypeError):
                        cls(T2, "/patas/estado", names="ab")
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    for names in (["a", "b"], ("a", "b"), Secuencia(), iter(["a", "b"])):
                        with self.subTest(nucleo=nucleo, cls=nombre, ok=type(names).__name__):
                            o = cls(T2, "/patas/estado", names=names)
                            self.assertEqual(o.names, ("a", "b"))
                            o.close()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            node = agmen.Node("n_set")
        self.addCleanup(node.close)
        with self.assertRaisesRegex(TypeError, "un set no tiene orden fijo"):
            node.on(T2, "/patas/estado", lambda s: None, names={"a", "b"})
        with self.assertRaisesRegex(TypeError, "un set no tiene orden fijo"):
            node.publisher(T2, "/patas/cmd", names={"a", "b"})

    def test_el_numero_del_tipo(self):
        for nucleo, Pub, Sub in self.clases():
            for cls in (Pub, Sub):
                with self.subTest(nucleo=nucleo, cls=cls):
                    with self.assertRaises(ValueError) as ctx:
                        cls(JointCommand[12], "/patas/cmd", names=list(PATAS[:10]))
                    self.assertIn("names tiene 10 nombres y JointCommand es de 12 articulaciones (f32[12] position)",
                                  str(ctx.exception))

    def test_un_tipo_sin_numero_manda_la_lista_igual(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            p = pubsub._PurePublisher(wire.ImuSample, "/x", names=["a", "b", "c"])
        try:
            self.assertEqual(p.names, ("a", "b", "c"))
        finally:
            p.close()

    def test_el_registro_con_la_lista_debe_caber_en_un_datagrama(self):
        class Grande(wire.Message):
            SCHEMA = "Grande{" + ";".join(f"f32 campo_{i:05d}" for i in range(2900)) + "}"

        nombres = [f"n{i}" for i in range(1500)]
        with self.assertRaises(ValueError) as ctx:
            pubsub._PurePublisher(Grande, "/grande", names=nombres)
        self.assertIn("el registro en el hub mide 6", str(ctx.exception))
        self.assertIn("en un datagrama caben 65507", str(ctx.exception))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            pubsub._PurePublisher(Grande, "/grande", names=["a"]).close()   # con una lista corta, sí cabe

    def test_con_direccion_fija_no_hay_hub(self):
        with self.assertRaisesRegex(ValueError, "names= va con un topic"):
            pubsub._PurePublisher(JointState[1], "127.0.0.1:7999", names=["a"])
        with self.assertRaisesRegex(ValueError, "names= va con un topic"):
            pubsub._PureSubscriber(JointState[1], "*", names=["a"])

    def test_el_mismo_tope_en_robot_toml(self):
        base = '[robot]\nname = "r"\n\n[[node]]\nname = "m"\ncmd = "x"\n\n[[joints]]\nname = "g"\nstate = "/g/e"\n'
        casos = [(json.dumps(["a" * 65]), "names[0] = 'aaaa", "mide 65 caracteres y el máximo es 64"),
                 (json.dumps([f"articulacion_{i:04d}" for i in range(500)]),
                  "names ocupa 8999 caracteres (los 500 nombres separados por comas) y el máximo es 8192",
                  "usa nombres más cortos o reparte las articulaciones en varios grupos")]
        for valor, *frags in casos:
            with self.subTest(valor=valor[:30]), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "robot.toml"
                path.write_text(base + f"names = {valor}\n", encoding="utf-8")
                with self.assertRaises(ConfigError) as ctx:
                    load_config(path)
                for frag in frags:
                    self.assertIn(frag, str(ctx.exception))
                self.assertIn("[[joints]] 'g'", str(ctx.exception))

    @unittest.skipUnless(_backend.NATIVE, "solo con el núcleo de C++")
    def test_con_el_nucleo_de_cpp_tambien(self):
        """Con el núcleo de C++, names= ya funciona (antes daba NotImplementedError): la lista llega al hub."""
        falso = FalsoHub(self)
        for cls, op in ((agmen.Publisher, "pub"), (agmen.Subscriber, "sub")):
            with self.subTest(cls=cls):
                o = cls(CMD12, "/patas/cmd", names=list(PATAS))
                self.addCleanup(o.close)
                self.assertEqual(o.names, PATAS)
                self.assertTrue(falso.registro(op).endswith(b" names=" + ",".join(PATAS).encode()))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            node = agmen.Node("n")
        self.addCleanup(node.close)
        p = node.publisher(CMD12, "/patas/cmd", names=list(PATAS))
        self.assertEqual(p.names, PATAS)
        self.assertTrue(falso.registro("pub").endswith(b" names=" + ",".join(PATAS).encode()))
        node.on(CMD12, "/patas/cmd", lambda s: None, names=list(PATAS))
        self.assertTrue(falso.registro("sub").endswith(b" names=" + ",".join(PATAS).encode()))
        with self.assertRaisesRegex(ValueError, "está dos veces"):   # revisada antes, igual que en Python puro
            node.on(JointState[2], "/x", lambda s: None, names=["a", "a"])


# --- el hub ----------------------------------------------------------------------------------------------------------

class TestHubCompara(HubEnMemoria):
    def setUp(self):
        self.nuevo_hub(joints_del_rfc())

    def test_la_misma_lista_entra(self):
        self.entra(self.pub("politica", PATAS))
        self.entra(self.sub("driver", PATAS))
        self.assertEqual(self.hub.endpoints["politica"].names, PATAS)
        self.assertEqual(self.avisos("sin declarar"), [])

    def test_el_orden_cambiado_se_rechaza_como_dice_el_rfc(self):
        otro = ("FL_hip", "FL_thigh", "FL_calf", "FR_hip", "FR_thigh", "FR_calf") + PATAS[6:]
        msg = self.rechazo(self.pub("politica", otro))
        self.assertIn("'/patas/cmd': 'politica' declara FL_hip, FL_thigh… y robot.toml dice FR_hip, FR_thigh…", msg)
        self.assertIn("cambian desde la articulación 1 de 12", msg)
        self.assertIn("pasa names= en el orden de [[joints]] de robot.toml", msg)
        self.assertNotIn("politica", self.hub.endpoints)
        # Un Subscriber con otra lista, también.
        self.assertIn("'driver' declara FL_hip", self.rechazo(self.sub("driver", otro)))

    def test_la_diferencia_en_medio_y_largos_distintos(self):
        otro = PATAS[:5] + ("X",) + PATAS[6:]
        self.assertIn("declara …, X, RR_hip… y robot.toml dice …, FL_calf, RR_hip… (cambian desde la articulación 6 "
                      "de 12)", self.rechazo(self.pub("p", otro, text="")))
        corta = PATAS[:11]
        msg = self.rechazo(self.pub("p", corta, text=""))
        self.assertIn("declara … (ahí termina) y robot.toml dice …, RL_calf", msg)
        self.assertIn("(cambian desde la articulación 12; 11 nombres contra 12)", msg)
        self.assertEqual(msg.count("'p'"), 1, msg)     # el nombre del nodo no se repite en el paréntesis

    def test_otra_lista_de_otro_nodo_se_rechaza_nombrando_al_otro(self):
        self.nuevo_hub()   # sin robot.toml: el hub compara entre endpoints
        self.entra(self.pub("politica", PATAS, node="politica"))
        otro = PATAS[1:] + PATAS[:1]
        msg = self.rechazo(self.sub("sim", otro, node="simulador"))
        self.assertIn("'/patas/cmd': 'simulador' declara FR_thigh, FR_calf… y 'politica', que ya está en el topic, "
                      "declara FR_hip, FR_thigh…", msg)
        self.assertIn("un topic, una lista", msg)
        self.entra(self.sub("echo"))             # sin lista: entra igual
        self.manda("bye", id="politica")
        self.entra(self.sub("sim", otro, node="simulador"))   # ya no hay con quién chocar

    def test_topic_que_no_es_de_joints_con_dos_listas(self):
        self.entra(self.pub("a", ["x", "y"], topic="/otro", text="", schema="00000000000000d1", tipo="Mio"))
        msg = self.rechazo(self.pub("b", ["x", "z"], topic="/otro", text="", schema="00000000000000d1", tipo="Mio"))
        self.assertIn("'/otro': 'b' declara …, z y 'a', que ya está en el topic, declara …, y", msg)
        self.entra(self.pub("c", ["x", "y"], topic="/otro", text="", schema="00000000000000d1", tipo="Mio"))

    def test_sin_lista_publicando_avisa_una_vez_por_endpoint(self):
        for _ in range(3):                         # cada latido vuelve a registrarse
            self.entra(self.pub("viejo"))
        avisos = self.avisos("sin declarar su lista de nombres")
        self.assertEqual(len(avisos), 1, self.logs)
        self.assertIn("'viejo' publica en '/patas/cmd', un topic de [[joints]], sin declarar su lista de nombres", avisos[0])
        self.assertIn("FR_hip, FR_thigh… (12 nombres)", avisos[0])
        self.assertIn("Pasa names=[...] al crear el Publisher", avisos[0])
        self.entra(self.pub("viejo2", topic="/patas/estado", text="", schema="00000000000000e1", tipo="JointState"))
        self.assertEqual(len(self.avisos("sin declarar su lista de nombres")), 2)

    def test_suscriptor_sin_lista_entra_sin_aviso(self):
        for _ in range(2):
            self.entra(self.sub("echo"))
            self.entra(self.sub("grabadora", topic="*", schema="*", tipo=""))
        self.assertEqual([x for x in self.logs if x.startswith("⚠")], [])

    def test_require_names_rechaza_al_que_publica_el_comando_sin_lista(self):
        self.nuevo_hub(joints_del_rfc(require=True))
        msg = self.rechazo(self.pub("viejo"))
        self.assertIn("'/patas/cmd': 'viejo' publica sin declarar su lista de nombres y robot.toml la exige "
                      "(require_names = true en [[joints]])", msg)
        self.assertIn("Pasa names=[...] al crear el Publisher, en el orden de [[joints]]: FR_hip, FR_thigh… "
                      "(12 nombres)", msg)
        self.entra(self.pub("politica", PATAS))
        self.entra(self.sub("echo"))                                        # un suscriptor sin lista, sí
        self.entra(self.pub("estado", topic="/patas/estado", text="", schema="00000000000000e1",
                            tipo="JointState"))                           # el estado no lo exige

    def test_el_numero_del_tipo_distinto_avisa_una_vez(self):
        t10 = JointState[10]
        for _ in range(2):
            self.entra(self.pub("sim", topic="/patas/estado", text=t10.SCHEMA_TEXT,
                                schema=f"{t10.SCHEMA_ID:016x}", tipo="JointState"))
        avisos = self.avisos("articulaciones y robot.toml nombra")
        self.assertEqual(len(avisos), 1, self.logs)
        self.assertIn("'/patas/estado' trae 10 articulaciones y robot.toml nombra 12", avisos[0])
        self.assertIn("corrige names en [[joints]] de robot.toml o el número del tipo que usa 'sim'", avisos[0])
        self.entra(self.pub("bien", PATAS, text=T12))   # 12 = 12: sin aviso de número
        self.assertEqual(len(self.avisos("articulaciones y robot.toml nombra")), 1)

    def test_names_con_comodin(self):
        self.assertIn("escucha todos los topics ('*') y declara una lista", self.rechazo(
            self.sub("x", ["a"], topic="*", schema="*", tipo="")))

    def test_la_lista_se_ve_en_topic_info(self):
        from agmen import topic as TP

        self.entra(self.pub("politica", PATAS))
        msgs = [hp.parse_line(ln) for ln in self.hub.state_text().split("\n")]
        out = io.StringIO()
        with mock.patch.object(TP.hubmod, "query", lambda *a, **k: msgs), contextlib.redirect_stdout(out):
            TP.cmd_info(types.SimpleNamespace(topic="/patas/cmd"))
        self.assertIn("articulaciones (12): FR_hip, FR_thigh, FR_calf … RL_calf", out.getvalue())


class TestHubDatosHostiles(HubEnMemoria):
    def setUp(self):
        self.nuevo_hub(joints_del_rfc())

    def test_listas_mal_escritas_se_rechazan_sin_tumbar_al_hub(self):
        casos = {
            "a,,b": "names[1] = '' no es un nombre de articulación válido (está vacío)",
            ",a": "names[0] = ''", "a,": "names[1] = ''",
            "": "names= vacío no vale",
            "a%20b": "tiene un espacio",
            "a%1B%5B31m": "carácter de control",
            "a%E2%80%AEb": "no se ve",
            "a%25b": "tiene '%'",
            ",".join(["a"] * 5000): "names ocupa 9999 caracteres (los 5000 nombres separados por comas) y el "
                                    "máximo es 8192: usa nombres más cortos o reparte las articulaciones en varios "
                                    "grupos (un topic por grupo)",
            ",".join(f"j{i}" for i in range(5000)): "el máximo es 8192",
            "x" * 65: "mide 65 caracteres y el máximo es 64",
            "a,b,a": "'a' está dos veces",
        }
        for valor, frag in casos.items():
            with self.subTest(valor=valor[:30]):
                linea = hp.line("pub", id="h", node="hostil\x1b[2J", pid=1, topic="/patas/cmd", schema=S12,
                                type="JointCommand", text=T12)
                # El valor tal cual (los %XX como los escribiría quien ataca), sin volver a escaparlo.
                msg = self.rechazo(self.manda_bytes((linea + " names=" + valor).encode()))
                self.assertIn(frag, msg)
                self.assertIn("'/patas/cmd': la lista de nombres de 'hostil?[2J' no vale", msg)
                self.assertLess(len(msg), 700)
                self.assertTrue(msg.isprintable(), repr(msg))
        self.assertNotIn("h", self.hub.endpoints)
        self.entra(self.pub("bien", PATAS))     # y sigue atendiendo

    def test_un_escape_de_un_signo_valido_es_el_mismo_nombre(self):
        """El valor se lee como cualquier otro (sin '%XX'): 'a%2Cb' es la lista 'a,b' (no hay dos formas)."""
        self.nuevo_hub()
        linea = hp.line("pub", id="h", node="n", pid=1, topic="/x", schema=S12, type="JointCommand", text="")
        self.entra(self.manda_bytes((linea + " names=FR_hip%2CFR_thigh").encode()))
        self.assertEqual(self.hub.endpoints["h"].names, ("FR_hip", "FR_thigh"))

    def test_bytes_que_no_son_utf8(self):
        linea = hp.line("pub", id="h", node="n", pid=1, topic="/patas/cmd", schema=S12, type="JointCommand", text="")
        self.assertEqual(self.manda_bytes(linea.encode() + b" names=a\xff\xfeb"), [])   # ni lo lee ni contesta
        self.assertTrue(self.avisos("no es UTF-8"), self.logs)
        self.assertIn("'%' mal escrito", self.rechazo(self.manda_bytes(linea.encode() + b" names=a%ZZ")))  # a él
        self.assertNotIn("h", self.hub.endpoints)
        self.entra(self.pub("bien", PATAS))

    def test_nombre_del_nodo_enorme_queda_acotado(self):
        msg = self.rechazo(self.pub("h", PATAS[::-1], node="n" * 5000))
        self.assertLess(len(msg), 700)

    def test_argumentos_del_hub(self):
        for args, frag in ((["/a"], "escríbelo como TOPIC=nombre"), (["/a=x", "/a=y"], "está dos veces"),
                           (["/a=x,,y"], "--names /a: names[1] = ''"), (["a=x"], "nombre de topic inválido")):
            with self.subTest(args=args), self.assertRaises(hp.ProtocolError) as ctx:
                H.parse_names_args(args, [])
            self.assertIn(frag, str(ctx.exception))
        with self.assertRaisesRegex(hp.ProtocolError, "--require-names /b: ese topic no tiene lista"):
            H.parse_names_args(["/a=x"], ["/b"])
        j, = H.parse_names_args(["/a=x,y"], ["/a"])
        self.assertEqual((j.topic, j.names, j.required), ("/a", ("x", "y"), True))


class TestHubAnterior(HubEnMemoria):
    """Un hub anterior ignora el campo `names` (no rechaza la línea) y entonces no compara."""

    def comprobar(self):
        self.entra(self.pub("politica", PATAS))
        self.entra(self.pub("otra", PATAS[::-1]))          # no compara: entra
        self.entra(self.sub("driver", ["z"]))
        self.assertFalse(any("names" in x for x in self.logs), self.logs)

    def test_el_hub_de_antes_de_verdad(self):
        """El hub.py de la versión anterior a este cambio (3a33553), si está en el repositorio."""
        try:
            src = subprocess.run(["git", "-C", str(ROOT), "show", "3a33553:python/agmen/hub.py"], capture_output=True,
                                 text=True, encoding="utf-8", timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self.skipTest("sin git")
        if src.returncode != 0:
            self.skipTest("este clon no tiene el commit 3a33553 (un clon superficial): lo cubre la prueba simulada")
        mod = types.ModuleType("agmen._hub_anterior")
        mod.__package__ = "agmen"
        sys.modules[mod.__name__] = mod          # (dataclass busca su módulo aquí)
        self.addCleanup(sys.modules.pop, mod.__name__, None)
        exec(compile(src.stdout, "hub_anterior.py", "exec"), mod.__dict__)
        self.assertNotIn("_check_names", src.stdout)   # de verdad no conoce el campo
        self.nuevo_hub(cls=mod.Hub)
        self.comprobar()

    def test_simulado(self):
        """Sin el commit 3a33553 (clon superficial): un hub mínimo como los de antes, que lee cada línea con el
        parse_line de siempre, usa solo los campos que conoce (id, topic, port) e ignora los demás. Un Publisher y un
        Subscriber de Python puro con names= se registran contra él y los datos llegan: el campo nuevo no estorba."""
        err = io.StringIO()
        ctx = contextlib.redirect_stderr(err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.05)
        self.addCleanup(sock.close)
        patch = mock.patch.dict(os.environ, {"AGMEN_HUB": f"127.0.0.1:{sock.getsockname()[1]}"})
        patch.start()
        self.addCleanup(patch.stop)
        stop = threading.Event()
        subs: dict[str, list[str]] = {}       # topic → "ip:puerto" de sus Subscriber
        vistos: list[dict] = []               # los campos de cada registro, como los leyó

        def hub_anterior():
            while not stop.is_set():
                try:
                    data, addr = sock.recvfrom(65536)
                except (socket.timeout, OSError):
                    continue
                for ln in data.decode().splitlines():
                    op, f = hp.parse_line(ln)
                    if op not in ("pub", "sub"):
                        continue
                    vistos.append(f)
                    if op == "sub":
                        to = f"127.0.0.1:{f['port']}"
                        if to not in subs.setdefault(f["topic"], []):
                            subs[f["topic"]].append(to)
                        sock.sendto(hp.encode("ok", id=f["id"]), addr)
                    else:
                        sock.sendto(hp.encode("subs", id=f["id"], topic=f["topic"],
                                              to=",".join(subs.get(f["topic"], [])), tcp=""), addr)

        hilo = threading.Thread(target=hub_anterior, daemon=True)
        hilo.start()
        self.addCleanup(hilo.join, 2)
        self.addCleanup(stop.set)
        sub = pubsub._PureSubscriber(CMD12, "/patas/cmd", names=list(PATAS))
        self.addCleanup(sub.close)
        pub = pubsub._PurePublisher(CMD12, "/patas/cmd", names=list(PATAS[::-1]), node="politica")  # no compara
        self.addCleanup(pub.close)
        self.assertTrue(until(lambda: pub.subscribers == 1, every=lambda: (pub.publish(CMD12()), time.sleep(0.02))),
                        err.getvalue())
        self.assertTrue(until(lambda: sub.receive(0.05) is not None, every=lambda: pub.publish(CMD12())))
        # Sí le llegó el campo (y lo ignoró): no es que el nodo no lo mandara.
        self.assertEqual({f.get("names") for f in vistos}, {",".join(PATAS), ",".join(PATAS[::-1])})
        self.assertNotIn("rechaz", err.getvalue())


# --- un hub de verdad, en otro proceso (agmen hub --names ... --require-names ...) ----------------------------------

class HubProceso:
    def __init__(self, case, *args):
        self.addr = f"127.0.0.1:{free_port()}"
        env = dict(os.environ, PYTHONPATH=str(ROOT / "python"), PYTHONIOENCODING="utf-8")
        self.p = subprocess.Popen([sys.executable, "-m", "agmen", "hub", "--listen", self.addr, *args],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True,
                                  encoding="utf-8")
        case.addCleanup(self.close)
        self.out: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        patch = mock.patch.dict(os.environ, {"AGMEN_HUB": self.addr})
        patch.start()
        case.addCleanup(patch.stop)
        case.assertTrue(until(lambda: H.ping(self.addr) is not None), self.text())

    def _read(self):
        for ln in self.p.stdout:
            self.out.append(ln)

    def text(self) -> str:
        return "".join(self.out)

    def close(self):
        self.p.kill()
        self.p.wait(10)
        self.p.stdout.close()


class _HubDeVerdad(unittest.TestCase):
    """Un `agmen hub` de verdad con las listas del RFC (como lo arranca agmen run) y require_names en /patas/cmd."""

    def setUp(self):
        self.err = io.StringIO()
        ctx = contextlib.redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        args = []
        for topic in ("/patas/cmd", "/patas/estado"):
            args += ["--names", f"{topic}={','.join(PATAS)}"]
        self.hub = HubProceso(self, *args, "--require-names", "/patas/cmd")

    def abrir(self, cls, *args, **kw):
        o = cls(*args, **kw)
        self.addCleanup(o.close)
        return o

    def rechazado(self, publicar) -> str:
        """Publica (o recibe) hasta que el nodo ve el rechazo del hub, con tope; el texto del rechazo."""
        got: list[str] = []

        def paso():
            try:
                publicar()
            except wire.HubRejected as e:
                got.append(str(e))
            time.sleep(0.02)

        until(lambda: bool(got), every=paso)
        self.assertTrue(got, self.hub.text())
        return got[0]


class TestHubDeVerdad(_HubDeVerdad):
    def test_bien_entra_y_el_orden_cambiado_no(self):
        bien = self.abrir(pubsub._PurePublisher, CMD12, "/patas/cmd", names=list(PATAS), node="politica")
        sub = self.abrir(pubsub._PureSubscriber, CMD12, "/patas/cmd")
        self.assertTrue(until(lambda: bien.subscribers == 1, every=lambda: (bien.publish(CMD12()), time.sleep(0.02))),
                        self.hub.text())
        self.assertTrue(until(lambda: sub.receive(0.05) is not None, every=lambda: bien.publish(CMD12())))
        otro = list(PATAS[3:6] + PATAS[:3] + PATAS[6:])
        mal = self.abrir(pubsub._PurePublisher, CMD12, "/patas/cmd", names=otro, node="politica2")
        msg = self.rechazado(lambda: mal.publish(CMD12()))
        self.assertIn("el hub rechazó publicar en '/patas/cmd': '/patas/cmd': 'politica2' declara FL_hip, FL_thigh… "
                      "y robot.toml dice FR_hip, FR_thigh…", msg)
        sub_mal = self.abrir(pubsub._PureSubscriber, CMD12, "/patas/cmd", names=otro)
        self.assertIn("el hub rechazó escuchar '/patas/cmd'", self.rechazado(lambda: sub_mal.receive(0.05)))
        bien.publish(CMD12())                     # el que declaró bien sigue

    def test_require_names_y_avisos(self):
        viejo = self.abrir(pubsub._PurePublisher, CMD12, "/patas/cmd", node="viejo")
        self.assertIn("robot.toml la exige (require_names = true en [[joints]])",
                      self.rechazado(lambda: viejo.publish(CMD12())))
        T10 = JointState[10]
        sim = self.abrir(agmen.Publisher, T10, "/patas/estado")   # el núcleo que haya (sin lista: también el de C++)
        self.assertTrue(until(lambda: "trae 10 articulaciones y robot.toml nombra 12" in self.hub.text()),
                        self.hub.text())
        self.assertIn("sin declarar su lista de nombres", self.hub.text())
        self.assertEqual(self.hub.text().count("trae 10 articulaciones"), 1)
        sim.publish(T10())


# --- agmen run de punta a punta ------------------------------------------------------------------------------------

NODO = '''
import sys, time
import agmen
from agmen import wire
from agmen.msgs import JointCommand
names = sys.argv[1].split(",")
pub = agmen.Publisher(JointCommand[len(names)], "/patas/cmd", names=names)
end = time.monotonic() + 30
while time.monotonic() < end:
    pub.publish(JointCommand[len(names)]())
    time.sleep(0.05)
'''


class TestAgmenRun(unittest.TestCase):
    def test_el_que_declara_otro_orden_no_entra_y_la_pantalla_lo_dice(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "nodo.py").write_text(NODO, encoding="utf-8")
            robot = ('[robot]\nname = "nombres"\n\n[[joints]]\nname = "patas"\ncommand = "/patas/cmd"\n'
                     'names = ["cadera", "rodilla", "tobillo"]\nkp = 1.0\nkd = 0.1\n')
            for name, names in (("bien", "cadera,rodilla,tobillo"), ("politica", "rodilla,cadera,tobillo")):
                robot += (f'\n[[node]]\nname = "{name}"\ncmd = ["python", "nodo.py", "{names}"]\n'
                          f'env = {{ AGMEN_BACKEND = "pure" }}\n')
            (Path(d) / "robot.toml").write_text(robot, encoding="utf-8")
            env = dict(os.environ, PYTHONPATH=str(ROOT / "python"), PYTHONIOENCODING="utf-8",
                       AGMEN_HUB=f"127.0.0.1:{free_port()}")
            kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
            p = subprocess.Popen([sys.executable, "-m", "agmen", "run", str(Path(d) / "robot.toml"), "--no-web"],
                                 cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True,
                                 encoding="utf-8", **kw)
            out: list[str] = []
            threading.Thread(target=lambda: out.extend(iter(p.stdout.readline, "")), daemon=True).start()
            try:
                texto = lambda: "".join(out)   # noqa: E731
                frag = "'/patas/cmd': 'politica' declara rodilla, cadera… y robot.toml dice cadera, rodilla…"
                self.assertTrue(until(lambda: frag in texto() and "'bien' publica /patas/cmd" in texto(), 30),
                                texto())
                self.assertIn("HubRejected", texto())
                self.assertNotIn("'bien' declara", texto())
            finally:
                p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
                try:
                    p.wait(15)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(5)
                p.stdout.close()

    def test_prepare_hub_pasa_las_listas_siempre(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "robot.toml"
            path.write_text('[robot]\nname = "r"\n\n[[node]]\nname = "m"\ncmd = "x"\n\n[[joints]]\nname = "patas"\n'
                            'state = "/patas/estado"\ncommand = "/patas/cmd"\nnames = ["a", "b"]\n'
                            'require_names = true\n', encoding="utf-8")
            cfg, _ = prepare_hub(load_config(path), ping=lambda a: None)
            cmd = next(n for n in cfg.nodes if n.name == "hub").cmd
            self.assertIn("/patas/estado=a,b", cmd)
            self.assertIn("/patas/cmd=a,b", cmd)
            self.assertEqual(cmd[cmd.index("--require-names") + 1], "/patas/cmd")
            self.assertNotIn("--expect", cmd)           # sin [[topic]]: el aviso de "no declarado" sigue apagado
            j = H.parse_names_args([cmd[i + 1] for i, a in enumerate(cmd) if a == "--names"],
                                    [cmd[i + 1] for i, a in enumerate(cmd) if a == "--require-names"])
            self.assertEqual({x.topic: (x.names, x.required) for x in j},
                             {"/patas/estado": (("a", "b"), False), "/patas/cmd": (("a", "b"), True)})
            _, nota = prepare_hub(load_config(path), ping=lambda a: "0.13.0")
            self.assertIn("(los [[joints]] de este robot.toml no se revisan)", nota)

    def test_una_linea_de_comandos_del_hub_que_no_cabe(self):
        grupos = "".join(f'\n[[joints]]\nname = "g{k}"\nstate = "/g{k}/e"\ncommand = "/g{k}/c"\n'
                         f'names = {json.dumps([f"g{k}_articulacion_{i:03d}" for i in range(300)])}\n'
                         for k in range(3))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "robot.toml"
            path.write_text('[robot]\nname = "r"\n\n[[node]]\nname = "m"\ncmd = "x"\n' + grupos, encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                prepare_hub(load_config(path), ping=lambda a: None)
            self.assertIn("no caben en la línea de comandos del agmen hub", str(ctx.exception))
            self.assertIn("el máximo es 30000", str(ctx.exception))


# --- entre computadoras: el campo pasa por tables.py ----------------------------------------------------------------

class TestEntreComputadorasSinRed(unittest.TestCase):
    def test_check_endpoint_conserva_y_revisa_names(self):
        d = {"id": "a1", "node": "n", "pid": 7, "topic": "/patas/cmd", "schema": S12, "type": "JointCommand",
             "text": T12, "names": ",".join(PATAS)}
        ok = T.check_endpoint("pubs", d)
        self.assertEqual(ok["names"], ",".join(PATAS))
        linea = T.hub_line("laptop", "127.0.0.3", "pubs", ok)
        self.assertTrue(linea.endswith(b" machine=laptop names=" + ",".join(PATAS).encode()), linea)
        s = T.check_endpoint("subs", {**{k: v for k, v in d.items() if k != "text"}, "port": 7000, "tcp": 0})
        self.assertTrue(T.hub_line("laptop", "127.0.0.3", "subs", s, []).endswith(
            b" solo=- names=" + ",".join(PATAS).encode()))
        for malo, frag in (("a,,b", "está vacío"), ("", "vacío no vale"), (3, "no es un texto"),
                           ("a b", "tiene un espacio"), ("x" * 9000, "mide más de 8192")):
            with self.subTest(malo=str(malo)[:20]), self.assertRaises(hp.ProtocolError) as ctx:
                T.check_endpoint("pubs", {**d, "names": malo})
            self.assertIn(frag, str(ctx.exception))

    def test_local_table_lleva_names_del_estado(self):
        hub = H.Hub(f"127.0.0.1:{free_port()}", log=lambda _: None)
        self.addCleanup(hub.sock.close)
        hub._send = lambda *a: None
        hub.handle(hp.encode("pub", id="a", node="politica", pid=1, topic="/patas/cmd", schema=S12,
                             type="JointCommand", text=T12, names=",".join(PATAS)), LOCAL, 0.0)
        hub.handle(hp.encode("pub", id="b", node="sin", pid=1, topic="/x", schema=S12, type="JointCommand",
                             text=T12), LOCAL, 0.0)
        msgs = [hp.parse_line(ln) for ln in hub.state_text().split("\n")]
        pubs, _ = T.local_table(msgs)
        by = {p["id"]: p for p in pubs}
        self.assertEqual(by["a"]["names"], ",".join(PATAS))
        self.assertNotIn("names", by["b"])
        # Y pasa por JSON (como por la conexión del agente) sin perderse.
        again = T.check_endpoint("pubs", json.loads(json.dumps(by["a"])))
        self.assertEqual(again["names"], ",".join(PATAS))


try:
    import test_tablas as TT     # (los ayudantes de dos computadoras en una; sus pruebas no se recogen aquí)
except ImportError:   # pragma: no cover
    TT = None


@unittest.skipUnless(TT is not None and TT.hay_ips(TT.LAPTOP_IP, TT.ROBOT_IP),
                     "hace falta 127.0.0.2 y 127.0.0.3 (en Linux siempre están)")
class TestEntreComputadoras(unittest.TestCase):
    def setUp(self):
        self.err = io.StringIO()
        ctx = contextlib.redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.la = TT.Lado(self, "laptop", TT.LAPTOP_IP)
        self.lb = TT.Lado(self, "robot", TT.ROBOT_IP)
        # El hub del robot, con su robot.toml: la lista de [[joints]] para /patas/cmd.
        with self.lb.hub.hub.lock:
            self.lb.hub.hub.joints = {j.topic: j for j in joints_del_rfc()}
        self.canal = TT.Canal(self.la, self.lb)
        self.canal.confirmar()

    def test_la_lista_cruza_y_el_hub_del_otro_lado_compara(self):
        bien = self.la.open(self, functools.partial(pubsub._PurePublisher, names=list(PATAS)), CMD12, "/patas/cmd")
        hid = f"laptop.{bien._link.id}"
        self.assertTrue(until(lambda: hid in self.lb.remotas("laptop")), (self.lb.said, self.lb.hub.logs))
        self.assertEqual(self.lb.remotas("laptop")[hid].names, PATAS)
        otro = list(PATAS[::-1])
        mal = self.la.open(self, functools.partial(pubsub._PurePublisher, names=otro, node="politica"), CMD12,
                           "/patas/estado")
        frag = ("'/patas/estado': 'politica' (de la computadora 'laptop') declara RL_calf, RL_thigh… y robot.toml "
                "dice FR_hip, FR_thigh…")
        self.assertTrue(until(lambda: any(frag in t for t in self.lb.said)), (self.lb.said, self.lb.hub.logs))
        self.assertNotIn(f"laptop.{mal._link.id}", self.lb.remotas("laptop"))
        # Lo que se mandó por la conexión trae la lista (no se perdió en el camino).
        eps = [e for part in self.canal.de_a for e in part["pubs"]]
        self.assertIn(",".join(otro), [e.get("names") for e in eps])


# =====================================================================================================================
# Ataques del tester (segunda tanda): lo que la primera no probó.
# =====================================================================================================================

def cargar_anterior(modulo: str, nombre: str):
    """El módulo `modulo` de agmen tal como era en 3a33553 (antes de names=); None si este clon no tiene el commit."""
    try:
        src = subprocess.run(["git", "-C", str(ROOT), "show", f"3a33553:python/agmen/{modulo}.py"],
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if src.returncode != 0:
        return None
    mod = types.ModuleType(f"agmen.{nombre}")
    mod.__package__ = "agmen"
    sys.modules[mod.__name__] = mod
    exec(compile(src.stdout, f"{modulo}_anterior.py", "exec"), mod.__dict__)
    return mod


def udp(addr: str, data: bytes, espera: float = 0.6, sock=None) -> list[tuple[str, dict]]:
    """Manda un datagrama a mano al hub y junta las respuestas que lleguen en `espera` s."""
    host, port = addr.rsplit(":", 1)
    s = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out = []
    try:   # (el sendto también: si no sale, el socket se cierra igual)
        s.settimeout(espera)
        s.sendto(data, (host, int(port)))
        while True:
            got, _ = s.recvfrom(65536)
            out.extend(hp.parse(got))
    except (socket.timeout, TimeoutError):
        pass
    finally:
        if sock is None:
            s.close()
    return out


def linea_pub(eid, topic="/patas/cmd", names=None, crudo=None, text=T12, schema=S12, tipo="JointCommand"):
    """Una línea `pub` en bytes; `crudo` se pega al final tal cual (para escribir lo que quiera quien ataca)."""
    base = hp.line("pub", id=eid, node=eid, pid=1, topic=topic, schema=schema, type=tipo, text=text)
    if names is not None:
        base += " names=" + (names if isinstance(names, str) else ",".join(names))
    if crudo:
        base += " " + crudo
    return base.encode()


class TestLineaDeSiempre(unittest.TestCase):
    """El formato es el contrato: sin names=, los bytes son los de la versión anterior (3a33553)."""

    def setUp(self):
        self.err = io.StringIO()
        ctx = contextlib.redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.anterior = cargar_anterior("pubsub", "_pubsub_anterior")
        if self.anterior is None:
            self.skipTest("este clon no tiene el commit 3a33553")
        self.addCleanup(sys.modules.pop, "agmen._pubsub_anterior", None)

    def registro(self, falso, op):
        return falso.registro(op).decode()

    def test_pub_igual_que_antes_menos_el_id(self):
        falso = FalsoHub(self)
        viejo = self.anterior._PurePublisher(CMD12, "/patas/cmd", node="n")
        self.addCleanup(viejo.close)
        l_viejo = self.registro(falso, "pub").replace(f"{viejo.source_id:016x}", "ID")
        nuevo = pubsub._PurePublisher(CMD12, "/patas/cmd", node="n")
        self.addCleanup(nuevo.close)
        l_nuevo = self.registro(falso, "pub").replace(f"{nuevo.source_id:016x}", "ID")
        self.assertEqual(l_nuevo, l_viejo)
        self.assertNotIn("names", l_nuevo)

    def test_sub_igual_que_antes_menos_id_y_puerto(self):
        falso = FalsoHub(self)
        viejo = self.anterior._PureSubscriber(CMD12, "/patas/cmd")
        self.addCleanup(viejo.close)
        l_viejo = re.sub(r" node=\S+", " node=N", self.registro(falso, "sub").replace(viejo._link.id, "ID").replace(
            "port=" + viejo.listen.rsplit(":", 1)[1], "port=P"))
        nuevo = pubsub._PureSubscriber(CMD12, "/patas/cmd")
        self.addCleanup(nuevo.close)
        l_nuevo = re.sub(r" node=\S+", " node=N", self.registro(falso, "sub").replace(nuevo._link.id, "ID").replace(
            "port=" + nuevo.listen.rsplit(":", 1)[1], "port=P"))
        self.assertEqual(l_nuevo, l_viejo)

    def test_names_none_es_lo_mismo_que_no_pasarlo(self):
        falso = FalsoHub(self)
        a = pubsub._PurePublisher(CMD12, "/patas/cmd", names=None)
        self.addCleanup(a.close)
        self.assertNotIn(b" names=", falso.registro("pub"))
        self.assertIsNone(a.names)

    def test_el_hub_anterior_acepta_la_linea_nueva_con_names(self):
        """Compatibilidad hacia atrás: el hub de 3a33553 recibe una línea con names= y la acepta sin chistar."""
        viejo = cargar_anterior("hub", "_hub_anterior_ataque")
        self.addCleanup(sys.modules.pop, "agmen._hub_anterior_ataque", None)
        hub = viejo.Hub(f"127.0.0.1:{free_port()}", log=lambda _: None, grace_s=0.0)
        self.addCleanup(hub.sock.close)
        enviado = []
        hub._send = lambda data, addr: enviado.append(data)
        for raros in ("a,b", "", "a,,b", "x" * 9000):
            hub.handle(linea_pub("p", crudo=f"names={raros}"), LOCAL, 0.0)
            ops = [hp.parse(d)[0][0] for d in enviado]
            self.assertNotIn("error", ops, raros[:20])


class TestHubMemoriaAtaques(HubEnMemoria):
    def setUp(self):
        self.nuevo_hub(joints_del_rfc())

    def test_mayusculas_distintas_se_rechazan(self):
        otro = ("fr_hip",) + PATAS[1:]
        msg = self.rechazo(self.pub("p", otro))
        self.assertIn("declara fr_hip, FR_thigh… y robot.toml dice FR_hip, FR_thigh…", msg)
        self.assertIn("cambian desde la articulación 1 de 12", msg)

    def test_lista_mas_larga_y_mas_corta(self):
        self.rechazo(self.pub("larga", PATAS + ("extra",), text=""))
        self.rechazo(self.pub("corta", PATAS[:1], text=""))
        self.rechazo(self.pub("corta2", PATAS[:11], text=""))
        self.assertEqual(self.hub.endpoints, {})

    def test_un_rechazado_no_deja_su_lista_guardada(self):
        """Sin [[joints]]: A entra con X, B con Y es rechazado; A se va; C con Y debe entrar (B no dejó nada)."""
        self.nuevo_hub()
        t = dict(topic="/otro", text="", schema="00000000000000d1", tipo="Mio")
        self.entra(self.pub("a", ["x", "y"], **t))
        self.rechazo(self.pub("b", ["z", "w"], **t))
        self.assertNotIn("b", self.hub.endpoints)
        self.manda("bye", id="a")
        self.entra(self.pub("c", ["z", "w"], **t))
        self.assertEqual(self.hub.endpoints["c"].names, ("z", "w"))
        self.rechazo(self.pub("d", ["x", "y"], **t))      # y ahora es X la que choca

    def test_el_que_se_va_y_vuelve_con_otra_lista(self):
        self.nuevo_hub()
        t = dict(topic="/otro", text="", schema="00000000000000d1", tipo="Mio")
        self.entra(self.pub("a", ["x", "y"], **t))
        self.entra(self.sub("s", ["x", "y"], topic="/otro", schema="00000000000000d1", tipo="Mio"))
        self.manda("bye", id="a")
        # El suscriptor todavía tiene X: a no puede volver con Y mientras él esté.
        self.rechazo(self.pub("a", ["p", "q"], **t))
        self.manda("bye", id="s")
        self.entra(self.pub("a", ["p", "q"], **t))
        self.assertEqual(self.hub.endpoints["a"].names, ("p", "q"))

    def test_mismo_id_cambia_de_lista_solo_si_nadie_mas_la_tiene(self):
        self.nuevo_hub()
        t = dict(topic="/otro", text="", schema="00000000000000d1", tipo="Mio")
        self.entra(self.pub("a", ["x", "y"], **t))
        self.entra(self.pub("a", ["p", "q"], **t))          # el único: puede cambiar
        self.entra(self.pub("b", ["p", "q"], **t))
        msg = self.rechazo(self.pub("a", ["x", "y"], **t))  # ahora b la tiene: no
        self.assertIn("'a' declara x, y", msg)
        self.assertEqual(self.hub.endpoints["a"].names, ("p", "q"), "el rechazo no debe cambiar lo guardado")

    def test_registro_rechazado_de_un_endpoint_ya_registrado_no_lo_tumba(self):
        self.entra(self.pub("p", PATAS))
        self.rechazo(self.pub("p", PATAS[::-1]))
        self.assertEqual(self.hub.endpoints["p"].names, PATAS)   # sigue con la buena

    def test_rechazo_no_deja_avisos_ni_endpoint_a_medias(self):
        self.rechazo(self.pub("x", PATAS[::-1]))
        self.assertEqual(self.avisos("sin declarar"), [])
        self.assertEqual(self.hub.endpoints, {})

    def test_reregistro_cada_segundo_no_repite_ningun_aviso(self):
        t10 = JointState[10]
        for i in range(30):
            self.entra(self.pub("sim", topic="/patas/estado", text=t10.SCHEMA_TEXT, schema=f"{t10.SCHEMA_ID:016x}",
                                tipo="JointState"))
            self.entra(self.pub("viejo"))
        avisos = [x for x in self.logs if x.startswith("⚠")]
        self.assertEqual(len(avisos), 3, avisos)     # sin lista (sim), 10 vs 12 (sim), sin lista (viejo)

    def test_names_dos_veces_en_la_misma_linea_se_rechaza(self):
        """Un registro con names repetido no se adivina (ni la primera ni la última): se rechaza y se dice qué hacer.
        Da igual el orden de la lista buena y la mala, y aunque las dos sean iguales."""
        bien, mal = ",".join(PATAS), ",".join(PATAS[::-1])
        for eid, primero, segundo in (("malo-primero", mal, bien), ("bueno-primero", bien, mal), ("iguales", bien, bien)):
            with self.subTest(eid=eid):
                msg = self.rechazo(self.manda_bytes(linea_pub(eid, names=primero, crudo=f"names={segundo}")))
                self.assertIn(f"'/patas/cmd': '{eid}' manda el campo names más de una vez en la misma línea y no sé "
                              f"cuál de las listas vale. Manda names= una sola vez, con la lista en orden", msg)
                self.assertNotIn(eid, self.hub.endpoints)
        # Ya registrado con su lista: un latido con names repetido no cambia lo guardado.
        self.entra(self.pub("p", PATAS))
        self.rechazo(self.manda_bytes(linea_pub("p", names=bien, crudo=f"names={mal}")))
        self.assertEqual(self.hub.endpoints["p"].names, PATAS)
        # Otras claves repetidas siguen como siempre (gana la última): esto es solo de names.
        self.entra(self.manda_bytes(linea_pub("q", names=bien, crudo="pid=2")))
        self.assertEqual(self.hub.endpoints["q"].pid, "2")

    def test_unicode_y_nul_y_escapes(self):
        for crudo in ("names=ñandú", "names=a%00b", "names=a%2Cb%2C", "names=%E4%B8%AD", "names=a%09b",
                      "names=a%0Ab", "names=a%0D%0Aagmen-hub/1%20ping"):
            with self.subTest(crudo=crudo):
                r = self.manda_bytes(linea_pub("h") + b" " + crudo.encode())
                # Nunca se cae y nunca queda guardado el endpoint hostil.
                self.assertNotIn("h", self.hub.endpoints)
                if r:
                    self.assertEqual(r[0][0], "error", crudo)
                    self.assertTrue(r[0][1]["msg"].isprintable(), repr(r[0][1]["msg"]))
        self.entra(self.pub("bien", PATAS))

    def test_percent_ff_no_hace_saltar_al_hub(self):
        """FALLA REAL (ya estaba antes de names=): %FF no es UTF-8 y hp.unquote lanza UnicodeDecodeError, que
        Hub.handle no atrapa (solo atrapa ProtocolError)."""
        for crudo in ("names=%FF", "names=a%C3", "node=%FF"):
            with self.subTest(crudo=crudo):
                try:
                    self.manda_bytes(linea_pub("h") + b" " + crudo.encode())
                except UnicodeDecodeError as e:
                    self.fail(f"Hub.handle dejó escapar {type(e).__name__}: {e}")
        self.entra(self.pub("bien", PATAS))

    def test_diez_mil_nombres_de_una_letra(self):
        crudo = "names=" + ",".join("a" for _ in range(10000))
        msg = self.rechazo(self.manda_bytes(linea_pub("h") + b" " + crudo.encode()))
        self.assertIn("el máximo es 8192", msg)
        self.assertLess(len(msg), 700)

    def test_lista_en_el_tope_justo_si_entra_y_uno_mas_no(self):
        self.nuevo_hub()
        # 127 nombres de 63 caracteres + comas = 8191 + ... se arma con nombres de 63: k*63 + (k-1) <= 8192.
        k = 8193 // 64                     # 128 nombres: 128*63 + 127 = 8191 caracteres
        justo = [f"{i:03d}" + "x" * 60 for i in range(k)]
        self.assertEqual(len(",".join(justo)), 8191)
        self.entra(self.manda_bytes(linea_pub("ok", topic="/x", text="", names=justo)))
        de_mas = [f"{i:03d}" + "x" * 61 for i in range(k)]       # 128*64 + 127 = 8319
        self.assertGreater(len(",".join(de_mas)), 8192)
        self.rechazo(self.manda_bytes(linea_pub("mal", topic="/y", text="", names=de_mas)))
        self.entra(self.manda_bytes(linea_pub("ok64", topic="/z", text="", names=["n" * 64])))
        self.rechazo(self.manda_bytes(linea_pub("mal65", topic="/z", text="", names=["n" * 65])))
        # exactamente 8192 caracteres: 8191 + una coma y un nombre de 0 no existe; 8192 = justo con un nombre de 64
        igual = justo[:-1] + ["y" * 62 + "zz"]          # 127*63+126 comas... el último mide 64: 8192 en total
        self.assertEqual(len(",".join(igual)), 8192)
        self.entra(self.manda_bytes(linea_pub("ok8192", topic="/w", text="", names=igual)))

    def test_require_names_con_lista_buena_entra_y_sub_sin_lista_tambien(self):
        self.nuevo_hub(joints_del_rfc(require=True))
        self.rechazo(self.pub("viejo"))
        self.entra(self.pub("bueno", PATAS))
        self.entra(self.sub("s"))
        self.entra(self.sub("s2", PATAS))
        self.rechazo(self.sub("s3", PATAS[::-1]))

    def test_require_names_tambien_rechaza_a_un_publisher_de_otra_computadora_sin_lista(self):
        self.nuevo_hub(joints_del_rfc(require=True))
        # Una entrada de otra computadora (machine=...): el puente la registra con machine=
        r = self.manda_bytes(hp.encode("pub", id="r.1", node="n", pid=1, topic="/patas/cmd", schema=S12,
                                       type="JointCommand", text=T12, machine="laptop"))
        self.assertIn("la computadora 'laptop'", self.rechazo(r))

    def test_names_en_sub_de_comodin_con_topic_concreto_y_schema_comodin(self):
        # sub de un topic concreto con schema '*' (RawSubscriber): puede declarar lista
        r = self.manda("sub", id="raw", node="raw", pid=1, topic="/patas/cmd", schema="*", type="", port=6000,
                       names=",".join(PATAS))
        self.entra(r)
        self.rechazo(self.manda("sub", id="raw2", node="raw", pid=1, topic="/patas/cmd", schema="*", type="",
                                port=6000, names=",".join(PATAS[::-1])))


class TestHubVerdaderoPorUdp(unittest.TestCase):
    """Líneas hostiles a mano, por UDP, a un agmen hub de verdad. No se cae, contesta con el porqué y sigue."""

    def setUp(self):
        self.hub = HubProceso(self, "--names", f"/patas/cmd={','.join(PATAS)}")

    def vivo(self):
        self.assertIsNotNone(H.ping(self.hub.addr, timeout=2.0), self.hub.text())
        self.assertIsNone(self.hub.p.poll(), self.hub.text())

    def test_listas_hostiles(self):
        casos = [
            ("names=", "vacío"),
            ("names=a,,b", "está vacío"),
            ("names=,a", "names[0]"),
            ("names=a,", "names[1]"),
            ("names=a%2Cb%2C", "vacío"),
            ("names=a%00b", "carácter de control"),
            ("names=" + "x" * 8193, "ocupa 8193 caracteres"),
            ("names=" + "n" * 65, "mide 65 caracteres y el máximo es 64"),
            ("names=" + ",".join(f"n{i:03d}" + "x" * 20 for i in range(400)), "el máximo es 8192"),
            ("names=" + ",".join("a" for _ in range(10000)), "el máximo es 8192"),
            ("names=a%20b", "espacio"),
            ("names=ñ", "ñ"),
            ("names=%E4%B8%AD", "no es un nombre"),
            ("names=a,b,a", "dos veces"),
            ("names=-a", "empieza con"),
            ("names=FR_hip names=FR_thigh,FR_calf", "más de una vez en la misma línea"),   # repetido: se rechaza
        ]
        for crudo, frag in casos:
            with self.subTest(crudo=crudo[:40]):
                linea = linea_pub("hostil", text="", schema="00000000000000d1", tipo="Mio", topic="/otro") \
                    + b" " + crudo.encode()
                if not crudo.startswith("names=FR"):
                    resp = udp(self.hub.addr, linea)
                else:
                    resp = udp(self.hub.addr, linea_pub("hostil", names=PATAS[0], crudo=crudo))
                self.assertTrue(resp, "el hub no contestó (fallo callado)")
                self.assertEqual(resp[0][0], "error", resp)
                msg = resp[0][1]["msg"]
                self.assertIn(frag, msg)
                self.assertLess(len(msg), 800)
                self.vivo()
        # Nada quedó registrado y un nodo bueno entra.
        resp = udp(self.hub.addr, linea_pub("bueno", names=PATAS))
        self.assertNotEqual(resp[0][0], "error", resp)
        self.assertTrue(any(f.get("id") == "bueno" for op, f in H.query(self.hub.addr) if op == "ep"))
        self.assertFalse(any(f.get("id") == "hostil" for op, f in H.query(self.hub.addr) if op == "ep"))

    def test_bytes_invalidos_no_tumban_el_hub(self):
        for data in (linea_pub("h") + b" names=a\xff\xfeb", linea_pub("h") + b" names=a%ZZ",
                     linea_pub("h") + b" names=%", linea_pub("h") + b" names=a%2", linea_pub("h") + b" names",
                     linea_pub("h") + b" =a", linea_pub("h") + b" names==", b"agmen-hub/1 pub names=" + b"%" * 70000):
            with self.subTest(data=data[-20:]):
                try:
                    udp(self.hub.addr, data, espera=0.3)
                except OSError:
                    pass       # (un datagrama de más de 65507 bytes no sale: eso es del sistema, no del hub)
                self.vivo()

    def test_percent_ff_no_tumba_el_hilo_del_hub(self):
        """FALLA REAL (ya estaba antes de names=): un solo datagrama con %FF mata el hilo que atiende al hub."""
        for crudo in (b"names=%FF", b"names=a%C3", b"node=%FF"):
            with self.subTest(crudo=crudo):
                try:
                    udp(self.hub.addr, linea_pub("h") + b" " + crudo, espera=0.3)
                except OSError:
                    pass
                self.assertIsNotNone(H.ping(self.hub.addr, timeout=2.0), "el hub dejó de contestar: " + self.hub.text())
                if self.hub.p.poll() is not None or "Traceback" in self.hub.text():
                    self.fail("el hub tuvo un error sin atrapar:\n" + self.hub.text()[-600:])

    def test_sub_de_comodin_con_names(self):
        resp = udp(self.hub.addr, hp.line("sub", id="g", node="g", pid=1, topic="*", schema="*", type="",
                                          port=6000, names="a,b").encode())
        self.assertEqual(resp[0][0], "error")
        self.assertIn("escucha todos los topics", resp[0][1]["msg"])
        self.vivo()

    def test_atiende_a_los_demas_mientras_lo_atacan(self):
        parar = threading.Event()

        def ataque():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            host, port = self.hub.addr.rsplit(":", 1)
            i = 0
            while not parar.is_set():
                i += 1
                s.sendto(linea_pub(f"x{i % 50}", crudo="names=" + ",".join("a" for _ in range(9000))),
                         (host, int(port)))
                time.sleep(0.001)
            s.close()
        t = threading.Thread(target=ataque, daemon=True)
        t.start()
        try:
            for _ in range(20):
                self.assertIsNotNone(H.ping(self.hub.addr, timeout=2.0), self.hub.text())
            resp = udp(self.hub.addr, linea_pub("bueno", names=PATAS), espera=1.0)
            self.assertTrue(resp and resp[0][0] != "error", resp)
        finally:
            parar.set()
            t.join(5)
        self.vivo()

    def test_hilos_con_listas_distintas_sin_joints_gana_una_sola(self):
        self.hub.close()
        self.hub = HubProceso(self)           # sin --names: el hub compara entre endpoints
        n = 16
        barrera = threading.Barrier(n)
        res: dict[int, str] = {}

        def hilo(i):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                barrera.wait(10)
                r = udp(self.hub.addr, linea_pub(f"t{i}", topic="/otro", text="", schema="00000000000000d1",
                                                 tipo="Mio", names=["x", f"y{i}"]), espera=2.0, sock=s)
                res[i] = r[0][0] if r else "nada"
                if r and r[0][0] == "error":
                    res[i] = "error:" + r[0][1]["msg"]
            finally:
                s.close()
        hilos = [threading.Thread(target=hilo, args=(i,)) for i in range(n)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(20)
        self.assertEqual(len(res), n, res)
        ganadores = [i for i, r in res.items() if not r.startswith("error") and r != "nada"]
        self.assertEqual(len(ganadores), 1, res)
        perdedores = [r for i, r in res.items() if i not in ganadores]
        self.assertTrue(all("un topic, una lista" in r for r in perdedores), perdedores)
        eps = [f for op, f in H.query(self.hub.addr) if op == "ep" and f.get("topic") == "/otro"]
        self.assertEqual(len(eps), 1, eps)
        self.vivo()

    def test_hilos_con_la_misma_lista_entran_todos(self):
        self.hub.close()
        self.hub = HubProceso(self)
        n = 16
        barrera = threading.Barrier(n)
        res = {}

        def hilo(i):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                barrera.wait(10)
                r = udp(self.hub.addr, linea_pub(f"t{i}", names=PATAS), espera=2.0, sock=s)
                res[i] = r[0][0] if r else "nada"
            finally:
                s.close()
        hilos = [threading.Thread(target=hilo, args=(i,)) for i in range(n)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(20)
        self.assertNotIn("error", res.values(), res)
        self.assertNotIn("nada", res.values(), res)

    def test_hilos_contra_lista_de_robot_toml_todos_rechazados_menos_los_buenos(self):
        n = 12
        barrera = threading.Barrier(n)
        res = {}

        def hilo(i):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                barrera.wait(10)
                lista = PATAS if i % 3 == 0 else PATAS[::-1]
                r = udp(self.hub.addr, linea_pub(f"t{i}", names=lista), espera=2.0, sock=s)
                res[i] = r[0][0] if r else "nada"
            finally:
                s.close()
        hilos = [threading.Thread(target=hilo, args=(i,)) for i in range(n)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(20)
        for i, r in res.items():
            self.assertEqual(r == "error", i % 3 != 0, res)


class TestHubProcesoRequire(unittest.TestCase):
    def setUp(self):
        self.err = io.StringIO()
        ctx = contextlib.redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.hub = HubProceso(self, "--names", f"/patas/cmd={','.join(PATAS)}", "--require-names", "/patas/cmd")

    def test_publisher_sin_lista_rechazado_con_lista_entra_subscriber_sin_lista_entra(self):
        viejo = pubsub._PurePublisher(CMD12, "/patas/cmd", node="viejo")
        self.addCleanup(viejo.close)
        got = []

        def paso():
            try:
                viejo.publish(CMD12())
            except wire.HubRejected as e:
                got.append(str(e))
            time.sleep(0.02)
        until(lambda: bool(got), every=paso)
        self.assertTrue(got, self.hub.text())
        self.assertIn("require_names = true", got[0])
        sub = pubsub._PureSubscriber(CMD12, "/patas/cmd")     # sin lista: entra
        self.addCleanup(sub.close)
        bueno = pubsub._PurePublisher(CMD12, "/patas/cmd", names=list(PATAS), node="bueno")
        self.addCleanup(bueno.close)
        self.assertTrue(until(lambda: sub.receive(0.05) is not None, every=lambda: bueno.publish(CMD12())),
                        self.hub.text())
        self.assertIsNone(self.hub.p.poll())

    def test_argumentos_malos_del_hub_dicen_que_hacer(self):
        casos = [(["--names", "/a"], "escríbelo como TOPIC=nombre"),
                 (["--require-names", "/b"], "ese topic no tiene lista"),
                 (["--names", "/a=x,,y"], "names[1]"),
                 (["--names", "/a=x", "--names", "/a=y"], "está dos veces"),
                 (["--names", "/a=" + "x" * 65], "65")]
        for args, frag in casos:
            with self.subTest(args=args):
                env = dict(os.environ, PYTHONPATH=str(ROOT / "python"), PYTHONIOENCODING="utf-8")
                r = subprocess.run([sys.executable, "-m", "agmen", "hub", "--listen", f"127.0.0.1:{free_port()}",
                                    *args], capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
                self.assertNotEqual(r.returncode, 0, (r.stdout, r.stderr))
                self.assertIn(frag, r.stdout + r.stderr)
                self.assertNotIn("Traceback", r.stdout + r.stderr)


# --- agmen run de punta a punta: el que llega es el de buen orden ---------------------------------------------------

POLITICA = '''
import sys, time
import agmen
from agmen.msgs import JointCommand
names = sys.argv[1].split(",")
valor = float(sys.argv[2])
topic = sys.argv[3] if len(sys.argv) > 3 else "/patas/cmd"
n = len(names) if sys.argv[1] != "-" else 3
T = JointCommand[n]
pub = agmen.Publisher(T, topic, names=names if sys.argv[1] != "-" else None)   # "-": sin lista
end = time.monotonic() + 40
while time.monotonic() < end:
    m = T()
    m.position = [valor] * n
    if len(sys.argv) > 3:   # hacia un [[mux]]: kp y kd finitos (el mux no deja pasar NaN)
        m.kp, m.kd = [1.0] * n, [0.1] * n
    try:
        pub.publish(m)
    except Exception as e:
        print("MALA", type(e).__name__, e, flush=True)
        time.sleep(0.5)
    time.sleep(0.05)
'''

LECTOR = '''
import time
import agmen
from agmen.msgs import JointCommand
T = JointCommand[3]
sub = agmen.Subscriber(T, "/patas/cmd")
end = time.monotonic() + 40
vistos = set()
while time.monotonic() < end:
    s = sub.receive(0.1)
    if s is not None:
        v = float(s.message.position[0])
        if v not in vistos:
            vistos.add(v)
            print("VISTO", v, flush=True)
'''


class TestAgmenRunLlegaElBueno(unittest.TestCase):
    def correr(self, robot_extra: str, tiene, tope=40):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "politica.py").write_text(POLITICA, encoding="utf-8")
            (Path(d) / "lector.py").write_text(LECTOR, encoding="utf-8")
            (Path(d) / "robot.toml").write_text(robot_extra, encoding="utf-8")
            env = dict(os.environ, PYTHONPATH=str(ROOT / "python"), PYTHONIOENCODING="utf-8",
                       AGMEN_HUB=f"127.0.0.1:{free_port()}")
            kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
            p = subprocess.Popen([sys.executable, "-m", "agmen", "run", str(Path(d) / "robot.toml"), "--no-web"],
                                 cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True,
                                 encoding="utf-8", **kw)
            out: list[str] = []
            threading.Thread(target=lambda: out.extend(iter(p.stdout.readline, "")), daemon=True).start()
            try:
                ok = until(lambda: tiene("".join(out)), tope)
                time.sleep(1.5)       # un rato más, por si el malo alcanzara a colarse
                return ok, "".join(out)
            finally:
                p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
                try:
                    p.wait(15)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(5)
                p.stdout.close()

    @staticmethod
    def nodo(name, cmd):
        return f'\n[[node]]\nname = "{name}"\ncmd = {json.dumps(cmd)}\nenv = {{ AGMEN_BACKEND = "pure" }}\n'

    def test_orden_malo_no_publica_y_el_bueno_llega(self):
        robot = ('[robot]\nname = "n"\n\n[[joints]]\nname = "patas"\ncommand = "/patas/cmd"\n'
                 'names = ["cadera", "rodilla", "tobillo"]\nkp = 1.0\nkd = 0.1\n')
        robot += self.nodo("lector", ["python", "lector.py"])
        robot += self.nodo("bueno", ["python", "politica.py", "cadera,rodilla,tobillo", "1.0"])
        robot += self.nodo("malo", ["python", "politica.py", "rodilla,cadera,tobillo", "99.0"])
        ok, out = self.correr(robot, lambda t: "VISTO 1.0" in t and "'malo' declara" in t)
        self.assertTrue(ok, out)
        self.assertNotIn("VISTO 99.0", out)

    def test_require_names_y_sin_lista_no_publica(self):
        robot = ('[robot]\nname = "n"\n\n[[joints]]\nname = "patas"\ncommand = "/patas/cmd"\n'
                 'names = ["cadera", "rodilla", "tobillo"]\nkp = 1.0\nkd = 0.1\nrequire_names = true\n')
        robot += self.nodo("lector", ["python", "lector.py"])
        robot += self.nodo("bueno", ["python", "politica.py", "cadera,rodilla,tobillo", "1.0"])
        # Sin lista: se escribe con python -c, que publica sin names=
        sin = ("import time,agmen;from agmen.msgs import JointCommand as J;T=J[3];p=agmen.Publisher(T,'/patas/cmd');"
               "m=T();m.position=[77.0]*3;hecho=False\n"
               "for _ in range(400):\n try: p.publish(m)\n except Exception as e:\n  if not hecho: print('SIN',e,flush=True); hecho=True\n time.sleep(0.05)")
        robot += self.nodo("sin", ["python", "-c", sin])
        ok, out = self.correr(robot, lambda t: "VISTO 1.0" in t and "require_names = true" in t)
        self.assertTrue(ok, out)
        self.assertNotIn("VISTO 77.0", out)

    # --- el [[mux]] hereda la lista (RFC 0013 D6 = d): su salida es el command de [[joints]] -----------------------
    MUX = ('\n[[mux]]\nname = "ruedas"\noutput = "/patas/cmd"\n\n[[mux.input]]\nname = "ia"\ntopic = "/cmd/ia"\n'
           'priority = 1\n')

    def test_mux_con_require_names_entra_y_compara_a_quien_publica_en_su_entrada(self):
        robot = ('[robot]\nname = "n"\n\n[[joints]]\nname = "patas"\ncommand = "/patas/cmd"\n'
                 'names = ["cadera", "rodilla", "tobillo"]\nkp = 1.0\nkd = 0.1\nrequire_names = true\n' + self.MUX)
        robot += self.nodo("lector", ["python", "lector.py"])
        robot += self.nodo("bueno", ["python", "politica.py", "cadera,rodilla,tobillo", "1.0", "/cmd/ia"])
        robot += self.nodo("malo", ["python", "politica.py", "rodilla,cadera,tobillo", "99.0", "/cmd/ia"])
        robot += self.nodo("sin", ["python", "politica.py", "-", "77.0", "/cmd/ia"])
        frag_malo = "'/cmd/ia': 'malo' declara rodilla, cadera… y robot.toml dice cadera, rodilla…"
        frag_sin = ("'/cmd/ia': 'sin' publica sin declarar su lista de nombres y robot.toml la exige "
                    "(require_names = true en [[joints]])")
        # "VISTO 1.0": lo que publicó 'bueno' en la entrada pasó por el mux y llegó al suscriptor del comando; con
        # require_names en /patas/cmd, eso solo pasa si el hub aceptó al mux (declara la lista).
        ok, out = self.correr(robot, lambda t: "VISTO 1.0" in t and frag_malo in t and frag_sin in t)
        self.assertTrue(ok, out)
        self.assertNotIn("VISTO 99.0", out)
        self.assertNotIn("VISTO 77.0", out)
        self.assertIn("la lista vale también en la entrada 'ia' del mux 'ruedas' (/cmd/ia)", out)

    def test_mux_sin_require_names_ya_no_da_el_aviso_falso(self):
        robot = ('[robot]\nname = "n"\n\n[[joints]]\nname = "patas"\ncommand = "/patas/cmd"\n'
                 'names = ["cadera", "rodilla", "tobillo"]\nkp = 1.0\nkd = 0.1\n' + self.MUX)
        robot += self.nodo("lector", ["python", "lector.py"])
        robot += self.nodo("bueno", ["python", "politica.py", "cadera,rodilla,tobillo", "1.0", "/cmd/ia"])
        robot += self.nodo("sin", ["python", "politica.py", "-", "1.0", "/cmd/ia"])
        aviso_sin = "'sin' publica en '/cmd/ia', un topic de [[joints]], sin declarar su lista de nombres"
        ok, out = self.correr(robot, lambda t: "VISTO 1.0" in t and aviso_sin in t)
        self.assertTrue(ok, out)
        self.assertNotIn("'ruedas' publica en '/patas/cmd'", out)       # el mux declara su lista
        self.assertNotIn("sin declarar su lista de nombres", out.replace(aviso_sin, ""))

    def test_perfil_distinto_no_manda_names_al_hub(self):
        base = ('[robot]\nname = "n"\n\n[[node]]\nname = "m"\ncmd = "x"\n\n[[joints]]\nname = "patas"\n'
                'state = "/patas/estado"\ncommand = "/patas/cmd"\nnames = ["a", "b"]\nrequire_names = true\n'
                'profile = "sim"\n')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "robot.toml"
            path.write_text(base, encoding="utf-8")
            for perfil, con in (("real", False), ("sim", True)):
                with self.subTest(perfil=perfil):
                    cfg = load_config(path, profile=perfil) if "profile" in load_config.__code__.co_varnames \
                        else load_config(path)
                    cfg, _ = prepare_hub(cfg, ping=lambda a: None)
                    hubn = next((n for n in cfg.nodes if n.name == "hub"), None)
                    cmd = hubn.cmd if hubn else []
                    self.assertEqual("--names" in cmd, con, cmd)
                    self.assertEqual("--require-names" in cmd, con, cmd)


# --- el núcleo de C++: valida primero y después dice qué hacer ----------------------------------------------------

@unittest.skipUnless(_backend.NATIVE, "solo con el núcleo de C++")
class TestNucleoCppValidaPrimero(unittest.TestCase):
    def test_lista_mala_da_error_de_lista_no_notimplemented(self):
        malas = [(["a", "a"], ValueError), ([], ValueError), (["a b"], ValueError), (["x" * 65], ValueError),
                 ("FR_hip", TypeError), (7, TypeError), ([1, 2], ValueError)]
        T2 = JointState[2]
        for names, error in malas:
            for cls in (agmen.Publisher, agmen.Subscriber):
                with self.subTest(names=repr(names), cls=cls.__name__):
                    with self.assertRaises(error) as ctx:
                        cls(T2, "/x", names=names)
                    self.assertNotIsInstance(ctx.exception, NotImplementedError)
        node = agmen.Node("n")
        self.addCleanup(node.close)
        with self.assertRaises(ValueError):
            node.publisher(T2, "/x", names=["a", "a"])
        with self.assertRaises(TypeError):
            node.publisher(T2, "/x", names="ab")
        with self.assertRaises(ValueError):
            node.publisher(JointState[3], "/x", names=["a", "b"])    # el número no es el del tipo

    def test_lista_buena_entra_y_va_en_el_registro(self):
        """Antes: NotImplementedError. Ahora la lista buena entra y va al hub, como en Python puro."""
        falso = FalsoHub(self)
        p = agmen.Publisher(JointState[2], "/x", names=["a", "b"])
        self.addCleanup(p.close)
        self.assertEqual(p.names, ("a", "b"))
        self.assertTrue(falso.registro("pub").endswith(b" names=a,b"))

    def test_sin_names_nada_cambia(self):
        pub = agmen.Publisher(JointState[2], "/x")
        sub = agmen.Subscriber(JointState[2], "/x")
        self.addCleanup(pub.close)
        self.addCleanup(sub.close)
        self.assertEqual(pub.type, JointState[2]) if hasattr(pub, "type") else None
        self.assertIsNone(pub.names)
        self.assertIsNone(sub.names)
        agmen.Publisher(JointState[2], "/x", names=None).close()

    def test_no_se_abre_ningun_socket_ni_se_registra_al_fallar(self):
        """names= malo con el núcleo de C++ no debe dejar nada registrado en el hub (ni revisado en Python, ni
        pasado directo al núcleo, que lo revisa otra vez antes de abrir nada)."""
        h = HubProceso(self)
        core, T2 = _backend.core, JointState[2]
        for _ in range(3):
            with self.assertRaises(ValueError):
                agmen.Publisher(T2, "/x", names=["a", "a"])
            with self.assertRaises(ValueError):
                agmen.Subscriber(T2, "/x", names=["a"])
            with self.assertRaises(wire.WireError):
                core.Publisher("/x", T2.NAME, T2.SCHEMA_TEXT, T2.SCHEMA_ID, T2.LAYOUT.size, False, "", ["a", "a"])
            with self.assertRaises(wire.WireError):
                core.Subscriber("/x", T2.NAME, T2.SCHEMA_TEXT, T2.SCHEMA_ID, T2.LAYOUT.size, False, ["a"])
        time.sleep(0.5)
        eps = [f for op, f in H.query(h.addr) if op == "ep"]
        self.assertEqual(eps, [], eps)


@unittest.skipUnless(_backend.NATIVE, "solo con el núcleo de C++")
class TestNucleoCpp(unittest.TestCase):
    """names= con el núcleo de C++: los mismos topes, los mismos textos, la misma línea y el mismo rechazo que en
    Python puro (C++: include/agmen/hubproto.hpp y src/pubsub.cpp; tests/test_nombres.cpp)."""

    def setUp(self):
        self.err = io.StringIO()
        ctx = contextlib.redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def test_los_topes_son_los_mismos(self):
        self.assertEqual(_backend.core.JOINT_NAME_MAX, hp.JOINT_NAME_MAX)
        self.assertEqual(_backend.core.JOINT_NAMES_MAX, hp.JOINT_NAMES_MAX)
        self.assertEqual(_backend.core.MAX_DATAGRAM, wire.MAX_DATAGRAM)

    def nucleo(self, cls, tipo, where, names):
        """El núcleo de C++ directo (sin la revisión de Python): el texto de su error."""
        core = _backend.core
        size = tipo.LAYOUT.size or 0
        arrow = tipo.LAYOUT.encoding == wire.ENC_ARROW
        with self.assertRaises(wire.WireError) as ctx:
            if cls == "Publisher":
                core.Publisher(where, tipo.NAME, tipo.SCHEMA_TEXT, tipo.SCHEMA_ID, size, arrow, "", names).close()
            else:
                core.Subscriber(where, tipo.NAME, tipo.SCHEMA_TEXT, tipo.SCHEMA_ID, size, arrow, names).close()
        return str(ctx.exception)

    def python(self, cls, tipo, where, names):
        with self.assertRaises(ValueError) as ctx:
            pubsub.joint_names_for(tipo, where, names, cls)
        return str(ctx.exception)

    def test_los_mismos_textos_de_error_que_python(self):
        casos = [n for n, _, _ in TestNamesAlCrear.CASOS if isinstance(n, list) and n and isinstance(n[0], str)]
        casos += [["a=b"], ["x​y"], ["a'b"], ["a\\b"], ["b" * 70 + " "], ["a\x00"], ["é" * 70], ["a\x85"],
                  ["a b"], ["\U0001F916"], [".a"], ["/"], ["a" * 64 + "é"], ["ok", "a b"], ["a", "b", "c", "b"]]
        for names in casos:
            for cls in ("Publisher", "Subscriber"):
                with self.subTest(names=repr(names)[:50], cls=cls):
                    tipo = JointState[len(names)]
                    self.assertEqual(self.nucleo(cls, tipo, "/patas/estado", names),
                                     self.python(cls, tipo, "/patas/estado", names))

    def test_el_numero_la_direccion_y_el_datagrama(self):
        for cls in ("Publisher", "Subscriber"):
            with self.subTest(cls=cls):
                self.assertEqual(self.nucleo(cls, CMD12, "/patas/cmd", list(PATAS[:10])),
                                 self.python(cls, CMD12, "/patas/cmd", list(PATAS[:10])))
                self.assertEqual(self.nucleo(cls, JointState[1], "127.0.0.1:7999", ["a"]),
                                 self.python(cls, JointState[1], "127.0.0.1:7999", ["a"]))

        class Grande(wire.Message):
            SCHEMA = "Grande{" + ";".join(f"f32 campo_{i:05d}" for i in range(2900)) + "}"

        nombres = [f"n{i}" for i in range(1500)]
        with self.assertRaises(ValueError) as ctx:
            pubsub._PurePublisher(Grande, "/grande", names=nombres)
        with self.assertRaises(ValueError) as ctx_nativo:   # el mismo texto, revisado en Python antes del núcleo
            agmen.Publisher(Grande, "/grande", names=nombres)
        self.assertEqual(str(ctx_nativo.exception), str(ctx.exception))
        self.assertEqual(self.nucleo("Publisher", Grande, "/grande", nombres), str(ctx.exception))

    def test_la_linea_con_names_es_la_de_python_puro(self):
        falso = FalsoHub(self)
        nativo = agmen.Publisher(CMD12, "/patas/cmd", names=list(PATAS))
        self.addCleanup(nativo.close)
        l_nativo = falso.registro("pub").replace(f"{nativo.source_id:016x}".encode(), b"ID")
        puro = pubsub._PurePublisher(CMD12, "/patas/cmd", names=list(PATAS))
        self.addCleanup(puro.close)
        l_puro = falso.registro("pub").replace(f"{puro.source_id:016x}".encode(), b"ID")
        self.assertEqual(l_nativo, l_puro)
        self.assertTrue(l_nativo.endswith(b" names=" + ",".join(PATAS).encode()))
        # sub: la de siempre del núcleo de C++ (un tipo compact no lleva tcp=) + names al final.
        sin = agmen.Subscriber(JointState[3], "/brazo/estado")
        self.addCleanup(sin.close)
        l_sin = falso.registro("sub").decode()
        con = agmen.Subscriber(JointState[3], "/brazo/estado", names=["hombro", "codo", "mano.1"])
        self.addCleanup(con.close)
        l_con = falso.registro("sub").decode()

        def sin_id_ni_puerto(line, s):
            return re.sub(r" id=\S+", " id=ID", line).replace(f" port={s.port}", " port=P")

        self.assertEqual(sin_id_ni_puerto(l_con, con), sin_id_ni_puerto(l_sin, sin) + " names=hombro,codo,mano.1")
        self.assertNotIn("names", l_sin)


@unittest.skipUnless(_backend.NATIVE, "solo con el núcleo de C++")
class TestNucleoCppHubDeVerdad(_HubDeVerdad):
    """Contra un `agmen hub` de verdad (con --names y --require-names como agmen run), con el núcleo de C++."""

    def test_bien_entra_los_datos_llegan_y_el_orden_cambiado_no(self):
        bien = self.abrir(agmen.Publisher, CMD12, "/patas/cmd", names=list(PATAS))
        sub = self.abrir(agmen.Subscriber, CMD12, "/patas/cmd", names=list(PATAS))
        self.assertTrue(until(lambda: sub.receive(0.05) is not None, every=lambda: bien.publish(CMD12())),
                        self.hub.text())
        otro = list(PATAS[3:6] + PATAS[:3] + PATAS[6:])
        mal = self.abrir(agmen.Publisher, CMD12, "/patas/cmd", names=otro)
        msg = self.rechazado(lambda: mal.publish(CMD12()))
        self.assertIn(f"el hub rechazó publicar en '/patas/cmd': '/patas/cmd': '{pubsub.node_name()}' declara FL_hip, "
                      f"FL_thigh… y robot.toml dice FR_hip, FR_thigh…", msg)
        sub_mal = self.abrir(agmen.Subscriber, JointState[12], "/patas/estado", names=otro)
        self.assertIn("el hub rechazó escuchar '/patas/estado': '/patas/estado': ",
                      self.rechazado(lambda: sub_mal.receive(0.05)))
        bien.publish(CMD12())                     # el que declaró bien sigue

    def test_el_texto_del_rechazo_llega_entero(self):
        """El texto del hub con listas largas mide ~1100 caracteres: llega entero, igual que en Python puro."""
        largos = [f"articulacion_muy_larga_numero_{i:02d}" for i in range(12)]
        nativo = self.abrir(agmen.Publisher, CMD12, "/patas/cmd", names=largos)
        msg_nativo = self.rechazado(lambda: nativo.publish(CMD12()))
        nativo.close()
        puro = self.abrir(pubsub._PurePublisher, CMD12, "/patas/cmd", names=largos, node=pubsub.node_name())
        msg_puro = self.rechazado(lambda: puro.publish(CMD12()))
        self.assertGreater(len(msg_nativo), 300)
        self.assertEqual(msg_nativo, msg_puro)

    def test_require_names_rechaza_al_nativo_sin_lista(self):
        viejo = self.abrir(agmen.Publisher, CMD12, "/patas/cmd")
        self.assertIn("robot.toml la exige (require_names = true en [[joints]])",
                      self.rechazado(lambda: viejo.publish(CMD12())))

    def test_node_nativo_con_names(self):
        node = agmen.Node("politica_nativa")
        self.addCleanup(node.close)
        got = []
        p = node.publisher(CMD12, "/patas/cmd", names=list(PATAS))
        node.on(CMD12, "/patas/cmd", got.append, names=list(PATAS))
        node.every(0.02, lambda: p.publish(CMD12()))
        self.assertTrue(until(lambda: bool(got), every=lambda: node.spin_for(0.05)), self.hub.text())
        # Con otro orden, Node.on detiene el nodo con el rechazo del hub (como cualquier rechazo).
        node2 = agmen.Node("otro_nativo")
        self.addCleanup(node2.close)
        node2.on(JointState[12], "/patas/estado", lambda s: None, names=list(PATAS[::-1]))
        got2 = []

        def paso():
            try:
                node2.spin_for(0.05)
            except wire.HubRejected as e:
                got2.append(str(e))

        self.assertTrue(until(lambda: bool(got2), every=paso), self.hub.text())
        self.assertIn("el hub rechazó escuchar '/patas/estado'", got2[0])
        self.assertIn("declara RL_calf, RL_thigh…", got2[0])

    def test_cpp_y_python_puro_se_entienden_y_el_segundo_con_otra_lista_no(self):
        otro = list(PATAS[::-1])
        for orden in ("C++ primero", "Python puro primero"):
            with self.subTest(orden=orden):
                topic = "/brazo/" + ("a" if orden == "C++ primero" else "b")
                T12 = JointState[12]
                if orden == "C++ primero":
                    pub = self.abrir(agmen.Publisher, T12, topic, names=list(PATAS))
                    sub = self.abrir(pubsub._PureSubscriber, T12, topic, names=list(PATAS))
                    mal = self.abrir(pubsub._PurePublisher, T12, topic, names=otro)
                else:
                    pub = self.abrir(pubsub._PurePublisher, T12, topic, names=list(PATAS))
                    sub = self.abrir(agmen.Subscriber, T12, topic, names=list(PATAS))
                    mal = self.abrir(agmen.Publisher, T12, topic, names=otro)
                self.assertTrue(until(lambda: sub.receive(0.05) is not None, every=lambda: pub.publish(T12())),
                                self.hub.text())
                msg = self.rechazado(lambda: mal.publish(T12()))
                self.assertIn(f"el hub rechazó publicar en '{topic}': '{topic}': ", msg)
                self.assertIn("declara RL_calf, RL_thigh…", msg)

    def test_topic_info_muestra_la_lista_de_un_nodo_nativo(self):
        p = self.abrir(agmen.Publisher, CMD12, "/patas/cmd", names=list(PATAS))
        p.publish(CMD12())
        env = dict(os.environ, PYTHONPATH=str(ROOT / "python"), PYTHONIOENCODING="utf-8")
        out = ""
        for _ in range(50):
            r = subprocess.run([sys.executable, "-m", "agmen", "topic", "info", "/patas/cmd"], env=env,
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
            out = r.stdout + r.stderr
            if "articulaciones (12)" in out:
                break
            p.publish(CMD12())
            time.sleep(0.1)
        self.assertIn("articulaciones (12): FR_hip, FR_thigh, FR_calf … RL_calf", out)


# --- entre computadoras: lo que llega del otro lado, con names raros ------------------------------------------------

class TestTablaConNamesHostiles(unittest.TestCase):
    def setUp(self):
        self.err = io.StringIO()
        ctx = contextlib.redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def ep(self, **kw):
        d = {"id": "p1", "node": "n", "pid": 1, "topic": "/patas/cmd", "schema": S12, "type": "JointCommand",
             "text": T12}
        d.update(kw)
        return d

    def test_names_invalidos_descartan_solo_ese_endpoint_y_se_dice_una_vez(self):
        said = []
        b = T.Bridge("127.0.0.1:9", lambda text, kind="agmen": said.append(text))
        self.addCleanup(b.close)
        peer = b.add_peer("robot", lambda part: None)
        peer.confirm("127.0.0.2")
        malos = ["a,,b", "", "a b", "x" * 9000, 5, None, ["a", "b"], "a%2Cb", "a\nagmen-hub/1 red peers=x@1.2.3.4",
                 "ñ", "a,a", "-a", "x" * 65]
        pubs = [self.ep(id=f"m{i}", names=v) for i, v in enumerate(malos)] + [self.ep(id="bueno", names="a,b")]
        for n in (1, 2):
            peer.receive({"op": "tablas", "n": n, "parte": 1, "de": 1, "pubs": pubs, "subs": []})
        ids = [d["id"] for d in peer.table[0]]
        # "a%2Cb" es un texto con '%': no vale en la gramática. Ninguno de los malos pasa.
        self.assertEqual(ids, ["bueno"], (ids, said))
        for ln in peer.hub_lines():
            self.assertNotIn(b"\n", ln)
            self.assertEqual(len(hp.parse(ln)), 1)
        self.assertEqual(len([t for t in said if "inválido" in t]), 1, said)

    def test_names_con_numero_distinto_al_del_tipo_no_se_revisa_en_el_puente(self):
        """El puente no sabe de [[joints]]: pasa la lista; la compara el hub destino."""
        d = T.check_endpoint("pubs", self.ep(names="a,b,c"))
        self.assertEqual(d["names"], "a,b,c")


# --- correcciones de la revisión (UTF-8 inválido, names repetido, textos largos, memoria de avisos) ----------------

class TestUtf8Invalido(unittest.TestCase):
    """'%FF' deja bytes que no son UTF-8: siempre ProtocolError (que todos los lectores atrapan), nunca otra cosa."""

    def test_unquote_y_parse_dan_protocol_error(self):
        for value in ("%FF", "a%C3", "%E4%B8"):
            with self.subTest(value=value), self.assertRaises(hp.ProtocolError) as ctx:
                hp.unquote(value, "names")
            self.assertIn("el campo 'names' no es texto UTF-8 después de leer sus '%XX'", str(ctx.exception))
            self.assertIn("escribe el valor en UTF-8", str(ctx.exception))
        with self.assertRaisesRegex(hp.ProtocolError, "el campo 'node' no es texto UTF-8"):
            hp.parse(linea_pub("h") + b" node=%FF")
        with self.assertRaisesRegex(hp.ProtocolError, "el campo 'a\\?b' no es texto UTF-8"):   # clave sin controles
            hp.parse_line(f"{hp.PREFIX} ping a\x1bb=%FF")
        self.assertEqual(hp.unquote("%C3%B1"), "ñ")    # UTF-8 bueno: como siempre

    def test_el_nodo_tampoco_se_cae(self):
        """Un Publisher puro lee las respuestas del hub con hp.parse: un datagrama con %FF del hub no lo tumba."""
        falso = FalsoHub(self)
        pub = pubsub._PurePublisher(CMD12, "/patas/cmd", names=list(PATAS))
        self.addCleanup(pub.close)
        data, addr = falso.sock.recvfrom(65536)
        falso.sock.sendto(hp.encode("subs", id=pub._link.id, topic="/patas/cmd", to="") + b" x=%FF", addr)
        time.sleep(0.05)
        pub.publish(CMD12())       # lee la respuesta: no debe lanzar


class TestServeNoSeCae(unittest.TestCase):
    """Un datagrama que hace fallar a handle con algo inesperado: el hilo sigue, y se dice una vez por tipo."""

    def test_error_inesperado_se_dice_una_vez_y_sigue(self):
        logs: list[str] = []

        class Fragil(H.Hub):
            def handle(self, data, addr, now):
                if data.startswith(b"boom"):
                    kind = data.split(b" ")[1].decode()
                    raise type(kind, (Exception,), {})(f"falla \x1b[2J\u202e {'x' * 500}")
                return super().handle(data, addr, now)

        hub = Fragil(f"127.0.0.1:{free_port()}", log=logs.append, grace_s=0.0)
        stop = threading.Event()
        hilo = threading.Thread(target=hub.serve, args=(stop,), daemon=True)
        hilo.start()
        self.addCleanup(hilo.join, 2)
        self.addCleanup(stop.set)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(s.close)
        for _ in range(5):
            s.sendto(b"boom Raro", ("127.0.0.1", int(hub.listen.rsplit(":", 1)[1])))
        for i in range(H.UNEXPECTED_MAX + 10):
            s.sendto(f"boom Tipo{i}".encode(), ("127.0.0.1", int(hub.listen.rsplit(":", 1)[1])))
        self.assertTrue(until(lambda: any("ya dije" in x for x in logs)), logs)
        self.assertIsNotNone(H.ping(hub.listen, timeout=2.0), logs)     # sigue atendiendo
        self.assertTrue(hilo.is_alive())
        raro = [x for x in logs if "(Raro:" in x]
        self.assertEqual(len(raro), 1, logs)                       # una vez por tipo
        self.assertTrue(raro[0].isprintable(), repr(raro[0]))      # sin controles
        self.assertIn("me hizo fallar (Raro: falla ?[2J? xxx", raro[0])
        self.assertIn("lo ignoro y sigo atendiendo. Es un error de Agmen", raro[0])
        self.assertLess(len(raro[0]), H.UNEXPECTED_SHOWN + 250)    # acotada
        fallas = [x for x in logs if "me hizo fallar" in x]
        self.assertEqual(len(fallas), H.UNEXPECTED_MAX, logs)      # con tope
        self.assertEqual(len([x for x in logs if "ya dije" in x]), 1)


class TestErrorLargoPorElPuente(unittest.TestCase):
    """El rechazo más largo de D6 (topic de 256, nodos y computadoras largos, nombres de 64, largos distintos) llega
    entero, con su "qué hacer", al aviso del puente (tables.Bridge._drain lo corta en HUB_ERROR_MAX)."""

    TOPIC = "/" + "t" * (T.TOPIC_MAX - 1)

    def rechazo_peor(self, contra_toml: bool) -> str:
        a = tuple(f"{i:04d}" + "a" * 60 for i in range(100))
        b = a[:50] + tuple(f"{i:04d}" + "b" * 60 for i in range(50, 120))
        joints = [H.JointNames(self.TOPIC, b)] if contra_toml else None
        hub = H.Hub(f"127.0.0.1:{free_port()}", log=lambda _: None, joints=joints)
        self.addCleanup(hub.sock.close)
        sent: list[bytes] = []
        hub._send = lambda data, addr: sent.append(data)
        m1, m2 = "M" * 64, "N" * 64
        hub.handle(hp.encode("red", peers=f"{m1}@192.168.1.20,{m2}@192.168.1.21"), LOCAL, 0.0)
        if not contra_toml:
            hub.handle(hp.encode("pub", id="x", node="o" * T.NODE_MAX, pid=1, topic=self.TOPIC, schema=S12,
                                 type="T", text="", machine=m2, names=",".join(b)), LOCAL, 0.0)
        sent.clear()
        hub.handle(hp.encode("pub", id="y", node="n" * T.NODE_MAX, pid=1, topic=self.TOPIC, schema=S12, type="T",
                             text="", machine=m1, names=",".join(a)), LOCAL, 0.0)
        (op, f), = hp.parse(sent[-1])
        self.assertEqual(op, "error")
        return sent[-1]

    def por_el_puente(self, data: bytes) -> str:
        said: list[str] = []
        bridge = T.Bridge("127.0.0.1:9", lambda text, kind="agmen": said.append(text))
        self.addCleanup(bridge.close)
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.setblocking(False)
        self.addCleanup(rx.close)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(tx.close)
        tx.sendto(data, rx.getsockname())
        bridge._sock = rx
        self.assertTrue(until(lambda: (bridge._drain(), said)[1]), "el puente no dijo nada")
        return said[0]

    def test_el_peor_caso_conserva_el_que_hacer(self):
        for contra_toml, final in ((True, "o corrige robot.toml si el que está mal es él"),
                                   (False, "Corrige la de uno de los dos (o declara el orden en [[joints]] de "
                                           "robot.toml)")):
            with self.subTest(contra_toml=contra_toml):
                data = self.rechazo_peor(contra_toml)
                msg = hp.parse(data)[0][1]["msg"]
                self.assertLessEqual(len(msg), T.HUB_ERROR_MAX)
                self.assertTrue(msg.endswith(final), msg[-200:])
                self.assertIn("(cambian desde la articulación 51; 100 nombres contra 120)", msg)
                self.assertEqual(msg.count("n" * 60), 1, "el nombre del nodo se repite")
                aviso = self.por_el_puente(data)
                self.assertTrue(aviso.endswith(final), aviso[-200:])
                self.assertIn(msg, aviso)

    def test_el_principio_sigue_siendo_el_del_rfc(self):
        hub = H.Hub(f"127.0.0.1:{free_port()}", log=lambda _: None, joints=joints_del_rfc())
        self.addCleanup(hub.sock.close)
        sent: list[bytes] = []
        hub._send = lambda data, addr: sent.append(data)
        otro = ("FL_hip", "FL_thigh", "FL_calf", "FR_hip", "FR_thigh", "FR_calf") + PATAS[6:]
        hub.handle(hp.encode("pub", id="p", node="politica", pid=1, topic="/patas/cmd", schema=S12,
                             type="JointCommand", text=T12, names=",".join(PATAS)), LOCAL, 0.0)
        hub.joints = {}       # sin robot.toml: contra la de otro nodo, como en el RFC
        sent.clear()
        hub.handle(hp.encode("pub", id="q", node="politica2", pid=1, topic="/patas/cmd", schema=S12,
                             type="JointCommand", text=T12, names=",".join(otro)), LOCAL, 0.0)
        msg = hp.parse(sent[-1])[0][1]["msg"]
        self.assertTrue(msg.startswith("'/patas/cmd': 'politica2' declara FL_hip, FL_thigh… y 'politica', que ya "
                                       "está en el topic, declara FR_hip, FR_thigh… (cambian desde la articulación 1 "
                                       "de 12)"), msg)


class TestAvisosNoCrecen(HubEnMemoria):
    """Los avisos de D6 van por id de endpoint: al irse el endpoint se olvidan (un nodo que vuelve en bucle con ids
    nuevos no suma memoria para siempre)."""

    def setUp(self):
        self.nuevo_hub(joints_del_rfc())

    def test_bye_y_caducar_olvidan_los_avisos(self):
        t10 = JointState[10]
        for i in range(300):
            eid = f"bucle{i}"
            self.entra(self.pub(eid, topic="/patas/estado", text=t10.SCHEMA_TEXT, schema=f"{t10.SCHEMA_ID:016x}",
                                tipo="JointState"))       # dos avisos: sin lista y 10 contra 12
            if i % 2:
                self.manda("bye", id=eid)
            else:
                self.hub.expire(self.hub.endpoints[eid].last_seen + hp.EXPIRE_S + 1)
        self.assertEqual(self.hub.endpoints, {})
        self.assertEqual({k for k in self.hub._warned if k[0] in ("sin-names", "n-joints")}, set())
        self.assertEqual(len(self.avisos("sin declarar su lista")), 300)   # cada aparición se dijo una vez

    def test_quitado_por_salir_de_peers_tambien(self):
        self.manda("red", peers="laptop@192.168.1.20")
        self.entra(self.pub("r.1", machine="laptop"))
        self.assertIn(("sin-names", "r.1"), self.hub._warned)
        self.manda("red", peers="")
        self.assertNotIn("r.1", self.hub.endpoints)
        self.assertNotIn(("sin-names", "r.1"), self.hub._warned)

    def pub(self, eid, names=None, machine=None, **kw):
        if machine is None:
            return super().pub(eid, names, **kw)
        return self.manda("pub", id=eid, node=eid, pid=1, topic="/patas/cmd", schema=S12, type="JointCommand",
                          text=T12, machine=machine)


class TestReregistroSinLista(HubEnMemoria):
    """Un endpoint que declaró su lista y luego late sin names: queda sin lista (como un Publisher que no la trae),
    así que sale el aviso (una vez) y require_names lo rechaza."""

    def test_sin_require_avisa_una_vez(self):
        self.nuevo_hub(joints_del_rfc())
        self.entra(self.pub("p", PATAS))
        self.assertEqual(self.avisos("sin declarar su lista"), [])
        for _ in range(3):
            self.entra(self.pub("p"))
        self.assertIsNone(self.hub.endpoints["p"].names)
        avisos = self.avisos("sin declarar su lista")
        self.assertEqual(len(avisos), 1, self.logs)
        self.assertIn("'p' publica en '/patas/cmd', un topic de [[joints]], sin declarar su lista de nombres", avisos[0])

    def test_con_require_se_rechaza_y_conserva_su_lista(self):
        self.nuevo_hub(joints_del_rfc(require=True))
        self.entra(self.pub("p", PATAS))
        msg = self.rechazo(self.pub("p"))
        self.assertIn("'/patas/cmd': 'p' publica sin declarar su lista de nombres y robot.toml la exige", msg)
        self.assertEqual(self.hub.endpoints["p"].names, PATAS)


class TestTablaLocalSinNamesDudosos(unittest.TestCase):
    """tables.local_table no copia names de una línea `ep` recortada (cortado=1) ni de una que lo trae dos veces;
    el endpoint pasa sin lista y se dice."""

    def respuesta(self, *lineas: str):
        repetidas: list = []
        msgs = H.Respuesta(hp.parse("\n".join(lineas).encode(), repetidas))
        msgs.repetidas = repetidas
        return msgs

    TOPIC = hp.line("topic", name="/patas/cmd", type="JointCommand", schema=S12, text=T12, pubs=1, subs=0,
                    expected="no")

    def ep(self, eid, **extra):
        return hp.line("ep", id=eid, kind="pub", node=eid, pid=1, topic="/patas/cmd", type="JointCommand",
                       addr="127.0.0.1:1", tcp="", age="0.1", **extra)

    def test_cortado_y_repetido(self):
        msgs = self.respuesta(self.TOPIC, self.ep("bien", names=",".join(PATAS)),
                              self.ep("cortado", names="FR_hip,FR_th", cortado=1),
                              self.ep("doble", names=",".join(PATAS)) + " names=" + ",".join(PATAS[::-1]))
        unnamed: list[str] = []
        pubs, _ = T.local_table(msgs, [], unnamed)
        by = {p["id"]: p for p in pubs}
        self.assertEqual(by["bien"]["names"], ",".join(PATAS))
        self.assertNotIn("names", by["cortado"])
        self.assertNotIn("names", by["doble"])
        self.assertEqual(unnamed, ["'cortado' en /patas/cmd", "'doble' en /patas/cmd"])
        # Sin saber las repetidas (una lista sin .repetidas): como siempre, la última; la recortada, nunca.
        plain = [hp.parse_line(ln) for ln in (self.TOPIC, self.ep("cortado", names="a", cortado=1))]
        self.assertNotIn("names", T.local_table(plain)[0][0])

    def test_query_sabe_las_repetidas(self):
        """hub.query deja en la Respuesta qué claves vinieron repetidas en cada línea (en uno y en varios datagramas)."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.bind(("127.0.0.1", 0))
        srv.settimeout(TOPE_S)
        self.addCleanup(srv.close)
        texto = "\n".join([hp.line("state", version="x", hub="h", robot=""), self.TOPIC,
                           self.ep("doble", names="a") + " names=b"])
        largo = max(len(ln.encode()) for ln in texto.split("\n"))
        partes = H.split_state(texto, 1, part_bytes=largo + 100)    # una línea por parte, ninguna recortada
        self.assertGreater(len(partes), 1)
        for respuesta in ([texto.encode()], partes):
            with self.subTest(partes=len(respuesta)):
                def contesta():
                    _, addr = srv.recvfrom(65536)
                    for d in respuesta:
                        srv.sendto(d, addr)
                hilo = threading.Thread(target=contesta)
                hilo.start()
                msgs = H.query(f"127.0.0.1:{srv.getsockname()[1]}", timeout=2.0, say=None)
                hilo.join()
                self.assertEqual(len(msgs.repetidas), len(msgs))
                i = next(k for k, (op, f) in enumerate(msgs) if op == "ep")
                self.assertEqual(msgs.repetidas[i], frozenset({"names"}))
                unnamed: list[str] = []
                pubs, _ = T.local_table(msgs, [], unnamed)
                self.assertNotIn("names", pubs[0])
                self.assertEqual(unnamed, ["'doble' en /patas/cmd"])


class TestNombresSinDuplicar(unittest.TestCase):
    def test_un_solo_nombre_para_cada_cosa(self):
        self.assertFalse(hasattr(H, "parse_joint_names"), "el de los argumentos se llama parse_names_args")
        self.assertFalse(hasattr(H.Hub, "_who_text"), "_who es el único")
        self.assertEqual(H.Hub._who("n\x1b" + "x" * 100, "laptop"),
                         "'n?" + "x" * 61 + "…' (de la computadora 'laptop')")

    def test_el_tope_de_la_red_dice_que_hacer(self):
        with self.assertRaises(hp.ProtocolError) as ctx:
            hp.parse_joint_names(",".join(["a"] * 5000))
        self.assertEqual(str(ctx.exception), "names ocupa 9999 caracteres (los 5000 nombres separados por comas) y el "
                                             "máximo es 8192: usa nombres más cortos o reparte las articulaciones "
                                             "en varios grupos (un topic por grupo)")

# --- nombres de nodo enormes que caben en una variable de entorno de Windows ----------------------------------------
# En Windows una variable de entorno mide como mucho 32 767 caracteres: os.environ["AGMEN_NODE"] = "n" * 60000 da
# ValueError (y en un subproceso, env= con ese valor tampoco sirve). Con '%', que en la línea del hub va como "%25"
# (3 bytes por carácter, y es ASCII: mide lo mismo leído por Python y por el núcleo de C++ en cualquier página de
# códigos), el registro pasa de 65507 bytes con menos de 25 000 caracteres: la MISMA prueba corre en Linux y Windows.
# El tope de Windows es para todo `NOMBRE=valor` (con el '\0'), no solo para el valor: las pruebas no pasan de 32 000
# caracteres en el valor, que deja lugar de sobra para el nombre de la variable y el '='.
MAX_ENV = 32000


def nodo_que_mide(n: int) -> str:
    """Un nombre de nodo que, escrito como va en la línea del hub (hp.quote), mide `n` bytes ((n - 4) % 3 == 0)."""
    nodo = "nodo" + "%" * ((n - 4) // 3)
    assert len(hp.quote(nodo)) == n and len(nodo) <= MAX_ENV, (n, len(nodo))
    return nodo


# --- ataques del tester: el rechazo sigue siendo un WireError, mezclas C++/Python y fugas ---------------------------

class _HubPlano(unittest.TestCase):
    """Un `agmen hub` de verdad, sin --names ni --require-names (cada topic aprende la lista del primero)."""

    def setUp(self):
        self.err = io.StringIO()
        ctx = contextlib.redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.hub = HubProceso(self)

    def abrir(self, cls, *args, **kw):
        o = cls(*args, **kw)
        self.addCleanup(o.close)
        return o

    def rechazado(self, publicar, excepcion=wire.WireError):
        """Hasta que el nodo ve el rechazo (atrapado como `excepcion`): su texto y la excepción."""
        got: list[BaseException] = []

        def paso():
            try:
                publicar()
            except excepcion as e:
                got.append(e)
            time.sleep(0.02)

        until(lambda: bool(got), every=paso)
        self.assertTrue(got, self.hub.text())
        return got[0]


class TestRechazoSigueSiendoWireError(_HubPlano):
    """Quien atrapaba WireError sigue atrapando el rechazo del hub (por tipo y por nombres), con el núcleo que
    haya; y es HubRejected (más fino)."""

    def test_rechazo_por_tipo_y_por_nombres_son_wire_error_y_hub_rejected(self):
        bien = self.abrir(agmen.Publisher, JointCommand[2], "/r/cmd", names=["a", "b"])
        bien.publish(JointCommand[2]())
        # Por tipo.
        otro_tipo = self.abrir(agmen.Publisher, JointState[2], "/r/cmd")
        e_tipo = self.rechazado(lambda: otro_tipo.publish(JointState[2]()))
        # Por nombres (publicar y escuchar).
        mal = self.abrir(agmen.Publisher, JointCommand[2], "/r/cmd", names=["b", "a"])
        e_pub = self.rechazado(lambda: mal.publish(JointCommand[2]()))
        sub_mal = self.abrir(agmen.Subscriber, JointCommand[2], "/r/cmd", names=["b", "a"])
        e_sub = self.rechazado(lambda: sub_mal.receive(0.05))
        for e in (e_tipo, e_pub, e_sub):
            self.assertIsInstance(e, wire.HubRejected)
            self.assertIsInstance(e, wire.WireError)
        self.assertIn("ya es de tipo", str(e_tipo))
        self.assertRegex(str(e_pub), r"declara b, a y '[^']+', que ya está en el topic, declara a, b \(cambian desde la "
                                     r"articulación 1 de 2\)")
        self.assertTrue(str(e_pub).startswith("el hub rechazó publicar en '/r/cmd': "), str(e_pub))
        self.assertTrue(str(e_sub).startswith("el hub rechazó escuchar '/r/cmd': "), str(e_sub))

    def test_el_mismo_texto_con_el_nucleo_y_con_python_puro(self):
        bien = self.abrir(agmen.Publisher, JointCommand[2], "/r/cmd", names=["a", "b"])
        bien.publish(JointCommand[2]())
        nodo = pubsub.node_name()
        a = self.abrir(agmen.Publisher, JointState[2], "/r/cmd")
        b = self.abrir(pubsub._PurePublisher, JointState[2], "/r/cmd", node=nodo)
        ea = self.rechazado(lambda: a.publish(JointState[2]()))
        eb = self.rechazado(lambda: b.publish(JointState[2]()))
        self.assertEqual(str(ea), str(eb))
        c = self.abrir(agmen.Publisher, JointCommand[2], "/r/cmd", names=["b", "a"])
        d = self.abrir(pubsub._PurePublisher, JointCommand[2], "/r/cmd", names=["b", "a"], node=nodo)
        ec = self.rechazado(lambda: c.publish(JointCommand[2]()))
        ed = self.rechazado(lambda: d.publish(JointCommand[2]()))
        self.assertEqual(str(ec), str(ed))
        self.assertEqual(type(ec), type(ed))

    def test_el_rechazo_largo_llega_entero_con_los_dos_nucleos(self):
        # Los dos con nombres de 64 caracteres: el texto del hub mide más de 1000 caracteres y no se corta.
        largos = ["j" * 63 + str(i % 10) if i < 10 else "k" * 62 + f"{i:02d}" for i in range(12)]
        bien = self.abrir(pubsub._PurePublisher, CMD12, "/r/larga", names=largos, node="primero")
        bien.publish(CMD12())
        otro = largos[::-1]
        a = self.abrir(agmen.Publisher, CMD12, "/r/larga", names=otro)
        b = self.abrir(pubsub._PurePublisher, CMD12, "/r/larga", names=otro, node=pubsub.node_name())
        ea = str(self.rechazado(lambda: a.publish(CMD12())))
        eb = str(self.rechazado(lambda: b.publish(CMD12())))
        self.assertEqual(ea, eb)
        self.assertGreater(len(ea), 150)
        self.assertNotIn("…" * 3, ea)


@unittest.skipUnless(_backend.NATIVE, "solo con el núcleo de C++")
class TestNucleoCppMezclasConHubPlano(_HubPlano):
    def test_cpp_publica_con_lista_y_python_puro_se_suscribe_con_la_misma_y_con_otra(self):
        T = JointState[3]
        lista = ["hombro", "codo", "mano.1"]
        pub = self.abrir(agmen.Publisher, T, "/m/estado", names=lista)
        sub = self.abrir(pubsub._PureSubscriber, T, "/m/estado", names=lista)
        self.assertTrue(until(lambda: sub.receive(0.05) is not None, every=lambda: pub.publish(T())), self.hub.text())
        otra = self.abrir(pubsub._PureSubscriber, T, "/m/estado", names=["codo", "hombro", "mano.1"])
        e = self.rechazado(lambda: otra.receive(0.05), wire.HubRejected)
        self.assertRegex(str(e).replace("…", ""), r"declara codo, hombro y '[^']+', que ya está en el topic, "
                                                    r"declara hombro, codo")
        # El que declaró bien sigue recibiendo.
        self.assertTrue(until(lambda: sub.receive(0.05) is not None, every=lambda: pub.publish(T())))

    def test_python_puro_publica_y_cpp_se_suscribe(self):
        T = JointState[3]
        lista = ["hombro", "codo", "mano.1"]
        pub = self.abrir(pubsub._PurePublisher, T, "/m/estado2", names=lista, node="puro")
        sub = self.abrir(agmen.Subscriber, T, "/m/estado2", names=lista)
        self.assertTrue(until(lambda: sub.receive(0.05) is not None, every=lambda: pub.publish(T())), self.hub.text())

    def test_sub_nativo_sin_lista_entra_junto_a_los_que_la_traen(self):
        T = JointState[3]
        pub = self.abrir(agmen.Publisher, T, "/m/estado3", names=["a", "b", "c"])
        sub = self.abrir(agmen.Subscriber, T, "/m/estado3")        # sin lista: entra como hoy
        self.assertTrue(until(lambda: sub.receive(0.05) is not None, every=lambda: pub.publish(T())), self.hub.text())

    def test_200_publishers_nativos_con_lista_no_dejan_sockets_ni_endpoints(self):
        T = JointState[2]
        # (calentar: el primer Publisher crea lo global del proceso)
        self.abrir(agmen.Publisher, T, "/f/x", names=["a", "b"]).close()
        fds = lambda: len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else -1   # noqa: E731
        antes = fds()
        for i in range(200):
            p = agmen.Publisher(T, f"/f/t{i % 5}", names=["a", "b"])
            p.publish(T())
            if i % 3 == 0:
                s = agmen.Subscriber(T, f"/f/t{i % 5}", names=["a", "b"])
                s.close()
            p.close()
        if antes >= 0:
            self.assertLessEqual(fds(), antes, "se quedaron sockets abiertos")

        def endpoints():
            msgs = H.query(self.hub.addr, timeout=2.0, say=None)
            return [f for op, f in msgs if op == "ep" and f.get("topic", "").startswith("/f/")]

        self.assertTrue(until(lambda: not endpoints()), f"quedaron endpoints en el hub: {endpoints()[:3]}")

    def test_publisher_nativo_se_borra_sin_close_y_deja_el_hub_limpio(self):
        import gc
        T = JointState[2]
        for i in range(20):
            p = agmen.Publisher(T, f"/g/t{i}", names=["a", "b"])
            p.publish(T())
            del p
        gc.collect()

        def endpoints():
            msgs = H.query(self.hub.addr, timeout=2.0, say=None)
            return [f for op, f in msgs if op == "ep" and f.get("topic", "").startswith("/g/")]

        self.assertTrue(until(lambda: not endpoints()), f"quedaron endpoints en el hub: {endpoints()[:3]}")

    def test_el_nombre_de_64_y_65_caracteres_con_el_nucleo(self):
        T = JointState[1]
        self.abrir(agmen.Publisher, T, "/n/64", names=["a" * 64])
        with self.assertRaises(ValueError) as ctx:
            agmen.Publisher(T, "/n/65", names=["a" * 65])
        self.assertIn("mide 65 caracteres y el máximo es 64", str(ctx.exception))

    def test_5453_articulaciones_dan_el_error_de_la_lista_con_los_dos_nucleos(self):
        T = JointState[5453]
        nombres = [f"n{i}" for i in range(5453)]
        for cls in (agmen.Publisher, pubsub._PurePublisher):
            with self.subTest(cls=cls.__name__):
                with self.assertRaises(ValueError) as ctx:
                    cls(T, "/n/enorme", names=nombres)
                self.assertIn("names ocupa 31607 caracteres (los 5453 nombres separados por comas) y el máximo es "
                              "8192", str(ctx.exception))

    def test_nombres_con_formas_raras_dan_el_mismo_error_en_los_dos_nucleos(self):
        T = JointState[2]
        for names in (["\udc80", "b"], [b"a", "b"], ["a\x00b", "c"], ["a\r\nagmen-hub/1 bye id=x", "b"], [None, "b"],
                      ["é", "b"], ["a", "a"], ["", "b"]):
            for cls in ("Publisher", "Subscriber"):
                with self.subTest(names=repr(names), cls=cls):
                    with self.assertRaises(ValueError) as c_nativo:
                        getattr(agmen, cls)(T, "/n/raro", names=names)
                    with self.assertRaises(ValueError) as c_puro:
                        getattr(pubsub, "_Pure" + cls)(T, "/n/raro", names=names)
                    self.assertEqual(str(c_nativo.exception), str(c_puro.exception))
        with self.assertRaises(TypeError):
            agmen.Publisher(T, "/n/raro", names="ab")

    def test_hilos_creando_publishers_nativos_con_lista_a_la_vez(self):
        T = JointState[2]
        fallos: list[BaseException] = []

        def hilo(k):
            try:
                for i in range(15):
                    p = agmen.Publisher(T, f"/hi/t{k}_{i}", names=[f"a{k}", f"b{i}"])
                    p.publish(T())
                    p.close()
            except BaseException as e:   # noqa: BLE001
                fallos.append(e)

        hilos = [threading.Thread(target=hilo, args=(k,)) for k in range(8)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(60)
        self.assertEqual(fallos, [])
        self.assertTrue(until(lambda: not [f for op, f in H.query(self.hub.addr, timeout=2.0, say=None)
                                           if op == "ep" and f.get("topic", "").startswith("/hi/")]))



class TestRegistroGrandeDeUnSubscriber(unittest.TestCase):
    """El registro (`pub` o `sub`) con names debe caber en un datagrama (65507). El Publisher lo revisa al crear; el
    Subscriber tiene que hacer lo mismo o decir algo: no quedarse sin registrar y sin decir por qué."""

    SCRIPT = r"""
import os, socket, sys, io, contextlib, time
hub = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); hub.bind(("127.0.0.1", 0)); hub.settimeout(0.3)
os.environ["AGMEN_HUB"] = f"127.0.0.1:{hub.getsockname()[1]}"
import agmen
from agmen.msgs import JointState
T = JointState[100]
names = [("j%060d" % i)[:64] for i in range(100)]
err = io.StringIO()
with contextlib.redirect_stderr(err):
    try:
        o = getattr(agmen, sys.argv[1])(T, "/x", names=names)
    except Exception as e:
        print("ERROR:", e); sys.exit(0)
    grande = False
    end = time.monotonic() + 2.0
    while time.monotonic() < end:
        try:
            d, _ = hub.recvfrom(70000)
            grande = grande or len(d) > 1000
        except socket.timeout:
            pass
    o.close()
print("DATAGRAMA" if grande else "NADA: " + err.getvalue().strip().replace("\n", " | ")[:300])
"""

    def corre(self, cls, nodo):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "python"), AGMEN_NODE=nodo, PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, "-c", self.SCRIPT, cls], env=dict(env, PYTHONPATH=str(ROOT / "python")),
                           capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT / "python"))
        return (r.stdout + r.stderr).strip()

    def test_publisher_y_subscriber_con_nombre_de_nodo_enorme_no_se_quedan_callados(self):
        for cls in ("Publisher", "Subscriber"):
            with self.subTest(cls=cls):
                out = self.corre(cls, nodo_que_mide(60001))
                self.assertTrue(out.startswith("ERROR:") or out.startswith("DATAGRAMA"),
                                f"{cls} con names y un AGMEN_NODE de 60001 bytes en la línea no registra nada y no lo dice "
                                f"claro (el registro mide más de 65507 bytes): {out}")
                if out.startswith("ERROR:"):
                    self.assertIn("65507", out)
                    self.assertNotIn("el esquema de JointState es muy largo", out)   # el culpable es el nombre del nodo

    def test_con_nombre_de_nodo_normal_los_dos_registran(self):
        for cls in ("Publisher", "Subscriber"):
            with self.subTest(cls=cls):
                self.assertTrue(self.corre(cls, "nodo_normal").startswith("DATAGRAMA"))


class TestRegistroQueNoCabe(unittest.TestCase):
    """Con o sin names, el registro de un Publisher o un Subscriber de topic debe caber en un datagrama (65507): si
    no, error claro al crear, con el mismo texto en los dos núcleos (y en C++: tests/test_nombres.cpp)."""

    NODO = nodo_que_mide(70000)   # 70000 bytes en la línea, 23 336 caracteres (cabe en el entorno de Windows)
    FIX_NODO = ("Lo más largo es el nombre del nodo: usa un nombre de nodo más corto (AGMEN_NODE, o el name de su "
                "[[node]] en robot.toml)")

    def setUp(self):
        self.falso = FalsoHub(self)
        patch = mock.patch.dict(os.environ, {"AGMEN_NODE": self.NODO})
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def peor_sub(nucleo: str, nodo: str, tipo, names=None, *, arrow: bool = False, schema: str = "0" * 16) -> bytes:
        """La línea `sub` más larga que puede mandar un Subscriber de ese núcleo: puertos de 5 dígitos y net=1. Con
        un tipo arrow, `tcp=` con su puerto; sin arrow, `tcp=` vacío en Python puro y sin el campo en C++ (como la
        línea de verdad de cada uno). `tipo`: la clase del mensaje o el texto de `type=`; `schema`: "*" en
        RawSubscriber."""
        tcp = {"tcp": 65535} if arrow else {"tcp": ""} if nucleo == "puro" else {}
        type_name = tipo if isinstance(tipo, str) else tipo.NAME
        return hp.encode("sub", id="0" * 16, node=nodo, pid=os.getpid(), topic="/x", schema=schema, type=type_name,
                         port=65535, **tcp, **({"names": ",".join(names)} if names else {}), net=1)

    @staticmethod
    def esperado(cls_name: str, tipo, names=None, nucleo="puro") -> str:
        """El texto, armado aquí a mano (el mismo que arma C++)."""
        nodo = TestRegistroQueNoCabe.NODO
        if cls_name == "Publisher":
            linea = hp.encode("pub", id="0" * 16, node=nodo, pid=os.getpid(), topic="/x", schema="0" * 16,
                              type=tipo.NAME, text=tipo.SCHEMA_TEXT, **({"names": ",".join(names)} if names else {}))
            partes = f"el nombre del nodo, 70000 bytes; el esquema de {tipo.NAME}, {len(hp.quote(tipo.SCHEMA_TEXT))} bytes"
        else:
            linea = TestRegistroQueNoCabe.peor_sub(nucleo, nodo, tipo, names)
            partes = "el nombre del nodo, 70000 bytes"
        if names:
            partes += f"; names, {len(','.join(names))} bytes"
        return (f"{cls_name} en '/x': el registro en el hub mide {len(linea)} bytes y en un datagrama caben 65507 (lo "
                f"que más ocupa, escrito como va en la línea: {partes}). " + TestRegistroQueNoCabe.FIX_NODO)

    def clases(self):
        out = [("puro", pubsub._PurePublisher, pubsub._PureSubscriber)]
        if _backend.NATIVE:
            out.append(("nativo", pubsub._NativePublisher, pubsub._NativeSubscriber))
        return out

    def test_sin_names_con_nombre_de_nodo_enorme_error_claro_en_los_dos_nucleos(self):
        T2 = JointCommand[2]
        for nucleo, Pub, Sub in self.clases():
            for nombre, cls in (("Publisher", Pub), ("Subscriber", Sub)):
                for names in (None, ["a", "b"]):
                    with self.subTest(nucleo=nucleo, cls=nombre, names=names):
                        with self.assertRaises(ValueError) as ctx:
                            cls(T2, "/x", names=names)
                        self.assertEqual(str(ctx.exception), self.esperado(nombre, T2, names, nucleo))
        # Ningún registro salió (se revisa antes de abrir nada).
        self.falso.sock.settimeout(0.3)
        with self.assertRaises((socket.timeout, TimeoutError)):
            self.falso.sock.recvfrom(65536)

    def test_nombre_enorme_de_set_default_node_name_sin_variable_de_entorno_da_error_claro(self):
        """Sin AGMEN_NODE, el nombre que se le dio al nodo (set_default_node_name, lo llama agmen.Node): no viaja por
        el entorno, así que puede medir 70 000 caracteres en cualquier sistema. El mismo texto en los dos núcleos."""
        T2 = JointCommand[2]
        viejo = pubsub._default_name
        self.addCleanup(pubsub.set_default_node_name, viejo)
        nodo = "n" * 70000
        with mock.patch.dict(os.environ):
            os.environ.pop("AGMEN_NODE", None)
            pubsub.set_default_node_name(nodo)
            for nucleo, Pub, Sub in self.clases():
                for nombre, cls in (("Publisher", Pub), ("Subscriber", Sub)):
                    with self.subTest(nucleo=nucleo, cls=nombre):
                        with self.assertRaises(ValueError) as ctx:
                            cls(T2, "/x")
                        texto = str(ctx.exception)
                        self.assertTrue(texto.startswith(f"{nombre} en '/x': el registro en el hub mide "), texto[:100])
                        self.assertIn("(lo que más ocupa, escrito como va en la línea: el nombre del nodo, 70000 bytes",
                                      texto)
                        self.assertTrue(texto.endswith(self.FIX_NODO), texto[-100:])

    def test_el_culpable_mas_grande_dice_que_hacer(self):
        class Grande(wire.Message):
            SCHEMA = "Grande{" + ";".join(f"f32 campo_{i:05d}" for i in range(2900)) + "}"

        with mock.patch.dict(os.environ, {"AGMEN_NODE": "nodo"}):
            with self.assertRaises(ValueError) as ctx:
                pubsub._PurePublisher(Grande, "/x", names=[f"n{i}" for i in range(1500)])
            self.assertTrue(str(ctx.exception).endswith("Lo más largo es el esquema de Grande: usa un tipo más chico "
                                                        "que Grande"), str(ctx.exception))
            self.assertIn("el nombre del nodo, 4 bytes; el esquema de Grande, ", str(ctx.exception))
        largos = [f"{'j' * 60}{i:04d}" for i in range(125)]   # 125 nombres de 64 caracteres: 8124 bytes
        with mock.patch.dict(os.environ, {"AGMEN_NODE": nodo_que_mide(58000)}):
            with self.assertRaises(ValueError) as ctx:
                pubsub._PureSubscriber(JointCommand[125], "/x", names=largos)
            self.assertIn("names, 8124 bytes", str(ctx.exception))
            self.assertTrue(str(ctx.exception).endswith(self.FIX_NODO))

    @unittest.skipUnless(_backend.NATIVE, "solo con el núcleo de C++")
    def test_el_mismo_texto_que_cpp(self):
        """El núcleo de C++ (sin la revisión de Python delante) da el mismo texto, como HubRejected."""
        T2 = JointCommand[2]
        core = _backend.core
        for names in (None, ["a", "b"]):
            with self.subTest(names=names):
                with self.assertRaises(wire.HubRejected) as ctx:
                    core.Publisher("/x", T2.NAME, T2.SCHEMA_TEXT, T2.SCHEMA_ID, T2.LAYOUT.size or 0, False, "", names)
                self.assertEqual(str(ctx.exception), self.esperado("Publisher", T2, names))
                with self.assertRaises(wire.HubRejected) as ctx:
                    core.Subscriber("/x", T2.NAME, T2.SCHEMA_TEXT, T2.SCHEMA_ID, T2.LAYOUT.size or 0, False, names)
                self.assertEqual(str(ctx.exception), self.esperado("Subscriber", T2, names, "nativo"))

    def borde(self, nucleo: str, crear, quien: str, tipo, names=None, *, arrow: bool = False,
              schema: str = "0" * 16, por_entorno: bool = True):
        """Un Subscriber (o RawSubscriber, o Node.on) cuya línea más larga mide justo 65507: se crea y manda su
        registro, y la línea que llega más lo que le falta (puertos de 5 dígitos y net=1) mide exactamente 65507;
        con un byte más, el error de Python (ValueError, antes de abrir nada) con el tamaño exacto. `crear(nodo)`
        crea el endpoint (con `por_entorno`, el nombre va en AGMEN_NODE) y devuelve lo que hay que cerrar."""
        base = len(self.peor_sub(nucleo, "", tipo, names, arrow=arrow, schema=schema))
        k = 65507 - base                                   # los bytes que debe medir el nombre en la línea
        nodo = "%" * (k // 3) + "n" * (k % 3)
        self.assertEqual(len(self.peor_sub(nucleo, nodo, tipo, names, arrow=arrow, schema=schema)), 65507)
        self.assertLessEqual(len(nodo), MAX_ENV)

        def entorno(n):
            return mock.patch.dict(os.environ, {"AGMEN_NODE": n} if por_entorno else {})

        with self.subTest(nucleo=nucleo, quien=quien, arrow=arrow, names=names, borde="cabe"):
            err = io.StringIO()
            with entorno(nodo), contextlib.redirect_stderr(err):
                self.falso.vaciar()           # (el de la vuelta anterior puede llamarse igual: su línea no cuenta)
                o = crear(nodo)
                self.addCleanup(o.close)
                linea = self.falso.registro("sub", node=nodo)
            (op, f), = hp.parse(linea)
            self.assertEqual((op, f["node"], f["topic"]), ("sub", nodo, "/x"))
            self.assertEqual(len(f["schema"]), len(schema))           # 16 dígitos hex, o "*"
            if schema == "*":
                self.assertEqual(f["schema"], "*")
            self.assertNotIn("net", f)
            self.assertEqual("tcp" in f, arrow or nucleo == "puro")   # la línea de verdad de cada núcleo
            falta = 5 - len(f["port"]) + len(" net=1") + (5 - len(f["tcp"]) if f.get("tcp") else 0)
            self.assertEqual(len(linea) + falta, 65507)
            o.close()
        with self.subTest(nucleo=nucleo, quien=quien, arrow=arrow, names=names, borde="un byte más"):
            with entorno(nodo + "n"), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(ValueError) as ctx:
                    crear(nodo + "n")
            self.assertTrue(str(ctx.exception).startswith(f"{quien} en '/x': el registro en el hub mide 65508 bytes "
                                                          f"y en un datagrama caben 65507 "),
                            str(ctx.exception)[:200])

    def test_subscriber_justo_en_el_borde(self):
        """La medida es la de la línea más larga que ese Subscriber puede mandar de verdad (no la de otro núcleo ni
        con un tcp que no manda): con 65507 bytes se crea y manda su registro; con uno más, el error con el tamaño
        exacto. Con un tipo que no es arrow y con uno arrow (que manda tcp en los dos núcleos), con y sin names."""
        for tipo, arrow in ((JointCommand[2], False), (Imagen, True)):
            for nucleo, _, Sub in self.clases():
                for names in (None, ["a", "b"]):
                    self.borde(nucleo, lambda _n, Sub=Sub, tipo=tipo, names=names: Sub(tipo, "/x", names=names),
                               "Subscriber", tipo, names, arrow=arrow)

    def test_raw_subscriber_justo_en_el_borde(self):
        """RawSubscriber (y la grabadora) manda schema=* (1 carácter, no 16) y type= vacío: se mide con eso."""
        self.borde("puro", lambda n: pubsub.RawSubscriber("/x", node=n), "RawSubscriber", "", arrow=True,
                   schema="*", por_entorno=False)

    def test_node_on_justo_en_el_borde(self):
        """agmen.Node(...).on(...) mide como el Subscriber de su núcleo (con el núcleo de C++, Python revisa antes
        con la línea de C++; en Python puro, Node.on crea un Subscriber). Con un tipo arrow y con uno que no."""
        nucleo = "nativo" if _backend.NATIVE else "puro"
        self.addCleanup(pubsub.set_default_node_name, pubsub._default_name)
        for tipo, arrow in ((JointCommand[2], False), (Imagen, True)):
            for names in (None, ["a", "b"]):
                def crear(_n, tipo=tipo, names=names):
                    nodo = agmen.Node("borde")
                    try:
                        nodo.on(tipo, "/x", lambda s: None, names=names)
                    except BaseException:
                        nodo.close()
                        raise
                    return nodo
                self.borde(nucleo, crear, "Subscriber", tipo, names, arrow=arrow)

    def test_quien_cabe_registra_la_linea_de_siempre(self):
        err = io.StringIO()
        nodo = nodo_que_mide(60001)
        with mock.patch.dict(os.environ, {"AGMEN_NODE": nodo}), contextlib.redirect_stderr(err):
            p = pubsub._PurePublisher(JointCommand[2], "/x")
            self.addCleanup(p.close)
            self.assertEqual(self.falso.registro("pub"),
                             hp.encode("pub", id=f"{p.source_id:016x}", node=nodo, pid=os.getpid(), topic="/x",
                                       schema=f"{JointCommand[2].SCHEMA_ID:016x}", type="JointCommand",
                                       text=JointCommand[2].SCHEMA_TEXT))


@unittest.skipUnless(_backend.NATIVE, "solo con el núcleo de C++")
class TestHubRejectedDelNucleo(_HubPlano):
    def test_el_rechazo_del_hub_con_el_nucleo_es_exactamente_wire_hub_rejected(self):
        """core.set_errors recibe HubRejected como argumento (no lo busca en sys.modules mientras agmen.wire se
        importa): el rechazo del hub con el núcleo compilado es exactamente agmen.wire.HubRejected."""
        bien = self.abrir(agmen.Publisher, JointCommand[2], "/r/cmd")
        bien.publish(JointCommand[2]())
        otro = self.abrir(pubsub._NativePublisher, JointState[2], "/r/cmd")
        e = self.rechazado(lambda: otro.publish(JointState[2]()))
        self.assertIs(type(e), wire.HubRejected)
        self.assertIs(type(e), agmen.wire.HubRejected)

    def test_la_clase_del_rechazo_es_la_que_llega_por_argumento(self):
        """Lo de arriba también pasaría si el núcleo buscara HubRejected por su nombre: aquí se le pasa a set_errors
        otra clase (una subclase nueva de WireError) y el rechazo del núcleo debe salir como ESA clase; luego, con
        la de siempre, como wire.HubRejected. (Un rechazo que lanza el núcleo al crear, sin hub: una lista repetida.)"""
        core = _backend.core
        T2 = JointCommand[2]

        class OtraHubRejected(wire.WireError):
            pass

        def rechazo():
            with self.assertRaises(wire.WireError) as ctx:
                core.Publisher("/x", T2.NAME, T2.SCHEMA_TEXT, T2.SCHEMA_ID, T2.LAYOUT.size or 0, False, "", ["a", "a"])
            return ctx.exception

        try:
            core.set_errors(wire._schema.SchemaError, wire.WireError, OtraHubRejected)
            e = rechazo()
            self.assertIs(type(e), OtraHubRejected)
            self.assertIn("'a' está dos veces en names", str(e))
        finally:
            core.set_errors(wire._schema.SchemaError, wire.WireError, wire.HubRejected)
        self.assertIs(type(rechazo()), wire.HubRejected)


# --- RawPublisher / RawSubscriber con names= (los del [[mux]]): los casos feos -------------------------------------

def _cmd12_finito():
    """Un JointCommand[12] sin NaN (el mux descarta lo no finito, y un JointCommand() nuevo trae position, kp y kd en
    NaN)."""
    m = CMD12()
    m.position, m.kp, m.kd = [1.0] * 12, [1.0] * 12, [0.1] * 12
    return m


def _fd_libres_detecta_fugas() -> int:
    """El número que da el sistema al siguiente descriptor: si no cambia tras crear y fallar muchas veces, no hubo
    fuga (en Windows los sockets no son descriptores de C: la prueba solo no se queja)."""
    fd = os.dup(0)
    os.close(fd)
    return fd


class TestRawConNamesBordes(unittest.TestCase):
    def setUp(self):
        self.falso = FalsoHub(self)
        self.err = io.StringIO()
        ctx = contextlib.redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def test_names_invalidos_dan_el_mismo_error_que_un_publisher(self):
        """Los mismos casos que TestNamesAlCrear: mismo tipo de excepción y mismo texto, con el nombre de la clase."""
        casos = [c for c in TestNamesAlCrear.CASOS if c[1] is not None]
        for names, error, frag in casos:
            for cls in (pubsub.RawPublisher, pubsub.RawSubscriber):
                with self.subTest(cls=cls.__name__, names=repr(names)[:40]):
                    with self.assertRaises(error) as ctx:
                        cls("/patas/estado", names=names)
                    texto = str(ctx.exception)
                    self.assertIn(frag, texto)
                    self.assertIn(f"{cls.__name__} en '/patas/estado'", texto)
                    self.assertNotIn("\x1b", texto)
        for names in ({"a", "b"}, frozenset({"a"}), {"a": 1}):
            for cls in (pubsub.RawPublisher, pubsub.RawSubscriber):
                with self.subTest(cls=cls.__name__, names=type(names).__name__):
                    with self.assertRaisesRegex(TypeError, "no tiene orden fijo"):
                        cls("/patas/estado", names=names)

    def test_names_invalidos_no_dejan_sockets_ni_registro(self):
        antes = _fd_libres_detecta_fugas()
        for _ in range(30):
            for cls in (pubsub.RawPublisher, pubsub.RawSubscriber):
                with self.assertRaises(ValueError):
                    cls("/patas/estado", names=["a", "a"])
        self.assertEqual(_fd_libres_detecta_fugas(), antes)
        self.falso.sock.settimeout(0.3)
        with self.assertRaises((socket.timeout, TimeoutError)):
            self.falso.sock.recvfrom(65536)               # ningún registro salió

    def test_names_con_una_direccion_o_con_estrella(self):
        with self.assertRaisesRegex(ValueError, "RawSubscriber: names= va con un topic"):
            pubsub.RawSubscriber("127.0.0.1:0", names=["a"])
        # '*' es "todos los topics": la lista es de un topic. Lo dice quien lo revise (aquí o el hub), no se cae.
        try:
            rs = pubsub.RawSubscriber("*", names=["a"])
        except (ValueError, TypeError) as e:
            self.assertIn("names", str(e))
        else:
            rs.close()
        with self.assertRaises((ValueError, hp.ProtocolError)):
            pubsub.RawPublisher("patas/cmd", names=["a"])          # un topic sin '/' inicial

    def test_names_vacio_o_none_es_distinto(self):
        rp = pubsub.RawPublisher("/patas/cmd", names=None)
        self.addCleanup(rp.close)
        self.assertIsNone(rp.names)
        with self.assertRaisesRegex(ValueError, "names está vacía"):
            pubsub.RawPublisher("/patas/cmd", names=[])
        with self.assertRaisesRegex(ValueError, "names está vacía"):
            pubsub.RawSubscriber("/patas/cmd", names=())

    def test_tupla_y_lista_valen_como_en_un_publisher(self):
        rp = pubsub.RawPublisher("/patas/cmd", names=tuple(PATAS))
        self.addCleanup(rp.close)
        self.assertEqual(rp.names, PATAS)
        rs = pubsub.RawSubscriber("/patas/cmd", names=list(PATAS))
        self.addCleanup(rs.close)
        self.assertEqual(rs.names, PATAS)

    def test_registro_que_no_cabe_se_dice_una_vez_y_se_reintenta_si_luego_cabe(self):
        rp = pubsub.RawPublisher("/patas/cmd", names=list(PATAS))
        self.addCleanup(rp.close)
        enorme = "Grande{" + ";".join(f"f32 campo_{i:05d}" for i in range(5000)) + "}"
        for _ in range(5):
            rp.set_schema(enorme, "Grande", 0x1234)
        self.assertIsNone(rp._link)
        self.assertEqual(self.err.getvalue().count("el registro en el hub mide"), 1, self.err.getvalue())
        self.assertFalse(rp.send(bytearray(wire.encode(CMD12(), 1, 1))))          # nadie a quién mandar: sin error
        self.falso.vaciar()
        rp.set_schema(T12, "JointCommand", CMD12.SCHEMA_ID)                         # ahora sí cabe: se registra
        self.assertIsNotNone(rp._link)
        linea = self.falso.registro("pub").decode()
        self.assertIn(f"names={','.join(PATAS)}", linea)
        self.assertEqual(self.err.getvalue().count("el registro en el hub mide"), 1)
        rp.set_schema(enorme, "Grande", 0x1234)                                      # ya registrado: no cambia nada
        self.assertEqual(self.err.getvalue().count("el registro en el hub mide"), 1)

    def test_registro_que_no_cabe_por_el_nombre_del_nodo_no_tumba_ni_calla(self):
        with mock.patch.dict(os.environ, {"AGMEN_NODE": "n" * 70000}):
            rp = pubsub.RawPublisher("/patas/cmd", names=list(PATAS))
            self.addCleanup(rp.close)
            rp.set_schema(T12, "JointCommand", CMD12.SCHEMA_ID)
            rp.set_schema(T12, "JointCommand", CMD12.SCHEMA_ID)
        texto = self.err.getvalue()
        self.assertEqual(texto.count("el registro en el hub mide"), 1, texto)
        self.assertIn("nombre del nodo", texto)
        self.assertIn("Mientras tanto no me registro en el hub", texto)

    def test_una_instancia_nueva_vuelve_a_decirlo_porque_es_otra(self):
        enorme = "Grande{" + ";".join(f"f32 campo_{i:05d}" for i in range(5000)) + "}"
        for _ in range(2):
            rp = pubsub.RawPublisher("/patas/cmd", names=list(PATAS))
            rp.set_schema(enorme, "Grande", 0x1234)
            rp.close()
        self.assertEqual(self.err.getvalue().count("el registro en el hub mide"), 2)

    def test_cerrar_y_reabrir_con_la_misma_lista(self):
        for vuelta in range(3):
            self.falso.vaciar()
            rp = pubsub.RawPublisher("/patas/cmd", names=list(PATAS))
            rp.set_schema(T12, "JointCommand", CMD12.SCHEMA_ID)
            self.assertIn(f"names={','.join(PATAS)}", self.falso.registro("pub").decode(), vuelta)
            rp.close()
            rp.close()                                              # cerrar dos veces no se queja
            with self.assertRaises(RuntimeError):
                rp.set_schema(T12, "JointCommand", CMD12.SCHEMA_ID)  # un RawPublisher cerrado no se registra
            self.falso.vaciar()
            rs = pubsub.RawSubscriber("/patas/cmd", names=list(PATAS))
            self.assertIn(f"names={','.join(PATAS)}", self.falso.registro("sub").decode(), vuelta)
            rs.close()
            rs.close()

    def test_la_linea_de_un_topic_con_signos_raros_y_lista_larga(self):
        largos = [("n%03d" % i).ljust(64, "x") for i in range(120)]       # 7800 caracteres de lista
        rp = pubsub.RawPublisher("/patas/cmd", names=largos)
        self.addCleanup(rp.close)
        rp.set_schema(T12, "JointCommand", CMD12.SCHEMA_ID)
        self.assertTrue(self.falso.registro("pub").endswith(b" names=" + ",".join(largos).encode()))


# --- el [[mux]] lanzado a mano contra un hub de verdad ---------------------------------------------------------------

def _mux_a_mano(case, hub_addr, *args, names=None):
    """Lanza `python -m agmen mux …` (de verdad) contra ese hub; devuelve (proceso, lista de líneas de salida)."""
    env = dict(os.environ, PYTHONPATH=str(ROOT / "python"), PYTHONIOENCODING="utf-8", AGMEN_HUB=hub_addr)
    cmd = [sys.executable, "-m", "agmen", "mux", "--in", "ia=/cmd/ia@1", "--out", "/patas/cmd", *args]
    if names is not None:
        cmd += ["--names", names]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True, encoding="utf-8")
    out: list[str] = []
    threading.Thread(target=lambda: out.extend(iter(p.stdout.readline, "")), daemon=True).start()

    def cerrar():
        p.kill()
        p.wait(10)
        p.stdout.close()
    case.addCleanup(cerrar)
    return p, out


class TestMuxALaMano(unittest.TestCase):
    def mux_corto(self, *args, hub="127.0.0.1:1"):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "python"), PYTHONIOENCODING="utf-8", AGMEN_HUB=hub)
        return subprocess.run([sys.executable, "-m", "agmen", "mux", "--in", "ia=/cmd/ia@1", *args],
                              capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)

    def test_lista_mala_sale_con_2_y_dice_que_hacer(self):
        casos = [("a,,b", "[mux] error: --names a,,b: names[1] = '' no es un nombre de articulación válido"),
                 ("a,a", "'a' está dos veces"),
                 ("a b", "tiene un espacio"),
                 ("x" * 65, "mide 65 caracteres"),
                 ("", "--names")]
        for lista, frag in casos:
            with self.subTest(lista=repr(lista)[:30]):
                r = self.mux_corto("--out", "/patas/cmd", "--names", lista)
                self.assertEqual(r.returncode, 2, (r.stdout, r.stderr))
                self.assertIn(frag, r.stderr)
                self.assertNotIn("Traceback", r.stdout + r.stderr)
                self.assertNotIn("\x1b", r.stderr)
                self.assertLess(len(r.stderr), 600)               # una lista enorme no se copia entera al error

    def test_el_error_no_repite_los_caracteres_de_control_del_argumento(self):
        """FALLA REAL (menor). `agmen mux --names $'a\\x1b[31m'`: el error repite el argumento tal cual, con el ESC
        crudo (los demás textos de nombres sí los cambian por '?'). Esperado: sin caracteres de control en stderr.
        Reproducir: python -m unittest test_nombres.TestMuxALaMano.test_el_error_no_repite_los_"""
        "caracteres_de_control_del_argumento"
        r = self.mux_corto("--out", "/patas/cmd", "--names", "a\x1b[31m")
        self.assertEqual(r.returncode, 2, (r.stdout, r.stderr))
        self.assertIn("carácter de control", r.stderr)
        self.assertEqual([c for c in r.stderr if not c.isprintable() and c != "\n"], [], repr(r.stderr[:80]))

    def test_lista_enorme_no_se_copia_entera_al_error(self):
        lista = ",".join(f"j{i:05d}" for i in range(2000))             # 14 000 caracteres: pasa el tope de 8192
        r = self.mux_corto("--out", "/patas/cmd", "--names", lista)
        self.assertEqual(r.returncode, 2, (r.stdout, r.stderr[:300]))
        self.assertIn("el máximo es 8192", r.stderr)
        self.assertLess(len(r.stderr), 600, len(r.stderr))

    def test_names_con_salida_que_es_una_direccion(self):
        r = self.mux_corto("--out", "127.0.0.1:7500", "--names", "a,b")
        self.assertEqual(r.returncode, 2, (r.stdout, r.stderr))
        self.assertIn("--names va con una salida que es un topic", r.stderr)
        self.assertIn("'127.0.0.1:7500' es una dirección", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_names_sin_valor_lo_dice_argparse(self):
        r = self.mux_corto("--out", "/patas/cmd", "--names")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--names", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_lista_buena_se_declara_y_la_politica_buena_llega(self):
        hub = HubProceso(self, "--names", f"/patas/cmd={','.join(PATAS)}", "--names", f"/cmd/ia={','.join(PATAS)}",
                         "--require-names", "/patas/cmd", "--require-names", "/cmd/ia")
        p, out = _mux_a_mano(self, hub.addr, names=",".join(PATAS))
        self.assertTrue(until(lambda: "declaro al hub la lista de robot.toml (12 articulaciones: FR_hip, FR_thigh … "
                                      "RL_calf)" in "".join(out)), "".join(out))
        sub = pubsub._PureSubscriber(CMD12, "/patas/cmd")
        self.addCleanup(sub.close)
        pol = pubsub._PurePublisher(CMD12, "/cmd/ia", names=list(PATAS), node="politica")
        self.addCleanup(pol.close)

        self.assertTrue(until(lambda: sub.receive(0.05) is not None, every=lambda: pol.publish(_cmd12_finito())),
                        hub.text() + "".join(out))
        self.assertIsNone(p.poll())

    def test_numero_de_nombres_que_no_coincide_con_el_tipo_lo_dice_el_hub_y_el_mux_sigue(self):
        """OBSERVACIÓN: 3 nombres y un tipo de 12 articulaciones. Quien lo dice es el hub (un aviso, no un rechazo);
        el mux no sabe contar (no conoce el tipo al crearse) y sigue reenviando."""
        hub = HubProceso(self, "--names", "/patas/cmd=a,b,c")
        p, out = _mux_a_mano(self, hub.addr, names="a,b,c")
        pol = pubsub._PurePublisher(CMD12, "/cmd/ia", node="politica")
        self.addCleanup(pol.close)
        frag = "'/patas/cmd' trae 12 articulaciones y robot.toml nombra 3"
        self.assertTrue(until(lambda: frag in hub.text(), every=lambda: pol.publish(_cmd12_finito())), hub.text() + "".join(out))
        self.assertIsNone(p.poll())
        self.assertNotIn("Traceback", "".join(out))

    def test_lista_con_orden_cambiado_en_una_entrada_el_mux_lo_dice(self):
        """El rechazo de la ENTRADA (RawSubscriber) sí llega al usuario: el mux cuenta el error y lo escribe."""
        hub = HubProceso(self, "--names", f"/patas/cmd={','.join(PATAS)}", "--names", f"/cmd/ia={','.join(PATAS)}")
        p, out = _mux_a_mano(self, hub.addr, names=",".join(reversed(PATAS)))
        self.assertTrue(until(lambda: "el hub rechazó escuchar '/cmd/ia'" in "".join(out)), "".join(out) + hub.text())
        self.assertIn("pasa names= en el orden de [[joints]] de robot.toml", "".join(out))
        self.assertIsNone(p.poll())

    def test_salida_del_mux_rechazada_por_el_hub_no_se_queda_callada(self):
        """FALLA REAL (nunca fallar en silencio, VISION §3.1). Un mux a mano con la lista en otro orden que la que el
        hub tiene para su SALIDA (/patas/cmd): el hub rechaza el registro del RawPublisher (la lista de la entrada
        no cuenta aquí), nada llega a /patas/cmd, y el mux solo dice "declaro al hub la lista…" y luego
        "reenviados=N"; RawPublisher guarda el error en _link.error y nadie lo lee. El hub tampoco lo muestra.
        Esperado: el mux (o el hub) dice que el hub rechazó la salida y qué hacer.
        Reproducir: python -m unittest test_nombres.TestMuxALaMano.test_salida_del_mux_rechazada_por_"""
        "el_hub_no_se_queda_callada"
        hub = HubProceso(self, "--names", f"/patas/cmd={','.join(PATAS)}")
        p, out = _mux_a_mano(self, hub.addr, names=",".join(reversed(PATAS)))
        sub = pubsub._PureSubscriber(CMD12, "/patas/cmd")
        self.addCleanup(sub.close)
        pol = pubsub._PurePublisher(CMD12, "/cmd/ia", node="politica")
        self.addCleanup(pol.close)
        llego = until(lambda: sub.receive(0.05) is not None, 4, every=lambda: pol.publish(_cmd12_finito()))
        self.assertFalse(llego, "con el orden cambiado no debería llegar nada a /patas/cmd")
        todo = "".join(out) + hub.text()
        self.assertRegex(todo, r"(?i)rechaz|declara .* y robot.toml dice",
                         f"nadie dijo que el hub rechazó la salida del mux:\n{todo}")

    def test_mux_viejo_sin_names_contra_require_names_no_se_queda_callado(self):
        """FALLA REAL (nunca fallar en silencio). Un mux sin --names (o de una versión anterior) contra un hub con
        --require-names en su salida: el hub rechaza su RawPublisher, el mux sigue contando "reenviados" y no dice
        nada; el hub tampoco. Esperado: algún texto que diga que el hub exige la lista (require_names) y qué hacer.
        Reproducir: python -m unittest test_nombres.TestMuxALaMano.test_mux_viejo_sin_names_contra_"""
        "require_names_no_se_queda_callado"
        hub = HubProceso(self, "--names", f"/patas/cmd={','.join(PATAS)}", "--require-names", "/patas/cmd")
        p, out = _mux_a_mano(self, hub.addr)
        sub = pubsub._PureSubscriber(CMD12, "/patas/cmd")
        self.addCleanup(sub.close)
        pol = pubsub._PurePublisher(CMD12, "/cmd/ia", node="politica")
        self.addCleanup(pol.close)
        llego = until(lambda: sub.receive(0.05) is not None, 4, every=lambda: pol.publish(_cmd12_finito()))
        self.assertFalse(llego, "un mux sin lista no debería poder publicar en un topic con require_names")
        todo = "".join(out) + hub.text()
        self.assertRegex(todo, r"(?i)rechaz|require_names|exige",
                         f"nadie dijo que el hub rechazó al mux viejo:\n{todo}")


# --- el panel: un [[web.command]] hacia una entrada que hereda la lista -------------------------------------------------

PANEL_ROBOT = """
[robot]
name = "n"

[[node]]
name = "motor"
cmd = "./build/bin/motor"

[[joints]]
name = "patas"
command = "/patas/cmd"
names = ["a", "b", "c"]
{require}
[[mux]]
name = "ruedas"
output = "/patas/cmd"

[[mux.input]]
name = "ia"
topic = "/cmd/ia"
priority = 1

[[web.command]]
name = "Pata"
target = "{target}"
type = "JointCommand"
messages = "m.agmen"

[web.command.default]
position = [1.0, 1.0, 1.0]
velocity = [0.0, 0.0, 0.0]
effort = [0.0, 0.0, 0.0]
kp = [1.0, 1.0, 1.0]
kd = [0.1, 0.1, 0.1]

[[web.command.button]]
label = "quieto"
"""

M_AGMEN = ("package mio\nmessage JointCommand {{\n    f32[{n}] position\n    f32[{n}] velocity\n    f32[{n}] effort\n"
           "    f32[{n}] kp\n    f32[{n}] kd\n}}\n")


class TestPanelHaciaEntradaHeredada(unittest.TestCase):
    """El panel de agmen run declara la lista de [[joints]] cuando su target la lleva (el command de un [[joints]]
    o una entrada de [[mux]] que la hereda), como un nodo (RFC 0013 D6 = d). Con require_names publica y llega;
    sin require_names, el hub no tiene nada que avisar. El hub, con los mismos --names que le pone agmen run."""

    def _robot(self, require: bool, target: str = "/cmd/ia", n: int = 3):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        (Path(d) / "m.agmen").write_text(M_AGMEN.format(n=n), encoding="utf-8")
        texto = PANEL_ROBOT.format(require="require_names = true\n" if require else "", target=target)
        for v in ("1.0", "0.0", "0.1"):        # los valores por defecto, con n articulaciones
            texto = texto.replace(f"[{v}, {v}, {v}]", "[" + ", ".join([v] * n) + "]")
        (Path(d) / "robot.toml").write_text(texto, encoding="utf-8")
        return load_config(Path(d) / "robot.toml")

    def _hub(self, cfg):
        """El agmen hub con los --names y --require-names que le pone agmen run (prepare_hub)."""
        cfg, _ = prepare_hub(cfg, ping=lambda a: None)
        cmd = next(n for n in cfg.nodes if n.name == "hub").cmd
        args = []
        for k, a in enumerate(cmd):
            if a in ("--names", "--require-names"):
                args += [a, cmd[k + 1]]
        return HubProceso(self, *args)

    def _panel(self, cfg):
        from agmen.cli import _commands_for
        from agmen.panel.server import Panel

        commands = _commands_for(cfg)
        panel = Panel("127.0.0.1:0", None, "prueba", None, None, topics=False, commands=commands)
        panel.start()
        self.addCleanup(panel.shutdown)
        return panel

    def _mandar(self, panel, hasta):
        """Aprieta el botón hasta que `hasta()` o el tope; los errores que devuelve el panel, sin repetir."""
        vistos: list[str] = []

        def paso():
            err = panel.command(0, button=0)
            if err and err not in vistos:
                vistos.append(err)
            time.sleep(0.06)           # (el panel descarta más de ~60 por segundo)
        until(lambda: hasta(vistos), 8, every=paso)
        return vistos

    def _escuchar(self, topic="/cmd/ia", n=3):
        sub = pubsub._PureSubscriber(JointCommand[n], topic)
        self.addCleanup(sub.close)
        return sub

    def test_con_require_names_el_panel_publica_y_llega(self):
        cfg = self._robot(require=True)
        hub = self._hub(cfg)
        panel = self._panel(cfg)
        self.assertEqual(panel.commands[0].pub.names, ("a", "b", "c"))
        sub = self._escuchar()
        llegaron: list = []

        def llego(vistos):
            got = sub.receive(0.02)
            if got is not None:
                llegaron.append(got)
            return bool(vistos) or bool(llegaron)
        vistos = self._mandar(panel, llego)
        self.assertEqual(vistos, [], hub.text())
        self.assertTrue(llegaron, hub.text())
        self.assertNotIn("rechaz", hub.text())

    def test_sin_require_names_publica_y_el_hub_no_avisa(self):
        cfg = self._robot(require=False)
        hub = self._hub(cfg)
        panel = self._panel(cfg)
        sub = self._escuchar()
        llegaron: list = []

        def llego(vistos):
            got = sub.receive(0.02)
            if got is not None:
                llegaron.append(got)
            return bool(vistos) or len(llegaron) >= 3
        errores = self._mandar(panel, llego)
        self.assertEqual(errores, [], errores)                        # el panel no ve ningún error
        self.assertGreaterEqual(len(llegaron), 3, hub.text())
        time.sleep(0.3)
        self.assertNotIn("sin declarar su lista de nombres", hub.text())

    def test_al_command_del_joints_tambien_la_declara(self):
        cfg = self._robot(require=True, target="/patas/cmd")
        hub = self._hub(cfg)
        panel = self._panel(cfg)
        self.assertEqual(panel.commands[0].pub.names, ("a", "b", "c"))
        sub = self._escuchar("/patas/cmd")
        llegaron: list = []

        def llego(vistos):
            got = sub.receive(0.02)
            if got is not None:
                llegaron.append(got)
            return bool(vistos) or bool(llegaron)
        self.assertEqual(self._mandar(panel, llego), [], hub.text())
        self.assertTrue(llegaron, hub.text())

    def test_otro_numero_de_articulaciones_publica_sin_lista_y_el_hub_lo_avisa(self):
        """Un mando de 2 articulaciones hacia un topic de 3: no se declara la lista (el Publisher no se podría
        crear con ella), se publica como antes y el hub dice que el número no coincide."""
        cfg = self._robot(require=False, n=2)
        hub = self._hub(cfg)
        panel = self._panel(cfg)
        self.assertIsNone(panel.commands[0].pub.names)
        self._mandar(panel, lambda vistos: bool(vistos) or "trae 2 articulaciones y robot.toml nombra 3" in hub.text())
        self.assertIn("'/cmd/ia' trae 2 articulaciones y robot.toml nombra 3", hub.text())

    def test_sin_joints_el_mando_no_declara_nada(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        (Path(d) / "m.agmen").write_text(M_AGMEN.format(n=3), encoding="utf-8")
        texto = PANEL_ROBOT.format(require="", target="/cmd/ia")
        texto = texto[:texto.index("[[joints]]")] + texto[texto.index("[[mux]]"):]
        (Path(d) / "robot.toml").write_text(texto, encoding="utf-8")
        cfg = load_config(Path(d) / "robot.toml")
        from agmen.cli import _commands_for

        commands = _commands_for(cfg)
        self.addCleanup(lambda: [c.close() for c in commands])
        self.assertIsNone(commands[0].pub.names)


# --- agmen run de punta a punta con [[mux]]: prioridades y NaN ----------------------------------------------------------

LECTOR_TICK = """
import time, agmen
from agmen.msgs import JointCommand
sub = agmen.Subscriber(JointCommand[3], "/patas/cmd")
end = time.monotonic() + 40
ultimo, t = None, 0.0
while time.monotonic() < end:
    s = sub.receive(0.1)
    if s is not None:
        ultimo = float(s.message.position[0])
    if ultimo is not None and time.monotonic() - t > 0.3:
        t = time.monotonic()
        print("TICK", ultimo, flush=True)
"""

POLITICA_NAN = """
import sys, time, agmen
from agmen.msgs import JointCommand
campo, topic = sys.argv[1], sys.argv[2]
T = JointCommand[3]
pub = agmen.Publisher(T, topic, names=["cadera", "rodilla", "tobillo"])
end = time.monotonic() + 40
while time.monotonic() < end:
    m = T()
    m.position, m.kp, m.kd = [5.0] * 3, [1.0] * 3, [0.1] * 3
    setattr(m, campo, [float("nan")] * 3)
    try:
        pub.publish(m)
    except Exception as e:
        print("MALA", type(e).__name__, e, flush=True)
        time.sleep(0.5)
    time.sleep(0.05)
"""


class TestAgmenRunMuxExtremo(unittest.TestCase):
    """Con el [[mux]] heredando la lista, de punta a punta. Los nodos usan el núcleo del proceso de prueba (el de C++
    cuando se corre con él), no el de Python puro fijo."""

    correr = TestAgmenRunLlegaElBueno.correr
    JOINTS = ('[robot]\nname = "n"\n\n[[joints]]\nname = "patas"\ncommand = "/patas/cmd"\n'
              'names = ["cadera", "rodilla", "tobillo"]\nkp = 1.0\nkd = 0.1\n')

    @staticmethod
    def nodo(name, cmd):
        return f'\n[[node]]\nname = "{name}"\ncmd = {json.dumps(cmd)}\n'

    @staticmethod
    def mux(*entradas):
        t = '\n[[mux]]\nname = "ruedas"\noutput = "/patas/cmd"\n'
        for nombre, topic, prio in entradas:
            t += f'\n[[mux.input]]\nname = "{nombre}"\ntopic = "{topic}"\npriority = {prio}\n'
        return t

    @staticmethod
    def ticks(out):
        return [float(ln.split()[1]) for ln in out.splitlines() if ln.startswith("TICK ") or " TICK " in ln
                for ln in [ln[ln.index("TICK"):]]]

    def test_dos_politicas_misma_lista_manda_la_de_mayor_prioridad_y_un_solo_aviso_del_sin_lista(self):
        for require in (False, True):
            with self.subTest(require_names=require):
                robot = self.JOINTS + ("require_names = true\n" if require else "")
                robot += self.mux(("baja", "/cmd/baja", 1), ("alta", "/cmd/alta", 10))
                robot += self.nodo("lector", ["python", "-c", LECTOR_TICK])
                robot += self.nodo("pbaja", ["python", "politica.py", "cadera,rodilla,tobillo", "1.0", "/cmd/baja"])
                robot += self.nodo("palta", ["python", "politica.py", "cadera,rodilla,tobillo", "2.0", "/cmd/alta"])
                robot += self.nodo("sin", ["python", "politica.py", "-", "1.0", "/cmd/baja"])
                aviso = "'sin' publica en '/cmd/baja', un topic de [[joints]], sin declarar su lista de nombres"
                rechazo = "'/cmd/baja': 'sin' publica sin declarar su lista de nombres y robot.toml la exige"
                ok, out = self.correr(robot, lambda t: t.count("TICK 2.0") >= 6 and (rechazo if require else aviso) in t)
                self.assertTrue(ok, out)
                ticks = self.ticks(out)
                primero = ticks.index(2.0)
                self.assertEqual(set(ticks[primero:]), {2.0}, ticks)          # manda la de mayor prioridad, y se queda
                if require:      # el nodo sin lista vuelve a intentar y vuelve a leer el rechazo (su propio print)
                    self.assertGreaterEqual(out.count(rechazo), 1, out)
                else:
                    self.assertEqual(out.count(aviso), 1, out)               # UN aviso del hub, no uno por mensaje
                self.assertNotIn("'ruedas' publica en", out)                  # el mux declara su lista: ningún aviso suyo
                mux_lineas = [ln for ln in out.splitlines() if ln.startswith("ruedas")]
                self.assertFalse([ln for ln in mux_lineas if "rechaz" in ln], mux_lineas)   # el mux no es rechazado

    def test_OBSERVACION_el_mux_descarta_position_kp_o_kd_en_nan(self):
        """OBSERVACIÓN (decisión pendiente): hoy el mux descarta cualquier comando con un valor no finito, también
        position, kp y kd, y dice una vez por entrada cuál campo. La entrada buena (de menor prioridad) manda."""
        robot = self.JOINTS + "require_names = true\n"
        robot += self.mux(("bueno", "/cmd/ok", 1), ("nanpos", "/cmd/np", 2), ("nankp", "/cmd/nkp", 3),
                          ("nankd", "/cmd/nkd", 4))
        robot += self.nodo("lector", ["python", "-c", LECTOR_TICK])
        robot += self.nodo("bueno", ["python", "politica.py", "cadera,rodilla,tobillo", "1.0", "/cmd/ok"])
        for nodo, campo, topic in (("np", "position", "/cmd/np"), ("nkp", "kp", "/cmd/nkp"), ("nkd", "kd", "/cmd/nkd")):
            robot += self.nodo(nodo, ["python", "-c", POLITICA_NAN, campo, topic])
        frags = ["descarté un comando de 'nanpos' con un valor no finito (position[0]=nan)",
                 "descarté un comando de 'nankp' con un valor no finito (kp[0]=nan)",
                 "descarté un comando de 'nankd' con un valor no finito (kd[0]=nan)"]
        ok, out = self.correr(robot, lambda t: t.count("TICK 1.0") >= 4 and all(f in t for f in frags))
        self.assertTrue(ok, out)
        self.assertEqual(set(self.ticks(out)), {1.0}, out)                   # nunca llegó un comando con NaN
        for f in frags:
            self.assertEqual(out.count(f), 1, out)                           # una vez por entrada


class TestAgmenRunMuxEnOtroPerfil(unittest.TestCase):
    correr = TestAgmenRunLlegaElBueno.correr
    nodo = staticmethod(TestAgmenRunMuxExtremo.nodo)

    def test_mux_de_otro_perfil_no_se_declara_ni_exige(self):
        """El [[mux]] de otro perfil no corre: el hub del perfil activo no exige la lista a una entrada ajena y una
        política sin lista que publica ahí no se rechaza (nadie escucha)."""
        robot = ('[robot]\nname = "n"\n\n[[joints]]\nname = "patas"\ncommand = "/patas/cmd"\n'
                 'names = ["cadera", "rodilla", "tobillo"]\nkp = 1.0\nkd = 0.1\nrequire_names = true\n'
                 '\n[[mux]]\nname = "ruedas"\noutput = "/patas/cmd"\nprofile = "otro"\n\n[[mux.input]]\nname = "ia"\n'
                 'topic = "/cmd/ia"\npriority = 1\n')
        robot += self.nodo("sin", ["python", "politica.py", "-", "77.0", "/cmd/ia"])
        robot += self.nodo("lector", ["python", "lector.py"])
        robot += self.nodo("bueno", ["python", "politica.py", "cadera,rodilla,tobillo", "1.0"])
        ok, out = self.correr(robot, lambda t: "VISTO 1.0" in t)
        self.assertTrue(ok, out)
        self.assertNotIn("la lista vale también", out)
        self.assertNotIn("VISTO 77.0", out)


if __name__ == "__main__":
    unittest.main()

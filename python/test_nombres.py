"""Los nombres de las articulaciones declarados al hub (RFC 0013 §3.4, D6 = d).

    python -m unittest discover -s python -p "test_nombres.py"

Quien trae su propia lista (una política, un puente) la pasa con names=[...] al publicar o al suscribirse;
va al hub en el campo `names` de la línea de registro (`pub`/`sub`), y el hub la compara con la de
[[joints]] de robot.toml (que le pasa agmen run con --names) y con la de los demás endpoints del topic.

Con el núcleo de C++ (agmen._core) names= todavía da un error claro (el registro lo arma C++): las pruebas
del camino de Python puro usan pubsub._PurePublisher y _PureSubscriber directo, así corren con los dos
núcleos. Nada espera un tiempo fijo: se espera cada condición, con tope.
"""

import contextlib
import functools
import io
import json
import os
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
from agmen.msgs import JointCommand, JointState
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
    return H.parse_joint_names(names, ["/patas/cmd"] if require else [])


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

    def registro(self, op: str) -> bytes:
        while True:
            data, _ = self.sock.recvfrom(65536)
            if data.startswith(f"{hp.PREFIX} {op} ".encode()):
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
    def test_con_el_nucleo_de_cpp_todavia_no(self):
        for cls in (agmen.Publisher, agmen.Subscriber):
            with self.subTest(cls=cls):
                with self.assertRaises(NotImplementedError) as ctx:
                    cls(CMD12, "/patas/cmd", names=list(PATAS))
                self.assertIn("todavía no funciona con el núcleo de C++", str(ctx.exception))
                self.assertIn("AGMEN_BACKEND=pure", str(ctx.exception))
        node = agmen.Node("n")
        self.addCleanup(node.close)
        with self.assertRaises(NotImplementedError):
            node.publisher(CMD12, "/patas/cmd", names=list(PATAS))
        with self.assertRaises(NotImplementedError):
            node.on(CMD12, "/patas/cmd", lambda s: None, names=list(PATAS))
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
        self.assertIn("'p' declara 11 nombres y robot.toml 12", msg)

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
            ",".join(["a"] * 5000): "names ocupa 9999 caracteres y el máximo es 8192",
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
        self.assertEqual(self.manda_bytes(linea.encode() + b" names=a%ZZ"), [])          # '%' mal escrito: igual
        self.assertNotIn("h", self.hub.endpoints)
        self.entra(self.pub("bien", PATAS))

    def test_nombre_del_nodo_enorme_queda_acotado(self):
        msg = self.rechazo(self.pub("h", PATAS[::-1], node="n" * 5000))
        self.assertLess(len(msg), 700)

    def test_argumentos_del_hub(self):
        for args, frag in ((["/a"], "escríbelo como TOPIC=nombre"), (["/a=x", "/a=y"], "está dos veces"),
                           (["/a=x,,y"], "--names /a: names[1] = ''"), (["a=x"], "nombre de topic inválido")):
            with self.subTest(args=args), self.assertRaises(hp.ProtocolError) as ctx:
                H.parse_joint_names(args, [])
            self.assertIn(frag, str(ctx.exception))
        with self.assertRaisesRegex(hp.ProtocolError, "--require-names /b: ese topic no tiene lista"):
            H.parse_joint_names(["/a=x"], ["/b"])
        j, = H.parse_joint_names(["/a=x,y"], ["/a"])
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
        class Anterior(H.Hub):
            def _check_names(self, *a):
                return None

            def _names_warnings(self, e):
                pass

        self.nuevo_hub(cls=Anterior)
        self.comprobar()


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


class TestHubDeVerdad(unittest.TestCase):
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
            j = H.parse_joint_names([cmd[i + 1] for i, a in enumerate(cmd) if a == "--names"],
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


if __name__ == "__main__":
    unittest.main()

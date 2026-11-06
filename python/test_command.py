"""Los mandos del panel para cualquier tipo (RFC 0012, Agmen 0.13.0).

- [[web.command]] en robot.toml: botones con un mensaje fijo, deslizadores y
  casillas con sus rangos; los errores dicen qué corregir.
- El servidor arma el mensaje con el esquema y revisa cada valor: nunca
  reenvía lo que mandó el navegador.

    python -m unittest discover -s python
"""

import math
import re
import tempfile
import time
import unittest
from pathlib import Path

from agmen import schema
from agmen.panel.command import Command, CommandError, parse_command
from agmen.run import ConfigError, load_config

AGMEN = """
package prueba

message Arca9Comando {
    f32[12] q
    bool[12] activo
}

struct Vec2 {
    f32 x
    f32 y
}

message Brazo {
    u8 modo
    Vec2 punta
    f64 fuerza
    bool pinza
}

message Texto {
    string s
}
"""

PARADO = [87.8, 123.7, 83.9] * 4
ECHADO = [87.8, 130.5, 42.4] * 4


class Publicador:
    """Un Publisher de mentira: guarda lo que se publicó."""

    def __init__(self):
        self.payloads = []

    def publish(self, msg):
        self.payloads.append(msg.encode_payload())
        return True

    def close(self):
        pass


class MandoCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.base = Path(self.dir.name)
        (self.base / "prueba.agmen").write_text(AGMEN, encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def cfg(self, **raw):
        raw.setdefault("target", "/arca9/comando/panel")
        raw.setdefault("type", "Arca9Comando")
        raw.setdefault("messages", "prueba.agmen")
        return parse_command(raw, 0, self.base)

    def poses(self, **extra):
        return self.cfg(name="Poses", default={"activo": [True] * 12},
                        button=[{"label": "parado", "value": {"q": PARADO}}, {"label": "echado", "value": {"q": ECHADO}}],
                        **extra)

    def assertError(self, msg, **raw):
        with self.assertRaises(CommandError) as ctx:
            self.cfg(**raw)
        self.assertIn(msg, str(ctx.exception))


class TestRobotToml(MandoCase):
    def test_botones_completos_con_default(self):
        c = self.poses()
        self.assertEqual(c.layout.text, "agmen/1 Arca9Comando{f32[12] q;bool[12] activo}")
        self.assertEqual([label for label, _ in c.buttons], ["parado", "echado"])
        parado = c.buttons[0][1]
        self.assertEqual(parado["q[1]"], 123.7)
        self.assertTrue(parado["activo[11]"])
        self.assertIsNone(c.start)                               # sin deslizadores no hace falta

    def test_un_campo_olvidado_nunca_va_como_cero(self):
        """Sin 'activo' (ni en el botón ni en default) no arranca: un servo a 0° sería un golpe."""
        with self.assertRaises(CommandError) as ctx:
            self.cfg(button=[{"label": "parado", "value": {"q": PARADO}}])
        self.assertIn("botón 'parado': falta 'activo'", str(ctx.exception))
        self.assertIn("[web.command.default]", str(ctx.exception))

    def test_deslizadores_y_casillas(self):
        c = self.poses(fields={"q": {"min": 20, "max": 160, "labels": [f"s{i}" for i in range(12)]}, "activo": {}})
        self.assertEqual(len(c.controls), 24)
        q0 = c.controls["q[0]"].info()
        self.assertEqual((q0["label"], q0["kind"], q0["min"], q0["max"], q0["step"], q0["int"]),
                         ("s0", "range", 20, 160, 0.1, False))
        self.assertEqual(c.controls["activo[3]"].info(), {"path": "activo[3]", "label": "activo[3]", "kind": "bool"})
        self.assertEqual(c.start, c.buttons[0][1])              # sin default completo: empiezan en el primer botón

    def test_struct_anidado_y_enteros(self):
        c = self.cfg(type="Brazo", default={"modo": 1, "punta": {"x": 0.1, "y": 0.2}, "fuerza": 2.0, "pinza": False},
                     fields={"punta": {"min": -0.5, "max": 0.5}, "modo": {"min": 0, "max": 3}, "pinza": {"label": "pinza"}})
        self.assertEqual(list(c.controls), ["modo", "punta.x", "punta.y", "pinza"])   # en el orden del esquema
        self.assertEqual(c.controls["modo"].info()["step"], 1)
        self.assertTrue(c.controls["modo"].info()["int"])
        self.assertEqual(c.start["punta.y"], 0.2)

    def test_errores_claros(self):
        casos = [
            ("falta 'target'", dict(target="")),
            ("falta 'type'", dict(type="")),
            ("clave desconocida 'botones'", dict(botones=[])),
            ("no tiene un mensaje 'Arca9Comandos'", dict(type="Arca9Comandos")),
            ("no es un tipo de Agmen", dict(type="Arca9Comando", messages=None)),
            ("no existe otro.agmen", dict(messages="otro.agmen")),
            ("tamaño variable", dict(type="Texto", button=[{"label": "x", "value": {"s": "hola"}}])),
            ("no tiene botones", dict()),
            ("debe ser una lista de 12 valores", dict(button=[{"label": "x", "value": {"q": [1.0], "activo": [True] * 12}}])),
            ("debe ser true o false", dict(button=[{"label": "x", "value": {"q": PARADO, "activo": [1] * 12}}])),
            ("'r' no es un campo de Arca9Comando", dict(button=[{"label": "x", "value": {"q": PARADO, "activo": [True] * 12, "r": 1}}])),
            ("hace falta min y max", dict(fields={"q": {}}, default={"q": PARADO, "activo": [True] * 12})),
            ("es bool, va una casilla", dict(fields={"activo": {"min": 0, "max": 1}}, default={"q": PARADO, "activo": [True] * 12})),
            ("min (90) debe ser menor que max (80)", dict(fields={"q": {"min": 90, "max": 80}}, default={"q": PARADO, "activo": [True] * 12})),
            ("'labels' es una lista de 12 textos", dict(fields={"q": {"min": 0, "max": 180, "labels": ["a"]}}, default={"q": PARADO, "activo": [True] * 12})),
            ("'pata' no es un campo", dict(fields={"pata": {"min": 0, "max": 1}}, default={"q": PARADO, "activo": [True] * 12})),
            ("¿con qué valores empiezan los deslizadores?", dict(fields={"q": {"min": 0, "max": 180}})),
            ("hay dos botones 'x'", dict(button=[{"label": "x", "value": {"q": PARADO, "activo": [True] * 12}}] * 2)),
            ("está fuera de su rango (20 a 160", dict(fields={"q": {"min": 20, "max": 160}},
                                                      button=[{"label": "x", "value": {"q": [10.0] * 12, "activo": [True] * 12}}])),
            ("tiene 2 valores y hacen falta 12", dict(fields={"q": {"min": [0, 0], "max": 180}}, default={"q": PARADO, "activo": [True] * 12})),
        ]
        for msg, raw in casos:
            with self.subTest(msg=msg):
                self.assertError(msg, **raw)

    def test_tipos_de_agmen_sin_messages(self):
        c = self.cfg(type="MotorCommand", messages=None, button=[{"label": "adelante", "value": {"left": 0.5, "right": 0.5}}])
        self.assertEqual(c.layout.text, "agmen/1 MotorCommand{f32 left;f32 right}")


class TestServidor(MandoCase):
    def mando(self, cfg=None):
        pub = Publicador()
        cmd = Command(cfg or self.poses(fields={"q": {"min": 20, "max": 160}, "activo": {}}), publisher=pub)
        return cmd, pub

    def unpack(self, cmd, payload):
        return cmd.cfg.layout.unpack(payload)

    def test_boton_por_numero_y_por_letrero(self):
        cmd, pub = self.mando()
        self.assertTrue(cmd.button(1))
        time.sleep(0.02)
        self.assertTrue(cmd.button("parado"))
        echado, parado = (self.unpack(cmd, p) for p in pub.payloads)
        self.assertEqual([round(x, 1) for x in echado["q"]], ECHADO)
        self.assertEqual([round(x, 1) for x in parado["q"]], PARADO)
        self.assertEqual(parado["activo"], [True] * 12)
        info = cmd.info()
        self.assertTrue(info["active"])
        self.assertEqual(info["last"], "parado")
        with self.assertRaisesRegex(CommandError, re.escape("no tiene un botón 'volar'")):
            cmd.button("volar")
        with self.assertRaisesRegex(CommandError, re.escape("no tiene el botón 5")):
            cmd.button(5)

    def test_mas_de_60_por_segundo_se_descartan(self):
        cmd, pub = self.mando()
        self.assertTrue(cmd.button(0))
        self.assertFalse(cmd.button(0))                          # 0 ms después: se descarta, sin error
        self.assertEqual((len(pub.payloads), cmd.skipped), (1, 1))

    def test_campos_revisados_uno_por_uno(self):
        cmd, pub = self.mando()
        self.assertTrue(cmd.fields({"q[1]": 100.25, "activo[0]": False}, base=1))
        m = self.unpack(cmd, pub.payloads[-1])
        self.assertAlmostEqual(m["q"][1], 100.25, places=4)
        self.assertAlmostEqual(m["q"][2], 42.4, places=4)        # lo demás, del botón 1 (echado)
        self.assertFalse(m["activo"][0])
        self.assertTrue(m["activo"][1])
        malos = [
            ({"q[1]": 500}, "fuera de su rango (20 a 160"),
            ({"q[1]": math.nan}, "NaN"),
            ({"q[1]": math.inf}, "NaN ni infinito"),
            ({"q[1]": "90"}, "debe ser un número"),
            ({"q[1]": True}, "debe ser un número"),
            ({"activo[1]": 1}, "true o false"),
            ({"q[12]": 90}, "no tiene control"),
            ({"__proto__": 1}, "no tiene control"),
        ]
        for values, msg in malos:
            with self.subTest(values=values):
                with self.assertRaisesRegex(CommandError, re.escape(msg)):
                    cmd.fields(values)
        with self.assertRaisesRegex(CommandError, re.escape("base es el número de un botón")):
            cmd.fields({}, base=9)
        self.assertEqual(len(pub.payloads), 1)                   # nada de lo malo se publicó

    def test_solo_se_controla_lo_declarado(self):
        cfg = self.cfg(type="Brazo", default={"modo": 2, "punta": {"x": 0.1, "y": 0.2}, "fuerza": 7.5, "pinza": True},
                       fields={"punta.x": {"min": -0.5, "max": 0.5}, "modo": {"min": 0, "max": 3}})
        cmd, pub = self.mando(cfg)
        cmd.fields({"punta.x": -0.25, "modo": 3.0})              # 3.0 de JavaScript es el entero 3
        m = self.unpack(cmd, pub.payloads[-1])
        self.assertEqual((m["modo"], m["fuerza"], m["pinza"]), (3, 7.5, True))
        self.assertAlmostEqual(m["punta"]["x"], -0.25)
        self.assertAlmostEqual(m["punta"]["y"], 0.2, places=6)   # sin control: el de robot.toml
        with self.assertRaisesRegex(CommandError, re.escape("no tiene control")):
            cmd.fields({"fuerza": 100.0})                        # sin control en robot.toml: no se toca
        time.sleep(0.02)
        with self.assertRaisesRegex(CommandError, re.escape("debe ser un entero")):
            cmd.fields({"modo": 1.5})

    def test_los_bytes_son_los_del_esquema(self):
        cmd, pub = self.mando()
        cmd.button(0)
        layout = schema.Layout("Arca9Comando{f32[12] q;bool[12] activo}")
        self.assertEqual(pub.payloads[0], layout.pack({"q": PARADO, "activo": [True] * 12}))


class TestEnRobotToml(unittest.TestCase):
    TOML = """
[robot]
name = "perro"

[[node]]
name = "puente"
cmd = "python puente.py"

[[web.command]]
name = "Poses"
target = "/arca9/comando/panel"
type = "Arca9Comando"
messages = "prueba.agmen"

[web.command.default]
activo = [true, true, true, true, true, true, true, true, true, true, true, true]

[[web.command.button]]
label = "parado"
value = { q = [87.8, 123.7, 83.9, 87.8, 123.7, 83.9, 87.8, 123.7, 83.9, 87.8, 123.7, 83.9] }

[web.command.fields]
q = { min = 20, max = 160 }

[[topic]]
name = "/arca9/articulaciones"
max_gap_ms = 200
"""

    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "prueba.agmen").write_text(AGMEN, encoding="utf-8")
            path = Path(d) / "robot.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    def test_se_lee(self):
        cfg = self.load(self.TOML)
        (c,) = cfg.web.commands
        self.assertEqual((c.name, c.target, c.layout.name, len(c.controls)), ("Poses", "/arca9/comando/panel",
                                                                             "Arca9Comando", 12))
        self.assertEqual(cfg.topics[0].max_gap_ms, 200.0)
        self.assertIn("/arca9/comando/panel", cfg.topics_used())

    def test_errores_de_robot_toml(self):
        casos = [
            (self.TOML.replace('type = "Arca9Comando"', 'type = "Arca9Comandos"'), "no tiene un mensaje 'Arca9Comandos'"),
            (self.TOML.replace("max_gap_ms = 200", "max_gap_ms = 0"), "max_gap_ms son los milisegundos"),
            (self.TOML.replace("max_gap_ms = 200", 'max_gap_ms = "200"'), "max_gap_ms son los milisegundos"),
            (self.TOML + '\n[[web.command]]\nname = "Poses"\ntarget = "/x"\ntype = "MotorCommand"\n'
                         '[[web.command.button]]\nlabel = "a"\nvalue = { left = 0.1, right = 0.1 }\n',
             "hay dos [[web.command]] llamados 'Poses'"),
        ]
        for text, msg in casos:
            with self.subTest(msg=msg):
                with self.assertRaises(ConfigError) as ctx:
                    self.load(text)
                self.assertIn(msg, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

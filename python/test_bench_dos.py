"""bench/dos_computadoras.py --local --rapido (RFC 0011 §7, punto 7): el banco de dos
computadoras corre de punta a punta en loopback (agmen agent + agmen run, dos IP 127.x)
y su tabla trae números razonables. No mide nada: solo que el banco sirve."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANCO = ROOT / "bench" / "dos_computadoras.py"


def _ip_disponible(ip: str) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


@unittest.skipUnless(_ip_disponible("127.0.0.2") and _ip_disponible("127.0.0.3"),
                     "el banco --local necesita 127.0.0.2 y 127.0.0.3 como direcciones de esta computadora "
                     "(en macOS: sudo ifconfig lo0 alias 127.0.0.2 y lo mismo con 127.0.0.3)")
class BancoDosComputadoras(unittest.TestCase):
    def test_local_rapido(self):
        with tempfile.TemporaryDirectory(prefix="agmen_test_dos_") as tmp:
            salida_json = Path(tmp) / "res.json"
            try:
                r = subprocess.run([sys.executable, str(BANCO), "--local", "--rapido", "--json", str(salida_json)],
                                   cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   stdin=subprocess.DEVNULL, timeout=150,
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8", "TMPDIR": tmp})
            except subprocess.TimeoutExpired as e:
                self.fail(f"el banco no terminó en 150 s:\n{e.stdout}\n{e.stderr}")
            texto = r.stdout + r.stderr
            self.assertEqual(r.returncode, 0, texto)
            self.assertIn("| Codificar+decodificar |", r.stdout, texto)
            for etiqueta in ("| 64 B |", "| máx. UDP |", "| 1 MiB |"):
                self.assertIn(etiqueta, r.stdout, texto)
            self.assertIn("NO son números de una red", r.stdout, texto)
            res = json.loads(salida_json.read_text(encoding="utf-8"))
            self.assertEqual(res["error"], "", texto)
            filas = {f["etiqueta"]: f for f in res["filas"]}
            self.assertEqual(set(filas), {"64 B", "máx. UDP", "1 MiB"}, texto)
            self.assertEqual(filas["64 B"]["camino"], "UDP", texto)
            self.assertEqual(filas["máx. UDP"]["camino"], "UDP", texto)
            self.assertEqual(filas["1 MiB"]["camino"], "TCP", texto)   # AGMEN_SHM=0: nunca memoria compartida
            for f in filas.values():
                with self.subTest(tamaño=f["etiqueta"]):
                    self.assertGreater(f["lat_med"], 0, texto)
                    self.assertGreaterEqual(f["lat_p99"], f["lat_med"], texto)
                    self.assertLess(f["lat_med"], 1000, texto)          # menos de 1 s en loopback
                    self.assertGreaterEqual(f["muestras"], 5, texto)
                    self.assertLessEqual(f["sin_respuesta"], f["muestras"] // 5, texto)
                    self.assertGreater(f["mbs"], 0, texto)
                    self.assertGreater(f["msgs"], 0, texto)
                    self.assertGreater(f["recibidos"], 1, texto)
                    # Las cuentas cuadran. Sin umbral de pérdida: en UDP, el que publica sin parar puede llenar el
                    # búfer del que recibe y cuánto se pierde depende de la máquina. Tampoco "perdidos == 0" en TCP:
                    # no es una garantía ni en loopback (un mensaje que iba en una conexión que se cierra o se
                    # rehace se pierde sin contar como descartado D3); el banco lo reporta, la prueba no lo exige.
                    self.assertEqual(f["recibidos"] + f["perdidos"] + f["descartados"], f["enviados"], texto)
                    self.assertGreater(f["cod_ms"], 0, texto)   # codificar+decodificar, medido aparte y sin red
                    self.assertEqual(f["limita_publicador"], f["recibidos"] == f["enviados"], texto)
            self.assertEqual(res["eco_ip"], "127.0.0.3", texto)   # el eco corrió en el "robot"
            self.assertEqual(res["eco"].get("desde"), "127.0.0.2", texto)



def _banco():
    """El módulo del banco, importado (sin correrlo)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("banco_dos_computadoras", BANCO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TablaDelBanco(unittest.TestCase):
    def fila(self, **cambio):
        f = dict(etiqueta="64 B", bytes=64, camino="UDP", cod_ms=0.012, lat_med=0.5, lat_p99=1.0, muestras=30,
                 sin_respuesta=0, enviados=100, recibidos=100, descartados=0, perdidos=0, mbs=1.0, msgs=100.0,
                 limita_publicador=True)
        return {**f, **cambio}

    def res(self, filas):
        lap = dict(agmen="0", sistema="x", nucleo="Python puro", python="3")
        return dict(filas=filas, laptop=lap, eco={}, eco_ip="", local=True, error="")

    def test_marca_cuando_llego_todo(self):
        t = _banco().tabla(self.res([self.fila()]))
        self.assertIn("| 0.012 ms |", t)
        self.assertIn("| 1.0 ¹ | 100 ¹ |", t)
        self.assertIn("¹ el eco recibió todo lo enviado: ese ritmo lo limitó quien publica", t)

    def test_sin_marca_si_se_perdio_algo(self):
        t = _banco().tabla(self.res([self.fila(recibidos=90, perdidos=10, limita_publicador=False)]))
        self.assertNotIn("¹", t)

    def test_codifica_ms_y_mensaje_del_tamano_justo(self):
        b = _banco()
        for etiqueta, tipo, tam in b.TAMANOS[:4] + b.TAMANOS[-2:-1]:
            with self.subTest(etiqueta):
                msg = b.mensaje(tipo, tam, 7, b.RITMO, b.datos_de(tam), ronda=3)
                if tipo is not b.M.CargaGrande:
                    self.assertEqual(len(msg.encode_payload()), tam)   # el tamaño de la tabla es el de verdad
                m = tipo.decode_payload(msg.encode_payload())
                self.assertEqual((m.n, m.fase, m.ronda), (7, b.RITMO, 3))
                self.assertGreater(b.codifica_ms(tipo, tam, b.datos_de(tam)), 0)


if __name__ == "__main__":
    unittest.main()

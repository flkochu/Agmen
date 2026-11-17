"""agmen.Recording y agmen export (RFC 0010 D7 y D8): leer una grabación en Python.

Corre con los dos núcleos. Se arma una grabación con valores conocidos (tipos
que el programa conoce y otros que no, compact y arrow, un paquete con dos
filas) y se revisa: los mensajes, las tablas (pyarrow es el juez de los
tipos), el Parquet y el MCAP en JSON para Foxglove (la biblioteca oficial de
MCAP es el juez). Sin pyarrow o sin `mcap`, esas pruebas se saltan.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import gc
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import agmen
from agmen import _mcap, recording, schema, wire
from agmen import hub as hubmod
from agmen.msgs import Imagen

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover
    pa = pq = None

try:
    from mcap.reader import make_reader
except ImportError:  # pragma: no cover
    make_reader = None

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_700_000_000_000_000_000
STEP = 10_000_000                          # 10 ms
N = 12


@dataclass
class Cmd(wire.Message):
    SCHEMA = "Cmd{f32 left;f32 right}"
    left: float
    right: float


# Dos tipos que este programa no conoce: Recording les arma su clase con el esquema grabado.
ESTADO = schema.Layout("Estado{f64 x;time t;i8[3] flags;bool ok;Sub{u16 a;bool b} sub;f32[2][3] m;bool[2] bits;"
                       "P{u8 k;f32 v}[2] ps;duration dt;u64 seq}")
DETECCION = schema.Layout("Deteccion{string clase;f32 confianza;f32[4] caja;f32[] puntos;tensor<u8,2> mapa;"
                          "bytes crudo}")


def estado(i: int) -> dict:
    return {"x": i / 2, "t": T0 + i, "flags": [i, -i, 1], "ok": i % 2 == 0, "sub": {"a": 100 + i, "b": i % 3 == 0},
            "m": [[i, 1, 2], [3, 4, 5]], "bits": [True, i % 2 == 1], "ps": [{"k": i, "v": 0.5}, {"k": 2, "v": i}],
            "dt": -i, "seq": 1000 + i}


def deteccion(i: int) -> dict:
    return {"clase": f"gato{i}", "confianza": 0.25 * (i % 4), "caja": [i, 2, 3, 4],
            "puntos": [float(k) for k in range(i)],
            "mapa": {"shape": (2, 3), "data": [i, 1, 2, 3, 4, 5]}, "crudo": bytes([i]) * i}


def imagen(i: int) -> Imagen:
    return Imagen(timestamp=T0 + i, frame_id="camara", width=4, height=2, encoding="mono8", step=4,
                  data=bytes(range(i, i + 8)))


def packet(layout: schema.Layout, payload: bytes, topic: str, src: int, seq: int, t: int) -> bytes:
    env = wire.Envelope(layout.encoding, 0, layout.schema_id, wire.topic_id(topic), src, seq, t - 2_000_000,
                        t - 1_000_000, len(payload), wire.CLOCK_REAL)
    return env.pack() + payload


def write_recording(path: str, compression=None) -> None:
    w = _mcap.Writer(path, chunk_size=4096, compression=compression)
    chans = {}
    for topic, layout in (("/imu", wire.ImuSample.LAYOUT), ("/cmd", Cmd.LAYOUT), ("/robot/estado", ESTADO),
                          ("/camara", Imagen.LAYOUT), ("/det", DETECCION)):
        sid = w.add_schema(layout.name, "agmen", layout.text.encode())
        chans[topic] = (w.add_channel(topic, "agmen", sid), layout)
    w.add_metadata("agmen.grabacion", {"version": agmen.__version__, "reloj": "real", "robot": "prueba"})
    w.add_attachment("robot.toml", "text/plain", b'[robot]\nname = "prueba"\n', T0, T0)
    for i in range(N):
        t = T0 + i * STEP
        imu = wire.ImuSample((float(i), 0.0, 9.8), (0.0, 0.0, float(-i))).encode_payload()
        if i == N - 1:
            imu += wire.ImuSample((99.0, 0.0, 0.0), (0.0, 0.0, 0.0)).encode_payload()   # dos filas en un paquete
        rows = [("/imu", imu, 0x11), ("/cmd", Cmd(i / 10, -i / 10).encode_payload(), 0x22),
                ("/robot/estado", ESTADO.pack(estado(i)), 0x33)]
        if i % 3 == 0:
            rows += [("/camara", imagen(i).encode_payload(), 0x44), ("/det", DETECCION.pack(deteccion(i)), 0x55)]
        for k, (topic, payload, src) in enumerate(rows):
            cid, layout = chans[topic]
            if isinstance(payload, memoryview):
                payload = payload.tobytes()
            w.add_message(cid, t + k, t - 1_000_000, i, packet(layout, payload, topic, src, i, t))
    w.add_metadata("agmen.perdidos", {"/imu antes de llegar": "2", "total": "2"})
    w.close()


def close_quoting(obj) -> str:
    """obj.close(), y lo que dijo por stderr (el aviso de un archivo que no se pudo soltar)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        obj.close()
    return err.getvalue()


def mapped(path: str) -> bool | None:
    """¿El archivo sigue mapeado en este proceso? (Linux: /proc/self/maps; en otros, None: no se sabe)."""
    try:
        with open("/proc/self/maps", encoding="utf-8", errors="replace") as f:
            maps = f.read()
    except OSError:
        return None
    return os.path.realpath(path) in maps


def opened(path: str) -> bool | None:
    """¿Este proceso tiene el archivo abierto? (Linux: /proc/self/fd; en otros, None: no se sabe)."""
    try:
        fds = os.listdir("/proc/self/fd")
    except OSError:
        return None
    real = os.path.realpath(path)
    for fd in fds:
        try:
            if os.readlink(f"/proc/self/fd/{fd}") == real:
                return True
        except OSError:
            pass
    return False


def damage_chunk(path: str, k: int) -> None:
    """Cambia un byte dentro de los datos del chunk k: al leerlo, McapError (CRC)."""
    with _mcap.Reader(path) as r:
        start = r.chunks[k].start
    with open(path, "r+b") as f:
        f.seek(start + 80)
        b = f.read(1)
        f.seek(start + 80)
        f.write(bytes([b[0] ^ 0xFF]))


def write_short_packet(path: str) -> None:
    """Una grabación (con robot.toml adjunto) cuyo tercer /cmd es un paquete cortado: wire.parse da WireError."""
    w = _mcap.Writer(path)
    cid = w.add_channel("/cmd", "agmen", w.add_schema(Cmd.LAYOUT.name, "agmen", Cmd.LAYOUT.text.encode()))
    w.add_attachment("robot.toml", "text/plain", b'[robot]\nname = "prueba"\n', T0, T0)
    for i in range(4):
        pkt = packet(Cmd.LAYOUT, Cmd(i, -i).encode_payload(), "/cmd", 0x22, i, T0 + i * STEP)
        w.add_message(cid, T0 + i * STEP, T0, i, pkt[:-3] if i == 2 else pkt)
    w.close()


class RecordingCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "vuelta.mcap")
        write_recording(self.path)
        self.rec = agmen.Recording(self.path)
        self.addCleanup(self.rec.close)


class TestLeer(RecordingCase):
    def test_topics_y_clases(self):
        rec = self.rec
        self.assertEqual(list(rec.topics), ["/camara", "/cmd", "/det", "/imu", "/robot/estado"])
        self.assertIs(rec.topics["/imu"], wire.ImuSample)
        self.assertIs(rec.topics["/cmd"], Cmd, "una @dataclass del programa con el mismo esquema")
        self.assertIs(rec.topics["/camara"], Imagen)
        made = rec.topics["/robot/estado"]
        self.assertEqual((made.NAME, made.SCHEMA_ID), ("Estado", ESTADO.schema_id))
        self.assertEqual(rec.schema("/robot/estado"), ESTADO.text)
        self.assertEqual(rec.info["robot"], "prueba")
        self.assertEqual(rec.lost, {"/imu antes de llegar": 2, "total": 2})
        self.assertEqual(rec.attachments, {"robot.toml": b'[robot]\nname = "prueba"\n'})
        self.assertTrue(rec.complete)
        self.assertEqual(rec.count("/imu"), N)
        self.assertEqual(len(rec), 3 * N + 2 * 4)
        self.assertEqual(rec.start, T0)
        self.assertAlmostEqual(rec.duration, ((N - 1) * STEP + 4) / 1e9)
        self.assertIn("/robot/estado: Estado", repr(rec))

    def test_mensajes_en_orden(self):
        got = list(self.rec.messages())
        self.assertEqual(len(got), 3 * N + 2 * 4 + 1, "el paquete de dos filas da dos mensajes")
        self.assertEqual([s.log_time for s in got], sorted(s.log_time for s in got))
        s = got[0]
        self.assertIsInstance(s, agmen.Sample)
        self.assertEqual((s.topic, s.log_time, s.sender), ("/imu", T0, "vuelta.mcap"))
        self.assertEqual((s.envelope.source_id, s.envelope.seq, s.envelope.t_data), (0x11, 0, T0 - 2_000_000))
        imus = [s.message.accel[0] for s in self.rec.messages("/imu")]
        self.assertEqual(imus, [float(i) for i in range(N)] + [99.0])
        est = [s.message for s in self.rec.messages(["/robot/estado"])]
        self.assertEqual(est[5].sub, {"a": 105, "b": False})
        self.assertEqual((est[5].t, est[5].dt, list(est[5].flags), est[5].seq), (T0 + 5, -5, [5, -5, 1], 1005))
        det = [s.message for s in self.rec.messages("/det")]
        self.assertEqual([d.clase for d in det], ["gato0", "gato3", "gato6", "gato9"])
        self.assertEqual(bytes(det[1].crudo), b"\x03\x03\x03")
        self.assertEqual([bytes(s.message.data) for s in self.rec.messages("/camara")],
                         [bytes(range(i, i + 8)) for i in (0, 3, 6, 9)])

    def test_ventana_de_tiempo(self):
        got = list(self.rec.messages("/cmd", start=0.02, end=0.05))
        self.assertEqual([s.envelope.seq for s in got], [2, 3, 4])
        with self.assertRaisesRegex(ValueError, "no tiene el topic /nada"):
            list(self.rec.messages("/nada"))

    def test_cerrar_con_mensajes_vivos(self):
        cams = list(self.rec.messages("/camara"))
        text = close_quoting(self.rec)
        self.assertEqual(bytes(cams[-1].message.data), bytes(range(9, 17)), "lo leído sigue sirviendo")
        self.assertEqual(text.count("[agmen] ⚠"), 1, "los mensajes vivos detienen el archivo: close() lo avisa")
        self.assertIn(self.path, text)

    def test_comprimida_y_cortada(self):
        z = os.path.join(self.dir, "z.mcap")
        try:
            write_recording(z, compression="zstd")
        except _mcap.McapError:
            self.skipTest("sin zstandard")
        with agmen.Recording(z) as rec:
            self.assertEqual(len(list(rec.messages())), 3 * N + 2 * 4 + 1)
        with open(self.path, "rb") as f:
            data = f.read()
        cut = os.path.join(self.dir, "cortada.mcap")
        with open(cut, "wb") as f:
            f.write(data[: len(data) * 6 // 10])
        with agmen.Recording(cut) as rec:
            self.assertFalse(rec.complete)
            seqs = [s.envelope.seq for s in rec.messages("/cmd")]
            self.assertGreater(len(seqs), 2)
            self.assertEqual(seqs, list(range(len(seqs))), "hasta el último chunk completo, sin huecos")

    def test_no_es_de_agmen(self):
        other = os.path.join(self.dir, "otro.mcap")
        w = _mcap.Writer(other, profile="")
        w.add_message(w.add_channel("/a", "json", w.add_schema("S", "jsonschema", b"{}")), 1, 1, 0, b"{}")
        w.close()
        with self.assertRaisesRegex(ValueError, "no es una grabación de Agmen"):
            agmen.Recording(other)
        with self.assertRaisesRegex(FileNotFoundError, "no existe"):
            agmen.Recording(os.path.join(self.dir, "no-existe.mcap"))


class TestCerrar(unittest.TestCase):
    """close() suelta el archivo de verdad (en Windows, si no, no se puede borrar ni reemplazar): el lector
    suelta sus propias vistas del mapa (los adjuntos, como el robot.toml de agmen run), y si el programa
    conserva un mensaje, avisa una vez y lo suelta en otro close() o en cuanto nada lo use."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "vuelta.mcap")
        write_recording(self.path)          # con un adjunto, como las de agmen run

    def assertClosed(self, reader):
        self.assertFalse(reader._mapped, "el estado del lector: ya no tiene el archivo mapeado")
        self.assertIsNone(reader._mm)
        self.assertIsNone(reader._mm_left)
        self.assertIn(mapped(self.path), (False, None), "/proc/self/maps ya no lo trae")
        os.remove(self.path)                # en Windows falla si sigue mapeado
        self.assertFalse(os.path.exists(self.path))

    def assertWarnedOnce(self, text):
        self.assertEqual(text.count("[agmen] ⚠"), 1, text)
        for words in (self.path, "sigue abierta", "sigue en uso", "Windows", "borrar ni reemplazar",
                      "recorrido de messages() a medias", "for m in rec.messages(...)", "as_array()",
                      "bytes de una imagen", "table()", "del m", "copy.deepcopy(msg)", "arr.copy()",
                      "tabla.take(numpy.arange(tabla.num_rows))"):
            self.assertIn(words, text)

    def read_everything(self, rec):
        """Lee todo sin quedarse con nada (en otra función: al volver no queda ninguna variable viva)."""
        self.assertEqual(rec.attachments, {"robot.toml": b'[robot]\nname = "prueba"\n'})
        self.assertEqual(sum(1 for _ in rec.messages()), 3 * N + 2 * 4 + 1)
        for s in rec.messages("/camara"):
            self.assertEqual(len(s.message.data), 8)

    def test_con_adjuntos_y_sin_conservar_nada_queda_cerrada(self):
        rec = agmen.Recording(self.path)
        self.read_everything(rec)
        self.assertTrue(rec._r._mapped)
        self.assertEqual(close_quoting(rec), "", "no conserva nada: no avisa")
        self.assertClosed(rec._r)
        self.assertEqual(close_quoting(rec), "", "cerrar dos veces no hace nada")

    def test_el_lector_suelta_sus_adjuntos(self):
        r = _mcap.Reader(self.path)
        self.assertEqual([a.name for a in r.attachments], ["robot.toml"])
        self.assertEqual(close_quoting(r), "")
        self.assertEqual(r.attachments, [], "el lector suelta su referencia")
        self.assertClosed(r)

    def test_un_adjunto_guardado_sigue_sirviendo_y_avisa(self):
        r = _mcap.Reader(self.path)
        keep = r.attachments[0].data          # la vista del adjunto, sin copiar
        self.assertWarnedOnce(close_quoting(r))
        self.assertEqual(bytes(keep), b'[robot]\nname = "prueba"\n', "lo leído sigue sirviendo")
        del keep
        gc.collect()
        self.assertFalse(r._mapped)
        self.assertEqual(close_quoting(r), "")
        self.assertClosed(r)

    def test_un_recorrido_a_medias_avisa(self):
        rec = agmen.Recording(self.path)
        it = rec.messages()
        next(it)                              # el recorrido guarda el chunk en curso (vistas del archivo)
        self.assertWarnedOnce(close_quoting(rec))
        with self.assertRaisesRegex(ValueError, "ya está cerrado"):
            next(it)                          # se porta como cerrado: no sigue leyendo
        del it
        gc.collect()
        self.assertFalse(rec._r._mapped)
        self.assertEqual(close_quoting(rec), "")
        self.assertClosed(rec._r)

    def assertErrorWarning(self, text):
        self.assertEqual(text.count("[agmen] ⚠"), 1, text)
        for words in (self.path, "se cerró por un error", "Lo guarda el error", "como la de un for",
                      "o el programa", "un mensaje en una lista", "Windows", "se suelta solo",
                      "copy.deepcopy(msg)", "arr.copy()", "del e", "o la variable que lo guarda (del m)"):
            self.assertIn(words, text)

    def test_chunk_danado_sin_que_el_programa_guarde_nada(self):
        """Un error a medio recorrido (aquí, un chunk dañado): el traceback guarda los marcos del lector, con
        mensajes y chunks (vistas). Al salir del with, el lector suelta los suyos: cierra de verdad, sin aviso."""
        damage_chunk(self.path, 2)
        err, error = io.StringIO(), None
        with contextlib.redirect_stderr(err):
            try:
                with agmen.Recording(self.path) as rec:
                    list(rec.messages())          # la lista a medias se descarta con el error
            except _mcap.McapError as e:
                error = e                         # con su traceback vivo
        self.assertIn("CRC", str(error))
        self.assertEqual(err.getvalue(), "")
        self.assertClosed(rec._r)

    def test_chunk_danado_en_un_genexpr(self):
        """sum(1 for _ in r.messages()): el marco del genexpr (código del programa) terminó por el error y su
        `_` (el último mensaje, una vista) queda en el traceback. Agmen no toca marcos del programa: mientras
        el error vive, el archivo sigue abierto y el aviso del camino de error lo dice; en cuanto nadie guarda
        el error (al salir del except), se suelta solo."""
        damage_chunk(self.path, 2)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                with _mcap.Reader(self.path) as r:
                    sum(1 for _ in r.messages())
            except _mcap.McapError:
                pass                              # nadie guarda el error: al salir del except se descarta
        self.assertErrorWarning(err.getvalue())
        gc.collect()
        self.assertFalse(r._mapped, "sin el error, nada lo usa: se suelta solo")
        self.assertEqual(close_quoting(r), "")
        self.assertClosed(r)

    def test_chunk_danado_con_la_variable_del_for_viva(self):
        """La variable de un for normal también es del programa: el aviso del camino de error, una vez."""
        damage_chunk(self.path, 2)
        err = io.StringIO()
        with (contextlib.redirect_stderr(err), self.assertRaisesRegex(_mcap.McapError, "CRC"),
              _mcap.Reader(self.path) as r):
            for m in r.messages():
                pass
        self.assertErrorWarning(err.getvalue())
        del m
        gc.collect()
        self.assertFalse(r._mapped, "al soltar la variable, se suelta solo")
        self.assertEqual(close_quoting(r), "")
        self.assertClosed(r)

    def test_un_mensaje_guardado_y_luego_un_error_cualquiera(self):
        guardados = []
        err = io.StringIO()
        with (contextlib.redirect_stderr(err), self.assertRaisesRegex(KeyError, "otra cosa"),
              agmen.Recording(self.path) as rec):
            guardados.append(next(iter(rec.messages("/camara"))))
            raise KeyError("otra cosa")
        self.assertErrorWarning(err.getvalue())       # dice que puede ser el programa (la lista), no solo el error
        self.assertEqual(len(guardados[0].message.data), 8, "lo guardado sigue sirviendo")
        guardados.clear()
        gc.collect()
        self.assertFalse(rec._r._mapped)
        self.assertEqual(close_quoting(rec), "")
        self.assertClosed(rec._r)

    @staticmethod
    def frame_locals(error, name):
        tb = error.__traceback__
        while tb is not None:
            if tb.tb_frame.f_code.co_name == name:
                return dict(tb.tb_frame.f_locals)
            tb = tb.tb_next
        raise AssertionError(f"{name} no está en el traceback")

    def test_no_borra_las_variables_del_programa(self):
        """Caso A: un error del programa (aunque no tenga que ver con la grabación) sale del with: sus marcos
        conservan sus variables (pdb.post_mortem, %debug, pytest --showlocals)."""
        def helper(r):
            secreto = 42      # noqa: F841 — la variable que debe sobrevivir
            raise RuntimeError("ajeno")

        for open_ in (_mcap.Reader, agmen.Recording):
            with self.subTest(open_.__name__):
                error, err = None, io.StringIO()
                with contextlib.redirect_stderr(err):
                    try:
                        with open_(self.path) as r:
                            helper(r)
                    except RuntimeError as e:
                        error = e
                self.assertEqual(self.frame_locals(error, "helper")["secreto"], 42)
                self.assertEqual(err.getvalue(), "", "helper no guarda nada leído: cierra sin avisar")
                self.assertFalse((r._r if open_ is agmen.Recording else r)._mapped)

    def test_no_termina_un_generador_del_programa(self):
        """Caso B: un generador del programa (suspendido) aparece en el traceback: sigue vivo después."""
        def gen():
            try:
                raise RuntimeError("guardado")
            except RuntimeError as e:
                capturado = e
            yield capturado
            yield "sigue vivo"

        g = gen()
        error = next(g)                           # el traceback del error trae el marco de g (ahora suspendido)
        with contextlib.suppress(RuntimeError), _mcap.Reader(self.path):
            raise error
        self.assertEqual(next(g), "sigue vivo")

    def test_generador_suspendido_de_otro_modulo_de_agmen(self):
        """La regla de los generadores: de Agmen se limpian las funciones que ya terminaron (de cualquier
        agmen.*), pero un generador de un módulo agmen.* que no es de grabaciones podría estar suspendido y
        seguir en uso (en 3.11/3.12, frame.clear() lo daría por terminado): no se toca."""
        ns = {"__name__": "agmen.otro_modulo"}
        exec(compile(textwrap.dedent("""
            def gen():
                try:
                    raise RuntimeError("guardado")
                except RuntimeError as e:
                    capturado = e
                yield capturado
                yield "sigue vivo"

            def funcion():
                local = "de agmen"
                raise ValueError("de una función de agmen")
        """), "<agmen.otro_modulo>", "exec"), ns)
        g = ns["gen"]()
        error = next(g)                           # el traceback trae el marco de g, suspendido
        with contextlib.suppress(RuntimeError), _mcap.Reader(self.path):
            raise error
        self.assertEqual(next(g), "sigue vivo")
        try:                                      # y una función de agmen.* que ya terminó sí se limpia
            with _mcap.Reader(self.path):
                ns["funcion"]()
        except ValueError as e:
            error = e
        self.assertNotIn("local", self.frame_locals(error, "funcion"))

    def test_throw_no_borra_las_variables_del_programa(self):
        """Caso C: un error del programa que entra a messages() con throw() conserva las variables de su marco."""
        def falla():
            local = "del programa"   # noqa: F841 — la variable que debe sobrevivir
            raise ValueError("ajeno")

        for which in ("Reader", "Recording"):
            with self.subTest(which):
                try:
                    falla()
                except ValueError as e:
                    error = e
                src = _mcap.Reader(self.path) if which == "Reader" else agmen.Recording(self.path)
                it = src.messages()
                next(it)
                got = None
                try:                              # sin assertRaises: en 3.13 le quita el traceback al error
                    it.throw(error)
                except ValueError as e:
                    got = e
                self.assertIs(got, error)
                self.assertEqual(self.frame_locals(error, "falla")["local"], "del programa")
                del it, error, got
                self.assertEqual(close_quoting(src), "")

    def test_paquete_cortado_no_culpa_al_programa(self):
        """wire.parse (y decode_payload) reciben las vistas del archivo: al salir por su error, sus marcos (de
        agmen.wire, de Agmen) también se sueltan. Sin aviso y sin mapear, aunque el error siga vivo."""
        write_short_packet(self.path)
        for which in ("Recording", "Reader"):
            with self.subTest(which):
                err, error = io.StringIO(), None
                with contextlib.redirect_stderr(err):
                    try:
                        if which == "Recording":
                            with agmen.Recording(self.path) as src:
                                for _s in src.messages():
                                    pass
                        else:
                            with _mcap.Reader(self.path) as reader:
                                for _p in map(wire.parse, (m.data for m in reader.messages())):
                                    pass
                    except wire.WireError as e:
                        error = e                 # con su traceback vivo
                    if which == "Recording":
                        reader = src._r
                self.assertIsInstance(error, wire.WireError)
                self.assertEqual(err.getvalue(), "")
                self.assertFalse(reader._mapped)
                self.assertIn(mapped(self.path), (False, None))
        os.remove(self.path)

    def test_reparar_sobre_si_misma_se_rechaza_antes_de_abrir(self):
        """repair(x, x) truncaría la grabación que está leyendo (mapeada: en Linux, Bus error)."""
        size = os.path.getsize(self.path)
        for out in (self.path, os.path.join(os.path.dirname(self.path), ".", "vuelta.mcap")):
            with self.assertRaisesRegex(ValueError, "la salida es la misma grabación.*da otra ruta"):
                _mcap.repair(self.path, out)
        self.assertEqual(os.path.getsize(self.path), size, "no la tocó")
        self.assertIn(opened(self.path), (False, None))

    def test_reparar_deja_nota_si_no_puede_borrar_la_salida(self):
        damage_chunk(self.path, 2)
        out = os.path.join(os.path.dirname(self.path), "reparada.mcap")
        error = None
        with mock.patch.object(_mcap.os, "remove", side_effect=PermissionError("en uso")):
            try:
                _mcap.repair(self.path, out)
            except _mcap.McapError as e:
                error = e
        self.assertIn("CRC", str(error))
        notes = getattr(error, "__notes__", [])
        self.assertEqual(len(notes), 1, notes)
        self.assertIn(out, notes[0])
        self.assertIn("quedó a medias", notes[0])
        self.assertIn("bórralo", notes[0])
        os.remove(out)

    def test_reparar_un_chunk_danado_suelta_los_dos_archivos(self):
        damage_chunk(self.path, 2)
        out = os.path.join(os.path.dirname(self.path), "reparada.mcap")
        err, error = io.StringIO(), None
        with contextlib.redirect_stderr(err):
            try:
                _mcap.repair(self.path, out)
            except _mcap.McapError as e:
                error = e                     # con su traceback todavía vivo (assertRaises lo limpiaría)
        self.assertIn("CRC", str(error))
        self.assertEqual(err.getvalue(), "")
        self.assertFalse(os.path.exists(out), "la salida a medias se borra (no sirve)")
        self.assertIn(mapped(self.path), (False, None))
        self.assertIn(opened(self.path), (False, None))
        os.remove(self.path)

    def test_si_el_mapa_falla_no_queda_abierto(self):
        error = None
        with mock.patch.object(_mcap.mmap, "mmap", side_effect=OSError("sin memoria para mapear")):
            try:
                _mcap.Reader(self.path)
            except OSError as e:
                error = e                     # con su traceback (y el lector a medias) todavía vivos
        self.assertIn("sin memoria", str(error))
        self.assertIn(opened(self.path), (False, None), "el archivo no quedó abierto")
        os.remove(self.path)

    def test_variable_del_for_avisa_una_vez_y_se_cierra_despues(self):
        rec = agmen.Recording(self.path)
        for m in rec.messages("/camara"):       # al salir, m sigue apuntando a la última imagen (una vista)
            pass
        self.assertWarnedOnce(close_quoting(rec))
        self.assertTrue(rec._r._mapped, "sigue abierta mientras m la use")
        self.assertEqual(bytes(m.message.data), bytes(range(9, 17)), "lo leído sigue sirviendo")
        self.assertEqual(close_quoting(rec), "", "un solo aviso por lector")
        # Entre un close() que no pudo y el siguiente, el lector se porta como cerrado.
        with self.assertRaisesRegex(ValueError, "ya está cerrado"):
            list(rec.messages())
        with self.assertRaisesRegex(ValueError, "ya está cerrado"):
            _ = rec.attachments
        del m
        self.assertEqual(close_quoting(rec), "", "ya soltó la vista: cierra sin avisar")
        self.assertClosed(rec._r)

    def test_al_soltar_la_vista_se_suelta_sola(self):
        r = _mcap.Reader(self.path)
        m = next(iter(r.messages()))
        self.assertIn(self.path, close_quoting(r))
        del m
        gc.collect()
        self.assertFalse(r._mapped, "sin otro close(): en cuanto nada la usa, se suelta")
        self.assertEqual(close_quoting(r), "")
        self.assertClosed(r)

    def test_as_array_guardado(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("sin numpy")
        rec = agmen.Recording(self.path)
        arr = [s.message.as_array() for s in rec.messages("/camara")][-1]
        self.assertWarnedOnce(close_quoting(rec))
        self.assertEqual(arr.tolist(), [[9, 10, 11, 12], [13, 14, 15, 16]], "el arreglo sigue sirviendo")
        copia = arr.copy()
        del arr
        self.assertEqual(close_quoting(rec), "")
        self.assertClosed(rec._r)
        self.assertEqual(copia.tolist(), [[9, 10, 11, 12], [13, 14, 15, 16]], "la copia no depende del archivo")

    def test_deepcopy_no_apunta_al_archivo(self):
        rec = agmen.Recording(self.path)
        cams = [copy.deepcopy(s.message) for s in rec.messages("/camara")]      # Imagen
        dets = [copy.deepcopy(s.message) for s in rec.messages("/det")]         # armado con el esquema grabado
        ests = [copy.deepcopy(s) for s in rec.messages("/robot/estado")]        # el Sample completo
        self.assertEqual(close_quoting(rec), "", "las copias no detienen el archivo")
        self.assertClosed(rec._r)
        self.assertIsInstance(cams[-1], Imagen)
        self.assertEqual(bytes(cams[-1].data), bytes(range(9, 17)))
        self.assertEqual(type(dets[1]).__name__, "Deteccion")
        self.assertEqual((dets[1].clase, bytes(dets[1].crudo)), ("gato3", b"\x03\x03\x03"))
        mapa = dets[1].mapa                   # numpy.ndarray, o agmen.Tensor sin NumPy
        self.assertEqual(tuple(mapa.shape), (2, 3))
        if hasattr(mapa, "reshape"):
            self.assertEqual(mapa.reshape(-1).tolist(), [3, 1, 2, 3, 4, 5])
        else:
            self.assertNotIsInstance(mapa.data, memoryview, "la copia no es una vista del archivo")
            self.assertEqual(list(mapa.data), [3, 1, 2, 3, 4, 5])
        self.assertEqual((ests[5].message.seq, ests[5].topic), (1005, "/robot/estado"))


@unittest.skipIf(pa is None, "sin pyarrow")
class TestTablas(RecordingCase):
    def test_conservar_una_tabla_despues_de_cerrar(self):
        """Una tabla de un topic en arrow lee los bytes del archivo sin copiar: guardarla detiene el archivo
        (el aviso lo dice), y lo que el aviso recomienda, tabla.take(...), la despega de verdad."""
        try:
            import numpy as np
        except ImportError:
            self.skipTest("sin numpy")
        for filas in (None, 0.001):           # varias filas (varios lotes) y una sola
            with self.subTest(filas=filas):
                rec = agmen.Recording(self.path)
                tabla = rec.table("/det", end=filas)
                text = close_quoting(rec)
                self.assertIn("tabla.take(numpy.arange(tabla.num_rows))", text)
                rec = agmen.Recording(self.path)
                tabla = rec.table("/det", end=filas)
                copia = tabla.take(np.arange(tabla.num_rows))
                del tabla
                self.assertEqual(close_quoting(rec), "", "la copia ya no apunta al archivo")
                self.assertFalse(rec._r._mapped)
                self.assertEqual(copia.column("clase").to_pylist()[0], "gato0", "la copia sigue sirviendo")

    def test_imu(self):
        t = self.rec.table("/imu")
        self.assertEqual(t.column_names, ["t_data", "t_pub", "log_time", "source_id", "seq",
                                          "ax", "ay", "az", "gx", "gy", "gz"])
        self.assertEqual([t.schema.field(c).type for c in ("t_data", "t_pub", "log_time", "source_id", "seq", "ax")],
                         [pa.timestamp("ns")] * 3 + [pa.uint64(), pa.uint64(), pa.float32()])
        self.assertEqual(t.num_rows, N + 1, "el paquete de dos filas da dos filas")
        self.assertEqual(t.column("ax").to_pylist(), [float(i) for i in range(N)] + [99.0])
        self.assertEqual(t.column("seq").to_pylist()[-2:], [N - 1, N - 1])
        self.assertEqual(t.column("t_data").cast(pa.int64()).to_pylist()[0], T0 - 2_000_000)
        self.assertEqual(t.column("log_time").cast(pa.int64()).to_pylist()[0], T0)
        self.assertEqual(t.schema.metadata[b"agmen:schema"], wire.ImuSample.SCHEMA_TEXT.encode())

    def test_tipos_anidados(self):
        t = self.rec.table("/robot/estado")
        self.assertIn("_seq", t.column_names, "el campo 'seq' es del mensaje; el del sobre lleva '_'")
        f = t.schema.field
        self.assertEqual(f("t").type, pa.timestamp("ns"))
        self.assertEqual(f("dt").type, pa.duration("ns"))
        self.assertEqual(f("flags").type, pa.list_(pa.int8(), 3))
        self.assertEqual(f("ok").type, pa.bool_())
        self.assertEqual(f("bits").type, pa.list_(pa.bool_(), 2))
        self.assertTrue(pa.types.is_struct(f("sub").type))
        self.assertIsInstance(f("m").type, pa.FixedShapeTensorType)
        self.assertEqual(f("m").type.shape, [2, 3])
        # Sin pandas, pyarrow no pasa un timestamp/duration en ns a datetime: esos se revisan como enteros.
        row = t.drop_columns(["t_data", "t_pub", "log_time", "t", "dt"]).slice(5, 1).to_pylist()[0]
        self.assertEqual((t.column("t").cast(pa.int64())[5].as_py(), t.column("dt").cast(pa.int64())[5].as_py()),
                         (T0 + 5, -5))
        self.assertEqual((row["x"], row["flags"], row["ok"], row["sub"]),
                         (2.5, [5, -5, 1], False, {"a": 105, "b": False}))
        self.assertEqual(row["ps"], [{"k": 5, "v": 0.5}, {"k": 2, "v": 5.0}])
        self.assertEqual((row["seq"], row["_seq"]), (1005, 5))
        self.assertEqual(t.column("m").chunk(0).to_numpy_ndarray()[5].tolist(), [[5, 1, 2], [3, 4, 5]])

    def test_arrow(self):
        t = self.rec.table("/camara")
        self.assertEqual(t.column_names[5:], ["timestamp", "frame_id", "width", "height", "encoding", "step", "data"])
        self.assertEqual(t.schema.field("data").type, pa.binary())
        self.assertEqual(t.column("data").to_pylist(), [bytes(range(i, i + 8)) for i in (0, 3, 6, 9)])
        d = self.rec.table("/det")
        self.assertEqual(d.column("puntos").to_pylist()[1], [0.0, 1.0, 2.0])
        self.assertEqual(d.column("clase").to_pylist(), ["gato0", "gato3", "gato6", "gato9"])
        self.assertEqual(d.num_rows, 4)

    def test_sin_numpy_da_lo_mismo(self):
        with_np = self.rec.table("/robot/estado")
        old = recording._numpy
        recording._numpy = lambda: None
        try:
            without = self.rec.table("/robot/estado")
        finally:
            recording._numpy = old
        self.assertTrue(with_np.equals(without), "NumPy solo lo hace más rápido")

    def test_por_partes(self):
        parts = list(self.rec._tables("/cmd", rows=5))
        self.assertGreater(len(parts), 1)
        self.assertTrue(pa.concat_tables(parts).equals(self.rec.table("/cmd")))
        empty = self.rec.table("/cmd", start=100)
        self.assertEqual((empty.num_rows, empty.schema), (0, self.rec.table("/cmd").schema))

    def test_parquet(self):
        out = os.path.join(self.dir, "vuelta")
        paths = self.rec.to_parquet(out)
        self.assertEqual(sorted(os.path.relpath(p, out) for p in paths),
                         sorted(["camara.parquet", "cmd.parquet", "det.parquet", "imu.parquet",
                                 os.path.join("robot", "estado.parquet")]))
        for topic in self.rec.topics:
            p = os.path.join(out, *topic.strip("/").split("/")) + ".parquet"
            back = pq.read_table(p)
            self.assertTrue(back.equals(self.rec.table(topic)), topic)
            self.assertEqual(back.schema.metadata[b"agmen:topic"], topic.encode())


@unittest.skipIf(make_reader is None, "sin la biblioteca oficial de mcap")
class TestFoxglove(RecordingCase):
    def test_json_que_lee_la_biblioteca_oficial(self):
        out = os.path.join(self.dir, "fox.mcap")
        n = self.rec.to_foxglove(out)
        self.assertEqual(n, 3 * N + 2 * 4 + 1)
        with open(out, "rb") as f:
            r = make_reader(f)
            header = r.get_header()
            self.assertEqual(header.profile, "")
            seen: dict[str, list] = {}
            for sch, ch, msg in r.iter_messages():
                self.assertEqual((ch.message_encoding, sch.encoding), ("json", "jsonschema"))
                seen.setdefault(ch.topic, []).append((sch, json.loads(msg.data)))
            meta = {m.name: m.metadata for m in r.iter_metadata()}
            atts = [a.name for a in r.iter_attachments()]
        self.assertEqual(sum(len(v) for v in seen.values()), n)
        sch, cam = seen["/camara"][1]
        self.assertEqual(sch.name, "foxglove.RawImage", "Foxglove la muestra en su panel Image")
        self.assertEqual(cam["timestamp"], {"sec": (T0 + 3) // 10**9, "nsec": (T0 + 3) % 10**9})
        self.assertEqual(base64.b64decode(cam["data"]), bytes(range(3, 11)))
        self.assertIn("timestamp", json.loads(sch.data)["properties"])
        sch, imu = seen["/imu"][4]
        self.assertEqual((sch.name, imu["ax"], imu["gz"]), ("ImuSample", 4.0, -4.0))
        self.assertEqual(seen["/robot/estado"][2][1]["sub"], {"a": 102, "b": False})
        self.assertEqual(meta["agmen.grabacion"]["robot"], "prueba")
        self.assertEqual(atts, ["robot.toml"])
        with self.assertRaisesRegex(ValueError, "la misma grabación"):
            self.rec.to_foxglove(self.path)


class TestComando(RecordingCase):
    def agmen(self, *args):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8"}
        return subprocess.run([sys.executable, "-m", "agmen", *args], env=env, capture_output=True, text=True,
                              encoding="utf-8", timeout=60, check=False)

    @unittest.skipIf(pa is None, "sin pyarrow")
    def test_parquet(self):
        out = os.path.join(self.dir, "pq")
        r = self.agmen("export", self.path, "--parquet", out, "/imu", "/robot/estado")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"imu.parquet: {N + 1} filas (ImuSample)", r.stdout)
        self.assertIn(f"estado.parquet: {N} filas (Estado)", r.stdout)
        self.assertEqual(sorted(os.listdir(out)), ["imu.parquet", "robot"])

    def test_foxglove(self):
        out = os.path.join(self.dir, "fox.mcap")
        r = self.agmen("export", self.path, "--foxglove", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("mensajes en JSON", r.stdout)
        self.assertTrue(os.path.getsize(out) > 0)

    def test_errores(self):
        r = self.agmen("export", os.path.join(self.dir, "nada.mcap"), "--foxglove", "x.mcap")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no existe", r.stderr)
        r = self.agmen("export", self.path, "/nada", "--foxglove", os.path.join(self.dir, "x.mcap"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("no tiene el topic /nada", r.stderr)
        r = self.agmen("export", self.path)
        self.assertEqual(r.returncode, 2, "falta --parquet o --foxglove")


class TestConElGrabador(unittest.TestCase):
    """De punta a punta: lo que escribe agmen record, Recording lo lee igual que lo publicado."""

    def test_publicar_grabar_leer(self):
        from agmen import record

        hub = hubmod.Hub("127.0.0.1:0", log=lambda t: None)
        old = os.environ.get("AGMEN_HUB")
        os.environ["AGMEN_HUB"] = f"127.0.0.1:{hub.sock.getsockname()[1]}"
        stop = threading.Event()
        threading.Thread(target=hub.serve, args=(stop,), daemon=True).start()
        try:
            path = os.path.join(tempfile.mkdtemp(), "e2e.mcap")
            rec = record.Recorder(path, ["*"], quiet=True).start()
            pub = agmen.Publisher(Cmd, "/cmd")
            cam = agmen.Publisher(Imagen, "/camara")
            end = time.monotonic() + 3
            while (pub.subscribers < 1 or cam.subscribers < 1) and time.monotonic() < end:
                time.sleep(0.02)
            for i in range(20):
                pub.publish(Cmd(i, -i))
                if i % 5 == 0:
                    cam.publish(imagen(i))
                time.sleep(0.005)
            time.sleep(0.3)
            rec.stop()
            pub.close()
            cam.close()
            with agmen.Recording(path) as r:
                self.assertEqual([s.message.left for s in r.messages("/cmd")], [float(i) for i in range(20)])
                self.assertEqual([bytes(s.message.data) for s in r.messages("/camara")],
                                 [bytes(range(i, i + 8)) for i in (0, 5, 10, 15)])
                self.assertEqual(r.info["version"], agmen.__version__)
                if pa is not None:
                    self.assertEqual(r.table("/cmd").column("right").to_pylist(), [float(-i) for i in range(20)])
        finally:
            stop.set()
            if old is None:
                os.environ.pop("AGMEN_HUB", None)
            else:
                os.environ["AGMEN_HUB"] = old


if __name__ == "__main__":
    unittest.main()

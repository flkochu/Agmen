"""agmen.Recording y agmen export (RFC 0010 D7 y D8): leer una grabación en Python.

Corre con los dos núcleos. Se arma una grabación con valores conocidos (tipos
que el programa conoce y otros que no, compact y arrow, un paquete con dos
filas) y se revisa: los mensajes, las tablas (pyarrow es el juez de los
tipos), el Parquet y el MCAP en JSON para Foxglove (la biblioteca oficial de
MCAP es el juez). Sin pyarrow o sin `mcap`, esas pruebas se saltan.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

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
        self.rec.close()
        self.assertEqual(bytes(cams[-1].message.data), bytes(range(9, 17)), "lo leído sigue sirviendo")

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


@unittest.skipIf(pa is None, "sin pyarrow")
class TestTablas(RecordingCase):
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
        row = t.slice(5, 1).to_pylist()[0]
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

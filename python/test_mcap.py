"""agmen._mcap: el lector y el escritor de MCAP de Agmen (RFC 0010).

El juez es la biblioteca oficial (`pip install mcap`, que el CI instala): lo que
escribe Agmen lo lee ella, y lo que escribe ella lo lee Agmen, con y sin
compresión. Además: un archivo cortado en cualquier punto (el grabador se cayó)
se lee hasta el último chunk completo, `repair` le devuelve el índice, y un
archivo dañado solo lanza McapError.
"""

from __future__ import annotations

import collections
import os
import random
import tempfile
import unittest

from agmen import _mcap

try:
    from mcap.reader import make_reader
    from mcap.writer import CompressionType
    from mcap.writer import Writer as OfficialWriter
except ImportError:   # pragma: no cover - el CI la instala
    make_reader = None

COMPRESSIONS = [None] + [c for c in ("zstd", "lz4") if c in _mcap._compressors()]


def read(path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def sample(path, compression=None, chunk_size=50_000):
    """Escribe una grabación de prueba: una IMU a menudo y una imagen de vez en
    cuando, con metadatos y un adjunto. Devuelve lo que se mandó."""
    rng = random.Random(7)
    w = _mcap.Writer(path, profile="agmen", library="agmen prueba", chunk_size=chunk_size, compression=compression)
    s1 = w.add_schema("Imu", "agmen", b"agmen/1 Imu{f32 x}")
    s2 = w.add_schema("Img", "agmen", b"agmen/1 Img{bytes data}")
    c1 = w.add_channel("/imu", "agmen", s1, {"schema_id": "1"})
    c2 = w.add_channel("/img", "agmen", s2)
    sent = []
    t = 1_000
    for i in range(400):
        t += rng.randint(1, 1000)
        if i % 10 == 0:
            data = bytes([i % 256]) * 5000
            w.add_message(c2, t, t - 5, i, data[:64], memoryview(data)[64:])   # en dos pedazos
            sent.append(("/img", t, i, data))
        else:
            data = i.to_bytes(4, "little") * 3
            w.add_message(c1, t, t - 1, i, data)
            sent.append(("/imu", t, i, data))
    w.add_metadata("agmen.grabacion", {"version": "prueba", "host": "aquí"})
    w.add_attachment("robot.toml", "text/plain", b"[robot]\nname = 'x'\n", log_time=t)
    w.close()
    return sent


class TestIdaYVuelta(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_agmen_lee_lo_que_escribe(self):
        for comp in COMPRESSIONS:
            with self.subTest(compresion=comp):
                p = os.path.join(self.dir, f"a-{comp}.mcap")
                sent = sample(p, comp)
                with _mcap.Reader(p) as r:
                    self.assertTrue(r.complete)
                    self.assertEqual(r.profile, "agmen")
                    got = [(m.channel.topic, m.log_time, m.sequence, bytes(m.data)) for m in r.messages()]
                    self.assertEqual(got, sent)
                    self.assertEqual([bytes(m.data) for m in r.messages("/img")],
                                     [x[3] for x in sent if x[0] == "/img"])
                    t1 = sent[100][1]
                    self.assertEqual([m.sequence for m in r.messages(start=t1, end=t1 + 1)], [sent[100][2]])
                    self.assertEqual(r.metadata, [("agmen.grabacion", {"version": "prueba", "host": "aquí"})])
                    self.assertEqual(bytes(r.attachments[0].data), b"[robot]\nname = 'x'\n")
                    self.assertEqual(r.statistics["message_count"], 400)

    @unittest.skipIf(make_reader is None, "sin la biblioteca mcap oficial")
    def test_la_biblioteca_oficial_lee_lo_de_agmen(self):
        for comp in COMPRESSIONS:
            with self.subTest(compresion=comp):
                p = os.path.join(self.dir, f"o-{comp}.mcap")
                sent = sample(p, comp)
                with open(p, "rb") as f:
                    r = make_reader(f, validate_crcs=True)
                    got = [(ch.topic, m.log_time, m.sequence, m.data)
                           for _, ch, m in r.iter_messages(log_time_order=True)]
                    self.assertEqual(got, sent)
                    summary = r.get_summary()
                    self.assertEqual(summary.statistics.message_count, 400)
                    self.assertGreater(len(summary.chunk_indexes), 1)
                    self.assertEqual(r.get_header().profile, "agmen")
                    self.assertEqual([m.name for m in r.iter_metadata()], ["agmen.grabacion"])
                    self.assertEqual([a.name for a in r.iter_attachments()], ["robot.toml"])

    @unittest.skipIf(make_reader is None, "sin la biblioteca mcap oficial")
    def test_agmen_lee_lo_de_la_biblioteca_oficial(self):
        kinds = [CompressionType.NONE] + [k for k, name in ((CompressionType.ZSTD, "zstd"), (CompressionType.LZ4, "lz4"))
                                          if name in COMPRESSIONS]
        for comp in kinds:
            with self.subTest(compresion=comp.name):
                p = os.path.join(self.dir, f"of-{comp.name}.mcap")
                with open(p, "wb") as f:
                    w = OfficialWriter(f, chunk_size=20_000, compression=comp)
                    w.start(profile="x", library="oficial")
                    sid = w.register_schema("S", "jsonschema", b"{}")
                    a = w.register_channel("/a", "json", sid)
                    b = w.register_channel("/b", "json", sid)
                    sent = []
                    for i in range(300):
                        data = (b'{"i": %d}' % i) * (1 + i % 50)
                        w.add_message(a if i % 3 else b, log_time=10 * i, data=data, publish_time=10 * i, sequence=i)
                        sent.append(("/a" if i % 3 else "/b", 10 * i, data))
                    w.add_metadata("m", {"k": "v"})
                    w.finish()
                with _mcap.Reader(p) as r:
                    self.assertTrue(r.complete)
                    self.assertEqual([(m.channel.topic, m.log_time, bytes(m.data)) for m in r.messages()], sent)
                    self.assertEqual(r.metadata, [("m", {"k": "v"})])


class TestArchivoCortado(unittest.TestCase):
    """Si el grabador se cae, se lee hasta el último chunk completo (D9)."""

    def test_cortado_en_cualquier_punto(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.mcap")
        w = _mcap.Writer(p, chunk_size=1500)
        cid = w.add_channel("/s", "agmen", w.add_schema("S", "agmen", b"agmen/1 S{u8 x}"))
        for i in range(60):
            w.add_message(cid, 1000 + i, 1000 + i, i, bytes([i]) * 100)
        w.close()
        full = read(p)
        best = 0
        for cut in range(len(full)):
            try:
                r = _mcap.Reader(full[:cut])
            except _mcap.McapError:
                self.assertLess(cut, 40, "solo si falta el encabezado")
                continue
            got = [m.sequence for m in r.messages()]
            self.assertEqual(got, list(range(len(got))), f"corte en {cut}: en orden y sin huecos")
            self.assertGreaterEqual(len(got), best - 60)
            best = max(best, len(got))
            if cut < len(full):
                self.assertFalse(r.complete)
        self.assertEqual(best, 60)

    def test_reparar(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.mcap")
        sent = sample(p, chunk_size=5000)
        full = read(p)
        cut = os.path.join(d, "cortado.mcap")
        with open(cut, "wb") as f:
            f.write(full[: len(full) * 2 // 3])
        with _mcap.Reader(cut) as r:
            self.assertFalse(r.complete)
            salvados = sum(1 for _ in r.messages())
        fixed = os.path.join(d, "reparado.mcap")
        info = _mcap.repair(cut, fixed)
        self.assertEqual(info["messages"], salvados)
        self.assertGreater(salvados, 100)
        with _mcap.Reader(fixed) as r:
            self.assertTrue(r.complete)
            self.assertEqual([bytes(m.data) for m in r.messages()], [x[3] for x in sent[:salvados]])
        if make_reader is not None:
            with open(fixed, "rb") as f:
                self.assertEqual(sum(1 for _ in make_reader(f, validate_crcs=True).iter_messages()), salvados)


class TestArchivoDanado(unittest.TestCase):
    def test_solo_mcaperror(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.mcap")
        for comp in COMPRESSIONS:
            with self.subTest(compresion=comp):
                sample(p, comp, chunk_size=3000)
                full = read(p)
                rng = random.Random(3)
                kinds = collections.Counter()
                for _ in range(800):
                    b = bytearray(full)
                    for _ in range(rng.randint(1, 6)):
                        b[rng.randrange(len(b))] = rng.randrange(256)
                    try:
                        r = _mcap.Reader(bytes(b))
                        for _ in r.messages():
                            pass
                        kinds["ok"] += 1
                    except _mcap.McapError:
                        kinds["McapError"] += 1
                self.assertEqual(set(kinds) - {"ok", "McapError"}, set())

    def test_no_es_mcap(self):
        for data in (b"", b"hola", b"\x89MCAP0\r\n", b"\x89MCAP0\r\n" + b"\x01" + b"\xff" * 8):
            with self.assertRaises(_mcap.McapError):
                _mcap.Reader(data)

    def test_compresion_que_falta(self):
        if "zstd" in COMPRESSIONS:
            self.skipTest("zstandard está instalado")
        with self.assertRaisesRegex(_mcap.McapError, "pip install agmen"):
            _mcap.Writer(os.path.join(tempfile.mkdtemp(), "x.mcap"), compression="zstd")



class TestVector(unittest.TestCase):
    """tests/vectors/grabacion.mcap (RFC 0010 §6.6): la leen igual el lector de Agmen, la
    biblioteca oficial y el de JavaScript (test_panel_js), y el escritor la escribe igual."""

    VECTOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests", "vectors", "grabacion.mcap")

    def test_el_escritor_escribe_los_mismos_bytes(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "generar_mcap", os.path.join(os.path.dirname(self.VECTOR), "generar_mcap.py"))
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        out = os.path.join(tempfile.mkdtemp(), "otra.mcap")
        from pathlib import Path

        gen.build(Path(out))
        self.assertEqual(read(out), read(self.VECTOR), "si cambió el formato a propósito: python tests/vectors/generar_mcap.py")

    def test_lo_mismo_que_la_biblioteca_oficial(self):
        with _mcap.Reader(self.VECTOR) as r:
            ours = [(m.channel.topic, m.log_time, m.publish_time, m.sequence, bytes(m.data)) for m in r.messages()]
            self.assertTrue(r.complete)
            self.assertEqual(len(r.chunks), 4)
            self.assertEqual(dict(r.metadata)["agmen.perdidos"], {"/imu antes de llegar": "1", "total": "1"})
        self.assertEqual(len(ours), 15)
        if make_reader is None:
            self.skipTest("sin la biblioteca oficial de mcap")
        with open(self.VECTOR, "rb") as f:
            theirs = [(c.topic, m.log_time, m.publish_time, m.sequence, m.data)
                      for _, c, m in make_reader(f).iter_messages(log_time_order=True)]
        self.assertEqual(ours, theirs)


if __name__ == "__main__":
    unittest.main()

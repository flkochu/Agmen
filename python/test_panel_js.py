"""El JavaScript del panel.

1. Debe ser válido: un error de sintaxis deja la página en "conectando…" sin
   decir nada.
2. `agmen.js` (el formato v1 en JavaScript, la tercera implementación junto a
   C++ y Python) debe dar EXACTAMENTE lo mismo que Python con los vectores de
   prueba compartidos (tests/vectors/v1.txt): texto canónico, schema_id,
   tamaño, sobre, anuncios y valores decodificados.

Usa Node si está instalado; si no, se salta (en el CI de GitHub Node siempre
está).

    python -m unittest discover -s python
"""

import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from agmen import schema, wire  # noqa: E402

PANEL = HERE / "agmen" / "panel"
INDEX = PANEL / "index.html"
AGMEN_JS = PANEL / "agmen.js"
MCAP_JS = PANEL / "mcap.js"
MCAP_VECTOR = HERE.parent / "tests" / "vectors" / "grabacion.mcap"
VECTORS = HERE.parent / "tests" / "vectors" / "v1.txt"
SAFE = 2**53 - 1

NODE_SCRIPT = r"""
const A = require(process.argv[2]);
const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
const big = (k, v) => typeof v === "bigint" ? (v >= -9007199254740991n && v <= 9007199254740991n ? Number(v) : v.toString()) : v;
const hex = (s) => Uint8Array.from(s.match(/../g) || [], (h) => parseInt(h, 16));
const tohex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
const norm = (v) => {
  if (v instanceof Uint8Array) return { $bytes: tohex(v) };
  if (A.isTensor(v)) return { shape: v.shape, data: Array.from(v.data) };
  if (ArrayBuffer.isView(v)) return Array.from(v);
  if (Array.isArray(v)) return v.map(norm);
  if (v !== null && typeof v === "object") { const o = {}; for (const k of Object.keys(v)) o[k] = norm(v[k]); return o; }
  return v;
};
const out = cases.map((c) => {
  try {
    if (c.kind === "arrow") {
      const L = new A.Layout(c.input);
      return { text: L.text, id: A.hex64(L.schemaId), encoding: L.encoding, rows: norm(L.decodeRows(hex(c.payload))) };
    }
    if (c.kind === "schema") {
      const L = new A.Layout(c.input);
      return { text: L.text, id: A.hex64(L.schemaId), size: L.size, rows: L.decodeRows(hex(c.payload)) };
    }
    if (c.kind === "message") {
      const env = A.parseEnvelope(hex(c.packet));
      const L = new A.Layout(c.text);
      return { schema: A.hex64(env.schemaId), topic: A.hex64(env.topicId), source: A.hex64(env.sourceId),
               seq: env.seq, t_data: env.tData, t_pub: env.tPub, rows: norm(L.decodeRows(env.payload)) };
    }
    if (c.kind === "announcement") {
      const d = new A.Decoder();
      const m = d.feed(hex(c.packet));
      return { kind: m.kind, topic: m.topic, text: m.layout.text, source: A.hex64(m.env.sourceId) };
    }
    if (c.kind === "fnv") return { hash: A.hex64(A.fnv1a64(c.input)) };
    if (c.kind === "bad") { try { new A.Layout(c.input); return { ok: true }; } catch (e) { return { error: e.message }; } }
  } catch (e) { return { error: String(e && e.stack || e) }; }
});
process.stdout.write(JSON.stringify(out, big));
"""


def _norm(v):
    """Como lo devuelve el script de Node: enteros de 64 bits fuera del rango exacto de JS, como texto;
    bytes como {"$bytes": hex}; tensores como {"shape", "data"}."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"$bytes": bytes(v).hex()}
    if hasattr(v, "shape") and hasattr(v, "tolist"):
        return {"shape": list(v.shape), "data": _norm(v.reshape(-1).tolist())}
    if isinstance(v, int):
        return v if -SAFE <= v <= SAFE else str(v)
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    return v


def _cases():
    cases, expected = [], []
    for line in VECTORS.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        kind = cols[0]
        if kind == "schema":
            _, entrada, canon, sid, size, _valores, payload = cols
            lay = schema.Layout(canon)
            cases.append({"kind": "schema", "input": entrada, "payload": payload})
            expected.append({"text": canon, "id": sid, "size": int(size),
                             "rows": _norm(lay.unpack_rows(bytes.fromhex(payload)))})
        elif kind == "message":
            _, canon, topic, src, seq, t_data, t_pub, payload, packet = cols
            env, body = wire.parse(bytes.fromhex(packet))
            cases.append({"kind": "message", "text": canon, "packet": packet})
            expected.append({"schema": f"0x{schema.schema_id(canon):016x}", "topic": f"0x{wire.topic_id(topic):016x}",
                             "source": f"0x{int(src, 16):016x}", "seq": _norm(int(seq)), "t_data": _norm(int(t_data)),
                             "t_pub": _norm(int(t_pub)), "rows": _norm(schema.Layout(canon).unpack_rows(body))})
        elif kind == "arrow":
            _, entrada, canon, sid, _valores, payload = cols
            cases.append({"kind": "arrow", "input": entrada, "payload": payload})
            expected.append({"text": canon, "id": sid, "encoding": 2,
                             "rows": _norm(schema.Layout(canon).unpack_rows(bytes.fromhex(payload)))})
        elif kind == "announcement":
            _, canon, topic, src, _seq, _t, packet = cols
            cases.append({"kind": "announcement", "packet": packet})
            expected.append({"kind": "announcement", "topic": topic, "text": canon, "source": f"0x{int(src, 16):016x}"})
        elif kind == "fnv1a64":
            _, texto, h = cols
            cases.append({"kind": "fnv", "input": texto})
            expected.append({"hash": h})
    return cases, expected


@unittest.skipIf(shutil.which("node") is None, "Node no está instalado")
class TestPanelJs(unittest.TestCase):
    def _node(self, *args, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(["node", *args], input=stdin, capture_output=True, text=True, encoding="utf-8")

    def test_sintaxis(self):
        scripts = re.findall(r"<script>(.*?)</script>", INDEX.read_text(encoding="utf-8"), re.S)
        self.assertTrue(scripts, "index.html no tiene <script>")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "panel.js"
            path.write_text("\n".join(scripts), encoding="utf-8")
            r = self._node("--check", str(path))
            self.assertEqual(r.returncode, 0, r.stderr)
        for js in (AGMEN_JS, MCAP_JS):
            r = self._node("--check", str(js))
            self.assertEqual(r.returncode, 0, r.stderr)

    def _run(self, cases):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "vectores.js"
            script.write_text(NODE_SCRIPT, encoding="utf-8")
            r = self._node(str(script), str(AGMEN_JS), stdin=json.dumps(cases))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_vectores_iguales_a_python(self):
        cases, expected = _cases()
        self.assertGreater(len(cases), 15, "no se leyeron los vectores")
        got = self._run(cases)
        for case, exp, res in zip(cases, expected, got, strict=True):
            with self.subTest(case=case.get("input") or case.get("text") or case["kind"]):
                self.assertEqual(res, exp)

    def test_errores_claros(self):
        bad = ["Foo{float x}", "Foo{optional f32 s}", "Foo{tensor<bool,2> t}", "Foo{f32 a;f32 a}", "Foo{f32[0] a}", "Foo{}", "Foo{f32 x} extra"]
        got = self._run([{"kind": "bad", "input": b} for b in bad])
        for text, res in zip(bad, got, strict=True):
            with self.subTest(text):
                self.assertIn("error", res, f"'{text}' debió ser un error")
        self.assertIn("usa 'f32'", got[0]["error"])

    def test_regresiones_del_fuzzer(self):
        """Las entradas de tests/fuzz/regresiones/ (las que rompieron C++ o Python):
        agmen.js las acepta o lanza AgmenError, nunca otro error. Y los mismos
        límites y reglas que C++ y Python (python/test_fuzz.py)."""
        from agmen import arrow

        root = HERE.parent / "tests" / "fuzz" / "regresiones"
        cases = []
        for target in ("wire", "layout", "arrow"):
            for f in sorted((root / target).iterdir()):
                data = f.read_bytes()
                if target == "arrow":
                    nl = data.index(b"\n")
                    cases.append({"name": f"{target}/{f.name}", "kind": "arrow", "schema": data[:nl].decode(),
                                  "payload": data[nl + 1:].hex()})
                else:
                    cases.append({"name": f"{target}/{f.name}", "kind": target, "data": data.hex()})
        nested = lambda n: "A{" * n + "u8 x" + "} a" * (n - 1) + "}"   # noqa: E731
        expect = {}
        for text, ok in [(nested(32), True), (nested(33), False), ("A{u8" + "[1]" * 31 + " x}", True),
                         ("A{u8" + "[1]" * 32 + " x}", False), ("A{u8[1048576][2047] x}", True),
                         ("A{u8[1048576][2048] x}", False), ("A{u8[1048576][1500] a;u8[1048576][1500] b;string s}", True)]:
            name = f"límite {len(expect)} ({len(text)} letras): {text[:30]}"
            cases.append({"name": name, "kind": "layout", "data": text.encode().hex()})
            expect[name] = ok
        # Un tensor con forma negativa ([-1, -2] "mide" 2) y offsets negativos: datos equivocados sin error.
        nodes = [(1, 0), (1, 0), (2, 0), (1, 0), (2, 0)]
        bufs = [b"", b"", struct.pack("<2i", 0, 2), b"", b"\x01\x02", b"", b"", struct.pack("<2i", -1, -2)]
        cases.append({"name": "tensor-forma-negativa", "kind": "arrow", "schema": "A{tensor<u8,2> t}",
                      "payload": arrow.assemble(nodes, bufs, 1).hex()})
        expect["tensor-forma-negativa"] = False
        script = ("const A=require(process.argv[2]);const c=JSON.parse(require('fs').readFileSync(0,'utf8'));"
                  "const hex=(s)=>Uint8Array.from(s.match(/../g)||[],(h)=>parseInt(h,16));"
                  "process.stdout.write(JSON.stringify(c.map((k)=>{try{"
                  "if(k.kind==='wire')new A.Decoder().feed(hex(k.data));"
                  "else if(k.kind==='layout')new A.Layout(new TextDecoder().decode(hex(k.data)));"
                  "else new A.Layout(k.schema).decodeRows(hex(k.payload));return 'ok'}"
                  "catch(e){return e instanceof A.AgmenError?'error':'OTRO '+e.name+': '+e.message}})));")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.js"
            path.write_text(script, encoding="utf-8")
            r = self._node(str(path), str(AGMEN_JS), stdin=json.dumps(cases))
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        self.assertGreater(len(got), 15)
        for case, res in zip(cases, got, strict=True):
            with self.subTest(case["name"]):
                self.assertIn(res, ("ok", "error"))
                if case["name"] in expect:
                    self.assertEqual(res, "ok" if expect[case["name"]] else "error")
        offs = next(res for case, res in zip(cases, got) if case["name"] == "arrow/offsets-negativos")
        self.assertEqual(offs, "error")

    def test_paquete_invalido(self):
        # El mismo paquete que acepta Python, con un byte cambiado en cada regla del sobre.
        msg = wire.ImuSample((0.1, 0.2, 9.8), (0.0, 0.0, 0.1))
        good = bytearray(wire.encode(msg, 3, topic="/imu"))
        variants = {
            "magic": bytes(b"XGM") + bytes(good[3:]),
            "v0": bytes(good[:3]) + b"\x00" + bytes(good[4:]),
            "largo": bytes(good[:-1]),
            "flags": bytes(good[:7]) + b"\x80" + bytes(good[8:]),
        }
        script = ("const A=require(process.argv[2]);const c=JSON.parse(require('fs').readFileSync(0,'utf8'));"
                  "const hex=(s)=>Uint8Array.from(s.match(/../g),(h)=>parseInt(h,16));"
                  "process.stdout.write(JSON.stringify(c.map((p)=>{try{A.parseEnvelope(hex(p));return 'ok'}"
                  "catch(e){return e.message}})));")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.js"
            path.write_text(script, encoding="utf-8")
            r = self._node(str(path), str(AGMEN_JS), stdin=json.dumps([good.hex()] + [v.hex() for v in variants.values()]))
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        self.assertEqual(res[0], "ok")
        for name, msg_ in zip(variants, res[1:], strict=True):
            with self.subTest(name):
                self.assertNotEqual(msg_, "ok")
        self.assertIn("v0", res[2])



# Lee cada archivo con mcap.js (por partes de 4 KB, como File.slice) y con su
# decodificador agmen.js, y lo devuelve en JSON para compararlo con Python.
MCAP_SCRIPT = r"""
const M = require(process.argv[2]), A = require(process.argv[3]), fs = require("fs");
const files = JSON.parse(fs.readFileSync(0, "utf8"));
const piecewise = (b) => ({ name: "", size: b.length, reads: 0,
  read(pos, n) { this.reads++; if (n > 4 * 1024 * 1024) throw new Error("lectura enorme"); return Promise.resolve(b.subarray(pos, pos + n)); } });
(async () => {
  const out = [];
  for (const f of files) {
    try {
      const src = piecewise(fs.readFileSync(f));
      const r = await M.open(src);
      const msgs = await r.allMessages();
      const topics = {};
      for (const c of r.channels.values()) topics[c.topic] = c.messageEncoding;
      const first = msgs.find((m) => r.channels.get(m.channelId).topic === "/imu");
      let decoded = null;
      if (first) {
        const sch = r.schemas.get(r.channels.get(first.channelId).schemaId);
        const L = new A.Layout(new TextDecoder().decode(sch.data)), env = A.parseEnvelope(first.data);
        decoded = { seq: Number(env.seq), rows: L.decodeRows(env.payload) };
      }
      out.push({ complete: r.complete, truncated: r.truncated, profile: r.profile, library: r.library, topics,
                 count: r.statistics.messageCount, start: r.start.toString(), end: r.end.toString(),
                 meta: Object.fromEntries(r.metadata.map((m) => [m.name, m.values])), decoded,
                 msgs: msgs.map((m) => [r.channels.get(m.channelId).topic, m.logTime.toString(), m.publishTime.toString(),
                                         m.sequence, Buffer.from(m.data).toString("hex")]) });
    } catch (e) {
      out.push({ error: e instanceof M.McapError ? "McapError: " + e.message : e instanceof A.AgmenError ? "AgmenError: " + e.message
                        : "OTRO " + String(e && e.stack || e) });
    }
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


@unittest.skipIf(shutil.which("node") is None, "Node no está instalado")
class TestMcapJs(unittest.TestCase):
    """mcap.js (el lector del panel, RFC 0010) lee lo mismo que el de Python."""

    def _run(self, paths):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "mcap.js"
            script.write_text(MCAP_SCRIPT, encoding="utf-8")
            r = subprocess.run(["node", str(script), str(MCAP_JS), str(AGMEN_JS)], input=json.dumps([str(p) for p in paths]),
                               capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def _python(self, path):
        from agmen import _mcap

        with _mcap.Reader(str(path)) as r:
            msgs = [[m.channel.topic, str(m.log_time), str(m.publish_time), m.sequence, bytes(m.data).hex()]
                    for m in r.messages()]
            return {"complete": r.complete, "truncated": r.truncated, "msgs": msgs, "meta": dict(r.metadata)}

    def test_el_vector(self):
        got = self._run([MCAP_VECTOR])[0]
        exp = self._python(MCAP_VECTOR)
        self.assertNotIn("error", got)
        self.assertEqual(got["msgs"], exp["msgs"])
        self.assertEqual((got["complete"], got["truncated"], got["profile"], got["library"]), (True, False, "agmen", "agmen 0.12.0"))
        self.assertEqual(got["topics"], {"/imu": "agmen", "/camara": "agmen"})
        self.assertEqual(got["count"], 15)
        self.assertEqual(got["meta"], exp["meta"])
        self.assertEqual(got["decoded"]["rows"][0]["ay"], wire.ImuSample.decode_payload(
            wire.parse(bytes.fromhex(exp["msgs"][0][4]))[1]).accel[1])

    def test_comprimidas_y_cortadas(self):
        """Sin comprimir y con lz4, completas y cortadas en varios puntos: lo mismo que Python
        (hasta el último chunk completo). Nunca otro error que McapError."""
        from agmen import _mcap

        data = MCAP_VECTOR.read_bytes()
        with tempfile.TemporaryDirectory() as d:
            paths = []
            try:
                _mcap._need("lz4")
                lz4 = Path(d) / "lz4.mcap"
                w = _mcap.Writer(str(lz4), chunk_size=600, compression="lz4")
                with _mcap.Reader(str(MCAP_VECTOR)) as r:
                    ids = {}
                    for c in r.channels.values():
                        s = r.schemas[c.schema_id]
                        ids[c.id] = w.add_channel(c.topic, "agmen", w.add_schema(s.name, s.encoding, bytes(s.data)))
                    for m in r.messages():
                        w.add_message(ids[m.channel.id], m.log_time, m.publish_time, m.sequence, bytes(m.data))
                w.close()
                paths.append(lz4)
            except _mcap.McapError:
                pass                                 # sin lz4 instalado
            for cut in (len(data) // 3, len(data) // 2, len(data) * 4 // 5, len(data) - 10):
                p = Path(d) / f"cortada-{cut}.mcap"
                p.write_bytes(data[:cut])
                paths.append(p)
            got = self._run(paths)
            for p, g in zip(paths, got, strict=True):
                with self.subTest(p.name):
                    self.assertNotIn("error", g, g.get("error"))
                    exp = self._python(p)
                    self.assertEqual(g["msgs"], exp["msgs"])
                    self.assertEqual(g["complete"], exp["complete"])

    def test_danado_solo_da_mcaperror(self):
        import random

        data = MCAP_VECTOR.read_bytes()
        rng = random.Random(10)
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for k in range(60):
                bad = bytearray(data)
                for _ in range(rng.randint(1, 8)):
                    bad[rng.randrange(len(bad))] = rng.randrange(256)
                p = Path(d) / f"danado-{k}.mcap"
                p.write_bytes(bytes(bad))
                paths.append(p)
            not_mcap = Path(d) / "no.mcap"
            not_mcap.write_bytes(b"hola, no soy un mcap")
            paths.append(not_mcap)
            got = self._run(paths)
        for p, g in zip(paths, got, strict=True):
            with self.subTest(p.name):
                self.assertFalse(g.get("error", "").startswith("OTRO"), g.get("error"))
        self.assertIn("no es un archivo MCAP", got[-1]["error"])

    def test_zstd_sin_servidor_lo_dice(self):
        from agmen import _mcap

        try:
            _mcap._need("zstd")
        except _mcap.McapError:
            self.skipTest("sin zstandard")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "z.mcap"
            w = _mcap.Writer(str(p), compression="zstd")
            c = w.add_channel("/imu", "agmen", w.add_schema("ImuSample", "agmen", wire.ImuSample.SCHEMA_TEXT.encode()))
            w.add_message(c, 1, 1, 0, wire.encode(wire.ImuSample((1, 2, 3), (4, 5, 6)), 0, topic="/imu"))
            w.close()
            got = self._run([p])[0]
        self.assertIn("agmen web", got["error"])


if __name__ == "__main__":
    unittest.main()

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
        r = self._node("--check", str(AGMEN_JS))
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


if __name__ == "__main__":
    unittest.main()

// agmen.js — el formato v1 de Agmen en JavaScript (RFC 0001).
//
// Es la tercera implementación del formato, junto a C++ y Python, y pasa los
// mismos vectores de prueba (tests/vectors/v1.txt, ver python/test_panel_js.py).
// El panel la usa para decodificar en el navegador los bytes que el robot
// manda SIN convertir (RFC 0002: "el robot no dibuja").
//
//   const dec = new Agmen.Decoder();
//   const m = dec.feed(new Uint8Array(datagrama));
//   // m.kind === "announcement" → aprendió un esquema
//   // m.kind === "data"         → m.topic, m.layout.name, m.rows[0] = { ax: 0.1, ... }
//
// Sin dependencias. Funciona en el navegador (global `Agmen`) y en Node (require).
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Agmen = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const PREFIX = "agmen/1 ";
  const ENVELOPE_LEN = 64;
  const ENC = { NONE: 0, COMPACT: 1, ARROW: 2, SCHEMA: 3 };
  const FLAG_SHM = 0x01, KNOWN_FLAGS = 0x03;
  const PRIMS = {
    bool: 1, i8: 1, u8: 1, i16: 2, u16: 2, i32: 4, u32: 4, i64: 8, u64: 8,
    f32: 4, f64: 8, time: 8, duration: 8,
  };
  const ALIASES = {
    float: "f32", float32: "f32", double: "f64", float64: "f64", int: "i32", int32: "i32",
    uint: "u32", uint32: "u32", int8: "i8", uint8: "u8", byte: "u8", char: "i8", int16: "i16",
    uint16: "u16", int64: "i64", uint64: "u64", long: "i64", boolean: "bool",
  };
  const VARIABLE = new Set(["string", "bytes", "tensor", "optional"]);
  const MAX_ARRAY = 1 << 20;

  class AgmenError extends Error {}

  // ---------------- FNV-1a 64 ----------------
  const FNV_OFFSET = 0xcbf29ce484222325n, FNV_PRIME = 0x100000001b3n, MASK64 = (1n << 64n) - 1n;
  const utf8 = new TextEncoder(), utf8dec = new TextDecoder("utf-8", { fatal: true });
  function fnv1a64(data) {
    const bytes = typeof data === "string" ? utf8.encode(data) : data;
    let h = FNV_OFFSET;
    for (const b of bytes) { h ^= BigInt(b); h = (h * FNV_PRIME) & MASK64; }
    return h;
  }
  const topicId = (topic) => fnv1a64(topic);
  const hex64 = (v) => "0x" + v.toString(16).padStart(16, "0");

  // ---------------- esquemas ----------------
  function tokenize(text) {
    const re = /\s*(?:([A-Za-z_][A-Za-z0-9_]*)|(\d+)|(\S))/y;
    const toks = [];
    let m;
    re.lastIndex = 0;
    while (re.lastIndex < text.length && (m = re.exec(text)) !== null) {
      if (m[1] !== undefined) toks.push(["id", m[1]]);
      else if (m[2] !== undefined) toks.push(["num", m[2]]);
      else if (m[3] !== undefined) toks.push(["sym", m[3]]);
      else break;
    }
    return toks;
  }

  function alignUp(n, a) { return Math.ceil(n / a) * a; }

  function makeStruct(name, fields) {
    let offset = 0, align = 1;
    const out = [];
    for (const [fname, type] of fields) {
      offset = alignUp(offset, type.align);
      out.push({ name: fname, type, offset });
      offset += type.size;
      align = Math.max(align, type.align);
    }
    return { kind: "struct", name, fields: out, size: alignUp(offset, align), align };
  }

  function canonicalOf(t) {
    if (t.kind === "prim") return t.name;
    if (t.kind === "array") {
      const dims = [];
      let base = t;
      while (base.kind === "array") { dims.push(base.n); base = base.elem; }
      return canonicalOf(base) + dims.map((d) => `[${d}]`).join("");
    }
    return t.name + "{" + t.fields.map((f) => `${canonicalOf(f.type)} ${f.name}`).join(";") + "}";
  }

  function parseSchema(text) {
    let body = text.trim();
    if (body.startsWith("agmen/")) {
      if (!body.startsWith(PREFIX.trim())) throw new AgmenError(`versión de esquema no soportada: '${body.split(/\s/)[0]}'`);
      body = body.slice(PREFIX.trim().length);
    }
    const toks = tokenize(body);
    let i = 0;
    const peek = () => toks[i];
    const fail = (msg) => { throw new AgmenError(`${msg} en '${body}'`); };
    const take = (kind, value, what) => {
      const t = toks[i];
      if (!t || t[0] !== kind || (value !== undefined && t[1] !== value)) {
        fail(`se esperaba ${what || `'${value}'`} y llegó ${t ? `'${t[1]}'` : "el final del texto"}`);
      }
      i++;
      return t;
    };
    function parseType() {
      const t = peek();
      if (!t || t[0] !== "id") fail("se esperaba un tipo (f32, u8, Vec3{...}...)");
      const nxt = toks[i + 1];
      let base;
      if (nxt && nxt[0] === "sym" && nxt[1] === "{") base = parseStruct();
      else {
        i++;
        const name = t[1];
        if (name in PRIMS) base = { kind: "prim", name, size: PRIMS[name], align: PRIMS[name] };
        else if (name in ALIASES) fail(`tipo '${name}' no existe en Agmen: usa '${ALIASES[name]}'`);
        else if (VARIABLE.has(name)) fail(`'${name}' es de tamaño variable (llega con arrow, nivel 7)`);
        else fail(`tipo desconocido '${name}'`);
      }
      const dims = [];
      while (peek() && peek()[0] === "sym" && peek()[1] === "[") {
        i++;
        const n = take("num", undefined, "el tamaño del arreglo");
        const v = parseInt(n[1], 10);
        if (v < 1 || v > MAX_ARRAY || (n[1].length > 1 && n[1][0] === "0")) fail(`tamaño de arreglo inválido '${n[1]}'`);
        take("sym", "]");
        dims.push(v);
      }
      for (let k = dims.length - 1; k >= 0; k--) {
        base = { kind: "array", elem: base, n: dims[k], size: base.size * dims[k], align: base.align };
      }
      return base;
    }
    function parseStruct() {
      const name = take("id", undefined, "el nombre del tipo")[1];
      take("sym", "{");
      const fields = [], seen = new Set();
      for (;;) {
        const t = peek();
        if (t && t[0] === "sym" && t[1] === "}") {
          if (!fields.length) fail(`'${name}' no tiene campos`);
          i++;
          break;
        }
        const type = parseType();
        const fname = take("id", undefined, "el nombre del campo")[1];
        if (seen.has(fname)) fail(`el campo '${fname}' está repetido en '${name}'`);
        seen.add(fname);
        fields.push([fname, type]);
        const s = peek();
        if (s && s[0] === "sym" && s[1] === ";") i++;
        else if (!s || s[1] !== "}") fail("se esperaba ';' o '}' después del campo");
      }
      return makeStruct(name, fields);
    }
    const root = parseStruct();
    if (i < toks.length) fail("sobra texto después del esquema");
    return root;
  }

  class Layout {
    constructor(text) {
      this.root = parseSchema(text);
      this.text = PREFIX + canonicalOf(this.root);
      this.schemaId = fnv1a64(this.text);
      this.name = this.root.name;
      this.size = this.root.size;
    }
    _read(t, view, at) {
      if (t.kind === "prim") {
        switch (t.name) {
          case "bool": return view.getUint8(at) !== 0;
          case "i8": return view.getInt8(at);
          case "u8": return view.getUint8(at);
          case "i16": return view.getInt16(at, true);
          case "u16": return view.getUint16(at, true);
          case "i32": return view.getInt32(at, true);
          case "u32": return view.getUint32(at, true);
          case "f32": return view.getFloat32(at, true);
          case "f64": return view.getFloat64(at, true);
          case "u64": return view.getBigUint64(at, true);
          default: return view.getBigInt64(at, true);   // i64, time, duration
        }
      }
      if (t.kind === "array") {
        const out = new Array(t.n);
        for (let k = 0; k < t.n; k++) out[k] = this._read(t.elem, view, at + k * t.elem.size);
        return out;
      }
      const obj = {};
      for (const f of t.fields) obj[f.name] = this._read(f.type, view, at + f.offset);
      return obj;
    }
    /** bytes compact (Uint8Array) → filas (normalmente 1). */
    decodeRows(payload) {
      if (payload.length % this.size) {
        throw new AgmenError(`payload de ${payload.length} bytes: no es múltiplo de ${this.size} (el tamaño de '${this.name}')`);
      }
      const view = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
      const rows = [];
      for (let at = 0; at < payload.length; at += this.size) rows.push(this._read(this.root, view, at));
      return rows;
    }
  }

  // ---------------- sobre ----------------
  function parseEnvelope(buf) {
    const b = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
    if (b.length < 4) throw new AgmenError(`paquete demasiado corto: ${b.length} bytes`);
    if (b[0] !== 0x41 || b[1] !== 0x47 || b[2] !== 0x4d) throw new AgmenError("magic inválido: el paquete no viene de un nodo de Agmen");
    if (b[3] === 0) throw new AgmenError("mensaje del formato v0 (Agmen 0.1–0.3): actualiza el nodo que lo manda");
    if (b[3] !== 1) throw new AgmenError(`versión de protocolo ${b[3]} no soportada (este panel habla la versión 1)`);
    if (b.length < ENVELOPE_LEN) throw new AgmenError(`paquete demasiado corto: ${b.length} bytes, se necesitan al menos 64`);
    const v = new DataView(b.buffer, b.byteOffset, b.byteLength);
    const env = {
      version: b[3], headerLen: v.getUint16(4, true), encoding: b[6], flags: b[7],
      schemaId: v.getBigUint64(8, true), topicId: v.getBigUint64(16, true), sourceId: v.getBigUint64(24, true),
      seq: v.getBigUint64(32, true), tData: v.getBigInt64(40, true), tPub: v.getBigInt64(48, true),
      payloadLen: v.getUint32(56, true), clock: b[60],
    };
    if (env.headerLen < ENVELOPE_LEN || env.headerLen > b.length) throw new AgmenError(`header_len inválido: ${env.headerLen}`);
    if (env.encoding > ENC.SCHEMA) throw new AgmenError(`encoding desconocido: ${env.encoding}`);
    if (env.flags & ~KNOWN_FLAGS) throw new AgmenError(`flags desconocidos: 0x${env.flags.toString(16)}`);
    if (env.flags & FLAG_SHM) throw new AgmenError("payload en memoria compartida: llega en el nivel 8");
    if (env.encoding === ENC.ARROW) throw new AgmenError("codificación arrow: llega en el nivel 7");
    env.payload = b.subarray(env.headerLen);
    if (env.payload.length !== env.payloadLen) {
      throw new AgmenError(`el sobre dice payload de ${env.payloadLen} bytes pero llegaron ${env.payload.length}`);
    }
    return env;
  }

  function readAnnouncement(env) {
    let text;
    try { text = utf8dec.decode(env.payload); } catch (e) { throw new AgmenError("anuncio de esquema que no es UTF-8"); }
    const nl = text.indexOf("\n");
    if (nl < 0) throw new AgmenError("anuncio de esquema mal formado (se esperaba 'topic\\nesquema')");
    const topic = text.slice(0, nl), schema = text.slice(nl + 1);
    if (fnv1a64(schema) !== env.schemaId) throw new AgmenError("anuncio inválido: el texto no coincide con su schema_id");
    if (topicId(topic) !== env.topicId) throw new AgmenError("anuncio inválido: el topic no coincide con su topic_id");
    const layout = new Layout(schema);
    if (layout.text !== schema) throw new AgmenError(`anuncio inválido: '${schema}' no está en forma canónica`);
    return { topic, text: schema, layout };
  }

  // ---------------- decodificador con memoria ----------------
  class Decoder {
    constructor() {
      this.layouts = new Map();   // schema_id (hex) → Layout
      this.topics = new Map();    // topic_id (hex) → nombre
    }
    feed(buf) {
      const env = parseEnvelope(buf);
      if (env.encoding === ENC.SCHEMA) {
        const a = readAnnouncement(env);
        this.layouts.set(hex64(env.schemaId), a.layout);
        this.topics.set(hex64(env.topicId), a.topic);
        return { kind: "announcement", env, topic: a.topic, layout: a.layout };
      }
      if (env.encoding !== ENC.COMPACT) throw new AgmenError(`encoding ${env.encoding} sin decodificador en el panel`);
      const layout = this.layouts.get(hex64(env.schemaId));
      if (!layout) throw new AgmenError(`llega schema_id ${hex64(env.schemaId)} sin anuncio de esquema todavía`);
      return { kind: "data", env, topic: this.topics.get(hex64(env.topicId)) || "", layout, rows: layout.decodeRows(env.payload) };
    }
  }

  /** {p:{x:1}, q:[1,2]} → {"p.x":1, "q[0]":1, "q[1]":2}, todo como Number. Para graficar. */
  function flatten(value, prefix = "", out = {}) {
    if (Array.isArray(value)) value.forEach((v, k) => flatten(v, `${prefix}[${k}]`, out));
    else if (value !== null && typeof value === "object") {
      for (const [k, v] of Object.entries(value)) flatten(v, prefix ? `${prefix}.${k}` : k, out);
    } else out[prefix] = typeof value === "boolean" ? (value ? 1 : 0) : Number(value);
    return out;
  }

  return { PREFIX, ENC, AgmenError, fnv1a64, topicId, hex64, parseSchema, canonical: (t) => PREFIX + canonicalOf(parseSchema(t)),
           Layout, parseEnvelope, readAnnouncement, Decoder, flatten };
});

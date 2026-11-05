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
// Desde Agmen 0.10 también lee la codificación arrow (RFC 0008): string → texto,
// bytes → Uint8Array (sin copiar), T[] → arreglo, tensor<T,N> → { shape, data }
// con data en un TypedArray.
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
  const TENSOR_ELEMS = new Set(["i8", "u8", "i16", "u16", "i32", "u32", "i64", "u64", "f32", "f64"]);
  const MAX_ARRAY = 1 << 20;
  // Los mismos límites que C++ y Python (agmen.schema.MAX_DEPTH, MAX_FIXED_SIZE).
  const MAX_DEPTH = 32;                 // niveles de structs y arreglos anidados
  const MAX_FIXED_SIZE = 2 ** 31 - 1;   // bytes de un tipo de tamaño fijo
  const MAX_NOMINAL = (1n << 63n) - 1n;   // forma de un tensor (como NumPy)
  const TOO_DEEP = "demasiados niveles: un esquema anida como mucho 32 structs y arreglos";
  const TOO_BIG = " y un tipo de tamaño fijo mide como mucho 2 GiB: para datos grandes usa bytes, T[] o tensor<T,N>";

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
    if (fields.some(([, t]) => t.size == null)) {   // tamaño variable: va en arrow
      return { kind: "struct", name, fields: fields.map(([fname, type]) => ({ name: fname, type, offset: 0 })), size: null, align: null };
    }
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
    if (t.kind === "prim" || t.kind === "var") return t.name;
    if (t.kind === "tensor") return `tensor<${t.elem.name},${t.ndim}>`;
    if (t.kind === "array" || t.kind === "list") {
      const dims = [];
      let base = t;
      while (base.kind === "array" || base.kind === "list") { dims.push(base.kind === "array" ? `[${base.n}]` : "[]"); base = base.elem; }
      return canonicalOf(base) + dims.join("");
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
    let depth = 0;   // structs abiertos: la recursión no pasa de MAX_DEPTH
    // → [tipo, altura]: un primitivo 0; cada struct y cada [N] o [] suma 1.
    function parseType() {
      const t = peek();
      if (!t || t[0] !== "id") fail("se esperaba un tipo (f32, u8, Vec3{...}...)");
      const nxt = toks[i + 1];
      let base, height = 0;
      if (nxt && nxt[0] === "sym" && nxt[1] === "{") [base, height] = parseStruct();
      else {
        i++;
        const name = t[1];
        if (name in PRIMS) base = { kind: "prim", name, size: PRIMS[name], align: PRIMS[name] };
        else if (name === "string" || name === "bytes") base = { kind: "var", name, size: null, align: null };
        else if (name === "tensor") {
          take("sym", "<");
          const e = take("id", undefined, "el tipo de los elementos")[1];
          if (!TENSOR_ELEMS.has(e)) fail(`un tensor solo puede ser de números, no de '${e}'`);
          take("sym", ",");
          const n = take("num", undefined, "el número de dimensiones");
          const nd = parseInt(n[1], 10);
          if (nd < 1 || nd > 32 || n[1][0] === "0") fail(`número de dimensiones inválido '${n[1]}' (de 1 a 32)`);
          take("sym", ">");
          base = { kind: "tensor", elem: { kind: "prim", name: e, size: PRIMS[e], align: PRIMS[e] }, ndim: nd, size: null, align: null };
        } else if (name in ALIASES) fail(`tipo '${name}' no existe en Agmen: usa '${ALIASES[name]}'`);
        else if (name === "optional") fail("'optional' todavía no existe en Agmen");
        else fail(`tipo desconocido '${name}'`);
      }
      const dims = [];
      while (peek() && peek()[0] === "sym" && peek()[1] === "[") {
        if (height + dims.length + 1 > MAX_DEPTH) fail(TOO_DEEP);
        i++;
        if (peek() && peek()[0] === "sym" && peek()[1] === "]") { i++; dims.push(null); continue; }
        const n = take("num", undefined, "el tamaño del arreglo");
        const v = parseInt(n[1], 10);
        if (v < 1 || v > MAX_ARRAY || (n[1].length > 1 && n[1][0] === "0")) fail(`tamaño de arreglo inválido '${n[1]}'`);
        take("sym", "]");
        dims.push(v);
      }
      for (let k = dims.length - 1; k >= 0; k--) {
        if (dims[k] !== null && base.size != null && base.size > MAX_FIXED_SIZE / dims[k]) fail("el arreglo mediría más de 2 GiB" + TOO_BIG);
        base = dims[k] === null ? { kind: "list", elem: base, size: null, align: null }
          : { kind: "array", elem: base, n: dims[k], size: base.size == null ? null : base.size * dims[k], align: base.align };
      }
      return [base, height + dims.length];
    }
    function parseStruct() {
      if (++depth > MAX_DEPTH) fail(TOO_DEEP);
      const name = take("id", undefined, "el nombre del tipo")[1];
      take("sym", "{");
      const fields = [], seen = new Set();
      let height = 0;
      for (;;) {
        const t = peek();
        if (t && t[0] === "sym" && t[1] === "}") {
          if (!fields.length) fail(`'${name}' no tiene campos`);
          i++;
          break;
        }
        const [type, h] = parseType();
        height = Math.max(height, h + 1);
        const fname = take("id", undefined, "el nombre del campo")[1];
        if (seen.has(fname)) fail(`el campo '${fname}' está repetido en '${name}'`);
        seen.add(fname);
        fields.push([fname, type]);
        const s = peek();
        if (s && s[0] === "sym" && s[1] === ";") i++;
        else if (!s || s[1] !== "}") fail("se esperaba ';' o '}' después del campo");
      }
      depth--;
      if (height > MAX_DEPTH) fail(TOO_DEEP);
      const st = makeStruct(name, fields);
      if (st.size != null && st.size > MAX_FIXED_SIZE) fail(`'${name}' mediría ${st.size} bytes` + TOO_BIG);
      return [st, height];
    }
    const [root] = parseStruct();
    if (i < toks.length) fail("sobra texto después del esquema");
    return root;
  }

  class Layout {
    constructor(text) {
      this.root = parseSchema(text);
      this.text = PREFIX + canonicalOf(this.root);
      this.schemaId = fnv1a64(this.text);
      this.name = this.root.name;
      this.size = this.root.size;          // null: tamaño variable (arrow)
      this.encoding = this.size == null ? ENC.ARROW : ENC.COMPACT;
      this._arrow = this.size == null ? arrowNode(this.root) : null;
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
    /** payload (Uint8Array) → filas (normalmente 1). */
    decodeRows(payload) {
      if (this._arrow) return readArrow(this, payload);
      if (payload.length % this.size) {
        throw new AgmenError(`payload de ${payload.length} bytes: no es múltiplo de ${this.size} (el tamaño de '${this.name}')`);
      }
      const view = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
      const rows = [];
      for (let at = 0; at < payload.length; at += this.size) rows.push(this._read(this.root, view, at));
      return rows;
    }
  }


  // ---------------- arrow (RFC 0008; la referencia es python/agmen/arrow.py) ----------------
  const NUM_KINDS = { i8: Int8Array, u8: Uint8Array, i16: Int16Array, u16: Uint16Array, i32: Int32Array, u32: Uint32Array,
                      i64: BigInt64Array, u64: BigUint64Array, f32: Float32Array, f64: Float64Array,
                      time: BigInt64Array, duration: BigInt64Array };

  function arrowNode(t) {
    if (t.kind === "prim") return t.name === "bool" ? { kind: "bool" } : { kind: "prim", prim: t.name, size: t.size };
    if (t.kind === "var") return { kind: t.name === "string" ? "utf8" : "binary" };
    if (t.kind === "list") return { kind: "list", child: arrowNode(t.elem) };
    if (t.kind === "tensor") return { kind: "tensor", ndim: t.ndim, child: arrowNode(t.elem) };
    if (t.kind === "array") {
      const dims = [];
      let base = t;
      while (base.kind === "array") { dims.push(base.n); base = base.elem; }
      if (base.kind === "prim" && base.name !== "bool") {
        return { kind: "fsl", dims, n: dims.reduce((a, b) => a * b, 1), child: arrowNode(base) };
      }
      return { kind: "fsl", dims: [t.n], n: t.n, child: arrowNode(t.elem) };
    }
    return { kind: "struct", names: t.fields.map((f) => f.name), children: t.fields.map((f) => arrowNode(f.type)) };
  }

  function readArrow(layout, payload) {
    const bad = (m) => { throw new AgmenError(`arrow: ${m}`); };
    if (payload.length < 8) bad(`el payload mide ${payload.length} bytes`);
    const pv = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
    if (pv.getUint32(0, true) !== 0xffffffff) bad("falta la marca de continuación 0xFFFFFFFF");
    const metaLen = pv.getInt32(4, true);
    if (metaLen <= 0 || 8 + metaLen > payload.length) bad("metadatos fuera del mensaje");
    const fb = new DataView(payload.buffer, payload.byteOffset + 8, metaLen);
    const need = (p, n) => { if (p < 0 || p + n > metaLen) bad("los metadatos apuntan fuera del mensaje"); };
    const u8 = (p) => { need(p, 1); return fb.getUint8(p); };
    const u16 = (p) => { need(p, 2); return fb.getUint16(p, true); };
    const i16 = (p) => { need(p, 2); return fb.getInt16(p, true); };
    const u32 = (p) => { need(p, 4); return fb.getUint32(p, true); };
    const i32 = (p) => { need(p, 4); return fb.getInt32(p, true); };
    const i64 = (p) => { need(p, 8); return Number(fb.getBigInt64(p, true)); };
    const table = (pos) => {
      const vt = pos - i32(pos), size = u16(vt);
      if (size < 4 || size % 2) bad("vtable inválida");
      return (k) => (4 + 2 * k + 2 > size ? 0 : u16(vt + 4 + 2 * k));
    };
    const deref = (p) => p + u32(p);
    const msg = deref(0), f = table(msg);
    const version = f(0) ? i16(msg + f(0)) : 0;
    if (version < 3) bad(`versión de metadatos ${version + 1} no soportada`);
    if ((f(1) ? u8(msg + f(1)) : 0) !== 3 || !f(2)) bad("se esperaba un RecordBatch");
    const bodyLen = f(3) ? i64(msg + f(3)) : 0;
    const rb = deref(msg + f(2)), g = table(rb);
    const rows = g(0) ? i64(rb + g(0)) : 0;
    if (g(3)) bad("RecordBatch comprimido (LZ4/ZSTD): todavía no se lee");
    const vec = (k) => {
      if (!g(k)) return [];
      const v = deref(rb + g(k)), n = u32(v);
      need(v + 4, 16 * n);
      const out = [];
      for (let j = 0; j < n; j++) out.push([i64(v + 4 + 16 * j), i64(v + 12 + 16 * j)]);
      return out;
    };
    const nodes = vec(1), bufs = vec(2);
    const bodyStart = 8 + metaLen;
    if (bodyLen < 0 || bodyStart + bodyLen > payload.length) bad("el cuerpo se sale del mensaje");
    if (rows < 0 || rows > payload.length) bad("número de filas inválido");   // como C++ y Python
    let iNode = 0, iBuf = 0;
    const node = (want) => {
      if (iNode >= nodes.length) bad("faltan columnas");
      const [len, nulls] = nodes[iNode++];
      if (nulls) bad("llegaron valores nulos");
      if (len < 0 || len > bodyLen * 8 + 64) bad("una columna con un largo inválido");   // cada valor usa al menos un bit
      if (want !== undefined && len !== want) bad(`una columna mide ${len} y debía medir ${want}`);
      return len;
    };
    const buf = (needLen = 0) => {
      if (iBuf >= bufs.length) bad("faltan buffers");
      const [off, len] = bufs[iBuf++];
      if (off < 0 || len < 0 || off + len > bodyLen) bad("un buffer se sale del cuerpo");
      if (len < needLen) bad(`un buffer mide ${len} y se necesitan ${needLen}`);
      return payload.subarray(bodyStart + off, bodyStart + off + len);
    };
    const dv = (b) => new DataView(b.buffer, b.byteOffset, b.byteLength);
    const offsets = (len) => {
      const b = dv(buf(4 * (len + 1))), out = new Array(len + 1);
      for (let j = 0; j <= len; j++) out[j] = b.getInt32(4 * j, true);
      if (out[0] < 0) bad("offsets negativos");   // subarray(-2, 2) no falla: tomaría bytes del final
      for (let j = 1; j <= len; j++) if (out[j] < out[j - 1]) bad("offsets que van hacia atrás");
      return out;
    };
    const typed = (prim, b, count) => {   // sin copiar si está alineado
      const K = NUM_KINDS[prim];
      if (b.byteOffset % K.BYTES_PER_ELEMENT === 0) return new K(b.buffer, b.byteOffset, count);
      return new K(b.slice(0, count * K.BYTES_PER_ELEMENT).buffer, 0, count);
    };
    const scalars = (prim, size, len) => {
      const b = dv(buf(len * size)), out = new Array(len);
      for (let j = 0; j < len; j++) {
        const at = j * size;
        switch (prim) {
          case "i8": out[j] = b.getInt8(at); break;
          case "u8": out[j] = b.getUint8(at); break;
          case "i16": out[j] = b.getInt16(at, true); break;
          case "u16": out[j] = b.getUint16(at, true); break;
          case "i32": out[j] = b.getInt32(at, true); break;
          case "u32": out[j] = b.getUint32(at, true); break;
          case "f32": out[j] = b.getFloat32(at, true); break;
          case "f64": out[j] = b.getFloat64(at, true); break;
          case "u64": out[j] = b.getBigUint64(at, true); break;
          default: out[j] = b.getBigInt64(at, true);
        }
      }
      return out;
    };
    const nest = (flat, dims) => {
      if (dims.length === 1) return flat;
      const step = flat.length / dims[0], out = [];
      for (let j = 0; j < dims[0]; j++) out.push(nest(flat.slice(j * step, (j + 1) * step), dims.slice(1)));
      return out;
    };
    const read = (n, len) => {
      buf();                                     // validez (sin nulos)
      switch (n.kind) {
        case "prim": return scalars(n.prim, n.size, len);
        case "bool": {
          const bits = buf(Math.ceil(len / 8)), out = new Array(len);
          for (let j = 0; j < len; j++) out[j] = ((bits[j >> 3] >> (j & 7)) & 1) === 1;
          return out;
        }
        case "utf8": case "binary": {
          const offs = offsets(len), data = buf(offs[len]), out = new Array(len);
          for (let j = 0; j < len; j++) {
            const part = data.subarray(offs[j], offs[j + 1]);
            if (n.kind === "binary") out[j] = part;
            else { try { out[j] = utf8dec.decode(part); } catch (e) { bad("un campo string no es UTF-8"); } }
          }
          return out;
        }
        case "list": {
          const offs = offsets(len), vals = read(n.child, node(offs[len])), out = new Array(len);
          for (let j = 0; j < len; j++) out[j] = vals.slice(offs[j], offs[j + 1]);
          return out;
        }
        case "fsl": {
          const vals = read(n.child, node(len * n.n)), out = new Array(len);
          for (let j = 0; j < len; j++) out[j] = nest(vals.slice(j * n.n, (j + 1) * n.n), n.dims);
          return out;
        }
        case "struct": {
          const cols = n.children.map((c) => read(c, node(len))), out = new Array(len);
          for (let j = 0; j < len; j++) {
            const o = {};
            n.names.forEach((name, k) => { o[name] = cols[k][j]; });
            out[j] = o;
          }
          return out;
        }
        case "tensor": {
          node(len); buf();                       // data: List<T>
          const offs = offsets(len), total = node(offs[len]);
          buf();
          const c = n.child, raw = buf(total * c.size);
          node(len); buf(); node(len * n.ndim); buf();
          const sh = dv(buf(4 * len * n.ndim)), out = new Array(len);
          for (let j = 0; j < len; j++) {
            const shape = [];
            for (let k = 0; k < n.ndim; k++) shape.push(sh.getInt32(4 * (j * n.ndim + k), true));
            const count = offs[j + 1] - offs[j];
            // Como C++ y Python (y NumPy): sin dimensiones negativas, y las dimensiones (un 0 cuenta como 1)
            // por el tamaño del elemento caben en 63 bits.
            const nominal = shape.reduce((a, d) => a * BigInt(d || 1), BigInt(c.size));
            if (shape.some((d) => d < 0) || nominal > MAX_NOMINAL || shape.reduce((a, b) => a * b, 1) !== count) {
              bad(`un tensor de forma [${shape}] trae ${count} datos`);
            }
            out[j] = { shape, data: typed(c.prim, raw.subarray(offs[j] * c.size, offs[j + 1] * c.size), count) };
          }
          return out;
        }
        default: bad(`tipo ${n.kind}`);
      }
      return [];
    };
    const tree = layout._arrow, cols = tree.children.map((c) => read(c, node(rows)));
    if (iNode !== nodes.length || iBuf !== bufs.length) bad(`el RecordBatch no coincide con el esquema '${layout.name}'`);
    const out = [];
    for (let j = 0; j < rows; j++) {
      const o = {};
      tree.names.forEach((name, k) => { o[name] = cols[k][j]; });
      out.push(o);
    }
    return out;
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
      if (env.encoding !== ENC.COMPACT && env.encoding !== ENC.ARROW) throw new AgmenError(`encoding ${env.encoding} sin decodificador en el panel`);
      const layout = this.layouts.get(hex64(env.schemaId));
      if (!layout) throw new AgmenError(`llega schema_id ${hex64(env.schemaId)} sin anuncio de esquema todavía`);
      return { kind: "data", env, topic: this.topics.get(hex64(env.topicId)) || "", layout, rows: layout.decodeRows(env.payload) };
    }
  }

  const FLATTEN_MAX = 64;   // un arreglo más largo no se grafica número por número
  const isTensor = (v) => v !== null && typeof v === "object" && Array.isArray(v.shape) && ArrayBuffer.isView(v.data);

  /** {p:{x:1}, q:[1,2]} → {"p.x":1, "q[0]":1, "q[1]":2}, todo como Number. Para graficar.
   *  Texto, bytes, tensores y arreglos de más de FLATTEN_MAX números se saltan. */
  function flatten(value, prefix = "", out = {}) {
    if (Array.isArray(value)) {
      if (value.length <= FLATTEN_MAX) value.forEach((v, k) => flatten(v, `${prefix}[${k}]`, out));
    } else if (ArrayBuffer.isView(value) || isTensor(value) || typeof value === "string") {
      // no se grafica
    } else if (value !== null && typeof value === "object") {
      for (const [k, v] of Object.entries(value)) flatten(v, prefix ? `${prefix}.${k}` : k, out);
    } else out[prefix] = typeof value === "boolean" ? (value ? 1 : 0) : Number(value);
    return out;
  }

  /** Un valor para mostrarlo (inspector, echo): los datos grandes se resumen. */
  function summarize(value) {
    if (value instanceof Uint8Array) return `<${value.length.toLocaleString("es")} bytes>`;
    if (isTensor(value)) return `tensor [${value.shape.join(", ")}]`;
    if (Array.isArray(value)) {
      if (value.length > FLATTEN_MAX && value.every((v) => typeof v === "number")) return `[${value.length.toLocaleString("es")} números]`;
      return value.map(summarize);
    }
    if (value !== null && typeof value === "object") {
      const o = {};
      for (const [k, v] of Object.entries(value)) o[k] = summarize(v);
      return o;
    }
    return value;
  }

  return { PREFIX, ENC, AgmenError, fnv1a64, topicId, hex64, parseSchema, canonical: (t) => PREFIX + canonicalOf(parseSchema(t)),
           Layout, parseEnvelope, readAnnouncement, Decoder, flatten, summarize, isTensor };
});

// mcap.js — leer grabaciones de Agmen (.mcap) en el navegador (RFC 0010 §3.6).
//
// El panel abre un .mcap (arrastrándolo, o con `agmen web vuelta1.mcap`) y lo
// lee POR PARTES con el índice de MCAP: aunque mida varios GB, solo se leen los
// chunks que se van a mostrar. Cada mensaje son los bytes de Agmen tal cual
// (sobre + payload): los decodifica agmen.js, igual que los que llegan en vivo.
//
//   const rec = await AgmenMcap.open(AgmenMcap.fileSource(file));
//   rec.channels   // Map(id → { id, topic, schemaId, messageEncoding, metadata })
//   rec.schemas    // Map(id → { id, name, encoding, data: Uint8Array })
//   rec.start, rec.end                       // log_time (BigInt, ns) del primero y del último
//   await rec.messagesBetween(t0, t1, ids)   // [{ channelId, sequence, logTime, publishTime, data }]
//
// Chunks sin comprimir o con lz4. Los de zstd los descomprime `agmen web` (el
// navegador no trae zstd). Sin el índice (el grabador se cayó) se lee todo de
// principio a fin, hasta el último chunk completo, como agmen info.
//
// Sin dependencias. Funciona en el navegador (global `AgmenMcap`) y en Node (require).
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.AgmenMcap = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const MAGIC = [0x89, 0x4d, 0x43, 0x41, 0x50, 0x30, 0x0d, 0x0a];   // \x89MCAP0\r\n
  const OP = { HEADER: 1, FOOTER: 2, SCHEMA: 3, CHANNEL: 4, MESSAGE: 5, CHUNK: 6, MESSAGE_INDEX: 7, CHUNK_INDEX: 8,
               ATTACHMENT: 9, ATTACHMENT_INDEX: 10, STATISTICS: 11, METADATA: 12, METADATA_INDEX: 13,
               SUMMARY_OFFSET: 14, DATA_END: 15 };
  const FOOTER_LEN = 1 + 8 + 20;              // opcode, largo, summary_start, summary_offset_start, crc
  const CACHE_BYTES = 192 * 1024 * 1024;      // chunks ya leídos que se guardan (los más recientes)
  const MAX_SUMMARY = 64 * 1024 * 1024;       // un resumen de verdad mide KB (unos 100 bytes por chunk)
  const MAX_CHUNK = 2 ** 31 - 1;              // lo más que se aparta para un chunk descomprimido
  const utf8 = new TextDecoder("utf-8", { fatal: true });

  class McapError extends Error {}

  // ---------------- leer registros ----------------
  class Cursor {
    constructor(b, pos = 0, end = b.length) {
      this.b = b; this.v = new DataView(b.buffer, b.byteOffset, b.byteLength); this.pos = pos; this.end = end;
    }
    need(n) { if (n < 0 || this.pos + n > this.end) throw new McapError("registro cortado"); }
    u8() { this.need(1); return this.b[this.pos++]; }
    u16() { this.need(2); const x = this.v.getUint16(this.pos, true); this.pos += 2; return x; }
    u32() { this.need(4); const x = this.v.getUint32(this.pos, true); this.pos += 4; return x; }
    u64() { this.need(8); const x = this.v.getBigUint64(this.pos, true); this.pos += 8; return x; }
    num() {                                    // un u64 que es un tamaño o una posición
      const x = this.u64();
      if (x > BigInt(Number.MAX_SAFE_INTEGER)) throw new McapError("tamaño imposible en el archivo");
      return Number(x);
    }
    take(n) { this.need(n); const s = this.b.subarray(this.pos, this.pos + n); this.pos += n; return s; }
    str() {
      const s = this.take(this.u32());
      try { return utf8.decode(s); } catch (e) { throw new McapError("texto que no es UTF-8"); }
    }
    strmap() {
      const n = this.u32(), end = this.pos + n, out = {};   // el largo primero: luego se cuenta desde ahí
      if (end > this.end) throw new McapError("registro cortado");
      while (this.pos < end) { const k = this.str(); out[k] = this.str(); }
      return out;
    }
    countmap() {                               // map<u16, u64>: mensajes por canal
      const n = this.u32(), end = this.pos + n, out = new Map();
      if (end > this.end) throw new McapError("registro cortado");
      while (this.pos < end) { const k = this.u16(); out.set(k, this.num()); }
      return out;
    }
  }

  function parseSchema(c) {
    return { id: c.u16(), name: c.str(), encoding: c.str(), data: c.take(c.u32()) };
  }
  function parseChannel(c) {
    return { id: c.u16(), schemaId: c.u16(), topic: c.str(), messageEncoding: c.str(), metadata: c.strmap() };
  }

  // ---------------- CRC32 y lz4 ----------------
  const CRC_TABLE = (() => {
    const t = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      t[i] = c >>> 0;
    }
    return t;
  })();
  function crc32(b) {
    let c = 0xffffffff;
    for (let i = 0; i < b.length; i++) c = CRC_TABLE[(c ^ b[i]) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  }

  /** Un frame de LZ4 (lo que MCAP llama "lz4") → los bytes, de `size` bytes. */
  function lz4Frame(src, size) {
    const out = new Uint8Array(size), v = new DataView(src.buffer, src.byteOffset, src.byteLength);
    const bad = () => new McapError("lz4: el chunk está dañado");
    let p = 0, o = 0;
    const u32 = () => { if (p + 4 > src.length) throw bad(); const x = v.getUint32(p, true); p += 4; return x; };
    while (p < src.length) {
      const magic = u32();
      if (magic >= 0x184d2a50 && magic <= 0x184d2a5f) { p += u32(); continue; }     // frame que se salta
      if (magic !== 0x184d2204) throw new McapError("lz4: no es un frame de LZ4");
      if (p + 3 > src.length) throw bad();
      const flg = src[p]; p += 2;                                                     // FLG y BD
      if (flg >> 6 !== 1) throw new McapError("lz4: versión de frame desconocida");
      if (flg & 0x01) throw new McapError("lz4: frames con diccionario no se leen aquí");
      if (flg & 0x08) p += 8;                                                         // tamaño del contenido
      p += 1;                                                                         // checksum del encabezado
      for (;;) {
        const bs = u32();
        if (bs === 0) break;                                                          // fin del frame
        const n = bs & 0x7fffffff;
        if (p + n > src.length) throw bad();
        if (bs & 0x80000000) {                                                        // bloque sin comprimir
          if (o + n > size) throw bad();
          out.set(src.subarray(p, p + n), o); o += n;
        } else o = lz4Block(src, p, p + n, out, o, bad);
        p += n;
        if (flg & 0x10) p += 4;                                                       // checksum del bloque
      }
      if (flg & 0x04) p += 4;                                                         // checksum del contenido
    }
    if (o !== size) throw bad();
    return out;
  }
  function lz4Block(src, p, end, out, o, bad) {
    while (p < end) {
      const token = src[p++];
      let lit = token >> 4;
      if (lit === 15) { let b; do { if (p >= end) throw bad(); b = src[p++]; lit += b; } while (b === 255); }
      if (p + lit > end || o + lit > out.length) throw bad();
      out.set(src.subarray(p, p + lit), o); p += lit; o += lit;
      if (p >= end) break;                                                            // la última secuencia: solo literales
      if (p + 2 > end) throw bad();
      const off = src[p] | (src[p + 1] << 8); p += 2;
      let ml = token & 15;
      if (ml === 15) { let b; do { if (p >= end) throw bad(); b = src[p++]; ml += b; } while (b === 255); }
      ml += 4;
      if (off === 0 || off > o || o + ml > out.length) throw bad();
      let from = o - off;
      if (off >= ml) { out.copyWithin(o, from, from + ml); o += ml; }
      else for (let k = 0; k < ml; k++) out[o++] = out[from++];                     // se encima: byte por byte
    }
    return o;
  }

  // ---------------- de dónde se lee ----------------
  /** Un File del navegador (arrastrado o elegido): se lee por partes, sin cargarlo entero. */
  function fileSource(file) {
    return { name: file.name, size: file.size,
             read: async (pos, n) => new Uint8Array(await file.slice(pos, pos + n).arrayBuffer()) };
  }
  /** Un arreglo de bytes ya en memoria (pruebas, Node). */
  function bytesSource(bytes, name = "") {
    const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    return { name, size: b.length, read: async (pos, n) => b.subarray(pos, pos + n) };
  }
  /** El archivo que sirve `agmen web vuelta1.mcap` (con Range), y sus chunks de zstd ya descomprimidos. */
  function urlSource(url, size, name, chunkUrl) {
    return {
      name, size,
      read: async (pos, n) => {
        const r = await fetch(url, { headers: { Range: `bytes=${pos}-${pos + n - 1}` }, credentials: "same-origin" });
        if (r.status !== 206 && r.status !== 200) throw new McapError(`no se pudo leer la grabación (HTTP ${r.status})`);
        const b = new Uint8Array(await r.arrayBuffer());
        return r.status === 200 ? b.subarray(pos, pos + n) : b;
      },
      decompress: chunkUrl ? async (chunk) => {
        const r = await fetch(`${chunkUrl}?start=${chunk.start}`, { credentials: "same-origin" });
        if (r.status !== 200) throw new McapError(`el panel no pudo descomprimir el chunk (HTTP ${r.status})`);
        return new Uint8Array(await r.arrayBuffer());
      } : null,
    };
  }

  // ---------------- la grabación ----------------
  class Recording {
    constructor(source) {
      this.source = source;
      this.name = source.name || "";
      this.size = source.size;
      this.profile = ""; this.library = "";
      this.schemas = new Map(); this.channels = new Map();
      this.chunks = [];             // { start, length, messageStartTime, messageEndTime, compression }
      this.statistics = null;       // { messageCount, channelMessageCounts: Map, messageStartTime, messageEndTime }
      this.metadata = [];           // [{ name, values }]
      this.complete = false;        // ¿trae el resumen (índice)?
      this.truncated = false;       // ¿se cortó a la mitad?
      this._cache = new Map(); this._cacheBytes = 0;
    }
    get start() { return this.statistics ? this.statistics.messageStartTime : 0n; }
    get end() { return this.statistics ? this.statistics.messageEndTime : 0n; }
    /** {nombre: {clave: valor}} de los metadatos (agmen.grabacion, agmen.perdidos). */
    meta(name) { const m = this.metadata.find((x) => x.name === name); return m ? m.values : null; }

    async _open() {
      const size = this.size;
      if (size < MAGIC.length + 9) throw new McapError("no es un archivo MCAP (es muy corto)");
      let head = await this.source.read(0, Math.min(size, 4096));
      if (!MAGIC.every((x, i) => head[i] === x)) throw new McapError("no es un archivo MCAP (no empieza con \\x89MCAP0)");
      const hc = new Cursor(head, MAGIC.length);
      if (hc.u8() !== OP.HEADER) throw new McapError("no es un archivo MCAP (falta el encabezado)");
      const hlen = hc.num();
      if (hc.pos + hlen > head.length) {
        if (hc.pos + hlen > size) throw new McapError("el encabezado está cortado");
        head = await this.source.read(0, hc.pos + hlen);
      }
      const h = new Cursor(head, hc.pos, hc.pos + hlen);
      this.profile = h.str(); this.library = h.str();
      this._dataStart = hc.pos + hlen;
      let summaryStart = 0;
      if (size >= this._dataStart + FOOTER_LEN + MAGIC.length) {
        const tail = await this.source.read(size - FOOTER_LEN - MAGIC.length, FOOTER_LEN + MAGIC.length);
        if (MAGIC.every((x, i) => tail[FOOTER_LEN + i] === x) && tail[0] === OP.FOOTER) {
          const f = new Cursor(tail, 1);
          if (f.num() === 20) summaryStart = f.num();
        }
      }
      const summaryEnd = size - FOOTER_LEN - MAGIC.length;
      if (summaryStart >= this._dataStart && summaryStart < summaryEnd && summaryEnd - summaryStart <= MAX_SUMMARY) {
        await this._readSummary(summaryStart, summaryEnd);
      } else {
        await this._scan();
      }
      this.chunks.sort((a, b) => (a.messageStartTime < b.messageStartTime ? -1 : a.messageStartTime > b.messageStartTime ? 1 : 0));
      return this;
    }

    async _readSummary(from, to) {
      const b = await this.source.read(from, to - from), c = new Cursor(b);
      const metaAt = [];
      while (c.pos + 9 <= c.end) {
        const op = c.u8(), n = c.num(), end = c.pos + n;
        if (end > c.end) throw new McapError("el resumen está cortado");
        const r = new Cursor(b, c.pos, end);
        if (op === OP.SCHEMA) { const s = parseSchema(r); this.schemas.set(s.id, s); }
        else if (op === OP.CHANNEL) { const ch = parseChannel(r); this.channels.set(ch.id, ch); }
        else if (op === OP.CHUNK_INDEX) {
          const messageStartTime = r.u64(), messageEndTime = r.u64(), start = r.num(), length = r.num();
          if (start < this._dataStart || length < 9 || start + length > this.size) {
            throw new McapError("el índice apunta fuera del archivo: está dañado (agmen info --reparar lo arregla)");
          }
          const skip = r.u32();                // message_index_offsets
          r.need(skip); r.pos += skip;
          r.u64();                             // message_index_length
          this.chunks.push({ start, length, messageStartTime, messageEndTime, compression: r.str() });
        } else if (op === OP.STATISTICS) {
          const messageCount = r.num();
          r.u16(); r.u32(); r.u32(); r.u32(); r.u32();   // esquemas, canales, adjuntos, metadatos, chunks
          const messageStartTime = r.u64(), messageEndTime = r.u64();
          this.statistics = { messageCount, messageStartTime, messageEndTime, channelMessageCounts: r.countmap() };
        } else if (op === OP.METADATA_INDEX) {
          metaAt.push([r.num(), r.num()]);
        }
        c.pos = end;
      }
      for (const [pos, n] of metaAt) {         // unos cuantos bytes: agmen.grabacion y agmen.perdidos
        if (pos < this._dataStart || n < 9 || n > MAX_SUMMARY || pos + n > this.size) continue;   // dañado: sin metadatos
        const m = new Cursor(await this.source.read(pos, n));
        if (m.u8() !== OP.METADATA) continue;
        const len = m.num(), r = new Cursor(m.b, m.pos, m.pos + len);
        this.metadata.push({ name: r.str(), values: r.strmap() });
      }
      this.complete = true;
      if (!this.statistics) await this._countFromChunks();
    }

    /** Sin resumen: de principio a fin, hasta el último registro completo. */
    async _scan() {
      let pos = this._dataStart;
      const size = this.size;
      while (pos + 9 <= size) {
        const h = await this.source.read(pos, Math.min(9 + 64, size - pos));
        const c = new Cursor(h), op = c.u8(), n = c.num();
        if (pos + 9 + n > size) { this.truncated = true; break; }      // cortado a la mitad
        if (op === OP.CHUNK) {
          const messageStartTime = c.u64(), messageEndTime = c.u64();
          c.u64(); c.u32();
          const chunk = { start: pos, length: 9 + n, messageStartTime, messageEndTime, compression: c.str() };
          try { await this._chunkMessages(chunk); } catch (e) { this.truncated = true; break; }   // dañado: hasta aquí
          this.chunks.push(chunk);
        } else if (op === OP.SCHEMA || op === OP.CHANNEL || op === OP.METADATA) {
          const b = await this.source.read(pos + 9, n), r = new Cursor(b);
          if (op === OP.SCHEMA) { const s = parseSchema(r); this.schemas.set(s.id, s); }
          else if (op === OP.CHANNEL) { const ch = parseChannel(r); this.channels.set(ch.id, ch); }
          else this.metadata.push({ name: r.str(), values: r.strmap() });
        } else if (op === OP.MESSAGE) {
          throw new McapError("mensajes fuera de chunks: el panel solo abre grabaciones con chunks (como las de agmen record)");
        } else if (op === OP.DATA_END || op === OP.FOOTER) break;
        pos += 9 + n;
      }
      await this._countFromChunks();
    }

    async _countFromChunks() {
      const counts = new Map();
      let total = 0, t0 = null, t1 = null;
      for (const ch of this.chunks) {
        for (const m of await this._chunkMessages(ch)) {
          counts.set(m.channelId, (counts.get(m.channelId) || 0) + 1); total++;
          if (t0 === null || m.logTime < t0) t0 = m.logTime;
          if (t1 === null || m.logTime > t1) t1 = m.logTime;
        }
      }
      this.statistics = { messageCount: total, channelMessageCounts: counts, messageStartTime: t0 ?? 0n, messageEndTime: t1 ?? 0n };
    }

    /** Los registros de un chunk, ya descomprimidos. */
    async _records(chunk) {
      const raw = await this.source.read(chunk.start, chunk.length), c = new Cursor(raw);
      if (c.u8() !== OP.CHUNK) throw new McapError("el índice apunta a algo que no es un chunk");
      c.num(); c.u64(); c.u64();
      const size = c.num(), crc = c.u32(), comp = c.str(), n = c.num();
      let rec = c.take(n);
      if (size > MAX_CHUNK) throw new McapError("el chunk dice medir más de 2 GiB: está dañado");
      if (comp === "lz4") {
        if (size > n * 255 + 1024) throw new McapError("lz4: el chunk dice medir más de lo que cabe: está dañado");
        rec = lz4Frame(rec, size);
      }
      else if (comp === "zstd") {
        if (!this.source.decompress) {
          throw new McapError("la grabación está comprimida con zstd y el navegador no la sabe abrir. Ábrela con " +
                              "'agmen web archivo.mcap', que la descomprime (o grábala sin --compress, o con lz4)");
        }
        rec = await this.source.decompress(chunk);
      } else if (comp !== "") throw new McapError(`compresión '${comp}' desconocida`);
      if (rec.length !== size) throw new McapError("el chunk no mide lo que dice");
      if (crc && comp !== "zstd" && crc32(rec) !== crc) throw new McapError("el chunk está dañado (CRC)");
      return rec;
    }

    /** Los mensajes de un chunk en orden de log_time (y los esquemas y canales que traiga). */
    async _chunkMessages(chunk) {
      const hit = this._cache.get(chunk.start);
      if (hit) { this._cache.delete(chunk.start); this._cache.set(chunk.start, hit); return hit.msgs; }
      const rec = await this._records(chunk), c = new Cursor(rec), msgs = [];
      while (c.pos < c.end) {
        const op = c.u8(), n = c.num(), end = c.pos + n;
        if (end > c.end) throw new McapError("registro cortado dentro de un chunk");
        const r = new Cursor(rec, c.pos, end);
        if (op === OP.MESSAGE) {
          const channelId = r.u16(), sequence = r.u32(), logTime = r.u64(), publishTime = r.u64();
          msgs.push({ channelId, sequence, logTime, publishTime, data: rec.subarray(r.pos, end) });
        } else if (op === OP.SCHEMA) { const s = parseSchema(r); if (!this.schemas.has(s.id)) this.schemas.set(s.id, s); }
        else if (op === OP.CHANNEL) { const ch = parseChannel(r); if (!this.channels.has(ch.id)) this.channels.set(ch.id, ch); }
        c.pos = end;
      }
      msgs.sort((a, b) => (a.logTime < b.logTime ? -1 : a.logTime > b.logTime ? 1 : 0));   // MCAP no exige orden
      this._cache.set(chunk.start, { msgs, bytes: rec.length });
      this._cacheBytes += rec.length;
      for (const [k, v] of this._cache) {      // se olvidan los más viejos
        if (this._cacheBytes <= CACHE_BYTES || this._cache.size <= 2) break;
        this._cache.delete(k); this._cacheBytes -= v.bytes;
      }
      return msgs;
    }

    /** Los mensajes con t0 <= log_time < t1 (BigInt, ns), de los canales `ids` (Set; null = todos), en orden. */
    async messagesBetween(t0, t1, ids = null) {
      const out = [];
      for (const ch of this.chunks) {
        if (ch.messageEndTime < t0 || ch.messageStartTime >= t1) continue;
        for (const m of await this._chunkMessages(ch)) {
          if (m.logTime >= t0 && m.logTime < t1 && (!ids || ids.has(m.channelId))) out.push(m);
        }
      }
      out.sort((a, b) => (a.logTime < b.logTime ? -1 : a.logTime > b.logTime ? 1 : 0));
      return out;
    }

    /** Todos los mensajes, en orden (para pruebas y grabaciones chicas). */
    async allMessages(ids = null) {
      return this.messagesBetween(0n, (1n << 64n) - 1n, ids);
    }
  }

  async function open(source) {
    return new Recording(source)._open();
  }

  return { open, fileSource, bytesSource, urlSource, McapError, crc32, lz4Frame, OP };
});

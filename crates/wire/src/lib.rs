//! # wire — formato binario v0 del framework
//!
//! Este crate define **el contrato entre lenguajes**. Cualquier nodo (Rust,
//! Python, C++, un ESP32...) que escriba y lea exactamente estos bytes puede
//! hablar con cualquier otro. La especificación completa está en
//! `docs/PROTOCOLO.md`; este código es la implementación de referencia.
//!
//! Reglas del formato v0:
//! - Todo en **little-endian** (es lo nativo en x86, ARM y ESP32).
//! - Header fijo de **32 bytes**, seguido del payload.
//! - Cada tipo de mensaje tiene un `type_id` = hash FNV-1a 64 de su esquema.
//!   Si el emisor y el receptor no coinciden, el receptor lo reporta con un
//!   error claro (lección #1 de ROS 2: nunca fallar en silencio).

use std::fmt;
use std::time::{SystemTime, UNIX_EPOCH};

/// Bytes mágicos al inicio de cada paquete: "RFW" (Robot FrameWork).
pub const MAGIC: [u8; 3] = *b"RFW";
/// Versión del formato de cable.
pub const WIRE_VERSION: u8 = 0;
/// Tamaño del header en bytes.
pub const HEADER_LEN: usize = 32;

// ---------------------------------------------------------------------------
// Errores
// ---------------------------------------------------------------------------

/// Todo lo que puede salir mal al decodificar. Cada variante dice
/// exactamente qué pasó, para que el usuario no tenga que adivinar.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WireError {
    /// El paquete es más corto que el header.
    TooShort { got: usize, need: usize },
    /// No empieza con "RFW": no es un paquete nuestro.
    BadMagic([u8; 3]),
    /// Versión del protocolo distinta.
    UnsupportedVersion { got: u8, expected: u8 },
    /// El tipo del mensaje no es el que el receptor espera.
    TypeMismatch {
        got: u64,
        expected: u64,
        expected_name: &'static str,
    },
    /// `payload_len` del header no coincide con los bytes recibidos.
    PayloadLenMismatch { header_says: usize, actual: usize },
    /// El payload no mide lo que el tipo requiere.
    BadPayloadSize {
        type_name: &'static str,
        got: usize,
        expected: usize,
    },
}

impl fmt::Display for WireError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            WireError::TooShort { got, need } => {
                write!(
                    f,
                    "paquete demasiado corto: {got} bytes, se necesitan al menos {need}"
                )
            }
            WireError::BadMagic(m) => write!(
                f,
                "magic inválido {:?}: el paquete no viene de un nodo del framework",
                String::from_utf8_lossy(m)
            ),
            WireError::UnsupportedVersion { got, expected } => write!(
                f,
                "versión de protocolo {got} no soportada (este nodo habla la versión {expected})"
            ),
            WireError::TypeMismatch {
                got,
                expected,
                expected_name,
            } => write!(
                f,
                "TIPO INCOMPATIBLE: se esperaba '{expected_name}' (type_id {expected:#018x}) \
                 pero llegó type_id {got:#018x}. ¿El publisher usa otra versión del mensaje?"
            ),
            WireError::PayloadLenMismatch {
                header_says,
                actual,
            } => write!(
                f,
                "el header dice payload de {header_says} bytes pero llegaron {actual}"
            ),
            WireError::BadPayloadSize {
                type_name,
                got,
                expected,
            } => write!(
                f,
                "payload de '{type_name}' debe medir {expected} bytes, llegaron {got}"
            ),
        }
    }
}

impl std::error::Error for WireError {}

// ---------------------------------------------------------------------------
// type_id: hash del esquema
// ---------------------------------------------------------------------------

/// FNV-1a de 64 bits. Es trivial de implementar en cualquier lenguaje
/// (ver `python/rfw.py`), por eso lo usamos en vez de algo más sofisticado.
pub const fn fnv1a64(bytes: &[u8]) -> u64 {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    let mut i = 0;
    while i < bytes.len() {
        hash ^= bytes[i] as u64;
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        i += 1;
    }
    hash
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

/// Header fijo de 32 bytes que precede a todo mensaje.
///
/// | offset | tamaño | campo         |
/// |--------|--------|---------------|
/// | 0      | 3      | magic "RFW"   |
/// | 3      | 1      | version       |
/// | 4      | 8      | type_id       |
/// | 12     | 8      | seq           |
/// | 20     | 8      | stamp_ns      |
/// | 28     | 4      | payload_len   |
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Header {
    pub type_id: u64,
    /// Número de secuencia: lo incrementa el publisher en cada mensaje.
    /// Sirve para detectar pérdidas, duplicados y desorden.
    pub seq: u64,
    /// Momento de envío en nanosegundos desde la época Unix.
    pub stamp_ns: u64,
    pub payload_len: u32,
}

impl Header {
    pub fn write_to(&self, out: &mut Vec<u8>) {
        out.extend_from_slice(&MAGIC);
        out.push(WIRE_VERSION);
        out.extend_from_slice(&self.type_id.to_le_bytes());
        out.extend_from_slice(&self.seq.to_le_bytes());
        out.extend_from_slice(&self.stamp_ns.to_le_bytes());
        out.extend_from_slice(&self.payload_len.to_le_bytes());
    }

    /// Lee y valida el header. Devuelve el header y el resto (payload).
    pub fn read_from(buf: &[u8]) -> Result<(Header, &[u8]), WireError> {
        if buf.len() < HEADER_LEN {
            return Err(WireError::TooShort {
                got: buf.len(),
                need: HEADER_LEN,
            });
        }
        let magic = [buf[0], buf[1], buf[2]];
        if magic != MAGIC {
            return Err(WireError::BadMagic(magic));
        }
        if buf[3] != WIRE_VERSION {
            return Err(WireError::UnsupportedVersion {
                got: buf[3],
                expected: WIRE_VERSION,
            });
        }
        let header = Header {
            type_id: read_u64(buf, 4),
            seq: read_u64(buf, 12),
            stamp_ns: read_u64(buf, 20),
            payload_len: read_u32(buf, 28),
        };
        let payload = &buf[HEADER_LEN..];
        if payload.len() != header.payload_len as usize {
            return Err(WireError::PayloadLenMismatch {
                header_says: header.payload_len as usize,
                actual: payload.len(),
            });
        }
        Ok((header, payload))
    }
}

fn read_u64(buf: &[u8], at: usize) -> u64 {
    u64::from_le_bytes(buf[at..at + 8].try_into().expect("rango validado"))
}

fn read_u32(buf: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(buf[at..at + 4].try_into().expect("rango validado"))
}

fn read_f32(buf: &[u8], at: usize) -> f32 {
    f32::from_le_bytes(buf[at..at + 4].try_into().expect("rango validado"))
}

// ---------------------------------------------------------------------------
// Mensajes
// ---------------------------------------------------------------------------

/// Un tipo de mensaje que se puede mandar por el cable.
///
/// En el nivel 2 estos impls se van a **generar automáticamente** desde un
/// archivo de definición (nuestro IDL). Por ahora se escriben a mano para
/// entender qué hace el generador.
pub trait Message: Sized {
    /// Nombre legible del tipo.
    const NAME: &'static str;
    /// Esquema canónico: nombre + campos en orden. Su hash es el `type_id`.
    const SCHEMA: &'static str;
    /// Hash del esquema. Cambia si cambia cualquier campo.
    const TYPE_ID: u64 = fnv1a64(Self::SCHEMA.as_bytes());

    fn encode_payload(&self, out: &mut Vec<u8>);
    fn decode_payload(payload: &[u8]) -> Result<Self, WireError>;
}

/// Lectura de una IMU: acelerómetro (m/s²) y giroscopio (rad/s).
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct ImuSample {
    pub accel: [f32; 3],
    pub gyro: [f32; 3],
}

impl ImuSample {
    pub const PAYLOAD_LEN: usize = 6 * 4;
}

impl Message for ImuSample {
    const NAME: &'static str = "ImuSample";
    const SCHEMA: &'static str = "ImuSample{f32 ax;f32 ay;f32 az;f32 gx;f32 gy;f32 gz}";

    fn encode_payload(&self, out: &mut Vec<u8>) {
        for v in self.accel.iter().chain(self.gyro.iter()) {
            out.extend_from_slice(&v.to_le_bytes());
        }
    }

    fn decode_payload(p: &[u8]) -> Result<Self, WireError> {
        if p.len() != Self::PAYLOAD_LEN {
            return Err(WireError::BadPayloadSize {
                type_name: Self::NAME,
                got: p.len(),
                expected: Self::PAYLOAD_LEN,
            });
        }
        Ok(ImuSample {
            accel: [read_f32(p, 0), read_f32(p, 4), read_f32(p, 8)],
            gyro: [read_f32(p, 12), read_f32(p, 16), read_f32(p, 20)],
        })
    }
}

// ---------------------------------------------------------------------------
// Funciones de alto nivel
// ---------------------------------------------------------------------------

/// Serializa header + payload en un paquete listo para enviar.
pub fn encode<M: Message>(msg: &M, seq: u64, stamp_ns: u64) -> Vec<u8> {
    let mut payload = Vec::new();
    msg.encode_payload(&mut payload);
    let header = Header {
        type_id: M::TYPE_ID,
        seq,
        stamp_ns,
        payload_len: payload.len() as u32,
    };
    let mut out = Vec::with_capacity(HEADER_LEN + payload.len());
    header.write_to(&mut out);
    out.extend_from_slice(&payload);
    out
}

/// Decodifica un paquete validando que sea del tipo `M`.
pub fn decode<M: Message>(buf: &[u8]) -> Result<(Header, M), WireError> {
    let (header, payload) = Header::read_from(buf)?;
    if header.type_id != M::TYPE_ID {
        return Err(WireError::TypeMismatch {
            got: header.type_id,
            expected: M::TYPE_ID,
            expected_name: M::NAME,
        });
    }
    let msg = M::decode_payload(payload)?;
    Ok((header, msg))
}

/// Tiempo actual en ns desde la época Unix (reloj de pared).
///
/// Nota: sirve para medir latencia entre procesos de la MISMA máquina.
/// Entre máquinas distintas los relojes no están sincronizados; eso se
/// resuelve en el nivel 7 (tiempo y transformadas).
pub fn now_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("reloj del sistema antes de 1970")
        .as_nanos() as u64
}

// ---------------------------------------------------------------------------
// Pruebas
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> ImuSample {
        ImuSample {
            accel: [0.1, -9.81, 0.0],
            gyro: [0.01, 0.02, -0.03],
        }
    }

    #[test]
    fn ida_y_vuelta() {
        let bytes = encode(&sample(), 42, 123_456_789);
        assert_eq!(bytes.len(), HEADER_LEN + ImuSample::PAYLOAD_LEN);
        let (h, m) = decode::<ImuSample>(&bytes).unwrap();
        assert_eq!(h.seq, 42);
        assert_eq!(h.stamp_ns, 123_456_789);
        assert_eq!(m, sample());
    }

    #[test]
    fn layout_del_header_es_estable() {
        // Si este test falla, rompiste la compatibilidad con Python/C++.
        let bytes = encode(&sample(), 1, 2);
        assert_eq!(&bytes[0..3], b"RFW");
        assert_eq!(bytes[3], 0);
        assert_eq!(&bytes[4..12], &ImuSample::TYPE_ID.to_le_bytes());
        assert_eq!(&bytes[12..20], &1u64.to_le_bytes());
        assert_eq!(&bytes[20..28], &2u64.to_le_bytes());
        assert_eq!(&bytes[28..32], &24u32.to_le_bytes());
    }

    #[test]
    fn type_id_conocido() {
        // Valor fijo: Python y C++ deben calcular exactamente el mismo.
        assert_eq!(ImuSample::TYPE_ID, fnv1a64(ImuSample::SCHEMA.as_bytes()));
        assert_eq!(fnv1a64(b""), 0xcbf2_9ce4_8422_2325);
        assert_eq!(fnv1a64(b"a"), 0xaf63_dc4c_8601_ec8c);
    }

    #[test]
    fn rechaza_magic_invalido() {
        let mut bytes = encode(&sample(), 0, 0);
        bytes[0] = b'X';
        assert!(matches!(
            decode::<ImuSample>(&bytes),
            Err(WireError::BadMagic(_))
        ));
    }

    #[test]
    fn rechaza_tipo_incorrecto() {
        let mut bytes = encode(&sample(), 0, 0);
        bytes[4] ^= 0xFF; // corrompemos el type_id
        let err = decode::<ImuSample>(&bytes).unwrap_err();
        assert!(matches!(err, WireError::TypeMismatch { .. }));
        assert!(err.to_string().contains("TIPO INCOMPATIBLE"));
    }

    #[test]
    fn rechaza_paquete_truncado() {
        let bytes = encode(&sample(), 0, 0);
        assert!(matches!(
            decode::<ImuSample>(&bytes[..10]),
            Err(WireError::TooShort { .. })
        ));
        assert!(matches!(
            decode::<ImuSample>(&bytes[..40]),
            Err(WireError::PayloadLenMismatch { .. })
        ));
    }
}

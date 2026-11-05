// agmen/shm.hpp — memoria compartida para los mensajes grandes (RFC 0009, nivel 8).
//
// No se usa directo: Publisher y Subscriber la eligen solos. Si un mensaje es
// arrow de 4 KiB o más y el suscriptor puede abrir la memoria del publicador
// (lo prueba al conectarse), el publicador escribe el payload UNA vez en un
// bloque de memoria compartida y por TCP manda solo un descriptor de 72 bytes.
// El suscriptor le entrega a tu código una vista del bloque (sin copiar) y se
// lo devuelve al publicador cuando ya nadie la usa. Si no puede, todo sigue
// por TCP, como en 0.10.0.
//
// Aquí vive todo lo que cambia entre sistemas operativos:
//
//   Linux / macOS   shm_open + mmap, nombres "/agm…" (macOS: máximo 31
//                   caracteres); un registro en $TMPDIR para borrar los bloques
//                   de los procesos que murieron sin cerrar.
//   Windows         CreateFileMappingW / OpenFileMappingW en "Local\agm…";
//                   el sistema borra el bloque solo cuando nadie lo usa.
//
// La implementación de referencia en Python es python/agmen/_shm.py; las dos
// escriben y leen los mismos bytes y los mismos nombres.
#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "agmen/wire.hpp"

namespace agmen::shm {

/// Bytes del descriptor que viaja en el payload de un sobre con kFlagShm.
inline constexpr std::size_t kDescriptorLen = 72;
/// Cabecera de cada bloque (el payload empieza aquí: alineado a 64, RFC 0001 §7).
inline constexpr std::size_t kHeaderLen = 64;
/// Un aviso del suscriptor al publicador.
inline constexpr std::size_t kRecordLen = 16;
/// Desde este tamaño de payload un mensaje arrow va por memoria compartida (D5).
inline constexpr std::size_t kMinPayload = 4096;
/// Mensajes prestados a la vez por suscriptor (y bytes); los demás se copian
/// al recibir. Siempre se puede prestar al menos uno (D6).
inline constexpr std::size_t kMaxLent = 4;
inline constexpr std::size_t kMaxLentBytes = std::size_t{64} << 20;
/// Descriptores sin devolver por conexión (y bytes); luego, solo el más nuevo.
/// Siempre al menos dos: así quien tiene prestado el máximo sigue recibiendo (D6).
inline constexpr std::size_t kMaxUnreturned = 8;
inline constexpr std::size_t kMaxUnreturnedBytes = std::size_t{128} << 20;
/// Bloques por publicador (D6).
inline constexpr std::size_t kMaxBlocks = 64;
/// Un bloque que nadie tiene prestado y que no se usó en este tiempo se libera
/// (D6 a′): después de una ráfaga, el pool vuelve a lo que de verdad se usa. El
/// suscriptor suelta lo que tenía abierto de un bloque tras el mismo tiempo.
inline constexpr std::chrono::seconds kIdleFree{5};
/// Tamaño del bloque de prueba (§3.3).
inline constexpr std::size_t kProbeSize = 4096;
/// Marca al principio de cada bloque.
inline constexpr char kMagic[8] = {'A', 'G', 'M', 'S', 'H', 'M', '1', '\0'};
/// Marca de los avisos del suscriptor.
inline constexpr char kRecordMagic[3] = {'A', 'G', 'B'};

/// Algo salió mal al crear o abrir un bloque. `what()` dice qué y por qué.
class ShmError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

/// ¿Está prendida? Sí, salvo AGMEN_SHM=0.
[[nodiscard]] bool enabled();

/// Lo más que puede apartar un publicador (D6 a′): AGMEN_SHM_MB si está puesta
/// (aunque pase de ¼ de la RAM); si no, lo menor entre 512 MiB, ¼ de la RAM
/// física y, en Linux, ½ de /dev/shm.
[[nodiscard]] std::size_t limit_bytes();
/// De dónde sale limit_bytes(), para los avisos: "AGMEN_SHM_MB", "¼ de la RAM",
/// "½ de /dev/shm" o "el máximo".
[[nodiscard]] std::string limit_reason();
/// La RAM física de la máquina (0 si no se sabe).
[[nodiscard]] std::uint64_t physical_ram();
/// Linux: lo que mide /dev/shm (en Docker, 64 MB si no se cambia). 0 en los demás.
[[nodiscard]] std::uint64_t devshm_size();

namespace detail {
/// Solo para las pruebas: finge que la máquina tiene `bytes` de RAM (0 = la de verdad).
void fake_physical_ram(std::uint64_t bytes);
}  // namespace detail

/// Los avisos del suscriptor al publicador, por la conexión TCP (§3.7).
enum class Op : std::uint8_t {
    Hello = 1,         ///< "puedo memoria compartida" (lo primero que dice el suscriptor)
    ProbeOk = 2,       ///< "abrí tu bloque de prueba"
    ProbeFailed = 3,   ///< "no lo pude abrir": que siga por TCP
    Release = 4,       ///< "ya solté el bloque `slot` (generación `generation`)"
};

/// Un aviso de 16 bytes: "AGB", op, slot (u32), generación (u64).
struct Record {
    Op op = Op::Hello;
    std::uint32_t slot = 0;
    std::uint64_t generation = 0;

    void write_to(wire::Bytes& out) const;
    /// Lee un aviso. Lanza wire::WireError si no empieza con "AGB" o la operación no existe.
    [[nodiscard]] static Record read(wire::ByteView data);
};

/// Dónde está el payload de un mensaje (RFC 0009 §3.7).
struct Descriptor {
    std::string name;             ///< "/agm…" (Windows lo abre como "Local\agm…")
    std::uint64_t size = 0;       ///< bytes del bloque
    std::uint64_t offset = kHeaderLen;
    std::uint64_t length = 0;     ///< bytes del payload
    std::uint64_t generation = 0; ///< el seq del mensaje
    std::uint32_t slot = 0;

    void write_to(wire::Bytes& out) const;
    /// Lanza wire::WireError si no mide 72 bytes o no cuadra.
    [[nodiscard]] static Descriptor read(wire::ByteView data);
};

/// La cabecera de 64 bytes de cada bloque.
struct BlockHeader {
    std::uint64_t owner = 0;       ///< source_id del publicador
    std::uint64_t generation = 0;  ///< seq del mensaje que guarda (0 en el de prueba)
    std::uint64_t length = 0;      ///< bytes del payload
    std::uint64_t size = 0;        ///< bytes del bloque

    void write(std::uint8_t* at) const;
    /// Lanza ShmError si la marca no es la de Agmen.
    [[nodiscard]] static BlockHeader read(const std::uint8_t* at);
};

/// Un bloque de memoria compartida con nombre (se cierra al destruirse).
class Segment {
public:
    /// Crea un bloque nuevo de `size` bytes, de lectura y escritura, solo para
    /// tu usuario. Lanza ShmError (ya existe, no hay espacio, sin permiso...).
    [[nodiscard]] static Segment create(const std::string& name, std::size_t size);
    /// Abre uno que ya existe, de solo lectura. Lanza ShmError.
    [[nodiscard]] static Segment open(const std::string& name, std::size_t size);

    Segment() = default;
    Segment(Segment&& other) noexcept;
    Segment& operator=(Segment&& other) noexcept;
    Segment(const Segment&) = delete;
    Segment& operator=(const Segment&) = delete;
    ~Segment();

    [[nodiscard]] std::uint8_t* data() { return static_cast<std::uint8_t*>(ptr_); }
    [[nodiscard]] const std::uint8_t* data() const { return static_cast<const std::uint8_t*>(ptr_); }
    [[nodiscard]] std::size_t size() const { return size_; }
    [[nodiscard]] const std::string& name() const { return name_; }
    [[nodiscard]] bool valid() const { return ptr_ != nullptr; }

private:
    void close() noexcept;

    std::string name_;
    void* ptr_ = nullptr;
    std::size_t size_ = 0;
    std::intptr_t handle_ = 0;   // Windows: el HANDLE del mapeo
    bool owner_ = false;         // POSIX: el que lo creó borra el nombre al cerrar
};

/// Borra el nombre de un bloque (POSIX; en Windows no hace nada). Quien ya lo
/// mapeó lo sigue viendo.
void unlink(const std::string& name) noexcept;

/// Borra los bloques de los procesos que murieron sin cerrar (Linux y macOS,
/// con el registro de $TMPDIR/agmen-shm-<usuario>). Pool lo llama una vez por
/// proceso; es seguro llamarlo cuando quieras.
void sweep() noexcept;

/// Los bloques de un publicador (RFC 0009 §3.4). Las cuentas de quién tiene
/// prestado qué viven aquí, en la memoria del publicador, y no en la memoria
/// compartida. No es seguro entre hilos: lo protege quien lo usa (el Fanout).
class Pool {
public:
    /// `owner` = source_id del publicador; va en la cabecera de cada bloque.
    explicit Pool(std::uint64_t owner);
    ~Pool();
    Pool(const Pool&) = delete;
    Pool& operator=(const Pool&) = delete;

    /// Escribe `payload` en un bloque libre (lo crea si hace falta) y devuelve
    /// su descriptor. El bloque queda libre hasta que alguien lo aparte con
    /// hold(). nullopt si no hay lugar; `why` dice por qué.
    [[nodiscard]] std::optional<Descriptor> write(std::uint64_t generation, wire::ByteView payload, std::string& why);

    /// El descriptor del bloque de prueba (lo crea la primera vez). nullopt si
    /// no se pudo crear; `why` dice por qué.
    [[nodiscard]] std::optional<Descriptor> probe(std::string& why);

    void hold(std::uint32_t slot);
    void release(std::uint32_t slot);

    /// Libera los bloques que nadie tiene prestado y que no se usaron en
    /// kIdleFree, y el de prueba si nadie lo pidió en ese tiempo (D6 a′): borra
    /// su nombre, suelta su memoria y lo quita del registro. Un bloque prestado
    /// o nombrado por un descriptor que todavía viaja (los dos cuentan en
    /// hold()) nunca se libera. Devuelve los bytes liberados.
    std::size_t trim(std::chrono::steady_clock::time_point now);

    /// Bloques de datos que existen (sin el de prueba).
    [[nodiscard]] std::size_t blocks() const;
    /// Bytes apartados en bloques de datos (sin el de prueba).
    [[nodiscard]] std::size_t bytes() const;
    /// Cuántos bloques tienen algún préstamo.
    [[nodiscard]] std::size_t busy() const;

private:
    struct Block {
        std::unique_ptr<Segment> segment;   // nullptr: la ranura quedó libre (trim)
        std::uint32_t holders = 0;
        std::chrono::steady_clock::time_point used{};   // la última vez que se escribió o se soltó
    };

    [[nodiscard]] std::string next_name();
    [[nodiscard]] Segment create(std::size_t size);
    void rewrite_registry() const;

    std::uint64_t owner_;
    std::uint32_t counter_ = 0;
    std::vector<Block> blocks_;
    std::unique_ptr<Segment> probe_;
    std::chrono::steady_clock::time_point probe_used_{};
    std::string registry_;   // POSIX: el archivo donde se anotan los nombres
};

}  // namespace agmen::shm

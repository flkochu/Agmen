// agmen._core — el núcleo de C++ de Agmen para Python (RFC 0007, nivel 6).
//
// No se usa directo: `import agmen` lo carga si está compilado y, si no, usa la
// versión de Python puro (agmen._backend). Aquí viven:
//
//   Layout       codificar y decodificar el layout compact de un esquema
//   Publisher    PublisherCore: topics, hub, anuncios, seq, sockets
//   Subscriber   SubscriberCore: espera SIN el GIL, revisa Ctrl+C cada 200 ms
//   ShmPayload   un payload en la memoria compartida del publicador (RFC 0009),
//                visto sin copiar (memoryview, NumPy)
//   Node         el executor de C++: temporizadores, colas y grupos sin el GIL;
//                tus funciones de Python toman el GIL solo mientras corren
//   reloj        el reloj de Agmen del proceso (agmen/clock.hpp)
//   avisos       agmen::log → sys.stderr
#include <nanobind/nanobind.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/tuple.h>
#include <nanobind/stl/vector.h>

#include <agmen/agmen.hpp>
#include <agmen/shm.hpp>

#if defined(__GLIBCXX__)
#include <cxxabi.h>
#endif

#include <array>
#include <atomic>
#include <bit>
#include <cfloat>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <tuple>
#include <vector>

namespace nb = nanobind;
using namespace nb::literals;

namespace {

// ---------------------------------------------------------------------------
// Errores: las clases de Python las pone agmen al importar (set_errors), así
// `except agmen.wire.WireError` atrapa lo que lanza C++.
// ---------------------------------------------------------------------------

PyObject* g_schema_error = nullptr;
PyObject* g_wire_error = nullptr;

[[noreturn]] void raise(PyObject* type, const std::string& msg) {
    PyErr_SetString(type != nullptr ? type : PyExc_ValueError, msg.c_str());
    throw nb::python_error();
}

[[noreturn]] void schema_error(const std::string& msg) { raise(g_schema_error, msg); }

// ---------------------------------------------------------------------------
// El cierre del intérprete.
//
// Un hilo que vuelve a tomar el GIL cuando el intérprete ya se está
// finalizando es terminado por CPython (3.12: pthread_exit). Si en su pila hay
// código nuestro (un gil_scoped_release, un gil_scoped_acquire, el despacho de
// nanobind), ese desenrollado forzado cruza un destructor noexcept y el
// proceso aborta ("terminate called without an active exception").
//
// Por eso, al salir (atexit, que corre ANTES de que empiece la finalización),
// agmen llama a _shutdown(): el núcleo queda "cerrando", y desde entonces
//   - las llamadas que soltarían el GIL, desde otros hilos, no lo sueltan: no
//     hacen nada y regresan (publish → False, receive → None, spin → vuelve);
//   - los hilos de C++ ya no toman el GIL (las funciones de un Node no corren;
//     un aviso sale directo a stderr);
//   - _shutdown() espera, con límite, a las llamadas que ya estaban dentro,
//     hasta que hayan vuelto a Python con el GIL.
// El hilo que cierra (el que corre atexit y luego finaliza) sigue normal.
//
// Si vence el límite, _shutdown() se rinde (g_abandoned): un hilo que vuelve
// de C++ después se queda dormido en vez de tomar el GIL (park_forever), y
// _shutdown() no regresa mientras haya un hilo A MEDIO tomar el GIL
// (g_retaking). Con glibc, una función de Python llamada desde C++ (un Node,
// el destino de avisos) que suelta y retoma el GIL por dentro (time.sleep, E/S,
// el cambio de hilo cada 5 ms) y es terminada en la finalización también se
// queda dormida (catch de abi::__forced_unwind en with_python).
//
// Sin cubrir: código de Python que corre DEBAJO de un marco de C++ del núcleo
// que tiene el GIL y lo suelta por dentro, p. ej. una @property leída por
// Layout.pack, o un __del__ que corre al soltar una referencia en C++. Si eso
// coincide con la finalización, el proceso todavía puede abortar.
// ---------------------------------------------------------------------------

// Lo que puede estar en curso al salir (para decir qué quedó sin cerrar).
enum class Inside : std::size_t { Publish, Receive, Heartbeat, Spin, Callback, Log, Close, Create, Release, Count };

constexpr std::array<const char*, static_cast<std::size_t>(Inside::Count)> kInsideName = {
    "publish()", "receive()", "un latido", "spin()", "una función de un Node", "un aviso",
    "close()", "crear un Publisher o Subscriber", "soltar una función de Python"};

// Un contador por línea de caché (relleno explícito: alignas da el aviso C4324 en MSVC).
struct InsideCount {
    std::atomic<int> n{0};
    char pad[64 - sizeof(std::atomic<int>)] = {};
};

std::array<InsideCount, static_cast<std::size_t>(Inside::Count)> g_inside;
std::atomic<int> g_retaking{0};         // hilos a medio tomar el GIL (ver _shutdown)
std::atomic<bool> g_closing{false};     // _shutdown() empezó
std::atomic<bool> g_abandoned{false};   // _shutdown() se rindió (venció el límite)
thread_local bool t_closer = false;     // este hilo es el que cierra

/// ¿Este hilo ya no debe soltar ni tomar el GIL?
bool closing_for_me() { return g_closing.load() && !t_closer; }

/// Un hilo que vuelve de C++ cuando el cierre ya se rindió: si tomara el GIL,
/// la finalización lo terminaría y el proceso abortaría. Se queda dormido; el
/// proceso termina igual (es un hilo daemon o de C++).
[[noreturn]] void park_forever() {
    for (;;) std::this_thread::sleep_for(std::chrono::hours(1));
}

/// Anuncia que este hilo va a tomar el GIL. false = el cierre ya se rindió: no
/// lo tomes. Con true, llama a retaken() en cuanto lo tengas.
bool begin_retake() {
    g_retaking.fetch_add(1);   // antes de mirar g_abandoned: _shutdown() lo ve o nosotros lo vemos
    if (g_abandoned.load() && !t_closer) {
        g_retaking.fetch_sub(1);
        return false;
    }
    return true;
}
void retaken() { g_retaking.fetch_sub(1); }

/// En lugar de nb::gil_scoped_release, en las llamadas desde Python. Si el
/// núcleo está cerrando, NO suelta el GIL (`released()` = false) y quien llama
/// decide qué hacer (normalmente regresar sin hacer nada).
class NoGil {
public:
    explicit NoGil(Inside what) : n_(g_inside[static_cast<std::size_t>(what)].n) {
        n_.fetch_add(1);   // antes de mirar g_closing: _shutdown() lo ve o nosotros lo vemos
        if (closing_for_me()) {
            n_.fetch_sub(1);
            return;
        }
        state_ = PyEval_SaveThread();
    }
    ~NoGil() {
        if (state_ == nullptr) return;
        if (!begin_retake()) park_forever();
        PyEval_RestoreThread(state_);
        retaken();
        n_.fetch_sub(1);   // DESPUÉS de retomar el GIL: si no, _shutdown() podría seguir antes
    }
    NoGil(const NoGil&) = delete;
    NoGil& operator=(const NoGil&) = delete;
    [[nodiscard]] bool released() const { return state_ != nullptr; }

private:
    std::atomic<int>& n_;
    PyThreadState* state_ = nullptr;
};

/// Un hilo (de C++, o de Python que soltó el GIL) que va a llamar a Python:
/// toma el GIL, corre `fn` y lo suelta. Cerrando el núcleo, no corre `fn` y
/// regresa false. Si `fn` es terminada a medias por la finalización (glibc:
/// abi::__forced_unwind), el hilo se queda dormido: el proceso termina igual.
template <class Fn>
bool with_python(Inside what, Fn&& fn) {
    std::atomic<int>& n = g_inside[static_cast<std::size_t>(what)].n;
    n.fetch_add(1);
    if (closing_for_me() || !begin_retake()) {
        n.fetch_sub(1);
        return false;
    }
    const PyGILState_STATE st = PyGILState_Ensure();
    retaken();
    try {
        fn();
#if defined(__GLIBCXX__)
    } catch (abi::__forced_unwind&) {
        park_forever();   // nunca sale del catch: no hay que relanzar
#endif
    } catch (...) {
        PyGILState_Release(st);
        n.fetch_sub(1);
        throw;
    }
    PyGILState_Release(st);
    n.fetch_sub(1);   // DESPUÉS de soltar el GIL
    return true;
}

std::runtime_error closing_error(const char* what) {
    return std::runtime_error(std::string("agmen se está cerrando (el programa está saliendo): ya no se puede crear ") +
                              what + " desde otro hilo. Crea tus Publisher y Subscriber antes de salir.");
}

// Una referencia a un objeto de Python que se puede guardar en C++ y
// destruir desde cualquier hilo (toma el GIL para el conteo de referencias).
// No se copia: se comparte con std::shared_ptr.
struct PyRef {
    PyObject* o = nullptr;
    explicit PyRef(nb::handle h) : o(h.ptr()) { Py_XINCREF(o); }
    PyRef(const PyRef&) = delete;
    PyRef& operator=(const PyRef&) = delete;
    ~PyRef() {
        if (o == nullptr || Py_IsInitialized() == 0) return;
        // Cerrando: se queda la referencia (el proceso ya termina).
        (void)with_python(Inside::Release, [this] { Py_DECREF(o); });
    }
};

// ---------------------------------------------------------------------------
// Layout: el mismo resultado que agmen.schema.Layout (pack / unpack)
// ---------------------------------------------------------------------------

using agmen::schema::Prim;
using agmen::schema::Type;

void put_u(std::uint8_t* p, std::uint64_t v, std::size_t n) {
    for (std::size_t k = 0; k < n; ++k) p[k] = static_cast<std::uint8_t>(v >> (8 * k));
}
std::uint64_t get_u(const std::uint8_t* p, std::size_t n) {
    std::uint64_t v = 0;
    for (std::size_t k = 0; k < n; ++k) v |= static_cast<std::uint64_t>(p[k]) << (8 * k);
    return v;
}

struct Range {
    long long lo;
    unsigned long long hi;
};

Range int_range(Prim p) {
    switch (p) {
        case Prim::I8: return {-128, 127};
        case Prim::U8: return {0, 255};
        case Prim::I16: return {-32768, 32767};
        case Prim::U16: return {0, 65535};
        case Prim::I32: return {-2147483648LL, 2147483647ULL};
        case Prim::U32: return {0, 4294967295ULL};
        case Prim::U64: return {0, std::numeric_limits<unsigned long long>::max()};
        default: return {std::numeric_limits<long long>::min(), std::numeric_limits<long long>::max()};
    }
}

bool is_unsigned(Prim p) { return p == Prim::U8 || p == Prim::U16 || p == Prim::U32 || p == Prim::U64; }

// Dónde va el campo ("pose.q[3]"), solo se arma si hay un error.
struct Where {
    const Where* parent = nullptr;
    const std::string* name = nullptr;
    long index = -1;

    [[nodiscard]] std::string str() const {
        std::string head = parent != nullptr ? parent->str() : std::string();
        if (name != nullptr) return head.empty() ? *name : head + "." + *name;
        if (index >= 0) return head + "[" + std::to_string(index) + "]";
        return head;
    }
};

class PyLayout {
public:
    explicit PyLayout(const std::string& text) : l_(text) {
        if (!l_.fixed()) {   // arrow: lo codifica agmen.arrow (Python), no este Layout
            throw agmen::schema::SchemaError("'" + l_.name() + "' tiene campos de tamaño variable: va en arrow");
        }
        // Nombres de los campos como objetos de Python, una sola vez.
        keys_.resize(l_.types().size());
        for (std::uint32_t i = 0; i < l_.types().size(); ++i) {
            for (const auto& f : l_.type(i).fields) {
                PyObject* k = PyUnicode_FromStringAndSize(f.name.data(), static_cast<Py_ssize_t>(f.name.size()));
                if (k == nullptr) throw nb::python_error();
                PyUnicode_InternInPlace(&k);
                keys_[i].push_back(nb::steal(k));
            }
        }
    }

    [[nodiscard]] const std::string& text() const { return l_.text(); }
    [[nodiscard]] std::uint64_t schema_id() const { return l_.schema_id(); }
    [[nodiscard]] std::size_t size() const { return l_.size(); }
    [[nodiscard]] const std::string& name() const { return l_.name(); }

    nb::bytes pack(nb::handle value) const {
        std::string out(l_.size(), '\0');
        const Where top;
        write(l_.root(), value.ptr(), reinterpret_cast<std::uint8_t*>(out.data()), top);
        return nb::bytes(out.data(), out.size());
    }

    nb::object unpack(nb::handle buf, std::size_t offset) const {
        Py_buffer view;
        if (PyObject_GetBuffer(buf.ptr(), &view, PyBUF_SIMPLE) != 0) throw nb::python_error();
        const auto len = static_cast<std::size_t>(view.len);
        if (offset > len || len - offset < l_.size()) {
            PyBuffer_Release(&view);
            schema_error("'" + l_.name() + "' necesita " + std::to_string(l_.size()) + " bytes desde el byte " +
                         std::to_string(offset) + " y el payload mide " + std::to_string(len));
        }
        PyObject* r = nullptr;
        try {
            r = read(l_.root(), static_cast<const std::uint8_t*>(view.buf) + offset);
        } catch (...) {
            PyBuffer_Release(&view);
            throw;
        }
        PyBuffer_Release(&view);
        return nb::steal(r);
    }

private:
    agmen::schema::Layout l_;
    std::vector<std::vector<nb::object>> keys_;

    void write(std::uint32_t ti, PyObject* v, std::uint8_t* at, const Where& where) const {
        const Type& t = l_.type(ti);
        switch (t.kind) {
            case Type::Kind::String:
            case Type::Kind::Bytes:
            case Type::Kind::List:
            case Type::Kind::Tensor:
                return;  // no llega aquí: el constructor solo acepta esquemas de tamaño fijo
            case Type::Kind::Prim:
                write_prim(t.prim, v, at, where);
                return;
            case Type::Kind::Array: {
                const Py_ssize_t n = PySequence_Size(v);
                if (n < 0) {
                    PyErr_Clear();
                    schema_error("'" + where.str() + "' debe ser una lista de " + std::to_string(t.count) + " elementos");
                }
                if (static_cast<std::size_t>(n) != t.count) {
                    schema_error("'" + where.str() + "' debe tener " + std::to_string(t.count) + " elementos, tiene " +
                                 std::to_string(n));
                }
                const std::size_t step = l_.type(t.elem).size;
                for (std::uint32_t k = 0; k < t.count; ++k) {
                    PyObject* item = PySequence_GetItem(v, k);
                    if (item == nullptr) throw nb::python_error();
                    try {
                        const Where w{&where, nullptr, static_cast<long>(k)};
                        write(t.elem, item, at + k * step, w);
                    } catch (...) {
                        Py_DECREF(item);
                        throw;
                    }
                    Py_DECREF(item);
                }
                return;
            }
            case Type::Kind::Struct: {
                const bool is_dict = PyDict_Check(v) != 0;
                for (std::size_t k = 0; k < t.fields.size(); ++k) {
                    const auto& f = t.fields[k];
                    PyObject* key = keys_[ti][k].ptr();
                    PyObject* fv = nullptr;
                    if (is_dict) {
                        fv = PyDict_GetItem(v, key);   // prestada
                        Py_XINCREF(fv);
                    } else {
                        fv = PyObject_GetAttr(v, key);
                        if (fv == nullptr) PyErr_Clear();
                    }
                    if (fv == nullptr) schema_error("falta el campo '" + f.name + "' para '" + l_.name() + "'");
                    try {
                        const Where w{&where, &f.name, -1};
                        write(f.type, fv, at + f.offset, w);
                    } catch (...) {
                        Py_DECREF(fv);
                        throw;
                    }
                    Py_DECREF(fv);
                }
                return;
            }
        }
    }

    static void write_prim(Prim p, PyObject* v, std::uint8_t* at, const Where& where) {
        switch (p) {
            case Prim::Bool: {
                const int b = PyObject_IsTrue(v);
                if (b < 0) throw nb::python_error();
                at[0] = static_cast<std::uint8_t>(b);
                return;
            }
            case Prim::F32:
            case Prim::F64: {
                const double d = PyFloat_AsDouble(v);
                if (d == -1.0 && PyErr_Occurred() != nullptr) throw nb::python_error();
                if (p == Prim::F64) {
                    put_u(at, std::bit_cast<std::uint64_t>(d), 8);
                } else {
                    const auto f = static_cast<float>(d);
                    if (std::isinf(f) && std::isfinite(d)) {
                        raise(PyExc_OverflowError, "'" + where.str() + "' = " + std::to_string(d) + " no cabe en un f32");
                    }
                    put_u(at, std::bit_cast<std::uint32_t>(f), 4);
                }
                return;
            }
            default: {
                PyObject* i = PyNumber_Long(v);   // como int(value) en Python
                if (i == nullptr) throw nb::python_error();
                const std::size_t n = agmen::schema::prim_size(p);
                const Range r = int_range(p);
                if (is_unsigned(p)) {
                    const unsigned long long u = PyLong_AsUnsignedLongLong(i);
                    const bool bad = (u == static_cast<unsigned long long>(-1) && PyErr_Occurred() != nullptr) || u > r.hi;
                    Py_DECREF(i);
                    if (bad) {
                        PyErr_Clear();
                        schema_error("'" + where.str() + "' no cabe en " + std::string(agmen::schema::prim_name(p)) +
                                     " (de 0 a " + std::to_string(r.hi) + ")");
                    }
                    put_u(at, u, n);
                } else {
                    int overflow = 0;
                    const long long s = PyLong_AsLongLongAndOverflow(i, &overflow);
                    Py_DECREF(i);
                    if (s == -1 && PyErr_Occurred() != nullptr) throw nb::python_error();
                    if (overflow != 0 || s < r.lo || s > static_cast<long long>(r.hi)) {
                        schema_error("'" + where.str() + "' no cabe en " + std::string(agmen::schema::prim_name(p)) +
                                     " (de " + std::to_string(r.lo) + " a " + std::to_string(r.hi) + ")");
                    }
                    put_u(at, static_cast<std::uint64_t>(s), n);
                }
                return;
            }
        }
    }

    PyObject* read(std::uint32_t ti, const std::uint8_t* at) const {
        const Type& t = l_.type(ti);
        switch (t.kind) {
            case Type::Kind::String:
            case Type::Kind::Bytes:
            case Type::Kind::List:
            case Type::Kind::Tensor:
                Py_RETURN_NONE;  // no llega aquí: el constructor solo acepta esquemas de tamaño fijo
            case Type::Kind::Prim:
                return read_prim(t.prim, at);
            case Type::Kind::Array: {
                PyObject* list = PyList_New(static_cast<Py_ssize_t>(t.count));
                if (list == nullptr) throw nb::python_error();
                const std::size_t step = l_.type(t.elem).size;
                for (std::uint32_t k = 0; k < t.count; ++k) {
                    PyObject* item = nullptr;
                    try {
                        item = read(t.elem, at + k * step);
                    } catch (...) {
                        Py_DECREF(list);
                        throw;
                    }
                    PyList_SetItem(list, k, item);   // se queda con la referencia
                }
                return list;
            }
            case Type::Kind::Struct: {
                PyObject* d = PyDict_New();
                if (d == nullptr) throw nb::python_error();
                for (std::size_t k = 0; k < t.fields.size(); ++k) {
                    const auto& f = t.fields[k];
                    PyObject* item = nullptr;
                    try {
                        item = read(f.type, at + f.offset);
                    } catch (...) {
                        Py_DECREF(d);
                        throw;
                    }
                    const int rc = PyDict_SetItem(d, keys_[ti][k].ptr(), item);
                    Py_DECREF(item);
                    if (rc != 0) {
                        Py_DECREF(d);
                        throw nb::python_error();
                    }
                }
                return d;
            }
        }
        return nullptr;
    }

    static PyObject* read_prim(Prim p, const std::uint8_t* at) {
        PyObject* r = nullptr;
        switch (p) {
            case Prim::Bool: r = PyBool_FromLong(at[0] != 0 ? 1 : 0); break;
            case Prim::F32: r = PyFloat_FromDouble(static_cast<double>(std::bit_cast<float>(static_cast<std::uint32_t>(get_u(at, 4))))); break;
            case Prim::F64: r = PyFloat_FromDouble(std::bit_cast<double>(get_u(at, 8))); break;
            case Prim::U8:
            case Prim::U16:
            case Prim::U32:
            case Prim::U64: r = PyLong_FromUnsignedLongLong(get_u(at, agmen::schema::prim_size(p))); break;
            default: {
                const std::size_t n = agmen::schema::prim_size(p);
                std::uint64_t u = get_u(at, n);
                if (n < 8 && (u >> (8 * n - 1)) != 0) u |= ~std::uint64_t{0} << (8 * n);   // extender el signo
                r = PyLong_FromLongLong(static_cast<long long>(u));
                break;
            }
        }
        if (r == nullptr) throw nb::python_error();
        return r;
    }
};

// ---------------------------------------------------------------------------
// Sobre → tupla con el orden de los campos de agmen.wire.Envelope
// ---------------------------------------------------------------------------

using EnvTuple = std::tuple<int, int, std::uint64_t, std::uint64_t, std::uint64_t, std::uint64_t, std::int64_t,
                            std::int64_t, std::uint32_t, int, int>;

EnvTuple env_tuple(const agmen::wire::Envelope& e) {
    return {static_cast<int>(e.encoding), e.flags, e.schema_id, e.topic_id, e.source_id, e.seq, e.t_data, e.t_pub,
            e.payload_len, static_cast<int>(e.clock), e.header_len};
}

agmen::TypeInfo type_info(const std::string& name, const std::string& text, std::uint64_t id, std::size_t size,
                          bool arrow) {
    return agmen::TypeInfo{name, text, id, arrow ? 0 : size, arrow};
}

// ---------------------------------------------------------------------------
// Un payload que vive en la memoria compartida del publicador (RFC 0009, D7).
// Python lo ve con el protocolo de buffer (memoryview, numpy.frombuffer) sin
// copiar, de solo lectura. Las vistas que salen de él lo mantienen vivo, así
// que el bloque vuelve al publicador cuando se destruye la última.
// ---------------------------------------------------------------------------

struct ShmPayload {
    agmen::Payload payload;
};

int shm_payload_getbuffer(PyObject* self, Py_buffer* view, int flags) {
    auto* p = nb::inst_ptr<ShmPayload>(self);
    return PyBuffer_FillInfo(view, self, const_cast<std::uint8_t*>(p->payload.data()),
                             static_cast<Py_ssize_t>(p->payload.size()), 1, flags);
}

PyType_Slot shm_payload_slots[] = {
    {Py_bf_getbuffer, reinterpret_cast<void*>(shm_payload_getbuffer)},
    {0, nullptr},
};

/// bytes (lo de siempre) o ShmPayload (memoria compartida, sin copiar).
nb::object payload_object(agmen::Payload p) {
    if (!p.shared()) return nb::bytes(reinterpret_cast<const char*>(p.data()), p.size());
    return nb::cast(ShmPayload{std::move(p)});
}

// ---------------------------------------------------------------------------
// Publisher / Subscriber
// ---------------------------------------------------------------------------

class PyPublisher {
public:
    PyPublisher(const std::string& to, const std::string& name, const std::string& text, std::uint64_t id,
                std::size_t size, bool arrow, const std::string& topic) {
        const NoGil nogil(Inside::Create);
        if (!nogil.released()) throw closing_error("un Publisher");
        core_ = std::make_unique<agmen::PublisherCore>(to, type_info(name, text, id, size, arrow), topic);
    }

    bool publish(nb::handle payload, std::int64_t t_data) {
        // El payload se lee del buffer de Python sin copiarlo aquí (una imagen de
        // varios MB): se copia una vez al armar el paquete, con el GIL suelto.
        Py_buffer view;
        if (PyObject_GetBuffer(payload.ptr(), &view, PyBUF_SIMPLE) != 0) throw nb::python_error();
        try {
            auto& c = live();
            const agmen::wire::ByteView data(static_cast<const std::uint8_t*>(view.buf),
                                             static_cast<std::size_t>(view.len));
            bool ok = false;   // cerrando: no se manda
            {
                const NoGil nogil(Inside::Publish);
                if (nogil.released()) ok = c.publish_payload(data, t_data) == agmen::net::SendStatus::Ok;
            }
            PyBuffer_Release(&view);
            return ok;
        } catch (...) {
            PyBuffer_Release(&view);
            throw;
        }
    }
    void announce() { live().announce(); }
    void heartbeat() {
        auto& c = live();
        const NoGil nogil(Inside::Heartbeat);
        if (nogil.released()) c.heartbeat();
    }
    void close() { core_.reset(); }
    [[nodiscard]] bool closed() const { return !core_; }
    [[nodiscard]] std::uint64_t seq() { return live().seq(); }
    [[nodiscard]] std::uint64_t source_id() { return live().source_id(); }
    [[nodiscard]] std::string topic() { return live().topic(); }
    [[nodiscard]] std::size_t subscribers() { return live().subscribers(); }
    [[nodiscard]] std::uint64_t dropped() { return live().dropped(); }
    [[nodiscard]] std::size_t shm_subscribers() { return live().shm_subscribers(); }
    [[nodiscard]] std::uint64_t shm_fallbacks() { return live().shm_fallbacks(); }
    [[nodiscard]] std::size_t shm_bytes() { return live().shm_bytes(); }

private:
    agmen::PublisherCore& live() {
        if (!core_) throw std::logic_error("el Publisher ya se cerró");
        return *core_;
    }
    std::unique_ptr<agmen::PublisherCore> core_;
};

class PySubscriber {
public:
    PySubscriber(const std::string& where, const std::string& name, const std::string& text, std::uint64_t id,
                 std::size_t size, bool arrow) {
        const NoGil nogil(Inside::Create);
        if (!nogil.released()) throw closing_error("un Subscriber");
        core_ = std::make_unique<agmen::SubscriberCore>(where, type_info(name, text, id, size, arrow));
    }

    /// None, o (sobre, payload, "ip:puerto"). timeout en segundos (None = sin límite).
    /// El payload es bytes o, si llegó por memoria compartida, un ShmPayload.
    /// Cerrando el núcleo (el programa sale), regresa None sin esperar.
    std::optional<std::tuple<EnvTuple, nb::object, std::string>> receive(std::optional<double> timeout) {
        using namespace std::chrono;
        auto& c = live();
        const auto start = steady_clock::now();
        for (;;) {
            milliseconds slice(200);   // Ctrl+C, latidos y el cierre, aunque el timeout sea largo
            if (timeout) {
                const auto left = duration_cast<milliseconds>(duration<double>(*timeout) - (steady_clock::now() - start));
                if (left.count() <= 0) slice = milliseconds(0);
                slice = std::min(slice, left.count() > 0 ? left : milliseconds(0));
            }
            std::optional<agmen::RawSample> s;
            {
                const NoGil nogil(Inside::Receive);
                if (!nogil.released()) return std::nullopt;
                s = c.receive_raw(slice);
            }
            if (s) return std::make_tuple(env_tuple(s->envelope), payload_object(std::move(s->payload)), s->from);
            if (PyErr_CheckSignals() != 0) throw nb::python_error();
            if (timeout && steady_clock::now() - start >= duration<double>(*timeout)) return std::nullopt;
        }
    }

    nb::dict announced() {
        nb::dict d;
        for (const auto& [id, a] : live().announced()) d[nb::int_(id)] = nb::make_tuple(a.topic, a.schema);
        return d;
    }
    void heartbeat() {
        auto& c = live();
        const NoGil nogil(Inside::Heartbeat);
        if (nogil.released()) c.heartbeat();
    }
    void close() { core_.reset(); }
    [[nodiscard]] bool closed() const { return !core_; }
    [[nodiscard]] std::intptr_t fileno() { return live().native_handle(); }
    [[nodiscard]] std::vector<std::intptr_t> filenos() { return live().native_handles(); }
    [[nodiscard]] std::uint16_t port() { return live().local_port(); }
    [[nodiscard]] std::uint16_t tcp_port() { return live().tcp_port(); }
    [[nodiscard]] std::size_t shm_mapped() { return live().shm_mapped(); }

private:
    agmen::SubscriberCore& live() {
        if (!core_) throw std::logic_error("el Subscriber ya se cerró");
        return *core_;
    }
    std::unique_ptr<agmen::SubscriberCore> core_;
};

// ---------------------------------------------------------------------------
// Node
// ---------------------------------------------------------------------------

class PyNode;
std::vector<PyNode*> g_nodes;   // los Node vivos; se toca solo con el GIL

class PyNode {
public:
    explicit PyNode(const std::string& name) : node_(std::make_unique<agmen::Node>(name)) { g_nodes.push_back(this); }
    ~PyNode() { std::erase(g_nodes, this); }
    PyNode(const PyNode&) = delete;
    PyNode& operator=(const PyNode&) = delete;

    /// Del cierre (con el GIL): que spin() vuelva.
    static void stop_all() {
        for (PyNode* p : g_nodes) {
            if (p->node_) p->node_->stop();
        }
    }

    void every(std::int64_t period_ns, nb::handle fn, const std::string& group) {
        auto ref = std::make_shared<PyRef>(fn);
        live().every(std::chrono::nanoseconds(period_ns),
                     [ref] {
                         // Cerrando: ya no se llama a Python.
                         (void)with_python(Inside::Callback, [&ref] {
                             PyObject* r = PyObject_CallNoArgs(ref->o);
                             if (r == nullptr) {
                                 PyErr_WriteUnraisable(ref->o);
                             } else {
                                 Py_DECREF(r);
                             }
                         });
                     },
                     agmen::Group{group});
    }

    void subscribe(const std::string& where, const std::string& name, const std::string& text, std::uint64_t id,
                   std::size_t size, bool arrow, nb::handle fn, std::size_t queue, const std::string& group) {
        auto ref = std::make_shared<PyRef>(fn);
        live().subscribe_raw(
            where, type_info(name, text, id, size, arrow),
            [ref](const agmen::RawSample& s) {
                std::string invalid;
                // Cerrando: ya no se llama a Python.
                (void)with_python(Inside::Callback, [&] {
                    try {
                        // Regresa un texto si el payload no se pudo decodificar.
                        const nb::object r =
                            nb::handle(ref->o)(env_tuple(s.envelope), payload_object(s.payload), s.from);
                        if (nb::isinstance<nb::str>(r)) invalid = nb::cast<std::string>(r);
                    } catch (nb::python_error& e) {
                        e.restore();
                        PyErr_WriteUnraisable(ref->o);
                    }
                });
                // Sin el GIL: el Node lo descarta, lo cuenta y lo avisa.
                if (!invalid.empty()) throw agmen::detail::InvalidPayload(invalid);
            },
            agmen::SubscribeOptions{.queue = queue, .group = agmen::Group{group}});
    }

    void group(const std::string& name) { (void)live().group(name); }

    void spin(std::optional<double> seconds) {
        auto& n = live();
        const NoGil nogil(Inside::Spin);
        if (!nogil.released()) return;   // cerrando: no corre
        if (seconds) {
            n.spin_for(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::duration<double>(*seconds)));
        } else {
            n.spin();
        }
    }

    void stop() { live().stop(); }
    [[nodiscard]] std::string name() { return live().name(); }
    void close() {
        // Se saca con el GIL: el cierre (stop_all) no debe ver un Node a medio destruir.
        std::unique_ptr<agmen::Node> n = std::move(node_);
        // Los destructores de las funciones toman el GIL. Cerrando, desde otro
        // hilo, se destruye con el GIL (ya no corre ninguna función de Python).
        const NoGil nogil(Inside::Close);
        n.reset();
    }

    nb::list timers() {
        nb::list out;
        for (const auto& t : live().timers()) {
            out.append(nb::make_tuple(t.name, t.group, t.period_ns, t.runs, t.late, t.skipped, t.worst_late_ns,
                                      t.worst_run_ns));
        }
        return out;
    }

    nb::list subscriptions() {
        nb::list out;
        for (const auto& s : live().subscriptions()) {
            out.append(nb::make_tuple(s.where, s.type, s.group, s.queue, s.received, s.delivered, s.replaced,
                                      s.dropped, s.worst_run_ns, s.invalid));
        }
        return out;
    }

private:
    agmen::Node& live() {
        if (!node_) throw std::logic_error("el Node ya se cerró");
        return *node_;
    }
    std::unique_ptr<agmen::Node> node_;
};

// ---------------------------------------------------------------------------
// Avisos → sys.stderr
// ---------------------------------------------------------------------------

void set_log_sink(nb::object fn) {
    if (fn.is_none()) {
        agmen::log::set_sink(nullptr);
        return;
    }
    auto ref = std::make_shared<PyRef>(fn);
    agmen::log::set_sink([ref](std::string_view line) {
        const bool sent = Py_IsInitialized() != 0 && with_python(Inside::Log, [&] {
            PyObject* s = PyUnicode_FromStringAndSize(line.data(), static_cast<Py_ssize_t>(line.size()));
            if (s == nullptr) {
                PyErr_Clear();
                return;
            }
            PyObject* r = PyObject_CallFunctionObjArgs(ref->o, s, nullptr);
            Py_DECREF(s);
            if (r == nullptr) {
                PyErr_Clear();
            } else {
                Py_DECREF(r);
            }
        });
        if (!sent) {
            // Cerrando (o ya sin intérprete): directo a stderr, sin tomar el GIL.
            std::fwrite(line.data(), 1, line.size(), stderr);
            std::fputc('\n', stderr);
            std::fflush(stderr);
        }
    });
}

// ---------------------------------------------------------------------------
// El cierre (ver arriba): lo llama agmen en atexit, con el GIL.
// ---------------------------------------------------------------------------

/// Declara el núcleo "cerrando", detiene los spin() y espera hasta `limit_s`
/// segundos a que las llamadas en curso vuelvan a Python. Regresa (qué quedó
/// sin cerrar o "", segundos que sobraron del límite).
std::tuple<std::string, double> shutdown(double limit_s) {
    using namespace std::chrono;
    const auto deadline = steady_clock::now() + duration_cast<steady_clock::duration>(duration<double>(limit_s));
    t_closer = true;
    g_closing.store(true);
    for (;;) {
        // Cada vuelta: un spin() que entró justo antes del cierre pudo empezar después del stop().
        PyNode::stop_all();
        int total = 0;
        for (const auto& c : g_inside) total += c.n.load();
        if (total <= 0) break;
        // Ctrl+C a media espera: no se espera más (el programa ya está saliendo).
        bool interrupted = false;
        if (PyErr_CheckSignals() != 0) {
            PyErr_Clear();
            interrupted = true;
        }
        if (interrupted || steady_clock::now() >= deadline) {
            g_abandoned.store(true);
            // Un hilo que miró g_abandoned antes de que se pusiera puede estar a
            // medio tomar el GIL: si regresáramos, lo tomaría ya en la
            // finalización y abortaría. Se le da el GIL hasta que lo haya tomado
            // y soltado. Esta espera no lleva límite propio: no depende del
            // código del usuario sino del turno del GIL (el que lo tenga lo suelta
            // a más tardar en el siguiente cambio de hilo, cada 5 ms, o al volver
            // de C), y los hilos que llegan después ya ven g_abandoned y no lo
            // piden. Si alguien retiene el GIL para siempre, este hilo tampoco lo
            // recuperaría: la salida se colgaría igual sin esta espera.
            while (g_retaking.load() > 0) {
                const nb::gil_scoped_release nogil;
                std::this_thread::sleep_for(milliseconds(1));
            }
            std::string what;
            for (std::size_t k = 0; k < g_inside.size(); ++k) {
                const int n = g_inside[k].n.load();
                if (n <= 0) continue;
                if (!what.empty()) what += ", ";
                what += std::to_string(n) + " en " + kInsideName[k];
            }
            return {what, 0.0};
        }
        nb::gil_scoped_release nogil;   // que los demás hilos retomen el GIL y salgan de C++
        std::this_thread::sleep_for(milliseconds(1));
    }
    const double left = duration<double>(deadline - steady_clock::now()).count();
    return {std::string(), left > 0 ? left : 0.0};
}

/// En el hijo de un fork (os.register_at_fork): solo existe el hilo que hizo
/// fork, así que nada está "dentro" del núcleo y el cierre empieza de cero.
void after_fork_child() {
    for (auto& c : g_inside) c.n.store(0);
    g_retaking.store(0);
    g_closing.store(false);
    g_abandoned.store(false);
    t_closer = false;
}

}  // namespace

NB_MODULE(_core, m) {
    m.doc() = "El núcleo de C++ de Agmen (RFC 0007). Lo usa agmen por dentro.";
    // Los esquemas de los tipos (clases de Python) viven hasta que Python se
    // cierra; no son fugas, así que no se avisa de ellos al salir.
    nb::set_leak_warnings(false);
    m.attr("VERSION") = std::string(agmen::kVersion);

    m.def("set_errors", [](nb::handle schema_err, nb::handle wire_err) {
        Py_XINCREF(schema_err.ptr());
        Py_XINCREF(wire_err.ptr());
        g_schema_error = schema_err.ptr();
        g_wire_error = wire_err.ptr();
    });
    nb::register_exception_translator([](const std::exception_ptr& p, void*) {
        try {
            std::rethrow_exception(p);
        } catch (const agmen::wire::WireError& e) {
            PyErr_SetString(g_wire_error != nullptr ? g_wire_error : PyExc_RuntimeError, e.what());
        } catch (const agmen::schema::SchemaError& e) {
            PyErr_SetString(g_schema_error != nullptr ? g_schema_error : PyExc_ValueError, e.what());
        } catch (const agmen::net::NetError& e) {
            PyErr_SetString(PyExc_OSError, e.what());
        }
    });

    nb::class_<PyLayout>(m, "Layout")
        .def(nb::init<const std::string&>(), "text"_a)
        .def_prop_ro("text", &PyLayout::text)
        .def_prop_ro("schema_id", &PyLayout::schema_id)
        .def_prop_ro("size", &PyLayout::size)
        .def_prop_ro("name", &PyLayout::name)
        .def("pack", &PyLayout::pack, "value"_a)
        .def("unpack", &PyLayout::unpack, "payload"_a, "offset"_a = 0);

    nb::class_<PyPublisher>(m, "Publisher")
        .def(nb::init<const std::string&, const std::string&, const std::string&, std::uint64_t, std::size_t, bool,
                      const std::string&>(),
             "to"_a, "name"_a, "text"_a, "schema_id"_a, "size"_a, "arrow"_a, "topic"_a)
        .def("publish", &PyPublisher::publish, "payload"_a, "t_data"_a)
        .def("announce", &PyPublisher::announce)
        .def("heartbeat", &PyPublisher::heartbeat)
        .def("close", &PyPublisher::close)
        .def_prop_ro("closed", &PyPublisher::closed)
        .def_prop_ro("seq", &PyPublisher::seq)
        .def_prop_ro("source_id", &PyPublisher::source_id)
        .def_prop_ro("topic", &PyPublisher::topic)
        .def_prop_ro("subscribers", &PyPublisher::subscribers)
        .def_prop_ro("dropped", &PyPublisher::dropped)
        .def_prop_ro("shm_subscribers", &PyPublisher::shm_subscribers)
        .def_prop_ro("shm_fallbacks", &PyPublisher::shm_fallbacks)
        .def_prop_ro("shm_bytes", &PyPublisher::shm_bytes);

    nb::class_<ShmPayload>(m, "ShmPayload", nb::type_slots(shm_payload_slots),
                           "Un payload en la memoria compartida del publicador (RFC 0009): úsalo con "
                           "memoryview() o numpy.frombuffer(), sin copiar. El bloque vuelve al publicador "
                           "cuando este objeto y sus vistas se destruyen.")
        .def("__len__", [](const ShmPayload& p) { return p.payload.size(); })
        .def("tobytes", [](const ShmPayload& p) {
            return nb::bytes(reinterpret_cast<const char*>(p.payload.data()), p.payload.size());
        });

    nb::class_<PySubscriber>(m, "Subscriber")
        .def(nb::init<const std::string&, const std::string&, const std::string&, std::uint64_t, std::size_t, bool>(),
             "where"_a, "name"_a, "text"_a, "schema_id"_a, "size"_a, "arrow"_a)
        .def("receive", &PySubscriber::receive, "timeout"_a.none())
        .def("announced", &PySubscriber::announced)
        .def("heartbeat", &PySubscriber::heartbeat)
        .def("fileno", &PySubscriber::fileno)
        .def("filenos", &PySubscriber::filenos)
        .def_prop_ro("port", &PySubscriber::port)
        .def_prop_ro("tcp_port", &PySubscriber::tcp_port)
        .def_prop_ro("shm_mapped", &PySubscriber::shm_mapped)
        .def_prop_ro("closed", &PySubscriber::closed)
        .def("close", &PySubscriber::close);

    nb::class_<PyNode>(m, "Node")
        .def(nb::init<const std::string&>(), "name"_a)
        .def("every", &PyNode::every, "period_ns"_a, "fn"_a, "group"_a)
        .def("subscribe", &PyNode::subscribe, "where"_a, "name"_a, "text"_a, "schema_id"_a, "size"_a, "arrow"_a,
             "fn"_a, "queue"_a, "group"_a)
        .def("group", &PyNode::group, "name"_a)
        .def("spin", &PyNode::spin, "seconds"_a.none())
        .def("stop", &PyNode::stop)
        .def("close", &PyNode::close)
        .def_prop_ro("name", &PyNode::name)
        .def("timers", &PyNode::timers)
        .def("subscriptions", &PyNode::subscriptions);

    // --- reloj de Agmen (uno por proceso, compartido con C++) ---
    namespace ck = agmen::clock;
    m.def("clock_mode", [] {
        const auto md = ck::mode();
        return md == ck::Mode::Sim ? "sim" : md == ck::Mode::Replay ? "replay" : "real";
    });
    m.def("clock_domain", [] { return static_cast<int>(ck::domain()); });
    m.def("clock_now_ns", [] { return ck::now_ns(); });
    m.def("clock_mono_ns", [] { return ck::mono_ns(); });
    m.def("clock_mono_jumps", [] {
        const auto r = ck::mono_reading();
        return std::make_tuple(r.t, r.jumped, r.jump_from, r.jump_to);
    });
    m.def("clock_wall_until", [](std::int64_t t) -> std::optional<double> {
        const auto w = ck::wall_until(t);
        if (w == ck::kUnknown) return std::nullopt;
        return static_cast<double>(w.count()) / 1e9;
    });
    m.def("clock_state", [] {
        const auto s = ck::state();
        return std::make_tuple(s.t, s.speed, s.paused, s.received);
    });
    m.def("clock_configure", [](const std::string& mode, bool stepped, bool listen) {
        ck::configure(mode == "sim" ? ck::Mode::Sim : mode == "replay" ? ck::Mode::Replay : ck::Mode::Real, stepped,
                      listen);
    }, "mode"_a, "stepped"_a, "listen"_a);
    m.def("clock_update", [](std::int64_t t, double speed, bool paused) {
        ck::update(agmen::ClockMsg{.t = t, .speed = speed, .paused = paused});
    }, "t"_a, "speed"_a, "paused"_a);

    // El sobre: (campos de agmen.wire.Envelope, payload). Lanza WireError.
    m.def("parse", [](nb::handle buf, bool shm) {
        Py_buffer view;
        if (PyObject_GetBuffer(buf.ptr(), &view, PyBUF_SIMPLE) != 0) throw nb::python_error();
        try {
            const agmen::wire::ByteView data(static_cast<const std::uint8_t*>(view.buf),
                                             static_cast<std::size_t>(view.len));
            const auto [env, payload] = agmen::wire::Envelope::read_from(data, shm);
            nb::object body;
            if (env.encoding == agmen::wire::Encoding::Arrow) {
                // Datos grandes: una vista (memoryview) del paquete, sin copiar (RFC 0008).
                nb::object mv = nb::steal(PyMemoryView_FromObject(buf.ptr()));
                if (!mv.is_valid()) throw nb::python_error();
                body = nb::steal(PySequence_GetSlice(mv.ptr(), static_cast<Py_ssize_t>(env.header_len),
                                                     static_cast<Py_ssize_t>(view.len)));
                if (!body.is_valid()) throw nb::python_error();
            } else {
                body = nb::bytes(reinterpret_cast<const char*>(payload.data()), payload.size());
            }
            auto out = nb::make_tuple(env_tuple(env), body);
            PyBuffer_Release(&view);
            return out;
        } catch (...) {
            PyBuffer_Release(&view);
            throw;
        }
    }, "buf"_a, "shm"_a = false);
    // Memoria compartida (RFC 0009 D6 a′): el tope y de dónde sale. _shm_fake_ram
    // es solo para las pruebas (finge la RAM de la máquina; 0 = la de verdad).
    m.def("shm_limit", [] { return nb::make_tuple(agmen::shm::limit_bytes(), agmen::shm::limit_reason()); });
    m.def("_shm_fake_ram", [](std::uint64_t bytes) { agmen::shm::detail::fake_physical_ram(bytes); }, "bytes"_a);
    m.def("fnv1a64", [](nb::bytes b) { return agmen::wire::fnv1a64(std::string_view(b.c_str(), b.size())); });
    m.def("set_default_node_name", [](const std::string& n) { agmen::hub::set_default_node_name(n); });
    m.def("set_log_sink", &set_log_sink, "fn"_a.none());
    m.def("_shutdown", &shutdown, "limit_s"_a);
    m.def("_after_fork_child", &after_fork_child);
    // ¿Una función de Python llamada desde C++ que la finalización termina a
    // medias se queda dormida (glibc) en vez de tumbar el proceso?
#if defined(__GLIBCXX__)
    m.attr("_PARK_FORCED_UNWIND") = true;
#else
    m.attr("_PARK_FORCED_UNWIND") = false;
#endif
}

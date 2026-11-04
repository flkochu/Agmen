// agmen._core — el núcleo de C++ de Agmen para Python (RFC 0007, nivel 6).
//
// No se usa directo: `import agmen` lo carga si está compilado y, si no, usa la
// versión de Python puro (agmen._backend). Aquí viven:
//
//   Layout       codificar y decodificar el layout compact de un esquema
//   Publisher    PublisherCore: topics, hub, anuncios, seq, sockets
//   Subscriber   SubscriberCore: espera SIN el GIL, revisa Ctrl+C cada 200 ms
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

#include <bit>
#include <cfloat>
#include <chrono>
#include <cmath>
#include <cstring>
#include <limits>
#include <memory>
#include <optional>
#include <string>
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

// Una referencia a un objeto de Python que se puede guardar en C++ y
// destruir desde cualquier hilo (toma el GIL para el conteo de referencias).
struct PyRef {
    PyObject* o = nullptr;
    explicit PyRef(nb::handle h) : o(h.ptr()) { Py_XINCREF(o); }
    PyRef(const PyRef& r) {
        nb::gil_scoped_acquire gil;
        o = r.o;
        Py_XINCREF(o);
    }
    PyRef& operator=(const PyRef&) = delete;
    ~PyRef() {
        if (o == nullptr || Py_IsInitialized() == 0) return;
        nb::gil_scoped_acquire gil;
        Py_DECREF(o);
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

agmen::TypeInfo type_info(const std::string& name, const std::string& text, std::uint64_t id, std::size_t size) {
    return agmen::TypeInfo{name, text, id, size};
}

std::vector<std::uint8_t> buffer_bytes(nb::handle buf) {
    Py_buffer view;
    if (PyObject_GetBuffer(buf.ptr(), &view, PyBUF_SIMPLE) != 0) throw nb::python_error();
    const auto* p = static_cast<const std::uint8_t*>(view.buf);
    std::vector<std::uint8_t> out(p, p + view.len);
    PyBuffer_Release(&view);
    return out;
}

// ---------------------------------------------------------------------------
// Publisher / Subscriber
// ---------------------------------------------------------------------------

class PyPublisher {
public:
    PyPublisher(const std::string& to, const std::string& name, const std::string& text, std::uint64_t id,
                std::size_t size, const std::string& topic)
        : core_(std::make_unique<agmen::PublisherCore>(to, type_info(name, text, id, size), topic)) {}

    bool publish(nb::handle payload, std::int64_t t_data) {
        const auto bytes = buffer_bytes(payload);
        return live().publish_payload(bytes, t_data) == agmen::net::SendStatus::Ok;
    }
    void announce() { live().announce(); }
    void heartbeat() {
        auto& c = live();
        nb::gil_scoped_release nogil;
        c.heartbeat();
    }
    void close() { core_.reset(); }
    [[nodiscard]] bool closed() const { return !core_; }
    [[nodiscard]] std::uint64_t seq() { return live().seq(); }
    [[nodiscard]] std::uint64_t source_id() { return live().source_id(); }
    [[nodiscard]] std::string topic() { return live().topic(); }
    [[nodiscard]] std::size_t subscribers() { return live().subscribers(); }

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
                 std::size_t size)
        : core_(std::make_unique<agmen::SubscriberCore>(where, type_info(name, text, id, size))) {}

    /// None, o (sobre, payload, "ip:puerto"). timeout en segundos (None = sin límite).
    std::optional<std::tuple<EnvTuple, nb::bytes, std::string>> receive(std::optional<double> timeout) {
        using namespace std::chrono;
        auto& c = live();
        const auto start = steady_clock::now();
        for (;;) {
            milliseconds slice(200);   // Ctrl+C y latidos, aunque el timeout sea largo
            if (timeout) {
                const auto left = duration_cast<milliseconds>(duration<double>(*timeout) - (steady_clock::now() - start));
                if (left.count() <= 0) slice = milliseconds(0);
                slice = std::min(slice, left.count() > 0 ? left : milliseconds(0));
            }
            std::optional<agmen::RawSample> s;
            {
                nb::gil_scoped_release nogil;
                s = c.receive_raw(slice);
            }
            if (s) {
                return std::make_tuple(env_tuple(s->envelope),
                                       nb::bytes(reinterpret_cast<const char*>(s->payload.data()), s->payload.size()),
                                       s->from);
            }
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
        nb::gil_scoped_release nogil;
        c.heartbeat();
    }
    void close() { core_.reset(); }
    [[nodiscard]] bool closed() const { return !core_; }
    [[nodiscard]] std::intptr_t fileno() { return live().native_handle(); }
    [[nodiscard]] std::uint16_t port() { return live().local_port(); }

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

class PyNode {
public:
    explicit PyNode(const std::string& name) : node_(std::make_unique<agmen::Node>(name)) {}

    void every(std::int64_t period_ns, nb::handle fn, const std::string& group) {
        PyRef ref(fn);
        live().every(std::chrono::nanoseconds(period_ns),
                     [ref] {
                         nb::gil_scoped_acquire gil;
                         PyObject* r = PyObject_CallNoArgs(ref.o);
                         if (r == nullptr) {
                             PyErr_WriteUnraisable(ref.o);
                         } else {
                             Py_DECREF(r);
                         }
                     },
                     agmen::Group{group});
    }

    void subscribe(const std::string& where, const std::string& name, const std::string& text, std::uint64_t id,
                   std::size_t size, nb::handle fn, std::size_t queue, const std::string& group) {
        PyRef ref(fn);
        live().subscribe_raw(
            where, type_info(name, text, id, size),
            [ref](const agmen::RawSample& s) {
                nb::gil_scoped_acquire gil;
                try {
                    nb::handle(ref.o)(env_tuple(s.envelope),
                                      nb::bytes(reinterpret_cast<const char*>(s.payload.data()), s.payload.size()),
                                      s.from);
                } catch (nb::python_error& e) {
                    e.restore();
                    PyErr_WriteUnraisable(ref.o);
                }
            },
            agmen::SubscribeOptions{.queue = queue, .group = agmen::Group{group}});
    }

    void group(const std::string& name) { (void)live().group(name); }

    void spin(std::optional<double> seconds) {
        auto& n = live();
        nb::gil_scoped_release nogil;
        if (seconds) {
            n.spin_for(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::duration<double>(*seconds)));
        } else {
            n.spin();
        }
    }

    void stop() { live().stop(); }
    [[nodiscard]] std::string name() { return live().name(); }
    void close() {
        nb::gil_scoped_release nogil;   // los destructores de las funciones toman el GIL
        node_.reset();
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
                                      s.dropped, s.worst_run_ns));
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
        if (Py_IsInitialized() == 0) return;
        nb::gil_scoped_acquire gil;
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
        .def(nb::init<const std::string&, const std::string&, const std::string&, std::uint64_t, std::size_t,
                      const std::string&>(),
             "to"_a, "name"_a, "text"_a, "schema_id"_a, "size"_a, "topic"_a,
             nb::call_guard<nb::gil_scoped_release>())
        .def("publish", &PyPublisher::publish, "payload"_a, "t_data"_a)
        .def("announce", &PyPublisher::announce)
        .def("heartbeat", &PyPublisher::heartbeat)
        .def("close", &PyPublisher::close)
        .def_prop_ro("closed", &PyPublisher::closed)
        .def_prop_ro("seq", &PyPublisher::seq)
        .def_prop_ro("source_id", &PyPublisher::source_id)
        .def_prop_ro("topic", &PyPublisher::topic)
        .def_prop_ro("subscribers", &PyPublisher::subscribers);

    nb::class_<PySubscriber>(m, "Subscriber")
        .def(nb::init<const std::string&, const std::string&, const std::string&, std::uint64_t, std::size_t>(),
             "where"_a, "name"_a, "text"_a, "schema_id"_a, "size"_a, nb::call_guard<nb::gil_scoped_release>())
        .def("receive", &PySubscriber::receive, "timeout"_a.none())
        .def("announced", &PySubscriber::announced)
        .def("heartbeat", &PySubscriber::heartbeat)
        .def("fileno", &PySubscriber::fileno)
        .def_prop_ro("port", &PySubscriber::port)
        .def_prop_ro("closed", &PySubscriber::closed)
        .def("close", &PySubscriber::close);

    nb::class_<PyNode>(m, "Node")
        .def(nb::init<const std::string&>(), "name"_a)
        .def("every", &PyNode::every, "period_ns"_a, "fn"_a, "group"_a)
        .def("subscribe", &PyNode::subscribe, "where"_a, "name"_a, "text"_a, "schema_id"_a, "size"_a, "fn"_a,
             "queue"_a, "group"_a)
        .def("group", &PyNode::group, "name"_a)
        .def("spin", &PyNode::spin, "seconds"_a.none())
        .def("stop", &PyNode::stop)
        .def("close", &PyNode::close)
        .def_prop_ro("name", &PyNode::name)
        .def("timers", &PyNode::timers)
        .def("subscriptions", &PyNode::subscriptions);

    // --- reloj de Agmen (uno por proceso, compartido con C++) ---
    namespace ck = agmen::clock;
    m.def("clock_mode", [] { return ck::mode() == ck::Mode::Sim ? "sim" : "real"; });
    m.def("clock_domain", [] { return static_cast<int>(ck::domain()); });
    m.def("clock_now_ns", [] { return ck::now_ns(); });
    m.def("clock_mono_ns", [] { return ck::mono_ns(); });
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
        ck::configure(mode == "sim" ? ck::Mode::Sim : ck::Mode::Real, stepped, listen);
    }, "mode"_a, "stepped"_a, "listen"_a);
    m.def("clock_update", [](std::int64_t t, double speed, bool paused) {
        ck::update(agmen::ClockMsg{.t = t, .speed = speed, .paused = paused});
    }, "t"_a, "speed"_a, "paused"_a);

    // El sobre: (campos de agmen.wire.Envelope, payload). Lanza WireError.
    m.def("parse", [](nb::handle buf) {
        Py_buffer view;
        if (PyObject_GetBuffer(buf.ptr(), &view, PyBUF_SIMPLE) != 0) throw nb::python_error();
        try {
            const agmen::wire::ByteView data(static_cast<const std::uint8_t*>(view.buf),
                                             static_cast<std::size_t>(view.len));
            const auto [env, payload] = agmen::wire::Envelope::read_from(data);
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
    });
    m.def("fnv1a64", [](nb::bytes b) { return agmen::wire::fnv1a64(std::string_view(b.c_str(), b.size())); });
    m.def("set_default_node_name", [](const std::string& n) { agmen::hub::set_default_node_name(n); });
    m.def("set_log_sink", &set_log_sink, "fn"_a.none());
}

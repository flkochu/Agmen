#include "agmen/wire.hpp"

#include <chrono>
#include <cstdio>
#include <random>

namespace agmen::wire {

namespace {

std::string hex64(std::uint64_t v) {
    char buf[19];
    std::snprintf(buf, sizeof buf, "0x%016llx", static_cast<unsigned long long>(v));
    return buf;
}

std::string printable(ByteView bytes) {
    std::string out;
    for (std::uint8_t b : bytes) {
        if (b >= 0x20 && b < 0x7f) {
            out += static_cast<char>(b);
        } else {
            char esc[5];
            std::snprintf(esc, sizeof esc, "\\x%02x", b);
            out += esc;
        }
    }
    return out;
}

constexpr std::uint8_t kKnownFlags = kFlagShm | kFlagFin;

}  // namespace

void Envelope::write_to(Bytes& out) const {
    out.insert(out.end(), kMagic.begin(), kMagic.end());
    out.push_back(kVersion);
    put_le(out, static_cast<std::uint16_t>(kEnvelopeLen));
    out.push_back(static_cast<std::uint8_t>(encoding));
    out.push_back(flags);
    put_le(out, schema_id);
    put_le(out, topic_id);
    put_le(out, source_id);
    put_le(out, seq);
    put_le(out, static_cast<std::uint64_t>(t_data));
    put_le(out, static_cast<std::uint64_t>(t_pub));
    put_le(out, payload_len);
    out.push_back(static_cast<std::uint8_t>(clock));
    put_padding(out, 3);  // reservado
}

std::pair<Envelope, ByteView> Envelope::read_from(ByteView buf) {
    const auto too_short = [&] {
        return WireError(WireError::Kind::TooShort,
                         "paquete demasiado corto: " + std::to_string(buf.size()) +
                             " bytes, se necesitan al menos " + std::to_string(kEnvelopeLen));
    };
    if (buf.size() < 4) throw too_short();
    if (buf[0] != kMagic[0] || buf[1] != kMagic[1] || buf[2] != kMagic[2]) {
        throw WireError(WireError::Kind::BadMagic,
                        "magic inválido \"" + printable(buf.first(3)) +
                            "\": el paquete no viene de un nodo de Agmen");
    }
    if (buf[3] == 0) {
        throw WireError(WireError::Kind::UnsupportedVersion,
                        "mensaje del formato v0 (Agmen 0.1–0.3): actualiza el nodo que lo manda "
                        "a Agmen 0.4 o posterior (formato v1)");
    }
    if (buf[3] != kVersion) {
        throw WireError(WireError::Kind::UnsupportedVersion,
                        "versión de protocolo " + std::to_string(buf[3]) +
                            " no soportada (este nodo habla la versión " +
                            std::to_string(kVersion) + ")");
    }
    if (buf.size() < kEnvelopeLen) throw too_short();

    Envelope env;
    env.header_len = get_le<std::uint16_t>(buf, 4);
    const std::uint8_t encoding = buf[6];
    env.flags = buf[7];
    env.schema_id = get_le<std::uint64_t>(buf, 8);
    env.topic_id = get_le<std::uint64_t>(buf, 16);
    env.source_id = get_le<std::uint64_t>(buf, 24);
    env.seq = get_le<std::uint64_t>(buf, 32);
    env.t_data = static_cast<std::int64_t>(get_le<std::uint64_t>(buf, 40));
    env.t_pub = static_cast<std::int64_t>(get_le<std::uint64_t>(buf, 48));
    env.payload_len = get_le<std::uint32_t>(buf, 56);
    env.clock = static_cast<Clock>(buf[60]);

    if (env.header_len < kEnvelopeLen || env.header_len > buf.size()) {
        throw WireError(WireError::Kind::BadHeader,
                        "header_len inválido: " + std::to_string(env.header_len) +
                            " (paquete de " + std::to_string(buf.size()) + " bytes)");
    }
    if (encoding > static_cast<std::uint8_t>(Encoding::Announcement)) {
        throw WireError(WireError::Kind::UnknownEncoding,
                        "encoding desconocido: " + std::to_string(encoding));
    }
    env.encoding = static_cast<Encoding>(encoding);
    if ((env.flags & ~kKnownFlags) != 0) {
        throw WireError(WireError::Kind::UnknownEncoding,
                        "flags desconocidos: " + std::to_string(env.flags));
    }
    if ((env.flags & kFlagShm) != 0) {
        throw WireError(WireError::Kind::Unsupported,
                        "payload en memoria compartida: llega en el nivel 8, este nodo no lo soporta");
    }
    if (env.encoding == Encoding::Arrow) {
        throw WireError(WireError::Kind::Unsupported,
                        "codificación arrow: llega en el nivel 7, este nodo no la soporta");
    }
    // header_len > 64: un sobre más nuevo; saltamos los campos que no conocemos.
    const ByteView payload = buf.subspan(env.header_len);
    if (payload.size() != env.payload_len) {
        throw WireError(WireError::Kind::PayloadLenMismatch,
                        "el sobre dice payload de " + std::to_string(env.payload_len) +
                            " bytes pero llegaron " + std::to_string(payload.size()));
    }
    return {env, payload};
}

std::string type_mismatch_text(std::string_view expected_text, std::uint64_t expected,
                               std::uint64_t got, std::string_view got_detail) {
    std::string text = "TIPO INCOMPATIBLE: se esperaba '" + std::string(expected_text) +
                       "' (schema_id " + hex64(expected) + ") pero llegó schema_id " + hex64(got);
    if (!got_detail.empty()) text += " (" + std::string(got_detail) + ")";
    return text + ". ¿El emisor usa otra versión del mensaje?";
}

std::string payload_size_text(std::string_view name, std::size_t expected, std::size_t got) {
    return "payload de '" + std::string(name) + "' debe medir " + std::to_string(expected) +
           " bytes, llegaron " + std::to_string(got);
}

Bytes encode_announcement(std::string_view schema_text, std::string_view topic, Envelope env) {
    std::string payload(topic);
    payload += '\n';
    payload += schema_text;
    env.header_len = kEnvelopeLen;
    env.encoding = Encoding::Announcement;
    env.flags = 0;
    env.schema_id = fnv1a64(schema_text);
    env.topic_id = topic_id(topic);
    env.t_data = env.t_pub;
    env.payload_len = static_cast<std::uint32_t>(payload.size());
    Bytes out;
    out.reserve(kEnvelopeLen + payload.size());
    env.write_to(out);
    out.insert(out.end(), payload.begin(), payload.end());
    return out;
}

Announcement read_announcement(const Envelope& env, ByteView payload) {
    const std::string_view text(reinterpret_cast<const char*>(payload.data()), payload.size());
    const auto nl = text.find('\n');
    const auto bad = [](const std::string& why) {
        return WireError(WireError::Kind::BadAnnouncement, "anuncio de esquema inválido: " + why);
    };
    if (nl == std::string_view::npos) throw bad("se esperaba 'topic\\nesquema'");
    Announcement a{std::string(text.substr(0, nl)), std::string(text.substr(nl + 1))};
    if (fnv1a64(a.schema) != env.schema_id) throw bad("el texto no coincide con su schema_id");
    if (topic_id(a.topic) != env.topic_id) throw bad("el topic no coincide con su topic_id");
    const std::string_view schema = a.schema;
    if (!schema.starts_with(kSchemaPrefix) || !schema::check(schema.substr(kSchemaPrefix.size())).ok) {
        throw bad("'" + a.schema + "' no es un esquema canónico");
    }
    return a;
}

void ImuSample::encode_payload(Bytes& out) const {
    for (float v : accel) put_f32(out, v);
    for (float v : gyro) put_f32(out, v);
}

ImuSample ImuSample::decode_payload(ByteView p) {
    if (p.size() != kPayloadLen) {
        throw WireError(WireError::Kind::BadPayloadSize,
                        payload_size_text(kName, kPayloadLen, p.size()));
    }
    return ImuSample{
        .accel = {get_f32(p, 0), get_f32(p, 4), get_f32(p, 8)},
        .gyro = {get_f32(p, 12), get_f32(p, 16), get_f32(p, 20)},
    };
}

std::int64_t now_ns() {
    using namespace std::chrono;
    return static_cast<std::int64_t>(
        duration_cast<nanoseconds>(system_clock::now().time_since_epoch()).count());
}

std::uint64_t random_source_id() {
    // random_device puede ser determinista en algunos compiladores viejos de
    // MinGW; mezclarlo con el reloj evita que dos procesos repitan el id.
    std::random_device rd;
    std::uint64_t v = (static_cast<std::uint64_t>(rd()) << 32) ^ rd();
    v ^= static_cast<std::uint64_t>(
        std::chrono::steady_clock::now().time_since_epoch().count()) * 0x9E3779B97F4A7C15ULL;
    return v == 0 ? 1 : v;
}

}  // namespace agmen::wire

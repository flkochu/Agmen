#include "agmen/wire.hpp"

#include <chrono>
#include <cstdio>

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

}  // namespace

void Header::write_to(Bytes& out) const {
    out.insert(out.end(), kMagic.begin(), kMagic.end());
    out.push_back(kVersion);
    put_le(out, type_id);
    put_le(out, seq);
    put_le(out, stamp_ns);
    put_le(out, payload_len);
}

std::pair<Header, ByteView> Header::read_from(ByteView buf) {
    if (buf.size() < kHeaderLen) {
        throw WireError(WireError::Kind::TooShort,
                        "paquete demasiado corto: " + std::to_string(buf.size()) +
                            " bytes, se necesitan al menos " + std::to_string(kHeaderLen));
    }
    if (buf[0] != kMagic[0] || buf[1] != kMagic[1] || buf[2] != kMagic[2]) {
        throw WireError(WireError::Kind::BadMagic,
                        "magic inválido \"" + printable(buf.first(3)) +
                            "\": el paquete no viene de un nodo de Agmen");
    }
    if (buf[3] != kVersion) {
        throw WireError(WireError::Kind::UnsupportedVersion,
                        "versión de protocolo " + std::to_string(buf[3]) +
                            " no soportada (este nodo habla la versión " +
                            std::to_string(kVersion) + ")");
    }
    const Header header{
        .type_id = get_le<std::uint64_t>(buf, 4),
        .seq = get_le<std::uint64_t>(buf, 12),
        .stamp_ns = get_le<std::uint64_t>(buf, 20),
        .payload_len = get_le<std::uint32_t>(buf, 28),
    };
    const ByteView payload = buf.subspan(kHeaderLen);
    if (payload.size() != header.payload_len) {
        throw WireError(WireError::Kind::PayloadLenMismatch,
                        "el header dice payload de " + std::to_string(header.payload_len) +
                            " bytes pero llegaron " + std::to_string(payload.size()));
    }
    return {header, payload};
}

std::string type_mismatch_text(std::string_view expected_name, std::uint64_t expected,
                               std::uint64_t got) {
    return "TIPO INCOMPATIBLE: se esperaba '" + std::string(expected_name) + "' (type_id " +
           hex64(expected) + ") pero llegó type_id " + hex64(got) +
           ". ¿El publisher usa otra versión del mensaje?";
}

void ImuSample::encode_payload(Bytes& out) const {
    for (float v : accel) put_f32(out, v);
    for (float v : gyro) put_f32(out, v);
}

ImuSample ImuSample::decode_payload(ByteView p) {
    if (p.size() != kPayloadLen) {
        throw WireError(WireError::Kind::BadPayloadSize,
                        "payload de '" + std::string(kName) + "' debe medir " +
                            std::to_string(kPayloadLen) + " bytes, llegaron " +
                            std::to_string(p.size()));
    }
    return ImuSample{
        .accel = {get_f32(p, 0), get_f32(p, 4), get_f32(p, 8)},
        .gyro = {get_f32(p, 12), get_f32(p, 16), get_f32(p, 20)},
    };
}

std::uint64_t now_ns() {
    using namespace std::chrono;
    return static_cast<std::uint64_t>(
        duration_cast<nanoseconds>(system_clock::now().time_since_epoch()).count());
}

}  // namespace agmen::wire

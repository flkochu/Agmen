#include "agmen/hubproto.hpp"

#include "agmen/log.hpp"
#include "agmen/platform.hpp"

#include <mutex>

#include <cstdio>
#include <cstdlib>
#include <stdexcept>

#ifdef _WIN32
#  include <process.h>
#else
#  include <unistd.h>
#endif

namespace agmen::hub {

namespace {

bool is_safe(unsigned char c) {
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' ||
           c == '.' || c == '/' || c == ':' || c == ',' || c == '*' || c == '-';
}

int hex_digit(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

std::string env_or(const char* name, std::string_view fallback) {
    std::string value = platform::env(name);
    return value.empty() ? std::string(fallback) : value;
}

}  // namespace

std::string quote(std::string_view value) {
    static constexpr char kHex[] = "0123456789ABCDEF";
    std::string out;
    for (char ch : value) {
        const auto c = static_cast<unsigned char>(ch);
        if (is_safe(c)) {
            out += ch;
        } else {
            out += '%';
            out += kHex[c >> 4];
            out += kHex[c & 15];
        }
    }
    return out;
}

std::string unquote(std::string_view value) {
    std::string out;
    for (std::size_t i = 0; i < value.size(); ++i) {
        if (value[i] != '%') {
            out += value[i];
            continue;
        }
        if (i + 2 >= value.size()) throw std::invalid_argument("'%' mal escrito");
        const int hi = hex_digit(value[i + 1]);
        const int lo = hex_digit(value[i + 2]);
        if (hi < 0 || lo < 0) throw std::invalid_argument("'%' mal escrito");
        out += static_cast<char>(hi * 16 + lo);
        i += 2;
    }
    return out;
}

std::string line(std::string_view op,
                 const std::vector<std::pair<std::string, std::string>>& fields) {
    std::string out(kPrefix);
    out += ' ';
    out += op;
    for (const auto& [k, v] : fields) {
        out += ' ';
        out += k;
        out += '=';
        out += quote(v);
    }
    return out;
}

bool is_control(wire::ByteView data) {
    return data.size() >= kPrefix.size() &&
           std::string_view(reinterpret_cast<const char*>(data.data()), kPrefix.size()) == kPrefix;
}

std::vector<Line> parse(wire::ByteView data) {
    const std::string_view text(reinterpret_cast<const char*>(data.data()), data.size());
    std::vector<Line> out;
    std::size_t pos = 0;
    while (pos < text.size()) {
        auto end = text.find('\n', pos);
        if (end == std::string_view::npos) end = text.size();
        std::string_view ln = text.substr(pos, end - pos);
        pos = end + 1;
        if (!ln.empty() && ln.back() == '\r') ln.remove_suffix(1);
        if (ln.empty()) continue;
        std::vector<std::string_view> parts;
        std::size_t p = 0;
        while (p <= ln.size()) {
            auto sp = ln.find(' ', p);
            if (sp == std::string_view::npos) sp = ln.size();
            if (sp > p) parts.push_back(ln.substr(p, sp - p));
            p = sp + 1;
        }
        if (parts.size() < 2 || parts[0] != kPrefix) {
            throw std::invalid_argument("no es un mensaje agmen-hub/1");
        }
        Line l{std::string(parts[1]), {}};
        for (std::size_t k = 2; k < parts.size(); ++k) {
            const auto eq = parts[k].find('=');
            if (eq == std::string_view::npos) throw std::invalid_argument("campo sin '='");
            l.fields.emplace(std::string(parts[k].substr(0, eq)), unquote(parts[k].substr(eq + 1)));
        }
        out.push_back(std::move(l));
    }
    return out;
}

void check_topic(std::string_view topic) {
    bool ok = topic.size() >= 2 && topic.front() == '/' && topic.back() != '/';
    for (std::size_t i = 0; ok && i < topic.size(); ++i) {
        const char c = topic[i];
        if (c == '/') {
            ok = i + 1 < topic.size() && topic[i + 1] != '/';
        } else {
            ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
                 c == '_';
        }
    }
    if (!ok) {
        throw std::invalid_argument("nombre de topic inválido '" + std::string(topic) +
                                    "': empieza con '/' y usa letras, dígitos y '_' separados por "
                                    "'/', ej. /imu o /arca9/articulaciones");
    }
}

// --- Modo red (RFC 0011 §3.5): los mismos textos que python/agmen/hubproto.py ---

std::vector<std::string> split_list(std::string_view list) {
    std::vector<std::string> out;
    std::size_t p = 0;
    while (p < list.size()) {
        auto c = list.find(',', p);
        if (c == std::string_view::npos) c = list.size();
        if (c > p) out.emplace_back(list.substr(p, c - p));
        p = c + 1;
    }
    return out;
}

namespace {

bool is_digit(char c) { return c >= '0' && c <= '9'; }

bool all_digits(std::string_view s) {
    if (s.empty()) return false;
    for (char c : s) {
        if (!is_digit(c)) return false;
    }
    return true;
}

std::string lower(std::string_view s) {
    std::string out(s);
    for (auto& c : out) {
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    }
    return out;
}

}  // namespace

void check_machine(std::string_view name) {
    bool ok = !name.empty();
    for (char c : name) {
        ok = ok && ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || is_digit(c) || c == '_' ||
                    c == '-');
    }
    if (!ok) {
        throw std::invalid_argument("'" + std::string(name) +
                                    "' no es un nombre de computadora válido: usa letras (sin acentos), "
                                    "dígitos, '_' o '-', como en [[machine]] name de robot.toml");
    }
}

std::string parse_ipv4(std::string_view text) {
    int parts = 0;
    bool ok = true;
    std::size_t p = 0;
    while (ok) {
        auto dot = text.find('.', p);
        if (dot == std::string_view::npos) dot = text.size();
        const auto part = text.substr(p, dot - p);
        ok = all_digits(part) && part.size() <= 3 && (part == "0" || part.front() != '0') &&
             std::stoi(std::string(part)) <= 255;
        ++parts;
        if (dot == text.size()) break;
        p = dot + 1;
    }
    if (!ok || parts != 4) {
        throw std::invalid_argument("'" + std::string(text) +
                                    "' no es una IP válida: escríbela como cuatro números de 0 a 255, "
                                    "ej. 192.168.1.20");
    }
    return std::string(text);
}

void check_peer_ip(std::string_view ip) {
    (void)parse_ipv4(ip);
    const int first = std::stoi(std::string(ip.substr(0, ip.find('.'))));
    if (ip == kLocalhost) {
        throw std::invalid_argument(
            "127.0.0.1 es esta misma computadora, no otra: pon la IP con la que esta computadora ve a "
            "la otra, ej. 192.168.1.20");
    }
    if (ip == "0.0.0.0" || ip == "255.255.255.255" || (first >= 224 && first <= 239)) {
        throw std::invalid_argument(
            std::string(ip) +
            " no es la IP de una computadora (es una dirección especial o de grupo): pon la IP con "
            "la que esta computadora ve a la otra, ej. 192.168.1.20");
    }
}

std::uint16_t parse_port(std::string_view text, std::string_view what) {
    unsigned long value = 0;
    bool ok = all_digits(text);
    for (std::size_t i = 0; ok && i < text.size(); ++i) {
        value = value * 10 + static_cast<unsigned long>(text[i] - '0');
        ok = value <= 65535;
    }
    if (!ok || value == 0) {
        throw std::invalid_argument(std::string(what) + " '" + std::string(text) +
                                    "' no es válido: es un número de 1 a 65535");
    }
    return static_cast<std::uint16_t>(value);
}

std::pair<std::string, std::uint16_t> parse_hostport(std::string_view text) {
    const auto colon = text.rfind(':');
    if (colon == std::string_view::npos) {
        throw std::invalid_argument("'" + std::string(text) +
                                    "' no es una dirección: escríbela como ip:puerto, ej. "
                                    "192.168.1.20:7500");
    }
    auto ip = parse_ipv4(text.substr(0, colon));
    return {std::move(ip), parse_port(text.substr(colon + 1))};
}

std::vector<std::pair<std::string, std::string>> parse_peers(std::string_view text) {
    std::vector<std::pair<std::string, std::string>> out;
    std::map<std::string, std::string, std::less<>> names;  // en minúsculas → como vino
    std::map<std::string, std::string, std::less<>> ips;    // IP → nombre
    for (const auto& item : split_list(text)) {
        const auto at = item.find('@');
        if (at == std::string::npos) {
            throw std::invalid_argument("'" + item +
                                        "' en peers no es nombre@ip, ej. robot@192.168.1.20");
        }
        std::string name = item.substr(0, at);
        std::string ip = item.substr(at + 1);
        check_machine(name);
        check_peer_ip(ip);
        if (names.count(lower(name)) != 0) {
            throw std::invalid_argument("la computadora '" + name +
                                        "' está dos veces en peers: cada una va una sola vez");
        }
        if (const auto it = ips.find(ip); it != ips.end()) {
            throw std::invalid_argument("'" + it->second + "' y '" + name + "' tienen la misma IP " +
                                        ip + " en peers: cada computadora tiene la suya");
        }
        names.emplace(lower(name), name);
        ips.emplace(ip, name);
        out.emplace_back(std::move(name), std::move(ip));
    }
    return out;
}

std::string format_peers(const std::vector<std::pair<std::string, std::string>>& peers) {
    std::string out;
    for (const auto& [name, ip] : peers) {
        if (!out.empty()) out += ',';
        out += name;
        out += '@';
        out += ip;
    }
    return out;
}

std::string hub_address() { return env_or("AGMEN_HUB", kDefaultHub); }

std::string process_id() {
#ifdef _WIN32
    return std::to_string(_getpid());
#else
    return std::to_string(getpid());
#endif
}

namespace {
std::mutex& name_mu() {
    static std::mutex m;
    return m;
}
std::string& default_name() {
    static std::string n;
    return n;
}
}  // namespace

void set_default_node_name(std::string name) {
    std::lock_guard lock(name_mu());
    default_name() = std::move(name);
}

std::string node_name() {
    std::string fallback;
    {
        std::lock_guard lock(name_mu());
        fallback = default_name();
    }
    return env_or("AGMEN_NODE", fallback.empty() ? "cpp-" + process_id() : fallback);
}

std::string hex16(std::uint64_t v) {
    char buf[17];
    std::snprintf(buf, sizeof buf, "%016llx", static_cast<unsigned long long>(v));
    return buf;
}

Link::Link(std::string id, std::string beat)
    : hub_(net::Address::resolve(hub_address())),
      id_(std::move(id)),
      beat_(std::move(beat)),
      started_(std::chrono::steady_clock::now()) {}

void Link::beat(net::UdpSocket& socket) {
    const wire::ByteView data(reinterpret_cast<const std::uint8_t*>(beat_.data()), beat_.size());
    try {
        (void)socket.send_to(data, hub_);
    } catch (const net::NetError&) {
        // sin hub todavía: se reintenta en el siguiente latido
    }
    last_beat_ = std::chrono::steady_clock::now();
}

void Link::maybe_beat(net::UdpSocket& socket) {
    const auto now = std::chrono::steady_clock::now();
    if (now - last_beat_ < kBeat) return;
    beat(socket);
    if (!replied_ && !warned_ && now - started_ > std::chrono::seconds(2)) {
        warned_ = true;
        log::write("[agmen] ⚠ no hay un agmen hub en esta máquina (" + hub_.to_string() +
                   "): corre tu robot con 'agmen run' o abre 'agmen hub' en otra terminal. Sigo "
                   "intentando cada segundo.");
    }
}

void Link::bye(net::UdpSocket& socket) noexcept {
    try {
        const auto msg = line("bye", {{"id", id_}});
        (void)socket.send_to(
            wire::ByteView(reinterpret_cast<const std::uint8_t*>(msg.data()), msg.size()), hub_);
    } catch (...) {
    }
}

bool Link::handle(wire::ByteView data, std::optional<std::vector<std::string>>& subs,
                  std::optional<std::vector<std::string>>* tcp) {
    if (!is_control(data)) return false;
    std::vector<Line> lines;
    try {
        lines = parse(data);
    } catch (const std::invalid_argument&) {
        return true;  // control mal formado: se ignora
    }
    for (const auto& l : lines) {
        if (l.get("id") != id_) continue;
        replied_ = true;
        if (l.op == "subs") {
            subs = split_list(l.get("to"));
            if (tcp != nullptr) *tcp = split_list(l.get("tcp"));
            error_.clear();
        } else if (l.op == "ok") {
            ++oks_;
            error_.clear();
            const auto it = l.fields.find("peers");
            if (it == l.fields.end()) {
                peers_.reset();
            } else {
                peers_ = split_list(it->second);
            }
        } else if (l.op == "error") {
            error_ = l.get("msg");
        }
    }
    return true;
}

}  // namespace agmen::hub

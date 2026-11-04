#include "agmen/hubproto.hpp"

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
#ifdef _MSC_VER
    char* value = nullptr;
    std::size_t len = 0;
    if (_dupenv_s(&value, &len, name) == 0 && value != nullptr) {
        std::string out(value);
        std::free(value);
        if (!out.empty()) return out;
    }
    return std::string(fallback);
#else
    const char* value = std::getenv(name);
    return value != nullptr && *value != '\0' ? std::string(value) : std::string(fallback);
#endif
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
        std::fprintf(stderr,
                     "[agmen] ⚠ no hay un agmen hub en esta máquina (%s): corre tu robot con "
                     "'agmen run' o abre 'agmen hub' en otra terminal. Sigo intentando cada "
                     "segundo.\n",
                     hub_.to_string().c_str());
        std::fflush(stderr);
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

bool Link::handle(wire::ByteView data, std::optional<std::vector<std::string>>& subs) {
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
            std::vector<std::string> to;
            const std::string list = l.get("to");
            std::size_t p = 0;
            while (p < list.size()) {
                auto c = list.find(',', p);
                if (c == std::string::npos) c = list.size();
                if (c > p) to.push_back(list.substr(p, c - p));
                p = c + 1;
            }
            subs = std::move(to);
            error_.clear();
        } else if (l.op == "ok") {
            error_.clear();
        } else if (l.op == "error") {
            error_ = l.get("msg");
        }
    }
    return true;
}

}  // namespace agmen::hub

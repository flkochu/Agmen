#include "agmen/shm.hpp"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <system_error>
#include <utility>

#include "agmen/platform.hpp"

#ifdef _WIN32
#  include <windows.h>
#else
#  include <fcntl.h>
#  include <signal.h>
#  include <sys/mman.h>
#  include <sys/stat.h>
#  include <unistd.h>
#  ifdef __linux__
#    include <sys/statvfs.h>
#  endif
#endif

namespace agmen::shm {

namespace {

constexpr std::size_t kBlockRound = std::size_t{64} << 10;   // los bloques miden múltiplos de 64 KiB

template <class T>
void put(std::uint8_t* at, T v) {
    std::memcpy(at, &v, sizeof v);   // little-endian: los tres sistemas lo son (wire.hpp lo revisa)
}

template <class T>
T get(const std::uint8_t* at) {
    T v{};
    std::memcpy(&v, at, sizeof v);
    return v;
}

std::size_t round_up(std::size_t n, std::size_t to) { return (n + to - 1) / to * to; }

std::string mib(std::size_t bytes) {
    char buf[32];
    std::snprintf(buf, sizeof buf, "%.1f", static_cast<double>(bytes) / (1 << 20));
    return buf;
}

#ifdef _WIN32
std::string os_error(DWORD code) { return std::system_category().message(static_cast<int>(code)); }

std::wstring windows_name(const std::string& name) {
    // "/agm…" → "Local\agm…": un bloque de la sesión del usuario.
    std::wstring out = L"Local\\";
    for (const char c : name.substr(name.empty() || name[0] != '/' ? 0 : 1)) out += static_cast<wchar_t>(c);
    return out;
}

unsigned long long process_id() { return static_cast<unsigned long long>(GetCurrentProcessId()); }
#else
std::string os_error(int code) {
    std::string text = std::error_code(code, std::generic_category()).message();
    if (code == ENOSPC) text += ". En Docker, /dev/shm mide 64 MB: súbelo con --shm-size";
    return text;
}

unsigned long long process_id() { return static_cast<unsigned long long>(getpid()); }

std::filesystem::path registry_dir() {
    std::error_code ec;
    auto base = std::filesystem::temp_directory_path(ec);
    if (ec) base = "/tmp";
    return base / ("agmen-shm-" + std::to_string(getuid()));
}

bool process_alive(long pid) {
    if (pid <= 0) return true;
    if (::kill(static_cast<pid_t>(pid), 0) == 0) return true;
    return errno != ESRCH;   // EPERM: existe, de otro usuario
}
#endif

}  // namespace

bool enabled() { return platform::env("AGMEN_SHM") != "0"; }

namespace {

std::atomic<std::uint64_t> g_fake_ram{0};
constexpr std::uint64_t kMaxPool = std::uint64_t{512} << 20;

// El tope y de dónde sale (D6 a′).
std::pair<std::uint64_t, const char*> limit() {
    // AGMEN_SHM_MB acepta fracciones ("0.5"), como en Python.
    const std::string raw = platform::env("AGMEN_SHM_MB");
    char* end = nullptr;
    const double mb = raw.empty() ? 0.0 : std::strtod(raw.c_str(), &end);
    if (mb > 0 && end != raw.c_str()) {
        return {static_cast<std::uint64_t>(std::min(mb, 1048576.0) * 1048576.0), "AGMEN_SHM_MB"};
    }
    std::pair<std::uint64_t, const char*> out{kMaxPool, "el máximo"};
    if (const auto ram = physical_ram(); ram > 0 && ram / 4 < out.first) out = {ram / 4, "¼ de la RAM"};
    if (const auto dev = devshm_size(); dev > 0 && dev / 2 < out.first) out = {dev / 2, "½ de /dev/shm"};
    return out;
}

}  // namespace

std::uint64_t physical_ram() {
    if (const auto fake = g_fake_ram.load(); fake > 0) return fake;
#ifdef _WIN32
    MEMORYSTATUSEX st{};
    st.dwLength = sizeof st;
    return GlobalMemoryStatusEx(&st) ? static_cast<std::uint64_t>(st.ullTotalPhys) : 0;
#else
    const long pages = ::sysconf(_SC_PHYS_PAGES);
    const long page = ::sysconf(_SC_PAGESIZE);
    if (pages <= 0 || page <= 0) return 0;
    return static_cast<std::uint64_t>(pages) * static_cast<std::uint64_t>(page);
#endif
}

std::uint64_t devshm_size() {
#ifdef __linux__
    struct statvfs st {};
    if (::statvfs("/dev/shm", &st) != 0) return 0;
    return static_cast<std::uint64_t>(st.f_blocks) * static_cast<std::uint64_t>(st.f_frsize);
#else
    return 0;
#endif
}

void detail::fake_physical_ram(std::uint64_t bytes) { g_fake_ram.store(bytes); }

std::size_t limit_bytes() { return static_cast<std::size_t>(limit().first); }

std::string limit_reason() { return limit().second; }

// ---------------------------------------------------------------------------
// Avisos, descriptor y cabecera
// ---------------------------------------------------------------------------

void Record::write_to(wire::Bytes& out) const {
    const auto at = out.size();
    out.resize(at + kRecordLen);
    std::memcpy(out.data() + at, kRecordMagic, 3);
    out[at + 3] = static_cast<std::uint8_t>(op);
    put<std::uint32_t>(out.data() + at + 4, slot);
    put<std::uint64_t>(out.data() + at + 8, generation);
}

Record Record::read(wire::ByteView data) {
    if (data.size() < kRecordLen || std::memcmp(data.data(), kRecordMagic, 3) != 0) {
        throw wire::WireError(wire::WireError::Kind::BadMagic,
                              "aviso de memoria compartida inválido: no empieza con 'AGB'");
    }
    const auto op = data[3];
    if (op < static_cast<std::uint8_t>(Op::Hello) || op > static_cast<std::uint8_t>(Op::Release)) {
        throw wire::WireError(wire::WireError::Kind::Unsupported,
                              "aviso de memoria compartida desconocido: " + std::to_string(op));
    }
    return Record{static_cast<Op>(op), get<std::uint32_t>(data.data() + 4), get<std::uint64_t>(data.data() + 8)};
}

void Descriptor::write_to(wire::Bytes& out) const {
    const auto at = out.size();
    out.resize(at + kDescriptorLen, 0);
    std::uint8_t* p = out.data() + at;
    std::memcpy(p, name.data(), std::min<std::size_t>(name.size(), 31));
    put<std::uint64_t>(p + 32, size);
    put<std::uint64_t>(p + 40, offset);
    put<std::uint64_t>(p + 48, length);
    put<std::uint64_t>(p + 56, generation);
    put<std::uint32_t>(p + 64, slot);
}

Descriptor Descriptor::read(wire::ByteView data) {
    using K = wire::WireError::Kind;
    if (data.size() != kDescriptorLen) {
        throw wire::WireError(K::BadPayloadSize, "el descriptor de memoria compartida debe medir " +
                                                     std::to_string(kDescriptorLen) + " bytes y llegaron " +
                                                     std::to_string(data.size()));
    }
    Descriptor d;
    const auto* p = data.data();
    std::size_t n = 0;
    while (n < 32 && p[n] != 0) ++n;
    d.name.assign(reinterpret_cast<const char*>(p), n);
    for (const char c : d.name) {
        const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '/' ||
                        c == '_' || c == '-';
        if (!ok) throw wire::WireError(K::BadHeader, "nombre de bloque de memoria compartida inválido");
    }
    d.size = get<std::uint64_t>(p + 32);
    d.offset = get<std::uint64_t>(p + 40);
    d.length = get<std::uint64_t>(p + 48);
    d.generation = get<std::uint64_t>(p + 56);
    d.slot = get<std::uint32_t>(p + 64);
    if (d.name.size() < 2 || d.name[0] != '/' || d.offset < kHeaderLen || d.offset > d.size ||
        d.length > d.size - d.offset) {
        throw wire::WireError(K::BadHeader, "descriptor de memoria compartida inválido: '" + d.name + "', " +
                                                std::to_string(d.length) + " bytes en " + std::to_string(d.offset) +
                                                " de un bloque de " + std::to_string(d.size));
    }
    return d;
}

void BlockHeader::write(std::uint8_t* at) const {
    std::memset(at, 0, kHeaderLen);
    std::memcpy(at, kMagic, sizeof kMagic);
    put<std::uint64_t>(at + 8, owner);
    put<std::uint64_t>(at + 16, generation);
    put<std::uint64_t>(at + 24, length);
    put<std::uint64_t>(at + 32, size);
}

BlockHeader BlockHeader::read(const std::uint8_t* at) {
    if (std::memcmp(at, kMagic, sizeof kMagic) != 0) {
        throw ShmError("el bloque no tiene la marca de Agmen (AGMSHM1)");
    }
    return BlockHeader{get<std::uint64_t>(at + 8), get<std::uint64_t>(at + 16), get<std::uint64_t>(at + 24),
                       get<std::uint64_t>(at + 32)};
}

// ---------------------------------------------------------------------------
// Segment: lo único que cambia entre sistemas
// ---------------------------------------------------------------------------

Segment::Segment(Segment&& other) noexcept
    : name_(std::move(other.name_)),
      ptr_(std::exchange(other.ptr_, nullptr)),
      size_(std::exchange(other.size_, 0)),
      handle_(std::exchange(other.handle_, 0)),
      owner_(std::exchange(other.owner_, false)) {}

Segment& Segment::operator=(Segment&& other) noexcept {
    if (this != &other) {
        close();
        name_ = std::move(other.name_);
        ptr_ = std::exchange(other.ptr_, nullptr);
        size_ = std::exchange(other.size_, 0);
        handle_ = std::exchange(other.handle_, 0);
        owner_ = std::exchange(other.owner_, false);
    }
    return *this;
}

Segment::~Segment() { close(); }

#ifdef _WIN32

Segment Segment::create(const std::string& name, std::size_t size) {
    const auto wname = windows_name(name);
    const auto big = static_cast<unsigned long long>(size);
    HANDLE h = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, static_cast<DWORD>(big >> 32),
                                  static_cast<DWORD>(big & 0xffffffffULL), wname.c_str());
    if (h == nullptr) {
        throw ShmError("no se pudo crear la memoria compartida '" + name + "' de " + mib(size) +
                       " MiB: " + os_error(GetLastError()));
    }
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        CloseHandle(h);
        throw ShmError("la memoria compartida '" + name + "' ya existe");
    }
    void* p = MapViewOfFile(h, FILE_MAP_ALL_ACCESS, 0, 0, size);
    if (p == nullptr) {
        const auto code = GetLastError();
        CloseHandle(h);
        throw ShmError("no se pudo mapear la memoria compartida '" + name + "': " + os_error(code));
    }
    Segment s;
    s.name_ = name;
    s.ptr_ = p;
    s.size_ = size;
    s.handle_ = reinterpret_cast<std::intptr_t>(h);
    s.owner_ = true;
    return s;
}

Segment Segment::open(const std::string& name, std::size_t size) {
    const auto wname = windows_name(name);
    HANDLE h = OpenFileMappingW(FILE_MAP_READ, FALSE, wname.c_str());
    if (h == nullptr) throw ShmError("no pude abrir " + name + " (" + os_error(GetLastError()) + ")");
    void* p = MapViewOfFile(h, FILE_MAP_READ, 0, 0, size);
    if (p == nullptr) {
        const auto code = GetLastError();
        CloseHandle(h);
        throw ShmError("no pude mapear " + name + " (" + os_error(code) + ")");
    }
    Segment s;
    s.name_ = name;
    s.ptr_ = p;
    s.size_ = size;
    s.handle_ = reinterpret_cast<std::intptr_t>(h);
    return s;
}

void Segment::close() noexcept {
    if (ptr_ != nullptr) UnmapViewOfFile(ptr_);
    if (handle_ != 0) CloseHandle(reinterpret_cast<HANDLE>(handle_));
    ptr_ = nullptr;
    handle_ = 0;
    size_ = 0;
}

void unlink(const std::string&) noexcept {}

void sweep() noexcept {}

#else

Segment Segment::create(const std::string& name, std::size_t size) {
    const int fd = ::shm_open(name.c_str(), O_CREAT | O_EXCL | O_RDWR, 0600);
    if (fd < 0) {
        throw ShmError("no se pudo crear la memoria compartida '" + name + "': " + os_error(errno));
    }
    auto fail = [&](const char* what, int code) {
        ::close(fd);
        ::shm_unlink(name.c_str());
        throw ShmError(std::string(what) + " '" + name + "' de " + mib(size) + " MiB: " + os_error(code));
    };
    if (::ftruncate(fd, static_cast<off_t>(size)) != 0) fail("no se pudo dar tamaño a la memoria compartida", errno);
#  ifdef __linux__
    // Reservar ya: si /dev/shm se llena, mejor un error aquí que SIGBUS al escribir la imagen.
    const int rc = ::posix_fallocate(fd, 0, static_cast<off_t>(size));
    if (rc != 0 && rc != EINVAL && rc != EOPNOTSUPP && rc != ENOSYS) fail("no hay memoria compartida para", rc);
#  endif
    void* p = ::mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) fail("no se pudo mapear la memoria compartida", errno);
    ::close(fd);
    Segment s;
    s.name_ = name;
    s.ptr_ = p;
    s.size_ = size;
    s.owner_ = true;
    return s;
}

Segment Segment::open(const std::string& name, std::size_t size) {
    const int fd = ::shm_open(name.c_str(), O_RDONLY, 0);
    if (fd < 0) throw ShmError("no pude abrir " + name + " (" + os_error(errno) + ")");
    struct stat st {};
    if (::fstat(fd, &st) != 0 || static_cast<std::uint64_t>(st.st_size) < size) {
        ::close(fd);
        throw ShmError("el bloque " + name + " mide menos de lo que dice el descriptor");
    }
    void* p = ::mmap(nullptr, size, PROT_READ, MAP_SHARED, fd, 0);
    const int code = errno;
    ::close(fd);
    if (p == MAP_FAILED) throw ShmError("no pude mapear " + name + " (" + os_error(code) + ")");
    Segment s;
    s.name_ = name;
    s.ptr_ = p;
    s.size_ = size;
    return s;
}

void Segment::close() noexcept {
    if (ptr_ != nullptr) ::munmap(ptr_, size_);
    if (owner_) ::shm_unlink(name_.c_str());
    ptr_ = nullptr;
    size_ = 0;
    owner_ = false;
}

void unlink(const std::string& name) noexcept { ::shm_unlink(name.c_str()); }

void sweep() noexcept {
    try {
        const auto dir = registry_dir();
        std::error_code ec;
        if (!std::filesystem::is_directory(dir, ec)) return;
        const long me = static_cast<long>(getpid());
        for (const auto& entry : std::filesystem::directory_iterator(dir, ec)) {
            const auto file = entry.path().filename().string();
            const auto dash = file.find('-');
            if (dash == std::string::npos || file.size() < 5 || file.substr(file.size() - 4) != ".txt") continue;
            char* end = nullptr;
            const long pid = std::strtol(file.c_str(), &end, 10);
            if (end != file.c_str() + dash || pid == me || process_alive(pid)) continue;
            std::ifstream in(entry.path());
            std::string name;
            while (std::getline(in, name)) {
                if (name.size() > 4 && name.compare(0, 4, "/agm") == 0) ::shm_unlink(name.c_str());
            }
            in.close();
            std::filesystem::remove(entry.path(), ec);
        }
    } catch (...) {
        // limpiar es una cortesía: nunca tumba al nodo
    }
}

#endif

// ---------------------------------------------------------------------------
// Pool
// ---------------------------------------------------------------------------

Pool::Pool(std::uint64_t owner) : owner_(owner) {
#ifndef _WIN32
    static std::once_flag swept;
    std::call_once(swept, [] { sweep(); });
    try {
        const auto dir = registry_dir();
        std::error_code ec;
        std::filesystem::create_directories(dir, ec);
        std::filesystem::permissions(dir, std::filesystem::perms::owner_all, ec);
        char file[64];
        std::snprintf(file, sizeof file, "%llu-%016llx.txt", process_id(), static_cast<unsigned long long>(owner));
        registry_ = (dir / file).string();
    } catch (...) {
        registry_.clear();
    }
#endif
}

Pool::~Pool() {
    blocks_.clear();
    probe_.reset();
    if (!registry_.empty()) {
        std::error_code ec;
        std::filesystem::remove(registry_, ec);
    }
}

std::string Pool::next_name() {
    // "/agm" + pid + 8 hex del publicador + contador = 24 caracteres (macOS acepta 31).
    char buf[32];
    std::snprintf(buf, sizeof buf, "/agm%08llx%08llx%04x", process_id() & 0xffffffffULL,
                  static_cast<unsigned long long>(owner_ & 0xffffffffULL), counter_ & 0xffffU);
    ++counter_;
    return buf;
}

Segment Pool::create(std::size_t size) {
    for (int attempt = 0;; ++attempt) {
        const auto name = next_name();
        try {
            Segment s = Segment::create(name, size);
            if (!registry_.empty()) {
                std::ofstream out(registry_, std::ios::app);
                out << name << '\n';
            }
            return s;
        } catch (const ShmError&) {
            if (attempt >= 3) throw;   // un nombre que ya existía: se prueba con el siguiente
        }
    }
}

std::optional<Descriptor> Pool::write(std::uint64_t generation, wire::ByteView payload, std::string& why) {
    const std::size_t need = kHeaderLen + payload.size();
    // El bloque libre que se usó más recientemente (sus páginas siguen
    // calientes): así, después de una ráfaga, los demás se quedan sin usar y
    // trim() los libera.
    std::optional<std::size_t> pick;
    for (std::size_t i = 0; i < blocks_.size(); ++i) {
        const auto& b = blocks_[i];
        if (b.holders == 0 && b.segment && b.segment->size() >= need && (!pick || b.order > blocks_[*pick].order)) {
            pick = i;
        }
    }
    if (!pick) {
        const std::size_t size = round_up(need + need / 8, kBlockRound);
        std::optional<std::size_t> small;
        for (std::size_t i = 0; i < blocks_.size() && !small; ++i) {
            if (blocks_[i].holders == 0) small = i;
        }
        if (!small && blocks_.size() >= kMaxBlocks) {
            why = "los suscriptores tienen prestados los " + std::to_string(kMaxBlocks) + " bloques";
            return std::nullopt;
        }
        std::size_t total = bytes();
        if (small && blocks_[*small].segment) total -= blocks_[*small].segment->size();
        if (const auto [cap, reason] = limit(); total + size > cap) {
            why = "se llegó al tope de memoria compartida de este publicador (" + mib(static_cast<std::size_t>(cap)) +
                  " MiB, " + reason + "; cámbialo con AGMEN_SHM_MB)";
            return std::nullopt;
        }
        try {
            auto seg = std::make_unique<Segment>(create(size));
            if (small) {
                blocks_[*small].segment = std::move(seg);   // el viejo se cierra y se borra
                pick = small;
            } else {
                blocks_.push_back(Block{std::move(seg), 0});
                pick = blocks_.size() - 1;
            }
        } catch (const ShmError& e) {
            why = e.what();
            return std::nullopt;
        }
    }
    auto& b = blocks_[*pick];
    touch(b);
    std::memcpy(b.segment->data() + kHeaderLen, payload.data(), payload.size());
    BlockHeader{owner_, generation, payload.size(), b.segment->size()}.write(b.segment->data());
    // Lo escrito se ve antes que el descriptor (que sale después por TCP).
    std::atomic_thread_fence(std::memory_order_release);
    return Descriptor{b.segment->name(), b.segment->size(), kHeaderLen, payload.size(), generation,
                      static_cast<std::uint32_t>(*pick)};
}

std::optional<Descriptor> Pool::probe(std::string& why) {
    if (!probe_) {
        try {
            probe_ = std::make_unique<Segment>(create(kProbeSize));
            BlockHeader{owner_, 0, 0, kProbeSize}.write(probe_->data());
            std::atomic_thread_fence(std::memory_order_release);
        } catch (const ShmError& e) {
            why = e.what();
            return std::nullopt;
        }
    }
    probe_used_ = std::chrono::steady_clock::now();
    return Descriptor{probe_->name(), probe_->size(), kHeaderLen, 0, 0, 0xffffffffU};
}

void Pool::hold(std::uint32_t slot) {
    if (slot < blocks_.size()) ++blocks_[slot].holders;
}

void Pool::release(std::uint32_t slot) {
    if (slot < blocks_.size() && blocks_[slot].holders > 0) {
        if (--blocks_[slot].holders == 0) touch(blocks_[slot]);
    }
}

void Pool::touch(Block& b) {
    b.used = std::chrono::steady_clock::now();
    b.order = ++tick_;
}

std::size_t Pool::trim(std::chrono::steady_clock::time_point now) {
    std::size_t freed = 0;
    for (auto& b : blocks_) {
        if (b.segment && b.holders == 0 && now - b.used >= kIdleFree) {
            freed += b.segment->size();
            b.segment.reset();   // borra el nombre y suelta la memoria
        }
    }
    if (probe_ && now - probe_used_ >= kIdleFree) {
        freed += probe_->size();
        probe_.reset();          // se vuelve a crear si llega otro suscriptor
    }
    // Las ranuras vacías del final se quitan (nadie las nombra: holders == 0).
    while (!blocks_.empty() && !blocks_.back().segment && blocks_.back().holders == 0) blocks_.pop_back();
    if (freed > 0) rewrite_registry();
    return freed;
}

void Pool::rewrite_registry() const {
    if (registry_.empty()) return;
    try {
        std::vector<std::string> names;
        for (const auto& b : blocks_) {
            if (b.segment) names.push_back(b.segment->name());
        }
        if (probe_) names.push_back(probe_->name());
        std::error_code ec;
        if (names.empty()) {
            std::filesystem::remove(registry_, ec);   // create() lo vuelve a escribir
            return;
        }
        const std::string tmp = registry_ + ".tmp";
        {
            std::ofstream out(tmp, std::ios::trunc);
            for (const auto& n : names) out << n << '\n';
        }
        std::filesystem::rename(tmp, registry_, ec);
    } catch (...) {
        // el registro es para limpiar si el proceso muere: nunca tumba al nodo
    }
}

std::size_t Pool::blocks() const {
    return static_cast<std::size_t>(
        std::count_if(blocks_.begin(), blocks_.end(), [](const Block& b) { return b.segment != nullptr; }));
}

std::size_t Pool::bytes() const {
    std::size_t n = 0;
    for (const auto& b : blocks_) n += b.segment ? b.segment->size() : 0;
    return n;
}

std::size_t Pool::busy() const {
    return static_cast<std::size_t>(
        std::count_if(blocks_.begin(), blocks_.end(), [](const Block& b) { return b.holders > 0; }));
}

}  // namespace agmen::shm

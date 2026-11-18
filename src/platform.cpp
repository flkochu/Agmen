#include "agmen/platform.hpp"

#include "agmen/log.hpp"

#include <cstdlib>
#include <thread>

#ifdef _WIN32
#  include <mutex>
#  include <set>
#endif

#if defined(__APPLE__)
#  include <pthread.h>
#  include <pthread/qos.h>
#endif

#ifdef _WIN32
#  include <windows.h>
#  include <mmsystem.h>  // timeBeginPeriod
#  ifndef CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
#    define CREATE_WAITABLE_TIMER_HIGH_RESOLUTION 0x00000002
#  endif
#endif

namespace agmen::platform {

#ifdef _WIN32

namespace {

// Un temporizador por hilo. CREATE_WAITABLE_TIMER_HIGH_RESOLUTION existe desde
// Windows 10 1803; si no está disponible se usa uno normal y se pide al
// sistema resolución de 1 ms.
struct HiResTimer {
    HANDLE handle = nullptr;
    HiResTimer() {
        handle = CreateWaitableTimerExW(nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
                                        TIMER_ALL_ACCESS);
        if (handle == nullptr) {
            handle = CreateWaitableTimerExW(nullptr, nullptr, 0, TIMER_ALL_ACCESS);
            timeBeginPeriod(1);
        }
    }
    ~HiResTimer() {
        if (handle != nullptr) CloseHandle(handle);
    }
    HiResTimer(const HiResTimer&) = delete;
    HiResTimer& operator=(const HiResTimer&) = delete;
};

}  // namespace

void sleep_until(std::chrono::steady_clock::time_point deadline) {
    using namespace std::chrono;
    const auto remaining = deadline - steady_clock::now();
    if (remaining <= nanoseconds::zero()) return;

    thread_local HiResTimer timer;
    if (timer.handle == nullptr) {
        std::this_thread::sleep_until(deadline);
        return;
    }
    // Tiempo relativo negativo, en unidades de 100 ns.
    LARGE_INTEGER due;
    due.QuadPart = -static_cast<LONGLONG>(duration_cast<nanoseconds>(remaining).count() / 100);
    if (SetWaitableTimer(timer.handle, &due, 0, nullptr, nullptr, FALSE)) {
        WaitForSingleObject(timer.handle, INFINITE);
    } else {
        std::this_thread::sleep_until(deadline);
    }
}

void init_console() {
    SetConsoleOutputCP(CP_UTF8);
}

FineTimers::FineTimers() { timeBeginPeriod(1); }
FineTimers::~FineTimers() { timeEndPeriod(1); }

#else

void sleep_until(std::chrono::steady_clock::time_point deadline) {
    std::this_thread::sleep_until(deadline);
}

void init_console() {}

FineTimers::FineTimers() = default;
FineTimers::~FineTimers() = default;

#endif

#if defined(__APPLE__)

PromptThread::PromptThread() {
    qos_class_t cls = QOS_CLASS_UNSPECIFIED;
    int rel = 0;
    if (pthread_get_qos_class_np(pthread_self(), &cls, &rel) == 0) previous_ = static_cast<int>(cls);
    (void)pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
}

PromptThread::~PromptThread() {
    if (previous_ >= 0 && previous_ != static_cast<int>(QOS_CLASS_UNSPECIFIED)) {
        (void)pthread_set_qos_class_self_np(static_cast<qos_class_t>(previous_), 0);
    }
}

#else

PromptThread::PromptThread() = default;
PromptThread::~PromptThread() = default;

#endif

#ifdef _WIN32

namespace {

// Avisa una sola vez por variable (si el valor no se puede leer, cada lectura
// fallaría igual y llenaría la terminal).
void warn_once(const char* name, const char* reason) {
    static std::mutex* mu = new std::mutex();                 // nunca se destruye
    static std::set<std::string>* warned = new std::set<std::string>();
    {
        std::lock_guard<std::mutex> lock(*mu);
        if (!warned->insert(name).second) return;
    }
    std::string line = "agmen: no se pudo leer la variable de entorno ";
    line += name;
    line += " (";
    line += reason;
    line += "); se usa como si no existiera. Revisa su valor: debe ser texto válido "
            "(por ejemplo, vuelve a ponerla desde la terminal o desde Python).";
    agmen::log::write(line);
}

}  // namespace

// En Windows el entorno es UTF-16. _dupenv_s/getenv lo devuelven en la página
// de códigos ANSI (un nombre como "brazo_ñ" llegaba al hub como %F1 y "中" como
// "?"). Aquí se lee en ancho del entorno del proceso y se convierte a UTF-8.
//
// GetEnvironmentVariableW lee el entorno del proceso, que es el que actualizan
// SetEnvironmentVariableW, _putenv_s y _wputenv_s (el CRT también lo cambia en
// el sistema) y el que heredan los hijos (agmen run, Python).
std::string env(const char* name) {
    // El nombre: de UTF-8 a UTF-16 (los nombres de Agmen son ASCII).
    const int name_len = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, name, -1, nullptr, 0);
    if (name_len <= 0) {
        warn_once(name, "el nombre no es UTF-8 válido");
        return std::string();
    }
    std::wstring wname(static_cast<std::size_t>(name_len), L'\0');
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, name, -1, wname.data(), name_len) != name_len) {
        warn_once(name, "el nombre no es UTF-8 válido");
        return std::string();
    }

    // El valor, en UTF-16. Si no cabe, GetEnvironmentVariableW dice cuánto
    // necesita (con el nulo); se reintenta por si otro hilo lo cambia en medio.
    std::wstring wvalue(128, L'\0');
    bool complete = false;
    for (int attempt = 0; attempt < 4 && !complete; ++attempt) {
        const DWORD got = GetEnvironmentVariableW(wname.c_str(), wvalue.data(),
                                                  static_cast<DWORD>(wvalue.size()));
        if (got == 0) return std::string();   // no existe, o está vacía
        if (static_cast<std::size_t>(got) < wvalue.size()) {
            wvalue.resize(static_cast<std::size_t>(got));
            complete = true;
        } else {
            wvalue.assign(static_cast<std::size_t>(got), L'\0');
        }
    }
    if (!complete) {
        warn_once(name, "su valor cambió mientras se leía");
        return std::string();
    }

    // De UTF-16 a UTF-8. WC_ERR_INVALID_CHARS: un sustituto suelto (UTF-16 mal
    // formado) es un error, no un "?" escondido en el valor.
    const int wlen = static_cast<int>(wvalue.size());
    const int bytes = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, wvalue.data(), wlen, nullptr, 0,
                                          nullptr, nullptr);
    if (bytes <= 0) {
        warn_once(name, "su valor no es UTF-16 válido y no se puede pasar a UTF-8");
        return std::string();
    }
    std::string out(static_cast<std::size_t>(bytes), '\0');
    if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, wvalue.data(), wlen, out.data(), bytes, nullptr,
                            nullptr) != bytes) {
        warn_once(name, "su valor no es UTF-16 válido y no se puede pasar a UTF-8");
        return std::string();
    }
    return out;
}

#else

std::string env(const char* name) {
    const char* value = std::getenv(name);
    return value != nullptr ? std::string(value) : std::string();
}

#endif

}  // namespace agmen::platform

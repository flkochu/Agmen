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

// Por qué no se pudo leer una variable. Cada causa tiene su "qué hacer".
enum class EnvProblem { invalid_value, changed_while_reading, invalid_name };

// El nombre tal cual, pero con lo que no es ASCII imprimible como \xNN: un
// nombre que no es UTF-8 no debe llegar crudo al aviso (Python lo lee en UTF-8).
std::string printable(const char* name) {
    static constexpr char kHex[] = "0123456789ABCDEF";
    std::string out;
    for (const char* p = name; *p != '\0'; ++p) {
        const auto c = static_cast<unsigned char>(*p);
        if (c >= 0x20 && c < 0x7F) {
            out += *p;
        } else {
            out += "\\x";
            out += kHex[c >> 4];
            out += kHex[c & 15];
        }
    }
    return out;
}

// Avisa una sola vez por variable y causa (cada lectura fallaría igual y
// llenaría la terminal). Solo se llega aquí si algo salió mal.
void warn_once(const char* name, EnvProblem problem) {
    static std::mutex* mu = new std::mutex();                 // nunca se destruyen:
    static std::set<std::string>* warned = new std::set<std::string>();   // hilos al salir
    const std::string shown = printable(name);
    {
        std::lock_guard<std::mutex> lock(*mu);
        std::string key(1, static_cast<char>('0' + static_cast<int>(problem)));
        key += shown;
        if (!warned->insert(key).second) return;
    }
    std::string line = "[agmen] ⚠ ";
    switch (problem) {
        case EnvProblem::invalid_value:
            line += "la variable de entorno " + shown +
                    " no es texto válido (UTF-16 con un sustituto suelto): Agmen la trata como si no "
                    "existiera. Revisa su valor y vuelve a ponerla (desde la terminal o desde Python).";
            break;
        case EnvProblem::changed_while_reading:
            line += "la variable de entorno " + shown +
                    " cambió mientras se leía: Agmen la trató como vacía esta vez. No cambies el entorno "
                    "desde otro hilo mientras arrancan los nodos.";
            break;
        case EnvProblem::invalid_name:
            line += "el nombre de variable de entorno " + shown +
                    " no es UTF-8 válido: es un error del programa que llama a agmen::platform::env() "
                    "(Agmen la trata como si no existiera).";
            break;
    }
    agmen::log::write(line);
}

// Búferes en la pila para el caso normal: wire::max_message() lee el entorno
// por cada mensaje, y una variable que no existe no debe reservar memoria.
constexpr int kNameStack = 64;                    // caracteres, con el nulo
constexpr DWORD kValueStack = 256;                // caracteres, con el nulo
constexpr int kValueAttempts = 4;                 // lecturas, si el valor no cabe

}  // namespace

// En Windows el entorno es UTF-16. _dupenv_s/getenv lo devuelven en la página
// de códigos ANSI (un nombre como "brazo_ñ" llegaba al hub como %F1). Aquí se
// lee en ancho del entorno del proceso y se convierte a UTF-8.
//
// GetEnvironmentVariableW lee el entorno del proceso, que es el que actualizan
// SetEnvironmentVariableW, _putenv_s y _wputenv_s (el CRT también lo cambia en
// el sistema) y el que heredan los hijos (agmen run, Python).
std::string env(const char* name) {
    if (name == nullptr) return std::string();

    // El nombre: de UTF-8 a UTF-16 (los nombres de Agmen son ASCII y cortos).
    wchar_t name_stack[kNameStack];
    std::wstring name_heap;
    const wchar_t* wname = name_stack;
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, name, -1, name_stack, kNameStack) == 0) {
        if (GetLastError() != ERROR_INSUFFICIENT_BUFFER) {
            warn_once(name, EnvProblem::invalid_name);
            return std::string();
        }
        const int needed = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, name, -1, nullptr, 0);
        if (needed <= 0) {
            warn_once(name, EnvProblem::invalid_name);
            return std::string();
        }
        name_heap.assign(static_cast<std::size_t>(needed), L'\0');
        if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, name, -1, name_heap.data(), needed) != needed) {
            warn_once(name, EnvProblem::invalid_name);
            return std::string();
        }
        wname = name_heap.c_str();
    }

    // El valor, en UTF-16. Si no cabe, GetEnvironmentVariableW dice cuánto
    // necesita (con el nulo) y se lee al heap; se reintenta por si otro hilo
    // lo cambia en medio. Si cabe, devuelve el largo sin el nulo.
    wchar_t value_stack[kValueStack];
    std::wstring value_heap;
    const wchar_t* wvalue = value_stack;
    DWORD got = GetEnvironmentVariableW(wname, value_stack, kValueStack);
    if (got == 0) return std::string();   // no existe, o está vacía
    if (got >= kValueStack) {
        bool complete = false;
        for (int attempt = 1; attempt < kValueAttempts && !complete; ++attempt) {
            value_heap.assign(static_cast<std::size_t>(got), L'\0');
            const DWORD room = static_cast<DWORD>(value_heap.size());
            got = GetEnvironmentVariableW(wname, value_heap.data(), room);
            if (got == 0) return std::string();   // la quitaron mientras se leía
            complete = got < room;
        }
        if (!complete) {
            warn_once(name, EnvProblem::changed_while_reading);
            return std::string();
        }
        wvalue = value_heap.c_str();
    }

    // De UTF-16 a UTF-8. WC_ERR_INVALID_CHARS: un sustituto suelto (UTF-16 mal
    // formado) es un error, no un "?" escondido en el valor.
    const int wlen = static_cast<int>(got);
    const int bytes = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, wvalue, wlen, nullptr, 0, nullptr,
                                          nullptr);
    if (bytes <= 0) {
        warn_once(name, EnvProblem::invalid_value);
        return std::string();
    }
    std::string out(static_cast<std::size_t>(bytes), '\0');
    if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, wvalue, wlen, out.data(), bytes, nullptr,
                            nullptr) != bytes) {
        warn_once(name, EnvProblem::invalid_value);
        return std::string();
    }
    return out;
}

#else

std::string env(const char* name) {
    if (name == nullptr) return std::string();
    const char* value = std::getenv(name);
    return value != nullptr ? std::string(value) : std::string();
}

#endif

}  // namespace agmen::platform

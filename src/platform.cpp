#include "agmen/platform.hpp"

#include <thread>

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

}  // namespace agmen::platform

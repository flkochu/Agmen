// Corre un fuzzer sobre archivos guardados, sin libFuzzer: así las entradas de
// tests/fuzz/regresiones/ se prueban en ctest con cualquier compilador (GCC,
// MSVC, Apple Clang), no solo con Clang. Con libFuzzer se hace lo mismo con
// `./fuzz_wire archivo...` (tests/fuzz/README.md).
//
//   replay_wire tests/fuzz/regresiones/wire
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t* data, std::size_t size);

int main(int argc, char** argv) {
    namespace fs = std::filesystem;
    std::vector<fs::path> files;
    for (int i = 1; i < argc; ++i) {
        const fs::path p(argv[i]);
        if (fs::is_directory(p)) {
            for (const auto& e : fs::directory_iterator(p)) {
                if (e.is_regular_file() && e.path().filename().string().front() != '.') files.push_back(e.path());
            }
        } else if (fs::is_regular_file(p)) {
            files.push_back(p);
        } else {
            std::fprintf(stderr, "no existe: %s\n", argv[i]);
            return 2;
        }
    }
    std::sort(files.begin(), files.end());
    for (const auto& f : files) {
        std::ifstream in(f, std::ios::binary);
        const std::vector<char> raw((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
        std::vector<std::uint8_t> data(raw.begin(), raw.end());
        // Como libFuzzer: el buffer mide exactamente lo que el archivo (ASan ve si se lee de más).
        std::uint8_t* buf = data.empty() ? nullptr : data.data();
        LLVMFuzzerTestOneInput(buf, data.size());
    }
    std::printf("%zu entradas, todas bien\n", files.size());
    return files.empty() ? 1 : 0;
}

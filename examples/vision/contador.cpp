// Un nodo de C++ que recibe la cámara (capítulo 14, RFC 0008).
//
// Imagen viene con Agmen: #include <agmen/msgs.hpp>. Es un tipo arrow (tiene
// string y bytes): en la misma computadora llega por memoria compartida (desde
// 0.11, capítulo 15) y si no, por TCP; el Node lo recibe igual que a los demás.
// Cuenta los píxeles verdes de cada imagen y lo dice una vez por segundo.
//
//   ./build/bin/vision_contador [segundos]      // con 'agmen run examples/vision/robot.toml'
#include <agmen/agmen.hpp>
#include <agmen/msgs.hpp>

#include <cstdio>
#include <cstdlib>

int main(int argc, char** argv) {
    const double seconds = argc > 1 ? std::atof(argv[1]) : 0.0;
    agmen::Node node("contador");
    std::size_t vistas = 0;
    std::size_t compartidas = 0;   // las que llegaron por memoria compartida
    std::size_t verdes = 0;
    std::uint32_t ancho = 0;
    std::uint32_t alto = 0;

    node.subscribe<agmen::msgs::Imagen>("/camara", [&](const agmen::Sample<agmen::msgs::Imagen>& s) {
        const auto& img = s.message;   // img.data: los píxeles, renglón por renglón
        ancho = img.width;
        alto = img.height;
        const std::size_t bpp = img.encoding == "bgr8" || img.encoding == "rgb8" ? 3 : img.encoding == "mono8" ? 1 : 0;
        if (bpp == 0) return;
        const std::size_t step = img.step != 0 ? img.step : img.width * bpp;
        std::size_t n = 0;
        for (std::uint32_t y = 0; y < img.height; ++y) {
            const std::uint8_t* px = img.data.data() + y * step;
            for (std::uint32_t x = 0; x < img.width; ++x, px += bpp) {
                if (bpp == 3 && px[1] > 150 && px[0] < 100 && px[2] < 100) ++n;
            }
        }
        verdes = n;
        ++vistas;
        if ((s.envelope.flags & agmen::wire::kFlagShm) != 0) ++compartidas;
    });

    node.every(std::chrono::seconds(1), [&] {
        std::printf("[contador] %zu imágenes de %ux%u (%zu por memoria compartida) · %zu píxeles verdes en la última\n",
                    vistas, ancho, alto, compartidas, verdes);
        std::fflush(stdout);
    });

    if (seconds > 0) {
        node.spin_for(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::duration<double>(seconds)));
        std::printf("RESULTADO vistas=%zu compartidas=%zu\n", vistas, compartidas);
    } else {
        node.spin();
    }
    return 0;
}

// agmen/geometry.hpp — el cuaternión sin adivinar el orden (RFC 0013 §3.2, reto 3).
//
// Los mensajes estándar guardan las rotaciones con campos con nombre x, y, z, w
// (el orden de ROS, Foxglove e Isaac Lab 3.0). MuJoCo, y otras bibliotecas,
// ponen w primero. Para pasar de uno a otro sin copiar números a mano:
//
//     #include <agmen/geometry.hpp>
//
//     agmen::msgs::Pose pose;
//     pose.orientation = agmen::msgs::from_wxyz(std::span<const double, 4>(d->qpos + 3, 4));  // MuJoCo
//     const std::array<double, 4> q = agmen::msgs::wxyz(pose.orientation);                     // w, x, y, z
//
// Solo cambia el orden: no normaliza ni revisa nada (un cuaternión vacío de
// Quaternionf sigue en NaN).
#pragma once

#include <agmen/msgs.hpp>

#include <array>
#include <span>

namespace agmen::msgs {

/// La rotación como (w, x, y, z): w primero, como MuJoCo.
[[nodiscard]] constexpr std::array<double, 4> wxyz(const Quaternion& q) noexcept { return {q.w, q.x, q.y, q.z}; }

/// La rotación como (w, x, y, z): w primero, como MuJoCo.
[[nodiscard]] constexpr std::array<float, 4> wxyz(const Quaternionf& q) noexcept { return {q.w, q.x, q.y, q.z}; }

/// Un Quaternion a partir de (w, x, y, z): w primero, como MuJoCo.
/// Acepta un std::array<double, 4> o std::span<const double, 4>(puntero, 4).
[[nodiscard]] constexpr Quaternion from_wxyz(std::span<const double, 4> q) noexcept {
    Quaternion out;
    out.w = q[0];
    out.x = q[1];
    out.y = q[2];
    out.z = q[3];
    return out;
}

/// Un Quaternionf a partir de (w, x, y, z): w primero, como MuJoCo.
/// Acepta un std::array<float, 4> o std::span<const float, 4>(puntero, 4).
[[nodiscard]] constexpr Quaternionf from_wxyz(std::span<const float, 4> q) noexcept {
    Quaternionf out;
    out.w = q[0];
    out.x = q[1];
    out.y = q[2];
    out.z = q[3];
    return out;
}

}  // namespace agmen::msgs

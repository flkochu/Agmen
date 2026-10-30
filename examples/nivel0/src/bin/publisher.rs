//! Nivel 0 — publisher.
//!
//! Manda `ImuSample` simulados por UDP a frecuencia fija.
//!
//!   cargo run --release --bin publisher -- --rate 100 --to 127.0.0.1:7400

use std::net::UdpSocket;
use std::time::{Duration, Instant};

use motio_core::wire::{encode, now_ns, ImuSample, Message};
use nivel0::{arg, wants_help};

fn main() -> std::io::Result<()> {
    if wants_help() {
        println!(
            "publisher [--to IP:PUERTO] [--rate HZ] [--count N]\n\
             \n  --to     destino           (default 127.0.0.1:7400)\
             \n  --rate   frecuencia en Hz  (default 100)\
             \n  --count  mensajes a enviar, 0 = infinito (default 0)"
        );
        return Ok(());
    }
    let to: String = arg("to", "127.0.0.1:7400".to_string());
    let rate: f64 = arg("rate", 100.0);
    let count: u64 = arg("count", 0);

    // Puerto 0 = el sistema operativo elige un puerto libre para enviar.
    let socket = UdpSocket::bind("0.0.0.0:0")?;
    socket.connect(&to)?;

    let period = Duration::from_secs_f64(1.0 / rate);
    println!(
        "[publisher] {} (type_id {:#018x}) → {to} a {rate} Hz",
        ImuSample::NAME,
        ImuSample::TYPE_ID
    );

    let start = Instant::now();
    let mut seq: u64 = 0;
    let mut overruns: u64 = 0;
    let mut last_report = Instant::now();

    loop {
        if count != 0 && seq >= count {
            break;
        }

        // IMU simulada: el robot oscila suavemente.
        let t = start.elapsed().as_secs_f32();
        let msg = ImuSample {
            accel: [0.5 * (2.0 * t).sin(), 0.5 * (2.0 * t).cos(), 9.81],
            gyro: [0.0, 0.0, 0.3 * (0.5 * t).sin()],
        };

        // El timestamp se toma justo antes de enviar.
        let packet = encode(&msg, seq, now_ns());
        if let Err(e) = socket.send(&packet) {
            // Con UDP "conectado", si nadie escucha el SO avisa con un error
            // en el siguiente envío. Linux/macOS lo reportan como
            // ConnectionRefused y Windows como ConnectionReset (WSAECONNRESET).
            // No es fatal: seguimos publicando hasta que alguien escuche.
            if !nivel0::is_no_listener(&e) {
                return Err(e);
            }
        }
        seq += 1;

        if last_report.elapsed() >= Duration::from_secs(1) {
            println!("[publisher] enviados={seq} retrasos={overruns}");
            last_report = Instant::now();
        }

        // Programación por "deadline": el mensaje k sale en start + k·periodo.
        // Si hiciéramos sleep(periodo) después de cada envío, el error se
        // acumularía y la frecuencia real sería menor que la pedida.
        let deadline = start + period.mul_f64(seq as f64);
        let now = Instant::now();
        if deadline > now {
            std::thread::sleep(deadline - now);
        } else {
            overruns += 1; // vamos tarde: no dormimos
        }
    }

    println!("[publisher] terminado: {seq} mensajes, {overruns} retrasos");
    Ok(())
}

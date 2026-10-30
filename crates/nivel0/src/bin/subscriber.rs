//! Nivel 0 — subscriber.
//!
//! Recibe `ImuSample` por UDP y cada segundo imprime: mensajes recibidos,
//! perdidos, desordenados, errores y latencia (min/prom/p99/max).
//!
//!   cargo run --release --bin subscriber -- --listen 127.0.0.1:7400

use std::net::UdpSocket;
use std::time::{Duration, Instant};

use nivel0::{arg, fmt_duration, wants_help, SeqEvent, SeqTracker, Window};
use wire::{decode, now_ns, ImuSample, Message};

fn main() -> std::io::Result<()> {
    if wants_help() {
        println!(
            "subscriber [--listen IP:PUERTO] [--verbose 1]\n\
             \n  --listen   dirección donde escuchar (default 127.0.0.1:7400)\
             \n  --verbose  1 = imprime cada mensaje (default 0)"
        );
        return Ok(());
    }
    let listen: String = arg("listen", "127.0.0.1:7400".to_string());
    let verbose: u8 = arg("verbose", 0);

    let socket = UdpSocket::bind(&listen)?;
    // Timeout para poder imprimir estadísticas aunque no lleguen mensajes.
    socket.set_read_timeout(Some(Duration::from_millis(200)))?;

    println!(
        "[subscriber] escuchando {} (type_id {:#018x}) en {listen}",
        ImuSample::NAME,
        ImuSample::TYPE_ID
    );

    // 64 KiB = tamaño máximo de un datagrama UDP.
    let mut buf = vec![0u8; 65_536];
    let mut seq = SeqTracker::default();
    let mut win = Window::default();
    let mut window_start = Instant::now();
    let mut last_msg_at: Option<Instant> = None;
    let mut last_error = String::new();

    loop {
        match socket.recv_from(&mut buf) {
            Ok((n, from)) => {
                let recv_ns = now_ns();
                match decode::<ImuSample>(&buf[..n]) {
                    Ok((h, msg)) => {
                        win.received += 1;
                        last_msg_at = Some(Instant::now());
                        win.add_latency(recv_ns.saturating_sub(h.stamp_ns));
                        match seq.update(h.seq) {
                            SeqEvent::First => println!("[subscriber] primer mensaje de {from}"),
                            SeqEvent::Restart => println!("[subscriber] el publisher se reinició"),
                            SeqEvent::Gap(k) => win.lost += k,
                            SeqEvent::OutOfOrder => win.out_of_order += 1,
                            SeqEvent::InOrder => {}
                        }
                        if verbose != 0 {
                            println!("  seq={} accel={:?} gyro={:?}", h.seq, msg.accel, msg.gyro);
                        }
                    }
                    Err(e) => {
                        win.errors += 1;
                        // Mostramos cada error distinto una vez, con su origen.
                        let text = format!("{e} (desde {from})");
                        if text != last_error {
                            eprintln!("[subscriber] ERROR: {text}");
                            last_error = text;
                        }
                    }
                }
            }
            Err(e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut => {}
            Err(e) => return Err(e),
        }

        if window_start.elapsed() >= Duration::from_secs(1) {
            report(&mut win, last_msg_at);
            win = Window::default();
            window_start = Instant::now();
        }
    }
}

fn report(win: &mut Window, last_msg_at: Option<Instant>) {
    match win.latency_us() {
        Some((min, avg, p99, max)) => println!(
            "[subscriber] rx={:>5} perdidos={:<4} desordenados={:<3} errores={:<3} \
             latencia µs: min={min:.0} prom={avg:.0} p99={p99:.0} max={max:.0}",
            win.received, win.lost, win.out_of_order, win.errors
        ),
        None => match last_msg_at {
            Some(t) => println!(
                "[subscriber] sin mensajes desde hace {} (errores={})",
                fmt_duration(t.elapsed()),
                win.errors
            ),
            None => println!(
                "[subscriber] esperando publisher... (errores={})",
                win.errors
            ),
        },
    }
}

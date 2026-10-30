//! Utilidades compartidas por los binarios del nivel 0.

use std::time::Duration;

/// Lee `--nombre valor` de los argumentos de línea de comandos.
/// (Sin dependencias a propósito; más adelante usaremos `clap`.)
pub fn arg<T: std::str::FromStr>(name: &str, default: T) -> T {
    let args: Vec<String> = std::env::args().collect();
    let flag = format!("--{name}");
    match args.iter().position(|a| *a == flag) {
        Some(i) => match args.get(i + 1).map(|v| v.parse::<T>()) {
            Some(Ok(v)) => v,
            _ => {
                eprintln!("error: valor inválido o faltante para {flag}");
                std::process::exit(2);
            }
        },
        None => default,
    }
}

/// ¿Este error de socket solo significa "nadie está escuchando del otro lado"?
/// Cada sistema operativo lo reporta distinto; aquí se unifica.
pub fn is_no_listener(e: &std::io::Error) -> bool {
    matches!(
        e.kind(),
        std::io::ErrorKind::ConnectionRefused | std::io::ErrorKind::ConnectionReset
    )
}

pub fn wants_help() -> bool {
    std::env::args().any(|a| a == "-h" || a == "--help")
}

/// Estadísticas de una ventana de recepción.
#[derive(Default)]
pub struct Window {
    pub received: u64,
    pub lost: u64,
    pub out_of_order: u64,
    pub errors: u64,
    latencies_ns: Vec<u64>,
}

impl Window {
    pub fn add_latency(&mut self, ns: u64) {
        self.latencies_ns.push(ns);
    }

    /// Devuelve (min, promedio, p99, max) en microsegundos.
    pub fn latency_us(&mut self) -> Option<(f64, f64, f64, f64)> {
        if self.latencies_ns.is_empty() {
            return None;
        }
        self.latencies_ns.sort_unstable();
        let n = self.latencies_ns.len();
        let us = |ns: u64| ns as f64 / 1_000.0;
        let avg = self.latencies_ns.iter().sum::<u64>() as f64 / n as f64 / 1_000.0;
        let p99 = self.latencies_ns[((n as f64 * 0.99) as usize).min(n - 1)];
        Some((
            us(self.latencies_ns[0]),
            avg,
            us(p99),
            us(self.latencies_ns[n - 1]),
        ))
    }
}

/// Seguimiento de números de secuencia para detectar pérdidas.
#[derive(Default)]
pub struct SeqTracker {
    expected: Option<u64>,
}

pub enum SeqEvent {
    /// Llegó el que esperábamos.
    InOrder,
    /// Se saltaron `n` mensajes (se perdieron en la red o en el buffer).
    Gap(u64),
    /// Llegó uno viejo o repetido.
    OutOfOrder,
    /// El publisher volvió a empezar desde 0.
    Restart,
    /// Primer mensaje recibido.
    First,
}

impl SeqTracker {
    pub fn update(&mut self, seq: u64) -> SeqEvent {
        let ev = match self.expected {
            None => SeqEvent::First,
            Some(_) if seq == 0 => SeqEvent::Restart,
            Some(exp) if seq == exp => SeqEvent::InOrder,
            Some(exp) if seq > exp => SeqEvent::Gap(seq - exp),
            Some(_) => return SeqEvent::OutOfOrder, // no movemos `expected`
        };
        self.expected = Some(seq + 1);
        ev
    }
}

pub fn fmt_duration(d: Duration) -> String {
    format!("{:.1}s", d.as_secs_f64())
}

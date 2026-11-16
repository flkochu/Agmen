"""agmen.relojes — la diferencia entre el reloj de esta computadora y el de otra (RFC 0011 §3.6, bloque 5).

Solo se MIDE y se AVISA: ningún `t_data` cambia (sincronizar es del nivel 11). Aquí vive lo que no toca la red:
la fórmula (como NTP: cuatro marcas), la ventana de mediciones, los estados ("medido" o "sin medir" con su
motivo), el campo `relojes` de la línea `red` del hub y los textos de los avisos. Las horas se inyectan (`hora`,
`mono`) para probar sin depender del reloj de la máquina.

Las cuatro marcas viajan en el `ping`/`pong` de agmen-agent/1 (decisión C1):

    laptop → agente   {"op": "ping", "t": …, "reloj": {}}                   (aún sin medición)
                      {"op": "ping", "t": …, "reloj": {"dif": ns, "err": ns}} (su mejor medición vigente)
    agente → laptop   {"op": "pong", "t": …, "reloj": {"rx": ns, "tx": ns}}  (solo si el ping traía "reloj")

`t1` (la laptop manda el ping) y `t4` (le llega el pong) son de la laptop; `rx` y `tx`, del agente. Con un Agmen
anterior del otro lado nada de esto viaja: el agente anterior contesta sin "reloj" (la laptop deja de mandarlo en
esa conexión) y una laptop anterior manda el ping sin "reloj" (el agente contesta el pong de siempre).

Signo: la diferencia de una computadora X es (reloj de X) − (reloj de aquí); positivo = X va ADELANTADO.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .hubproto import check_machine

RELOJ_VENTANA = 8                    # de las últimas N mediciones se usa la de menor ida y vuelta
RELOJ_VIGENTE_S = 10.0               # una medición más vieja ya no cuenta: "sin medir"
RELOJ_UMBRAL_NS = 10_000_000         # |diferencia| > 10 ms: aviso (sesión real)
RELOJ_BIEN_NS = 8_000_000            # … y vuelve a "bien" por debajo de 8 ms
RELOJ_RECORDATORIO_S = 600.0         # mientras siga mal, un recordatorio cada 10 min
RELOJ_IDA_VUELTA_MAX_NS = 2_000_000_000   # una medición con más ida y vuelta (o negativa) se descarta
MARCA_MAX = 2 ** 63                  # una marca es un entero de 0 a 2**63 − 1 (ns desde 1970)
_PENDIENTES_MAX = 16                 # pings sin pong que se recuerdan (los demás ya no cuentan)

# Los motivos de "sin medir" (C6): el código viaja en `red` y en `state`; el texto es para las personas.
MOTIVOS = {
    "esperando": "aún no hay medición",
    "anterior": "el otro lado tiene un Agmen anterior, que no mide el reloj",
    "vieja": f"la última medición tiene más de {RELOJ_VIGENTE_S:g} s",
    "sin_conexion": "sin conexión",
    "sin_dato": "el hub no recibió la medición",
}


def hora_ns() -> int:
    """La hora "de pared" de esta computadora en ns: la misma que usa agmen.clock en modo real para `t_data`
    (time.time_ns), no el reloj monótono."""
    return time.time_ns()


@dataclass(frozen=True)
class Medicion:
    dif_ns: int          # (reloj del otro) − (reloj de aquí)
    err_ns: int          # incertidumbre: la mitad de la ida y vuelta
    ida_vuelta_ns: int
    cuando: float        # monotonic de aquí al medirla


@dataclass(frozen=True)
class Estado:
    """Cómo está la diferencia con una computadora. `medido` False = "sin medir" (nunca se da 0 ni un valor viejo
    como si fuera actual), con `motivo` (un código de MOTIVOS) y, si hubo alguna, `edad_s` de la última."""
    medido: bool
    dif_ns: int = 0
    err_ns: int = 0
    edad_s: float | None = None
    motivo: str = ""


def es_marca(v) -> bool:
    return type(v) is int and 0 <= v < MARCA_MAX


def medir(t1: int, rx: int, tx: int, t4: int, cuando: float = 0.0) -> Medicion | None:
    """Las cuatro marcas → la medición, o None si no sirve (ida y vuelta negativa o de más de 2 s)."""
    ida_vuelta = (t4 - t1) - (tx - rx)
    if ida_vuelta < 0 or ida_vuelta > RELOJ_IDA_VUELTA_MAX_NS or tx < rx:
        return None
    dif = ((rx - t1) + (tx - t4)) // 2
    return Medicion(dif, ida_vuelta // 2, ida_vuelta, cuando)


class _Ventana:
    """Las últimas RELOJ_VENTANA mediciones; la mejor = la de menor ida y vuelta entre las vigentes."""

    def __init__(self) -> None:
        self.ultimas: deque[Medicion] = deque(maxlen=RELOJ_VENTANA)

    def poner(self, m: Medicion) -> None:
        self.ultimas.append(m)

    def mejor(self, ahora: float) -> Medicion | None:
        vigentes = [m for m in self.ultimas if ahora - m.cuando <= RELOJ_VIGENTE_S]
        return min(vigentes, key=lambda m: m.ida_vuelta_ns) if vigentes else None

    def edad(self, ahora: float) -> float | None:
        return max(0.0, ahora - self.ultimas[-1].cuando) if self.ultimas else None


class Medidor:
    """El lado de la laptop (agmen run), por robot: anota t1 al mandar cada ping y mide con cada pong.
    Seguro entre hilos (el de ping, el lector y el puente)."""

    def __init__(self, hora: Callable[[], int] = hora_ns, mono: Callable[[], float] = time.monotonic) -> None:
        self.hora, self.mono = hora, mono
        self._lock = threading.Lock()
        self._ventana = _Ventana()
        self._pendientes: dict[float, tuple[int, bool]] = {}   # t del ping → (t1, ¿llevaba "reloj"?)
        self._anterior_en: object | None = None   # la conexión cuyo agente no mide (no se le manda "reloj")
        self._anterior = False
        self._dicho: set[str] = set()

    def ping(self, canal: object, t: float) -> dict | None:
        """Antes de mandar el ping `t` por `canal`: el valor de su clave "reloj" (None = no llevarla)."""
        with self._lock:
            lleva = canal is not self._anterior_en
            valor = None
            if lleva:
                mejor = self._ventana.mejor(self.mono())
                valor = {"dif": mejor.dif_ns, "err": mejor.err_ns} if mejor is not None else {}
            if len(self._pendientes) >= _PENDIENTES_MAX:
                self._pendientes.pop(next(iter(self._pendientes)))
            self._pendientes[t] = (self.hora(), lleva)
            return valor

    def pong(self, canal: object, msg: dict) -> list[str]:
        """Llegó un pong por `canal`. Devuelve los avisos que hay que decir (cada uno, una sola vez):
        "anterior" (el agente no mide) o "mal_formado" (marcas que no sirven: se ignoran)."""
        t4 = self.hora()
        ahora = self.mono()
        t = msg.get("t")
        with self._lock:
            if type(t) is not float:
                return []
            pendiente = self._pendientes.pop(t, None)
            if pendiente is None:
                return []
            t1, llevaba = pendiente
            reloj = msg.get("reloj")
            if reloj is None:
                if not llevaba:
                    return []
                self._anterior_en, self._anterior = canal, True
                return self._una_vez("anterior")
            rx, tx = (reloj.get("rx"), reloj.get("tx")) if isinstance(reloj, dict) else (None, None)
            if not (es_marca(rx) and es_marca(tx) and tx >= rx):
                return self._una_vez("mal_formado")
            self._anterior = False
            m = medir(t1, rx, tx, t4, ahora)
            if m is not None:   # (una ida y vuelta absurda se descarta sin más: la red estuvo lenta)
                self._ventana.poner(m)
            return []

    def _una_vez(self, clave: str) -> list[str]:
        if clave in self._dicho:
            return []
        self._dicho.add(clave)
        return [clave]

    def estado(self, conectado: bool = True) -> Estado:
        with self._lock:
            ahora = self.mono()
            edad = self._ventana.edad(ahora)
            if not conectado:
                return Estado(False, edad_s=edad, motivo="sin_conexion")
            mejor = self._ventana.mejor(ahora)
            if mejor is not None:
                return Estado(True, mejor.dif_ns, mejor.err_ns, max(0.0, ahora - mejor.cuando))
            if self._anterior:
                return Estado(False, edad_s=edad, motivo="anterior")
            return Estado(False, edad_s=edad, motivo="vieja" if edad is not None else "esperando")


class Remoto:
    """El lado del agente: la laptop le pasa su mejor medición en cada ping (con el signo de la laptop); aquí se
    guarda con el signo cambiado (la diferencia de la LAPTOP respecto a esta computadora)."""

    def __init__(self, mono: Callable[[], float] = time.monotonic) -> None:
        self.mono = mono
        self._lock = threading.Lock()
        self._ultima: Medicion | None = None
        self._anterior = False
        self._perdida = False
        self._mal = False       # ¿está por encima del umbral? (con histéresis, para las líneas de la terminal)
        self._dicho: set[str] = set()

    def ping(self, reloj, trae: bool) -> list[str]:
        """Llegó un ping (`trae`: ¿con la clave "reloj"?; `reloj`: su valor). Devuelve los eventos para la terminal
        del agente: "anterior", "mal_formado" (una vez cada uno), "primera", "mal" y "bien" (al cruzar el umbral)."""
        with self._lock:
            self._perdida = False
            if not trae:
                self._anterior = True
                return self._una_vez("anterior")
            self._anterior = False
            if not isinstance(reloj, dict) or not reloj:
                return [] if isinstance(reloj, dict) else self._una_vez("mal_formado")
            dif, err = reloj.get("dif"), reloj.get("err")
            if not (type(dif) is int and abs(dif) < MARCA_MAX and type(err) is int
                    and 0 <= err <= RELOJ_IDA_VUELTA_MAX_NS // 2):
                return self._una_vez("mal_formado")
            primera = self._ultima is None
            self._ultima = Medicion(-dif, err, 2 * err, self.mono())
            out = ["primera"] if primera else []
            if not self._mal and abs(dif) > RELOJ_UMBRAL_NS and err <= RELOJ_UMBRAL_NS:
                self._mal = True
                out.append("mal")
            elif self._mal and abs(dif) < RELOJ_BIEN_NS:
                self._mal = False
                out.append("bien")
            return out

    def _una_vez(self, clave: str) -> list[str]:
        if clave in self._dicho:
            return []
        self._dicho.add(clave)
        return [clave]

    def perdida(self) -> None:
        with self._lock:
            self._perdida = True

    def estado(self) -> Estado:
        with self._lock:
            u = self._ultima
            edad = None if u is None else max(0.0, self.mono() - u.cuando)
            if self._perdida:
                return Estado(False, edad_s=edad, motivo="sin_conexion")
            if u is not None and edad is not None and edad <= RELOJ_VIGENTE_S:
                return Estado(True, u.dif_ns, u.err_ns, edad)
            if self._anterior:
                return Estado(False, edad_s=edad, motivo="anterior")
            return Estado(False, edad_s=edad, motivo="vieja" if u is not None else "esperando")


# --- textos -----------------------------------------------------------------------------------------------------

def fmt_ms(ns: int) -> str:
    """Una duración sin signo, legible: '2.1 ms', '3.40 s', '2.5 h'."""
    ns = abs(ns)
    if ns < 1_000_000_000:
        return f"{ns / 1e6:.1f} ms"
    if ns < 3600 * 1_000_000_000:
        return f"{ns / 1e9:.2f} s"
    return f"{ns / 3.6e12:.1f} h"


def fmt_dif(ns: int) -> str:
    """'+2.1 ms', '-35.0 ms' (siempre con signo)."""
    return ("-" if ns < 0 else "+") + fmt_ms(ns)


def linea_corta(nombre: str, e: Estado) -> str:
    """'robot: reloj +2.1 ms (±0.6)' o 'robot: reloj sin medir (aún no hay medición)' (para la parte B)."""
    if e.medido:
        return f"{nombre}: reloj {fmt_dif(e.dif_ns)} (±{fmt_ms(e.err_ns).removesuffix(' ms')})"
    return f"{nombre}: reloj sin medir ({MOTIVOS.get(e.motivo, e.motivo or 'sin dato')})"


def _hacia(dif: int) -> str:
    return "adelantado" if dif > 0 else "atrasado"


def consejos(so: str = "") -> list[str]:
    """Qué hacer con un reloj corrido, el del sistema `so` del robot primero ("" = no se sabe)."""
    linux = ("en Linux o Raspberry Pi, instala chrony (sudo apt install chrony); sin internet, pon la hora a mano "
             "(sudo date -s '2026-01-31 12:00:00') o apunta chrony a la otra computadora")
    windows = "en Windows, w32tm /resync (en una terminal de administrador)"
    mac = "en macOS, sudo sntp -sS time.apple.com"
    orden = {"windows": [windows, linux], "darwin": [mac, linux, windows]}.get(so, [linux, windows])
    return orden


def aviso_mal(nombre: str, e: Estado, so: str = "", recordatorio: bool = False) -> str:
    cabeza = "⚠ sigue igual: " if recordatorio else "⚠ "
    return (f"{cabeza}el reloj de {nombre} va {fmt_ms(e.dif_ns)} {_hacia(e.dif_ns)} respecto al de esta "
            f"computadora (±{fmt_ms(e.err_ns)}): los t_data de sus topics no coinciden con los de aquí, así que las "
            f"latencias y las grabaciones salen corridas. No se sabe cuál de los dos relojes está mal: revisa los "
            f"dos. Qué hacer: {'; '.join(consejos(so))}")


def aviso_bien(nombre: str, e: Estado) -> str:
    return (f"✓ el reloj de {nombre} ya coincide con el de esta computadora: {fmt_dif(e.dif_ns)} "
            f"(±{fmt_ms(e.err_ns)})")


def aviso_lenta(nombre: str, e: Estado) -> str:
    return (f"⚠ la red con {nombre} está demasiado lenta para medir su reloj con precisión (±{fmt_ms(e.err_ns)}): "
            f"no puedo decir si va bien o corrido. Lo sigo midiendo; si la red mejora, aviso solo si está corrido")


def aviso_anterior(nombre: str) -> str:
    return (f"{nombre} tiene un Agmen anterior: no mide la diferencia entre su reloj y el de esta computadora (queda "
            f"«sin medir»). Actualiza Agmen en {nombre} (pip install -U agmen) para verla")


def aviso_mal_formado(nombre: str) -> str:
    return (f"⚠ {nombre} manda las marcas del reloj mal formadas: las ignoro (su reloj queda «sin medir»). ¿Tiene "
            f"otra versión de Agmen? Usa la misma en las dos computadoras")


class Vigia:
    """Los avisos de agmen run para UNA computadora (C3, C4): solo en sesión real; > 10 ms avisa, < 8 ms dice que
    ya está bien, y mientras siga mal, un recordatorio cada 10 min. Con la incertidumbre > 10 ms no se afirma nada
    (se dice una vez que la red está lenta). `so()`: el sistema del robot si se sabe ("" si no)."""

    def __init__(self, nombre: str, modo: str, so: Callable[[], str] = lambda: "",
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.nombre, self.modo, self.so, self.mono = nombre, modo, so, mono
        self.mal = False
        self._avisado = 0.0
        self._lenta_dicha = False

    def revisar(self, e: Estado) -> list[str]:
        if self.modo != "real" or not e.medido:
            return []
        if e.err_ns > RELOJ_UMBRAL_NS:
            if self._lenta_dicha:
                return []
            self._lenta_dicha = True
            return [aviso_lenta(self.nombre, e)]
        ahora = self.mono()
        dif = abs(e.dif_ns)
        if not self.mal and dif > RELOJ_UMBRAL_NS:
            self.mal, self._avisado = True, ahora
            return [aviso_mal(self.nombre, e, self.so())]
        if self.mal and dif < RELOJ_BIEN_NS:
            self.mal = False
            return [aviso_bien(self.nombre, e)]
        if self.mal and ahora - self._avisado >= RELOJ_RECORDATORIO_S:
            self._avisado = ahora
            return [aviso_mal(self.nombre, e, self.so(), recordatorio=True)]
        return []


def textos_agente(eventos: list[str], laptop: str, e: Estado, modo: str) -> list[tuple[str, str]]:
    """Las líneas (texto, tipo) de la terminal del agente (§3.5, informativas). `e`: el estado ya actualizado."""
    out: list[tuple[str, str]] = []
    nota = {"sim": " (no afecta en simulación)", "replay": " (no afecta en reproducción)"}.get(modo, "")
    for ev in eventos:
        if ev == "anterior":
            out.append((f"{laptop} tiene un Agmen anterior: no me pasa la diferencia de relojes (aquí queda «sin "
                        f"medir»). Actualiza Agmen allá (pip install -U agmen) para verla", "agmen"))
        elif ev == "mal_formado":
            out.append((f"⚠ {laptop} manda la medición del reloj mal formada: la ignoro (aquí queda «sin medir»)",
                        "error"))
        elif ev == "primera" and e.medido:
            out.append((f"reloj de {laptop}: {fmt_dif(e.dif_ns)} (±{fmt_ms(e.err_ns)}) respecto al de esta "
                        f"computadora{nota}", "agmen"))
        elif ev == "mal" and e.medido and modo == "real":
            out.append((f"⚠ el reloj de {laptop} va {fmt_ms(e.dif_ns)} {_hacia(e.dif_ns)} respecto al de esta "
                        f"computadora (±{fmt_ms(e.err_ns)}): revisa la hora de las dos (qué hacer: en la terminal "
                        f"de agmen run)", "error"))
        elif ev == "bien" and e.medido and modo == "real":
            out.append((f"✓ el reloj de {laptop} ya coincide con el de esta computadora: {fmt_dif(e.dif_ns)}",
                        "agmen"))
    return out


# --- el campo `relojes` de la línea `red` (agmen-hub/1) -----------------------------------------------------------
#
#   relojes=<entrada>,<entrada>,…
#   medido:     <nombre>:<dif_ns>:<err_ns>:<edad_ms>       (dif con signo: el reloj de <nombre> menos el de aquí)
#   sin medir:  <nombre>:-:<motivo>:<edad_ms o ->           (edad de la última medición, si hubo)
#
# Todo cabe en los bytes que agmen-hub/1 manda sin escapar (letras, dígitos, ':' ',' '-' '_').

_ENTERO = re.compile(r"^-?[0-9]{1,19}$")
_MOTIVO = re.compile(r"^[a-z_]{1,20}$")


def _edad_ms(e: Estado) -> str:
    return "-" if e.edad_s is None else str(int(round(e.edad_s * 1000)))


def formato_red(relojes: list[tuple[str, Estado]]) -> str:
    partes = []
    for nombre, e in relojes:
        if e.medido:
            partes.append(f"{nombre}:{e.dif_ns}:{e.err_ns}:{_edad_ms(e)}")
        else:
            partes.append(f"{nombre}:-:{e.motivo or 'esperando'}:{_edad_ms(e)}")
    return ",".join(partes)


def leer_red(texto: str) -> dict[str, Estado]:
    """El valor de `relojes` → {nombre: Estado} (edad_s = la que traía). ValueError si algo está mal escrito."""
    out: dict[str, Estado] = {}
    for item in (x for x in texto.split(",") if x):
        partes = item.split(":")
        if len(partes) != 4:
            raise ValueError(f"'{item[:60]}' en relojes no es nombre:dif:err:edad ni nombre:-:motivo:edad")
        nombre, dif, err, edad = partes
        check_machine(nombre)
        if nombre in out:
            raise ValueError(f"la computadora '{nombre}' está dos veces en relojes")
        if edad == "-":
            edad_s = None
        elif _ENTERO.match(edad) and 0 <= int(edad) <= 10 ** 9:
            edad_s = int(edad) / 1000
        else:
            raise ValueError(f"edad '{edad[:20]}' en relojes no es un número de ms (o -)")
        if dif == "-":
            if not _MOTIVO.match(err):
                raise ValueError(f"motivo '{err[:20]}' en relojes no es válido (letras minúsculas y _)")
            out[nombre] = Estado(False, edad_s=edad_s, motivo=err)
            continue
        if not (_ENTERO.match(dif) and _ENTERO.match(err)) or abs(int(dif)) >= MARCA_MAX \
                or not 0 <= int(err) <= RELOJ_IDA_VUELTA_MAX_NS or edad_s is None:
            raise ValueError(f"'{item[:60]}' en relojes: dif y err son enteros en ns (err de 0 a "
                             f"{RELOJ_IDA_VUELTA_MAX_NS}) y la edad, ms")
        out[nombre] = Estado(True, int(dif), int(err), edad_s)
    return out

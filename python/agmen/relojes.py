"""agmen.relojes — la diferencia entre el reloj de esta computadora y el de otra (RFC 0011 §3.6, bloque 5).

Solo se MIDE y se AVISA: ningún `t_data` cambia (sincronizar es del nivel 11). Aquí vive lo que no toca la red:
la fórmula (como NTP: cuatro marcas), la ventana de mediciones, los estados ("medido" o "sin medir" con su
motivo), el campo `relojes` de la línea `red` del hub y los textos de los avisos. Las horas se inyectan (`hora`,
`mono`) para probar sin depender del reloj de la máquina.

Las cuatro marcas viajan en el `ping`/`pong` de agmen-agent/1 (decisión C1), solo si los dos lados dijeron
`"relojes": 1` (la laptop en `sesion` y `reanudar`; el agente, si la laptop lo dijo, en `sesion_ok`/`reanudar_ok`):

    laptop → agente   {"op": "ping", "t": …, "reloj": {}}                               (sin medición vigente)
                      {"op": "ping", "t": …, "reloj": {"dif": ns, "err": ns, "edad": ms}} (su mejor medición vigente)
    agente → laptop   {"op": "pong", "t": …, "reloj": {"rx": ns, "tx": ns, "res": ns}}    (`res` opcional)

`t1` (la laptop manda el ping) y `t4` (le llega el pong) son de la laptop; `rx` y `tx`, del agente (`tx` lo escribe
el hilo que manda, justo antes de escribir). `res`: la resolución del reloj de pared del agente (8.4). Con un Agmen
anterior del otro lado nada de esto viaja: `ping` y `pong` son exactamente los de antes desde el primero, y el
estado es «sin medir (Agmen anterior)».

Signo: la diferencia de una computadora X es (reloj de X) − (reloj de aquí); positivo = X va ADELANTADO.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from . import wire
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
    "vieja": f"no hay una medición de los últimos {RELOJ_VIGENTE_S:g} s",
    "sin_conexion": "sin conexión con esa computadora",
    "sin_dato": "el hub no recibió la medición",
    "descartadas": "las mediciones salen absurdas: ¿la red tarda más de 2 s?",
}
RES_MAX_NS = 1_000_000_000           # una resolución de reloj de más de 1 s no es creíble: se ignora


def hora_ns() -> int:
    """La hora "de pared" de esta computadora en ns: la misma función que usa agmen.clock en modo real para
    `t_data` (wire.wall_ns), no el reloj monótono."""
    return wire.wall_ns()


def resolucion_ns() -> int:
    """La resolución de ese reloj de pared en ns (8.4)."""
    return min(RES_MAX_NS, wire.wall_resolution_ns())


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


def medir(t1: int, rx: int, tx: int, t4: int, cuando: float = 0.0, res_ns: int = 0) -> Medicion | None:
    """Las cuatro marcas → la medición, o None si no sirve (ida y vuelta negativa o de más de 2 s).

    `res_ns` (8.4): la suma de las resoluciones de los dos relojes de pared. Se suma a la incertidumbre, y una ida y
    vuelta ligeramente negativa por esa resolución (dentro de −res_ns) no se descarta: cuenta como 0."""
    ida_vuelta = (t4 - t1) - (tx - rx)
    if ida_vuelta < -res_ns or ida_vuelta > RELOJ_IDA_VUELTA_MAX_NS or tx < rx:
        return None
    ida_vuelta = max(0, ida_vuelta)
    dif = ((rx - t1) + (tx - t4)) // 2
    return Medicion(dif, ida_vuelta // 2 + res_ns, ida_vuelta, cuando)


class _Ventana:
    """Las últimas RELOJ_VENTANA mediciones; la mejor = la de menor ida y vuelta entre las vigentes (con empate, la
    más nueva)."""

    def __init__(self) -> None:
        self.ultimas: deque[Medicion] = deque(maxlen=RELOJ_VENTANA)

    def poner(self, m: Medicion) -> None:
        self.ultimas.append(m)

    def mejor(self, ahora: float) -> Medicion | None:
        vigentes = [m for m in reversed(self.ultimas) if ahora - m.cuando <= RELOJ_VIGENTE_S]
        return min(vigentes, key=lambda m: m.ida_vuelta_ns) if vigentes else None   # (min: la primera = más nueva)

    def edad(self, ahora: float) -> float | None:
        return max(0.0, ahora - self.ultimas[-1].cuando) if self.ultimas else None


class Medidor:
    """El lado de la laptop (agmen run), por robot: anota t1 al mandar cada ping y mide con cada pong.
    Seguro entre hilos (el de ping, el lector y el puente)."""

    def __init__(self, hora: Callable[[], int] = hora_ns, mono: Callable[[], float] = time.monotonic,
                 res: int | None = None) -> None:
        self.hora, self.mono = hora, mono
        self.res = resolucion_ns() if res is None else res   # 8.4: la resolución del reloj de pared de aquí
        self._lock = threading.Lock()
        self._ventana = _Ventana()
        self._pendientes: dict[float, tuple[int, bool]] = {}   # t del ping → (t1, ¿llevaba "reloj"?)
        self._anterior = False      # el agente no dijo "relojes" (8.5)
        self._descartada = False    # la última medición se descartó por absurda (8.6)
        self._dicho: set[str] = set()

    def capacidad(self, sabe: bool) -> list[str]:
        """8.5: el agente dijo (o no) "relojes" en su sesion_ok/reanudar_ok. Sin él: «sin medir (Agmen anterior)»; el
        evento "anterior" sale con el primer pong de esa conexión (como antes de la negociación: una sesión que se
        pierde antes de contestar un ping no dice nada de relojes)."""
        with self._lock:
            self._anterior = not sabe
            return []

    def ping(self, canal: object, t: float, sabe: bool = True) -> dict | None:
        """Antes de mandar el ping `t` por `canal`: el valor de su clave "reloj" (None = no llevarla: el agente de esa
        conexión no dijo "relojes"). La mejor medición vigente va con su edad en ms (8.2); {} si no hay ninguna."""
        with self._lock:
            valor = None
            if sabe:
                ahora = self.mono()
                mejor = self._ventana.mejor(ahora)
                valor = {} if mejor is None else {"dif": mejor.dif_ns, "err": mejor.err_ns,
                                                  "edad": max(0, int(round((ahora - mejor.cuando) * 1000)))}
            if len(self._pendientes) >= _PENDIENTES_MAX:
                self._pendientes.pop(next(iter(self._pendientes)))
            self._pendientes[t] = (self.hora(), sabe)
            return valor

    def pong(self, canal: object, msg: dict) -> list[str]:
        """Llegó un pong por `canal`. Devuelve los avisos que hay que decir (cada uno, una sola vez): "mal_formado"
        (marcas que no sirven: se ignoran) o "descartadas" (todas las mediciones salen absurdas)."""
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
            if not llevaba:   # (un ping sin "reloj": su pong no mide nada)
                return self._una_vez("anterior") if self._anterior else []
            reloj = msg.get("reloj")
            rx, tx = (reloj.get("rx"), reloj.get("tx")) if isinstance(reloj, dict) else (None, None)
            res = reloj.get("res", 0) if isinstance(reloj, dict) else 0
            if not (es_marca(rx) and es_marca(tx) and tx >= rx and type(res) is int and 0 <= res <= RES_MAX_NS):
                return self._una_vez("mal_formado")
            m = medir(t1, rx, tx, t4, ahora, res_ns=self.res + res)
            if m is not None:
                self._descartada = False
                self._ventana.poner(m)
                return []
            # Una ida y vuelta absurda (negativa o > 2 s) se descarta. Si ya no queda ninguna vigente, se dice.
            self._descartada = True
            if self._ventana.mejor(ahora) is None:
                return self._una_vez("descartadas")
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
            if self._anterior:
                return Estado(False, edad_s=edad, motivo="anterior")
            mejor = self._ventana.mejor(ahora)
            if mejor is not None:
                return Estado(True, mejor.dif_ns, mejor.err_ns, max(0.0, ahora - mejor.cuando))
            if self._descartada:
                return Estado(False, edad_s=edad, motivo="descartadas")
            return Estado(False, edad_s=edad, motivo="vieja" if edad is not None else "esperando")


class Remoto:
    """El lado del agente: la laptop le pasa su mejor medición en cada ping (con el signo de la laptop); aquí se
    guarda con el signo cambiado (la diferencia de la LAPTOP respecto a esta computadora)."""

    def __init__(self, mono: Callable[[], float] = time.monotonic) -> None:
        self.mono = mono
        self._lock = threading.Lock()
        self._ultima: Medicion | None = None
        self._borrada = False   # la laptop dijo {} (ya no tiene medición vigente): «sin medir» (8.2)
        self._anterior = False
        self._perdida = False
        self._mal = False       # ¿está por encima del umbral? (con histéresis, para las líneas de la terminal)
        self._dicho: set[str] = set()

    def ping(self, reloj, trae: bool) -> list[str]:
        """Llegó un ping (`trae`: ¿con la clave "reloj"?; `reloj`: su valor). Devuelve los eventos para la terminal
        del agente: "anterior", "mal_formado" (una vez cada uno), "primera", "mal" y "bien" (al cruzar el umbral).

        8.2: la medición se da por tomada hace `edad` ms (no "ahora") y caduca a los 10 s de verdad; un {} BORRA la
        que había (la laptop ya no tiene una vigente). Nunca se guarda como medido un valor de más de 10 s."""
        with self._lock:
            self._perdida = False
            if not trae:
                self._anterior = True
                return self._una_vez("anterior")
            self._anterior = False
            if not isinstance(reloj, dict):
                return self._una_vez("mal_formado")
            if not reloj:
                self._borrada = True
                return []
            dif, err, edad = reloj.get("dif"), reloj.get("err"), reloj.get("edad")
            if not (type(dif) is int and abs(dif) < MARCA_MAX and type(err) is int
                    and 0 <= err <= RELOJ_IDA_VUELTA_MAX_NS and type(edad) is int and edad >= 0):
                return self._una_vez("mal_formado")
            if edad > RELOJ_VIGENTE_S * 1000:   # ya vieja al llegar: como {} (la laptop no debería mandarla)
                self._borrada = True
                return []
            primera = self._ultima is None
            self._borrada = False
            self._ultima = Medicion(-dif, err, 2 * err, self.mono() - edad / 1000)
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
            if u is not None and edad is not None and edad <= RELOJ_VIGENTE_S and not self._borrada:
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
    return (f"⚠ la red con {nombre} está demasiado lenta para medir su reloj con precisión (±{fmt_ms(e.err_ns)}), o "
            f"el reloj de alguna de las dos avanza a saltos (Windows con Python 3.12 o anterior: de 15.6 ms en "
            f"15.6 ms): no puedo decir si va bien o corrido. Lo sigo midiendo; si mejora, aviso solo si está corrido")


def aviso_anterior(nombre: str) -> str:
    return (f"{nombre} tiene un Agmen anterior: no mide la diferencia entre su reloj y el de esta computadora (queda "
            f"«sin medir»). Actualiza Agmen en {nombre} (pip install -U agmen) para verla")


def aviso_descartadas(nombre: str) -> str:
    return (f"⚠ las mediciones del reloj de {nombre} salen absurdas (la ida y vuelta es negativa o de más de "
            f"{RELOJ_IDA_VUELTA_MAX_NS / 1e9:g} s): las descarto y su reloj queda «sin medir». ¿La red tarda más de "
            f"{RELOJ_IDA_VUELTA_MAX_NS / 1e9:g} s, o alguien está cambiando la hora de una de las dos?")


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


# --- lo que se ve (parte B, §4): agmen node list, el panel y agmen record ------------------------------------------

def leer_maq(f: dict) -> Estado:
    """Una línea `maq` del `state` del hub (ya en campos) → su Estado. Lo que no se entiende queda «sin medir»
    (`sin_dato`): nunca se inventa un 0 (C6)."""
    edad = None
    try:
        if "edad" in f:
            edad = float(f["edad"])
            if not (math.isfinite(edad) and 0.0 <= edad <= 1e9):   # 8.6: nunca Infinity/NaN en el JSON del panel
                edad = None
    except (ValueError, TypeError):
        edad = None
    reloj, err = f.get("reloj"), f.get("err")
    if reloj is not None and err is not None:
        if _ENTERO.match(reloj) and _ENTERO.match(err) and abs(int(reloj)) < MARCA_MAX \
                and 0 <= int(err) <= RELOJ_IDA_VUELTA_MAX_NS:
            return Estado(True, int(reloj), int(err), edad)
        return Estado(False, edad_s=edad, motivo="sin_dato")
    sin = f.get("sin", "")
    return Estado(False, edad_s=edad, motivo=sin if _MOTIVO.match(sin) else "sin_dato")


def maquinas_del_hub(msgs) -> dict[str, Estado]:
    """Las líneas `maq` de una respuesta del hub (hubmod.query) → {computadora: Estado}. Vacío sin modo red."""
    return {f["name"]: leer_maq(f) for op, f in msgs if op == "maq" and f.get("name")}


def lenta(e: Estado) -> bool:
    """¿La incertidumbre es tan grande que no se puede afirmar nada (> 10 ms, §3.3)?"""
    return e.medido and e.err_ns > RELOJ_UMBRAL_NS


def pasa_umbral(e: Estado) -> bool:
    """¿Va corrido (|dif| > 10 ms) con una medición confiable? (sin la histéresis: una sola mirada)"""
    return e.medido and not lenta(e) and abs(e.dif_ns) > RELOJ_UMBRAL_NS


def que_hacer_corto(so: str = "") -> str:
    """Qué hacer con un reloj corrido, en una línea (el del sistema `so` primero)."""
    linux, windows, mac = "Linux/Raspberry Pi: chrony", "Windows: w32tm /resync", "macOS: sntp"
    orden = {"windows": [windows, linux], "darwin": [mac, linux, windows]}.get(so, [linux, windows])
    return "revisa la hora de las dos (" + "; ".join(orden) + ")"


NOTA_MODO = {"sim": "no afecta en sim", "replay": "no afecta en reproducción"}


def linea_node_list(nombre: str, e: Estado, modo: str) -> str:
    """La línea de `agmen node list` (§4.1). `modo`: "real", "sim", "replay" o "" (no se sabe: la herramienta
    suelta no conoce el modo de la sesión). Solo en "real" se marca con ⚠; sin saberlo, se dice el dato y que,
    si la sesión es real, hay que revisar (sin ⚠ ni nota de sim)."""
    base = linea_corta(nombre, e)
    if not e.medido:
        return base
    if modo in NOTA_MODO:
        return f"{base} ({NOTA_MODO[modo]})"
    if lenta(e):
        return f"{base} — la red está demasiado lenta para afirmar si va corrido"
    if not pasa_umbral(e):
        return base
    umbral = f"más de {RELOJ_UMBRAL_NS / 1e6:g} ms"
    if modo == "real":
        return f"⚠ {base} — {umbral}: {que_hacer_corto()}"
    return f"{base} — {umbral}: si la sesión usa el reloj real, {que_hacer_corto()}"


def info_panel(nombre: str, e: Estado, modo: str, mal_antes: bool, so: str = "") -> tuple[dict, bool]:
    """El `clock` de una computadora en `machines` de /api/state (§4.2) y si sigue «mal» (con la histéresis de
    10/8 ms de los avisos de agmen run, C4; solo en sesión real, C3). `mal_antes`: lo de la vez anterior."""
    mal = mal_antes
    if modo == "real" and e.medido and not lenta(e):
        if not mal and abs(e.dif_ns) > RELOJ_UMBRAL_NS:
            mal = True
        elif mal and abs(e.dif_ns) < RELOJ_BIEN_NS:
            mal = False
    if modo != "real":
        mal = False
    edad = None if e.edad_s is None else round(e.edad_s, 1)
    if not e.medido:
        return ({"offset_ms": None, "error_ms": None, "state": "sin_medir",
                 "why": MOTIVOS.get(e.motivo, e.motivo or "sin dato"), "age_s": edad,
                 "text": "reloj sin medir"}, mal)
    texto = linea_corta(nombre, e).split(": ", 1)[1]
    if modo in NOTA_MODO:
        why = NOTA_MODO[modo]
    elif mal:
        why = (f"va {fmt_ms(e.dif_ns)} {_hacia(e.dif_ns)} respecto a esta computadora: las latencias y las "
               f"grabaciones salen corridas. No se sabe cuál de los dos relojes está mal: "
               f"{que_hacer_corto(so)}")
    elif lenta(e):
        why = "la red está demasiado lenta para afirmar si va corrido"
    else:
        why = ""
    return ({"offset_ms": round(e.dif_ns / 1e6, 3), "error_ms": round(e.err_ns / 1e6, 3),
             "state": "mal" if mal else "ok", "why": why, "age_s": edad, "text": texto}, mal)


class Resumen:
    """Lo que `agmen record` guarda de cada computadora en `relojes` de agmen.grabacion (§4.4, C5): la diferencia
    al empezar y al terminar, la mínima, la máxima, la mayor incertidumbre y cuántas muestras. Solo cuentan las
    mediciones vigentes ("sin medir" no es una muestra).

    Riesgo aceptado: un salto brusco de un reloj a media grabación no se reconstruye (se consulta cada ~10 s); la
    mínima y la máxima solo lo delatan."""

    def __init__(self) -> None:
        self.por: dict[str, dict[str, int]] = {}

    def poner(self, estados: dict[str, Estado]) -> None:
        for nombre, e in estados.items():
            if not e.medido:
                continue
            r = self.por.get(nombre)
            if r is None:
                self.por[nombre] = {"inicio_ns": e.dif_ns, "fin_ns": e.dif_ns, "min_ns": e.dif_ns,
                                    "max_ns": e.dif_ns, "error_ns": e.err_ns, "muestras": 1}
                continue
            r["fin_ns"] = e.dif_ns
            r["min_ns"], r["max_ns"] = min(r["min_ns"], e.dif_ns), max(r["max_ns"], e.dif_ns)
            r["error_ns"] = max(r["error_ns"], e.err_ns)
            r["muestras"] += 1

    def texto(self) -> str:
        """El valor de la clave `relojes` (los metadatos MCAP son texto: JSON compacto). "" = no hay nada."""
        if not self.por:
            return ""
        return json.dumps(self.por, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def lineas_grabacion(texto: str) -> list[str]:
    """Cómo dice `agmen record info` la clave `relojes` (vacía: ninguna línea). Una mal escrita se dice, no truena."""
    try:
        d = json.loads(texto)
        if not isinstance(d, dict):
            raise ValueError("no es un objeto")
        out = []
        for nombre in sorted(d):
            r = d[nombre]
            ini, fin, lo, hi = (int(r[k]) for k in ("inicio_ns", "fin_ns", "min_ns", "max_ns"))
            err, n = int(r["error_ns"]), int(r["muestras"])
            linea = (f"reloj de {nombre}: {fmt_dif(ini)} al empezar, {fmt_dif(fin)} al terminar (mín {fmt_dif(lo)}, "
                     f"máx {fmt_dif(hi)}, ±{fmt_ms(err)}, {n} muestra{'s' if n != 1 else ''})")
            out.append(linea)
            if hi - lo > RELOJ_UMBRAL_NS:
                out.append(f"  ⚠ cambió {fmt_ms(hi - lo)} durante la grabación (¿alguien ajustó un reloj?): las "
                           f"latencias de ese rato no se pueden corregir con un solo número")
        return out
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        return [f"⚠ la diferencia de relojes guardada no se puede leer ({e}); lo demás de la grabación sí"]

"""Verifica que el código del tutorial funcione tal como lo describe.

Corre en el CI: si un cambio rompe el tutorial, el CI falla.

    python scripts/tutorial_check.py
"""

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

# En Windows, la salida a un archivo/CI usa cp1252 y truena con acentos o "→".
for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
EXE = ".exe" if sys.platform == "win32" else ""
BIN = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "build" / "bin"
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8"}


def run_case(name, sender_cmd, expected, motor_args=()):
    """El motor C++ escucha 4 s; el emisor le manda `expected` comandos. Los dos por correr(): si alguno se cuelga,
    se dice y se mata su árbol, sin traceback."""
    env = {**ENV, "AGMEN_HUB": f"127.0.0.1:{puerto_libre(socket.SOCK_DGRAM)}"}   # (si algo buscara un hub: el suyo)
    motor: dict = {}
    tope = 30
    hilo = threading.Thread(target=lambda: motor.update(r=correr([str(BIN / f"tutorial_motor{EXE}"), "4",
                                                                  *motor_args], tope, env=env)), daemon=True)
    hilo.start()
    time.sleep(0.8)
    emisor, parcial_emisor = correr(sender_cmd, tope, env=env)
    hilo.join(correr_peor(tope))   # lo más que tarda correr() del motor, aun si se cuelga y hay que matarlo
    r, parcial = motor.get("r", (None, "(el motor no terminó)"))
    out = (r.stdout + r.stderr) if r is not None else parcial
    m = re.search(r"recibidos=(\d+) errores=(\d+)", out)
    ok = (r is not None and emisor is not None and emisor.returncode == 0
          and m is not None and int(m.group(1)) == expected and m.group(2) == "0"
          and "ALTO" in out)  # al terminar el emisor, el motor debe frenar solo
    print(f"{'OK  ' if ok else 'FALLA'} {name}: {m.group(0) if m else 'sin resumen'}, "
          f"freno de seguridad {'sí' if 'ALTO' in out else 'NO'}")
    if emisor is None:
        print(f"     el emisor se colgó (más de 30 s); lo maté a él y a sus hijos\n{parcial_emisor}")
    elif emisor.returncode != 0:
        print(f"     el emisor salió con {emisor.returncode}\n{emisor.stdout}{emisor.stderr}")
    if r is None:
        print("     el motor se colgó (más de 30 s); lo maté a él y a sus hijos")
    if not ok:
        print(out)
    return ok


def run_vision() -> bool:
    """Capítulos 14 y 15 (RFC 0008 y 0009): camara.py (Imagen de 640×480 a 30 fps) →
    vision_contador (C++) e ia.py (NumPy sin copiar), todos por topics y, en la
    misma computadora, por memoria compartida."""
    try:
        import numpy  # noqa: F401
    except ImportError:
        print("SALTO capítulo 14: sin NumPy")
        return True
    from agmen import hub as hubmod

    h = hubmod.Hub("127.0.0.1:0", log=lambda t: None)
    env = {**ENV, "AGMEN_HUB": f"127.0.0.1:{h.sock.getsockname()[1]}"}
    stop = threading.Event()
    threading.Thread(target=h.serve, args=(stop,), daemon=True).start()
    vision = ROOT / "examples" / "vision"
    # El contador corre hasta que la cámara termina (y entonces Ctrl+C): no se
    # puede saber cuánto tarda la cámara en arrancar (una webcam real en
    # Windows tarda 1-5 s en abrir, y una máquina cargada, más).
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
    if os.name != "nt":
        group = {"start_new_session": True}   # su propio grupo: si se cuelga, matar_arbol mata también a sus hijos
    contador = subprocess.Popen([str(BIN / f"vision_contador{EXE}"), "60"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env,
                                **group)
    ia = subprocess.Popen([sys.executable, str(vision / "ia.py")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace", env=env, **group)

    def subs_of(topic: str) -> int:
        try:
            return sum(1 for e in list(h.endpoints.values()) if e.kind == "sub" and e.topic == topic)
        except RuntimeError:          # el hilo del hub cambió el diccionario mientras se contaba
            return 0

    deadline = time.monotonic() + 20
    while subs_of("/camara") < 2 and time.monotonic() < deadline:   # los dos ya escuchan
        time.sleep(0.05)
    cam, cam_parcial = correr([sys.executable, str(vision / "camara.py"), "--seconds", "3"], 60, env=env)
    time.sleep(0.3)                   # que lleguen las últimas imágenes
    contador.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
    out, contador_ok = esperar(contador, 20)
    ia.terminate()
    ia_out, ia_ok = esperar(ia, 10)
    stop.set()
    colgados = [que for que, bien in (("la cámara (60 s)", cam is not None), ("el contador (20 s tras Ctrl+C)",
                                      contador_ok), ("la IA (10 s tras terminate)", ia_ok)) if not bien]
    if cam is None:
        cam = subprocess.CompletedProcess([], -1, cam_parcial, "")
    m = re.search(r"RESULTADO vistas=(\d+) compartidas=(\d+)", out)
    vistas = int(m.group(1)) if m else 0
    compartidas = int(m.group(2)) if m else 0
    verdes = re.findall(r"(\d+) píxeles verdes", out)
    ia_m = re.findall(r"miré (\d+) imágenes \((\d+) por memoria compartida\)", ia_out)
    ia_vistas = int(ia_m[-1][0]) if ia_m else 0
    ia_compartidas = int(ia_m[-1][1]) if ia_m else 0
    pm = re.search(r"terminé: (\d+) imágenes publicadas", cam.stdout)
    publicadas = int(pm.group(1)) if pm else 0
    # Contra lo que la cámara publicó de verdad: en una máquina cargada (macOS del
    # CI) no llega a 30 fps, y eso no es culpa de quien recibe. El contador debe
    # ver casi todas; la IA usa queue=1 y se salta las viejas a propósito.
    # Capítulo 15: casi todas por memoria compartida (las primeras, mientras la
    # prueba va y viene, pueden llegar por TCP).
    ok = (publicadas >= 30 and vistas >= 0.8 * publicadas and verdes and int(verdes[-1]) == 3600
          and ia_vistas >= 0.5 * publicadas and "confianza 1.00" in ia_out and cam.returncode == 0
          and compartidas >= 0.8 * vistas and ia_compartidas >= 0.8 * ia_vistas and not colgados)
    print(f"{'OK  ' if ok else 'FALLA'} capítulos 14 y 15: cámara Python -> contador C++ e IA Python: la cámara "
          f"publicó {publicadas}, C++ vio {vistas} ({compartidas} por memoria compartida, "
          f"{verdes[-1] if verdes else '?'} píxeles verdes), la IA miró {ia_vistas} ({ia_compartidas} por memoria "
          f"compartida)")
    for que in colgados:
        print(f"     se colgó {que}; lo maté a él y a sus hijos")
    if not ok:
        print(out, ia_out, cam.stdout, cam.stderr)
    return ok


def run_dataset() -> bool:
    """Capítulo 16 (RFC 0010): la IMU de C++ → agmen record → dataset.py (agmen.Recording y Parquet)."""
    import tempfile

    from agmen import hub as hubmod

    h = hubmod.Hub("127.0.0.1:0", log=lambda t: None)
    env = {**ENV, "AGMEN_HUB": f"127.0.0.1:{h.sock.getsockname()[1]}"}
    stop = threading.Event()
    threading.Thread(target=h.serve, args=(stop,), daemon=True).start()
    work = Path(tempfile.mkdtemp(prefix="agmen-cap16-"))
    rec = None
    try:
        grupo = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                 else {"start_new_session": True})
        rec = subprocess.Popen([sys.executable, "-m", "agmen", "record", "-o", str(work / "vuelta1.mcap"),
                                "--duration", "6s", "/imu"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                               errors="replace", env=env, **grupo)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not any(e.kind == "sub" and e.topic == "/imu"
                                                      for e in list(h.endpoints.values())):
            time.sleep(0.05)
        pub, pub_parcial = correr([str(BIN / f"publisher{EXE}"), "--rate", "100", "--to", "/imu", "--count", "200"], 30,
                                  env=env)
        rec_out, rec_ok = esperar(rec, 30)
        stop.set()
        r, parcial = correr([sys.executable, str(ROOT / "examples" / "tutorial" / "dataset.py"), "vuelta1.mcap"], 60,
                            cwd=work, env=env)
        colgados = [que for que, bien in (("el publisher de la IMU (30 s)", pub is not None),
                                          ("agmen record (30 s)", rec_ok), ("dataset.py (60 s)", r is not None))
                    if not bien]
        if r is None:
            r = subprocess.CompletedProcess([], -1, parcial, "")
        out = r.stdout + r.stderr
        m = re.search(r"/imu: (\d+) mensajes de ImuSample", out)
        n = int(m.group(1)) if m else 0
        try:
            import pyarrow  # noqa: F401
            tables = "huecos en seq: 0" in out and (work / "dataset" / "imu.parquet").is_file()
        except ImportError:
            tables = "pip install agmen[datasets]" in out
        ok = (r.returncode == 0 and rec.returncode == 0 and n >= 180 and "az promedio 9.81" in out and tables
              and not colgados)
        print(f"{'OK  ' if ok else 'FALLA'} capítulo 16: IMU C++ -> agmen record -> dataset.py: {n} mensajes de 200, "
              f"{'tabla y Parquet' if tables else 'SIN tabla'}")
        for que in colgados:
            print(f"     se colgó {que}; lo maté a él y a sus hijos")
        if not ok:
            print(rec_out, pub_parcial, out)
        return ok
    finally:   # su directorio, también si falló o se colgó algo
        stop.set()
        if rec is not None and rec.poll() is None:
            # Sigue vivo (algo tronó antes de esperarlo): en Windows tendría abierto el .mcap y el directorio no se
            # podría borrar. Se mata con sus hijos y se recoge.
            matar_arbol(rec)
            try:
                rec.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                print(f"     agmen record (pid {rec.pid}) no terminó ni con SIGKILL en 10 s")
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# Capítulo 19 (RFC 0013 §3.4): articulaciones. El código y las salidas se sacan del
# propio capítulo, de los bloques marcados con <!-- tutorial_check: NOMBRE --> en la
# línea de antes de la valla. Si el capítulo y lo que pasa de verdad no coinciden,
# la prueba dice cuál línea del capítulo no salió.
# ---------------------------------------------------------------------------

CAP19 = ROOT / "docs" / "tutorial" / "19-articulaciones.md"
CAP19_REL = CAP19.relative_to(ROOT).as_posix()
_MARCA = re.compile(r"^(\s*)<!-- tutorial_check: (\S+) -->\s*$")


class Bloque:
    """Un bloque de código del capítulo: su texto y la línea del .md donde empieza."""

    def __init__(self, nombre: str, linea: int, lineas: list[str]) -> None:
        self.nombre, self.linea, self.lineas = nombre, linea, lineas

    @property
    def texto(self) -> str:
        return "\n".join(self.lineas) + "\n"

    def numeradas(self) -> list[tuple[int, str]]:
        """(línea del .md, texto) de cada línea que no está vacía."""
        return [(self.linea + i, t) for i, t in enumerate(self.lineas) if t.strip()]


def bloques_marcados(md: Path) -> dict[str, Bloque]:
    lineas = md.read_text(encoding="utf-8").splitlines()
    bloques: dict[str, Bloque] = {}
    for i, linea in enumerate(lineas):
        m = _MARCA.match(linea)
        if not m:
            continue
        sangria, nombre = len(m.group(1)), m.group(2)
        valla = lineas[i + 1] if i + 1 < len(lineas) else ""
        if not valla.lstrip().startswith("```"):
            raise ValueError(f"{CAP19_REL}:{i + 1}: la marca '{nombre}' debe ir justo antes de la valla ``` del bloque")
        cuerpo, j = [], i + 2
        while j < len(lineas) and not lineas[j].lstrip().startswith("```"):
            cuerpo.append(lineas[j][sangria:] if lineas[j][:sangria].strip() == "" else lineas[j])
            j += 1
        if j == len(lineas):
            raise ValueError(f"{CAP19_REL}:{i + 2}: el bloque '{nombre}' no se cierra (falta la valla ```)")
        if nombre in bloques:
            raise ValueError(f"{CAP19_REL}:{i + 1}: la marca '{nombre}' está dos veces")
        bloques[nombre] = Bloque(nombre, i + 3, cuerpo)
    return bloques


def normalizar(texto: str) -> str:
    """Solo lo que cambia en cada corrida: versión, puertos, pid, tiempos, el ángulo
    que se lee (depende de cuándo arrancó cada nodo) y los espacios de las columnas."""
    t = re.sub(r"\x1b\[[0-9;]*m", "", texto)
    t = re.sub(r"\bAgmen \d+\.\d+\.\d+\S*", "Agmen X.Y.Z", t)
    t = re.sub(r"\b127\.0\.0\.1:\d+", "127.0.0.1:PUERTO", t)
    t = re.sub(r"\bpid \d+", "pid N", t)
    t = re.sub(r"latió hace [\d.]+ s", "latió hace T s", t)
    t = re.sub(r"cadera FR = -?\d+\.\d+ rad", "cadera FR = X rad", t)
    return re.sub(r"\s+", " ", t).strip()


# El [web] listen del panel cuando robot.toml no lo dice (el robot.toml del capítulo no lo dice). Al correr agmen run,
# Cap19 le pone uno libre (otro programa puede tener este) y, en lo que sale, lo traduce de vuelta a este.
_LISTEN_CAPITULO = "127.0.0.1:7400"

_HUB_DE = re.compile(r"\[hub\] \S+ '([^']+)' ")


def _quien(linea_normalizada: str) -> str:
    """De quién es una línea de agmen run, para el orden: la columna ("hub │ …" → "hub"; "" si no la lleva). Las
    líneas del hub sobre un registro ("hub │ [hub] ▶ 'politica' publica …") van cada una por su lado: el hub las
    escribe en el orden en que le llegan los registros, y eso depende de qué nodo arranca antes (y, dentro de un
    nodo, el Subscriber se registra al crearse y el Publisher al publicar su primer mensaje)."""
    quien, barra, resto = linea_normalizada.partition(" │ ")
    if not barra or " " in quien:
        return ""
    return f"{quien}:{resto}" if _HUB_DE.match(resto) else quien


def faltan(esperado: Bloque, salida: str) -> list[tuple[int, str, str]]:
    """Las líneas del bloque del capítulo que no salen así (normalizadas) en la salida: (línea del .md, texto, qué
    pasó). Dos casos:

    - Falta: cada línea del capítulo se busca después de la anterior del MISMO origen (ver _quien: las de procesos
      distintos se mezclan según quién escribe primero, y ese orden no se exige), y una línea real sirve para una
      sola del capítulo (si el capítulo la muestra dos veces, tiene que salir dos veces).
    - Sale de más: una línea del capítulo que en la salida real sale más veces que en el capítulo (un aviso que
      debía salir una vez y salió dos). Se cuentan sus copias en toda la salida.
    - Salió y el capítulo no la muestra: un aviso (⚠) o error (✗) real de agmen run o del hub (columnas `agmen │`
      y `hub │`) que no sirvió para ninguna línea del bloque. Esas se reportan con la línea donde empieza el bloque
      y el texto real."""
    crudas = salida.splitlines()
    reales = [normalizar(t) for t in crudas]
    usadas: set[int] = set()
    desde: dict[str, int] = {}          # por proceso: la línea real siguiente a la última que se encontró
    malas = []
    lineas = [(n, t, normalizar(t)) for n, t in esperado.numeradas()]
    en_capitulo: dict[str, int] = {}
    for _, _, e in lineas:
        en_capitulo[e] = en_capitulo.get(e, 0) + 1
    for n, t, e in lineas:
        quien = _quien(e)
        k = next((i for i in range(desde.get(quien, 0), len(reales)) if i not in usadas and reales[i] == e), None)
        if k is None:
            malas.append((n, t, "dice esto y no salió así"))
            continue
        usadas.add(k)
        desde[quien] = k + 1
    dichas: set[str] = set()
    for n, t, e in lineas:
        if e in dichas:
            continue
        dichas.add(e)
        veces = reales.count(e)
        if veces > en_capitulo[e]:
            malas.append((n, t, f"dice esto y salió {veces} veces y el capítulo la muestra {en_capitulo[e]}"))
    for i, r in enumerate(reales):
        if i in usadas or not ("⚠" in r or "✗" in r):
            continue
        if _quien(r).split(":", 1)[0] in ("agmen", "hub"):
            malas.append((esperado.linea, crudas[i], "es el bloque, y esta línea real salió y el capítulo no la "
                                                      "muestra"))
    return sorted(malas)


_VALOR = re.compile(r"[-+\d\[\](){}'\"]|(nan|inf|True|False|None)\b")


def impreso_igual(real: str, comentario: str) -> bool:
    """¿Lo que imprimió `print(...)` es lo que dice su comentario? Completo: el comentario es el valor, y puede
    seguirle una explicación separada por DOS o más espacios ("[0.0, 0.0]   por defecto: 0"), que no parece más valor.
    Con un solo espacio no: "JointState[4]" no es igual a "# JointState[4] position=[…]" (lo que sigue podría ser
    parte de lo que debía imprimirse), ni "0.0" a "# 0.0 1.0"."""
    if real == comentario:
        return True
    if not real or not comentario.startswith(real):
        return False
    resto = comentario[len(real):]
    if not resto.startswith("  "):
        return False
    resto = resto.strip()
    return bool(resto) and _VALOR.match(resto) is None


def puerto_libre(tipo: int) -> int:
    with socket.socket(socket.AF_INET, tipo) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def matar_arbol(proc: subprocess.Popen, salida: str = "") -> None:
    """Mata a `proc` y a sus hijos y nietos: su grupo de procesos (lo arrancó correr() en uno propio) y, como en
    Cap19.agmen_run, los pid que salieron como "▶ iniciado (pid N)" (los nodos de un agmen run)."""
    pids = re.findall(r"▶ iniciado \(pid (\d+)\)", salida)
    if os.name == "nt":
        for pid in [str(proc.pid), *pids]:
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass
    try:
        proc.kill()
    except OSError:
        pass


def esperar(proc: subprocess.Popen, tope: float) -> tuple[str, bool]:
    """Espera a que `proc` termine (hasta `tope` s) y devuelve (su salida, True). Si se cuelga no lanza
    TimeoutExpired: mata a todo su árbol de procesos (matar_arbol) y devuelve (lo que alcanzó a escribir, False).
    Nunca deja a `proc` sin wait(). Para un proceso con stdout (y stderr, si va aparte) en tubería."""
    try:
        out, err = proc.communicate(timeout=tope)
        return (out or "") + (err or ""), True
    except subprocess.TimeoutExpired as e:
        # (lo que alcanzó a leer viene en bytes, aunque la tubería sea de texto)
        parcial = "".join(x.decode("utf-8", "replace") if isinstance(x, bytes) else x
                          for x in (e.stdout, e.stderr) if x)
    matar_arbol(proc, parcial)
    try:
        out, err = proc.communicate(timeout=10)
        parcial = (out or "") + (err or "")
    except subprocess.TimeoutExpired:   # algún nieto que no se dejó matar sigue con la tubería: no se espera más
        for f in (proc.stdout, proc.stderr):
            if f is not None:
                f.close()
        try:
            proc.wait(timeout=10)       # el proceso mismo ya recibió SIGKILL: que no quede sin recoger
        except subprocess.TimeoutExpired:
            parcial += f"\n(el proceso {proc.pid} no terminó ni con SIGKILL en 10 s)"
    return parcial, False


# Lo más que esperar() tarda de más tras matar un árbol colgado: communicate(10) y luego wait(10).
_ESPERA_AL_MATAR = 10 + 10


def correr_peor(tope: float) -> float:
    """Lo más que puede tardar correr(cmd, tope): el tope y, si se colgó, lo que espera al matarlo."""
    return tope + _ESPERA_AL_MATAR


def correr(cmd: list[str], tope: float, **kw) -> tuple[subprocess.CompletedProcess | None, str]:
    """Como subprocess.run(cmd, capture_output=True, timeout=tope), pero si se cuelga no lanza TimeoutExpired:
    mata a todo su árbol de procesos y devuelve (None, lo que alcanzó a escribir). Si termina: (resultado, "")."""
    grupo = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", **grupo, **kw)
    try:
        out, err = proc.communicate(timeout=tope)
    except subprocess.TimeoutExpired:
        parcial, termino = esperar(proc, 0)    # (vence enseguida: mata el árbol y recoge lo que haya)
        if not termino:
            return None, parcial
        out, err = parcial, ""                  # (terminó justo entre las dos esperas)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err), ""


class Cap19:
    def __init__(self, work: Path) -> None:
        self.work, self.ok, self.b = work, True, bloques_marcados(CAP19)
        # El capítulo corre "python simulador.py": primero el Python de esta prueba.
        path = os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")])
        # PYTHONUNBUFFERED: lo que un nodo imprime sin flush no se queda atrás de lo que escribe en stderr (agmen run
        # ya se lo pone a sus nodos; aquí también a agmen run y a los fragmentos), así el orden por columna es real.
        self.env = {**ENV, "PATH": path, "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"}
        self.listen = _LISTEN_CAPITULO   # el [web] listen del agmen run en curso (ver agmen_run)

    def traduce(self, texto: str) -> str:
        """Lo que sale de agmen run (o de un comando con él corriendo), con el [web] listen libre que se le puso
        traducido de vuelta al del capítulo: lo que se compara es lo que vería quien corre el capítulo."""
        return texto.replace(self.listen, _LISTEN_CAPITULO) if self.listen != _LISTEN_CAPITULO else texto

    def reporta(self, nombre: str, ok: bool, detalle: str = "", salida: str = "") -> bool:
        print(f"{'OK  ' if ok else 'FALLA'} capítulo 19: {nombre}" + ("" if ok or not detalle else f"\n{detalle}"))
        if not ok and salida:
            print(f"--- salida real ---\n{salida.rstrip()}\n--- fin de la salida real ---")
        self.ok &= ok
        return ok

    def compara(self, nombre: str, esperado: Bloque, salida: str) -> bool:
        malas = faltan(esperado, salida)
        detalle = "\n".join(f"  {CAP19_REL}:{n} {que}:\n    {t.strip()}" for n, t, que in malas)
        return self.reporta(nombre, not malas, detalle, salida)

    # -- el robot de la sección 4, con agmen run de verdad --------------------------------------

    def carpeta(self, nombre: str, robot: str, politica: str) -> Path:
        d = self.work / nombre / "patas"
        d.mkdir(parents=True)
        for archivo, texto in (("robot.toml", robot), ("simulador.py", self.b["simulador.py"].texto),
                               ("politica.py", politica)):
            (d / archivo).write_text(texto, encoding="utf-8")
        return d

    def agmen_run(self, carpeta: Path, listo, tope: float, mientras=None) -> str:
        """Corre `agmen run robot.toml` en la carpeta hasta que `listo(salida)` (o el tope), y lo
        detiene como Ctrl+C. Con su propio hub y panel en puertos libres (también el [web] listen del
        panel: se le agrega al robot.toml de la carpeta, y en la salida se traduce de vuelta al del
        capítulo). `mientras(env)` corre con el robot vivo. Nunca deja procesos: si agmen run no se
        detiene, se matan él y sus nodos."""
        env = {**self.env, "AGMEN_HUB": f"127.0.0.1:{puerto_libre(socket.SOCK_DGRAM)}"}
        toml = carpeta / "robot.toml"
        texto = toml.read_text(encoding="utf-8")
        if re.search(r"^\s*\[web\]", texto, re.M):
            self.reporta("el robot.toml del capítulo ya trae [web]: tutorial_check no sabe darle al panel un "
                         "[web] listen libre (ajusta Cap19.agmen_run)", False, salida=texto)
            return ""
        self.listen = f"127.0.0.1:{puerto_libre(socket.SOCK_DGRAM)}"
        toml.write_text(f'{texto.rstrip()}\n\n[web]\nlisten = "{self.listen}"\n', encoding="utf-8")
        http = f"127.0.0.1:{puerto_libre(socket.SOCK_STREAM)}"
        grupo = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        proc = subprocess.Popen([sys.executable, "-m", "agmen", "run", "robot.toml", "--no-browser", "--http", http],
                                cwd=carpeta, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **grupo)
        lineas: list[str] = []

        # Se lee mientras corre: si nadie lee, la tubería se llena y agmen run se traba.
        lector = threading.Thread(target=lambda: lineas.extend(proc.stdout), daemon=True)
        lector.start()
        try:
            fin = time.monotonic() + tope
            while time.monotonic() < fin and proc.poll() is None and not listo(self.traduce("".join(lineas))):
                time.sleep(0.1)
            # Un poco más con el robot vivo: una línea que sale de más (un aviso repetido) suele salir justo
            # después de la primera, y faltan() la cuenta solo si alcanzó a salir.
            time.sleep(1.5)
            if mientras is not None and proc.poll() is None:
                mientras(env)
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                lector.join(5)
                for pid in re.findall(r"▶ iniciado \(pid (\d+)\)", "".join(lineas)):   # sus nodos, si quedaron
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
                    else:
                        try:
                            os.kill(int(pid), signal.SIGKILL)
                        except OSError:
                            pass
                lineas.append("\n(agmen run no se detuvo en 20 s con Ctrl+C: lo maté a él y a sus nodos)\n")
            lector.join(10)
        return self.traduce("".join(lineas))

    def robot(self) -> None:
        robot, politica = self.b["robot.toml"].texto, self.b["politica.py"].texto
        nombres = re.search(r'^names = (\[.*?\])', robot, re.M)
        if not self.reporta("robot.toml trae names = [...] en [[joints]]", nombres is not None, salida=robot):
            return
        lista = nombres.group(1)
        if not self.reporta("politica.py declara NOMBRES y names=NOMBRES en su Publisher",
                            f"NOMBRES = {lista}" in politica and ", names=NOMBRES)" in politica, salida=politica):
            return

        # 1. El orden bueno: la salida de la sección 4, y topic info y topic list de la sección 7.
        esperado = self.b["salida-run"]
        vistos: dict[str, str] = {}
        colgados: list[str] = []

        def consulta(env: dict) -> None:
            for clave, args in (("salida-topic-info", ["info", "/patas/estado"]), ("salida-topic-list", ["list"])):
                fin = time.monotonic() + 10
                while True:   # hasta que el hub tenga a todos (con tope)
                    r, parcial = correr([sys.executable, "-m", "agmen", "topic", *args], 30, env=env)
                    if r is None:
                        colgados.append(f"agmen topic {' '.join(args)}")
                        vistos[clave] = f"(agmen topic {' '.join(args)} se colgó más de 30 s)\n{parcial}"
                        break
                    vistos[clave] = self.traduce(r.stdout + r.stderr)
                    if not faltan(self.b[clave], vistos[clave]) or time.monotonic() > fin:
                        break
                    time.sleep(0.3)

        t0 = time.monotonic()
        salida = self.agmen_run(self.carpeta("bueno", robot, politica), lambda s: not faltan(esperado, s), 30,
                                consulta)
        self.compara(f"agmen run con el orden bueno: la salida de la sección 4 ({time.monotonic() - t0:.1f} s)",
                     esperado, salida)
        for que in colgados:
            self.reporta(f"{que} se colgó (más de 30 s) con el robot corriendo; lo maté a él y a sus hijos", False)
        for clave, que in (("salida-topic-info", "agmen topic info /patas/estado"),
                           ("salida-topic-list", "agmen topic list")):
            self.compara(f"{que} con el robot corriendo (sección 7)", self.b[clave], vistos.get(clave, ""))

        # 2. El orden cambiado en la política (FL_hip primero, como en la sección 5): el hub la rechaza.
        nombres_py = re.findall(r'"([^"]+)"', lista)
        cambiada = ["FL_hip"] + [n for n in nombres_py if n != "FL_hip"]
        malo = politica.replace(f"NOMBRES = {lista}", "NOMBRES = [" + ", ".join(f'"{n}"' for n in cambiada) + "]", 1)
        esperado = self.b["salida-orden"]
        salida = self.agmen_run(self.carpeta("orden", robot, malo), lambda s: not faltan(esperado, s), 20)
        self.compara("con el orden cambiado (FL_hip primero), el hub rechaza a la política", esperado, salida)

        # 3. Sin names= en el Publisher del comando: un aviso del hub y la política sigue.
        sin_lista = politica.replace(", names=NOMBRES)", ")", 1)
        esperado = self.b["salida-sin-lista"]
        salida = self.agmen_run(self.carpeta("sin-lista", robot, sin_lista), lambda s: not faltan(esperado, s), 20)
        self.compara("sin names= en el Publisher: el aviso del hub", esperado, salida)

        # 4. Lo mismo, con require_names = true en [[joints]]: el hub la rechaza.
        exige = robot.rstrip("\n") + "\nrequire_names = true\n"
        try:
            import tomllib

            j = tomllib.loads(exige)["joints"]
            bien = len(j) == 1 and j[0].get("require_names") is True
        except ImportError:   # Python < 3.11: agmen run lo revisa igual
            bien = True
        if self.reporta("require_names = true queda dentro de [[joints]] (es la última tabla del robot.toml)", bien,
                        salida=exige):
            esperado = self.b["salida-require_names"]
            salida = self.agmen_run(self.carpeta("exige", exige, sin_lista), lambda s: not faltan(esperado, s), 20)
            self.compara("con require_names = true, el hub rechaza al que publica sin lista", esperado, salida)

        # 5. Un nombre repetido en names: agmen run lo dice antes de arrancar nada (sección 4).
        esperado = self.b["salida-robot_error"]
        d = self.work / "repetido" / "patas"
        d.mkdir(parents=True)
        repetida = list(nombres_py)
        repetida[2] = repetida[0]
        (d / "robot_error.toml").write_text(
            robot.replace(lista, "[" + ", ".join(f'"{n}"' for n in repetida) + "]", 1), encoding="utf-8")
        # Con su propio hub en un puerto libre, como los demás: si por error arrancara, no toca el de nadie.
        env = {**self.env, "AGMEN_HUB": f"127.0.0.1:{puerto_libre(socket.SOCK_DGRAM)}"}
        r, parcial = correr([sys.executable, "-m", "agmen", "run", "patas/robot_error.toml", "--no-web"], 60,
                            cwd=d.parent, env=env)
        if r is None:
            self.reporta("un nombre repetido en names: agmen run se colgó (más de 60 s) en vez de salir con el "
                         "error; lo maté a él y a sus nodos", False, salida=parcial)
            return
        salida = r.stdout + r.stderr
        self.compara("un nombre repetido en names: agmen run no arranca y lo dice", esperado, salida)
        self.reporta("... y sale con código 2", r.returncode == 2, f"  salió con {r.returncode}")

    # -- los fragmentos cortos de Python ------------------------------------------------------

    def python(self, que: str, codigo: str) -> subprocess.CompletedProcess | None:
        """Corre un fragmento. None si se colgó (más de 60 s): ya se reportó FALLA y no quedó ningún proceso."""
        archivo = self.work / "fragmento.py"
        archivo.write_text(codigo, encoding="utf-8")
        env = {**self.env, "AGMEN_HUB": f"127.0.0.1:{puerto_libre(socket.SOCK_DGRAM)}"}
        r, parcial = correr([sys.executable, str(archivo)], 60, cwd=self.work, env=env)
        if r is None:
            self.reporta(f"{que}: el fragmento se colgó (más de 60 s); lo maté a él y a sus hijos", False,
                         salida=parcial)
        return r

    def error(self, que: str, codigo: str, esperado: str, ajuste=None) -> None:
        """El fragmento truena y la última línea (la que lee el usuario) es la del capítulo. `ajuste(texto)`, si
        se da, se aplica a las dos (lo esperado y lo real) antes de comparar: para un número que cambia."""
        r = self.python(que, codigo)
        if r is None:
            return
        ultima = (r.stderr.strip().splitlines() or [""])[-1]
        real = ultima if r.returncode != 0 else f"(no tronó)\n{r.stdout}"
        bloque = self.b[esperado]
        if ajuste is not None:
            bloque = Bloque(bloque.nombre, bloque.linea, [ajuste(t) for t in bloque.lineas])
            real = ajuste(real)
        self.compara(f"{que}: el error del capítulo", bloque, real)

    def impresos(self, que: str, bloque: Bloque) -> None:
        """Un fragmento con `print(...)  # lo que sale`: cada línea que imprime es el comentario."""
        esperados, lineas = [], bloque.numeradas()
        for k, (n, t) in enumerate(lineas):
            if t.startswith("print("):
                comentario = t.split("#", 1)[1].strip() if "#" in t else None
                if comentario is None and k + 1 < len(lineas) and lineas[k + 1][1].startswith("#"):
                    n, comentario = lineas[k + 1][0], lineas[k + 1][1][1:].strip()
                esperados.append((n, comentario or ""))
        r = self.python(que, bloque.texto)
        if r is None:
            return
        reales = r.stdout.splitlines()
        malas = [f"  {CAP19_REL}:{n} dice '{c}' y salió '{reales[i] if i < len(reales) else '(nada)'}'"
                 for i, (n, c) in enumerate(esperados) if i >= len(reales) or not impreso_igual(reales[i], c)]
        ok = r.returncode == 0 and len(reales) == len(esperados) and not malas
        self.reporta(f"{que}: imprime lo que dicen sus comentarios", ok,
                     "\n".join(malas) or f"  esperaba {len(esperados)} líneas", r.stdout + r.stderr)

    def fragmentos(self) -> None:
        self.impresos("JointState[4]() (sección 2)", self.b["nan-por-defecto"])
        self.impresos("JointCommand[4] y sus valores por defecto (sección 3)", self.b["comando-por-defecto"])
        con_set = self.b["names-set"].texto
        self.error("names= con un set", con_set, "error-names-set")
        # names=[] y la lista corta (sección 5 y 8): el capítulo solo muestra el error, el código
        # es el del set con otra lista.
        nombres = re.findall(r'"([^"]+)"', self.b["robot.toml"].texto.split("names = ", 1)[1].splitlines()[0])
        poner = re.compile(r"names=\{.*?\}")
        self.error("names=[]", poner.sub("names=[]", con_set, 1), "error-names-vacia")
        tres = "names=[" + ", ".join(f'"{n}"' for n in nombres[:3]) + "]"
        self.error("names= con 3 nombres y JointCommand[4]", poner.sub(lambda _: tres, con_set, 1),
                   "error-names-cuantos")
        self.error("un nombre con espacio", self.b["names-espacio"].texto, "error-names-espacio")
        self.error("JointState[5454]", self.b["jointstate-5454"].texto, "error-jointstate-5454")
        # El registro que no cabe (sección 8): el nodo, el topic y el tipo salen del propio error.
        texto = self.b["error-registro"].texto
        m = re.search(r"Publisher en '([^']+)'.*el nombre del nodo, (\d+) bytes; el esquema de (\w+)", texto)
        if self.reporta("el error del registro dice topic, tamaño del nombre y tipo", m is not None, salida=texto):
            topic, largo, tipo = m.groups()
            # El total depende de cuántos dígitos tiene el pid (la línea de registro lleva pid=): se enmascara en
            # los dos lados, y aparte se revisa que el número real sí pase el tope de un datagrama.
            medidas: list[int] = []
            mide = re.compile(r"mide (\d+) bytes y en un datagrama caben (\d+)")

            def sin_total(texto: str) -> str:
                for total, tope in mide.findall(texto):
                    medidas.append(int(total) - int(tope))
                return mide.sub(r"mide N bytes y en un datagrama caben \2", texto)
            self.error(f"un nombre de nodo de {largo} caracteres",
                       f"from agmen import Node\nfrom agmen.msgs import {tipo}\n\n"
                       f"nodo = Node('n' * {largo})\nnodo.publisher({tipo}[4], {topic!r})\n", "error-registro",
                       sin_total)
            self.reporta("... y el registro que dice medir pasa el tope de un datagrama",
                         len(medidas) == 2 and all(x > 0 for x in medidas),
                         f"  diferencias (medida - tope), capítulo y real: {medidas}")


def run_articulaciones() -> bool:
    """Capítulo 19 (RFC 0013 §3.4): JointState[N], JointCommand[N], [[joints]] y names=."""
    import shutil
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="agmen-cap19-"))
    t0 = time.monotonic()
    try:
        try:
            cap = Cap19(work)
        except (OSError, ValueError) as e:
            print(f"FALLA capítulo 19: no pude leer los bloques del capítulo: {e}")
            return False
        faltantes = [n for n in ("robot.toml", "simulador.py", "politica.py", "salida-run", "salida-orden",
                                 "salida-sin-lista", "salida-require_names", "salida-robot_error",
                                 "salida-topic-info", "salida-topic-list", "nan-por-defecto",
                                 "comando-por-defecto", "names-set", "error-names-set", "error-names-vacia",
                                 "error-names-cuantos", "names-espacio", "error-names-espacio", "jointstate-5454",
                                 "error-jointstate-5454", "error-registro") if n not in cap.b]
        if faltantes:
            print(f"FALLA capítulo 19: faltan las marcas <!-- tutorial_check: ... --> de: {', '.join(faltantes)}")
            return False
        if shutil.which("python", path=cap.env["PATH"]) is None:
            print("FALLA capítulo 19: el robot.toml del capítulo corre 'python simulador.py' y no hay ningún "
                  "'python' en el PATH. Instala Python con ese nombre (o un entorno virtual) y vuelve a correr")
            return False
        cap.fragmentos()
        cap.robot()
        print(f"     capítulo 19: {time.monotonic() - t0:.1f} s en total")
        return cap.ok
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _cap3():
    destino = f"127.0.0.1:{puerto_libre(socket.SOCK_DGRAM)}"
    return ([sys.executable, str(ROOT / "examples" / "tutorial" / "cerebro.py"), "40", "--destino", destino], 40,
            ("--escuchar", destino))


def main():
    sys.path.insert(0, str(ROOT / "python"))
    results = [
        run_case("capítulo 2: controlador C++ -> motor C++",
                 [str(BIN / f"tutorial_controlador{EXE}"), "100"], 100),
        # El capítulo 2 usa la dirección fija del ejemplo (127.0.0.1:7500: tutorial_controlador no deja
        # cambiarla); el 3, una libre (cerebro.py --destino y tutorial_motor --escuchar).
        run_case("capítulo 3: cerebro Python -> motor C++", *_cap3()),
        run_vision(),
        run_dataset(),
        run_articulaciones(),
    ]
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

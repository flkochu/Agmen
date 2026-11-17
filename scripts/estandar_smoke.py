"""Mensajes estándar (RFC 0013, bloque 1) de punta a punta, entre procesos, con un `agmen hub` real.

1. C++ publica Odometry e Imu (con NaN en la orientación) y Python los recibe, con el núcleo de
   C++ (`agmen._core`) y con Python puro. Los valores, los bits (NaN incluidos) y el t_data del
   sobre llegan intactos.
2. Python publica (los dos núcleos) y C++ los recibe: mismos bytes.
3. `agmen topic echo` (texto y --json) con una Imu sin orientación: nada falla y el JSON es
   válido (sin la palabra NaN).
4. `agmen record` -> `agmen info` -> `agmen play` -> `agmen export --parquet` y `--foxglove`, y el
   .mcap leído por mcap.js + agmen.js (el lector del panel, si hay Node).

    python scripts/estandar_smoke.py              # requiere haber compilado antes (build/bin/estandar_peer)
    python scripts/estandar_smoke.py --required   # en el CI: sin núcleo de C++, Node o pyarrow, falla
"""

import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))
EXE = ".exe" if sys.platform == "win32" else ""
BIN = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else ROOT / "build" / "bin"
T0 = 5_000_000_000


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------------------------
# Los valores del mensaje i: los mismos que arma tests/estandar_peer.cpp
# --------------------------------------------------------------------------------------------
def make(kind: str, i: int):
    from agmen import msgs

    if kind == "odometry":
        return msgs.Odometry(
            pose=msgs.Pose(position=msgs.Vector3(i + 0.5, -float(i), 0.25),
                           orientation=msgs.Quaternion.from_wxyz(0.5, 0.5, -0.5, 0.5)),
            linear_velocity=msgs.Vector3(0.1 * i, 0.2, 0.3), angular_velocity=msgs.Vector3(0.0, 0.0, -0.25))
    u = msgs.Imu(angular_velocity=msgs.Vector3f(0.5 * i, -0.02, 0.03), linear_acceleration=msgs.Vector3f(0.0, 0.0, 9.81))
    if i % 2 == 1:
        u.orientation = msgs.Quaternionf.from_wxyz(0.5, 0.5, -0.5, 0.5)
    return u


def worker() -> int:
    """`--py pub|sub odometry|imu /topic N`: el lado de Python (el núcleo lo elige AGMEN_BACKEND)."""
    import agmen
    from agmen import msgs

    mode, kind, topic, n = sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
    cls = msgs.Odometry if kind == "odometry" else msgs.Imu
    print("backend", agmen.backend() if hasattr(agmen, "backend") else "?", flush=True)
    if mode == "pub":
        pub = agmen.Publisher(cls, topic)
        for i in range(n):
            pub.publish(make(kind, i), t_data=T0 + 1000 * i)
            time.sleep(0.01)
        pub.close()
        return 0
    sub = agmen.Subscriber(cls, topic)
    print("LISTO", flush=True)
    got, end = 0, time.monotonic() + 20
    while got < n and time.monotonic() < end:
        s = sub.receive(0.1)
        if s is not None:
            # Una sola escritura por línea: con -u, print("RX", a, b) hace una escritura por pedazo, y algo
            # que escriba otro hilo en medio (o un terminate() a la mitad) partiría la línea.
            sys.stdout.write(f"RX {s.envelope.t_data} {s.message.encode_payload().hex()}\n")
            sys.stdout.flush()
            got += 1
    sub.close()
    return 0 if got else 2


# --------------------------------------------------------------------------------------------
REQUIRED = "--required" in sys.argv     # en el CI: sin núcleo de C++, estandar_peer, Node o pyarrow, falla
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"OK   {name}" if ok else f"FALLA {name}\n      {detail}", flush=True)
    failures += 0 if ok else 1


def missing(what: str) -> None:
    """Algo opcional que no está: con --required es una falla; sin él, se dice que se saltó."""
    global failures
    if REQUIRED:
        print(f"FALLA {what} (--required)", flush=True)
        failures += 1
    else:
        print(f"OMITIDO {what}", flush=True)


def wait_for(cond, timeout: float, every: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(every)
    return bool(cond())


class Proc:
    """Un proceso con dos hilos que van leyendo su stdout y su stderr mientras corre: así nunca se
    llena un pipe (en Windows son chicos), las esperas tienen un límite de verdad, y lo que el proceso
    escribe en stderr (avisos de otros hilos) nunca se mete a la mitad de una línea "RX …" de stdout."""

    def __init__(self, name: str, cmd, env):
        self.name = name
        self.lines: list[str] = []          # stdout: lo que se analiza (LISTO, RX, JSON)
        self.err: list[str] = []            # stderr: avisos, el "⏺ grabando" de agmen record...
        self.p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                  text=True, encoding="utf-8", errors="replace", env=env)
        self._readers = [threading.Thread(target=self._read, args=(f, out), daemon=True)
                         for f, out in ((self.p.stdout, self.lines), (self.p.stderr, self.err))]
        for t in self._readers:
            t.start()

    @staticmethod
    def _read(f, out: list) -> None:
        for line in f:
            out.append(line.rstrip("\r\n"))

    def text(self) -> str:
        """stdout."""
        return "\n".join(self.lines)

    def everything(self) -> str:
        """stdout y stderr, para mostrar cuando algo falla."""
        return self.text() + ("\n--- stderr ---\n" + "\n".join(self.err) if self.err else "")

    def tail(self, n: int = 8) -> str:
        return "\n".join(f"      | {x!r}" for x in (list(self.lines)[-n:] + [f"(stderr) {e}" for e in list(self.err)[-n:]]))

    def _seen(self, token: str) -> bool:
        return any(token in x for x in list(self.lines) + list(self.err))

    def wait_line(self, token: str, timeout: float = 20.0) -> bool:
        """Espera una línea con `token` (en stdout o stderr), con tope. Si se vence o el proceso termina antes, lo dice."""
        wait_for(lambda: self._seen(token) or self.p.poll() is not None, timeout)
        if self._seen(token):
            return True
        why = (f"el proceso terminó con código {self.p.returncode}" if self.p.poll() is not None
               else f"se venció el tope de {timeout:g} s")
        print(f"      {self.name}: esperaba una línea con {token!r} y no llegó ({why}); lo último que escribió:\n"
              f"{self.tail()}", flush=True)
        return False

    def wait(self, timeout: float) -> int | None:
        """Espera a que termine solo; si no, lo detiene. Devuelve su código (None si hubo que matarlo)."""
        try:
            code = self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop()
            code = None
        for t in self._readers:
            t.join(5)
        return code

    def stop(self) -> None:
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        for t in self._readers:
            t.join(5)


def hub_has_sub(hub_addr: str, topic: str, proc: "Proc") -> bool:
    """¿El hub ya tiene en `topic` un suscriptor de ESE proceso (por su pid)? Así se sabe que el receptor
    está listo, sin dormir, y no se confunde con otro que ya se fue (ej. el grabador del mismo topic)."""
    from agmen import hub as hubmod

    try:
        msgs = hubmod.query(hub_addr, timeout=0.5, say=None)
    except ConnectionError:
        return False
    pid = str(proc.p.pid)
    return any(op == "ep" and f.get("kind") == "sub" and f.get("topic") == topic and str(f.get("pid", "")) == pid
               for op, f in msgs)


def rx_lines(proc: "Proc", what: str):
    """Las líneas "RX t_data payload_hex" de un receptor. Una línea que empiece con RX y no tenga esa forma
    no truena: cuenta como FALLA y se muestra tal cual (repr), con lo último que escribió el proceso."""
    rows, odd = [], []
    for line in list(proc.lines):
        if not line.startswith("RX"):
            continue
        parts = line.split()
        try:
            if len(parts) != 3 or parts[0] != "RX":
                raise ValueError(f"se esperaban 3 campos, hay {len(parts)}")
            t, h = int(parts[1]), parts[2]
            bytes.fromhex(h)
        except ValueError as e:
            odd.append(f"{line!r} ({e})")
            continue
        rows.append((t, h))
    if odd:
        check(f"{what}: líneas RX bien formadas", False,
              f"{len(odd)} líneas raras de {proc.name}: {odd[:3]}\n      lo último que escribió:\n{proc.tail()}")
    return rows


def foxglove_json(path: str) -> tuple[int, list[str]]:
    """Cuántos mensajes trae el MCAP de --foxglove y cuáles no son JSON válido.

    Ojo (Windows): _mcap.Reader mapea el archivo a memoria y cada m.data es una vista de ese mapa. Si
    alguna vista sigue viva al cerrar (aunque sea la variable `m` del for), Reader.close() no puede
    cerrar el mapa y el archivo queda abierto: Windows no deja borrarlo. Por eso esto vive en su
    propia función, copia los bytes y suelta `m` antes de cerrar."""
    from agmen import _mcap

    bad, count = [], 0
    with _mcap.Reader(path) as r:
        for m in r.messages():
            data = bytes(m.data)
            count += 1
            try:
                strict_json(data.decode("utf-8"))
            except ValueError as e:
                bad.append(f"{data[:200]!r} ({e})")
        m = None                     # noqa: F841 — suelta la última vista antes de close()
    return count, bad


def cleanup(tmp: str) -> None:
    """Borra el directorio temporal. Si algo no se puede borrar (en Windows: un archivo que alguien
    todavía tiene abierto), es un AVISO que dice cuál, no un traceback: las pruebas ya se contaron."""
    left = []
    for root, dirs, files in os.walk(tmp, topdown=False):
        for name in files:
            try:
                os.unlink(os.path.join(root, name))
            except OSError as e:
                left.append(f"{os.path.join(root, name)}: {e}")
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError as e:
                left.append(f"{os.path.join(root, name)}: {e}")
    try:
        os.rmdir(tmp)
    except OSError as e:
        left.append(f"{tmp}: {e}")
    for x in left:
        print(f"AVISO no pude borrar {x} (¿quedó abierto?)", flush=True)


def strict_json(text):
    def bad(c):
        raise ValueError(f"constante no válida en JSON: {c}")
    return json.loads(text, parse_constant=bad)


def check_bytes(rows, kind, n):
    bad = []
    for t_data, h in rows:
        i = (t_data - T0) // 1000
        if t_data != T0 + 1000 * i or not 0 <= i < n:
            bad.append(f"t_data inesperado {t_data}")
        elif bytes.fromhex(h) != make(kind, i).encode_payload():
            bad.append(f"bytes distintos en i={i}")
    return bad


def main() -> int:
    from agmen import _backend, msgs
    from agmen.hub import ping

    hub_addr = f"127.0.0.1:{free_port()}"
    env0 = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8", "AGMEN_HUB": hub_addr}
    py = [sys.executable, "-u"]
    peer = str(BIN / f"estandar_peer{EXE}")
    if not Path(peer).exists():
        print(f"FALLA no existe {peer}: compila antes (cmake --build build --config Release)", flush=True)
        return 1
    if _backend.NATIVE:
        backends = ("native", "pure")
    else:
        missing(f"el núcleo de C++ (agmen._core no está compilado: {_backend.WHY or 'sin módulo'}): solo Python puro")
        backends = ("pure",)
    hub = Proc("agmen hub", py + ["-m", "agmen", "hub", "--listen", hub_addr], env0)
    procs = [hub]
    tmp = tempfile.mkdtemp(prefix="agmen-estandar-")
    try:
        if not wait_for(lambda: ping(hub_addr) is not None, 20, 0.1):
            check("el hub arranca", False, hub.everything())
            return 1
        n = 40
        for backend in backends:
            env = {**env0, "AGMEN_BACKEND": backend}
            for kind in ("odometry", "imu"):
                topic = f"/{kind}_{backend}"
                # 1) C++ -> Python
                sub = Proc("receptor de Python", py + [str(Path(__file__)), "--py", "sub", kind, topic, str(n)], env)
                procs.append(sub)
                ready = sub.wait_line("LISTO") and wait_for(lambda: hub_has_sub(hub_addr, topic, sub), 15, 0.1)
                cpp = subprocess.run([peer, "pub", kind, topic, str(n)], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                                     timeout=60)
                sub.wait(30)                   # termina solo al recibir n (o a los 20 s del propio receptor)
                rows = rx_lines(sub, f"C++ -> Python {kind} ({backend})")
                bad = check_bytes(rows, kind, n)
                check(f"C++ publica {kind} -> Python ({backend}): bytes y t_data intactos ({len(rows)}/{n} llegaron)",
                      ready and cpp.returncode == 0 and len(rows) >= n // 2 and not bad,
                      f"{bad[:3]}\npeer: {cpp.stdout}{cpp.stderr}\npython: {sub.everything()}")
                if kind == "imu" and rows:
                    # decodificado de verdad: NaN en pares, valores en impares
                    t0, h0 = rows[0]
                    m = msgs.Imu.decode_payload(bytes.fromhex(h0))
                    i = (t0 - T0) // 1000
                    check(f"  la orientación sin medir llega como NaN ({backend})",
                          math.isnan(m.orientation.w) == (i % 2 == 0), repr(m))
                # 2) Python -> C++
                topic2 = topic + "_rev"
                cpp_sub = Proc("receptor de C++", [peer, "sub", kind, topic2, str(n)], env)
                procs.append(cpp_sub)
                ready = cpp_sub.wait_line("LISTO") and wait_for(lambda: hub_has_sub(hub_addr, topic2, cpp_sub), 15, 0.1)
                pyp = subprocess.run(py + [str(Path(__file__)), "--py", "pub", kind, topic2, str(n)],
                                     capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=60)
                cpp_sub.wait(20)
                rows = rx_lines(cpp_sub, f"Python -> C++ {kind} ({backend})")
                bad = check_bytes(rows, kind, n)
                check(f"Python ({backend}) publica {kind} -> C++: bytes y t_data intactos ({len(rows)}/{n})",
                      ready and pyp.returncode == 0 and len(rows) >= n // 2 and not bad,
                      f"{bad[:3]}\npython: {pyp.stdout}{pyp.stderr}\ncpp: {cpp_sub.everything()}")

        # 3) topic echo con Imu sin orientación (los pares)
        for k, flag in enumerate(([], ["--json"])):
            topic = f"/imu_echo{k}"
            echo = Proc("agmen topic echo",
                        py + ["-m", "agmen", "topic", "echo", topic, "--max-hz", "0", "--count", "6", *flag], env0)
            procs.append(echo)
            ready = wait_for(lambda: hub_has_sub(hub_addr, topic, echo), 20, 0.1)
            if not ready:
                print(f"      agmen topic echo no apareció en el hub como suscriptor de {topic}: {echo.everything()}")
            subprocess.run([peer, "pub", "imu", topic, "60"], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env0, timeout=60)
            code = echo.wait(20)
            out = echo.text()
            if flag:
                bad_lines, rows = [], 0
                for x in out.splitlines():
                    if x.startswith("{"):
                        try:
                            strict_json(x)
                            rows += 1
                        except ValueError as e:
                            bad_lines.append(f"{x[:300]!r} ({e})")
                check("agmen topic echo --json con Imu sin orientación: JSON válido (sin NaN)",
                      ready and code == 0 and rows >= 1 and not bad_lines,
                      f"{len(bad_lines)} líneas que no son JSON válido: {bad_lines[:2]}\n{echo.everything()[:1500]}")
            else:
                check("agmen topic echo (texto) con Imu sin orientación: no falla y escribe nan",
                      ready and code == 0 and "Traceback" not in echo.everything() and "nan" in out.lower(),
                      echo.everything()[:1500])

        # 4) record -> info -> play -> export
        mcap = str(Path(tmp) / "imu.mcap")
        # --duration: el grabador termina solo y cierra el archivo, sin señales (igual en Windows y Linux).
        # Sin --quiet: dice "⏺ grabando …" cuando ya está grabando, y "⏹ …: N mensajes" al terminar.
        rec = Proc("agmen record", py + ["-m", "agmen", "record", "/imu_rec", "-o", mcap, "--duration", "8s"], env0)
        procs.append(rec)
        ready = rec.wait_line("grabando") and wait_for(lambda: hub_has_sub(hub_addr, "/imu_rec", rec), 15, 0.1)
        subprocess.run([peer, "pub", "imu", "/imu_rec", "40"], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env0, timeout=60)
        code = rec.wait(60)
        summary = next((x for x in rec.lines + rec.err if "⏹" in x), "")
        found = re.search(r"(\d+) mensajes", summary)
        recorded = int(found.group(1)) if found else 0
        check(f"agmen record graba la Imu ({recorded} mensajes)",
              ready and code == 0 and os.path.exists(mcap) and recorded > 0,
              f"la grabación quedó {'vacía' if found else 'sin resumen'}: el grabador "
              f"{'no llegó a decir que estaba grabando' if not ready else 'no recibió nada'}\n{rec.everything()}")

        def cli(*args, timeout=60):
            return subprocess.run(py + ["-m", "agmen", *args], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", env=env0, timeout=timeout)

        info = cli("info", mcap)
        check("agmen info lee la grabación con Imu", info.returncode == 0 and "/imu_rec" in info.stdout
              and "Traceback" not in info.stderr, info.stdout + info.stderr)
        # play: un receptor del topic para ver que sale NaN en lo reproducido
        sub = Proc("receptor de Python", py + [str(Path(__file__)), "--py", "sub", "imu", "/imu_rec", "5"], env0)
        procs.append(sub)
        ready = sub.wait_line("LISTO") and wait_for(lambda: hub_has_sub(hub_addr, "/imu_rec", sub), 15, 0.1)
        play = cli("play", mcap, "--quiet", "--no-clock", timeout=90)
        sub.wait(20)
        check("agmen play reproduce la Imu con NaN (un receptor la recibe)",
              ready and play.returncode == 0 and len(rx_lines(sub, "agmen play -> Python")) >= 1 and "Traceback" not in play.stderr,
              f"{play.stdout}{play.stderr}\n{sub.everything()}")
        if _has_module("pyarrow"):
            pq = str(Path(tmp) / "pq")
            exp = cli("export", mcap, "--parquet", pq)
            check("agmen export --parquet con NaN", exp.returncode == 0 and "Traceback" not in exp.stderr,
                  exp.stdout + exp.stderr)
        else:
            missing("agmen export --parquet: falta pyarrow (pip install pyarrow)")
        fox = str(Path(tmp) / "fox.mcap")
        exp2 = cli("export", mcap, "--foxglove", fox)
        check("agmen export --foxglove con NaN", exp2.returncode == 0 and os.path.exists(fox),
              exp2.stdout + exp2.stderr)
        if os.path.exists(fox):
            count, bad_json = foxglove_json(fox)
            check(f"el JSON de --foxglove es válido, sin NaN ({count} mensajes)", count > 0 and not bad_json,
                  str(bad_json[:2]))
        # el lector JavaScript del panel
        if _has("node"):
            js = r"""
const M = require(process.argv[2]), A = require(process.argv[3]), fs = require("fs");
(async () => {
  const b = fs.readFileSync(process.argv[4]);
  const r = await M.open({ name: "", size: b.length, read(p, n) { return Promise.resolve(b.subarray(p, p + n)); } });
  const msgs = await r.allMessages(); let nan = 0, total = 0;
  for (const m of msgs) {
    const sch = r.schemas.get(r.channels.get(m.channelId).schemaId);
    const L = new A.Layout(new TextDecoder().decode(sch.data)); const env = A.parseEnvelope(m.data);
    const rows = L.decodeRows(env.payload); const flat = A.flatten(rows[0]); A.summarize(rows[0]);
    total++; if (Number.isNaN(flat["orientation.w"])) nan++;
  }
  console.log(JSON.stringify({ total, nan }));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
"""
            jsf = Path(tmp) / "lee.js"
            jsf.write_text(js, encoding="utf-8")
            pn = ROOT / "python" / "agmen" / "panel"
            r = subprocess.run(["node", str(jsf), str(pn / "mcap.js"), str(pn / "agmen.js"), mcap], capture_output=True,
                               text=True, encoding="utf-8", timeout=60)
            try:
                res = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
            except ValueError:
                res = {}                         # la FALLA de abajo muestra r.stdout tal cual
            if not isinstance(res, dict):
                res = {}
            check("mcap.js + agmen.js leen la grabación (NaN en las Imu pares)",
                  r.returncode == 0 and res.get("total", 0) > 0 and res.get("nan", 0) > 0,
                  f"stdout={r.stdout!r} stderr={r.stderr!r}")
        else:
            missing("mcap.js + agmen.js: Node no está instalado")
    finally:
        for p in procs:
            p.stop()                         # todos los hijos terminados (wait) antes de limpiar
        cleanup(tmp)
    if failures:
        print(f"\n--- salida del hub ---\n{hub.everything()[-3000:]}")
    print(f"\n{'TODO BIEN' if not failures else str(failures) + ' FALLAS'}")
    return 1 if failures else 0


def _has_module(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


def _has(cmd: str) -> bool:
    import shutil
    return shutil.which(cmd) is not None


if __name__ == "__main__":
    sys.exit(worker() if len(sys.argv) > 1 and sys.argv[1] == "--py" else main())

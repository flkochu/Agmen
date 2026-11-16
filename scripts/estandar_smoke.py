"""Mensajes estándar (RFC 0013, bloque 1) de punta a punta, entre procesos, con un `agmen hub` real.

1. C++ publica Odometry e Imu (con NaN en la orientación) y Python los recibe, con el núcleo de
   C++ (`agmen._core`) y con Python puro. Los valores, los bits (NaN incluidos) y el t_data del
   sobre llegan intactos.
2. Python publica (los dos núcleos) y C++ los recibe: mismos bytes.
3. `agmen topic echo` (texto y --json) con una Imu sin orientación: nada falla y el JSON es
   válido (sin la palabra NaN).
4. `agmen record` -> `agmen info` -> `agmen play` -> `agmen export --parquet` y `--foxglove`, y el
   .mcap leído por mcap.js + agmen.js (el lector del panel, si hay Node).

    python scripts/estandar_smoke.py          # requiere haber compilado antes (build/bin/estandar_peer)
"""

import json
import math
import os
import socket
import subprocess
import sys
import tempfile
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
            print("RX", s.envelope.t_data, s.message.encode_payload().hex(), flush=True)
            got += 1
    sub.close()
    return 0 if got else 2


# --------------------------------------------------------------------------------------------
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"OK   {name}" if ok else f"FALLA {name}\n      {detail}", flush=True)
    failures += 0 if ok else 1


def start(cmd, env):
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", env=env)


def wait_line(p, token, timeout=15):
    """Lee la salida de p hasta ver `token`; devuelve lo leído."""
    seen, end = [], time.monotonic() + timeout
    while time.monotonic() < end:
        line = p.stdout.readline()
        if not line:
            break
        seen.append(line)
        if token in line:
            break
    return "".join(seen)


def rx_lines(out):
    rows = []
    for line in out.splitlines():
        if line.startswith("RX "):
            _, t, h = line.split()
            rows.append((int(t), h))
    return rows


def strict_json(text):
    def bad(c):
        raise ValueError(f"constante no válida en JSON: {c}")
    return json.loads(text, parse_constant=bad)


def main() -> int:
    from agmen import msgs

    hub_addr = f"127.0.0.1:{free_port()}"
    env0 = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8", "AGMEN_HUB": hub_addr}
    py = [sys.executable, "-u"]
    peer = str(BIN / f"estandar_peer{EXE}")
    hub = start(py + ["-m", "agmen", "hub", "--listen", hub_addr], env0)
    procs = [hub]
    tmp = tempfile.TemporaryDirectory()
    try:
        time.sleep(1.0)
        n = 40
        for backend in ("native", "pure"):
            env = {**env0, "AGMEN_BACKEND": backend}
            for kind in ("odometry", "imu"):
                topic = f"/{kind}_{backend}"
                # 1) C++ -> Python
                sub = start(py + [str(Path(__file__)), "--py", "sub", kind, topic, str(n)], env)
                procs.append(sub)
                head = wait_line(sub, "LISTO")
                time.sleep(0.5)
                cpp = subprocess.run([peer, "pub", kind, topic, str(n)], capture_output=True, text=True, env=env,
                                     timeout=60)
                time.sleep(1.0)
                sub.terminate()
                out = head + sub.communicate(timeout=10)[0]
                rows = rx_lines(out)
                bad = []
                for t_data, h in rows:
                    i = (t_data - T0) // 1000
                    if t_data != T0 + 1000 * i or not 0 <= i < n:
                        bad.append(f"t_data inesperado {t_data}")
                    elif bytes.fromhex(h) != make(kind, i).encode_payload():
                        bad.append(f"bytes distintos en i={i}")
                check(f"C++ publica {kind} -> Python ({backend}): bytes y t_data intactos ({len(rows)}/{n} llegaron)",
                      cpp.returncode == 0 and len(rows) >= n // 2 and not bad,
                      f"{bad[:3]}\npeer: {cpp.stdout}{cpp.stderr}\npython: {out}")
                if kind == "imu" and rows:
                    # decodificado de verdad: NaN en pares, valores en impares
                    from agmen import wire
                    t0, h0 = rows[0]
                    m = msgs.Imu.decode_payload(bytes.fromhex(h0))
                    i = (t0 - T0) // 1000
                    nan_ok = math.isnan(m.orientation.w) == (i % 2 == 0)
                    check(f"  la orientación sin medir llega como NaN ({backend})", nan_ok, repr(m))
                # 2) Python -> C++
                topic2 = topic + "_rev"
                cpp_sub = start([peer, "sub", kind, topic2, str(n)], env)
                procs.append(cpp_sub)
                head = wait_line(cpp_sub, "LISTO")
                time.sleep(0.5)
                pyp = subprocess.run(py + [str(Path(__file__)), "--py", "pub", kind, topic2, str(n)],
                                     capture_output=True, text=True, env=env, timeout=60)
                time.sleep(1.0)
                cpp_sub.terminate()
                out = head + cpp_sub.communicate(timeout=10)[0]
                rows = rx_lines(out)
                bad = []
                for t_data, h in rows:
                    i = (t_data - T0) // 1000
                    if t_data != T0 + 1000 * i or not 0 <= i < n:
                        bad.append(f"t_data inesperado {t_data}")
                    elif bytes.fromhex(h) != make(kind, i).encode_payload():
                        bad.append(f"bytes distintos en i={i}")
                check(f"Python ({backend}) publica {kind} -> C++: bytes y t_data intactos ({len(rows)}/{n})",
                      pyp.returncode == 0 and len(rows) >= n // 2 and not bad,
                      f"{bad[:3]}\npython: {pyp.stdout}{pyp.stderr}\ncpp: {out}")

        # 3) topic echo con Imu sin orientación (los pares)
        for flag in ([], ["--json"]):
            topic = "/imu_echo"
            echo = start(py + ["-m", "agmen", "topic", "echo", topic, "--max-hz", "0", "--count", "6", *flag], env0)
            procs.append(echo)
            time.sleep(1.5)
            subprocess.run([peer, "pub", "imu", topic, "60"], capture_output=True, text=True, env=env0, timeout=60)
            try:
                out = echo.communicate(timeout=20)[0]
            except subprocess.TimeoutExpired:
                echo.kill()
                out = echo.communicate()[0]
            if flag:
                try:
                    rows = [strict_json(x) for x in out.splitlines() if x.startswith("{")]
                    ok = len(rows) >= 1
                    err = ""
                except ValueError as e:
                    ok, err = False, str(e)
                check("agmen topic echo --json con Imu sin orientación: JSON válido (sin NaN)", ok, f"{err}\n{out[:800]}")
            else:
                check("agmen topic echo (texto) con Imu sin orientación: no falla y escribe nan",
                      echo.returncode in (0, None) and "Traceback" not in out and "nan" in out.lower(), out[:800])

        # 4) record -> info -> play -> export
        mcap = str(Path(tmp.name) / "imu.mcap")
        rec = start(py + ["-m", "agmen", "record", "/imu_rec", "-o", mcap, "--quiet"], env0)
        procs.append(rec)
        time.sleep(1.5)
        subprocess.run([peer, "pub", "imu", "/imu_rec", "40"], capture_output=True, text=True, env=env0, timeout=60)
        time.sleep(1.0)
        rec.terminate()                     # SIGTERM: el grabador cierra el archivo
        rec_out = rec.communicate(timeout=20)[0]
        check("agmen record cierra el archivo", os.path.exists(mcap) and os.path.getsize(mcap) > 0, rec_out)

        def cli(*args, timeout=60):
            return subprocess.run(py + ["-m", "agmen", *args], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", env=env0, timeout=timeout)

        info = cli("info", mcap)
        check("agmen info lee la grabación con Imu", info.returncode == 0 and "/imu_rec" in info.stdout
              and "Traceback" not in info.stderr, info.stdout + info.stderr)
        # play: un receptor del topic para ver que sale NaN en lo reproducido
        sub = start(py + [str(Path(__file__)), "--py", "sub", "imu", "/imu_rec", "5"], env0)
        procs.append(sub)
        head = wait_line(sub, "LISTO")
        time.sleep(0.5)
        play = cli("play", mcap, "--quiet", "--no-clock", timeout=90)
        time.sleep(0.5)
        sub.terminate()
        out = head + sub.communicate(timeout=10)[0]
        check("agmen play reproduce la Imu con NaN (un receptor la recibe)",
              play.returncode == 0 and len(rx_lines(out)) >= 1 and "Traceback" not in play.stderr,
              f"{play.stdout}{play.stderr}\n{out}")
        pq = str(Path(tmp.name) / "pq")
        exp = cli("export", mcap, "--parquet", pq)
        check("agmen export --parquet con NaN", exp.returncode == 0 and "Traceback" not in exp.stderr,
              exp.stdout + exp.stderr)
        fox = str(Path(tmp.name) / "fox.mcap")
        exp2 = cli("export", mcap, "--foxglove", fox)
        check("agmen export --foxglove con NaN", exp2.returncode == 0 and os.path.exists(fox),
              exp2.stdout + exp2.stderr)
        if os.path.exists(fox):
            from agmen import _mcap

            bad_json, count = [], 0
            with _mcap.Reader(fox) as r:
                for m in r.messages():
                    count += 1
                    try:
                        strict_json(bytes(m.data).decode("utf-8"))
                    except ValueError as e:
                        bad_json.append(str(e))
            check(f"el JSON de --foxglove es válido, sin NaN ({count} mensajes)", count > 0 and not bad_json,
                  str(bad_json[:2]))
        # el lector JavaScript del panel
        node = subprocess.run(["node", "--version"], capture_output=True, text=True) if _has("node") else None
        if node is not None:
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
            jsf = Path(tmp.name) / "lee.js"
            jsf.write_text(js, encoding="utf-8")
            pn = ROOT / "python" / "agmen" / "panel"
            r = subprocess.run(["node", str(jsf), str(pn / "mcap.js"), str(pn / "agmen.js"), mcap], capture_output=True,
                               text=True, encoding="utf-8")
            res = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
            check("mcap.js + agmen.js leen la grabación (NaN en las Imu pares)",
                  r.returncode == 0 and res.get("total", 0) > 0 and res.get("nan", 0) > 0, r.stdout + r.stderr)
        else:
            print("OMITIDO mcap.js: Node no está instalado")
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        tmp.cleanup()
    print(f"\n{'TODO BIEN' if not failures else str(failures) + ' FALLAS'}")
    return 1 if failures else 0


def _has(cmd: str) -> bool:
    import shutil
    return shutil.which(cmd) is not None


if __name__ == "__main__":
    sys.exit(worker() if len(sys.argv) > 1 and sys.argv[1] == "--py" else main())

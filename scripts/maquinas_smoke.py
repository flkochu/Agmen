"""Prueba de dos computadoras (RFC 0011 §6 reto 9, bloque 4c): `agmen agent` en el
"robot" y `agmen run` en la "laptop", con el robot de dos computadoras del tutorial
(`examples/tutorial/robot_dos.toml`).

Dos modos; la primera línea de la salida dice cuál se usó:

- aislado (Linux con root o `sudo -n`, y `ip`, `unshare`, `nsenter`): dos "computadoras"
  hechas con espacios de nombres del kernel (el mismo aislamiento de un contenedor, sin
  imágenes). Cada una tiene su red (un par veth entre las dos: 10.77.0.1 y 10.77.0.2) y
  su propio /dev/shm. Los procesos corren como el usuario normal, no como root.
- loopback (Windows, macOS, o Linux sin permisos): las dos en esta máquina, en dos IP de
  loopback (AGMEN_NET_IP), con hubs en puertos distintos y AGMEN_SHM=0.

Se elige con AGMEN_SMOKE_MODO=aislado|loopback; sin ella, aislado si se puede y si no
loopback (diciéndolo). En el CI de Linux (variable CI) aislado es obligatorio: si no se
puede, falla y dice qué faltó. Todo lo creado se borra, también si falla o con Ctrl+C.

Requiere haber compilado antes (usa build/bin/publisher). Solo librería estándar.

    python scripts/maquinas_smoke.py
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# En Windows, la salida a un archivo/CI usa cp1252 y truena con acentos o "→".
for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
LINUX = sys.platform.startswith("linux")
NT = os.name == "nt"
results: list[bool] = []
notas: list[str] = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'OK  ' if ok else 'FALLA'} {name}" + (f" — {detail}" if detail and not ok else ""), flush=True)


def no_probado(name, motivo):
    """Una comprobación que aquí no se pudo hacer: se anota, no cuenta como OK."""
    notas.append(name)
    print(f"NO SE PUDO PROBAR {name} — {motivo}", flush=True)


def wait_for(pred, timeout=15.0, paso=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if pred():
                return True
        except (OSError, ValueError, KeyError, StopIteration):
            pass
        time.sleep(paso)
    return False


class Falta(Exception):
    """Algo que el modo aislado necesita y no hay."""


# ---------------------------------------------------------------- los dos modos

def _sudo_n() -> bool:
    try:
        return subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _requisitos_aislado() -> str | None:
    """None si el modo aislado se puede; si no, qué falta."""
    if not LINUX:
        return "solo existe en Linux"
    faltan = [t for t in ("ip", "unshare", "nsenter") if not shutil.which(t)]
    if faltan:
        return f"faltan {', '.join(faltan)} (en Debian/Ubuntu: sudo apt-get install -y iproute2 util-linux)"
    if os.geteuid() != 0 and not (shutil.which("sudo") and _sudo_n()):
        return "hace falta ser root o tener `sudo -n` (sudo sin contraseña) para crear espacios de nombres de red"
    try:   # ¿el entorno deja crearlos? (algunos contenedores no)
        if os.geteuid() == 0:
            r = subprocess.run(["unshare", "--net", "--mount", "true"], capture_output=True, text=True, timeout=20)
        else:
            r = subprocess.run(["sudo", "-n", "unshare", "--net", "--mount", "true"], capture_output=True, text=True,
                               timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"unshare no corrió: {e}"
    if r.returncode != 0:
        return f"este entorno no deja crear espacios de nombres (unshare: {r.stderr.strip()})"
    return None


def elegir_modo() -> str:
    pedido = os.environ.get("AGMEN_SMOKE_MODO", "").strip().lower()
    if pedido not in ("", "aislado", "loopback"):
        sys.exit(f"AGMEN_SMOKE_MODO={pedido!r} no es válido: usa 'aislado' o 'loopback' (o quita la variable).")
    obligatorio = pedido == "aislado" or (not pedido and LINUX and bool(os.environ.get("CI")))
    if pedido == "loopback":
        return "loopback"
    falta = _requisitos_aislado()
    if falta is None:
        return "aislado"
    if obligatorio:
        sys.exit(f"FALLA modo aislado no disponible: {falta}.\n"
                 + ("(En el CI de Linux este modo es obligatorio.)" if not pedido else
                    "(Lo pediste con AGMEN_SMOKE_MODO=aislado.)"))
    print(f"(aislado no disponible: {falta}; uso loopback)")
    return "loopback"


def uid_normal() -> tuple[int, int]:
    """El usuario con el que corren los procesos dentro de las 'computadoras'."""
    if os.geteuid() != 0:
        return os.geteuid(), os.getegid()
    if os.environ.get("SUDO_UID"):
        return int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", os.environ["SUDO_UID"]))
    return 65534, 65534   # nobody


def recorrible_por(carpeta: Path, uid: int) -> bool:
    """¿Puede el usuario `uid` entrar a `carpeta` (cada carpeta del camino con permiso de paso)?"""
    for d in [carpeta.resolve(), *carpeta.resolve().parents]:
        st = d.stat()
        if not (st.st_mode & 0o001 or (st.st_uid == uid and st.st_mode & 0o100)):
            return False
    return True


class Aislado:
    """Dos espacios de nombres (red + montajes), unidos por un par veth. Se borran al morir sus 'holder'."""

    IPS = ("10.77.0.1", "10.77.0.2")

    def __init__(self, workdir: Path):
        self.uid, self.gid = uid_normal()
        self.sufijo = str(os.getpid())
        self.holders: dict[str, subprocess.Popen] = {}
        self.workdir = workdir

    def _root(self, args: list[str], **kw):
        return subprocess.run(args, capture_output=True, text=True, timeout=30, **kw)

    def crear(self):
        for nombre in ("laptop", "robot"):
            # unshare crea el espacio y ahí se monta un /dev/shm propio. Muere solo en 30 min si nadie lo recoge.
            self.holders[nombre] = subprocess.Popen(
                ["unshare", "--net", "--mount", "--", "sh", "-c",
                 "mount -t tmpfs -o mode=1777 tmpfs /dev/shm && exec sleep 1800"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        host_shm = os.stat("/dev/shm").st_dev
        for nombre, h in self.holders.items():
            if not wait_for(lambda h=h: os.stat(f"/proc/{h.pid}/root/dev/shm").st_dev != host_shm, 15):
                raise Falta(f"el espacio de nombres '{nombre}' no se creó: "
                            f"{h.stdout.read().decode(errors='replace') if h.poll() is not None else 'sin /dev/shm propio'}")
        a, b = f"vl{self.sufijo}", f"vr{self.sufijo}"
        self.veth = (a, b)
        pasos = [(["ip", "link", "add", a, "type", "veth", "peer", "name", b], None),
                 (["ip", "link", "set", a, "netns", str(self.holders["laptop"].pid)], None),
                 (["ip", "link", "set", b, "netns", str(self.holders["robot"].pid)], None)]
        for args, _ in pasos:
            r = self._root(args)
            if r.returncode:
                raise Falta(f"`{' '.join(args)}` falló: {r.stderr.strip()}")
        for nombre, dev, ip in (("laptop", a, self.IPS[0]), ("robot", b, self.IPS[1])):
            for args in (["ip", "addr", "add", f"{ip}/24", "dev", dev], ["ip", "link", "set", dev, "up"],
                         ["ip", "link", "set", "lo", "up"]):
                r = self.en(nombre, args)
                if r.returncode:
                    raise Falta(f"en '{nombre}', `{' '.join(args)}` falló: {r.stderr.strip()}")
        # Que las dos se ven: un connect TCP de una a la otra no hace falta; el ARP lo hace el primer paquete.

    def en(self, nombre: str, args: list[str]):
        """Un comando como root dentro de la red de `nombre` (ip, iptables...)."""
        return self._root(["nsenter", "-t", str(self.holders[nombre].pid), "-n", "--", *args])

    def prefijo(self, nombre: str) -> list[str]:
        """Para lanzar un proceso del usuario normal dentro de `nombre`."""
        return ["nsenter", "-t", str(self.holders[nombre].pid), "-n", "-m", "-S", str(self.uid), "-G", str(self.gid), "--"]

    def red_de(self, nombre: str) -> str:
        return os.readlink(f"/proc/{self.holders[nombre].pid}/ns/net")

    def borrar(self):
        for h in self.holders.values():
            if h.poll() is None:
                h.kill()
        for h in self.holders.values():
            try:
                h.wait(10)
            except subprocess.TimeoutExpired:
                pass
            if h.stdout:
                h.stdout.close()
        self.holders.clear()


# ---------------------------------------------------------------- procesos

class Proc:
    """Un proceso `agmen ...`: su salida se lee MIENTRAS corre (nunca una tubería sin leer; en Windows es chica)."""

    def __init__(self, nombre: str, args: list[str], env: dict, cwd: Path, prefijo: list[str], pilas: Path):
        self.nombre = nombre
        self.pilas = pilas
        # Cada 20 s deja sus pilas en un archivo, que solo se muestra si algo falla (como run_smoke).
        con_pilas = ("import faulthandler, runpy; faulthandler.dump_traceback_later(20, repeat=True, "
                     f"file=open({str(pilas)!r}, 'w')); runpy.run_module('agmen', run_name='__main__', alter_sys=True)")
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if NT else {}
        self.p = subprocess.Popen([*prefijo, sys.executable, "-c", con_pilas, *args], cwd=cwd, env=env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding="utf-8", errors="replace", **kw)
        self.lines: list[str] = []
        self.reader = threading.Thread(target=lambda: self.lines.extend(self.p.stdout), daemon=True)
        self.reader.start()

    @property
    def out(self) -> str:
        return "".join(self.lines)

    def dijo(self, texto: str) -> bool:
        return texto in self.out

    def ctrl_c(self):
        if self.p.poll() is None:
            self.p.send_signal(signal.CTRL_BREAK_EVENT if NT else signal.SIGINT)

    def detener(self, timeout=25) -> int | None:
        self.ctrl_c()
        try:
            self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        self.reader.join(10)
        return self.p.returncode

    def vivo(self) -> bool:
        return self.p.poll() is None


def free_port(udp=False) -> int:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


def toml_del_robot(dest: Path, ip: str, puerto: int) -> Path:
    """El robot de dos computadoras del tutorial, con la IP de esta corrida y dos nodos de apoyo.
    Las rutas son absolutas (el archivo vive en otra carpeta)."""
    texto = (ROOT / "examples" / "tutorial" / "robot_dos.toml").read_text(encoding="utf-8")
    base = ROOT.as_posix()
    for viejo, nuevo in (('host = "192.168.1.20"', f'host = "{ip}:{puerto}"'),
                         ("../../build/bin/publisher", f"{base}/build/bin/publisher"),
                         ("python cerebro.py", f"python {base}/examples/tutorial/cerebro.py")):
        if viejo not in texto:
            sys.exit(f"examples/tutorial/robot_dos.toml cambió: ya no tiene {viejo!r}; ajusta scripts/maquinas_smoke.py")
        texto = texto.replace(viejo, nuevo)
    nodo = json.dumps(f"{base}/scripts/maquinas_nodo.py")
    marcas = json.dumps(str(dest.parent / "marcas"))
    texto += (f'\n[[node]]\nname = "grande"          # de apoyo: manda /grande (TCP) y recibe /ping\n'
              f'cmd = ["python", {nodo}, "robot", {marcas}]\nmachine = "robot"\n'
              f'\n[[node]]\nname = "ojo"             # de apoyo, en la laptop: recibe /grande y manda /ping\n'
              f'cmd = ["python", {nodo}, "laptop", {marcas}]\n\n[web]\nenabled = false\n')
    dest.write_text(texto, encoding="utf-8")
    return dest


# ---------------------------------------------------------------- la prueba

def main():
    modo = elegir_modo()
    if modo == "aislado" and os.geteuid() != 0:
        # Hace falta root para los espacios de nombres: se vuelve a lanzar con sudo, conservando el entorno.
        print("(pido sudo para crear los espacios de nombres)", flush=True)
        os.execvp("sudo", ["sudo", "-n", "-E", "env", f"PATH={os.environ.get('PATH', '')}", sys.executable,
                           str(Path(__file__).resolve()), *sys.argv[1:]])
    print(f"modo: {modo}" + (" (dos espacios de nombres de red y de /dev/shm, procesos como usuario normal)"
                              if modo == "aislado" else
                              " (dos IP de loopback, AGMEN_SHM=0, hubs en puertos distintos)"), flush=True)
    t0 = time.monotonic()
    if not NT:   # kill <pid> o cerrar la terminal también limpian (como Ctrl+C)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    base_tmp = None
    if modo == "aislado" and not recorrible_por(Path(tempfile.gettempdir()), uid_normal()[0]):
        base_tmp = "/tmp"   # el TMPDIR de aquí no lo puede cruzar el usuario normal: la carpeta de trabajo va en /tmp
    tmp = Path(tempfile.mkdtemp(prefix="agmen_maquinas_", dir=base_tmp))
    aislado = Aislado(tmp) if modo == "aislado" else None
    if aislado:
        os.chown(tmp, aislado.uid, aislado.gid)
    os.chmod(tmp, 0o777)
    marcas = tmp / "marcas"
    marcas.mkdir()
    os.chmod(marcas, 0o777)
    procs: list[Proc] = []
    exito = False
    try:
        # --- las dos computadoras
        base_env = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1",
                    "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(tmp), "TEMP": str(tmp), "TMP": str(tmp),
                    "HOME": str(tmp), "AGMEN_REMOTE_HOSTS": "", "AGMEN_AGENT_KEY": ""}
        for v in ("AGMEN_HUB", "AGMEN_NET_IP", "AGMEN_SHM"):
            base_env.pop(v, None)
        if aislado:
            aislado.crear()
            ip_laptop, ip_robot = aislado.IPS
            puerto = 7391
            envs = {"laptop": dict(base_env), "robot": dict(base_env)}
            pref = {n: aislado.prefijo(n) for n in ("laptop", "robot")}
            ping = aislado.en("laptop", ["ping", "-c", "1", "-W", "2", ip_robot]) if shutil.which("ping") else None
            if ping is not None and ping.returncode:
                raise Falta(f"las dos computadoras no se ven por el veth: {ping.stdout.strip()} {ping.stderr.strip()}")
        else:
            ip_laptop, ip_robot = "127.0.0.2", "127.0.0.3"
            puerto = free_port()
            envs = {"laptop": {**base_env, "AGMEN_NET_IP": ip_laptop, "AGMEN_SHM": "0",
                               "AGMEN_HUB": f"127.0.0.1:{free_port(True)}"},
                    "robot": {**base_env, "AGMEN_NET_IP": ip_robot, "AGMEN_SHM": "0",
                              "AGMEN_HUB": f"127.0.0.1:{free_port(True)}"}}
            pref = {"laptop": [], "robot": []}
        for nombre in envs:
            envs[nombre]["AGMEN_CONFIG_DIR"] = str(tmp / f"cfg-{nombre}")
        envs["laptop"]["AGMEN_REMOTE_HOSTS"] = "" if aislado else ip_robot
        toml = toml_del_robot(tmp / "robot_dos.toml", ip_robot, puerto)
        if aislado:
            os.chown(toml, aislado.uid, aislado.gid)

        def lanzar(donde, nombre, args):
            p = Proc(nombre, args, envs[donde], tmp, pref[donde], tmp / f"pilas_{nombre}.txt")
            procs.append(p)
            return p

        def cli(donde, args, timeout=40):
            return subprocess.run([*pref[donde], sys.executable, "-m", "agmen", *args], env=envs[donde], cwd=tmp,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  stdin=subprocess.DEVNULL, timeout=timeout)

        # --- 1. conectar
        agente = lanzar("robot", "agent", ["agent", str(toml), "--machine", "robot", "--port", str(puerto)])
        if not wait_for(lambda: agente.dijo("esperando a agmen run") or not agente.vivo(), 40, 0.05) or not agente.vivo():
            check("agmen agent arranca en el robot y espera", False, agente.out)
            return
        r = cli("robot", ["agent", "--show-key"])
        clave = r.stdout.strip()
        envs["laptop"]["AGMEN_AGENT_KEY"] = clave
        check("agmen agent arranca en el robot y espera (con su clave)", r.returncode == 0 and len(clave) >= 12, r.stdout + r.stderr)

        def correr(nombre, extra=()):
            run = lanzar("laptop", nombre, ["run", str(toml), "--no-browser", "--no-web", *extra])
            wait_for(lambda: run.dijo("todo arrancado") or not run.vivo(), 60, 0.05)
            return run

        run = correr("run")
        check("agmen run se conecta al agente y arranca todo",
              run.vivo() and run.dijo("todo arrancado"), run.out)
        check("la prueba de red pasa y lo dice",
              run.dijo("red con 'robot'") and run.dijo("los topics pasan en los dos sentidos"), run.out)
        mar = marcas

        def marca(nombre):
            return json.loads((mar / f"{nombre}.json").read_text(encoding="utf-8"))

        # --- 2. los nodos del robot corren allá
        cond = wait_for(lambda: marca("robot") and marca("laptop"), 30)
        check("los nodos de apoyo arrancaron", cond, run.out + agente.out)
        if cond:
            rr, ll = marca("robot"), marca("laptop")
            vivos = []
            for m in (rr, ll):
                try:
                    os.kill(m["pid"], 0) if not NT else None
                    vivos.append(True)
                except OSError:
                    vivos.append(False)
            if aislado:
                dentro = rr["red"] == aislado.red_de("robot") and ll["red"] == aislado.red_de("laptop")
                check("los nodos del robot corren en el robot, no en la laptop (su pid está en la red del robot)",
                      all(vivos) and dentro and rr["red"] != ll["red"],
                      f"robot {rr} laptop {ll}; redes: robot {aislado.red_de('robot')} laptop {aislado.red_de('laptop')}")
            else:
                check("los nodos del robot corren en el robot, no en la laptop (cada uno tiene la IP de su computadora)",
                      rr["net_ip"] == ip_robot and ll["net_ip"] == ip_laptop and rr["pid"] != ll["pid"],
                      f"robot {rr} laptop {ll}")

        # --- 3. topics en los dos sentidos
        eco = cli("laptop", ["topic", "echo", "/imu", "--count", "2"], 60)
        check("un topic del robot (/imu, el publisher en C++) llega a la laptop (agmen topic echo)",
              eco.returncode == 0 and "/imu" not in eco.stderr and len(eco.stdout.strip()) > 0,
              f"código {eco.returncode}\n{eco.stdout}{eco.stderr}")
        recibido = wait_for(lambda: (mar / "ping_ok").exists(), 30)
        ping_txt = (mar / "ping_ok").read_text(encoding="utf-8") if recibido else ""
        check("un topic de la laptop (/ping) llega a un nodo del robot, desde la IP de la laptop",
              recibido and ping_txt.split(" ")[1].split(":")[0] == ip_laptop, ping_txt or run.out + agente.out)

        # --- 4. de qué computadora sale cada topic
        lista = ""
        for _ in range(30):
            lista = cli("laptop", ["topic", "list"]).stdout
            if "/imu" in lista and "/grande" in lista and "/ping" in lista:
                break
            time.sleep(0.3)
        filas = {ln.split()[0]: ln for ln in lista.splitlines() if ln.startswith("/")}
        check("agmen topic list dice de qué computadora sale cada topic (publica: robot / aquí)",
              "publica" in lista and "robot" in filas.get("/imu", "") and "robot" in filas.get("/grande", "")
              and "aquí" in filas.get("/ping", "") and "robot" not in filas.get("/ping", ""), lista)

        # --- 5. el mensaje grande (TCP) y sin memoria compartida
        ok_grande = wait_for(lambda: (mar / "grande_ok").exists(), 30)
        g = (mar / "grande_ok").read_text(encoding="utf-8").split(" ") if ok_grande else []
        check("un mensaje grande (300 KiB, por TCP) cruza entero, desde la IP del robot",
              ok_grande and g[0] == "1" and g[1] == "307200" and g[2].split(":")[0] == ip_robot, " ".join(g))
        if aislado:
            lp, rp = aislado.holders["laptop"].pid, aislado.holders["robot"].pid
            sl, sr = (os.stat(f"/proc/{p}/root/dev/shm") for p in (lp, rp))
            seg = lambda p: sorted(x for x in os.listdir(f"/proc/{p}/root/dev/shm"))
            check("cada computadora tiene su /dev/shm (distinto): el mensaje no pudo cruzar por memoria compartida",
                  sl.st_dev != sr.st_dev and sl.st_dev != os.stat("/dev/shm").st_dev
                  and not (set(seg(lp)) & set(seg(rp)) - {""}) and ok_grande,
                  f"laptop {seg(lp)} robot {seg(rp)}")
        else:
            check("sin memoria compartida (AGMEN_SHM=0): el mensaje grande llegó por la red", ok_grande)

        # --- 7. Ctrl+C
        antes = {n: (mar / f"{n}.latido").stat().st_mtime for n in ("robot", "laptop")}
        code = run.detener()
        check("Ctrl+C en la laptop detiene todo de forma ordenada (código 0 y 'listo')",
              code == 0 and run.dijo("listo"), f"código {code}\n{run.out}")

        def muertos():
            return all(time.time() - (mar / f"{n}.latido").stat().st_mtime > 1.5 for n in ("robot", "laptop"))

        check("los nodos del robot también se detuvieron (y los de la laptop)", wait_for(muertos, 20) and
              all(not Path(f"/proc/{marca(n)['pid']}").exists() for n in ("robot", "laptop")) if LINUX else wait_for(muertos, 20),
              str(antes))
        check("el agente sigue esperando al próximo agmen run", agente.vivo())

        # --- 6. el firewall
        def arranques():
            return len((mar / "robot.arranques").read_text(encoding="utf-8").split())

        if not aislado:
            no_probado("con la entrada UDP bloqueada en el robot, agmen run no arranca nada",
                       "solo se puede en modo aislado (hace falta un firewall propio en el robot)")
        else:
            regla = None
            for nombre, cmds in (("iptables", [["iptables", "-A", "INPUT", "-p", "udp", "-j", "DROP"]]),
                                 ("nft", [["nft", "add", "table", "inet", "agmen_smoke"],
                                          ["nft", "add", "chain", "inet", "agmen_smoke", "entrada",
                                           "{ type filter hook input priority 0 ; }"],
                                          ["nft", "add", "rule", "inet", "agmen_smoke", "entrada", "meta", "l4proto",
                                           "udp", "drop"]])):
                if not shutil.which(nombre):
                    continue
                rs = [aislado.en("robot", c) for c in cmds]
                if all(x.returncode == 0 for x in rs):
                    regla = nombre
                    break
                regla = None
                ultimo = next(x for x in rs if x.returncode).stderr.strip()
            if regla is None:
                no_probado("con la entrada UDP bloqueada en el robot, agmen run no arranca nada",
                           "no hay iptables ni nft que funcione aquí" + (f" ({ultimo})" if "ultimo" in locals() else ""))
            else:
                n0 = arranques()
                bloq = correr("run_bloqueado")
                wait_for(lambda: not bloq.vivo(), 60)
                code = bloq.p.poll()
                if code is None:
                    code = bloq.detener()
                check(f"con UDP bloqueado en el robot ({regla}): agmen run no arranca nada, sale con error y dice "
                      "que el robot no recibe por UDP",
                      code not in (0, None) and not bloq.dijo("todo arrancado") and arranques() == n0
                      and "no recibe de esta computadora" in bloq.out and "UDP" in bloq.out
                      and "--no-net-check" in bloq.out, f"código {code}\n{bloq.out}")
                saltada = correr("run_sin_prueba", ["--no-net-check"])
                wait_for(lambda: arranques() > n0, 30)
                check("con --no-net-check sí arranca, y avisa que se saltó la prueba",
                      saltada.vivo() and saltada.dijo("--no-net-check") and arranques() > n0, saltada.out)
                saltada.detener()

        # --- fin: se detiene el agente
        code = agente.detener()
        check("Ctrl+C en el robot detiene el agente", code is not None and not agente.vivo(), agente.out)
        exito = all(results)
    except Falta as e:
        check("el modo aislado se pudo armar", False, str(e))
    finally:
        for p in procs:
            if p.vivo():
                p.detener(15)
        # ningún proceso de los nodos queda vivo (los pids que anotaron)
        for f in marcas.glob("*.json"):
            try:
                pid = json.loads(f.read_text(encoding="utf-8"))["pid"]
                if not NT:
                    os.kill(pid, signal.SIGKILL)
            except (OSError, ValueError, KeyError):
                pass
        if aislado:
            aislado.borrar()
        # Primero se limpia todo; recién entonces se imprime (si la salida se cierra, ya no queda nada).
        informe = ""
        if sys.exc_info()[0] is not None or not all(results) or os.environ.get("AGMEN_SMOKE_VERBOSE"):
            for p in procs:
                informe += f"\n--- salida de agmen {p.nombre} ---\n{p.out}"
                try:
                    informe += "--- sus pilas (cada 20 s) ---\n" + "\n".join(p.pilas.read_text(
                        encoding="utf-8", errors="replace").splitlines()[-60:]) + "\n"
                except OSError:
                    pass
        shutil.rmtree(tmp, ignore_errors=True)
        if informe:
            print(informe, flush=True)
    # lo creado ya no está
    if aislado and not (LINUX and not any(Path("/sys/class/net").glob(f"v[lr]{aislado.sufijo}"))):
        check("no queda ninguna interfaz veth", False)
    print(f"({'modo ' + modo}, {time.monotonic() - t0:.0f} s"
          + (f"; sin probar: {len(notas)}" if notas else "") + ")")
    sys.exit(0 if exito and all(results) else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)

"""agmen agent y la conexión con clave `agmen-agent/1` (RFC 0011 §3.4, D6, D7, D9, parte 3b).

Contrato: la especificación de la 3b (§1-§7). Aquí se prueba, de lo más chico a lo más grande:

  1. la clave y sus archivos (new_key, normalize_key, derive_key, agent.key, keys.toml);
  2. el saludo y los mensajes firmados, contra vectores calculados a mano con hmac/hashlib
     (el formato en la red tiene que ser EXACTAMENTE el del contrato);
  3. lo escrito de cada nodo (written_nodes, diff_nodes) y qué [[machine]] es el agente;
  4. un AgentServer real, en un hilo, con nodos de prueba en Python: la sesión, los límites
     del saludo, qué pasa al cortarse el cliente, que ningún campo del cliente llega a
     subprocess (D7) y que un cliente que no lee no hace crecer la memoria ni traba al agente;
  5. de punta a punta: `agmen agent` y `agmen run` como subprocesos reales.

Sin computadoras reales: "otra computadora" es 127.0.0.1 con AGMEN_REMOTE_HOSTS. Los puertos
son siempre libres (puerto 0 o elegido al momento); cada prueba usa su propia carpeta de
configuración (AGMEN_CONFIG_DIR) y su propio AGMEN_HUB, para no tocar los del usuario.

    python -m unittest python/test_agent.py
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import random
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import warnings
from pathlib import Path
from unittest import mock

from agmen import agent, machines
from agmen._version import __version__
from agmen.agent import (AgentError, AgentKeyError, Channel, ChannelClosed, ChannelError, ChannelTimeout,
                         HandshakeError, MessageTooLarge, client_handshake, agent_handshake, derive_key, diff_nodes,
                         format_key, new_key, normalize_key, pick_machine, written_nodes)
from agmen.machines import Place
from agmen.run import ConfigError, load_config



def setUpModule():
    # Los nodos dejan tuberias de subprocess sin cerrar al terminar (run.py): ruido, no un fallo de aqui.
    # (unittest reinicia los filtros al empezar a correr, por eso va aqui y no al importar)
    warnings.simplefilter("ignore", ResourceWarning)


ROOT = Path(__file__).resolve().parents[1]
KEY = "kq3f-9xm2-7tdw"
LINUX = sys.platform.startswith("linux")
POSIX = os.name == "posix"

_K: list[bytes] = []


def K() -> bytes:
    """La clave derivada de KEY (PBKDF2 con 200000 vueltas: se calcula una sola vez)."""
    if not _K:
        _K.append(derive_key(KEY))
    return _K[0]


# --- La referencia: el contrato escrito a mano con hmac/hashlib, sin usar agmen.agent ---
def ref_k(key: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", key.replace("-", "").lower().encode(), b"agmen-agent/1", 200000, 32)


def ref_mac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def ref_proof(k: bytes, who: str, nc: str, na: str) -> str:
    return ref_mac(k, f"agmen-agent/1|{who}|{nc}|{na}").hex()


def ref_session(k: bytes, nc: str, na: str) -> bytes:
    return ref_mac(k, f"agmen-agent/1|sesion|{nc}|{na}")


def ref_line(s: bytes, direction: str, n: int, body: str) -> bytes:
    return f"{n} {body} {ref_mac(s, f'{direction}|{n}|{body}').hex()}\n".encode("utf-8")


_PUERTOS_DADOS: set[tuple[int, int]] = set()


def _puerto_libre(tipo: int) -> int:
    """Un puerto libre FUERA del rango efímero del sistema operativo.

    Con bind(0) el sistema da un puerto efímero y, al soltarlo, cualquier conexión saliente (de esta prueba, de
    un hilo que quedó de otra o de un subproceso) puede tomarlo como puerto de origen antes de que el agente haga
    su bind: en el CI salió "Address already in use" en un `agmen agent` recién lanzado. Las conexiones salientes
    nunca usan puertos por debajo del rango efímero (Linux 32768+, Windows y macOS 49152+), así que se elige uno
    de ahí, se comprueba que se puede abrir y no se reparte dos veces en el mismo proceso."""
    bajo = 32768
    try:
        with open("/proc/sys/net/ipv4/ip_local_port_range") as f:
            bajo = int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        if os.name == "nt" or sys.platform == "darwin":
            bajo = 49152
    for _ in range(200):
        puerto = random.randrange(12000, max(12001, bajo - 1))
        if (tipo, puerto) in _PUERTOS_DADOS:
            continue
        try:
            with socket.socket(socket.AF_INET, tipo) as s:
                s.bind(("127.0.0.1", puerto))
        except OSError:
            continue
        _PUERTOS_DADOS.add((tipo, puerto))
        return puerto
    with socket.socket(socket.AF_INET, tipo) as s:       # sin hueco abajo: lo de siempre
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def free_tcp_port() -> int:
    return _puerto_libre(socket.SOCK_STREAM)


def free_udp_port() -> int:
    return _puerto_libre(socket.SOCK_DGRAM)


def source_ip_available(ip: str) -> bool:
    """¿Se puede conectar DESDE `ip` (127.0.0.N)? En Linux sí; en macOS y Windows 127.0.0.2+ puede no existir."""
    try:
        with socket.socket() as s:
            s.bind((ip, 0))
        return True
    except OSError:
        return False


class Peer:
    """Un extremo crudo de una conexión: manda y lee líneas sin pasar por agmen.agent."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""

    def send(self, data: bytes | str) -> None:
        self.sock.sendall(data.encode("utf-8") if isinstance(data, str) else data)

    def line(self, timeout: float = 5.0) -> str | None:
        """La siguiente línea sin su \n; None si la otra punta cerró (o no llegó a tiempo)."""
        end = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError("no llegó una línea")
            self.sock.settimeout(left)
            try:
                data = self.sock.recv(65536)
            except (ConnectionError, OSError):
                data = b""
            if not data:
                rest, self.buf = self.buf, b""
                return rest.decode("utf-8", "replace") if rest else None
            self.buf += data
        out, _, self.buf = self.buf.partition(b"\n")
        return out.decode("utf-8")

    def closed(self, timeout: float = 3.0) -> bool:
        """¿La otra punta cerró? (lee hasta el final)"""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                self.sock.settimeout(max(0.05, end - time.monotonic()))
                if not self.sock.recv(65536):
                    return True
            except socket.timeout:
                continue
            except OSError:
                return True
        return False

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ===========================================================================
# 1. La clave
# ===========================================================================

class ConfigDirCase(unittest.TestCase):
    """Cada prueba, su propia carpeta de configuración (nunca la del usuario)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cfg = self.dir / "cfg"
        p = mock.patch.dict(os.environ, {"AGMEN_CONFIG_DIR": str(self.cfg)})
        p.start()
        self.addCleanup(p.stop)


class TestClave(unittest.TestCase):
    def test_alfabeto_y_formato(self):
        vistas = set()
        for _ in range(300):
            k = new_key()
            self.assertRegex(k, r"^[abcdefghjkmnpqrstuvwxyz2-9]{4}-[abcdefghjkmnpqrstuvwxyz2-9]{4}-"
                                r"[abcdefghjkmnpqrstuvwxyz2-9]{4}$")
            for malo in "ilo01":
                self.assertNotIn(malo, k)
            vistas.add(k)
        self.assertGreater(len(vistas), 290, "las claves nuevas no parecen al azar")
        self.assertEqual(agent.KEY_ALPHABET, "abcdefghjkmnpqrstuvwxyz23456789")
        self.assertEqual(agent.KEY_LENGTH, 12)

    def test_new_key_usa_secrets(self):
        with mock.patch("agmen.agent.secrets.choice", side_effect=lambda a: a[0]) as ch:
            self.assertEqual(new_key(), "aaaa-aaaa-aaaa")
        self.assertEqual(ch.call_count, 12)

    def test_format_key(self):
        self.assertEqual(format_key("kq3f9xm27tdw"), "kq3f-9xm2-7tdw")
        self.assertEqual(format_key("KQ3F-9XM2-7TDW"), "kq3f-9xm2-7tdw")

    def test_normalize_acepta_lo_que_escribe_una_persona(self):
        for texto in ("kq3f-9xm2-7tdw", "KQ3F-9XM2-7TDW", "kq3f9xm27tdw", "  kq3f 9xm2 7tdw  ", "Kq3F-9xM2 7TdW",
                      "kq3f_9xm2.7tdw", "kq3f\t9xm2\n7tdw"):
            with self.subTest(texto=texto):
                self.assertEqual(normalize_key(texto), "kq3f9xm27tdw")

    def test_normalize_errores_dicen_que_hacer(self):
        casos = {
            "kq3f-9xm2": "12 letras y números",                 # corta
            "kq3f-9xm2-7tdw-abcd": "12 letras y números",       # larga
            "": "12 letras y números",
            "kq3f-9xm2-7tdi": "(la clave no usa i, l, o, 0 ni 1",
            "kq3f-9xm2-7tdl": "'l'",
            "kq3f-9xm2-7tdo": "'o'",
            "kq3f-9xm2-7td0": "'0'",
            "kq3f-9xm2-7td1": "'1'",
            "kq3f-9xm2-7td!": "caracteres que no lleva ('!')",
            "kq3f-9xm2-7tdé": "caracteres que no lleva",
        }
        for texto, frag in casos.items():
            with self.subTest(texto=texto):
                with self.assertRaises(AgentKeyError) as ctx:
                    normalize_key(texto)
                self.assertIn(frag, str(ctx.exception))
                self.assertIn("kq3f-9xm2-7tdw", str(ctx.exception))   # el ejemplo

    def test_normalize_no_es_texto(self):
        for malo in (None, 12, b"kq3f9xm27tdw", ["kq3f"]):
            with self.subTest(malo=malo):
                with self.assertRaises(AgentKeyError):
                    normalize_key(malo)

    def test_derive_key_es_pbkdf2_del_contrato(self):
        esperado = hashlib.pbkdf2_hmac("sha256", b"kq3f9xm27tdw", b"agmen-agent/1", 200000, 32)
        self.assertEqual(K(), esperado)
        self.assertEqual(len(K()), 32)
        # el valor fijo calculado a mano (si cambia la sal, las vueltas o el hash, esto lo dice)
        self.assertEqual(K().hex(), "0de003ec80910f46f9cc331f0549b96e06105c2e80cb4ce22f9f95f713241220")
        self.assertEqual(agent.PBKDF2_SALT, b"agmen-agent/1")
        self.assertEqual(agent.PBKDF2_ROUNDS, 200000)

    def test_derive_key_es_determinista_y_no_distingue_formas(self):
        a = derive_key("KQ3F9XM27TDW")
        self.assertEqual(a, K())
        self.assertEqual(derive_key("kq3f 9xm2 7tdw"), K())
        self.assertNotEqual(derive_key("kq3f-9xm2-7tdx"), K())

    def test_derive_key_clave_invalida_es_error(self):
        with self.assertRaises(AgentKeyError):
            derive_key("corta")


class TestArchivosDeClave(ConfigDirCase):
    def test_config_dir_respeta_la_variable(self):
        self.assertEqual(agent.config_dir(), self.cfg)
        self.assertEqual(agent.agent_key_path(), self.cfg / "agent.key")
        self.assertEqual(agent.client_keys_path(), self.cfg / "keys.toml")

    def test_config_dir_por_defecto_por_sistema(self):
        with mock.patch.dict(os.environ, {"AGMEN_CONFIG_DIR": ""}):
            if os.name == "nt":
                with mock.patch.dict(os.environ, {"APPDATA": r"C:\Users\x\AppData\Roaming"}):
                    self.assertEqual(agent.config_dir(), Path(r"C:\Users\x\AppData\Roaming") / "agmen")
            else:
                with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg-agmen"}):
                    self.assertEqual(agent.config_dir(), Path("/tmp/xdg-agmen/agmen"))
                with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "relativa"}):    # no absoluta: se ignora
                    self.assertEqual(agent.config_dir(), Path.home() / ".config" / "agmen")

    def test_agent_key_se_crea_una_vez(self):
        k, nueva = agent.load_agent_key()
        self.assertTrue(nueva)
        self.assertRegex(k, r"^[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}$")
        self.assertEqual((self.cfg / "agent.key").read_text(encoding="utf-8"), k + "\n")
        k2, nueva2 = agent.load_agent_key()
        self.assertEqual((k2, nueva2), (k, False))

    @unittest.skipUnless(POSIX, "permisos 0600 solo en POSIX (en Windows la carpeta de usuario ya es privada)")
    def test_permisos_0600_y_carpeta_0700(self):
        agent.load_agent_key()
        self.assertEqual((self.cfg / "agent.key").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.cfg.stat().st_mode & 0o777, 0o700)

    @unittest.skipUnless(POSIX, "permisos solo en POSIX")
    def test_agent_key_con_permisos_abiertos_se_cierra(self):
        agent.load_agent_key()
        os.chmod(self.cfg / "agent.key", 0o644)
        agent.load_agent_key()
        self.assertEqual((self.cfg / "agent.key").stat().st_mode & 0o777, 0o600)

    def test_new_key_reemplaza_y_dice_que_es_nueva(self):
        k, _ = agent.load_agent_key()
        k2, nueva = agent.load_agent_key(new=True)
        self.assertTrue(nueva)
        self.assertNotEqual(k, k2)
        self.assertEqual(agent.load_agent_key(), (k2, False))

    def test_agent_key_editada_a_mano_es_error_que_dice_new_key(self):
        self.cfg.mkdir(parents=True)
        (self.cfg / "agent.key").write_text("esto no es una clave\n", encoding="utf-8")
        with self.assertRaises(AgentKeyError) as ctx:
            agent.load_agent_key()
        self.assertIn("--new-key", str(ctx.exception))
        self.assertIn("agent.key", str(ctx.exception))

    def test_agent_key_acepta_mayusculas_y_sin_guiones_escritas_a_mano(self):
        self.cfg.mkdir(parents=True)
        (self.cfg / "agent.key").write_text("KQ3F9XM27TDW", encoding="utf-8")
        self.assertEqual(agent.load_agent_key(), ("kq3f-9xm2-7tdw", False))

    def test_carpeta_que_no_se_puede_crear_es_error_claro(self):
        archivo = self.dir / "es-un-archivo"
        archivo.write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"AGMEN_CONFIG_DIR": str(archivo / "dentro")}):
            with self.assertRaises(AgentKeyError) as ctx:
                agent.load_agent_key()
        self.assertIn("AGMEN_CONFIG_DIR", str(ctx.exception))

    def test_creacion_con_varios_hilos_a_la_vez_es_una_sola_clave(self):
        resultados, barrera = [], threading.Barrier(12)

        def uno():
            barrera.wait()
            resultados.append(agent.load_agent_key())

        hilos = [threading.Thread(target=uno) for _ in range(12)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(30)
        self.assertEqual(len(resultados), 12)
        self.assertEqual(len({k for k, _ in resultados}), 1, "cada hilo se quedó con una clave distinta")
        self.assertEqual(sum(1 for _, nueva in resultados if nueva), 1, "más de uno creó el archivo")
        self.assertEqual(sorted(p.name for p in self.cfg.iterdir()), ["agent.key"], "quedaron temporales")

    def test_creacion_con_varios_procesos_a_la_vez_es_una_sola_clave(self):
        listo = self.dir / "go"
        codigo = ("import os, sys, time\nfrom agmen import agent\n"
                  "while not os.path.exists(sys.argv[1]): time.sleep(0.001)\n"
                  "k, nueva = agent.load_agent_key()\nprint(k, int(nueva))\n")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "AGMEN_BACKEND": "pure"}
        procs = [subprocess.Popen([sys.executable, "-c", codigo, str(listo)], env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(5)]
        time.sleep(1.0)   # que todos importen y esperen la señal
        listo.write_text("go", encoding="utf-8")
        salidas = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
            salidas.append(out.split())
        self.assertEqual(len({k for k, _ in salidas}), 1, salidas)
        self.assertEqual(sum(int(n) for _, n in salidas), 1, salidas)
        self.assertEqual(sorted(p.name for p in self.cfg.iterdir()), ["agent.key"])

    # --- keys.toml
    def test_machine_address(self):
        self.assertEqual(agent.machine_address("192.168.1.20", 7391), "192.168.1.20:7391")
        self.assertEqual(agent.machine_address("Robot.Local.", 7400), "robot.local:7400")
        self.assertEqual(agent.machine_address("fe80::1%eth0", 7391), "[fe80::1%eth0]:7391")
        self.assertEqual(agent.machine_address("::1", 7391), "[::1]:7391")

    def test_keys_toml_guardar_leer_olvidar(self):
        self.assertEqual(agent.load_client_keys(), {})
        self.assertIsNone(agent.client_key("192.168.1.20:7391"))
        agent.save_client_key("192.168.1.20:7391", "KQ3F9XM27TDW")
        agent.save_client_key("[fe80::1%eth0]:7400", "aaaa-bbbb-cccc")
        self.assertEqual(agent.client_key("192.168.1.20:7391"), "kq3f-9xm2-7tdw")
        self.assertEqual(agent.load_client_keys(), {"192.168.1.20:7391": "kq3f-9xm2-7tdw",
                                                    "[fe80::1%eth0]:7400": "aaaa-bbbb-cccc"})
        self.assertTrue(agent.forget_client_key("192.168.1.20:7391"))
        self.assertFalse(agent.forget_client_key("192.168.1.20:7391"))
        self.assertIsNone(agent.client_key("192.168.1.20:7391"))
        self.assertEqual(agent.client_key("[fe80::1%eth0]:7400"), "aaaa-bbbb-cccc")

    @unittest.skipUnless(POSIX, "permisos solo en POSIX")
    def test_keys_toml_es_0600(self):
        agent.save_client_key("1.2.3.4:7391", KEY)
        self.assertEqual((self.cfg / "keys.toml").stat().st_mode & 0o777, 0o600)

    def test_keys_toml_se_puede_leer_como_toml(self):
        agent.save_client_key("1.2.3.4:7391", KEY)
        import tomllib
        self.assertEqual(tomllib.loads((self.cfg / "keys.toml").read_text(encoding="utf-8")),
                         {"1.2.3.4:7391": KEY})

    def test_keys_toml_invalido_dice_que_borrarlo(self):
        self.cfg.mkdir(parents=True)
        (self.cfg / "keys.toml").write_text("esto = no es toml [", encoding="utf-8")
        with self.assertRaises(AgentKeyError) as ctx:
            agent.load_client_keys()
        self.assertIn("no es TOML válido", str(ctx.exception))
        self.assertIn("Bórralo", str(ctx.exception))

    def test_keys_toml_con_clave_mala_dice_forget_key(self):
        self.cfg.mkdir(parents=True)
        (self.cfg / "keys.toml").write_text('"1.2.3.4:7391" = "mala"\n', encoding="utf-8")
        with self.assertRaises(AgentKeyError) as ctx:
            agent.load_client_keys()
        self.assertIn("1.2.3.4:7391", str(ctx.exception))
        self.assertIn("--forget-key", str(ctx.exception))

    def test_escritura_atomica_si_falla_queda_el_archivo_de_antes_y_ningun_temporal(self):
        agent.save_client_key("1.2.3.4:7391", KEY)
        antes = (self.cfg / "keys.toml").read_text(encoding="utf-8")
        with mock.patch("agmen.agent.os.replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(AgentKeyError) as ctx:
                agent.save_client_key("5.6.7.8:7391", "aaaa-bbbb-cccc")
        self.assertIn("no pude guardar", str(ctx.exception))
        self.assertEqual((self.cfg / "keys.toml").read_text(encoding="utf-8"), antes)
        self.assertEqual(sorted(p.name for p in self.cfg.iterdir()), ["keys.toml"])

    def test_la_clave_no_aparece_en_los_errores_de_keys_toml(self):
        self.cfg.mkdir(parents=True)
        (self.cfg / "keys.toml").write_text(f'"1.2.3.4:7391" = "{KEY}"\n"x" = "mala"\n', encoding="utf-8")
        with self.assertRaises(AgentKeyError) as ctx:
            agent.load_client_keys()
        self.assertNotIn(KEY, str(ctx.exception))


class TestObtenerLaClave(ConfigDirCase):
    ADDR = "1.2.3.4:7391"

    def test_guardada_gana(self):
        agent.save_client_key(self.ADDR, KEY)
        with mock.patch.dict(os.environ, {agent.KEY_ENV: "aaaa-bbbb-cccc"}):
            self.assertEqual(agent.get_key("robot", self.ADDR, interactive=False), (KEY, "guardada"))

    def test_variable_si_no_hay_guardada(self):
        self.assertEqual(agent.get_key("robot", self.ADDR, interactive=False, env={agent.KEY_ENV: "KQ3F9XM27TDW"}),
                         (KEY, "variable"))

    def test_variable_invalida_dice_cual_variable(self):
        with self.assertRaises(AgentKeyError) as ctx:
            agent.get_key("robot", self.ADDR, interactive=False, env={agent.KEY_ENV: "mala"})
        self.assertTrue(str(ctx.exception).startswith("AGMEN_AGENT_KEY: "))

    def test_sin_terminal_interactiva_dice_como_darla(self):
        with self.assertRaises(AgentKeyError) as ctx:
            agent.get_key("robot", self.ADDR, interactive=False, env={})
        t = str(ctx.exception)
        for frag in ("'robot'", self.ADDR, "AGMEN_AGENT_KEY", "no es interactiva"):
            self.assertIn(frag, t)

    def test_nul_como_entrada_no_es_terminal(self):
        # stdin=DEVNULL: en Windows NUL es un dispositivo de caracteres e isatty() dice que sí; getpass leería
        # del teclado de la consola y agmen run se quedaría esperando para siempre en vez de salir con 2
        with open(os.devnull, encoding="utf-8") as nul, mock.patch.object(sys, "stdin", nul):
            self.assertFalse(agent._stdin_is_terminal())
        with mock.patch.object(sys, "stdin", None):
            self.assertFalse(agent._stdin_is_terminal())

    @unittest.skipUnless(os.name == "posix", "una pty solo hay en POSIX")
    def test_una_terminal_de_verdad_si_es_terminal(self):
        maestro, esclavo = os.openpty()
        self.addCleanup(os.close, maestro)
        with open(esclavo, encoding="utf-8") as tty, mock.patch.object(sys, "stdin", tty):
            self.assertTrue(agent._stdin_is_terminal())

    def test_sin_ctypes_no_es_terminal_y_no_truena(self):
        # en Windows, un Python sin ctypes: se trata como "no hay terminal" (el error dice cómo dar la clave)
        class Consola:
            def isatty(self):
                return True

            def fileno(self):
                return 0

        real_import = __import__

        def sin_ctypes(name, *a, **kw):
            if name == "ctypes":
                raise ImportError("No module named 'ctypes'")
            return real_import(name, *a, **kw)

        with mock.patch.object(sys, "stdin", Consola()), mock.patch.object(agent.os, "name", "nt"), \
                mock.patch("builtins.__import__", sin_ctypes):
            self.assertFalse(agent._stdin_is_terminal())

    def test_se_pide_en_la_terminal_y_es_escrita(self):
        pedidos = []
        res = agent.get_key("robot", self.ADDR, interactive=True, env={}, ask=lambda p: pedidos.append(p) or "KQ3F-9XM2-7TDW")
        self.assertEqual(res, (KEY, "escrita"))
        self.assertIn("robot", pedidos[0])
        self.assertIn(self.ADDR, pedidos[0])
        self.assertNotIn(KEY, pedidos[0])

    def test_pide_hasta_tres_veces(self):
        respuestas = iter(["mala", "tambien mala", "kq3f-9xm2-7tdw"])
        with mock.patch("sys.stderr", io.StringIO()):
            res = agent.get_key("robot", self.ADDR, interactive=True, env={}, ask=lambda p: next(respuestas))
        self.assertEqual(res, (KEY, "escrita"))
        with mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(AgentKeyError):
                agent.get_key("robot", self.ADDR, interactive=True, env={}, ask=lambda p: "mala")

    def test_no_se_guarda_al_obtenerla(self):
        agent.get_key("robot", self.ADDR, interactive=True, env={}, ask=lambda p: KEY)
        agent.get_key("robot", self.ADDR, interactive=False, env={agent.KEY_ENV: KEY})
        self.assertFalse((self.cfg / "keys.toml").exists(), "solo se guarda tras una conexión aceptada")


# ===========================================================================
# 2. El saludo y los mensajes firmados
# ===========================================================================

def handshake_pair(key_client: bytes | None = None, key_agent: bytes | None = None, **agent_kw):
    """Corre los dos saludos, cada uno en su hilo, sobre un socketpair. Devuelve
    (canal_cliente, error_cliente, canal_agente, error_agente, socket_cliente, socket_agente)."""
    sc, sa = socket.socketpair()
    out = {}

    def agente():
        try:
            out["canal"] = agent_handshake(sa, key_agent or K(), **agent_kw)
        except Exception as e:
            out["error"] = e

    t = threading.Thread(target=agente, daemon=True)
    t.start()
    canal_c = err_c = None
    try:
        canal_c = client_handshake(sc, key_client or K(), timeout=agent_kw.get("timeout", 10.0))
    except Exception as e:
        err_c = e
    t.join(15)
    return canal_c, err_c, out.get("canal"), out.get("error"), sc, sa


class TestSaludo(unittest.TestCase):
    def test_clave_buena_en_los_dos_sentidos(self):
        cc, ec, ca, ea, sc, sa = handshake_pair()
        self.addCleanup(lambda: (cc and cc.close(), ca and ca.close()))
        self.assertIsNone(ec)
        self.assertIsNone(ea)
        self.assertEqual(cc.peer_version, __version__)
        cc.send({"op": "ping", "t": 1})
        self.assertEqual(ca.recv(2), {"op": "ping", "t": 1})
        ca.send({"op": "pong", "t": 1})
        self.assertEqual(cc.recv(2), {"op": "pong", "t": 1})

    def test_clave_mala_la_rechazan_las_dos_puntas(self):
        cc, ec, ca, ea, sc, sa = handshake_pair(key_client=derive_key("aaaa-bbbb-cccc"))
        self.assertIsNone(cc)
        self.assertIsNone(ca)
        self.assertIsInstance(ec, HandshakeError)
        self.assertEqual(ec.code, "clave")
        self.assertIsInstance(ea, HandshakeError)
        self.assertEqual(ea.code, "clave")
        self.assertIn("la clave no es la de este agente", str(ec))
        sc.close()
        sa.close()

    def test_el_cliente_rechaza_a_un_agente_que_no_prueba_la_clave(self):
        # un "agente" que contesta bien el hola y luego una prueba al azar (no tiene la clave)
        sc, sa = socket.socketpair()
        peer = Peer(sa)

        def impostor():
            peer.line()
            peer.send(f"agmen-agent/1 hola {secrets.token_hex(16)} 0.13.0\n")
            peer.line()
            peer.send(f"prueba {secrets.token_hex(32)}\n")

        t = threading.Thread(target=impostor, daemon=True)
        t.start()
        with self.assertRaises(HandshakeError) as ctx:
            client_handshake(sc, K())
        t.join(5)
        self.assertEqual(ctx.exception.code, "impostor")
        self.assertIn("otra computadora con su IP", str(ctx.exception))
        sc.close()
        peer.close()

    def test_un_impostor_con_otra_clave_tambien_se_nota(self):
        cc, ec, ca, ea, sc, sa = handshake_pair(key_agent=derive_key("aaaa-bbbb-cccc"))
        # el agente (con otra clave) rechaza al cliente: el cliente ve "clave" (error del agente), no un éxito
        self.assertIsNone(cc)
        self.assertEqual(ec.code, "clave")

    def test_impostor_que_copia_la_prueba_del_cliente(self):
        # refleja la prueba del cliente como si fuera suya: no coincide ("agente" != "cliente")
        sc, sa = socket.socketpair()
        peer = Peer(sa)

        def reflejo():
            peer.line()
            peer.send(f"agmen-agent/1 hola {secrets.token_hex(16)} 0.13.0\n")
            prueba = peer.line()
            peer.send(prueba + "\n")

        threading.Thread(target=reflejo, daemon=True).start()
        with self.assertRaises(HandshakeError) as ctx:
            client_handshake(sc, K())
        self.assertEqual(ctx.exception.code, "impostor")
        sc.close()
        peer.close()

    def test_version_del_protocolo_distinta_en_el_cliente(self):
        sc, sa = socket.socketpair()
        peer = Peer(sa)

        def viejo():
            peer.line()
            peer.send(f"agmen-agent/2 hola {secrets.token_hex(16)} 9.0.0\n")

        threading.Thread(target=viejo, daemon=True).start()
        with self.assertRaises(HandshakeError) as ctx:
            client_handshake(sc, K())
        self.assertEqual(ctx.exception.code, "version")
        self.assertIn("agmen-agent/2", str(ctx.exception))
        self.assertIn("pip install -U agmen", str(ctx.exception))
        sc.close()
        peer.close()

    def test_version_del_protocolo_distinta_en_el_agente(self):
        for linea, frag in (("agmen-agent/2 hola " + "a" * 32, "actualiza Agmen en esta computadora"),
                            ("agmen-agent/0 hola " + "a" * 32, "actualiza Agmen en la laptop")):
            with self.subTest(linea=linea):
                sc, sa = socket.socketpair()
                peer = Peer(sc)
                errores = []

                def agente():
                    try:
                        agent_handshake(sa, K())
                    except HandshakeError as e:
                        errores.append(e)

                t = threading.Thread(target=agente, daemon=True)
                t.start()
                peer.send(linea + "\n")
                respuesta = peer.line()
                t.join(5)
                self.assertTrue(respuesta.startswith("error version "), respuesta)
                self.assertIn(frag, respuesta)
                self.assertEqual(errores[0].code, "version")
                peer.close()
                sa.close()

    def test_el_agente_cierra_ante_basura(self):
        casos = [b"GET / HTTP/1.1\r\n", b"hola\n", b"\n", b"\xff\xfe\xfd\n", b"agmen\n", b"AGMEN-AGENT/1 hola x\n"]
        for basura in casos:
            with self.subTest(basura=basura):
                sc, sa = socket.socketpair()
                peer = Peer(sc)
                errores = []

                def agente():
                    try:
                        agent_handshake(sa, K())
                    except HandshakeError as e:
                        errores.append(e)

                t = threading.Thread(target=agente, daemon=True)
                t.start()
                peer.send(basura)
                t.join(5)
                self.assertEqual(len(errores), 1)
                self.assertIn(errores[0].code, ("no_agmen",))
                sa.close()
                self.assertIsNone(peer.line(1.0), "el agente debía cerrar sin contestar")
                peer.close()

    def test_hola_mal_formado_dice_error_protocolo(self):
        for malo in ("agmen-agent/1 hola", "agmen-agent/1 hola xyz", "agmen-agent/1 hola " + "a" * 31,
                     "agmen-agent/1 chau " + "a" * 32, "agmen-agent/1 hola " + "a" * 32 + " extra",
                     "agmen-agent/1 hola " + "A" * 32):
            with self.subTest(malo=malo):
                sc, sa = socket.socketpair()
                peer = Peer(sc)
                errores = []

                def agente():
                    try:
                        agent_handshake(sa, K())
                    except HandshakeError as e:
                        errores.append(e)

                t = threading.Thread(target=agente, daemon=True)
                t.start()
                peer.send(malo + "\n")
                self.assertTrue(peer.line().startswith("error protocolo"))
                t.join(5)
                self.assertEqual(errores[0].code, "protocolo")
                peer.close()
                sa.close()

    def test_prueba_mal_formada_no_cuenta_como_clave_mala(self):
        for malo in ("prueba xyz", "prueba " + "g" * 64, "prueba " + "a" * 63, "hola", "prueba"):
            with self.subTest(malo=malo):
                sc, sa = socket.socketpair()
                peer = Peer(sc)
                errores = []

                def agente():
                    try:
                        agent_handshake(sa, K())
                    except HandshakeError as e:
                        errores.append(e)

                t = threading.Thread(target=agente, daemon=True)
                t.start()
                peer.send("agmen-agent/1 hola " + "b" * 32 + "\n")
                peer.line()
                peer.send(malo + "\n")
                self.assertTrue(peer.line().startswith("error protocolo"))
                t.join(5)
                self.assertEqual(errores[0].code, "protocolo")
                peer.close()
                sa.close()

    def test_linea_larga_sin_salto_de_linea(self):
        # al agente: más de 1 KiB sin \n
        sc, sa = socket.socketpair()
        peer = Peer(sc)
        errores = []

        def agente():
            try:
                agent_handshake(sa, K())
            except HandshakeError as e:
                errores.append(e)

        t = threading.Thread(target=agente, daemon=True)
        t.start()
        peer.send(b"agmen-agent/1 hola " + b"a" * 5000)
        t.join(5)
        self.assertEqual(errores[0].code, "no_agmen")
        peer.close()
        sa.close()
        # al cliente: un "agente" que manda 5000 bytes sin \n
        sc, sa = socket.socketpair()
        peer = Peer(sa)

        def largo():
            peer.line()
            peer.send(b"x" * 5000)

        threading.Thread(target=largo, daemon=True).start()
        with self.assertRaises(HandshakeError) as ctx:
            client_handshake(sc, K())
        self.assertEqual(ctx.exception.code, "no_agmen")
        sc.close()
        peer.close()

    def test_el_cliente_distingue_a_otro_programa(self):
        for respuesta, codigo in ((b"HTTP/1.1 400 Bad Request\r\n", "no_agmen"), (b"SSH-2.0-OpenSSH_9\r\n", "no_agmen"),
                                  (b"agmen-agent/1 hola\n", "protocolo"),
                                  (b"agmen-agent/1 hola " + b"z" * 32 + b" 0.1\n", "protocolo")):
            with self.subTest(respuesta=respuesta):
                sc, sa = socket.socketpair()
                peer = Peer(sa)

                def otro():
                    peer.line()
                    peer.send(respuesta)

                threading.Thread(target=otro, daemon=True).start()
                with self.assertRaises(HandshakeError) as ctx:
                    client_handshake(sc, K())
                self.assertEqual(ctx.exception.code, codigo)
                sc.close()
                peer.close()

    def test_saludo_lento_tiene_limite(self):
        # el agente: el cliente conecta y no dice nada
        sc, sa = socket.socketpair()
        t0 = time.monotonic()
        with self.assertRaises(HandshakeError) as ctx:
            agent_handshake(sa, K(), timeout=0.4)
        self.assertEqual(ctx.exception.code, "tiempo")
        self.assertLess(time.monotonic() - t0, 15.0)
        sc.close()
        sa.close()
        # el cliente: el "agente" acepta y no contesta
        sc, sa = socket.socketpair()
        t0 = time.monotonic()
        with self.assertRaises(HandshakeError) as ctx:
            client_handshake(sc, K(), timeout=0.4)
        self.assertEqual(ctx.exception.code, "tiempo")
        self.assertLess(time.monotonic() - t0, 15.0)
        sc.close()
        sa.close()

    def test_goteo_lento_no_alarga_el_limite(self):
        # un byte cada 0.1 s: el límite es del saludo completo, no de cada byte
        sc, sa = socket.socketpair()
        peer = Peer(sc)
        parar = threading.Event()

        def gotea():
            for c in b"agmen-agent/1 hola ":
                if parar.is_set():
                    return
                try:
                    peer.send(bytes([c]))
                except OSError:
                    return
                time.sleep(0.1)

        threading.Thread(target=gotea, daemon=True).start()
        t0 = time.monotonic()
        with self.assertRaises(HandshakeError) as ctx:
            agent_handshake(sa, K(), timeout=0.6)
        parar.set()
        self.assertEqual(ctx.exception.code, "tiempo")
        self.assertLess(time.monotonic() - t0, 15.0)
        peer.close()
        sa.close()

    def test_la_otra_punta_cierra_en_medio(self):
        sc, sa = socket.socketpair()
        sc.close()
        with self.assertRaises(HandshakeError) as ctx:
            agent_handshake(sa, K())
        self.assertEqual(ctx.exception.code, "cerrado")
        sa.close()
        sc, sa = socket.socketpair()
        sa.close()
        with self.assertRaises(HandshakeError) as ctx:
            client_handshake(sc, K())
        self.assertIn(ctx.exception.code, ("cerrado",))
        sc.close()

    def test_refuse_responde_espera_sin_revisar_la_prueba(self):
        sc, sa = socket.socketpair()
        peer = Peer(sc)
        errores = []

        def agente():
            try:
                agent_handshake(sa, K(), refuse=lambda: "vuelve en 30 s")
            except HandshakeError as e:
                errores.append(e)

        t = threading.Thread(target=agente, daemon=True)
        t.start()
        peer.send("agmen-agent/1 hola " + "c" * 32 + "\n")
        peer.line()
        peer.send("prueba " + "0" * 64 + "\n")      # ni siquiera es la buena: no se revisa
        self.assertEqual(peer.line(), "error espera vuelve en 30 s")
        t.join(5)
        self.assertEqual(errores[0].code, "espera")
        peer.close()
        sa.close()

    def test_el_cliente_traduce_error_espera(self):
        sc, sa = socket.socketpair()
        peer = Peer(sa)

        def agente():
            peer.line()
            peer.send(f"agmen-agent/1 hola {secrets.token_hex(16)} 0.13.0\n")
            peer.line()
            peer.send("error espera demasiados intentos, espera 30 s\n")

        threading.Thread(target=agente, daemon=True).start()
        with self.assertRaises(HandshakeError) as ctx:
            client_handshake(sc, K())
        self.assertEqual(ctx.exception.code, "espera")
        self.assertNotEqual(ctx.exception.code, "clave", "espera NO debe decir que la clave está mal")
        sc.close()
        peer.close()

    # --- el formato EXACTO en la red, contra vectores calculados a mano
    NC = "00112233445566778899aabbccddeeff"
    NA = "ffeeddccbbaa99887766554433221100"
    PC = "4ccd04c6bd307ed3e48e8cd315d5e5bb15aac997d193ededeb480773d8c376a9"
    PA = "db3e2b006ee58e65791487588690824aa08f34c3b3be982036532322666f6cee"
    S = "5c8b7630ef446344669af3d4e406f590883efd0b94b39afb772f1afde0223989"

    def test_vectores_fijos_de_la_referencia(self):
        # la referencia (hmac/hashlib) da los valores escritos a mano en esta prueba
        self.assertEqual(ref_proof(K(), "cliente", self.NC, self.NA), self.PC)
        self.assertEqual(ref_proof(K(), "agente", self.NC, self.NA), self.PA)
        self.assertEqual(ref_session(K(), self.NC, self.NA).hex(), self.S)
        self.assertEqual(agent.proof(K(), "cliente", self.NC, self.NA), self.PC)
        self.assertEqual(agent.proof(K(), "agente", self.NC, self.NA), self.PA)
        self.assertEqual(agent.session_key(K(), self.NC, self.NA).hex(), self.S)

    def test_formato_del_agente_en_la_red(self):
        sc, sa = socket.socketpair()
        peer = Peer(sc)
        res = {}

        def agente():
            with mock.patch("agmen.agent.secrets.token_hex", return_value=self.NA):
                res["canal"] = agent_handshake(sa, K(), version="9.9.9")

        t = threading.Thread(target=agente, daemon=True)
        t.start()
        peer.send(f"agmen-agent/1 hola {self.NC}\n")
        self.assertEqual(peer.line(), f"agmen-agent/1 hola {self.NA} 9.9.9")
        peer.send(f"prueba {self.PC}\n")
        self.assertEqual(peer.line(), f"prueba {self.PA}")
        t.join(5)
        # y el primer mensaje del cliente, firmado con S, lo lee el canal del agente
        peer.send(ref_line(bytes.fromhex(self.S), "c", 1, '{"op":"ping","t":1.5}'))
        self.assertEqual(res["canal"].recv(2), {"op": "ping", "t": 1.5})
        res["canal"].send({"op": "pong", "t": 1.5})
        self.assertEqual(peer.line(), ref_line(bytes.fromhex(self.S), "a", 1, '{"op":"pong","t":1.5}')[:-1].decode())
        peer.close()
        sa.close()

    def test_formato_del_cliente_en_la_red(self):
        sc, sa = socket.socketpair()
        peer = Peer(sa)
        res = {}

        def cliente():
            with mock.patch("agmen.agent.secrets.token_hex", return_value=self.NC):
                res["canal"] = client_handshake(sc, K())

        t = threading.Thread(target=cliente, daemon=True)
        t.start()
        self.assertEqual(peer.line(), f"agmen-agent/1 hola {self.NC}")
        peer.send(f"agmen-agent/1 hola {self.NA} 1.2.3\n")
        self.assertEqual(peer.line(), f"prueba {self.PC}")
        peer.send(f"prueba {self.PA}\n")
        t.join(5)
        canal = res["canal"]
        self.assertEqual(canal.peer_version, "1.2.3")
        canal.send({"op": "ping", "t": 1.5})
        self.assertEqual(peer.line(), ref_line(bytes.fromhex(self.S), "c", 1, '{"op":"ping","t":1.5}')[:-1].decode())
        peer.close()
        sc.close()

    def test_la_clave_no_viaja(self):
        # nada de lo que cruza la red contiene la clave, ni K, ni S (se captura todo)
        sc, sa = socket.socketpair()
        capturado = bytearray()
        a2, b2 = socket.socketpair()      # el "cable": cliente <-> agente pasando por este puente
        c2, d2 = socket.socketpair()

        def puente(origen, destino):
            while True:
                try:
                    data = origen.recv(65536)
                except OSError:
                    break
                if not data:
                    break
                capturado.extend(data)
                try:
                    destino.sendall(data)
                except OSError:
                    break

        threading.Thread(target=puente, args=(b2, c2), daemon=True).start()
        threading.Thread(target=puente, args=(c2, b2), daemon=True).start()
        res = {}
        t = threading.Thread(target=lambda: res.setdefault("a", agent_handshake(d2, K())), daemon=True)
        t.start()
        canal = client_handshake(a2, K())
        t.join(5)
        canal.send({"op": "sesion", "x": "y"})
        res["a"].recv(2)
        time.sleep(0.2)
        datos = bytes(capturado)
        for secreto in (KEY.encode(), KEY.replace("-", "").encode(), K(), K().hex().encode()):
            self.assertNotIn(secreto, datos)
        canal.close()
        res["a"].close()


class TestCanal(unittest.TestCase):
    S = bytes.fromhex("5c8b7630ef446344669af3d4e406f590883efd0b94b39afb772f1afde0223989")

    def setUp(self):
        self.sc, self.sa = socket.socketpair()
        self.cliente = Channel(self.sc, self.S, "c")
        self.agente = Channel(self.sa, self.S, "a")
        self.peer_c = Peer(self.sc)       # para escribir líneas crudas hacia el agente (por el socket del cliente)
        self.addCleanup(lambda: (self.cliente.close(), self.agente.close()))

    def malo(self, datos: bytes, *, frag: str = "") -> ChannelError:
        """Escribe `datos` crudos hacia el agente y comprueba que recv() levanta ChannelError (no Timeout ni Closed)."""
        self.sc.sendall(datos)
        with self.assertRaises(ChannelError) as ctx:
            self.agente.recv(2)
        e = ctx.exception
        self.assertNotIsInstance(e, (ChannelTimeout, ChannelClosed), str(e))
        if frag:
            self.assertIn(frag, str(e))
        return e

    def test_formato_exacto_y_vectores(self):
        self.sc.settimeout(2)
        self.cliente.send({"op": "ping", "t": 1.5})
        raw = Peer(self.sa).line()
        self.assertEqual(raw, '1 {"op":"ping","t":1.5} abf6628a56840d9a62e07d7670936ead562102b9cdf5b14d262b51406242f169')
        self.agente.send({"op": "ping", "t": 1.5})     # el primero de ESTE sentido es el n=1
        self.agente.send({"op": "ping", "t": 1.5})
        peer = Peer(self.sc)
        peer.line()
        self.assertEqual(peer.line(), '2 {"op":"ping","t":1.5} '
                                      '16f7c3e052178f3fe439e90eeb119575a971aa6f14e7d8ddb7a2373cba35f6a0')

    def test_json_compacto_y_sin_ascii_escapado(self):
        self.cliente.send({"op": "log", "linea": "ñ ✔ \"x\"", "n": 3, "l": [1, 2], "d": {"a": None}})
        raw = Peer(self.sa).line()
        n, _, resto = raw.partition(" ")
        cuerpo, _, firma = resto.rpartition(" ")
        self.assertEqual(n, "1")
        self.assertEqual(cuerpo, '{"op":"log","linea":"ñ ✔ \\"x\\"","n":3,"l":[1,2],"d":{"a":null}}')
        self.assertEqual(firma, ref_mac(self.S, "c|1|" + cuerpo).hex())

    def test_contadores_por_sentido(self):
        for i in range(3):
            self.cliente.send({"op": "ping", "t": i})
        for i in range(3):
            self.assertEqual(self.agente.recv(2)["t"], i)
        for i in range(2):
            self.agente.send({"op": "pong", "t": i})
        for i in range(2):
            self.assertEqual(self.cliente.recv(2)["t"], i)
        self.cliente.send({"op": "ping", "t": 99})     # el sentido cliente→agente sigue en 4
        self.assertEqual(self.agente.recv(2)["t"], 99)

    def test_mensajes_en_un_solo_paquete_y_partidos(self):
        a, b = ref_line(self.S, "c", 1, '{"op":"a"}'), ref_line(self.S, "c", 2, '{"op":"b"}')
        self.sc.sendall(a + b)
        self.assertEqual([self.agente.recv(2)["op"], self.agente.recv(2)["op"]], ["a", "b"])
        c = ref_line(self.S, "c", 3, '{"op":"c"}')
        for trozo in (c[:3], c[3:20], c[20:]):
            self.sc.sendall(trozo)
            time.sleep(0.05)
        self.assertEqual(self.agente.recv(2)["op"], "c")

    def test_recv_sin_nada_es_timeout_y_no_rompe_el_canal(self):
        t0 = time.monotonic()
        with self.assertRaises(ChannelTimeout):
            self.agente.recv(0.3)
        self.assertLess(time.monotonic() - t0, 10)
        self.cliente.send({"op": "ping"})
        self.assertEqual(self.agente.recv(2)["op"], "ping")

    def test_la_otra_punta_cierra(self):
        self.sc.close()
        with self.assertRaises(ChannelClosed):
            self.agente.recv(2)

    def test_firma_mala_cierra(self):
        linea = ref_line(self.S, "c", 1, '{"op":"ping"}')
        self.malo(linea[:-3] + b"00\n" if linea[-3:-1] != b"00" else linea[:-3] + b"11\n", frag="firma")

    def test_firma_de_otra_sesion_cierra(self):
        otra = ref_session(K(), "a" * 32, "b" * 32)
        self.malo(ref_line(otra, "c", 1, '{"op":"ping"}'), frag="firma")

    def test_firma_del_cuerpo_cambiado_cierra(self):
        linea = ref_line(self.S, "c", 1, '{"op":"ping","t":1}')
        self.malo(linea.replace(b'"t":1', b'"t":2'), frag="firma")

    def test_n_repetido_cierra(self):
        self.sc.sendall(ref_line(self.S, "c", 1, '{"op":"a"}'))
        self.agente.recv(2)
        self.malo(ref_line(self.S, "c", 1, '{"op":"a"}'), frag="repetido o fuera de orden")

    def test_n_saltado_cierra(self):
        self.malo(ref_line(self.S, "c", 2, '{"op":"a"}'), frag="cuando tocaba el 1")

    def test_n_saltado_despues_de_uno_bueno(self):
        self.sc.sendall(ref_line(self.S, "c", 1, '{"op":"a"}'))
        self.agente.recv(2)
        self.malo(ref_line(self.S, "c", 3, '{"op":"a"}'), frag="cuando tocaba el 2")

    def test_n_con_ceros_a_la_izquierda_o_signo_cierra(self):
        for n in ("01", "001", "+1", "-1", "1.0", "0x1", "０１", "1 ", "", "abc", "0"):
            with self.subTest(n=n):
                s2, a2 = socket.socketpair()
                canal = Channel(a2, self.S, "a")
                cuerpo = '{"op":"a"}'
                firma = ref_mac(self.S, f"c|1|{cuerpo}").hex()
                s2.sendall(f"{n} {cuerpo} {firma}\n".encode())
                with self.assertRaises(ChannelError):
                    canal.recv(1)
                canal.close()
                s2.close()

    def test_linea_reflejada_cierra(self):
        # lo que el agente mandó, devuelto tal cual al agente (firma 'a' donde se espera 'c')
        self.agente.send({"op": "ping"})
        reflejo = Peer(self.sc).line()
        self.malo((reflejo + "\n").encode(), frag="firma")

    def test_linea_del_otro_sentido_con_n_correcto_cierra(self):
        self.malo(ref_line(self.S, "a", 1, '{"op":"ping"}'), frag="firma")      # firmada como 'a' hacia el agente

    def test_mas_de_64_kib_cierra(self):
        cuerpo = '{"op":"x","d":"' + "a" * (agent.MAX_LINE + 10) + '"}'
        self.malo(ref_line(self.S, "c", 1, cuerpo), frag="más de")

    def test_justo_en_el_limite_de_64_kib_pasa(self):
        base = '{"op":"x","d":"'
        resto = '"}'
        # la línea completa (n + espacio + cuerpo + espacio + 64 hex) mide exactamente MAX_LINE bytes
        relleno = agent.MAX_LINE - len(f"1 {base}{resto} ") - 64
        cuerpo = base + "a" * relleno + resto
        linea = ref_line(self.S, "c", 1, cuerpo)
        self.assertEqual(len(linea) - 1, agent.MAX_LINE)
        self.sc.sendall(linea)
        self.assertEqual(self.agente.recv(5)["op"], "x")

    def test_64_kib_sin_salto_de_linea_cierra(self):
        self.malo(b"a" * (agent.MAX_LINE + 100), frag="más de")

    def test_json_invalido_cierra(self):
        for cuerpo in ("{op:ping}", '{"op":"ping"', "no es json", '{"op":"ping"}}', "", "{'op':'x'}"):
            with self.subTest(cuerpo=cuerpo):
                s2, a2 = socket.socketpair()
                canal = Channel(a2, self.S, "a")
                s2.sendall(ref_line(self.S, "c", 1, cuerpo))
                with self.assertRaises(ChannelError) as ctx:
                    canal.recv(1)
                self.assertNotIsInstance(ctx.exception, (ChannelTimeout, ChannelClosed))
                canal.close()
                s2.close()

    def test_json_anidado_30000_niveles_cierra_sin_romper_el_proceso(self):
        for cuerpo in ('{"op":"x","a":' + "[" * 30000 + "]" * 30000 + "}", "[" * 30000 + "]" * 30000,
                       '{"op":"x","a":' + '{"b":' * 30000 + "1" + "}" * 30000 + "}"):
            with self.subTest(inicio=cuerpo[:20]):
                s2, a2 = socket.socketpair()
                canal = Channel(a2, self.S, "a")
                s2.sendall(ref_line(self.S, "c", 1, cuerpo))
                with self.assertRaises(ChannelError) as ctx:
                    canal.recv(2)
                self.assertNotIsInstance(ctx.exception, (ChannelTimeout, ChannelClosed))
                canal.close()
                s2.close()

    def test_lista_en_vez_de_objeto_cierra(self):
        for cuerpo in ('[{"op":"x"}]', '"op"', "1", "null", "true"):
            with self.subTest(cuerpo=cuerpo):
                s2, a2 = socket.socketpair()
                canal = Channel(a2, self.S, "a")
                s2.sendall(ref_line(self.S, "c", 1, cuerpo))
                with self.assertRaises(ChannelError) as ctx:
                    canal.recv(1)
                self.assertIn("no es un objeto con 'op'", str(ctx.exception))
                canal.close()
                s2.close()

    def test_op_que_no_es_texto_cierra(self):
        for cuerpo in ('{"op":1}', '{"op":null}', '{"op":["a"]}', '{"op":{"a":1}}', '{"x":"ping"}', "{}",
                       '{"op":true}'):
            with self.subTest(cuerpo=cuerpo):
                s2, a2 = socket.socketpair()
                canal = Channel(a2, self.S, "a")
                s2.sendall(ref_line(self.S, "c", 1, cuerpo))
                with self.assertRaises(ChannelError):
                    canal.recv(1)
                canal.close()
                s2.close()

    def test_no_es_utf8_cierra(self):
        self.malo(b"\xff\xfe\xfd 1 2\n", frag="UTF-8")

    def test_linea_sin_la_forma_n_json_firma_cierra(self):
        for linea in (b"solo-una-palabra\n", b"1 \n", b"1 {} \n", b"1 {\"op\":\"x\"}\n", b"\n", b"1 {\"op\":\"x\"} zz\n"):
            with self.subTest(linea=linea):
                s2, a2 = socket.socketpair()
                canal = Channel(a2, self.S, "a")
                s2.sendall(linea)
                with self.assertRaises(ChannelError) as ctx:
                    canal.recv(1)
                self.assertNotIsInstance(ctx.exception, (ChannelTimeout, ChannelClosed))
                canal.close()
                s2.close()

    def test_un_error_no_consume_el_numero(self):
        # (el contador solo avanza con un mensaje bueno; igual, después de un error el canal no se usa)
        try:
            self.malo(ref_line(self.S, "c", 5, '{"op":"a"}'))
        finally:
            pass
        self.assertEqual(self.agente._received, 0)

    def test_enviar_mas_de_64_kib_no_se_manda_y_el_canal_sigue_bien(self):
        with self.assertRaises(MessageTooLarge):
            self.cliente.send({"op": "x", "d": "a" * (agent.MAX_LINE + 1)})
        self.cliente.send({"op": "ping"})
        self.assertEqual(self.agente.recv(2)["op"], "ping")      # el n=1: el grande no gastó número

    def test_send_sin_op_es_error_de_programacion(self):
        for malo in ({}, {"op": 1}, [], "x", None):
            with self.subTest(malo=malo):
                with self.assertRaises(ValueError):
                    self.cliente.send(malo)

    def test_lado_invalido(self):
        with self.assertRaises(ValueError):
            Channel(self.sc, self.S, "x")

    def test_send_desde_varios_hilos_no_mezcla_lineas(self):
        def mandar(i):
            for j in range(50):
                self.cliente.send({"op": "ping", "t": f"{i}-{j}", "x": "y" * 200})

        hilos = [threading.Thread(target=mandar, args=(i,)) for i in range(6)]
        for h in hilos:
            h.start()
        recibidos = [self.agente.recv(5) for _ in range(300)]       # si se mezclaran, la firma fallaría
        for h in hilos:
            h.join()
        self.assertEqual(len(recibidos), 300)


# ===========================================================================
# 3. Lo escrito de cada nodo, qué [[machine]] soy, AGMEN_REMOTE_HOSTS
# ===========================================================================

def toml_con(maquinas: str, nodos: str, extra: str = "") -> str:
    return f'[robot]\nname = "r"\n{maquinas}\n{nodos}\n[hub]\nenabled = false\n[web]\nenabled = false\n{extra}'


def cargar(texto: str, perfil: str | None = None):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "robot.toml"
        p.write_text(texto, encoding="utf-8")
        return load_config(p, perfil)


MAQ = '[[machine]]\nname = "robot"\nhost = "192.0.2.10"\n[[machine]]\nname = "brazo"\nhost = "192.0.2.11"\n'


class TestNodosEscritos(unittest.TestCase):
    def nodo(self, name, machine="robot", **claves):
        t = f'[[node]]\nname = "{name}"\ncmd = "python x.py"\n'
        if machine:
            t += f'machine = "{machine}"\n'
        for k, v in claves.items():
            t += f"{k} = {v}\n"
        return t

    def test_solo_las_seis_claves_y_solo_lo_escrito(self):
        c = cargar(toml_con(MAQ, self.nodo("motor", cwd='"sub"', env='{ A = "1" }', restart='"always"',
                                           autostart="false", profile='["real", "mesa"]')
                            + self.nodo("imu") + self.nodo("ia", machine="")))
        w = written_nodes(c, "robot")
        self.assertEqual(list(w), ["motor", "imu"])           # el orden del archivo, solo los de esa machine
        self.assertEqual(w["motor"], {"cmd": "python x.py", "env": {"A": "1"}, "cwd": "sub", "restart": "always",
                                      "autostart": False, "profile": ["real", "mesa"]})
        self.assertEqual(w["imu"], {"cmd": "python x.py"})   # lo no escrito no aparece
        self.assertNotIn("name", w["motor"])
        self.assertNotIn("machine", w["motor"])
        json.dumps(w)                                         # viaja como JSON

    def test_por_perfil_solo_los_del_perfil_activo(self):
        texto = toml_con(MAQ, self.nodo("motor", profile='"real"') + self.nodo("mundo", profile='"sim"'))
        self.assertEqual(list(written_nodes(cargar(texto, "real"), "robot")), ["motor"])
        self.assertEqual(list(written_nodes(cargar(texto, "sim"), "robot")), ["mundo"])

    def test_un_mux_con_machine_se_compara_por_las_seis_claves(self):
        mux = ('[[mux]]\nname = "ruedas"\noutput = "127.0.0.1:7500"\nmachine = "robot"\n'
               '[[mux.input]]\nname = "a"\nlisten = "127.0.0.1:7501"\npriority = 1\n')
        c = cargar(toml_con(MAQ, self.nodo("motor") + mux))
        w = written_nodes(c, "robot")
        self.assertEqual(sorted(w), ["motor", "ruedas"])      # (config.nodes pone los mux antes que los [[node]])
        self.assertTrue(set(w["ruedas"]) <= set(agent.WRITTEN_KEYS), w["ruedas"])
        self.assertEqual(written_nodes(c, "brazo"), {})

    def test_session_message_lleva_todo_y_no_arrancar(self):
        c = cargar(toml_con(MAQ, self.nodo("motor") + self.nodo("imu")), None)
        m = agent.session_message(c, "robot", "real")
        self.assertEqual(m["op"], "sesion")
        self.assertEqual((m["perfil"], m["reloj"], m["agmen"], m["maquina"]), ("real", "real", __version__, "robot"))
        self.assertEqual(list(m["nodos"]), ["motor", "imu"])
        self.assertEqual(m["no_arrancar"], ["motor", "imu"])
        json.dumps(m)


class TestDiffNodos(unittest.TestCase):
    BASE = {"cmd": "python x.py", "env": {"A": "1"}, "cwd": "sub", "restart": "always", "autostart": True,
            "profile": "real"}

    def mod(self, **cambios):
        d = dict(self.BASE)
        for k, v in cambios.items():
            if v is None:
                d.pop(k, None)
            else:
                d[k] = v
        return d

    def test_iguales_no_difieren(self):
        self.assertEqual(diff_nodes("robot", "real", {"motor": self.BASE}, {"motor": dict(self.BASE)}), [])
        self.assertEqual(diff_nodes("robot", "real", {}, {}), [])

    def test_cada_clave_por_separado(self):
        cambios = {"cmd": "python y.py", "env": {"A": "2"}, "cwd": "otra", "restart": "never", "autostart": False,
                   "profile": "sim"}
        for clave, valor in cambios.items():
            with self.subTest(clave=clave):
                out = diff_nodes("robot", "real", {"motor": self.mod(**{clave: valor})}, {"motor": self.BASE})
                self.assertEqual(out, [f"el nodo motor es distinto en su robot.toml ({clave})"])

    def test_clave_escrita_de_un_lado_y_no_del_otro_es_diferencia(self):
        for clave in agent.WRITTEN_KEYS:
            with self.subTest(clave=clave):
                self.assertEqual(len(diff_nodes("robot", "real", {"motor": self.mod(**{clave: None})},
                                                {"motor": self.BASE})), 1)
                self.assertEqual(len(diff_nodes("robot", "real", {"motor": self.BASE},
                                                {"motor": self.mod(**{clave: None})})), 1)

    def test_autostart_ausente_no_es_lo_mismo_que_false(self):
        self.assertEqual(len(diff_nodes("robot", "real", {"m": {"cmd": "x"}}, {"m": {"cmd": "x", "autostart": False}})), 1)

    def test_varias_claves_a_la_vez_en_el_orden_del_contrato(self):
        out = diff_nodes("robot", "real", {"motor": self.mod(profile="sim", cmd="otro", cwd="z")}, {"motor": self.BASE})
        self.assertEqual(out, ["el nodo motor es distinto en su robot.toml (cmd, cwd, profile)"])

    def test_todas_las_diferencias_a_la_vez(self):
        theirs = {"motor": self.mod(cmd="otro"), "solo_laptop": self.BASE, "imu": self.mod(restart="never")}
        mine = {"motor": self.BASE, "imu": self.BASE, "solo_robot": self.BASE}
        out = diff_nodes("robot", "mesa", theirs, mine)
        self.assertEqual(len(out), 4, out)
        self.assertIn("el nodo motor es distinto en su robot.toml (cmd)", out)
        self.assertIn("el nodo imu es distinto en su robot.toml (restart)", out)
        self.assertIn("el nodo solo_laptop no está en su robot.toml (con machine = \"robot\" en el perfil 'mesa')", out)
        self.assertIn("el nodo solo_robot está en su robot.toml (con machine = \"robot\" en el perfil 'mesa') "
                      "y no en el tuyo", out)

    def test_nodos_basura_del_cliente_no_revientan(self):
        # el valor de un nodo que no es un objeto: se toma como vacío, sin excepción
        out = diff_nodes("robot", "real", {"motor": "texto"}, {"motor": self.BASE})
        self.assertTrue(out)

    def test_de_punta_a_punta_con_dos_robot_toml(self):
        a = cargar(toml_con(MAQ, '[[node]]\nname = "m"\ncmd = "python a.py"\nmachine = "robot"\n'))
        b = cargar(toml_con(MAQ, '[[node]]\nname = "m"\ncmd = "python b.py"\nmachine = "robot"\n'))
        self.assertEqual(diff_nodes("robot", "real", written_nodes(a, "robot"), written_nodes(b, "robot")),
                         ["el nodo m es distinto en su robot.toml (cmd)"])
        self.assertEqual(diff_nodes("robot", "real", written_nodes(a, "robot"), written_nodes(a, "robot")), [])

    def test_comparar_lo_escrito_no_rutas_resueltas(self):
        # dos copias del repo en carpetas distintas: lo escrito es igual aunque las rutas resueltas no
        t = toml_con(MAQ, '[[node]]\nname = "m"\ncmd = "./build/bin/motor --rate 10"\nmachine = "robot"\n')
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            (Path(d1) / "robot.toml").write_text(t, encoding="utf-8")
            (Path(d2) / "robot.toml").write_text(t, encoding="utf-8")
            c1, c2 = load_config(Path(d1) / "robot.toml"), load_config(Path(d2) / "robot.toml")
        self.assertNotEqual(c1.base_dir, c2.base_dir)
        self.assertEqual(diff_nodes("robot", "real", written_nodes(c1, "robot"), written_nodes(c2, "robot")), [])


class TestElegirMachine(unittest.TestCase):
    def cfg(self, hosts, extra_nodes=True):
        maq = "".join(f'[[machine]]\nname = "{n}"\nhost = "{h}"\n' for n, h in hosts.items())
        return cargar(toml_con(maq, '[[node]]\nname = "m"\ncmd = "x"\n'))

    def test_una_sola_es_de_aqui(self):
        self.assertEqual(pick_machine(self.cfg({"laptop": "192.0.2.50", "robot": "127.0.0.1"})).name, "robot")

    def test_ninguna_es_de_aqui(self):
        with self.assertRaises(AgentError) as ctx:
            pick_machine(self.cfg({"a": "192.0.2.10", "b": "192.0.2.11"}))
        t = str(ctx.exception)
        self.assertIn("ninguna [[machine]]", t)
        self.assertIn("--machine NOMBRE", t)
        self.assertIn("'a' (192.0.2.10)", t)

    @unittest.skipUnless(source_ip_available("127.0.0.2"), "necesita 127.0.0.2 como dirección de esta computadora")
    def test_varias_son_de_aqui(self):
        with self.assertRaises(AgentError) as ctx:
            pick_machine(self.cfg({"a": "127.0.0.1", "b": "127.0.0.2"}))
        self.assertIn("varias [[machine]]", str(ctx.exception))
        self.assertIn("a, b", str(ctx.exception))
        self.assertIn("--machine NOMBRE", str(ctx.exception))

    def test_machine_pedida_gana_aunque_no_sea_de_aqui(self):
        c = self.cfg({"a": "192.0.2.10", "b": "127.0.0.1"})
        self.assertEqual(pick_machine(c, "a").name, "a")
        self.assertEqual(pick_machine(c, "b").name, "b")

    def test_machine_pedida_que_no_existe_sugiere(self):
        c = self.cfg({"robot": "127.0.0.1", "laptop": "192.0.2.50"})
        with self.assertRaises(AgentError) as ctx:
            pick_machine(c, "robto")
        self.assertIn("¿Quisiste decir 'robot'?", str(ctx.exception))
        with self.assertRaises(AgentError) as ctx:
            pick_machine(c, "zzzzzz")
        self.assertIn("Hay: robot, laptop", str(ctx.exception))

    def test_sin_machine_declaradas_da_el_ejemplo(self):
        with self.assertRaises(AgentError) as ctx:
            pick_machine(cargar('[robot]\nname = "r"\n[[node]]\nname = "m"\ncmd = "x"\n'))
        self.assertIn("no declara ninguna [[machine]]", str(ctx.exception))
        self.assertIn('host = "192.168.1.20"', str(ctx.exception))

    def test_agmen_remote_hosts_no_se_aplica_a_machine_pedida_pero_si_a_la_automatica(self):
        c = self.cfg({"robot": "127.0.0.1"})
        with mock.patch.dict(os.environ, {"AGMEN_REMOTE_HOSTS": "127.0.0.1"}):
            self.assertEqual(pick_machine(c, "robot").name, "robot")
            with self.assertRaises(AgentError):
                pick_machine(c)       # el entorno del agente no debe tener AGMEN_REMOTE_HOSTS


class TestHostsForzados(unittest.TestCase):
    """AGMEN_REMOTE_HOSTS: convierte "aquí" en "otra computadora" y nunca al revés."""

    def lugar(self, hosts, forzados, **kw):
        with mock.patch.dict(os.environ, {"AGMEN_REMOTE_HOSTS": forzados}):
            return machines.check_hosts(hosts, **kw)

    def test_sin_la_variable_nada_cambia(self):
        self.assertTrue(self.lugar(["127.0.0.1"], "")["127.0.0.1"].here)
        self.assertEqual(machines.forced_remote({}), set())

    def test_una_ip_de_aqui_pasa_a_otra(self):
        p = self.lugar(["127.0.0.1"], "127.0.0.1")["127.0.0.1"]
        self.assertEqual((p.here, p.why), (False, "other"))
        self.assertIn("AGMEN_REMOTE_HOSTS", p.note)

    def test_solo_las_nombradas(self):
        r = self.lugar(["127.0.0.1", "::1"], "127.0.0.1")
        self.assertFalse(r["127.0.0.1"].here)
        self.assertTrue(r["::1"].here)

    def test_localhost_y_nombres_que_resuelven_a_una_forzada(self):
        self.assertFalse(self.lugar(["localhost"], "127.0.0.1")["localhost"].here)
        self.assertFalse(self.lugar(["LOCALHOST."], "localhost")["LOCALHOST."].here)
        with mock.patch("socket.getaddrinfo", return_value=[(socket.AF_INET, 2, 17, "", ("127.0.0.1", 0))]):
            self.assertFalse(self.lugar(["mi-robot.local"], "127.0.0.1")["mi-robot.local"].here)

    def test_normaliza_mayusculas_espacios_punto_final_y_ipv4_mapeada(self):
        self.assertFalse(self.lugar(["127.0.0.1"], "  ::ffff:127.0.0.1 , otra ")["127.0.0.1"].here)
        self.assertFalse(self.lugar(["localhost"], "LocalHost.")["localhost"].here)
        self.assertEqual(machines.forced_remote({"AGMEN_REMOTE_HOSTS": "A.B., ,127.0.0.1,"}), {"a.b", "127.0.0.1"})

    def test_nunca_convierte_otra_en_aqui(self):
        # "otra" se queda "otra" aunque esté en la lista; y listar una computadora ajena no la vuelve de aquí
        r = self.lugar(["192.0.2.10", "198.51.100.7"], "192.0.2.10,127.0.0.1")
        self.assertFalse(r["192.0.2.10"].here)
        self.assertFalse(r["198.51.100.7"].here)
        self.assertEqual(r["198.51.100.7"].why, "other")
        for why in ("unresolved", "special", "zone"):
            with self.subTest(why=why):
                p = self.lugar(["0.0.0.0", "fe80::1"], "127.0.0.1,fe80::1")
                self.assertFalse(p["0.0.0.0"].here)
                self.assertEqual(p["0.0.0.0"].why, "special")
                self.assertEqual(p["fe80::1"].why, "zone")

    def test_una_mixta_sigue_mixta(self):
        info = [(socket.AF_INET, 2, 17, "", ("127.0.0.1", 0)), (socket.AF_INET, 2, 17, "", ("192.0.2.10", 0))]
        with mock.patch("socket.getaddrinfo", return_value=info):
            p = self.lugar(["dos.local"], "10.9.9.9")["dos.local"]
        self.assertEqual(p.why, "mixed")

    def test_con_local_true_no_importa(self):
        c = cargar(toml_con(MAQ.replace("192.0.2.10", "127.0.0.1"),
                            '[[node]]\nname = "m"\ncmd = "x"\nmachine = "robot"\nprofile = "sim"\n',
                            "[profile.sim]\nlocal = true\n"), "sim")
        with mock.patch.dict(os.environ, {"AGMEN_REMOTE_HOSTS": "127.0.0.1"}):
            self.assertEqual(machines.place_machines(c), {"robot": Place(True, "local")})

    def test_place_machines_ve_la_forzada_como_otra(self):
        c = cargar(toml_con(MAQ.replace("192.0.2.10", "127.0.0.1"),
                            '[[node]]\nname = "m"\ncmd = "x"\nmachine = "robot"\n'))
        with mock.patch.dict(os.environ, {"AGMEN_REMOTE_HOSTS": "127.0.0.1"}):
            self.assertFalse(machines.place_machines(c)["robot"].here)
        self.assertTrue(machines.place_machines(c)["robot"].here)


class TestAddressError(unittest.TestCase):
    def test_conserva_las_notas_de_la_3a_antes_de_conectar(self):
        c = cargar(toml_con('[[machine]]\nname = "robot"\nhost = "robot.local"\n',
                            '[[node]]\nname = "m"\ncmd = "x"\nmachine = "robot"\n'))
        for why, nota in (("unresolved", "no pude resolver 'robot.local' (la red no lo conoce)"),
                          ("timeout", "no pude resolver 'robot.local' en 1.5 s"),
                          ("zone", "falta %interfaz"), ("mixed", "una es de esta computadora y otra no"),
                          ("special", "no es la dirección de una computadora")):
            with self.subTest(why=why):
                e = machines.address_error(c, {"robot": Place(False, why, nota)})
                self.assertIn("no puedo conectarme con 'robot' (robot.local)", e)
                self.assertIn(nota, e)
                self.assertIn("Corrige host en robot.toml", e)

    def test_una_direccion_buena_se_intenta(self):
        c = cargar(toml_con('[[machine]]\nname = "robot"\nhost = "192.0.2.10"\n',
                            '[[node]]\nname = "m"\ncmd = "x"\nmachine = "robot"\n'))
        self.assertEqual(machines.address_error(c, {"robot": Place(False, "other")}), "")
        self.assertEqual(machines.address_error(c, {"robot": Place(True, "mine")}), "")
        self.assertEqual(machines.address_error(c, {}), "")


# ===========================================================================
# 4. La cola de salida del agente
# ===========================================================================

class TestNodosAjenosConListaIncompleta(unittest.TestCase):
    """§10.1: la comprobación de nodos ajenos falla cerrada: una lista incompleta del hub se reintenta una vez y, si
    sigue incompleta, la sesión se rechaza diciendo qué pasó y qué hacer."""

    def server(self):
        s = agent.AgentServer.__new__(agent.AgentServer)
        s.machine = "robot"
        return s

    def respuesta(self, lines, incompleta=None):
        from agmen import hub as H
        from agmen import hubproto as hp

        r = H.Respuesta(hp.parse("\n".join(lines).encode()))
        r.incompleta = incompleta
        return r

    def foreign(self, *respuestas):
        from agmen import hub as H

        q = mock.Mock(side_effect=list(respuestas))
        with mock.patch.object(H, "ping", return_value="9.9"), mock.patch.object(H, "query", q):
            out = self.server().foreign("127.0.0.1:7")
        return out, q.call_count

    def test_incompleta_dos_veces_rechaza(self):
        st = "agmen-hub/1 state version=x"
        (why, hub), veces = self.foreign(self.respuesta([st], (1, 3)), self.respuesta([st], (2, 3)))
        self.assertEqual(veces, 2)
        self.assertTrue(hub)
        self.assertIn("no llegó completa (2 de 3 partes, dos veces)", why)
        self.assertIn("Vuelve a correr agmen run; si se repite, hay demasiados topics", why)

    def test_incompleta_una_vez_reintenta(self):
        st = "agmen-hub/1 state version=x"
        out, veces = self.foreign(self.respuesta([st], (1, 3)), self.respuesta([st]))
        self.assertEqual((out, veces), (("", True), 2))
        ep = "agmen-hub/1 ep id=p1 node=imu kind=pub topic=/x"
        (why, _), veces = self.foreign(self.respuesta([st], (1, 3)), self.respuesta([st, ep]))
        self.assertIn("ya corren nodos que este agente no lanzó (imu)", why)

    def test_completa_como_antes(self):
        st = "agmen-hub/1 state version=x"
        self.assertEqual(self.foreign(self.respuesta([st])), (("", True), 1))
        ep = "agmen-hub/1 ep id=p1 node=imu kind=pub topic=/x"
        (why, hub), veces = self.foreign(self.respuesta([st, ep]))
        self.assertEqual(veces, 1)
        self.assertIn("ya corren nodos que este agente no lanzó (imu)", why)


class TestColaDeSalida(unittest.TestCase):
    def test_orden_y_log_descartable(self):
        ob = agent._Outbox()
        for i in range(3):
            self.assertTrue(ob.put({"op": "estado", "i": i}))
        self.assertTrue(ob.put({"op": "log", "n": 1}, droppable=True))
        self.assertEqual([ob.get()[0] for _ in range(4)], ["estado", "estado", "estado", "log"])

    def test_tope_en_mensajes_solo_descarta_logs(self):
        ob = agent._Outbox()
        for i in range(agent.OUT_MAX_MSGS):
            self.assertTrue(ob.put({"op": "log", "n": i}, droppable=True))
        self.assertFalse(ob.put({"op": "log", "n": -1}, droppable=True))
        self.assertEqual(ob.dropped, 1)
        self.assertTrue(ob.put({"op": "estado", "nodo": "x"}), "un estado nunca se descarta")
        self.assertEqual(len(ob.items), agent.OUT_MAX_MSGS + 1)

    def test_tope_en_bytes(self):
        ob = agent._Outbox()
        grande = {"op": "log", "linea": "x" * 100_000}
        aceptados = 0
        for _ in range(100):
            aceptados += ob.put(grande, droppable=True)
        self.assertLessEqual(ob.bytes, agent.OUT_MAX_BYTES)
        self.assertLess(aceptados, 100)
        self.assertGreater(ob.dropped, 0)
        self.assertEqual(ob.bytes, sum(i[2] for i in ob.items))

    def test_replies_full_y_espera(self):
        ob = agent._Outbox()
        for _ in range(agent.OUT_MAX_REPLIES):
            ob.put({"op": "pong", "t": 1})
        self.assertTrue(ob.replies_full())
        self.assertFalse(ob.wait_replies(0.1))
        for _ in range(agent.OUT_MAX_REPLIES // 2 + 1):
            ob.get()
        self.assertFalse(ob.replies_full())
        self.assertTrue(ob.wait_replies(0.1))

    def test_close_libera_la_memoria_y_ya_no_acepta(self):
        ob = agent._Outbox()
        for i in range(100):
            ob.put({"op": "log", "linea": "x" * 1000}, droppable=True)
        ob.close()
        self.assertEqual((len(ob.items), ob.bytes, ob.replies), (0, 0, 0))
        self.assertFalse(ob.put({"op": "estado"}))
        self.assertIsNone(ob.get())

    def test_get_espera_y_se_despierta(self):
        ob = agent._Outbox()
        res = []
        t = threading.Thread(target=lambda: res.append(ob.get()), daemon=True)
        t.start()
        time.sleep(0.2)
        self.assertEqual(res, [])
        ob.put({"op": "estado", "nodo": "a"})
        t.join(2)
        self.assertEqual(res[0][0], "estado")


class TestCortarLineas(unittest.TestCase):   # antes TestCortes: la tapaba la otra TestCortes y no corría
    def test_cortar_y_limpiar(self):
        self.assertEqual(agent._cut("hola"), "hola")
        largo = agent._cut("é" * 20000)
        self.assertLessEqual(len(largo.encode("utf-8")), agent.LOG_LINE_MAX + len(" … (línea cortada)".encode()))
        self.assertTrue(largo.endswith("(línea cortada)"))
        self.assertEqual(agent._clean("a\x1b[31mb\nc\x00"), "a�[31mb�c�")
        self.assertEqual(len(agent._clean("x" * 500)), 81)
        self.assertEqual(agent._printable("a\x1b[2Jb\nc"), "a�[2Jb\nc")
        self.assertEqual(agent._ip_key("::ffff:10.0.0.5"), "10.0.0.5")
        self.assertEqual(agent._ip_key("2001:db8:1:2:aaaa::1"), agent._ip_key("2001:db8:1:2:bbbb::2"))
        self.assertNotEqual(agent._ip_key("2001:db8:1:2::1"), agent._ip_key("2001:db8:1:3::1"))
        self.assertEqual(agent._ip_key("10.0.0.5"), "10.0.0.5")


# ===========================================================================
# 5. Un AgentServer real, en un hilo, con nodos de prueba en Python
# ===========================================================================

NODO_PY = '''\
import os, signal, sys, time
# (si el proceso que nos lanzó ignora SIGINT, Python no lo convierte en KeyboardInterrupt: se pide a mano)
signal.signal(signal.SIGINT, signal.default_int_handler)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)
name, marks = sys.argv[1], sys.argv[2]
with open(os.path.join(marks, "arranque_" + name), "a") as f:
    f.write(str(os.getpid()) + "\\n")
print("HOLA", name, os.getpid(), flush=True)
fin = time.time() + 120          # nunca se queda para siempre si algo sale mal
try:
    while time.time() < fin:
        with open(os.path.join(marks, "hb_" + name), "w") as f:
            f.write(str(time.time()))
        time.sleep(0.1)
except KeyboardInterrupt:
    pass
'''

BEFORE_PY = '''\
import sys
print("SALIDA-DEL-BEFORE", flush=True)
sys.exit(int(sys.argv[1]))
'''


class Cli:
    """Una laptop de mentira: un Channel y un hilo que junta todo lo que llega."""

    def __init__(self, ch: Channel, leer: bool = True) -> None:
        self.ch = ch
        self.msgs: list[dict] = []
        self.error: Exception | None = None
        self.cond = threading.Condition()
        self._parar = False
        if leer:
            threading.Thread(target=self._leer, daemon=True).start()

    def _leer(self) -> None:
        while not self._parar:
            try:
                m = self.ch.recv(0.3)
            except ChannelTimeout:
                continue
            except ChannelError as e:
                with self.cond:
                    self.error = e
                    self.cond.notify_all()
                return
            with self.cond:
                self.msgs.append(m)
                self.cond.notify_all()

    def send(self, obj: dict) -> None:
        self.ch.send(obj)

    def wait(self, pred, timeout: float = 30.0) -> bool:
        with self.cond:
            return self.cond.wait_for(lambda: pred(self.msgs) or self.error is not None, timeout) and pred(self.msgs)

    def first(self, op: str, **campos) -> dict | None:
        with self.cond:
            for m in self.msgs:
                if m["op"] == op and all(m.get(k) == v for k, v in campos.items()):
                    return m
        return None

    def all(self, op: str, **campos) -> list[dict]:
        with self.cond:
            return [m for m in self.msgs if m["op"] == op and all(m.get(k) == v for k, v in campos.items())]

    def estado(self, nodo: str):
        e = self.all("estado", nodo=nodo)
        return e[-1]["estado"] if e else None

    def hay(self, op: str, **campos):
        return lambda msgs: any(m["op"] == op and all(m.get(k) == v for k, v in campos.items()) for m in msgs)

    def close(self) -> None:
        self._parar = True
        self.ch.close()


class ServerCase(ConfigDirCase):
    """Un agente real escuchando en 127.0.0.1:<puerto libre>, con su robot.toml de prueba."""

    TOML = (
        '[robot]\nname = "r"\n\n[[machine]]\nname = "robot"\nhost = "127.0.0.1:{port}"\n\n'
        '[[node]]\nname = "motor"\ncmd = ["python", {nodo}, "motor", {marks}]\nmachine = "robot"\n\n'
        '[[node]]\nname = "imu"\ncmd = ["python", {nodo}, "imu", {marks}]\nmachine = "robot"\n\n'
        '[[node]]\nname = "ia"\ncmd = ["python", {nodo}, "ia", {marks}]\n\n'
        '[hub]\nenabled = false\n\n[web]\nenabled = false\n{extra}')

    def setUp(self):
        super().setUp()
        self.marks = self.dir / "marcas"
        self.marks.mkdir()
        (self.dir / "nodo.py").write_text(NODO_PY, encoding="utf-8")
        (self.dir / "before.py").write_text(BEFORE_PY, encoding="utf-8")
        self.toml = self.dir / "robot.toml"
        self.said: list[tuple[str, str]] = []
        self.clients: list[Cli] = []
        self.peers: list[socket.socket] = []
        # el hub de esta prueba (nadie escucha ahí): no el de 7390 de otro agmen que corra en esta computadora
        self.hub_port = free_udp_port()
        for patcher in (mock.patch.dict(os.environ, {"AGMEN_HUB": f"127.0.0.1:{self.hub_port}",
                                                     "AGMEN_REMOTE_HOSTS": ""}),
                        mock.patch("sys.stdout", io.StringIO())):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server: agent.AgentServer | None = None
        self.stop = threading.Event()
        self.addCleanup(self._limpiar)

    # --- el agente
    def write_toml(self, extra: str = "", **sust) -> Path:
        texto = self.TOML.format(port=self.port, nodo=json.dumps(str(self.dir / "nodo.py")),
                                 marks=json.dumps(str(self.marks)), extra=extra)
        for viejo, nuevo in sust.items():
            texto = texto.replace(viejo, nuevo)
        self.toml.write_text(texto, encoding="utf-8")
        return self.toml

    sin_hub = False      # las pruebas que arman su propio hub (o prueban que no lo hay) lo ponen en True

    def start_server(self, extra: str = "", write: bool = True, **kw):
        if not self.sin_hub:
            # un hub vacío vivo en AGMEN_HUB: sin él, cada comprobación de "nodos ajenos" espera 0.5 s a que nadie
            # conteste (dos por sesión). Un hub vacío no cuenta como nodos ajenos.
            from agmen import hub as hubmod
            hub = hubmod.Hub(f"127.0.0.1:{self.hub_port}", log=lambda t: None)
            parar = threading.Event()
            threading.Thread(target=hub.serve, args=(parar,), daemon=True).start()
            self.addCleanup(parar.set)
        self.server = agent.AgentServer(self.toml, "robot", KEY, port=0,
                                        say=lambda t, k="agmen": self.said.append((t, k)), **kw)
        self.server.bind()
        self.port = self.server.sock.getsockname()[1]
        if write:
            self.write_toml(extra)
        threading.Thread(target=self.server.serve, args=(self.stop,), daemon=True).start()
        return self.server

    def _limpiar(self):
        # primero el agente (con la laptop todavia conectada: sale rapido; si la laptop ya cerro, end() espera
        # hasta 2 s a que salga lo pendiente), despues las laptops
        self.stop.set()
        if self.server is not None:
            self.server.close()
        for c in self.clients:
            c.close()
        for s in self.peers:
            try:
                s.close()
            except OSError:
                pass
        # por si algo quedó vivo: se matan SOLO los procesos que escribieron su pid en marcas/
        for f in self.marks.glob("arranque_*"):
            for pid in f.read_text().split():
                matar(int(pid))

    # --- la laptop
    def conectar(self, key: bytes | None = None, source: str | None = None, leer: bool = True,
                 timeout: float = 30.0) -> Cli:
        s = socket.socket()
        if source:
            s.bind((source, 0))
        s.settimeout(15)
        s.connect(("127.0.0.1", self.port))
        ch = client_handshake(s, key or K(), timeout=max(timeout, 30.0))
        c = Cli(ch, leer)
        self.clients.append(c)
        return c

    def mensaje(self, toml: Path | None = None, clock: str = "real", **cambios) -> dict:
        m = agent.session_message(load_config(toml or self.toml), "robot", clock)
        m.update(cambios)
        return m

    def sesion(self, cli: Cli | None = None, msg: dict | None = None, arrancar: bool = True) -> tuple[Cli, dict]:
        """Abre una sesión y espera el sesion_ok (o el sesion_error). Con arrancar, el agente arranca solo los
        nodos (sin no_arrancar), como si la laptop no los frenara."""
        cli = cli or self.conectar()
        msg = msg or self.mensaje()
        if arrancar:
            msg.pop("no_arrancar", None)
        cli.send(msg)
        self.assertTrue(cli.wait(lambda ms: any(m["op"] in ("sesion_ok", "sesion_error") for m in ms)), cli.msgs)
        fin = cli.first("sesion_ok") or cli.first("sesion_error")
        return cli, fin

    def vivo(self, nodo: str, ventana: float = 3.0) -> bool:
        return nodo_vivo(self.marks, nodo, ventana)

    def espera(self, cond, timeout: float = 30.0, paso: float = 0.05) -> bool:
        fin = time.monotonic() + timeout
        while time.monotonic() < fin:
            if cond():
                return True
            time.sleep(paso)
        return cond()

    def pids(self, nodo: str) -> list[int]:
        f = self.marks / f"arranque_{nodo}"
        return [int(x) for x in f.read_text().split()] if f.exists() else []

    def sin_arrancar_nada(self):
        self.assertEqual(sorted(p.name for p in self.marks.iterdir()), [], "arrancó algún nodo")


def pid_vivo(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def nodo_vivo(marks: Path, nodo: str, ventana: float = 3.0) -> bool:
    """¿El nodo de prueba sigue vivo? Escribe un latido cada 0.1 s: con una ventana generosa (una máquina
    cargada se atrasa), y en POSIX además el último pid que arrancó tiene que existir (así "murió" se nota
    al instante en vez de esperar a que venza la ventana)."""
    try:
        if time.time() - (marks / f"hb_{nodo}").stat().st_mtime >= ventana:
            return False
        if os.name == "posix":
            pids = (marks / f"arranque_{nodo}").read_text().split()
            return bool(pids) and pid_vivo(int(pids[-1]))
        return True
    except (OSError, ValueError):
        return False


def matar(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
        else:
            os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


class TestSesion(ServerCase):
    def test_sesion_ok_estados_y_logs(self):
        self.start_server()
        cli, ok = self.sesion()
        self.assertEqual(ok["op"], "sesion_ok")
        self.assertRegex(ok.get("sesion", ""), r"^[0-9a-f]{32}$")   # 3c: el identificador para reanudar…
        self.assertEqual(ok.get("gracia"), 10)                        # … y la gracia
        self.assertEqual(ok, {"op": "sesion_ok", "maquina": "robot", "agmen": __version__, "nodos": ["motor", "imu"],
                              "sesion": ok["sesion"], "gracia": 10})
        self.assertTrue(cli.wait(lambda ms: any(m["op"] == "estado" and m["nodo"] == "motor" and m["estado"] == "corriendo"
                                               for m in ms)))
        self.assertTrue(cli.wait(cli.hay("estado", nodo="imu", estado="corriendo")))
        e = cli.first("estado", nodo="motor", estado="corriendo")
        self.assertIsInstance(e["pid"], int)
        self.assertTrue(self.espera(lambda: self.pids("motor")))
        self.assertEqual(e["pid"], self.pids("motor")[-1])
        self.assertIsNone(e["codigo"])
        self.assertTrue(self.espera(lambda: self.vivo("motor") and self.vivo("imu")))
        self.assertFalse((self.marks / "arranque_ia").exists(), "'ia' es de la laptop: el agente no lo arranca")
        # los logs: numerados de uno en uno por nodo, con la salida del nodo
        self.assertTrue(cli.wait(lambda ms: any(m["op"] == "log" and m["nodo"] == "motor" and "HOLA motor" in m["linea"]
                                               for m in ms)))
        for nodo in ("motor", "imu"):
            ns = [m["n"] for m in cli.all("log", nodo=nodo)]
            self.assertEqual(ns, list(range(1, len(ns) + 1)), f"los log de {nodo} no suben de uno en uno: {ns}")
        self.assertTrue(any("▶ iniciado" in m["linea"] for m in cli.all("log", nodo="motor")))

    def test_ningun_estado_antes_de_sesion_ok(self):
        self.start_server()
        cli, _ = self.sesion()
        cli.wait(cli.hay("estado", nodo="motor", estado="corriendo"))
        ops = [m["op"] for m in cli.msgs]
        self.assertLess(ops.index("sesion_ok"), ops.index("estado"))

    def test_ningun_estado_despues_de_sesion_error(self):
        self.start_server()
        cli, fin = self.sesion(msg=self.mensaje(maquina="otra"))
        self.assertEqual(fin["op"], "sesion_error")
        time.sleep(0.5)
        self.assertEqual(cli.all("estado"), [])
        self.assertTrue(cli.wait(lambda ms: True) and self.espera(lambda: cli.error is not None, 30), "no cerró")

    def test_detener_arrancar_reiniciar(self):
        self.start_server()
        cli, _ = self.sesion()
        cli.wait(cli.hay("estado", nodo="motor", estado="corriendo"))
        self.assertTrue(self.espera(lambda: self.vivo("motor") and self.vivo("imu")))
        pid1 = cli.first("estado", nodo="motor", estado="corriendo")["pid"]
        cli.send({"op": "detener", "nodo": "motor"})
        self.assertTrue(cli.wait(cli.hay("estado", nodo="motor", estado="detenido")))
        self.assertTrue(self.espera(lambda: not self.vivo("motor")), "el nodo detenido sigue escribiendo")
        self.assertTrue(self.vivo("imu"), "detener motor no debe tocar imu")
        cli.send({"op": "arrancar", "nodo": "motor"})
        self.assertTrue(self.espera(lambda: len(cli.all("estado", nodo="motor", estado="corriendo")) >= 2))
        pid2 = cli.all("estado", nodo="motor", estado="corriendo")[-1]["pid"]
        self.assertNotEqual(pid1, pid2)
        self.assertTrue(self.espera(lambda: self.vivo("motor")))
        cli.send({"op": "reiniciar", "nodo": "motor"})
        self.assertTrue(self.espera(lambda: len(cli.all("estado", nodo="motor", estado="corriendo")) >= 3, 30))
        self.assertNotEqual(cli.all("estado", nodo="motor", estado="corriendo")[-1]["pid"], pid2)

    def test_nodo_desconocido_y_ordenes_malas_son_error_y_la_sesion_sigue(self):
        self.start_server()
        cli, _ = self.sesion()
        # sesion_ok sale ANTES de arrancar los nodos: hay que esperar a que motor esté vivo (en Windows Python
        # tarda en arrancar más de lo que tardan las órdenes de abajo)
        self.assertTrue(self.espera(lambda: self.vivo("motor"), 30), f"motor no arrancó: {cli.msgs}")
        casos = [({"op": "detener", "nodo": "zzz"}, "no tiene el nodo 'zzz'"),
                 ({"op": "arrancar", "nodo": "ia"}, "no tiene el nodo 'ia'"),      # de la laptop
                 ({"op": "reiniciar", "nodo": "x" * 500}, "no tiene el nodo"),
                 ({"op": "arrancar"}, "lleva el nombre del nodo"),
                 ({"op": "arrancar", "nodo": 5}, "lleva el nombre del nodo"),
                 ({"op": "arrancar", "nodo": ["motor"]}, "lleva el nombre del nodo"),
                 ({"op": "baila"}, "orden desconocida 'baila'"),
                 ({"op": "sesion"}, "ya tiene su sesión abierta")]
        for orden, frag in casos:
            with self.subTest(orden=orden):
                antes = len(cli.all("error"))
                cli.send(orden)
                self.assertTrue(cli.wait(lambda ms, a=antes: len([m for m in ms if m["op"] == "error"]) > a))
                self.assertIn(frag, cli.all("error")[-1]["texto"])
        cli.send({"op": "ping", "t": 7})
        self.assertTrue(cli.wait(cli.hay("pong", t=7)))
        self.assertTrue(self.vivo("motor"))
        self.assertEqual(len(self.pids("motor")), 1, "una orden mala no debe reiniciar nada")

    def test_pong_acota_la_t(self):
        self.start_server()
        cli, _ = self.sesion()
        for t, esperado in ((12, 12), (1.5, 1.5), ("corto", "corto"), ("x" * 5000, None), (True, None),
                            ([1, 2], None), ({"a": 1}, None), (None, None), ("y" * 64, "y" * 64), ("z" * 65, None)):
            with self.subTest(t=str(t)[:20]):
                antes = len(cli.all("pong"))
                cli.send({"op": "ping", "t": t})
                self.assertTrue(cli.wait(lambda ms, a=antes: len([m for m in ms if m["op"] == "pong"]) > a))
                self.assertEqual(cli.all("pong")[-1]["t"], esperado)

    def test_detener_todo_y_adios(self):
        self.start_server()
        cli, _ = self.sesion()
        cli.wait(cli.hay("estado", nodo="motor", estado="corriendo"))
        cli.wait(cli.hay("estado", nodo="imu", estado="corriendo"))
        cli.send({"op": "detener_todo"})
        self.assertTrue(cli.wait(cli.hay("estado", nodo="motor", estado="detenido")))
        self.assertTrue(cli.wait(cli.hay("estado", nodo="imu", estado="detenido")))
        self.assertTrue(self.espera(lambda: not self.vivo("motor") and not self.vivo("imu")))
        cli.send({"op": "adios"})
        self.assertTrue(self.espera(lambda: self.server.session is None, 30))
        self.assertTrue(self.espera(lambda: cli.error is not None, 30), "el agente no cerró la conexión")
        # y acepta otra sesión
        cli2, ok = self.sesion()
        self.assertEqual(ok["op"], "sesion_ok")

    def test_no_arrancar_los_arranca_la_laptop_despues(self):
        self.start_server()
        cli, ok = self.sesion(msg=self.mensaje(no_arrancar=["motor"]), arrancar=False)
        self.assertEqual(ok["op"], "sesion_ok")
        self.assertTrue(cli.wait(cli.hay("estado", nodo="imu", estado="corriendo")))
        time.sleep(0.5)
        self.assertFalse((self.marks / "arranque_motor").exists(), "no_arrancar arrancó el nodo")
        self.assertNotEqual(cli.estado("motor"), "corriendo")
        cli.send({"op": "arrancar", "nodo": "motor"})
        self.assertTrue(cli.wait(cli.hay("estado", nodo="motor", estado="corriendo")))
        self.assertTrue(self.espera(lambda: self.vivo("motor")))

    def test_no_arrancar_con_nombre_ajeno_es_protocolo_y_no_arranca_nada(self):
        self.start_server()
        for ajeno in (["ia"], ["motor", "fantasma"], ["x" * 300]):
            with self.subTest(ajeno=[a[:10] for a in ajeno]):
                cli, fin = self.sesion(msg=self.mensaje(no_arrancar=ajeno), arrancar=False)
                self.assertEqual((fin["op"], fin.get("codigo")), ("sesion_error", "protocolo"))
                self.assertIn("no_arrancar", fin["texto"])
                self.assertTrue(self.espera(lambda: self.server.session is None, 30))
        self.sin_arrancar_nada()

    def test_no_arrancar_mal_formado_es_protocolo(self):
        self.start_server()
        for malo in ("motor", {"motor": 1}, [1, 2], None):
            with self.subTest(malo=str(malo)):
                msg = self.mensaje()
                msg["no_arrancar"] = malo
                cli, fin = self.sesion(msg=msg, arrancar=False)
                self.assertEqual(fin.get("codigo"), "protocolo")
                self.assertTrue(self.espera(lambda: self.server.session is None, 30))
        self.sin_arrancar_nada()

    def test_autostart_false_no_arranca_solo(self):
        self.start_server()
        self.write_toml()
        t = self.toml.read_text(encoding="utf-8").replace('machine = "robot"\n\n[[node]]\nname = "imu"',
                                                          'machine = "robot"\nautostart = false\n\n[[node]]\nname = "imu"')
        self.toml.write_text(t, encoding="utf-8")
        cli, ok = self.sesion()
        self.assertEqual(ok["op"], "sesion_ok")
        self.assertTrue(cli.wait(cli.hay("estado", nodo="imu", estado="corriendo")))
        time.sleep(0.5)
        self.assertFalse((self.marks / "arranque_motor").exists())
        self.assertTrue(any("autostart = false" in m["linea"] for m in cli.all("log", nodo="agmen")))
        cli.send({"op": "arrancar", "nodo": "motor"})
        self.assertTrue(cli.wait(cli.hay("estado", nodo="motor", estado="corriendo")))

    def test_primera_orden_que_no_es_sesion(self):
        self.start_server()
        cli = self.conectar()
        cli.send({"op": "ping", "t": 1})
        self.assertTrue(cli.wait(cli.hay("sesion_error", codigo="protocolo")))
        self.assertIn("lo primero es 'sesion'", cli.first("sesion_error")["texto"])
        self.sin_arrancar_nada()

    def test_sesion_con_campos_invalidos_es_protocolo(self):
        self.start_server()
        malos = [{"perfil": 1}, {"reloj": "otro"}, {"agmen": None}, {"maquina": ["robot"]}, {"nodos": []},
                 {"nodos": {"motor": "x"}}, {"nodos": None}, {"perfil": None}]
        for cambio in malos:
            with self.subTest(cambio=cambio):
                cli, fin = self.sesion(msg=self.mensaje(**cambio), arrancar=False)
                self.assertEqual((fin["op"], fin.get("codigo")), ("sesion_error", "protocolo"), fin)
                self.assertTrue(self.espera(lambda: self.server.session is None, 30))
        self.sin_arrancar_nada()

    def test_maquina_que_no_es_la_del_agente(self):
        self.start_server()
        cli, fin = self.sesion(msg=self.mensaje(maquina="laptop"))
        self.assertEqual((fin["op"], fin["codigo"]), ("sesion_error", "maquina"))
        self.assertIn("esta computadora es 'robot' en su robot.toml, no 'laptop'", fin["texto"])
        self.assertTrue(self.espera(lambda: cli.error is not None, 30))
        self.sin_arrancar_nada()

    def test_perfil_que_el_agente_no_tiene(self):
        self.start_server()
        cli, fin = self.sesion(msg=self.mensaje(perfil="nope"))
        self.assertEqual(fin["codigo"], "perfil")
        self.assertIn("no tiene el perfil 'nope' (tiene: real, sim, replay)", fin["texto"])
        self.sin_arrancar_nada()

    def test_distinto_lista_todas_las_diferencias_y_no_arranca_nada(self):
        self.start_server()
        laptop = self.dir / "laptop.toml"
        texto = self.toml.read_text(encoding="utf-8")
        texto = texto.replace('"motor", ' + json.dumps(str(self.marks)) + ']', '"motor", "otra-cosa"]')
        texto = texto.replace('[[node]]\nname = "imu"', '[[node]]\nname = "extra"\ncmd = "python x.py"\n'
                                                       'machine = "robot"\n\n[[node]]\nname = "imuu"')
        laptop.write_text(texto, encoding="utf-8")
        cli, fin = self.sesion(msg=self.mensaje(toml=laptop))
        self.assertEqual((fin["op"], fin["codigo"]), ("sesion_error", "distinto"))
        t = fin["texto"]
        self.assertTrue(t.startswith("robot: "), t)
        for frag in ("el nodo motor es distinto en su robot.toml (cmd)", "el nodo extra no está en su robot.toml",
                     "el nodo imuu no está en su robot.toml", "el nodo imu está en su robot.toml", "Haz git pull en robot"):
            self.assertIn(frag, t)
        self.assertTrue(self.espera(lambda: cli.error is not None, 30))
        self.sin_arrancar_nada()
        self.assertEqual(cli.all("estado"), [])

    def test_el_reloj_distinto_es_una_diferencia_mas(self):
        self.start_server()
        cli, fin = self.sesion(msg=self.mensaje(clock="sim"))
        self.assertEqual(fin.get("codigo"), "distinto")
        self.assertIn("el reloj es distinto (real en su robot.toml, sim en el tuyo)", fin["texto"])
        self.sin_arrancar_nada()

    def test_reloj_replay_se_acepta_y_los_nodos_lo_saben(self):
        self.start_server()
        espia = EspiaPopen()
        with espia:
            cli, ok = self.sesion(msg=self.mensaje(clock="replay"))
            self.assertEqual(ok["op"], "sesion_ok", ok)
            self.assertTrue(cli.wait(cli.hay("estado", nodo="motor", estado="corriendo")))
        for call in espia.de_nodos("motor", "imu"):
            self.assertEqual(call.kwargs["env"].get("AGMEN_CLOCK"), "replay")
            self.assertEqual(call.kwargs["env"].get("AGMEN_CLOCK_SOURCE"), "agmen")

    def test_config_con_error_es_config(self):
        self.start_server()
        msg = self.mensaje()
        self.toml.write_text("esto no es toml [", encoding="utf-8")
        cli, fin = self.sesion(msg=msg)
        self.assertEqual(fin.get("codigo"), "config")
        self.assertIn("robot.toml tiene un error", fin["texto"])
        self.sin_arrancar_nada()

    def test_el_toml_del_agente_ya_no_declara_la_machine(self):
        self.start_server()
        self.toml.write_text(self.toml.read_text(encoding="utf-8").replace('name = "robot"', 'name = "otra"')
                             .replace('machine = "robot"', 'machine = "otra"'), encoding="utf-8")
        cli, fin = self.sesion()
        self.assertEqual(fin.get("codigo"), "maquina")
        self.assertIn("ya no declara [[machine]] 'robot'", fin["texto"])

    def test_el_perfil_con_local_true_en_el_agente(self):
        self.start_server()
        self.write_toml(extra="\n[profile.sim]\nlocal = true\n")
        cli, fin = self.sesion(msg=self.mensaje(perfil="sim"))
        self.assertEqual(fin.get("codigo"), "perfil")
        self.assertIn("local = true", fin["texto"])
        self.sin_arrancar_nada()

    def test_before_ok_su_salida_llega_como_log_antes_de_sesion_ok(self):
        self.start_server()
        t = self.toml.read_text(encoding="utf-8").replace(
            '[robot]\nname = "r"\n', f'[robot]\nname = "r"\nbefore = [{json.dumps("python " + str(self.dir / "before.py") + " 0")}]\n')
        self.toml.write_text(t, encoding="utf-8")
        cli, ok = self.sesion()
        self.assertEqual(ok["op"], "sesion_ok", ok)
        ops = [(m["op"], m.get("linea")) for m in cli.msgs]
        indice_ok = [i for i, (op, _) in enumerate(ops) if op == "sesion_ok"][0]
        salida = [i for i, (op, l) in enumerate(ops) if op == "log" and l and "SALIDA-DEL-BEFORE" in l]
        self.assertTrue(salida and salida[0] < indice_ok, ops)
        self.assertEqual(cli.all("log", nodo="agmen")[0]["n"], 1)

    def test_before_que_falla_es_antes_y_su_salida_llego(self):
        self.start_server()
        t = self.toml.read_text(encoding="utf-8").replace(
            '[robot]\nname = "r"\n', f'[robot]\nname = "r"\nbefore = [{json.dumps("python " + str(self.dir / "before.py") + " 3")}]\n')
        self.toml.write_text(t, encoding="utf-8")
        cli, fin = self.sesion()
        self.assertEqual((fin["op"], fin["codigo"]), ("sesion_error", "antes"))
        self.assertIn("falló [robot] before", fin["texto"])
        self.assertIn("terminó con código 3", fin["texto"])
        self.assertTrue(any("SALIDA-DEL-BEFORE" in m["linea"] for m in cli.all("log")), "su salida no llegó")
        self.sin_arrancar_nada()
        self.assertEqual(cli.all("estado"), [])

    def test_before_que_no_existe_es_antes(self):
        self.start_server()
        t = self.toml.read_text(encoding="utf-8").replace(
            '[robot]\nname = "r"\n', '[robot]\nname = "r"\nbefore = ["programa-que-no-existe-agmen --x"]\n')
        self.toml.write_text(t, encoding="utf-8")
        cli, fin = self.sesion()
        self.assertEqual(fin.get("codigo"), "antes")
        self.assertIn("no se pudo correr", fin["texto"])

    def test_ctrl_c_en_la_laptop_durante_before_lo_corta(self):
        self.start_server()
        (self.dir / "lento.py").write_text("import time\nprint('LENTO', flush=True)\ntime.sleep(60)\n", encoding="utf-8")
        t = self.toml.read_text(encoding="utf-8").replace(
            '[robot]\nname = "r"\n', f'[robot]\nname = "r"\nbefore = [{json.dumps("python " + str(self.dir / "lento.py"))}]\n')
        self.toml.write_text(t, encoding="utf-8")
        cli = self.conectar()
        msg = self.mensaje()
        msg.pop("no_arrancar")
        cli.send(msg)
        self.assertTrue(cli.wait(lambda ms: any(m["op"] == "log" and "LENTO" in m.get("linea", "") for m in ms)))
        cli.send({"op": "detener_todo"})
        cli.send({"op": "adios"})
        self.assertTrue(self.espera(lambda: self.server.session is None, 30), "el before siguió corriendo")
        self.sin_arrancar_nada()

    def test_nodos_ajenos_con_un_hub_que_ya_tiene_nodos(self):
        self.sin_hub = True
        import agmen
        from agmen import hub as hubmod
        from agmen import wire

        hub = hubmod.Hub(f"127.0.0.1:{self.hub_port}", log=lambda t: None)
        parar = threading.Event()
        threading.Thread(target=hub.serve, args=(parar,), daemon=True).start()
        self.addCleanup(parar.set)
        pub = agmen.Publisher(wire.ImuSample, "/ajeno")
        self.addCleanup(pub.close)
        from agmen.hub import query
        self.assertTrue(self.espera(lambda: any(op in ("ep", "stat") for op, _ in
                                                query(f"127.0.0.1:{self.hub_port}", timeout=1.0)), 10), "el hub no ve al publicador")
        self.start_server()
        cli, fin = self.sesion()
        self.assertEqual((fin["op"], fin["codigo"]), ("sesion_error", "nodos_ajenos"))
        self.assertIn("ya corren nodos que este agente no lanzó", fin["texto"])
        self.sin_arrancar_nada()

    def test_un_hub_que_ocupa_el_puerto_y_no_contesta_es_rechazo(self):
        self.sin_hub = True
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", self.hub_port))
        self.peers.append(s)
        self.start_server()
        cli, fin = self.sesion()
        self.assertEqual((fin["op"], fin["codigo"]), ("sesion_error", "nodos_ajenos"))
        self.assertIn("no contesta", fin["texto"])
        self.sin_arrancar_nada()

    def test_un_hub_vacio_ajeno_se_reusa_y_se_avisa_a_la_laptop(self):
        self.sin_hub = True
        from agmen import hub as hubmod

        # config con hub: el agente lo reusa si ya hay uno vivo sin nodos
        hub = hubmod.Hub(f"127.0.0.1:{self.hub_port}", log=lambda t: None)
        parar = threading.Event()
        threading.Thread(target=hub.serve, args=(parar,), daemon=True).start()
        self.addCleanup(parar.set)
        self.start_server()
        t = self.toml.read_text(encoding="utf-8").replace("[hub]\nenabled = false", "[hub]\nenabled = true")
        self.toml.write_text(t, encoding="utf-8")
        cli, ok = self.sesion()
        self.assertEqual(ok["op"], "sesion_ok", ok)
        self.assertTrue(cli.wait(lambda ms: any("ya corría un agmen hub sin nodos: lo reuso" in m.get("linea", "")
                                               for m in ms if m["op"] == "log")))

    def test_hub_propio_arranca_y_se_detiene_con_la_sesion(self):
        self.sin_hub = True
        self.start_server()
        t = self.toml.read_text(encoding="utf-8").replace("[hub]\nenabled = false", "[hub]\nenabled = true")
        self.toml.write_text(t, encoding="utf-8")
        cli, ok = self.sesion()
        self.assertEqual(ok["op"], "sesion_ok", ok)
        self.assertEqual(ok["nodos"], ["motor", "imu"], "el hub no va en nodos")
        self.assertTrue(cli.wait(lambda ms: any(m["op"] == "log" and m["nodo"] == "agmen" and m["linea"].startswith("hub: ")
                                               for m in ms)), "las líneas del hub llegan como log de 'agmen' con 'hub: '")
        self.assertEqual([m for m in cli.all("estado") if m["nodo"] == "hub"], [], "el hub no manda estado")
        from agmen.hub import ping
        self.assertTrue(self.espera(lambda: ping(f"127.0.0.1:{self.hub_port}", timeout=0.5) is not None, 30))
        cli.send({"op": "adios"})
        self.assertTrue(self.espera(lambda: ping(f"127.0.0.1:{self.hub_port}", timeout=0.3) is None, 30),
                        "el hub del agente siguió vivo")

    def test_version_de_agmen_distinta_se_avisa_y_se_acepta(self):
        self.start_server()
        cli, ok = self.sesion(msg=self.mensaje(agmen="0.0.1"))
        self.assertEqual(ok["op"], "sesion_ok")
        self.assertTrue(any("la laptop usa Agmen 0.0.1" in t for t, _ in self.said), self.said)


class EspiaPopen:
    """Anota cada subprocess.Popen (y deja que se haga de verdad)."""

    def __init__(self):
        self.calls: list = []
        self._orig = subprocess.Popen

    def __enter__(self):
        espia, orig = self, self._orig

        class P(orig):
            def __init__(self, *a, **k):
                espia.calls.append(mock.call(*a, **k))
                super().__init__(*a, **k)

        self._p = mock.patch("subprocess.Popen", P)
        self._p.start()
        return self

    def __exit__(self, *a):
        self._p.stop()

    def de_nodos(self, *nombres):
        out = []
        for c in self.calls:
            args = c.args[0] if c.args else c.kwargs.get("args")
            if isinstance(args, (list, tuple)) and len(args) > 2 and str(args[-2]) in nombres:
                out.append(c)
        return out


class TestD7NadaDelClienteLlegaAUnComando(ServerCase):
    def test_los_argumentos_de_subprocess_salen_solo_del_robot_toml_del_agente(self):
        self.start_server()
        hostil = self.mensaje()
        hostil.pop("no_arrancar")
        hostil.update({"cmd": ["evil", "--rm"], "env": {"EVIL": "1", "PATH": "/evil"}, "cwd": "/tmp/evil",
                       "comando": "rm -rf /", "shell": True, "antes": ["evil"], "before": ["evil"]})
        hostil["nodos"]["motor"].update({"extra": "evil", "shell": True, "args": ["evil"], "machine": "otra"})
        espia = EspiaPopen()
        with espia:
            cli = self.conectar()
            cli.send(hostil)
            self.assertTrue(cli.wait(cli.hay("estado", nodo="motor", estado="corriendo")), cli.msgs)
            cli.wait(cli.hay("estado", nodo="imu", estado="corriendo"))
            # y órdenes con campos extra
            cli.send({"op": "reiniciar", "nodo": "motor", "cmd": ["evil"], "env": {"EVIL": "1"}, "cwd": "/tmp/evil"})
            cli.send({"op": "detener", "nodo": "imu", "cmd": ["evil"], "extra": "x"})
            cli.send({"op": "arrancar", "nodo": "imu", "cmd": ["evil"], "env": {"EVIL": "2"}})
            self.assertTrue(self.espera(lambda: len(cli.all("estado", nodo="imu", estado="corriendo")) >= 2, 30))
        nodo = str(self.dir / "nodo.py")
        con_cmd_real = 0
        for call in espia.calls:
            args = call.args[0] if call.args else call.kwargs.get("args")
            texto = json.dumps([str(a) for a in args]) + json.dumps({k: str(v) for k, v in call.kwargs.items()
                                                                     if k != "env"})
            self.assertNotIn("evil", texto)
            env = call.kwargs.get("env") or {}
            self.assertNotIn("EVIL", env)
            self.assertNotEqual(env.get("PATH"), "/evil")
            self.assertNotIn(str(call.kwargs.get("cwd")), ("/tmp/evil",))
            if args[0] == sys.executable and nodo in args:
                con_cmd_real += 1
                self.assertEqual(args[1], nodo)
                self.assertEqual(args[3], str(self.marks))
                # load_config resuelve la ruta del robot.toml: en Windows sale el nombre largo
                # (runneradmin) aunque la carpeta temporal se escriba con el corto 8.3 (RUNNER~1)
                self.assertEqual(Path(call.kwargs["cwd"]), self.dir.resolve())
        self.assertGreaterEqual(con_cmd_real, 4, "no se vieron los arranques de los nodos")

    def test_el_nodo_distinto_no_llega_a_popen(self):
        self.start_server()
        laptop = self.dir / "laptop.toml"
        laptop.write_text(self.toml.read_text(encoding="utf-8").replace(
            '["python", ' + json.dumps(str(self.dir / "nodo.py")) + ', "motor"',
            '["evil", ' + json.dumps(str(self.dir / "nodo.py")) + ', "motor"'), encoding="utf-8")
        espia = EspiaPopen()
        with espia:
            cli, fin = self.sesion(msg=self.mensaje(toml=laptop))
        self.assertEqual(fin.get("codigo"), "distinto")
        self.assertEqual(espia.calls, [], "se lanzó un proceso con una sesión rechazada")

    def test_un_nombre_de_nodo_con_caracteres_raros_no_llega_a_la_terminal_sin_limpiar(self):
        self.start_server()
        msg = self.mensaje()
        msg["nodos"]["\x1b[2Jfalso\nagmen agent │ clave: ZZZZ"] = {"cmd": "x"}
        cli, fin = self.sesion(msg=msg)
        self.assertEqual(fin.get("codigo"), "distinto")
        for texto, _ in self.said:
            self.assertNotIn("\x1b", texto)
        self.assertEqual(fin["texto"].count("\n"), 0, "el texto del rechazo debe ir en una sola línea")


class TestLimitesDelSaludo(ServerCase):
    MALA = None

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(agent, "FAIL_PAUSE", 0.0),):
            p.start()
            self.addCleanup(p.stop)
        self.mala = derive_key("aaaa-bbbb-cccc")

    def saludo(self, key: bytes, source: str | None = None, keep: bool = False):
        """Un saludo completo desde `source`. Devuelve el Channel (o levanta HandshakeError)."""
        s = socket.socket()
        if source:
            s.bind((source, 0))
        s.settimeout(15)
        s.connect(("127.0.0.1", self.port))
        try:
            ch = client_handshake(s, key, timeout=30.0)
        except BaseException:
            s.close()
            raise
        if keep:
            self.peers.append(s)
            return ch
        ch.close()
        return ch

    def sesion_aceptada_desde(self, ip: str) -> None:
        """Una sesión que el agente ACEPTA (sesion_ok) desde `ip`, y se despide: esa IP queda como la de la
        última sesión aceptada."""
        cli, ok = self.sesion(self.conectar(source=ip))
        self.assertEqual(ok["op"], "sesion_ok", ok)
        cli.send({"op": "adios"})
        self.assertTrue(self.espera(lambda: self.server.session is None, 30))

    def test_cinco_malas_por_ip_y_la_sexta_es_espera_aunque_traiga_la_buena(self):
        self.start_server()
        for i in range(5):
            with self.assertRaises(HandshakeError) as ctx:
                self.saludo(self.mala)
            self.assertEqual(ctx.exception.code, "clave", i)
        for key, nombre in ((self.mala, "mala"), (K(), "buena")):
            with self.subTest(sexta=nombre):
                with self.assertRaises(HandshakeError) as ctx:
                    self.saludo(key)
                self.assertEqual(ctx.exception.code, "espera")
                self.assertNotEqual(ctx.exception.code, "clave", "espera no es clave mala")
                self.assertIn("espera", str(ctx.exception))
                self.assertRegex(str(ctx.exception), r"espera \d+ s")

    @unittest.skipUnless(source_ip_available("127.0.0.2"), "necesita 127.0.0.2 para ser otra IP")
    def test_otra_ip_con_la_buena_entra_aunque_la_primera_este_bloqueada(self):
        self.start_server()
        for _ in range(5):
            with self.assertRaises(HandshakeError):
                self.saludo(self.mala)
        with self.assertRaises(HandshakeError) as ctx:
            self.saludo(K())
        self.assertEqual(ctx.exception.code, "espera")
        ch = self.saludo(K(), source="127.0.0.2")
        self.assertIsInstance(ch, Channel)

    def test_la_pausa_de_un_segundo_vale_en_paralelo(self):
        with mock.patch.object(agent, "FAIL_PAUSE", 0.6):
            self.start_server()
            resultados, barrera = [], threading.Barrier(4)

            def intento():
                barrera.wait()
                t0 = time.monotonic()
                try:
                    self.saludo(self.mala)
                    resultados.append(("ok", t0, time.monotonic()))
                except HandshakeError as e:
                    resultados.append((e.code, t0, time.monotonic()))

            hilos = [threading.Thread(target=intento) for _ in range(4)]
            t0 = time.monotonic()
            for h in hilos:
                h.start()
            for h in hilos:
                h.join(30)
            total = time.monotonic() - t0
        self.assertEqual(sorted(c for c, _, _ in resultados), ["clave"] * 4)
        # cuatro pruebas malas simultáneas desde la misma IP no se revisan a la vez: se espacian ≥ ~0.6 s
        self.assertGreater(total, 0.6 * 3 - 0.3, f"las 4 pruebas en paralelo tardaron {total:.2f} s: no se espaciaron")

    def test_seis_pruebas_malas_en_paralelo_no_se_saltan_el_tope(self):
        self.start_server()
        codigos, barrera = [], threading.Barrier(8)

        def intento():
            barrera.wait()
            try:
                self.saludo(self.mala)
                codigos.append("ok")
            except HandshakeError as e:
                codigos.append(e.code)

        hilos = [threading.Thread(target=intento) for _ in range(8)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(30)
        # 8 pruebas malas a la vez desde una IP: nunca se revisan más de 5 (el tope por minuto), ninguna entra, y
        # el resto recibe espera (por el tope de pruebas, o porque no caben más de 4 conexiones sin saludar)
        self.assertEqual(len(codigos), 8, codigos)
        self.assertLessEqual(codigos.count("clave"), 5, codigos)
        self.assertGreaterEqual(codigos.count("clave"), 4, codigos)
        self.assertEqual(codigos.count("clave") + codigos.count("espera"), 8, codigos)

    def test_tope_global_con_la_ip_de_la_ultima_sesion_aceptada(self):
        ips = ["127.0.0.2", "127.0.0.3", "127.0.0.4", "127.0.0.9"]
        if not all(source_ip_available(ip) for ip in ips):
            self.skipTest("necesita 127.0.0.2, .3, .4 y .9 (Linux): en Windows y macOS pueden no existir")
        with mock.patch.object(agent, "MAX_FAILS_TOTAL", 10):
            self.start_server()
            self.sesion_aceptada_desde("127.0.0.9")           # la IP de la última sesión aceptada
            for ip in ("127.0.0.2", "127.0.0.3"):
                for _ in range(5):
                    with self.assertRaises(HandshakeError) as ctx:
                        self.saludo(self.mala, source=ip)
                    self.assertEqual(ctx.exception.code, "clave")
            # ya van 10 en el último minuto: otra IP, aun con la buena, espera
            with self.assertRaises(HandshakeError) as ctx:
                self.saludo(K(), source="127.0.0.4")
            self.assertEqual(ctx.exception.code, "espera")
            self.assertIn("varias IP", str(ctx.exception))
            # la IP de la última sesión aceptada conserva su presupuesto
            self.assertIsInstance(self.saludo(K(), source="127.0.0.9"), Channel)
            # pero el suyo por IP sigue valiendo (5 malas y espera)
            for _ in range(5):
                with self.assertRaises(HandshakeError):
                    self.saludo(self.mala, source="127.0.0.9")
            with self.assertRaises(HandshakeError) as ctx:
                self.saludo(K(), source="127.0.0.9")
            self.assertEqual(ctx.exception.code, "espera")

    def test_solo_el_saludo_no_hace_a_una_ip_de_confianza(self):
        # la confianza se gana al ACEPTAR la sesión (sesion_ok), no con completar el saludo
        if not all(source_ip_available(ip) for ip in ("127.0.0.2", "127.0.0.4")):
            self.skipTest("necesita 127.0.0.2 y .4")
        with mock.patch.object(agent, "MAX_FAILS_TOTAL", 5):
            self.start_server()
            self.saludo(K(), source="127.0.0.4")          # solo el saludo
            for _ in range(5):
                with self.assertRaises(HandshakeError):
                    self.saludo(self.mala, source="127.0.0.2")
            with self.assertRaises(HandshakeError) as ctx:
                self.saludo(K(), source="127.0.0.4")
            self.assertEqual(ctx.exception.code, "espera")

    def test_una_prueba_buena_no_gasta_presupuesto(self):
        self.start_server()
        for _ in range(12):
            self.saludo(K())
        with self.assertRaises(HandshakeError) as ctx:
            self.saludo(self.mala)
        self.assertEqual(ctx.exception.code, "clave")

    def test_cuatro_conexiones_mudas_por_ip_y_la_quinta_es_espera(self):
        self.start_server()
        mudas = []
        for _ in range(agent.MAX_PENDING_IP):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            self.peers.append(s)
            mudas.append(s)
        self.assertTrue(self.espera(lambda: self.server._pending_total == agent.MAX_PENDING_IP, 30))
        quinta = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.peers.append(quinta)
        linea = Peer(quinta).line(5)
        self.assertTrue(linea.startswith("error espera demasiadas conexiones"), linea)
        self.assertTrue(Peer(quinta).closed(3), "debe cerrar después del error")
        # al cerrar las mudas, otra vez caben
        for s in mudas:
            s.close()
        self.assertTrue(self.espera(lambda: self.server._pending_total == 0, 30), self.server._pending_total)
        self.assertIsInstance(self.saludo(K()), Channel)

    def test_la_rechazada_que_ya_saludo_recibe_su_error_y_un_cierre_normal_no_un_rst(self):
        # La quinta manda su hola (como agmen run) y el agente la rechaza sin leerlo. Si cerrara así, con datos
        # sin leer, el sistema mandaría RST; en Windows el RST borra lo que agmen run aún no leía y la línea de
        # error se pierde ("se cerró la conexión" en vez de "espera"). En Linux el RST no borra la línea, pero se
        # ve: tras ella, recv da ConnectionResetError en vez de fin de conexión (b"").
        self.start_server()
        for _ in range(agent.MAX_PENDING_IP):
            self.peers.append(socket.create_connection(("127.0.0.1", self.port), timeout=5))
        self.assertTrue(self.espera(lambda: self.server._pending_total == agent.MAX_PENDING_IP, 30))
        for intento in range(3):
            with self.subTest(intento=intento):
                quinta = socket.create_connection(("127.0.0.1", self.port), timeout=5)
                self.peers.append(quinta)
                quinta.sendall(f"{agent.PROTOCOL} hola {'a' * 32}\n".encode())
                time.sleep(0.2)                     # que el agente la rechace con el hola ya llegado
                recibido = b""
                try:
                    while not recibido.endswith(b"\n"):
                        dato = quinta.recv(4096)
                        if not dato:
                            break
                        recibido += dato
                    fin = quinta.recv(4096)
                except ConnectionResetError:
                    self.fail(f"el agente cerró con RST (llegó {recibido!r}): en Windows la línea de error se pierde")
                self.assertTrue(recibido.startswith(b"error espera demasiadas conexiones"), recibido)
                self.assertEqual(fin, b"", "después del error, el fin normal de la conexión")
                quinta.close()

    @unittest.skipUnless(source_ip_available("127.0.0.2"), "necesita 127.0.0.2 para ser otra IP")
    def test_las_mudas_de_una_ip_no_bloquean_a_otra(self):
        self.start_server()
        for _ in range(agent.MAX_PENDING_IP):
            self.peers.append(socket.create_connection(("127.0.0.1", self.port), timeout=5))
        self.assertTrue(self.espera(lambda: self.server._pending_total == agent.MAX_PENDING_IP, 30))
        self.assertIsInstance(self.saludo(K(), source="127.0.0.2"), Channel)

    def test_tope_total_de_mudas_con_una_reservada_para_la_ip_de_confianza(self):
        if not all(source_ip_available(ip) for ip in ("127.0.0.2", "127.0.0.3")):
            self.skipTest("necesita 127.0.0.2 y .3")
        with mock.patch.object(agent, "MAX_PENDING_TOTAL", 6):
            self.start_server()
            self.sesion_aceptada_desde("127.0.0.3")    # 127.0.0.3 = la última sesión aceptada
            for _ in range(4):
                self.peers.append(socket.create_connection(("127.0.0.1", self.port), timeout=5))
            s = socket.socket()
            s.bind(("127.0.0.2", 0))
            s.connect(("127.0.0.1", self.port))
            self.peers.append(s)                       # la 5.ª: cabe (el tope sin confianza es 6 - 1 = 5)
            self.assertTrue(self.espera(lambda: self.server._pending_total == 5, 30))
            sexta = socket.socket()
            sexta.bind(("127.0.0.2", 0))
            sexta.connect(("127.0.0.1", self.port))
            self.peers.append(sexta)
            self.assertTrue(Peer(sexta).line(5).startswith("error espera demasiadas conexiones"))
            # la IP de confianza sí entra por la plaza reservada
            self.assertIsInstance(self.saludo(K(), source="127.0.0.3"), Channel)

    def test_una_segunda_laptop_con_la_clave_que_recibe_ocupado_no_le_quita_la_reserva_a_la_conectada(self):
        if not all(source_ip_available(ip) for ip in ("127.0.0.2", "127.0.0.3")):
            self.skipTest("necesita 127.0.0.2 y .3")
        with mock.patch.object(agent, "MAX_PENDING_TOTAL", 6):
            self.start_server()
            conectada, ok = self.sesion(self.conectar(source="127.0.0.2"))      # A: sesión aceptada y abierta
            self.assertEqual(ok["op"], "sesion_ok")
            self.assertEqual(self.server.trusted, "127.0.0.2")
            otra = self.conectar(source="127.0.0.3")                             # B: tiene la clave, recibe ocupado
            otra.send(self.mensaje())
            self.assertTrue(otra.wait(otra.hay("sesion_error", codigo="ocupado"), 30))
            self.assertEqual(self.server.trusted, "127.0.0.2", "ocupado no debe darle la reserva a B")
            otra.close()
            self.assertTrue(self.espera(lambda: self.server._pending_total == 0, 30))
            for _ in range(4):                                                   # 4 mudas (de 127.0.0.1)
                self.peers.append(socket.create_connection(("127.0.0.1", self.port), timeout=5))
            s = socket.socket()
            s.bind(("127.0.0.4", 0)) if source_ip_available("127.0.0.4") else s.bind(("127.0.0.1", 0))
            s.connect(("127.0.0.1", self.port))
            self.peers.append(s)                                                 # la 5.ª
            self.assertTrue(self.espera(lambda: self.server._pending_total == 5, 30))
            # B (con la clave, pero sin reserva) ya no cabe; A (la conectada) sí, por su plaza reservada
            b = socket.socket()
            b.bind(("127.0.0.3", 0))
            b.connect(("127.0.0.1", self.port))
            self.peers.append(b)
            self.assertTrue(Peer(b).line(10).startswith("error espera demasiadas conexiones"))
            self.assertIsInstance(self.saludo(K(), source="127.0.0.2"), Channel)

    def test_avisos_de_la_terminal_uno_por_ip_por_minuto_y_sin_caracteres_de_control(self):
        self.start_server()
        for _ in range(8):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.sendall(b"\x1b]0;pwn\x07GET /\x1b[2J" + b"A" * 300 + b"\n")
            s.close()
        for _ in range(8):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.sendall(("agmen-agent/9\x1b[31m" + "Z" * 400 + " hola " + "a" * 32 + "\n").encode())
            s.close()
        time.sleep(0.8)
        textos = [t for t, _ in self.said]
        for t in textos:
            for c in t:
                self.assertTrue(c.isprintable() or c == "\n", repr(t))
        basura = [t for t in textos if "no habla agmen-agent" in t]
        self.assertLessEqual(len(basura), 1, "un aviso por IP por minuto")
        versiones = [t for t in textos if "rechacé a 127.0.0.1" in t and "agmen-agent" in t]
        self.assertLessEqual(len(versiones), 1)
        for t in textos:
            self.assertLess(len(t), 700, "el texto ajeno debe ir cortado")
            self.assertNotIn(KEY, t)

    def test_los_avisos_totales_tienen_tope_por_minuto(self):
        if not all(source_ip_available(f"127.0.0.{n}") for n in range(2, 8)):
            self.skipTest("necesita varias 127.0.0.N")
        with mock.patch.object(agent, "NOTES_PER_MINUTE", 3):
            self.start_server()
            for n in range(1, 8):
                s = socket.socket()
                s.bind((f"127.0.0.{n}", 0))
                s.connect(("127.0.0.1", self.port))
                s.sendall(b"basura\n")
                s.close()
            time.sleep(1.0)
            avisos = [t for t, _ in self.said if "no habla agmen-agent" in t]
            self.assertLessEqual(len(avisos), 3, avisos)

    def test_la_clave_mala_se_anota_sin_la_clave(self):
        self.start_server()
        with self.assertRaises(HandshakeError):
            self.saludo(self.mala)
        self.assertTrue(self.espera(lambda: any("su clave no es la de este agente" in t for t, _ in self.said), 30), self.said)
        for t, _ in self.said:
            self.assertNotIn("aaaa-bbbb-cccc", t)
            self.assertNotIn(KEY, t)

    def test_tablas_por_ip_con_tope(self):
        self.start_server()
        with mock.patch.object(agent, "MAX_TRACKED_IPS", 8):
            for n in range(40):
                with self.server.lock:
                    self.server._rec(f"10.0.{n // 250}.{n % 250}")
            self.assertLessEqual(len(self.server._ips), 9)


class TestCortes(ServerCase):
    def abrir(self, **kw):
        self.start_server(**kw)
        cli, ok = self.sesion()
        self.assertEqual(ok["op"], "sesion_ok", ok)
        self.assertTrue(cli.wait(cli.hay("estado", nodo="motor", estado="corriendo")))
        self.assertTrue(cli.wait(cli.hay("estado", nodo="imu", estado="corriendo")))
        self.assertTrue(self.espera(lambda: self.vivo("motor") and self.vivo("imu")))
        return cli

    def comprueba_fin(self, nombre: str, nueva: bool = True):
        self.assertTrue(self.espera(lambda: not self.vivo("motor") and not self.vivo("imu"), 30),
                        f"{nombre}: los nodos siguen vivos")
        self.assertTrue(self.espera(lambda: self.server.session is None, 30), f"{nombre}: el agente sigue ocupado")
        if not nueva:
            return
        cli2, ok = self.sesion()          # el agente acepta otra sesión
        self.assertEqual(ok["op"], "sesion_ok", f"{nombre}: {ok}")
        self.assertTrue(cli2.wait(cli2.hay("estado", nodo="motor", estado="corriendo")))
        cli2.send({"op": "adios"})        # y se despide: el agente queda libre para la siguiente vuelta
        self.assertTrue(self.espera(lambda: self.server.session is None, 30))

    def test_la_laptop_cierra_la_conexion(self):
        cli = self.abrir()
        cli.ch.sock.shutdown(socket.SHUT_RDWR)
        cli.close()
        self.comprueba_fin("cierre")
        self.assertTrue(any("cerró la conexión sin despedirse" in t for t, _ in self.said), self.said)

    def test_la_laptop_se_despide(self):
        cli = self.abrir()
        cli.send({"op": "adios"})
        self.comprueba_fin("adios")
        self.assertTrue(any("se despidió" in t for t, _ in self.said))

    def test_silencio_de_la_laptop(self):
        with mock.patch.object(agent, "SILENCE_TIMEOUT", 1.0):
            cli = self.abrir()          # (esta laptop no manda ping: se queda callada)
            t0 = time.monotonic()
            self.assertTrue(self.espera(lambda: not self.vivo("motor"), 30))
            self.assertLess(time.monotonic() - t0, 30)
            self.comprueba_fin("silencio")
        self.assertTrue(any("no dijo nada en 1 s" in t for t, _ in self.said), self.said)

    def test_un_ping_mantiene_viva_la_sesion(self):
        # el silencio vence a los 2.5 s; con un ping cada 0.5 s la sesión sigue viva el doble de eso
        with mock.patch.object(agent, "SILENCE_TIMEOUT", 2.5):
            cli = self.abrir()
            fin = time.monotonic() + 5.0
            i = 0
            while time.monotonic() < fin:
                cli.send({"op": "ping", "t": i})
                i += 1
                time.sleep(0.5)
            self.assertTrue(self.vivo("motor"))
            self.assertIsNotNone(self.server.session)

    def test_mensajes_malos_cierran_la_conexion_y_detienen_los_nodos(self):
        # (cada causa por separado, en TestCanal; aquí que el AGENTE cierre y limpie ante cada familia)
        casos = {
            "firma mala": lambda c, n: f'{n} {{"op":"ping"}} {"0" * 64}\n'.encode(),
            "n repetido": lambda c, n: ref_line(c.ch._key, "c", n - 1, '{"op":"ping"}'),
            "anidado": lambda c, n: ref_line(c.ch._key, "c", n, "[" * 30000 + "]" * 30000),
            "mas de 64 KiB": lambda c, n: ref_line(c.ch._key, "c", n, '{"op":"x","d":"' + "a" * 70000 + '"}'),
        }
        for nombre, hacer in casos.items():
            with self.subTest(caso=nombre):
                cli = self.abrir() if self.server is None else self._reabrir()
                n = cli.ch._sent + 1
                cli.ch.sock.sendall(hacer(cli, n))
                self.comprueba_fin(nombre, nueva=nombre == "mas de 64 KiB")
                self.assertTrue(any("mensaje inválido de la laptop" in t for t, _ in self.said), (nombre, self.said))
                self.said.clear()

    def _reabrir(self):
        cli, ok = self.sesion()
        self.assertEqual(ok["op"], "sesion_ok", ok)
        self.assertTrue(cli.wait(cli.hay("estado", nodo="motor", estado="corriendo")))
        self.assertTrue(self.espera(lambda: self.vivo("motor") and self.vivo("imu")))
        return cli

    def test_ctrl_c_en_el_agente_detiene_todo_y_cierra_la_conexion(self):
        cli = self.abrir()
        t0 = time.monotonic()
        self.server.close()
        self.assertLess(time.monotonic() - t0, 20)
        self.assertTrue(self.espera(lambda: not self.vivo("motor") and not self.vivo("imu"), 30))
        self.assertTrue(self.espera(lambda: cli.error is not None, 30), "la laptop no se enteró")
        self.assertTrue(any("Ctrl+C en el agente" in t for t, _ in self.said))

    def test_una_sesion_nueva_mientras_la_anterior_se_despide_espera_en_vez_de_ocupado(self):
        cli = self.abrir()
        primera = self.server.session
        cli.send({"op": "detener_todo"})
        cli.send({"op": "adios"})
        # (si la nueva llegara ANTES de que el agente procese el adios, sería ocupado con razón: se espera a que se despida)
        self.assertTrue(self.espera(lambda: primera._ending or primera.done.is_set(), 30))
        cli2 = self.conectar()
        cli2.send(self.mensaje())
        self.assertTrue(cli2.wait(lambda ms: any(m["op"] in ("sesion_ok", "sesion_error") for m in ms), 60))
        self.assertIsNotNone(cli2.first("sesion_ok"), cli2.msgs)

    def test_ocupado_dice_desde_que_ip_y_no_toca_la_sesion(self):
        cli = self.abrir()
        otra = self.conectar()
        otra.send(self.mensaje())
        self.assertTrue(otra.wait(otra.hay("sesion_error")))
        e = otra.first("sesion_error")
        self.assertEqual(e["codigo"], "ocupado")
        self.assertIn("127.0.0.1", e["texto"])
        self.assertIn("una laptop a la vez", e["texto"])
        self.assertTrue(self.vivo("motor"))
        self.assertEqual(len(self.pids("motor")), 1, "el rechazo no debe reiniciar nada")
        cli.send({"op": "ping", "t": 1})
        self.assertTrue(cli.wait(cli.hay("pong", t=1)))


def _pila_corta(frame) -> str:
    """El tope de la pila de un hilo, resumido: 'función:línea<llamadora' (para saber en qué está parado)."""
    try:
        partes = []
        while frame is not None and len(partes) < 2:
            partes.append(f"{frame.f_code.co_name}:{frame.f_lineno}")
            frame = frame.f_back
        return "<".join(partes)
    except Exception:
        return "?"


def _tcp_resumen(sock) -> str:
    """Diagnóstico de un socket (solo Linux; donde no se pueda, '?'): bytes sin leer / sin salir y TCP_INFO."""
    try:
        import fcntl
        import termios
        fd = sock.fileno()
        buf = bytearray(4)
        fcntl.ioctl(fd, termios.FIONREAD, buf)
        sin_leer = int.from_bytes(buf, sys.byteorder, signed=True)
        buf = bytearray(4)
        fcntl.ioctl(fd, termios.TIOCOUTQ, buf)
        sin_salir = int.from_bytes(buf, sys.byteorder, signed=True)
    except Exception:
        sin_leer = sin_salir = "?"
    try:
        info = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 256)
        import struct
        estado, _ca, retrans_p, probes, backoff = struct.unpack_from("5B", info, 0)
        if len(info) >= 104:
            (rto, _ato, _mss, _rmss, unacked, _sacked, _lost, retrans, _f, _ds, _as, _dr, _ar, _pmtu, rcv_ssthresh,
             _rtt, _rttvar, _ssthresh, cwnd, _adv, _reord, _rcv_rtt, rcv_space, tot) = struct.unpack_from("24I", info, 8)
            tcp = (f"est={estado} rto={rto // 1000}ms bo={backoff} pr={probes} unack={unacked} retr={retrans}/{tot} "
                   f"cwnd={cwnd} rcvsp={rcv_space} rcvss={rcv_ssthresh}")
        else:
            tcp = f"est={estado} bo={backoff} pr={probes}"
    except Exception:
        tcp = "?"
    return f"in={sin_leer} out={sin_salir} {tcp}"


def _sysctl(nombre: str) -> str:
    try:
        with open("/proc/sys/" + nombre.replace(".", "/")) as f:
            return " ".join(f.read().split())
    except Exception:
        return "?"



class TestClienteQueNoLee(ServerCase):
    def abrir_sin_leer(self, rcvbuf: int | None = 4096):
        """Una laptop que abre la sesión y luego NO lee nada más (su buffer de recepción es mínimo; con
        `rcvbuf=None`, el de siempre: para las pruebas en que la laptop además no para de mandar)."""
        self.start_server()
        s = socket.socket()
        if rcvbuf is not None:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        s.settimeout(5)
        s.connect(("127.0.0.1", self.port))
        ch = client_handshake(s, K())
        cli = Cli(ch, leer=False)
        self.clients.append(cli)
        return cli

    def leer_hasta_sesion_ok(self, cli):
        while True:
            m = cli.ch.recv(10)
            cli.msgs.append(m)
            if m["op"] == "sesion_ok":
                return

    @staticmethod
    def cola(sesion) -> int:
        """Cuántos mensajes esperan salir hacia la laptop (sirve con la cola de hoy y con la de antes)."""
        out = sesion.out
        return out.qsize() if hasattr(out, "qsize") else len(out.items)

    def test_un_cliente_lento_no_congela_al_agente_ni_a_los_nodos(self):
        # B1 de la revisión: con una laptop que no lee y más de 5000 mensajes en cola, el agente se trababa contra
        # sí mismo (un candado no reentrante tomado dos veces al descartar logs) y desde ahí ningún nodo podía
        # escribir ni detenerse. Aquí se inunda de logs una sesión cuya laptop no lee y se comprueba que quien
        # escribe (el hilo del nodo) NUNCA espera, que los candados quedan libres y que el agente sigue contestando.
        self.abrir_sin_leer_sesion()
        sesion = self.server.session
        launcher = sesion.launcher
        hecho = threading.Event()

        def inundar():
            for i in range(300):                       # líneas grandes: llenan los buffers y bloquean al que manda
                launcher.logs.add("motor", f"{i:04d} " + "x" * 7000)
            for i in range(9000):                      # y luego muchas chicas: la cola pasa de 5000 mensajes
                launcher.logs.add("motor", f"corto {i}")
            hecho.set()

        hilo = threading.Thread(target=inundar, daemon=True)
        t0 = time.monotonic()
        hilo.start()
        self.assertTrue(hecho.wait(25), "quien escribe los logs quedó esperando a la laptop: el agente se trabó")
        self.assertLess(time.monotonic() - t0, 25)
        nodo = launcher.nodes["motor"]
        for lock in (nodo.lock, self.server.lock):
            self.assertTrue(lock.acquire(timeout=2), "un candado quedó tomado esperando a la laptop")
            lock.release()
        # el agente sigue atendiendo: otra laptop recibe "ocupado" enseguida
        otra = self.conectar()
        otra.send(self.mensaje())
        self.assertTrue(otra.wait(otra.hay("sesion_error", codigo="ocupado"), 30), "el agente no contesta")
        # y la cola de salida tiene tope
        self.assertLessEqual(self.cola(sesion), agent.OUT_MAX_MSGS + 10)
        if hasattr(sesion.out, "bytes"):
            self.assertLessEqual(sesion.out.bytes, agent.OUT_MAX_BYTES + 20_000)
            self.assertGreater(sesion.out.dropped, 0, "tantos logs debieron descartar algunos")

    def abrir_sin_leer_mandando(self):
        """Una laptop que no lee PERO sigue mandando; devuelve (cli, sesion). El agente debe llegar a "no lee".

        El CI falló varias veces con "no dijo nada en 6 s" en vez de "no lee", y la línea de tiempo dijo por qué:
        con el buffer de recepción de 4096 bytes de `abrir_sin_leer`, lo que mandaba la laptop se atascaba en su
        propio kernel (1.7 MB en cola, sondeando una ventana que creía cerrada) mientras el agente ya había leído
        todo y tenía sitio; sus respuestas cabían enteras en su buffer de envío (que el kernel agranda solo, hasta
        4 MB) y, sin más pings, quien envía nunca se bloqueaba: el agente veía silencio, y con razón. Aquí la
        laptop usa su buffer de siempre (su ventana se cierra sin atascar lo que manda) y el buffer de envío del
        agente queda fijo y chico: quien envía se bloquea tras unas mil respuestas, lleguen o no más mensajes."""
        cli = self.abrir_sin_leer(rcvbuf=None)
        cli.send(self.mensaje())
        self.leer_hasta_sesion_ok(cli)
        sesion = self.server.session
        sesion.channel.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
        return cli, sesion

    def abrir_sin_leer_sesion(self):
        cli = self.abrir_sin_leer()
        cli.send(self.mensaje())
        self.leer_hasta_sesion_ok(cli)
        return cli

    def test_ping_sin_leer_las_respuestas_acota_la_cola_y_cierra(self):
        with mock.patch.object(agent, "SEND_TIMEOUT", 1.5):
            cli, sesion = self.abrir_sin_leer_mandando()
            maximo = 0
            # El cliente NO se rinde antes que el agente: su envío tenía el mismo tiempo límite (1.5 s) que el
            # agente para decidir "no lee"; al empatarse, el cliente podía rendirse a media línea (sendall no es
            # atómico) justo cuando el agente volvía a leer, y el agente esperaba el resto de esa línea hasta dar
            # "no dijo nada en 6 s" en vez de "no lee". Con 20 s quien cierra es siempre el agente.
            cli.ch.sock.settimeout(20)
            t0 = time.monotonic()
            enviados = 0
            est = {"n": 0, "desde": None}      # lo que hace el hilo del cliente (solo lo lee el muestreador)
            filas: list[str] = []
            parar_muestreo = threading.Event()
            self.addCleanup(parar_muestreo.set)

            def muestrear():
                # Solo lee (sin candados del agente): si algo falla, '?'. Una fila cada 0.25 s.
                while not parar_muestreo.is_set():
                    try:
                        ahora = time.monotonic()
                        out, ch = sesion.out, sesion.channel
                        marcos = sys._current_frames()
                        lector = next((_pila_corta(f) for f in marcos.values() if any(
                            c.f_code.co_name == "_loop" and c.f_code.co_filename == agent.__file__
                            for c in _cadena(f))), "?")
                        sender = _pila_corta(marcos.get(sesion.sender.ident))
                        desde = est["desde"]
                        fila = (f"{ahora - t0:6.2f}s cli: n={est['n']} "
                                f"{'en send ' + format(ahora - desde, '.2f') + 's' if desde else 'fuera de send'} | "
                                f"agente: rep={out.replies} items={len(out.items)} bytes={out.bytes} "
                                f"sending={out.sending} closed={out.closed} ending={sesion._ending} "
                                f"leidos={ch._received} media_linea={len(ch._lines.buf)} | lector={lector} "
                                f"sender={sender} | sock agente: {_tcp_resumen(ch.sock)} | sock cliente: "
                                f"{_tcp_resumen(cli.ch.sock)}")
                    except Exception as e:
                        fila = f"{time.monotonic() - t0:6.2f}s ? ({type(e).__name__}: {e})"
                    filas.append(fila)
                    parar_muestreo.wait(0.25)

            def _cadena(f):
                while f is not None:
                    yield f
                    f = f.f_back

            def linea_de_tiempo() -> str:
                if len(filas) > 55:
                    filas_vistas = filas[:15] + [f"... ({len(filas) - 55} filas omitidas)"] + filas[-40:]
                else:
                    filas_vistas = filas
                return ("\nlínea de tiempo (muestreada cada 0.25 s):\n" + "\n".join(filas_vistas) +
                        "\nsysctl: " + "; ".join(f"{k}={_sysctl(k)}" for k in
                                                  ("net.ipv4.tcp_rmem", "net.ipv4.tcp_wmem", "net.ipv4.tcp_mem")))

            threading.Thread(target=muestrear, daemon=True, name="muestreador-ping").start()
            try:
                # Sin tope de cantidad: cuántas respuestas caben en los buffers del kernel (hasta tcp_wmem máx.,
                # 4 MB en Linux por omisión, más en otros) cambia de una máquina a otra; si el tope de pings
                # cabía entero en ellos, el agente nunca veía "sin leer" y cerraba por silencio. Se manda hasta
                # que el agente cierra la sesión (el envío falla) o se acaba el tiempo.
                while time.monotonic() - t0 < 30 and not sesion._ending:
                    est["desde"] = time.monotonic()
                    try:
                        cli.ch.send({"op": "ping", "t": "x" * 60})
                    finally:
                        est["desde"] = None
                    enviados += 1
                    est["n"] = enviados
                    if enviados % 50 == 0 and sesion is not None:
                        maximo = max(maximo, self.cola(sesion))
            except ChannelError:
                pass
            maximo = max(maximo, self.cola(sesion))
            self.assertLessEqual(maximo, getattr(agent, "OUT_MAX_REPLIES", 1000) + 5,
                                 f"la cola de respuestas llegó a {maximo}" + linea_de_tiempo())
            self.assertGreater(enviados, 100, linea_de_tiempo())
            # el agente da la sesión por perdida, detiene los nodos y queda libre
            self.assertTrue(self.espera(lambda: self.server.session is None, 30),
                            "el agente no se recuperó" + linea_de_tiempo())
            self.assertTrue(self.espera(lambda: not self.vivo("motor"), 30), linea_de_tiempo())
            self.assertEqual(self.cola(sesion), 0, "la memoria de la cola no se liberó al cerrar" + linea_de_tiempo())
        parar_muestreo.set()
        self.assertTrue(any("no lee" in t for t, _ in self.said),
                        f"{enviados} ping en {time.monotonic() - t0:.1f} s, cola máx. {maximo}; el agente dijo: "
                        f"{self.said}" + linea_de_tiempo())

    def test_ordenes_con_error_sin_leer_tambien_acotan_la_cola(self):
        with mock.patch.object(agent, "SEND_TIMEOUT", 1.5):
            cli, sesion = self.abrir_sin_leer_mandando()
            cli.ch.sock.settimeout(20)      # como en la prueba del ping: el cliente no se rinde antes que el agente
            maximo, enviados = 0, 0
            try:
                while enviados < 40000:
                    cli.ch.send({"op": "baila" + "z" * 100})
                    enviados += 1
                    if enviados % 50 == 0:
                        maximo = max(maximo, self.cola(sesion))
                    if sesion._ending:
                        break
            except ChannelError:
                pass
            self.assertLessEqual(maximo, getattr(agent, "OUT_MAX_REPLIES", 1000) + 5)
            self.assertTrue(self.espera(lambda: self.server.session is None, 30))


# ===========================================================================
# 6. El cliente (AgentLink / RemoteNode) en proceso
# ===========================================================================

class FakeAgent:
    """Un "agente" de mentira en un puerto libre: cada conexión la atiende `comportamiento(sock)` en su hilo."""

    def __init__(self, comportamiento):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.comportamiento = comportamiento
        self.conns: list[socket.socket] = []
        threading.Thread(target=self._aceptar, daemon=True).start()

    def _aceptar(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self._atender, args=(c,), daemon=True).start()

    def _atender(self, c):
        try:
            self.comportamiento(c)
        except Exception:
            pass

    def close(self):
        for c in self.conns + [self.sock]:
            try:
                c.close()
            except OSError:
                pass


def agente_que_saluda_bien(script):
    """Hace el saludo de verdad (con la clave KEY), espera la sesión y corre script(canal, sesion)."""
    def comportamiento(c):
        ch = agent_handshake(c, K())
        script(ch, ch.recv(5))
    return comportamiento


class LinkCase(ServerCase):
    def machine(self, port: int | None = None):
        import dataclasses
        cfg = load_config(self.toml)
        m = cfg.machines[0]
        return cfg, (dataclasses.replace(m, port=port) if port else m)

    def montar(self, key: str = KEY):
        """Un Launcher como el de agmen run, con un AgentLink abierto contra el agente de prueba."""
        from agmen.run import Launcher
        cfg, m = self.machine()
        link = agent.AgentLink(m, toml_hint=str(self.toml))
        self.stream = io.StringIO()
        launcher = Launcher(cfg, stream=self.stream, places={"robot": Place(False, "other")}, remotes={"robot": link})
        self.addCleanup(lambda: (link.close(), launcher.printer.close()))
        return cfg, link, launcher

    def esperar_estado(self, nodo, estado, timeout=15):
        return self.espera(lambda: nodo.state == estado, timeout)


class TestEnlace(LinkCase):
    def test_abrir_arrancar_detener_reiniciar_y_terminar(self):
        self.start_server()
        cfg, link, launcher = self.montar()
        claves = []
        link.open(KEY, agent.session_message(cfg, "robot", "real"), on_key_ok=lambda: claves.append(1))
        self.assertEqual(claves, [1])
        self.assertEqual(link.version, __version__)
        self.assertEqual(sorted(link.nodes), ["imu", "motor"])
        motor = launcher.nodes["motor"]
        self.assertIsInstance(motor, agent.RemoteNode)
        self.assertNotIsInstance(launcher.nodes["ia"], agent.RemoteNode)
        self.assertFalse((self.marks / "arranque_motor").exists(), "la sesión de agmen run lleva no_arrancar")
        motor.start()
        self.assertTrue(self.esperar_estado(motor, "corriendo"))
        info = motor.info()
        self.assertEqual((info["name"], info["machine"], info["state"]), ("motor", "robot", "corriendo"))
        self.assertIsInstance(info["pid"], int)
        self.assertTrue(self.espera(lambda: self.vivo("motor")))
        self.assertFalse(self.vivo("imu"), "arrancar motor no arranca imu")
        motor.restart()
        self.assertTrue(self.espera(lambda: len(self.pids("motor")) >= 2, 30))
        self.assertTrue(self.esperar_estado(motor, "corriendo"))
        motor.stop()
        self.assertTrue(self.esperar_estado(motor, "detenido"))
        self.assertEqual(motor.info()["pid"], None)
        self.assertTrue(self.espera(lambda: not self.vivo("motor")))
        launcher.nodes["imu"].start()
        self.assertTrue(self.esperar_estado(launcher.nodes["imu"], "corriendo"))
        launcher.stop_all()      # detiene primero la otra computadora (detener_todo, adios) y luego lo de aquí
        self.assertTrue(self.espera(lambda: not self.vivo("imu"), 30))
        self.assertTrue(self.espera(lambda: self.server.session is None, 30))
        salida = self.stream.getvalue()
        self.assertIn("robot/motor │ ", salida)
        self.assertIn("HOLA motor", salida)
        self.assertNotIn(KEY, salida)

    def test_los_logs_llegan_al_logbook_con_el_nombre_del_nodo(self):
        self.start_server()
        cfg, link, launcher = self.montar()
        link.open(KEY, agent.session_message(cfg, "robot", "real"))
        launcher.nodes["motor"].start()
        self.assertTrue(self.espera(lambda: any(l["node"] == "motor" and "HOLA motor" in l["text"]
                                               for l in launcher.logs.since(0)[0]), 15))

    def test_clave_mala_no_dice_la_clave_ni_llama_a_on_key_ok(self):
        self.start_server()
        cfg, link, launcher = self.montar()
        llamadas = []
        with self.assertRaises(agent.LinkError) as ctx:
            link.open("aaaa-bbbb-cccc", agent.session_message(cfg, "robot", "real"), on_key_ok=lambda: llamadas.append(1))
        self.assertEqual(ctx.exception.code, "clave")
        self.assertEqual(llamadas, [])
        self.assertIn("rechazó la clave", str(ctx.exception))
        self.assertNotIn("aaaa-bbbb-cccc", str(ctx.exception))
        self.assertIn("robot", str(ctx.exception))

    def test_sin_agente_dice_donde_correrlo(self):
        self.start_server()
        cfg, m = self.machine(port=free_tcp_port())
        link = agent.AgentLink(m, toml_hint="mi-robot.toml")
        with self.assertRaises(agent.LinkError) as ctx:
            link.open(KEY, agent.session_message(cfg, "robot", "real"))
        self.assertEqual(ctx.exception.code, "rechazada")
        t = str(ctx.exception)
        self.assertIn("no contesta", t)
        self.assertIn("¿está corriendo agmen agent ahí? En robot: agmen agent mi-robot.toml", t)

    def test_sin_ruta_y_tiempo_agotado(self):
        self.start_server()
        cfg, link, _ = self.montar()
        sesion = agent.session_message(cfg, "robot", "real")
        with mock.patch("socket.create_connection", side_effect=OSError(101, "Network is unreachable")):
            with self.assertRaises(agent.LinkError) as ctx:
                link.open(KEY, sesion)
        self.assertEqual(ctx.exception.code, "sin_ruta")
        self.assertIn("no llego a robot", str(ctx.exception))
        self.assertIn("Network is unreachable", str(ctx.exception))
        with mock.patch("socket.create_connection", side_effect=socket.timeout("timed out")):
            with self.assertRaises(agent.LinkError) as ctx:
                link.open(KEY, sesion)
        self.assertEqual(ctx.exception.code, "tiempo")
        self.assertIn("no contestó en 5 s", str(ctx.exception))

    def test_otro_programa_en_el_puerto(self):
        for respuesta, codigo in ((b"HTTP/1.1 400 Bad Request\r\n\r\n", "no_agmen"), (b"SSH-2.0-OpenSSH\r\n", "no_agmen")):
            falso = FakeAgent(lambda c, r=respuesta: (c.recv(100), c.sendall(r)))
            self.addCleanup(falso.close)
            self.start_server() if self.server is None else None
            cfg, m = self.machine(port=falso.port)
            link = agent.AgentLink(m)
            with self.assertRaises(agent.LinkError) as ctx:
                link.open(KEY, agent.session_message(cfg, "robot", "real"))
            self.assertEqual(ctx.exception.code, codigo)
            self.assertIn("contestó algo que no es agmen agent", str(ctx.exception))

    def test_cierra_en_el_saludo_y_no_contesta(self):
        self.start_server()
        cerrar = FakeAgent(lambda c: c.close())
        mudo = FakeAgent(lambda c: time.sleep(5))
        self.addCleanup(cerrar.close)
        self.addCleanup(mudo.close)
        cfg, _ = self.machine()
        with self.assertRaises(agent.LinkError) as ctx:
            agent.AgentLink(self.machine(port=cerrar.port)[1]).open(KEY, agent.session_message(cfg, "robot", "real"))
        self.assertEqual(ctx.exception.code, "cerrado")
        with mock.patch.object(agent.client_handshake, "__kwdefaults__", {"timeout": 0.5}):
            t0 = time.monotonic()
            with self.assertRaises(agent.LinkError) as ctx:
                agent.AgentLink(self.machine(port=mudo.port)[1]).open(KEY, agent.session_message(cfg, "robot", "real"))
        self.assertEqual(ctx.exception.code, "tiempo")
        self.assertLess(time.monotonic() - t0, 20)

    def test_impostor(self):
        self.start_server()

        def impostor(c):
            p = Peer(c)
            p.line()
            p.send(f"agmen-agent/1 hola {secrets.token_hex(16)} 0.13.0\n")
            p.line()
            p.send("prueba " + "0" * 64 + "\n")

        falso = FakeAgent(impostor)
        self.addCleanup(falso.close)
        cfg, _ = self.machine()
        with self.assertRaises(agent.LinkError) as ctx:
            agent.AgentLink(self.machine(port=falso.port)[1]).open(KEY, agent.session_message(cfg, "robot", "real"))
        self.assertEqual(ctx.exception.code, "impostor")

    def test_espera_no_se_confunde_con_clave_mala(self):
        with mock.patch.object(agent, "FAIL_PAUSE", 0.0):
            self.start_server()
            cfg, link, _ = self.montar()
            for _ in range(5):
                with self.assertRaises(agent.LinkError) as ctx:
                    link.open("aaaa-bbbb-cccc", agent.session_message(cfg, "robot", "real"))
                self.assertEqual(ctx.exception.code, "clave")
            with self.assertRaises(agent.LinkError) as ctx:
                link.open(KEY, agent.session_message(cfg, "robot", "real"))
            self.assertEqual(ctx.exception.code, "espera")
            self.assertNotIn("rechazó la clave", str(ctx.exception))
            self.assertIn("no acepta intentos por ahora", str(ctx.exception))

    def test_los_codigos_de_sesion_error_llegan_con_el_nombre_de_la_computadora(self):
        self.start_server()
        cfg, link, _ = self.montar()
        casos = {"perfil": {"perfil": "nope"}, "maquina": {"maquina": "otra"}, "protocolo": {"no_arrancar": ["fantasma"]}}
        for codigo, cambio in casos.items():
            with self.subTest(codigo=codigo):
                msg = agent.session_message(cfg, "robot", "real")
                msg.update(cambio)
                with self.assertRaises(agent.LinkError) as ctx:
                    agent.AgentLink(self.machine()[1]).open(KEY, msg)
                self.assertEqual(ctx.exception.code, codigo)
                self.assertTrue(str(ctx.exception).startswith("robot: "), str(ctx.exception))
                self.assertTrue(self.espera(lambda: self.server.session is None, 30))

    def test_agente_que_no_contesta_la_sesion(self):
        mudo = FakeAgent(agente_que_saluda_bien(lambda ch, ses: time.sleep(5)))
        self.addCleanup(mudo.close)
        self.start_server()
        cfg, _ = self.machine()
        with mock.patch.object(agent, "SILENCE_TIMEOUT", 0.8):
            link = agent.AgentLink(self.machine(port=mudo.port)[1])
            with self.assertRaises(agent.LinkError) as ctx:
                link.open(KEY, agent.session_message(cfg, "robot", "real"))
        self.assertEqual(ctx.exception.code, "perdida")
        self.assertIn("dejó de contestar", str(ctx.exception))

    def test_el_enlace_se_pierde_con_el_nodo_corriendo(self):
        self.start_server()
        cfg, link, launcher = self.montar()
        perdidas = []
        link.open(KEY, agent.session_message(cfg, "robot", "real"))
        link.on_lost = lambda l, t: perdidas.append(t)
        launcher.nodes["motor"].start()
        self.assertTrue(self.esperar_estado(launcher.nodes["motor"], "corriendo"))
        self.server.session.channel.close()          # el agente "se cae"
        self.assertTrue(self.espera(lambda: perdidas, 30), "no se enteró")
        self.assertEqual(len(perdidas), 1)
        t = perdidas[0]
        for frag in ("robot (127.0.0.1:", "se perdió la conexión con agmen agent", "Se detiene todo", "vuelve a correr agmen run"):
            self.assertIn(frag, t)
        self.assertEqual(launcher.nodes["motor"].state, "falló")
        self.assertIn("no hay conexión con robot", link.order("arrancar", "motor"))
        time.sleep(0.3)
        self.assertEqual(len(perdidas), 1, "on_lost se llama una sola vez")

    def test_el_silencio_del_agente_en_plena_sesion_lo_da_por_perdido(self):
        quieto = FakeAgent(agente_que_saluda_bien(
            lambda ch, ses: (ch.send({"op": "sesion_ok", "maquina": "robot", "agmen": __version__, "nodos": ["motor", "imu"]}),
                             time.sleep(6))))
        self.addCleanup(quieto.close)
        self.start_server()
        cfg, _ = self.machine()
        perdidas = []
        with mock.patch.object(agent, "SILENCE_TIMEOUT", 0.8):
            link = agent.AgentLink(self.machine(port=quieto.port)[1])
            self.addCleanup(link.close)
            link.open(KEY, agent.session_message(cfg, "robot", "real"))
            link.on_lost = lambda l, t: perdidas.append(t)
            self.assertTrue(self.espera(lambda: perdidas, 30))
        self.assertIn("dejó de contestar", perdidas[0])

    def test_finish_manda_detener_todo_y_adios(self):
        self.start_server()
        cfg, link, launcher = self.montar()
        link.open(KEY, agent.session_message(cfg, "robot", "real"))
        for n in launcher.nodes.values():
            if n.cfg.machine == "robot":
                n.start()
        self.assertTrue(self.espera(lambda: self.vivo("motor") and self.vivo("imu")))
        link.finish()
        self.assertTrue(self.espera(lambda: not self.vivo("motor") and not self.vivo("imu"), 30))
        self.assertTrue(self.espera(lambda: self.server.session is None, 30))

    def test_remote_node_sin_conexion_dice_que_no_se_hizo(self):
        self.start_server()
        cfg, link, launcher = self.montar()
        # sin open(): no hay canal
        launcher.nodes["motor"].start()
        self.assertTrue(self.espera(lambda: any("no hay conexión con robot" in l["text"] for l in launcher.logs.since(0)[0]), 30))

    # --- lo que llega del AGENTE se limpia (segunda revisión)
    def con_agente_falso(self, enviar):
        """Un agente que acepta la sesión, manda `enviar(canal)` y se queda. Devuelve (link, launcher, salida)."""
        from agmen.run import Launcher

        def script(ch, ses):
            ch.send({"op": "sesion_ok", "maquina": "robot", "agmen": __version__, "nodos": ["motor", "imu"]})
            enviar(ch)
            time.sleep(4)

        falso = FakeAgent(agente_que_saluda_bien(script))
        self.addCleanup(falso.close)
        self.start_server()
        cfg, m = self.machine(port=falso.port)
        link = agent.AgentLink(m)
        stream = io.StringIO()
        launcher = Launcher(cfg, stream=stream, places={"robot": Place(False, "other")}, remotes={"robot": link})
        self.addCleanup(lambda: (link.close(), launcher.printer.close()))
        link.open(KEY, agent.session_message(cfg, "robot", "real"))
        return link, launcher, stream

    def test_texto_del_agente_sin_escapes_ni_saltos_de_linea(self):
        sucio = "a\x1b[2Jb\x07c\nrobot/motor │ ✖ FALSO\rotra"

        def enviar(ch):
            ch.send({"op": "log", "nodo": "motor", "n": 1, "linea": sucio})
            ch.send({"op": "log", "nodo": "agmen", "n": 1, "linea": sucio})
            ch.send({"op": "log", "nodo": "no-existe", "n": 1, "linea": sucio})
            ch.send({"op": "error", "texto": sucio})
            ch.send({"op": "ping"})

        link, launcher, stream = self.con_agente_falso(enviar)
        self.assertTrue(self.espera(lambda: len([l for l in launcher.logs.since(0)[0]]) >= 4, 30), launcher.logs.since(0))
        time.sleep(0.5)
        launcher.printer.close()
        salida = stream.getvalue()
        for c in salida:
            self.assertTrue(c.isprintable() or c == "\n", repr(c))
        for linea in salida.splitlines():
            self.assertFalse(linea.startswith("robot/motor │ ✖ FALSO"), "una línea inyectada no debe parecer de la terminal")
        for l in launcher.logs.since(0)[0]:
            self.assertNotIn("\n", l["text"])
            self.assertNotIn("\x1b", l["text"])

    def test_estado_desconocido_del_agente_se_ignora(self):
        def enviar(ch):
            ch.send({"op": "estado", "nodo": "motor", "estado": "corriendo", "pid": 5, "codigo": None})
            ch.send({"op": "estado", "nodo": "motor", "estado": "zzz\x1b[31m", "pid": 5, "codigo": None})
            ch.send({"op": "estado", "nodo": "motor", "estado": 7, "pid": 5, "codigo": None})
            ch.send({"op": "estado", "nodo": "imu", "estado": "falló", "pid": None, "codigo": 3})

        link, launcher, stream = self.con_agente_falso(enviar)
        self.assertTrue(self.espera(lambda: launcher.nodes["imu"].state == "falló", 30))
        self.assertEqual(launcher.nodes["motor"].state, "corriendo", "un estado que el Launcher no conoce debe ignorarse")
        self.assertEqual(launcher.nodes["imu"].exit_code, 3)

    def test_cada_estado_conocido_se_acepta(self):
        estados = ["corriendo", "detenido", "falló", "reiniciando", "terminó"]

        def enviar(ch):
            for e in estados:
                ch.send({"op": "estado", "nodo": "motor", "estado": e, "pid": None, "codigo": None})

        link, launcher, stream = self.con_agente_falso(enviar)
        self.assertTrue(self.espera(lambda: launcher.nodes["motor"].state == "terminó", 30))


# ===========================================================================
# 7. De punta a punta: `agmen agent` y `agmen run` como subprocesos reales
# ===========================================================================

class E2EBase(unittest.TestCase):
    """El agente en 127.0.0.1:<puerto libre> y agmen run que trata a 127.0.0.1 como "otra computadora"
    (AGMEN_REMOTE_HOSTS). Cada proceso con su carpeta de configuración y su hub; se detiene SOLO lo que se lanza aquí."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.marks = self.dir / "marcas"
        self.marks.mkdir()
        (self.dir / "nodo.py").write_text(NODO_PY, encoding="utf-8")
        self.port = free_tcp_port()
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self._limpiar)
        self.write_toml()

    def write_toml(self, hub: bool = False, web: str = "", motor_cmd_extra: str = "") -> Path:
        nodo = json.dumps(str(self.dir / "nodo.py"))
        marks = json.dumps(str(self.marks))
        self.toml = self.dir / "robot.toml"
        self.toml.write_text(
            f'[robot]\nname = "e2e"\n\n[[machine]]\nname = "robot"\nhost = "127.0.0.1:{self.port}"\n\n'
            f'[[node]]\nname = "motor"\ncmd = ["python", {nodo}, "motor", {marks}{motor_cmd_extra}]\nmachine = "robot"\n\n'
            f'[[node]]\nname = "imu"\ncmd = ["python", {nodo}, "imu", {marks}]\nmachine = "robot"\n\n'
            f'[[node]]\nname = "ia"\ncmd = ["python", {nodo}, "ia", {marks}]\n\n'
            f'[hub]\nenabled = {str(hub).lower()}\n\n[web]\nenabled = {"true" if web else "false"}\n{web}',
            encoding="utf-8")
        return self.toml

    hub_real = False     # True: que agmen arranque su propio agmen hub (si no, hay uno vacío vivo en el hilo de la prueba)

    def hub_vacio(self) -> str:
        """Un hub vacío en un puerto libre, dentro de esta prueba: las comprobaciones de "nodos ajenos" de agmen
        run y agmen agent lo encuentran enseguida (sin él esperan 0.5 s a que nadie conteste) y no hay nodos ajenos."""
        from agmen import hub as hubmod
        puerto = free_udp_port()
        hub = hubmod.Hub(f"127.0.0.1:{puerto}", log=lambda t: None)
        parar = threading.Event()
        threading.Thread(target=hub.serve, args=(parar,), daemon=True).start()
        self.addCleanup(parar.set)
        return f"127.0.0.1:{puerto}"

    def env(self, quien: str, **extra) -> dict:
        e = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1",
             "AGMEN_CONFIG_DIR": str(self.dir / f"cfg-{quien}"),
             "AGMEN_HUB": f"127.0.0.1:{free_udp_port()}" if self.hub_real else self.hub_vacio(),
             "AGMEN_REMOTE_HOSTS": "", "AGMEN_AGENT_KEY": ""}
        e.update(extra)
        return e

    def lanzar(self, args: list[str], env: dict) -> tuple[subprocess.Popen, list[str]]:
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        p = subprocess.Popen([sys.executable, "-m", "agmen", *args], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                             errors="replace", cwd=self.dir, **kw)
        lineas: list[str] = []
        threading.Thread(target=lambda: lineas.extend(iter(p.stdout.readline, "")), daemon=True).start()
        self.procs.append(p)
        return p, lineas

    def agente(self, toml: Path | None = None, hub_env: str | None = None, **extra):
        env = self.env("agente", **extra)
        if hub_env:
            env["AGMEN_HUB"] = hub_env
        p, lineas = self.lanzar(["agent", str(toml or self.toml), "--machine", "robot", "--port", str(self.port)], env)
        self.assertTrue(self.espera(lambda: any("esperando a agmen run" in l for l in lineas) or p.poll() is not None,
                                    40), "".join(lineas))
        self.assertIsNone(p.poll(), "".join(lineas))
        # Con la salida por una tubería el agente no imprime la clave (3c §5): se pide con --show-key,
        # con la misma carpeta de configuración.
        r = subprocess.run([sys.executable, "-m", "agmen", "agent", "--show-key"], env=env, capture_output=True,
                           text=True, encoding="utf-8", timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        clave = r.stdout.strip()
        return p, lineas, clave

    def correr(self, clave: str | None = KEY, args: tuple = ("--no-web",), toml: Path | None = None, **extra):
        env = self.env("run", AGMEN_REMOTE_HOSTS="127.0.0.1", **({"AGMEN_AGENT_KEY": clave} if clave else {}), **extra)
        return self.lanzar(["run", str(toml or self.toml), "--no-browser", *args], env)

    def espera(self, cond, timeout=40.0, paso=0.05) -> bool:
        fin = time.monotonic() + timeout
        while time.monotonic() < fin:
            if cond():
                return True
            time.sleep(paso)
        return cond()

    def vivo(self, nodo, ventana=3.0):
        return nodo_vivo(self.marks, nodo, ventana)

    def ninguno_vivo(self, nodos, ventana=3.0) -> bool:
        """¿Ya no queda vivo ninguno de estos nodos de prueba? En POSIX nodo_vivo mira el pid: se comprueba al
        instante. En Windows solo ve el latido (cada 0.1 s): un nodo recién detenido sigue "vivo" hasta que vence
        la ventana, así que se espera la ventana más un margen corto; uno que de verdad quedó vivo sigue latiendo."""
        if os.name == "posix":
            return not any(self.vivo(n, ventana) for n in nodos)
        return self.espera(lambda: not any(self.vivo(n, ventana) for n in nodos), ventana + 2.0)

    def fin(self, p: subprocess.Popen, timeout=40) -> int | None:
        """El código de salida de p; None si no salió en `timeout` s (se mata, y la falla muestra su salida en
        vez de un TimeoutExpired sin ella)."""
        try:
            return p.wait(timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            return None

    def detener(self, p: subprocess.Popen, timeout=40) -> int | None:
        if p.poll() is None:
            p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        try:
            return p.wait(timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            return None

    def _limpiar(self):
        for p in reversed(self.procs):
            if p.poll() is None:
                try:
                    self.detener(p, 20)
                except Exception:
                    pass
        for f in self.marks.glob("arranque_*"):      # lo que haya quedado, por su pid (los de esta prueba)
            for pid in f.read_text().split():
                matar(int(pid))

    def texto(self, lineas):
        return "".join(lineas)

    def pids_de_marcas(self) -> dict:
        """Los pids que escribió cada nodo de prueba al arrancar (para el mensaje de una falla)."""
        return {f.name[len("arranque_"):]: f.read_text().split() for f in sorted(self.marks.glob("arranque_*"))}


class E2E(E2EBase):
    # --- el camino feliz
    def test_arranca_conecta_logs_y_ctrl_c_detiene_las_dos_puntas(self):
        self.hub_real = True       # aquí cada punta arranca SU agmen hub (hubs distintos)
        self.write_toml(hub=True)
        pa, la, clave = self.agente()
        pr, lr = self.correr(clave=clave)
        self.assertTrue(self.espera(lambda: all(any(f"HOLA {n} " in l for l in lr) for n in ("motor", "imu", "ia"))
                                    or pr.poll() is not None, 60), self.texto(lr))
        self.assertIsNone(pr.poll(), self.texto(lr))
        salida = self.texto(lr)
        self.assertIn(f"robot (127.0.0.1:{self.port}): conectado · Agmen {__version__}", salida)
        self.assertIn("robot/motor │ HOLA motor", salida)
        self.assertRegex(salida, r"robot/imu\s+│ HOLA imu")
        self.assertIn("todo arrancado", salida)
        self.assertNotIn(clave, salida, "la clave nunca debe salir en la terminal de agmen run")
        self.assertNotIn("Traceback", salida)
        self.assertTrue(self.espera(lambda: self.vivo("motor") and self.vivo("imu") and self.vivo("ia"), 30))
        pids = {n: [int(x) for x in (self.marks / f"arranque_{n}").read_text().split()] for n in ("motor", "imu", "ia")}
        # el hub de cada lado: uno en la laptop y otro en el agente (hubs distintos)
        self.assertTrue(any("agmen hub en" in l for l in lr), salida)
        # Ctrl+C en agmen run: se detienen las dos puntas, código 0
        self.assertEqual(self.detener(pr), 0, self.texto(lr))
        self.assertTrue(self.espera(lambda: not any(self.vivo(n) for n in ("motor", "imu", "ia")), 30), "quedaron nodos vivos")
        self.assertIn("listo. ¡Hasta luego!", self.texto(lr))
        # el agente quedó libre y esperando otra vez
        self.assertTrue(self.espera(lambda: any("(otra vez)" in l for l in la), 30), self.texto(la))
        self.assertIsNone(pa.poll(), "el agente no debe terminar porque la laptop se fue")
        # y acepta otra sesión
        pr2, lr2 = self.correr(clave=clave)
        self.assertTrue(self.espera(lambda: any("robot/motor │ HOLA motor" in l for l in lr2) or pr2.poll() is not None, 60),
                        self.texto(lr2))
        self.assertEqual(self.detener(pr2), 0, self.texto(lr2))
        # Ctrl+C en el agente
        self.assertEqual(self.detener(pa), 0, self.texto(la))
        self.assertIn("listo. ¡Hasta luego!", self.texto(la))
        self.assertTrue(self.ninguno_vivo(("motor", "imu", "ia")),
                        "quedaron nodos vivos tras detener las dos puntas: "
                        f"{[n for n in ('motor', 'imu', 'ia') if self.vivo(n)]}; pids {self.pids_de_marcas()}; "
                        f"agmen run: código {pr2.poll()}\n{self.texto(lr2)}\nagmen agent: código {pa.poll()}\n"
                        f"{self.texto(la)}")
        self.assertNotIn(clave, self.texto(lr2))

    def test_la_clave_escrita_a_mano_no_se_guarda_si_viene_de_la_variable(self):
        pa, la, clave = self.agente()
        pr, lr = self.correr(clave=clave)
        self.assertTrue(self.espera(lambda: any("todo arrancado" in l for l in lr) or pr.poll() is not None, 40), self.texto(lr))
        self.detener(pr)
        self.assertFalse((self.dir / "cfg-run" / "keys.toml").exists(), "una clave de AGMEN_AGENT_KEY no se guarda")
        self.assertTrue((self.dir / "cfg-agente" / "agent.key").exists())

    def test_la_clave_guardada_se_usa_sin_la_variable_y_forget_key_la_borra(self):
        pa, la, clave = self.agente()
        cfg = self.dir / "cfg-run"
        with mock.patch.dict(os.environ, {"AGMEN_CONFIG_DIR": str(cfg)}):
            agent.save_client_key(agent.machine_address("127.0.0.1", self.port), clave)
        pr, lr = self.correr(clave=None)          # sin AGMEN_AGENT_KEY: usa keys.toml
        self.assertTrue(self.espera(lambda: any("todo arrancado" in l for l in lr) or pr.poll() is not None, 40), self.texto(lr))
        self.assertIsNone(pr.poll(), self.texto(lr))
        self.assertEqual(self.detener(pr), 0)
        # --forget-key
        pf, lf = self.correr(clave=None, args=("--forget-key", "robot"))
        self.assertEqual(self.fin(pf, 40), 0, self.texto(lf))
        self.assertIn(f"borré la clave de robot (127.0.0.1:{self.port})", self.texto(lf))
        self.assertNotIn(clave, self.texto(lf))
        with mock.patch.dict(os.environ, {"AGMEN_CONFIG_DIR": str(cfg)}):
            self.assertIsNone(agent.client_key(agent.machine_address("127.0.0.1", self.port)))
        pf2, lf2 = self.correr(clave=None, args=("--forget-key", "robot"))
        self.assertEqual(self.fin(pf2, 40), 0)
        self.assertIn("no había una clave guardada para robot", self.texto(lf2))
        pf3, lf3 = self.correr(clave=None, args=("--forget-key", "robto"))
        self.assertEqual(self.fin(pf3, 40), 2)
        self.assertIn("¿Quisiste decir 'robot'?", self.texto(lf3))
        # y sin clave guardada ni variable ni terminal: error que dice como darla, y no arranca nada
        antes = (self.marks / "arranque_ia").read_text() if (self.marks / "arranque_ia").exists() else ""
        pn, ln = self.correr(clave=None)
        self.assertEqual(self.fin(pn, 40), 2, self.texto(ln))
        self.assertIn("AGMEN_AGENT_KEY", self.texto(ln))
        despues = (self.marks / "arranque_ia").read_text() if (self.marks / "arranque_ia").exists() else ""
        self.assertEqual(despues, antes, "arrancó 'ia' sin tener la clave")

    def test_la_clave_guardada_que_ya_no_sirve_dice_como_cambiarla(self):
        pa, la, clave = self.agente()
        with mock.patch.dict(os.environ, {"AGMEN_CONFIG_DIR": str(self.dir / "cfg-run")}):
            agent.save_client_key(agent.machine_address("127.0.0.1", self.port), "aaaa-bbbb-cccc")
        pr, lr = self.correr(clave=None)
        self.assertEqual(self.fin(pr, 40), 2, self.texto(lr))
        t = self.texto(lr)
        self.assertIn("rechazó la clave", t)
        self.assertIn("La clave guardada ya no sirve", t)
        self.assertIn("agmen run --forget-key robot", t)
        self.assertNotIn("aaaa-bbbb-cccc", t)
        self.assertFalse((self.marks / "arranque_ia").exists(), "no debe arrancar nada local")

    # --- los errores: código 2 y no arranca nada
    def test_sin_agente_codigo_2_y_no_arranca_nada_ni_el_hub(self):
        self.hub_real = True
        self.write_toml(hub=True)
        pr, lr = self.correr(clave=KEY)
        self.assertEqual(self.fin(pr, 60), 2, self.texto(lr))
        t = self.texto(lr)
        self.assertIn(f"error: robot (127.0.0.1:{self.port}) no contesta: ¿está corriendo agmen agent ahí? En robot: agmen agent", t)
        self.assertFalse(list(self.marks.iterdir()), "arrancó algún nodo")
        self.assertNotIn("iniciado", t)           # ni el hub ni un nodo
        self.assertNotIn("todo arrancado", t)
        self.assertNotIn("Traceback", t)
        self.assertNotIn(KEY, t)

    def test_clave_mala_codigo_2_no_se_guarda_y_no_sale_en_pantalla(self):
        pa, la, clave = self.agente()
        mala = "aaaa-bbbb-cccc"
        pr, lr = self.correr(clave=mala)
        self.assertEqual(self.fin(pr, 60), 2, self.texto(lr))
        t = self.texto(lr)
        self.assertIn("rechazó la clave", t)
        self.assertIn("AGMEN_AGENT_KEY", t)
        for secreto in (mala, clave, mala.replace("-", "")):
            self.assertNotIn(secreto, t)
        # el agente muestra SU clave al arrancar (por diseño), pero nunca la que intentó otra persona
        self.assertNotIn(mala, self.texto(la))
        self.assertNotIn(mala.replace("-", ""), self.texto(la))
        self.assertFalse((self.dir / "cfg-run" / "keys.toml").exists(), "una clave mala no se guarda")
        self.assertFalse(list(self.marks.iterdir()))

    def test_robot_toml_distinto_codigo_2_con_todas_las_diferencias(self):
        pa, la, clave = self.agente()
        laptop = self.dir / "laptop.toml"
        laptop.write_text(self.toml.read_text(encoding="utf-8").replace(
            'name = "motor"\ncmd = ["python"', 'name = "motor"\ncmd = ["python3"', 1), encoding="utf-8")
        pr, lr = self.correr(clave=clave, toml=laptop)
        self.assertEqual(self.fin(pr, 60), 2, self.texto(lr))
        t = self.texto(lr)
        self.assertIn("el nodo motor es distinto en su robot.toml (cmd)", t)
        self.assertIn("Haz git pull en robot", t)
        self.assertFalse(list(self.marks.iterdir()), "no debe arrancar nada, ni lo local")
        self.assertTrue(self.espera(lambda: any("rechacé la sesión" in l for l in la), 30), self.texto(la))
        self.assertIsNone(pa.poll(), "el agente sigue esperando")

    # --- cortes
    def test_matar_el_agente_con_todo_corriendo_agmen_run_sigue_reintenta_y_lo_dice(self):
        # 3c: perder un agente ya no detiene todo: agmen run lo dice, sigue con lo local y reintenta hasta Ctrl+C
        pa, la, clave = self.agente()
        pr, lr = self.correr(clave=clave)
        self.assertTrue(self.espera(lambda: any("robot/motor │ HOLA motor" in l for l in lr) or pr.poll() is not None, 60),
                        self.texto(lr))
        self.assertTrue(self.espera(lambda: self.vivo("ia") and self.vivo("motor"), 30))
        pa.kill()
        pa.wait()
        self.assertTrue(self.espera(lambda: any("se perdió la conexión" in l for l in lr), 30), self.texto(lr))
        t_perdio = time.monotonic()
        self.assertIsNone(pr.poll(), "agmen run ya no sale por perder un agente\n" + self.texto(lr))
        self.assertTrue(self.vivo("ia"), "lo local sigue corriendo")
        # sigue vivo pasados ~12 s (la gracia de 10 s ya acabó), lo dice una vez y no escribe una línea por intento
        self.assertTrue(self.espera(lambda: any("ya pasaron 10 s" in l for l in lr), 30), self.texto(lr))
        self.assertTrue(self.espera(lambda: time.monotonic() - t_perdio >= 12.0, 15))
        self.assertIsNone(pr.poll(), "agmen run salió pasada la gracia\n" + self.texto(lr))
        self.assertTrue(self.vivo("ia"), "lo local sigue corriendo pasada la gracia")
        t = self.texto(lr)
        self.assertEqual(sum("): se perdió la conexión (" in l for l in lr), 1, t)
        self.assertEqual(sum("ya pasaron 10 s" in l for l in lr), 1, t)
        propias = [l for l in lr if re.match(r"agmen\s+│ robot", l) and "): conectado" not in l]
        self.assertEqual(len(propias), 2, "agmen run dice solo la pérdida y el 'ya pasaron': nada por intento\n" + t)
        self.assertEqual(self.detener(pr), 0, self.texto(lr))
        t = self.texto(lr)
        self.assertIn("reintento", t)
        self.assertIn("robot no se enteró (sin conexión)", t)
        self.assertNotIn("Traceback", t)
        self.assertTrue(self.espera(lambda: not self.vivo("ia"), 30), "Ctrl+C detiene lo local")
        # (los nodos del agente quedaron huérfanos porque se mató al agente sin avisarle: los limpia _limpiar)

    def test_matar_agmen_run_el_agente_detiene_sus_nodos_y_acepta_otra(self):
        pa, la, clave = self.agente()
        pr, lr = self.correr(clave=clave)
        self.assertTrue(self.espera(lambda: any("robot/motor │ HOLA motor" in l for l in lr) or pr.poll() is not None, 60),
                        self.texto(lr))
        self.assertTrue(self.espera(lambda: self.vivo("motor") and self.vivo("imu"), 30))
        pr.kill()
        pr.wait()
        self.assertTrue(self.espera(lambda: not self.vivo("motor") and not self.vivo("imu"), 30), "el agente no detuvo sus nodos")
        self.assertTrue(self.espera(lambda: any("cerró la conexión sin despedirse" in l for l in la), 30), self.texto(la))
        self.assertTrue(self.espera(lambda: any("(otra vez)" in l for l in la), 30), self.texto(la))
        pr2, lr2 = self.correr(clave=clave)
        self.assertTrue(self.espera(lambda: any("robot/motor │ HOLA motor" in l for l in lr2) or pr2.poll() is not None, 60),
                        self.texto(lr2))
        self.assertIsNone(pr2.poll(), self.texto(lr2))

    # --- el panel
    def test_panel_detener_y_arrancar_un_nodo_remoto(self):
        self.write_toml(web=f'listen = "127.0.0.1:{free_udp_port()}"\n')
        pa, la, clave = self.agente()
        http = free_tcp_port()
        pr, lr = self.correr(clave=clave, args=("--http", f"127.0.0.1:{http}"))
        self.assertTrue(self.espera(lambda: any("panel web:" in l for l in lr) or pr.poll() is not None, 60), self.texto(lr))
        self.assertIsNone(pr.poll(), self.texto(lr))
        base = f"http://127.0.0.1:{http}"

        def estado():
            return json.loads(urllib.request.urlopen(base + "/api/state", timeout=5).read())

        def nodo(n):
            return next(x for x in estado()["nodes"] if x["name"] == n)

        def post(ruta):
            req = urllib.request.Request(base + ruta, data=b"{}", method="POST",
                                         headers={"X-Agmen": "1", "Content-Type": "application/json"})
            return urllib.request.urlopen(req, timeout=5).status

        self.assertTrue(self.espera(lambda: nodo("motor")["state"] == "corriendo", 30), self.texto(lr))
        self.assertEqual(nodo("motor").get("machine"), "robot")
        self.assertIsNone(nodo("ia").get("machine"))
        # "corriendo" sale al lanzar el proceso, antes de su primer latido: imu tiene que estar vivo ANTES de
        # detener motor para que "detener motor no toca imu" signifique algo
        self.assertTrue(self.espera(lambda: self.vivo("imu"), 30), self.texto(lr))
        self.assertEqual(post("/api/node/motor/stop"), 200)
        self.assertTrue(self.espera(lambda: nodo("motor")["state"] == "detenido", 30))
        self.assertTrue(self.espera(lambda: not self.vivo("motor"), 30))
        self.assertTrue(self.vivo("imu"))
        self.assertEqual(post("/api/node/motor/start"), 200)
        self.assertTrue(self.espera(lambda: nodo("motor")["state"] == "corriendo", 30))
        self.assertTrue(self.espera(lambda: self.vivo("motor"), 30))
        self.assertEqual(post("/api/node/motor/restart"), 200)
        self.assertTrue(self.espera(lambda: len((self.marks / "arranque_motor").read_text().split()) >= 3, 30))
        self.assertEqual(self.detener(pr), 0, self.texto(lr))


class TestCtrlCAlArrancar(E2EBase):
    """Ctrl+C mientras agmen run arranca (RFC 0011, parte 3b; regla 1): sale con 130, sin traceback, dice en qué
    fase se canceló y no deja vivo nada de lo que ya arrancó."""

    def test_ctrl_c_durante_before_cancela_avisa_al_agente_y_mata_al_before(self):
        # el before solo en el robot.toml de agmen run (el agente corre el [robot] before de SU robot.toml)
        antes = json.dumps(["python", str(self.dir / "nodo.py"), "antes", str(self.marks)])
        toml_run = self.dir / "robot_run.toml"
        toml_run.write_text(self.toml.read_text(encoding="utf-8").replace(
            '[robot]\nname = "e2e"\n', f'[robot]\nname = "e2e"\nbefore = [{antes}]\n', 1), encoding="utf-8")
        pa, la, clave = self.agente()
        pr, lr = self.correr(clave=clave, toml=toml_run)
        # el before es un nodo de prueba que late hasta 120 s: corre mientras no lo maten
        self.assertTrue(self.espera(lambda: self.vivo("antes") or pr.poll() is not None, 60), self.texto(lr))
        self.assertIsNone(pr.poll(), self.texto(lr))
        codigo = self.detener(pr)        # SIGINT (en Windows, Ctrl+Break al grupo) en plena fase de before
        texto = self.texto(lr)
        self.assertEqual(codigo, 130, texto)
        self.assertIn(f"robot (127.0.0.1:{self.port}): conectado", texto)
        self.assertIn("agmen │ cancelado (Ctrl+C) mientras corría [robot] before: no arrancó ningún nodo "
                      "(les avisé a las otras computadoras)", texto)
        self.assertNotIn("Traceback", texto)
        self.assertNotIn("KeyboardInterrupt", texto)
        self.assertNotIn("todo arrancado", texto)
        # el comando de before no quedó vivo (aquí se espera: el proceso muerto puede seguir un rato como zombi,
        # con pid, hasta que alguien lo recoja; su latido sí se detuvo)
        self.assertTrue(self.espera(lambda: not self.vivo("antes"), 5.0), f"el before quedó vivo: {self.pids_de_marcas()}")
        # ningún nodo arrancó, ni aquí ni en el agente; y el agente recibió adios: queda esperando otra vez
        self.assertEqual(sorted(p.name for p in self.marks.glob("arranque_*")), ["arranque_antes"])
        self.assertTrue(self.espera(lambda: any("(otra vez)" in l for l in la), 30), self.texto(la))
        self.assertIsNone(pa.poll(), self.texto(la))

    def test_ctrl_c_al_terminar_de_arrancar_los_nodos_los_detiene(self):
        """Ctrl+C justo después de start_all y antes de esperar en _wait_for_ctrl_c. Esa ventana dura
        microsegundos: para que sea determinista, agmen run corre aquí (en este proceso) y start_all se envuelve
        para mandar SIGINT al propio proceso en cuanto arrancó los nodos."""
        from agmen import cli
        from agmen import run as runmod

        nodo = json.dumps(str(self.dir / "nodo.py"))
        marks = json.dumps(str(self.marks))
        self.toml.write_text(
            f'[robot]\nname = "e2e"\n\n'
            f'[[node]]\nname = "ia"\ncmd = ["python", {nodo}, "ia", {marks}]\n\n'
            f'[[node]]\nname = "cam"\ncmd = ["python", {nodo}, "cam", {marks}]\n\n'
            f'[hub]\nenabled = false\n\n[web]\nenabled = false\n', encoding="utf-8")
        original = runmod.Launcher.start_all
        lanzadores = []

        def start_all_y_ctrl_c(launcher):
            lanzadores.append(launcher)
            original(launcher)
            signal.raise_signal(signal.SIGINT)

        def matar_los_que_queden():   # si la prueba falla, que no queden nodos de este proceso
            for launcher in lanzadores:
                for n in launcher.nodes.values():
                    if n.proc is not None and n.proc.poll() is None:
                        n.proc.kill()
                        n.proc.wait()

        self.addCleanup(matar_los_que_queden)
        antes = signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGINT, antes)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(runmod.Launcher, "start_all", start_all_y_ctrl_c), \
                mock.patch.dict(os.environ, {"AGMEN_REMOTE_HOSTS": "", "NO_COLOR": "1"}), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            try:
                codigo = cli.main(["run", str(self.toml), "--no-browser"])
            except KeyboardInterrupt:
                codigo = "KeyboardInterrupt sin atrapar"
        texto = out.getvalue() + err.getvalue()
        self.assertEqual(codigo, 130, texto)
        self.assertEqual(len(lanzadores), 1, texto)
        nodos = lanzadores[0].nodes
        self.assertEqual(sorted(nodos), ["cam", "ia"])
        self.assertTrue(all(n.proc is not None for n in nodos.values()), "no arrancó algún nodo: la prueba no prueba nada")
        self.assertEqual([n for n, x in nodos.items() if x.proc.poll() is None], [], f"quedaron nodos vivos\n{texto}")
        self.assertTrue(self.ninguno_vivo(("ia", "cam")), f"quedaron nodos vivos: {self.pids_de_marcas()}")
        self.assertIn("agmen │ cancelado (Ctrl+C) mientras arrancaba: detuve todo lo que alcanzó a arrancar", texto)
        self.assertIn("listo. ¡Hasta luego!", texto)
        self.assertNotIn("todo arrancado", texto)
        self.assertNotIn("Traceback", texto)
        self.assertIs(signal.getsignal(signal.SIGINT), antes, "agmen run no dejó el manejador de SIGINT como estaba")

    def test_si_el_panel_no_abre_se_despide_del_agente_y_sale_con_1(self):
        # un puerto ocupado de verdad (en Windows, exclusivo: SO_REUSEADDR de http.server no se lo salta)
        ocupado = socket.socket()
        self.addCleanup(ocupado.close)
        if os.name == "nt":
            ocupado.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        ocupado.bind(("127.0.0.1", 0))
        ocupado.listen()
        http = f"127.0.0.1:{ocupado.getsockname()[1]}"
        self.write_toml(web=f'http = "{http}"\n')
        pa, la, clave = self.agente()
        pr, lr = self.correr(clave=clave, args=())
        codigo = self.fin(pr, 60)
        texto = self.texto(lr)
        self.assertEqual(codigo, 1, texto)
        self.assertIn(f"no se pudo abrir el panel web en {http}", texto)
        self.assertIn(f"robot (127.0.0.1:{self.port}): conectado", texto)
        self.assertNotIn("Traceback", texto)
        self.assertNotIn("todo arrancado", texto)
        # el agente recibió adios (no un corte), y queda esperando otra vez
        self.assertTrue(self.espera(lambda: any("la laptop (127.0.0.1) se despidió" in l for l in la), 30),
                        self.texto(la))
        self.assertTrue(self.espera(lambda: any("(otra vez)" in l for l in la), 30), self.texto(la))
        self.assertFalse(list(self.marks.glob("arranque_*")), "arrancó algún nodo")

    # --- agmen run aquí mismo (en este proceso), con lo lento simulado con mock

    def toml_local(self, maquina: bool = False) -> Path:
        nodo = json.dumps(str(self.dir / "nodo.py"))
        marks = json.dumps(str(self.marks))
        extra = (f'[[machine]]\nname = "robot"\nhost = "127.0.0.1:{self.port}"\n\n'
                 f'[[node]]\nname = "motor"\ncmd = ["python", {nodo}, "motor", {marks}]\nmachine = "robot"\n\n'
                 if maquina else "")
        self.toml.write_text(
            f'[robot]\nname = "e2e"\n\n{extra}'
            f'[[node]]\nname = "ia"\ncmd = ["python", {nodo}, "ia", {marks}]\n\n'
            f'[hub]\nenabled = false\n\n[web]\nenabled = false\n', encoding="utf-8")
        return self.toml

    def run_aqui(self, *args) -> tuple:
        """cli.main(["run", ...]) en este proceso: (código o "KeyboardInterrupt sin atrapar", salida, segundos)."""
        from agmen import cli

        self.addCleanup(signal.signal, signal.SIGINT, signal.getsignal(signal.SIGINT))
        out, err = io.StringIO(), io.StringIO()
        t0 = time.monotonic()
        with mock.patch.dict(os.environ, {"AGMEN_REMOTE_HOSTS": "", "NO_COLOR": "1", "AGMEN_AGENT_KEY": KEY,
                                          "AGMEN_CONFIG_DIR": str(self.dir / "cfg-run")}), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), \
                mock.patch("sys.stdin", io.StringIO()):
            try:
                codigo = cli.main(["run", str(self.toml), "--no-browser", *args])
            except KeyboardInterrupt:
                codigo = "KeyboardInterrupt sin atrapar"
        return codigo, out.getvalue() + err.getvalue(), time.monotonic() - t0

    def grabacion_lenta(self):
        """Un Player que tarda en abrir (como una grabación cortada de varios GB, que se recorre entera) y recibe
        Ctrl+C a la mitad. Sin poder cortarlo tardaría 10 s y diría un error en vez de cancelado."""
        class Lento:
            def __init__(self, *a, **kw):
                signal.raise_signal(signal.SIGINT)
                fin = time.monotonic() + 10
                while time.monotonic() < fin:
                    time.sleep(0.05)
                raise ValueError("terminó de abrirla: Ctrl+C no la cortó")

        return mock.patch("agmen.play.Player", Lento)

    def test_ctrl_c_mientras_abre_la_grabacion_la_corta(self):
        for maquina in (False, True):     # sin [[machine]] se abre tras before; con, antes de consultar la red
            with self.subTest(maquina=maquina):
                self.toml_local(maquina)
                with self.grabacion_lenta():
                    codigo, texto, dura = self.run_aqui("--play", str(self.dir / "grabacion.mcap"))
                self.assertEqual(codigo, 130, texto)
                self.assertIn("cancelado (Ctrl+C) mientras abría la grabación", texto)
                self.assertNotIn("Traceback", texto)
                self.assertLess(dura, 5, "Ctrl+C esperó a que terminara de abrir la grabación")
                self.assertFalse(list(self.marks.glob("arranque_*")), "arrancó algún nodo")

    def test_ctrl_c_durante_la_consulta_de_red_gana_aunque_la_direccion_salga_mal(self):
        self.toml_local(maquina=True)

        def consulta_con_ctrl_c(config):
            signal.raise_signal(signal.SIGINT)    # Ctrl+C mientras consulta (DNS): solo se anota
            return {}

        with mock.patch("agmen.machines.place_machines", consulta_con_ctrl_c), \
                mock.patch("agmen.machines.address_error", return_value="no encontré robot (DNS)"):
            codigo, texto, _ = self.run_aqui()
        self.assertEqual(codigo, 130, texto)
        self.assertIn("error: no encontré robot (DNS)", texto)
        self.assertIn("cancelado (Ctrl+C) antes de conectarme: no arrancó nada", texto)
        self.assertNotIn("Traceback", texto)
        self.assertFalse(list(self.marks.glob("arranque_*")), "arrancó algún nodo")

    def test_ctrl_c_anotado_justo_antes_de_una_fase_que_se_puede_cortar_no_se_pierde(self):
        from agmen import cli

        for s in (signal.SIGINT, *([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])):
            self.addCleanup(signal.signal, s, signal.getsignal(s))
        c = cli._CtrlC(threading.Event())
        c.catch()
        signal.raise_signal(signal.SIGINT)        # entre el "if ctrl_c.pressed" y el with: solo se anota
        self.assertTrue(c.pressed)
        entro = []
        with self.assertRaises(KeyboardInterrupt):
            with c.interruptible():
                entro.append(True)                # aquí estaría get_key esperando la clave
        self.assertEqual(entro, [], "entró a la fase aunque Ctrl+C ya se había anotado")
        # al salir vuelve el manejador que solo anota: tras KeyboardInterrupt, tras otra excepción y sin ninguna
        self.assertEqual(signal.getsignal(signal.SIGINT), c._note)
        c.pressed = False
        with self.assertRaises(OSError):
            with c.interruptible():
                self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)
                raise OSError("otra falla dentro de la fase")
        self.assertEqual(signal.getsignal(signal.SIGINT), c._note)
        with c.interruptible():
            pass
        self.assertEqual(signal.getsignal(signal.SIGINT), c._note)

    def test_fuera_del_hilo_principal_lo_dice_en_espanol_y_no_arranca_nada(self):
        self.toml_local()
        from agmen import cli

        errores, codigos, err = [], [], io.StringIO()

        def correr():
            try:
                with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", err):
                    codigos.append(cli.main(["run", str(self.toml), "--no-browser"]))
            except BaseException as e:   # noqa: BLE001 - se revisa abajo
                errores.append(e)

        h = threading.Thread(target=correr, daemon=True)
        h.start()
        h.join(30)
        self.assertFalse(h.is_alive(), "agmen run se quedó corriendo en el hilo")
        self.assertEqual(errores, [], "debe ser un error dicho en español, no una excepción")
        self.assertEqual(codigos, [2])
        self.assertIn("necesita el hilo principal", err.getvalue())
        self.assertIn("Ctrl+C", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertFalse(list(self.marks.glob("arranque_*")), "arrancó algún nodo")


# ===========================================================================
# 8. Segunda revisión: texto de un agente, AGMEN_REMOTE_HOSTS, Ctrl+C al conectar, computadora perdida
# ===========================================================================

class TestTextoQueLlegaDelAgente(LinkCase):
    SUCIO = "a\x1b[2Jb\x07c\nrobot/motor │ ✖ FALSO\rotra"

    def test_sesion_error_se_limpia_en_codigo_y_texto(self):
        def script(ch, ses):
            ch.send({"op": "sesion_error", "codigo": "x\x1b[31m" + "c" * 100, "texto": self.SUCIO * 400})

        falso = FakeAgent(agente_que_saluda_bien(script))
        self.addCleanup(falso.close)
        self.start_server()
        cfg, m = self.machine(port=falso.port)
        with self.assertRaises(agent.LinkError) as ctx:
            agent.AgentLink(m).open(KEY, agent.session_message(cfg, "robot", "real"))
        e = ctx.exception
        for t in (str(e), e.code):
            for c in t:
                self.assertTrue(c.isprintable(), repr(c))
        self.assertLessEqual(len(e.code), 41)
        self.assertLessEqual(len(str(e)), 2100)
        self.assertTrue(str(e).startswith("robot: "))

    def test_la_version_del_agente_se_limpia(self):
        falso = FakeAgent(lambda c: (lambda ch: (ch.recv(5), ch.send({"op": "sesion_ok", "maquina": "robot",
                                                                      "agmen": "x", "nodos": []}), time.sleep(3)))(
            agent_handshake(c, K(), version="9.9\x1b[31m" + "v" * 100)))
        self.addCleanup(falso.close)
        self.start_server()
        cfg, m = self.machine(port=falso.port)
        link = agent.AgentLink(m)
        self.addCleanup(link.close)
        link.open(KEY, agent.session_message(cfg, "robot", "real"))
        self.assertNotIn("\x1b", link.version)
        self.assertLessEqual(len(link.version), 41)

    def test_una_linea_enorme_o_con_control_del_agente_se_corta_y_limpia(self):
        def enviar(ch):
            ch.send({"op": "log", "nodo": "motor", "n": 1, "linea": "é" * 20000})
            ch.send({"op": "log", "nodo": "motor", "n": 2, "linea": self.SUCIO})
            ch.send({"op": "log", "nodo": 5, "n": 3, "linea": "nodo que no es texto"})
            ch.send({"op": "log", "nodo": ["motor"], "n": 3, "linea": "otro nodo raro"})
            ch.send({"op": "log", "nodo": "motor", "n": 4, "linea": 12345})
            ch.send({"op": "error", "texto": None})

        link, launcher, stream = TestEnlace.con_agente_falso(self, enviar)
        self.assertTrue(self.espera(lambda: len(launcher.logs.since(0)[0]) >= 6, 30), launcher.logs.since(0)[0])
        for l in launcher.logs.since(0)[0]:
            self.assertNotIn("\n", l["text"])
            self.assertNotIn("\x1b", l["text"])
            self.assertLessEqual(len(l["text"]), agent.LOG_LINE_MAX + 1)

    def con_agente_falso(self, enviar):          # (compartida con TestEnlace)
        return TestEnlace.con_agente_falso(self, enviar)

    def test_lo_que_manda_el_agente_se_puede_descartar_en_la_terminal_lenta(self):
        from agmen.run import Printer
        liberar = threading.Event()

        class Lenta:
            def __init__(self):
                self.lineas = []

            def write(self, t):
                liberar.wait(20)
                self.lineas.append(t)

            def flush(self):
                pass

        lenta = Lenta()
        pr = Printer(["a"], stream=lenta, max_queue=5)
        try:
            for i in range(30):
                pr("a", f"propia {i}", "agmen")                        # lo de aquí: nunca se descarta
            for i in range(30):
                pr("a", f"del agente {i}", "agmen", droppable=True)    # lo de un agente: sí
            for i in range(30):
                pr("a", f"error del agente {i}", "error", droppable=True)
            self.assertGreaterEqual(pr.dropped, 60)
            self.assertGreaterEqual(len(pr.queue), 30)
            self.assertFalse(any("del agente" in l for l in pr.queue if "propia" not in l) and len(pr.queue) > 40)
        finally:
            liberar.set()
            pr.close()
        todas = "".join(lenta.lineas)
        for i in range(30):
            self.assertIn(f"propia {i}\n", todas)
        self.assertRegex(todas, r"… \d+ líneas no cupieron en la terminal")

    def test_los_avisos_propios_de_remote_node_no_se_descartan(self):
        self.start_server()
        cfg, link, launcher = self.montar()
        llamadas = []
        grabadora = lambda *a, **k: llamadas.append((a, k))
        nodo = launcher.nodes["motor"]
        nodo.printer = grabadora
        nodo.start()           # sin open(): "no hay conexión" es un aviso propio de agmen run
        self.assertTrue(self.espera(lambda: llamadas, 30))
        self.assertEqual(llamadas[-1][1].get("droppable"), False)
        nodo._log("lo manda el agente", "out")
        self.assertEqual(llamadas[-1][1].get("droppable"), True)


class TestEstadosDelAgente(LinkCase):
    def test_un_estado_que_no_se_conoce_se_ignora_y_se_avisa_una_sola_vez(self):
        def enviar(ch):
            for malo in ("zzz", "CORRIENDO", "", None, 7, ["corriendo"], "corriendo ", "\x1b[31m"):
                ch.send({"op": "estado", "nodo": "motor", "estado": malo, "pid": 1, "codigo": None})
            ch.send({"op": "estado", "nodo": "imu", "estado": "corriendo", "pid": True, "codigo": False})

        link, launcher, stream = TestEnlace.con_agente_falso(self, enviar)
        self.assertTrue(self.espera(lambda: launcher.nodes["imu"].state == "corriendo", 30))
        self.assertEqual(launcher.nodes["motor"].state, "detenido")
        avisos = [l for l in launcher.logs.since(0)[0] if "estado que no conozco" in l["text"]]
        self.assertEqual(len(avisos), 1, "el aviso se da una vez por conexión")
        self.assertIn("'motor'", avisos[0]["text"])
        self.assertIsNone(launcher.nodes["imu"].remote_pid, "un pid booleano no es un pid")
        self.assertIsNone(launcher.nodes["imu"].exit_code)

    def test_node_states_son_los_del_launcher(self):
        self.assertEqual(set(agent.NODE_STATES), {"corriendo", "detenido", "falló", "reiniciando", "terminó"})
        # cada estado que run.Node usa en su código está permitido
        fuente = (ROOT / "python" / "agmen" / "run.py").read_text(encoding="utf-8")
        for estado in re.findall(r'self\.state = "([^"]+)"', fuente):
            self.assertIn(estado, agent.NODE_STATES)
        for estado in re.findall(r'self\.exit_code, self\.state = \w+, "([^"]+)"', fuente):
            self.assertIn(estado, agent.NODE_STATES)


class TestAgmenRemoteHosts(unittest.TestCase):
    def forzados(self, valor):
        return machines.forced_remote({"AGMEN_REMOTE_HOSTS": valor})

    def test_acepta_puerto_punto_final_y_mayusculas(self):
        self.assertEqual(self.forzados("127.0.0.2:7391"), {"127.0.0.2"})
        self.assertEqual(self.forzados("Robot.LOCAL."), {"robot.local"})
        self.assertEqual(self.forzados("ROBOT.local:7400"), {"robot.local"})
        self.assertEqual(self.forzados("[::1]:7391"), {"::1"})
        self.assertEqual(self.forzados("::1"), {"::1"})
        self.assertEqual(self.forzados("::ffff:127.0.0.1"), {"127.0.0.1"})
        self.assertEqual(self.forzados(" 127.0.0.1 , robot.local:7391 ,, "), {"127.0.0.1", "robot.local"})
        self.assertEqual(self.forzados(""), set())
        self.assertEqual(self.forzados(" , ,"), set())

    def test_entrada_que_no_se_puede_interpretar_es_error(self):
        for malo in ("bad host!", "999.1.1.1", "http://x", "a..b", "127.0.0.1,roto!,127.0.0.2", "a b",
                     "robot.local:x", "robot.local:99999", "[::1]:abc"):
            with self.subTest(malo=malo):
                with self.assertRaises(ValueError) as ctx:
                    self.forzados(malo)
                self.assertIn("AGMEN_REMOTE_HOSTS", str(ctx.exception))
                self.assertIn("ej. AGMEN_REMOTE_HOSTS=192.168.1.20,robot.local:7391", str(ctx.exception))

    def test_el_mensaje_nombra_la_entrada_una_vez_y_da_el_motivo(self):
        casos = {"http://robot.local": "http://", "robot.local:99999": "puerto", "robot.local:x": "puerto",
                 "[robot.local]": "corchetes", "[robot.local]:7391": "corchetes", "a b": "no es una IP ni un nombre"}
        for malo, motivo in casos.items():
            with self.subTest(malo=malo):
                with self.assertRaises(ValueError) as ctx:
                    self.forzados("127.0.0.1," + malo)
                msg = str(ctx.exception)
                self.assertEqual(msg.count(repr(malo)), 1, msg)
                self.assertEqual(msg.count("AGMEN_REMOTE_HOSTS:"), 1, msg)
                self.assertIn(motivo, msg)
                self.assertIn("ej. AGMEN_REMOTE_HOSTS=192.168.1.20,robot.local:7391", msg)

    def test_el_puerto_mal_escrito_de_una_ip_tambien_es_error(self):
        for malo in ("127.0.0.1:99999", "127.0.0.1:abc", "127.0.0.1:0", "127.0.0.1:"):
            with self.subTest(malo=malo):
                with self.assertRaises(ValueError):
                    self.forzados(malo)

    def test_el_puerto_en_la_entrada_se_ignora_al_forzar(self):
        with mock.patch.dict(os.environ, {"AGMEN_REMOTE_HOSTS": "127.0.0.1:7391"}):
            self.assertFalse(machines.check_hosts(["127.0.0.1"])["127.0.0.1"].here)

    def test_agmen_run_y_agmen_agent_con_la_variable_mala_salen_con_2_sin_traceback(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        toml = d / "robot.toml"
        toml.write_text('[robot]\nname = "r"\n[[machine]]\nname = "robot"\nhost = "127.0.0.1"\n'
                        '[[node]]\nname = "m"\ncmd = "x"\nmachine = "robot"\n[web]\nenabled = false\n', encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "AGMEN_REMOTE_HOSTS": "roto!!", "NO_COLOR": "1",
               "AGMEN_CONFIG_DIR": str(d / "cfg")}
        for args in (["run", str(toml), "--no-browser"], ["agent", str(toml), "--machine", "robot", "--port",
                                                              str(free_tcp_port())]):
            with self.subTest(args=args[0]):
                r = subprocess.run([sys.executable, "-m", "agmen", *args], env=env, capture_output=True, text=True,
                                   encoding="utf-8", timeout=60, stdin=subprocess.DEVNULL)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("error: AGMEN_REMOTE_HOSTS: la entrada 'roto!!' no se entiende", r.stderr)
                self.assertNotIn("Traceback", r.stderr)
        # otros comandos no se meten con esa variable
        r = subprocess.run([sys.executable, "-m", "agmen", "--version"], env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=60, stdin=subprocess.DEVNULL)
        self.assertNotIn("AGMEN_REMOTE_HOSTS", r.stderr)


class TestRunConAgentesDeMentira(E2EBase):
    """agmen run (subproceso) contra agentes falsos: Ctrl+C al conectar y una computadora que se pierde."""

    def falso_que_registra(self, demora: float = 0.0, cerrar_tras_ok: bool = False):
        recibidos: list[str] = []

        def script(ch, ses):
            recibidos.append(ses["op"])
            fin = time.monotonic() + demora
            while time.monotonic() < fin:        # mientras "prepara la sesión" sigue leyendo (ping, adios…)
                try:
                    m = ch.recv(0.2)
                except ChannelTimeout:
                    continue
                except ChannelError:
                    return
                recibidos.append(m["op"])
                if m["op"] == "adios":
                    return
                if m["op"] == "ping":
                    ch.send({"op": "pong", "t": m.get("t")})
            ch.send({"op": "sesion_ok", "maquina": "robot", "agmen": __version__, "nodos": list(ses["nodos"])})
            if cerrar_tras_ok:
                ch.close()
                return
            while True:
                try:
                    m = ch.recv(10)
                    recibidos.append(m["op"])
                    if m["op"] == "ping":
                        ch.send({"op": "pong", "t": m.get("t")})
                except ChannelError:
                    return

        f = FakeAgent(agente_que_saluda_bien(script))
        self.addCleanup(f.close)
        return f, recibidos

    def toml_con(self, puertos: dict[str, int], before: bool = False) -> Path:
        nodo = json.dumps(str(self.dir / "nodo.py"))
        marks = json.dumps(str(self.marks))
        maq = "".join(f'[[machine]]\nname = "{n}"\nhost = "127.0.0.1:{p}"\n\n' for n, p in puertos.items())
        nodos = "".join(f'[[node]]\nname = "n_{n}"\ncmd = ["python", {nodo}, "n_{n}", {marks}]\nmachine = "{n}"\n\n'
                        for n in puertos)
        antes = ""
        if before:
            (self.dir / "antes.py").write_text(
                f"import os\nopen(os.path.join({str(self.marks)!r}, 'before_corrio'), 'w').write('x')\n", encoding="utf-8")
            antes = f'before = [{json.dumps("python " + str(self.dir / "antes.py"))}]\n'
        t = self.dir / "robot2.toml"
        t.write_text(f'[robot]\nname = "r"\n{antes}\n{maq}{nodos}[[node]]\nname = "ia"\ncmd = ["python", {nodo}, "ia", {marks}]\n\n'
                     f'[hub]\nenabled = false\n[web]\nenabled = false\n', encoding="utf-8")
        return t

    def test_ctrl_c_mientras_se_conecta_cancela_avisa_adios_y_no_arranca_nada(self):
        falso, recibidos = self.falso_que_registra(demora=30)
        t = self.toml_con({"robot": falso.port})
        pr, lr = self.correr(clave=KEY, toml=t)
        self.assertTrue(self.espera(lambda: recibidos, 40), self.texto(lr))     # el agente recibió la sesión: se está conectando
        time.sleep(0.3)
        codigo = self.detener(pr, 40)
        # (en Windows la prueba manda Ctrl+Break: 0xC000013A sería la consola matando a agmen run sin avisar)
        self.assertEqual(codigo, 130, f"código {codigo!r} ({codigo & 0xFFFFFFFF:#x}); el agente recibió {recibidos}\n"
                                      f"{self.texto(lr)}" if isinstance(codigo, int) else
                         f"agmen run no salió en 40 s; el agente recibió {recibidos}\n{self.texto(lr)}")
        texto = self.texto(lr)
        self.assertIn("cancelado (Ctrl+C) mientras me conectaba", texto)
        self.assertNotIn("Traceback", texto)
        self.assertNotIn("KeyboardInterrupt", texto)
        self.assertTrue(self.espera(lambda: "adios" in recibidos, 30), f"el agente no recibió adios: {recibidos}")
        self.assertFalse(list(self.marks.iterdir()), "no debe arrancar nada")

    def test_una_computadora_que_se_pierde_justo_despues_de_abrirse_no_se_olvida(self):
        """cli._connect abre cada sesión y DESPUÉS asigna link.on_lost: si el agente se cae en medio, _lose() ve
        on_lost vacío y nadie se entera (agmen run seguiría: [robot] before, nodos locales…). Se fuerza la ventana
        con un open() que tarda un segundo en volver."""
        from agmen import cli
        from agmen.run import Launcher

        rapido, _ = self.falso_que_registra(cerrar_tras_ok=True)      # contesta sesion_ok y se cae
        lento, _ = self.falso_que_registra(demora=0.3)
        cfg = load_config(self.toml_con({"robot": rapido.port, "brazo": lento.port}))
        links = {m.name: agent.AgentLink(m) for m in cfg.machines}
        launcher = Launcher(cfg, stream=io.StringIO(), places={n: Place(False, "other") for n in links}, remotes=links)
        self.addCleanup(launcher.printer.close)
        perdidas, parar = [], threading.Event()
        original = agent.AgentLink.open

        def open_lento(self_, *a, **k):
            r = original(self_, *a, **k)
            time.sleep(1.0)               # la caída ocurre aquí, antes de que _connect asigne on_lost
            return r

        with mock.patch.object(agent.AgentLink, "open", open_lento):
            error = cli._connect(launcher, cfg, links, {n: (KEY, "variable") for n in links}, "real", perdidas, parar)
        self.assertTrue(links["robot"].lost, "el enlace sí supo que se perdió")
        self.assertTrue(perdidas or parar.is_set() or error,
                        "agmen run no se enteró de que una computadora se perdió mientras se conectaba otra")


def sin_control(texto: str, permitir_nl: bool = False) -> bool:
    """True si el texto no trae ningún carácter de control (ni \\r, \\x1b, \\x07…); \\n solo si se permite."""
    return all(c.isprintable() or (permitir_nl and c == "\n") for c in texto)


def servidor_crudo(respuesta: bytes, despues: float = 0.0):
    """Un servidor falso (sin clave) que lee el hola del cliente, contesta `respuesta` tal cual y cierra."""
    def comportamiento(c):
        buf = b""
        c.settimeout(10)
        while b"\n" not in buf:
            d = c.recv(1024)
            if not d:
                return
            buf += d
        c.sendall(respuesta)
        time.sleep(despues)
    return FakeAgent(comportamiento)


class TestSaludoAjenoLimpio(LinkCase):
    """Quien conteste en el puerto (sin clave) no puede meter caracteres de control ni textos enormes en lo
    que ve la laptop: el error va en una línea y nombra la computadora."""

    def setUp(self):
        super().setUp()
        self.port = free_tcp_port()

    def abrir_contra(self, respuesta: bytes) -> agent.LinkError:
        falso = servidor_crudo(respuesta)
        self.addCleanup(falso.close)
        self.write_toml()
        cfg, m = self.machine(port=falso.port)
        with self.assertRaises(agent.LinkError) as ctx:
            agent.AgentLink(m).open(KEY, agent.session_message(cfg, "robot", "real"))
        return ctx.exception

    def revisar(self, e, largo=1500):
        t = str(e)
        self.assertTrue(sin_control(t), repr(t))
        self.assertTrue(sin_control(e.code), repr(e.code))
        self.assertLessEqual(len(t), largo, len(t))
        self.assertIn("robot", t)

    def test_error_del_saludo_con_retorno_de_carro_y_escape(self):
        e = self.abrir_contra(b"error version texto\rerror: todo bien\x1b[2J\n")
        self.revisar(e)
        self.assertIn("texto", str(e))

    def test_version_del_hola_con_escape(self):
        e = self.abrir_contra(b"agmen-agent/9\x1b[2J\rX hola 00 v\n")
        self.revisar(e)

    def test_codigo_de_error_desconocido_no_se_imprime(self):
        e = self.abrir_contra(b"error loquesea\x1b[31m texto\n")
        self.revisar(e)
        self.assertNotIn("loquesea", str(e))
        self.assertNotIn("loquesea", e.code)

    def test_texto_enorme_se_corta(self):
        for resp in (b"error version " + b"A" * 10000 + b"\n", b"error loquesea " + b"B\x1b" * 5000 + b"\n",
                     b"agmen-agent/" + b"9" * 10000 + b" hola 00 v\n"):
            with self.subTest(resp=resp[:20]):
                self.revisar(self.abrir_contra(resp), largo=1000)

    def test_agmen_run_de_punta_a_punta_sale_con_2_sin_caracteres_de_control(self):
        falso = servidor_crudo(b"error version texto\rerror: todo bien\x1b[2J\n")
        self.addCleanup(falso.close)
        d = self.dir
        toml = d / "r2.toml"
        toml.write_text(f'[robot]\nname = "r"\n[[machine]]\nname = "robot"\nhost = "127.0.0.1:{falso.port}"\n'
                        '[[node]]\nname = "m"\ncmd = ["python", "x"]\nmachine = "robot"\n[hub]\nenabled = false\n'
                        '[web]\nenabled = false\n', encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1",
               "AGMEN_CONFIG_DIR": str(d / "cfg"), "AGMEN_REMOTE_HOSTS": "127.0.0.1", "AGMEN_AGENT_KEY": KEY,
               "AGMEN_HUB": f"127.0.0.1:{free_udp_port()}"}
        r = subprocess.run([sys.executable, "-m", "agmen", "run", str(toml), "--no-browser", "--no-web"], env=env,
                           capture_output=True, text=True, encoding="utf-8", timeout=90, stdin=subprocess.DEVNULL)
        salida = r.stdout + r.stderr
        self.assertEqual(r.returncode, 2, salida)
        self.assertTrue(sin_control(salida, permitir_nl=True), repr(salida))
        self.assertNotIn("Traceback", salida)
        self.assertIn("robot", salida)


class TestPerdidaCortaLaEspera(E2EBase):
    """Una computadora que se pierde corta la espera de otra que tarda en preparar su sesión."""

    def falso(self, demora=0.0, cerrar_a: float | None = None):
        recibidos: list[str] = []

        def script(ch, ses):
            recibidos.append(ses["op"])
            fin = time.monotonic() + demora
            while time.monotonic() < fin:
                try:
                    m = ch.recv(0.2)
                except ChannelTimeout:
                    continue
                except ChannelError:
                    return
                recibidos.append(m["op"])
                if m["op"] == "adios":
                    return
                if m["op"] == "ping":
                    ch.send({"op": "pong", "t": m.get("t")})
            ch.send({"op": "sesion_ok", "maquina": "x", "agmen": __version__, "nodos": list(ses["nodos"])})
            fin = time.monotonic() + (cerrar_a if cerrar_a is not None else 60)
            while time.monotonic() < fin:
                try:
                    m = ch.recv(0.2)
                except ChannelTimeout:
                    continue
                except ChannelError:
                    return
                recibidos.append(m["op"])
                if m["op"] == "ping":
                    ch.send({"op": "pong", "t": m.get("t")})
            ch.close()

        f = FakeAgent(agente_que_saluda_bien(script))
        self.addCleanup(f.close)
        return f, recibidos

    def toml_dos(self, puertos: dict) -> Path:
        return TestRunConAgentesDeMentira.toml_con(self, puertos, before=True)

    def test_agmen_run_no_espera_a_la_lenta_y_no_corre_nada_local(self):
        a, _ = self.falso(cerrar_a=0.5)
        robot, rec = self.falso(demora=25)
        t = self.toml_dos({"a": a.port, "robot": robot.port})
        t0 = time.monotonic()
        p, lineas = self.correr(clave=KEY, toml=t)
        self.assertTrue(self.espera(lambda: p.poll() is not None, 20), "agmen run siguió esperando a robot\n"
                        + self.texto(lineas))
        self.assertLess(time.monotonic() - t0, 20)
        self.assertEqual(p.returncode, 1, self.texto(lineas))
        self.assertTrue(self.espera(lambda: "adios" in rec, 15), f"robot no recibió adios: {rec}")
        texto = self.texto(lineas)
        self.assertNotIn("robot (127.0.0.1:" + str(robot.port) + "): conectado", texto)
        self.assertNotIn("Traceback", texto)
        self.assertFalse((self.marks / "before_corrio").exists(), "corrió el [robot] before local")
        self.assertFalse(list(self.marks.glob("arranque_*")), "arrancó un nodo")

    def test_una_sola_computadora_perdida_tras_sesion_ok_no_dice_conectado(self):
        from agmen import cli
        from agmen.run import Launcher

        a, _ = self.falso(cerrar_a=0.0)
        cfg = load_config(self.toml_dos({"a": a.port}))
        links = {m.name: agent.AgentLink(m) for m in cfg.machines}
        salida = io.StringIO()
        launcher = Launcher(cfg, stream=salida, places={n: Place(False, "other") for n in links}, remotes=links)
        self.addCleanup(launcher.printer.close)
        perdidas, parar = [], threading.Event()
        original = agent.AgentLink.open

        def open_lento(self_, *args, **kw):
            r = original(self_, *args, **kw)
            self.espera(lambda: self_.lost, 10)     # la caída se nota antes de que _connect siga
            return r

        with mock.patch.object(agent.AgentLink, "open", open_lento):
            cli._connect(launcher, cfg, links, {n: (KEY, "variable") for n in links}, "real", perdidas, parar)
        launcher.printer.close()
        self.assertTrue(links["a"].lost)
        self.assertTrue(perdidas or parar.is_set())
        self.assertNotIn("conectado", salida.getvalue())


class TestAdiosNoSeBloquea(LinkCase):
    """El adios de salida es de cortesía: un agente callado o que no lee no detiene la salida."""
    TOPE = 3.0   # el adios espera 0.5 s (BYE_TIMEOUT); sin ese límite serían los 6 s del socket. Holgado para un runner cargado

    def setUp(self):
        super().setUp()
        self.port = free_tcp_port()

    def test_close_con_el_buffer_de_envio_lleno(self):
        def script(ch, ses):
            ch.send({"op": "sesion_ok", "maquina": "robot", "agmen": __version__, "nodos": list(ses["nodos"])})
            time.sleep(30)          # deja de leer

        falso = FakeAgent(agente_que_saluda_bien(script))
        self.addCleanup(falso.close)
        self.write_toml()
        cfg, m = self.machine(port=falso.port)
        link = agent.AgentLink(m)
        link.open(KEY, agent.session_message(cfg, "robot", "real"))
        self.addCleanup(link.close)
        s = link.channel.sock
        s.setblocking(False)
        llenos = 0
        while llenos < 200000:       # se llena el buffer hacia el agente (que no lee)
            try:
                s.send(b"x" * 65536)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            llenos += 1
        s.settimeout(6)
        t0 = time.monotonic()
        link.close()
        self.assertLess(time.monotonic() - t0, self.TOPE)

    def test_open_que_sale_por_stop_con_el_agente_callado(self):
        falso = FakeAgent(agente_que_saluda_bien(lambda ch, ses: time.sleep(30)))
        self.addCleanup(falso.close)
        self.write_toml()
        cfg, m = self.machine(port=falso.port)
        stop = threading.Event()
        threading.Timer(0.5, stop.set).start()
        link = agent.AgentLink(m)
        t0 = time.monotonic()
        with self.assertRaises(agent.LinkError) as ctx:
            link.open(KEY, agent.session_message(cfg, "robot", "real"), stop=stop)
        self.assertLess(time.monotonic() - t0, 0.5 + self.TOPE)
        self.assertEqual(ctx.exception.code, "detenido")
        t0 = time.monotonic()
        link.close()
        self.assertLess(time.monotonic() - t0, self.TOPE)


class TestAdiosCierraLimpio(LinkCase):
    """Tras el adios, agmen run lee lo que el agente aún mande y cierra sin RST. Con RST, en
    Windows el agente pierde lo que no había leído (el adios) y dice "cerró la conexión sin
    despedirse" (falló en el CI de Windows: el panel no abre y agmen run sale con 1). En Linux el
    RST no borra nada, pero se ve: el agente recibe ConnectionResetError en vez de un fin normal."""
    TOPE = 3.0

    def setUp(self):
        super().setUp()
        self.port = free_tcp_port()
        self.write_toml()

    def agente(self, sesion_ok: bool):
        """Contesta tarde (después del adios, como los estado/log de detener_todo cuando agmen run
        ya se despidió), cierra su lado y cuenta cómo cerró agmen run: 'fin' o 'rst: …'."""
        resultado: list = []
        ops: list = []

        def script(ch, ses):
            if sesion_ok:
                ch.send({"op": "sesion_ok", "maquina": "robot", "agmen": __version__, "nodos": list(ses["nodos"])})
            try:
                while True:
                    op = ch.recv(10)["op"]
                    if op != "ping":
                        ops.append(op)
                    if op == "adios":
                        break
                time.sleep(0.1)   # agmen run ya mandó el adios: lo de antes ya habría cerrado
                ch.send({"op": "log", "nodo": "agmen", "linea": "la laptop pidió detener todo"})
                ch.send({"op": "estado", "nodo": "motor", "estado": "detenido"})
                time.sleep(0.1)
                ch.sock.shutdown(socket.SHUT_WR)
                ch.sock.settimeout(5)
                while ch.sock.recv(4096):
                    pass
                resultado.append("fin")
            except (OSError, AgentError) as e:
                resultado.append(f"rst: {e!r}")

        falso = FakeAgent(agente_que_saluda_bien(script))
        self.addCleanup(falso.close)
        return falso, ops, resultado

    def test_finish_lee_lo_que_llega_despues_del_adios(self):
        falso, ops, resultado = self.agente(sesion_ok=True)
        cfg, m = self.machine(port=falso.port)
        link = agent.AgentLink(m)
        link.open(KEY, agent.session_message(cfg, "robot", "real"))
        self.addCleanup(link.close)
        t0 = time.monotonic()
        link.finish()   # sin nodos corriendo: detener_todo y adios enseguida (el camino del panel que no abre)
        self.assertLess(time.monotonic() - t0, self.TOPE)
        self.assertTrue(self.espera(lambda: resultado, 10))
        self.assertEqual(ops, ["detener_todo", "adios"])
        self.assertEqual(resultado, ["fin"], "agmen run cerró con RST: en Windows el agente perdería el adios")
        self.assertEqual(link.lost, "")

    def test_open_que_sale_por_stop_lee_lo_que_llega_despues_del_adios(self):
        falso, ops, resultado = self.agente(sesion_ok=False)
        cfg, m = self.machine(port=falso.port)
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        link = agent.AgentLink(m)
        with self.assertRaises(agent.LinkError) as ctx:
            link.open(KEY, agent.session_message(cfg, "robot", "real"), stop=stop)
        self.assertEqual(ctx.exception.code, "detenido")
        self.assertTrue(self.espera(lambda: resultado, 10))
        self.assertEqual(ops, ["adios"])
        self.assertEqual(resultado, ["fin"], "agmen run cerró con RST: en Windows el agente perdería el adios")

    def test_nunca_dos_lectores_si_el_lector_sigue_vivo(self):
        """Si el join del lector vuelve con el lector vivo (p. ej. el temporizador de Windows lo
        despierta un poco antes del plazo), _bye no lee del socket: le robaría bytes al lector."""
        class Espia(socket.socket):
            recvs = 0

            def recv(self, *a, **k):
                type(self).recvs += 1
                return super().recv(*a, **k)

        class LectorVivo:   # join vuelve enseguida y sigue vivo
            def join(self, timeout=None):
                pass

            def is_alive(self):
                return True

        a, b = socket.socketpair()
        self.addCleanup(b.close)
        s = Espia(fileno=a.detach())
        b.sendall(b"1 {\"op\":\"log\"} firma\n")   # algo del agente, pendiente para el lector

        class Canal:
            sock = s

            def send(self, obj):
                s.sendall(b"adios\n")

            def close(self):
                s.close()

        agent._bye(Canal(), LectorVivo())
        self.assertEqual(Espia.recvs, 0, "_bye leyó del socket con el lector vivo: dos lectores")
        self.assertEqual(s.fileno(), -1, "_bye no cerró el socket")


@unittest.skipUnless(os.name == "posix", "necesita una terminal de verdad (pty): solo POSIX")
class TestTerminalInteractiva(E2EBase):
    """agmen run con una terminal interactiva (pty): pide la clave sin mostrarla y la guarda; Ctrl+C al pedirla."""

    def run_en_pty(self, extra_env: dict | None = None):
        import pty

        maestro, esclavo = pty.openpty()
        env = self.env("run", AGMEN_REMOTE_HOSTS="127.0.0.1", **(extra_env or {}))
        p = subprocess.Popen([sys.executable, "-m", "agmen", "run", str(self.toml), "--no-browser", "--no-web"],
                             env=env, stdin=esclavo, stdout=esclavo, stderr=esclavo, cwd=self.dir,
                             start_new_session=True)
        os.close(esclavo)
        self.procs.append(p)
        salida = bytearray()

        def leer():
            while True:
                try:
                    d = os.read(maestro, 4096)
                except OSError:
                    return
                if not d:
                    return
                salida.extend(d)

        threading.Thread(target=leer, daemon=True).start()
        self.addCleanup(lambda: os.close(maestro))
        return p, maestro, salida

    def ver(self, salida) -> str:
        return bytes(salida).decode("utf-8", "replace").replace("\r", "")

    def test_pide_la_clave_sin_mostrarla_la_guarda_y_la_proxima_vez_no_pregunta(self):
        pa, la, clave = self.agente()
        p, maestro, salida = self.run_en_pty()
        self.assertTrue(self.espera(lambda: "clave de agmen agent en 'robot'" in self.ver(salida), 60), self.ver(salida))
        os.write(maestro, (clave + "\n").encode())
        self.assertTrue(self.espera(lambda: "todo arrancado" in self.ver(salida), 60), self.ver(salida))
        texto = self.ver(salida)
        self.assertNotIn(clave, texto, "la clave tecleada no debe verse en pantalla")
        self.assertNotIn(clave.replace("-", ""), texto)
        self.assertIn("guardé la clave de robot", texto)
        self.assertEqual(self.detener(p), 0, texto)
        with mock.patch.dict(os.environ, {"AGMEN_CONFIG_DIR": str(self.dir / "cfg-run")}):
            self.assertEqual(agent.client_key(agent.machine_address("127.0.0.1", self.port)), clave)
        self.assertEqual((self.dir / "cfg-run" / "keys.toml").read_text(encoding="utf-8").count(clave), 1)
        # la segunda vez no pregunta
        p2, maestro2, salida2 = self.run_en_pty()
        self.assertTrue(self.espera(lambda: "todo arrancado" in self.ver(salida2), 60), self.ver(salida2))
        self.assertNotIn("clave de agmen agent en 'robot'", self.ver(salida2))
        self.assertEqual(self.detener(p2), 0)

    def test_clave_tecleada_mal_se_vuelve_a_pedir_y_una_rechazada_no_se_guarda(self):
        pa, la, clave = self.agente()
        p, maestro, salida = self.run_en_pty()
        self.assertTrue(self.espera(lambda: "clave de agmen agent en 'robot'" in self.ver(salida), 60))
        os.write(maestro, b"esto no es una clave\n")                  # mal escrita: se pide otra vez
        self.assertTrue(self.espera(lambda: "la clave tiene" in self.ver(salida) or "caracteres que no lleva" in self.ver(salida), 30),
                        self.ver(salida))
        os.write(maestro, b"aaaa-bbbb-cccc\n")                        # bien escrita pero no es la del agente
        self.assertEqual(p.wait(60), 2, self.ver(salida))
        texto = self.ver(salida)
        self.assertIn("rechazó la clave", texto)
        self.assertNotIn("aaaa-bbbb-cccc", texto)
        self.assertFalse((self.dir / "cfg-run" / "keys.toml").exists(), "una clave rechazada no se guarda")
        self.assertFalse((self.marks / "arranque_ia").exists())

    def test_ctrl_c_al_pedir_la_clave_cancela_sin_traceback_y_no_arranca_nada(self):
        pa, la, clave = self.agente()
        p, maestro, salida = self.run_en_pty()
        self.assertTrue(self.espera(lambda: "clave de agmen agent en 'robot'" in self.ver(salida), 60), self.ver(salida))
        p.send_signal(signal.SIGINT)
        self.assertEqual(p.wait(60), 130, self.ver(salida))
        texto = self.ver(salida)
        self.assertIn("cancelado (Ctrl+C) antes de conectarme", texto)
        self.assertNotIn("Traceback", texto)
        self.assertNotIn("KeyboardInterrupt", texto)
        self.assertFalse(list(self.marks.iterdir()))
        self.assertFalse((self.dir / "cfg-run" / "keys.toml").exists())

    def test_ctrl_d_al_pedir_la_clave_cancela_sin_traceback_y_no_arranca_nada(self):
        pa, la, clave = self.agente()
        p, maestro, salida = self.run_en_pty()
        self.assertTrue(self.espera(lambda: "clave de agmen agent en 'robot'" in self.ver(salida), 60), self.ver(salida))
        os.write(maestro, b"\x04")                                   # Ctrl+D: la terminal cierra la entrada (EOF)
        self.assertEqual(p.wait(60), 130, self.ver(salida))
        texto = self.ver(salida)
        self.assertIn("cancelado (Ctrl+D) antes de conectarme: no arrancó nada", texto)
        self.assertNotIn("Traceback", texto)
        self.assertNotIn("EOFError", texto)
        self.assertFalse(list(self.marks.iterdir()))
        self.assertFalse((self.dir / "cfg-run" / "keys.toml").exists())


# ===========================================================================
# Parte 3c §5: la clave del agente como servicio (agmen agent --show-key) y la línea de espera sin la clave
# ===========================================================================

FORMA_CLAVE = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$")


class TestClaveDelAgenteComoServicio(E2EBase):
    """agmen agent --show-key: imprime solo la clave y sale, sin robot.toml ni puertos; y con la salida por una
    tubería (journalctl) la línea de espera no lleva la clave."""

    def cfg_env(self) -> dict:
        return self.env("agente")

    def show(self, *args: str, env: dict | None = None, cwd: Path | None = None):
        return subprocess.run([sys.executable, "-m", "agmen", "agent", "--show-key", *args], env=env or self.cfg_env(),
                              capture_output=True, text=True, encoding="utf-8", timeout=60, stdin=subprocess.DEVNULL,
                              cwd=cwd or self.dir)

    def crear_clave(self) -> str:
        """Crea la clave de la carpeta cfg-agente como lo hace agmen agent la primera vez (sin abrir puertos)."""
        with mock.patch.dict(os.environ, {"AGMEN_CONFIG_DIR": str(self.dir / "cfg-agente"), "AGMEN_AGENT_KEY": ""}):
            clave, creada = agent.load_agent_key()
            self.assertTrue(creada)
            return clave

    def archivo_clave(self) -> Path:
        return self.dir / "cfg-agente" / "agent.key"

    def sin_robot_toml(self) -> Path:
        carpeta = self.dir / "vacia"
        carpeta.mkdir()
        self.addCleanup(shutil.rmtree, carpeta, True)
        return carpeta

    def test_con_clave_imprime_solo_la_clave_y_sale_con_0_sin_robot_toml(self):
        clave = self.crear_clave()
        vacia = self.sin_robot_toml()
        r = self.show(cwd=vacia)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, clave + "\n", "stdout debe ser solo la clave")
        self.assertRegex(r.stdout.strip(), FORMA_CLAVE)
        self.assertEqual(r.stderr, "", "sin avisos cuando no se le da nada más")
        self.assertEqual(list(vacia.iterdir()), [], "no debe crear archivos junto a donde corre")

    def test_no_abre_ningun_puerto_ni_crea_ni_cambia_la_clave(self):
        import contextlib
        from agmen import cli
        clave = self.crear_clave()
        antes = self.archivo_clave().read_bytes()
        vacia = self.sin_robot_toml()
        salida, err = io.StringIO(), io.StringIO()

        def prohibido(*a, **k):
            raise AssertionError("--show-key no debe abrir ni escuchar en ningún puerto")

        viejo = os.getcwd()
        os.chdir(vacia)
        self.addCleanup(os.chdir, viejo)
        with mock.patch.dict(os.environ, {"AGMEN_CONFIG_DIR": str(self.dir / "cfg-agente"), "AGMEN_AGENT_KEY": ""}), \
                mock.patch.object(socket.socket, "bind", prohibido), mock.patch.object(socket.socket, "listen", prohibido), \
                mock.patch.object(socket.socket, "connect", prohibido), \
                contextlib.redirect_stdout(salida), contextlib.redirect_stderr(err):
            codigo = cli.main(["agent", "--show-key"])
        self.assertEqual(codigo, 0, err.getvalue())
        self.assertEqual(salida.getvalue(), clave + "\n")
        self.assertEqual(self.archivo_clave().read_bytes(), antes)
        self.assertEqual(list(vacia.iterdir()), [])

    def test_sin_clave_creada_error_en_espanol_que_dice_como_se_crea_y_no_la_crea(self):
        r = self.show()
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(r.stdout, "", "el error va a stderr; stdout queda vacío para los scripts")
        self.assertTrue(r.stderr.startswith("error:"), r.stderr)
        self.assertIn("agmen agent robot.toml", r.stderr, "debe decir cómo se crea la clave")
        self.assertIn("todavía no hay clave", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(self.archivo_clave().exists(), "--show-key no debe crear la clave")
        self.assertFalse((self.dir / "cfg-agente").exists() and any((self.dir / "cfg-agente").iterdir()),
                         "ni dejar nada en la carpeta de configuración")

    def test_junto_con_new_key_se_niega_y_no_toca_la_clave(self):
        clave = self.crear_clave()
        antes = self.archivo_clave().read_bytes()
        r = self.show("--new-key")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(r.stdout, "", "no imprime ninguna clave: no queda claro cuál se pidió")
        self.assertIn("--show-key y --new-key no van juntas", r.stderr)
        self.assertNotIn(clave, r.stderr)
        self.assertEqual(self.archivo_clave().read_bytes(), antes, "la clave sigue igual")
        # y sin clave previa tampoco la crea
        self.archivo_clave().unlink()
        r = self.show("--new-key")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertFalse(self.archivo_clave().exists(), "--new-key junto a --show-key no debe crear nada")

    def test_junto_con_un_robot_toml_lo_ignora_avisa_por_stderr_y_no_cambia_nada(self):
        clave = self.crear_clave()
        antes = self.archivo_clave().read_bytes()
        for args in ([str(self.toml)], [str(self.dir / "no_existe.toml")], [str(self.toml), "--machine", "robot", "--port", "9"]):
            with self.subTest(args=args):
                r = self.show(*args)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, clave + "\n", "stdout sigue siendo solo la clave")
                self.assertIn("nota: --show-key no usa", r.stderr)
                self.assertNotIn(clave, r.stderr)
                self.assertEqual(self.archivo_clave().read_bytes(), antes)
        self.assertFalse(list(self.marks.iterdir()), "no arrancó ningún nodo")

    def test_con_la_salida_por_una_tuberia_la_linea_de_espera_no_lleva_la_clave(self):
        p, lineas, clave = self.agente()
        espera = next(l for l in lineas if "esperando a agmen run" in l)
        ruta = self.archivo_clave()
        self.assertIn(f"clave: guardada en {ruta} (agmen agent --show-key la muestra)", espera)
        self.assertNotIn("clave: " + clave, espera)
        self.assertTrue(self.espera(lambda: any("clave creada" in l for l in lineas), 10), "".join(lineas))
        self.assertEqual(self.detener(p), 0, "".join(lineas))
        texto = "".join(lineas)
        for forma in (clave, clave.replace("-", ""), clave.upper()):
            self.assertNotIn(forma, texto, "la clave no debe salir en ninguna línea del agente sin terminal")
        self.assertRegex(clave, FORMA_CLAVE)

    @unittest.skipUnless(os.name == "posix", "necesita una terminal de verdad (pty): solo POSIX")
    def test_con_una_terminal_de_verdad_la_linea_de_espera_si_lleva_la_clave(self):
        import pty

        env = self.env("agente")
        maestro, esclavo = pty.openpty()
        p = subprocess.Popen([sys.executable, "-m", "agmen", "agent", str(self.toml), "--machine", "robot", "--port",
                              str(self.port)], env=env, stdin=subprocess.DEVNULL, stdout=esclavo, stderr=esclavo,
                             cwd=self.dir, start_new_session=True)
        os.close(esclavo)
        self.procs.append(p)
        salida = bytearray()

        def leer():
            while True:
                try:
                    d = os.read(maestro, 4096)
                except OSError:
                    return
                if not d:
                    return
                salida.extend(d)

        threading.Thread(target=leer, daemon=True).start()
        self.addCleanup(os.close, maestro)
        ver = lambda: bytes(salida).decode("utf-8", "replace").replace("\r", "")
        self.assertTrue(self.espera(lambda: "esperando a agmen run" in ver(), 40), ver())
        espera = next(l for l in ver().splitlines() if "esperando a agmen run" in l)
        m = re.search(r"clave: (\S+)", espera)
        self.assertIsNotNone(m, espera)
        r = self.show(env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(m.group(1), r.stdout.strip(), "con terminal la línea trae la clave de verdad")
        self.assertNotIn("guardada en", espera)
        self.assertEqual(self.detener(p), 0, ver())


class TestServicioDeSystemd(unittest.TestCase):
    """§15: examples/tutorial/agmen-agent.service dice la verdad y no se rinde (revisión del archivo, sin systemd)."""

    def test_machine_reintentos_sin_limite_y_tiempo_para_despedirse(self):
        texto = (ROOT / "examples" / "tutorial" / "agmen-agent.service").read_text(encoding="utf-8")
        campos = {}
        for linea in texto.splitlines():
            if linea and not linea.startswith("#") and "=" in linea:
                k, v = linea.split("=", 1)
                campos[k.strip()] = v.strip()
        self.assertRegex(campos["ExecStart"], r" agent .* --machine \S+", "sin --machine, sin IP al encender sale con 2")
        self.assertRegex(texto, r"--machine.*<-- EDITA|<-- EDITA.*\n(#.*\n)*ExecStart=.*--machine")
        self.assertEqual(campos["StartLimitIntervalSec"], "0", "systemd no debe rendirse al arrancar sin red")
        self.assertEqual(campos["Restart"], "on-failure")
        self.assertTrue(1 <= float(campos["RestartSec"]) <= 30, campos["RestartSec"])
        # el peor caso de end(): 2 órdenes a medias (15 s c/u), nodos y hub (STOP_BUDGET c/u), lo pendiente y el adios
        peor = 2 * 15 + 2 * agent.STOP_BUDGET + 2.0 + 2 * agent.BYE_TIMEOUT + 1.0
        self.assertGreaterEqual(float(campos["TimeoutStopSec"]), peor + 5, f"peor caso ~{peor:g} s, con margen")
        self.assertNotIn("escucha en la IP", texto, "el agente escucha en todas las interfaces")

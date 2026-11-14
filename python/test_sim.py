"""Pruebas del mundo de MuJoCo (examples/sim/, RFC 0011 §3.3), que son tres archivos: fisica.py (la clase
`Mundo`, la física sola, sin Agmen), ver.py (la ventana de --ver) y mundo.py (el nodo).

No son un paquete: fisica.py se carga por ruta (el nodo y ver.py, con examples/sim en sys.path). Se saltan si no hay MuJoCo (pip install mujoco);
las de la cámara se saltan además si esta máquina no puede dibujar sin ventana (OpenGL).
El nodo completo (freno, reloj, /imu, /camara) se prueba en scripts/sim_smoke.py.

    python -m unittest discover -s python
"""

import contextlib
import importlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
SIM_DIR = RAIZ / "examples" / "sim"
MUNDO_PY = SIM_DIR / "mundo.py"      # el nodo
FISICA_PY = SIM_DIR / "fisica.py"    # la clase Mundo y la plomería de MuJoCo/OpenGL
VER_PY = SIM_DIR / "ver.py"          # la ventana de --ver
ROBOT_XML = RAIZ / "examples" / "sim" / "robot.xml"
HAY_MUJOCO = importlib.util.find_spec("mujoco") is not None
SIN_MUJOCO = "falta MuJoCo: pip install mujoco (o pip install agmen[sim])"


def cargar_modulo():
    spec = importlib.util.spec_from_file_location("agmen_ejemplo_mundo", FISICA_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = cargar_modulo() if HAY_MUJOCO else None
_gl_cache: list = []   # [motivo]: "" = se puede dibujar


_PREPARACION_FALLO: list = []   # si elegir_gl()/import mujoco falla aquí: el motivo (TestPreparacion lo reporta)


def _elegir_gl_como_el_nodo() -> None:
    """MuJoCo lee MUJOCO_GL al importarse. El nodo lo elige con elegir_gl() ANTES de importar; aquí se hace lo
    mismo, una vez, y se deja os.environ como estaba (mujoco ya quedó cargado con ese OpenGL). Si falla, NO se
    traga en silencio: se imprime y TestPreparacion falla con el mismo motivo."""
    if not HAY_MUJOCO or "mujoco" in sys.modules:
        return
    antes = dict(os.environ)
    try:
        _mod.elegir_gl()
        import mujoco  # noqa: F401
    except Exception as e:
        _PREPARACION_FALLO.append(f"{type(e).__name__}: {e}")
        print(f"[test_sim] ⚠ falló la preparación de OpenGL/MuJoCo: {_PREPARACION_FALLO[-1]}", file=sys.stderr, flush=True)
    finally:
        os.environ.clear()
        os.environ.update(antes)


_elegir_gl_como_el_nodo()


def sin_opengl() -> str:
    """"" si esta máquina dibuja sin ventana; si no, el motivo (para @skipIf)."""
    if not HAY_MUJOCO:
        return SIN_MUJOCO
    if not _gl_cache:
        m = _mod.Mundo()
        try:
            m.imagen()
            _gl_cache.append("")
        except _mod.ErrorDelMundo as e:
            _gl_cache.append(f"no hay OpenGL sin ventana aquí: {e}")
        finally:
            m.cerrar()
    return _gl_cache[0]


def correr(codigo: str, pythonpath: list, extra_env=None, args=None):
    """Corre Python aparte con PYTHONPATH controlado."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(p) for p in pythonpath), "PYTHONIOENCODING": "utf-8",
           "NO_COLOR": "1"}
    env.pop("MUJOCO_GL", None)
    env.update(extra_env or {})
    cmd = [sys.executable, "-c", codigo] if args is None else [sys.executable, *args]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120, cwd=tempfile.gettempdir())


@unittest.skipUnless(HAY_MUJOCO, SIN_MUJOCO)
class TestMundo(unittest.TestCase):
    def setUp(self):
        self.m = _mod.Mundo()
        self.addCleanup(self.m.cerrar)

    def test_en_reposo_la_gravedad(self):
        self.m.avanzar(250)   # 0.5 s para que se asiente
        (ax, ay, az), (gx, gy, gz) = self.m.imu()
        self.assertAlmostEqual(az, 9.81, delta=0.1)
        self.assertAlmostEqual(ax, 0.0, delta=0.3)
        self.assertAlmostEqual(ay, 0.0, delta=0.3)
        for g in (gx, gy, gz):
            self.assertAlmostEqual(g, 0.0, delta=0.01)
        x, y, rumbo = self.m.pose()
        self.assertAlmostEqual(x, 0.0, delta=0.01)
        self.assertAlmostEqual(y, 0.0, delta=0.01)
        self.assertAlmostEqual(rumbo, 0.0, delta=0.01)

    def test_imu_devuelve_dos_tuplas_de_tres_floats(self):
        acel, giro = self.m.imu()
        for t in (acel, giro):
            self.assertIsInstance(t, tuple)
            self.assertEqual(len(t), 3)
            self.assertTrue(all(type(v) is float for v in t))

    def test_cero_cero_se_queda_quieto(self):
        self.m.comando(0, 0)
        self.m.avanzar(1000)
        x, y, rumbo = self.m.pose()
        self.assertLess(abs(x) + abs(y), 0.01)
        self.assertLess(abs(rumbo), 0.01)

    def test_avanza_en_mas_x(self):
        self.m.comando(0.5, 0.5)
        self.m.avanzar(1000)   # 2 s
        x, y, rumbo = self.m.pose()
        self.assertGreater(x, 0.1)
        self.assertLess(abs(y), 0.05)
        self.assertLess(abs(rumbo), 0.05)
        _, (gx, gy, gz) = self.m.imu()
        self.assertLess(abs(gz), 0.05)

    def test_hacia_atras_con_negativos(self):
        self.m.comando(-0.5, -0.5)
        self.m.avanzar(1000)
        self.assertLess(self.m.pose()[0], -0.1)

    def test_gira_en_su_lugar(self):
        self.m.comando(0.5, -0.5)
        self.m.avanzar(500)   # 1 s
        x, y, rumbo = self.m.pose()
        _, (_, _, gz) = self.m.imu()
        self.assertGreater(abs(gz), 0.1)
        self.assertGreater(abs(rumbo), 0.1)
        self.assertLess((x * x + y * y) ** 0.5, 0.15, "al girar casi no se traslada")
        # el rumbo y el giroscopio cuentan la misma historia (mismo signo)
        self.assertGreater(gz * rumbo, 0)
        # y gira al revés con el comando al revés
        m2 = _mod.Mundo()
        self.addCleanup(m2.cerrar)
        m2.comando(-0.5, 0.5)
        m2.avanzar(500)
        self.assertLess(m2.pose()[2] * rumbo, 0)

    def test_frenar_con_comando_cero(self):
        self.m.comando(0.5, 0.5)
        self.m.avanzar(500)
        self.m.comando(0, 0)
        self.m.avanzar(500)      # que frene
        x1 = self.m.pose()[0]
        self.m.avanzar(500)
        self.assertAlmostEqual(self.m.pose()[0], x1, delta=1e-3)

    def test_comando_recorta_a_menos1_mas1(self):
        self.m.comando(5, -7)
        self.assertEqual(self.m.ruedas, (1.0, -1.0))
        self.m.comando(-1e30, 1e30)
        self.assertEqual(self.m.ruedas, (-1.0, 1.0))
        # recortar es igual a mandar el tope: el mismo movimiento, bit a bit
        otro = _mod.Mundo()
        self.addCleanup(otro.cerrar)
        self.m.comando(3, 3)
        otro.comando(1, 1)
        self.m.avanzar(300)
        otro.avanzar(300)
        self.assertEqual(self.m.pose(), otro.pose())

    def test_el_actuador_recibe_rad_s_de_la_rueda(self):
        self.m.comando(0.5, -1)
        ctrl = [float(v) for v in self.m.datos.ctrl]
        self.assertAlmostEqual(ctrl[0], 5.0)
        self.assertAlmostEqual(ctrl[1], -10.0)

    def test_nan_e_infinito_son_esa_rueda_en_cero(self):
        nan, inf = float("nan"), float("inf")
        self.m.comando(nan, 0.5)
        self.assertEqual(self.m.ruedas, (0.0, 0.5))
        self.m.comando(0.5, inf)
        self.assertEqual(self.m.ruedas, (0.5, 0.0))
        self.m.comando(-inf, nan)
        self.assertEqual(self.m.ruedas, (0.0, 0.0))
        self.assertTrue(all(v == v for v in self.m.datos.ctrl), "el actuador nunca recibe NaN")
        # y la simulación no se rompe
        self.m.comando(nan, nan)
        self.m.avanzar(100)
        self.assertTrue(all(v == v for v in self.m.pose()))
        self.assertTrue(all(v == v for t in self.m.imu() for v in t))

    def test_avanzar_cuenta_el_tiempo(self):
        self.assertEqual(self.m.t, 0.0)
        self.assertAlmostEqual(self.m.dt, 0.002)
        self.m.avanzar(500)
        self.assertAlmostEqual(self.m.t, 1.0, places=6)
        self.m.avanzar(1)
        self.assertAlmostEqual(self.m.t, 1.002, places=6)

    def test_avanzar_cero_o_negativo_no_hace_nada(self):
        self.m.avanzar(0)
        self.m.avanzar(-10)
        self.assertEqual(self.m.t, 0.0)

    def test_reiniciar_deja_todo_como_al_inicio(self):
        inicial = (self.m.pose(), self.m.imu())
        self.m.comando(0.5, -0.5)
        self.m.avanzar(500)
        self.assertNotEqual(self.m.pose(), inicial[0])
        self.m.reiniciar()
        self.assertEqual(self.m.t, 0.0)
        self.assertEqual(self.m.ruedas, (0.0, 0.0))
        self.assertTrue(all(v == 0 for v in self.m.datos.ctrl))
        self.assertEqual((self.m.pose(), self.m.imu()), inicial)
        # sigue quieto después de reiniciar (no heredó el comando)
        self.m.avanzar(500)
        x, y, rumbo = self.m.pose()
        self.assertLess(abs(x) + abs(y) + abs(rumbo), 0.01)

    def test_determinista(self):
        otro = _mod.Mundo()
        self.addCleanup(otro.cerrar)
        for w in (self.m, otro):
            w.comando(0.4, 0.1)
            w.avanzar(400)
        self.assertEqual(self.m.pose(), otro.pose())
        self.assertEqual(self.m.imu(), otro.imu())

    def test_cerrar_sin_haber_dibujado_no_truena(self):
        self.m.cerrar()
        self.m.cerrar()

    def test_arranca_ya_asentado(self):
        (ax, ay, az), (gx, gy, gz) = self.m.imu()   # sin avanzar ni un paso
        self.assertAlmostEqual(az, 9.81, delta=0.05)
        self.assertLess(abs(ax), 0.01)
        self.assertLess(abs(ay), 0.01)
        self.assertTrue(all(g == 0 for g in (gx, gy, gz)))
        # y se queda asentado: sin comandos, tras 0.2 s sigue igual (no cae ni rebota)
        self.m.avanzar(100)
        (ax, ay, az), _ = self.m.imu()
        self.assertAlmostEqual(az, 9.81, delta=0.05)
        self.assertLess(abs(ax), 0.01)

    def test_aceleracion_maxima_acotada_y_no_se_levanta(self):
        z0 = float(self.m.datos.qpos[2])
        picos = []
        for _ in range(100):           # 1 s a fondo, mirando la IMU cada 10 ms
            self.m.comando(1, 1)
            self.m.avanzar(5)
            (ax, ay, az), _ = self.m.imu()
            picos.append((abs(ax), az))
            self.assertLess(abs(self.m.datos.qpos[2] - z0), 0.005, "el robot no se levanta ni se hunde")
        self.assertLess(max(p[0] for p in picos), 2.5, "no debe pasar de ~1.5 m/s² (si no, patina)")
        self.assertGreater(max(p[0] for p in picos), 0.5, "pero sí acelera")
        self.assertTrue(all(abs(p[1] - 9.81) < 0.6 for p in picos), "az sigue ~9.81 al arrancar a fondo")

    def test_a_los_100_ms_la_velocidad_es_razonable(self):
        self.m.comando(1, 1)
        self.m.avanzar(50)             # 0.1 s
        vx = float(self.m.datos.qvel[0])
        self.assertGreater(vx, 0.05)
        self.assertLess(vx, 0.3, "con 1.5 m/s² son ~0.15 m/s")

    def test_el_giro_de_antes_sigue_igual(self):
        self.m.comando(0.5, -0.5)
        self.m.avanzar(500)
        _, (gx, gy, gz) = self.m.imu()
        self.assertAlmostEqual(gz, -1.9, delta=0.3)
        self.m.reiniciar()
        self.m.comando(-0.5, 0.5)      # izquierda = +z
        self.m.avanzar(500)
        self.assertGreater(self.m.imu()[1][2], 1.5)

    def test_construir_y_avanzar_no_toca_el_entorno(self):
        antes = dict(os.environ)
        m = _mod.Mundo()
        m.comando(0.5, 0.5)
        m.avanzar(100)
        m.imu(), m.pose()
        m.reiniciar()
        m.cerrar()
        self.assertEqual(dict(os.environ), antes, "la clase no cambia variables de entorno")


@unittest.skipUnless(HAY_MUJOCO, SIN_MUJOCO)
class TestModelosRotos(unittest.TestCase):
    """Un modelo malo da un ErrorDelMundo que dice qué pasó y qué hacer; nunca un traceback."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.xml = ROBOT_XML.read_text(encoding="utf-8")

    def modelo(self, texto: str, nombre="m.xml") -> Path:
        p = self.dir / nombre
        p.write_text(texto, encoding="utf-8")
        return p

    def falla(self, ruta, *fragmentos) -> str:
        with self.assertRaises(_mod.ErrorDelMundo) as ctx:
            _mod.Mundo(ruta)
        msg = str(ctx.exception)
        for f in fragmentos:
            self.assertIn(f, msg)
        return msg

    def test_el_modelo_original_carga(self):
        _mod.Mundo(self.modelo(self.xml))

    def test_no_existe(self):
        msg = self.falla(self.dir / "nada.xml", "no encuentro el modelo")
        self.assertIn("nada.xml", msg)
        self.assertNotIn("--modelo", msg, "la opción es del nodo: la clase no la menciona")

    def test_es_una_carpeta(self):
        self.falla(self.dir, "no encuentro el modelo")

    def test_xml_roto(self):
        self.falla(self.modelo("<mujoco><worldbody>"), "MuJoCo no pudo leer")

    def test_archivo_vacio(self):
        self.falla(self.modelo(""), "MuJoCo no pudo leer")

    def test_no_es_mjcf(self):
        self.falla(self.modelo("<html><body>hola</body></html>"), "MuJoCo no pudo leer")

    def test_include_que_no_existe(self):
        self.falla(self.modelo('<mujoco><include file="no_existe.xml"/></mujoco>'), "MuJoCo no pudo leer")

    def test_falta_un_actuador(self):
        malo = self.xml.replace('<velocity name="rueda_der"', '<velocity name="otra"')
        self.falla(self.modelo(malo), "no tiene el actuador 'rueda_der'")

    def test_faltan_los_dos_actuadores(self):
        a = self.xml.index("<actuator>")
        b = self.xml.index("</actuator>") + len("</actuator>")
        self.falla(self.modelo(self.xml[:a] + self.xml[b:]), "el actuador 'rueda_izq'")

    def test_falta_un_sensor(self):
        for sensor in ("acel", "giro"):
            with self.subTest(sensor=sensor):
                malo = self.xml.replace(f'name="{sensor}"', 'name="otro"')
                self.falla(self.modelo(malo), f"no tiene el sensor '{sensor}'")

    def test_sensor_que_no_mide_tres_ejes(self):
        malo = self.xml.replace('<gyro name="giro" site="imu"/>',
                                '<jointpos name="giro" joint="rueda_izq"/>')
        self.falla(self.modelo(malo), "'giro'", "3 ejes")

    def test_sin_ctrlrange_en_las_ruedas(self):
        malo = self.xml.replace(' ctrlrange="-10 10"', "")
        self.falla(self.modelo(malo), "ctrlrange")

    def test_sin_junta_libre_base(self):
        malo = self.xml.replace('<freejoint name="base"/>', '<joint name="base" type="slide" axis="1 0 0"/>')
        self.falla(self.modelo(malo), "'base'", "freejoint")

    def test_sin_la_junta_base(self):
        malo = self.xml.replace('<freejoint name="base"/>', '<freejoint name="otra"/>')
        self.falla(self.modelo(malo), "la junta libre 'base'")

    def test_modelo_como_texto_de_ruta_str(self):
        _mod.Mundo(str(self.modelo(self.xml)))   # acepta str y Path

    def test_camara_que_no_existe(self):
        # se revisa antes de abrir OpenGL: no necesita pantalla ni EGL
        m = _mod.Mundo(camara="nada")
        self.addCleanup(m.cerrar)
        with self.assertRaises(_mod.ErrorDelMundo) as ctx:
            m.imagen()
        self.assertIn("no tiene la cámara 'nada'", str(ctx.exception))
        m.avanzar(10)   # y la física sigue

    def test_tamano_mayor_al_buffer_del_modelo(self):
        m = _mod.Mundo(ancho=800, alto=600)
        self.addCleanup(m.cerrar)
        with self.assertRaises(_mod.ErrorDelMundo) as ctx:
            m.imagen()
        msg = str(ctx.exception)
        self.assertIn("no puede dar 800×600", msg)
        self.assertIn("640×480", msg)

    def test_tamanos_invalidos(self):
        for ancho, alto in ((0, 240), (320, 0), (-1, 240), (641, 480), (640, 481)):
            with self.subTest(ancho=ancho, alto=alto):
                m = _mod.Mundo(ancho=ancho, alto=alto)
                self.addCleanup(m.cerrar)
                with self.assertRaises(_mod.ErrorDelMundo) as ctx:
                    m.imagen()
                self.assertIn("no puede dar", str(ctx.exception))


@unittest.skipUnless(HAY_MUJOCO, SIN_MUJOCO)
class TestImagen(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        motivo = sin_opengl()
        if motivo:
            raise unittest.SkipTest(motivo)

    def setUp(self):
        self.m = _mod.Mundo()
        self.addCleanup(self.m.cerrar)

    def test_forma_y_tipo_por_defecto(self):
        img = self.m.imagen()
        self.assertEqual(img.shape, (240, 320, 3))   # alto × ancho × 3
        self.assertEqual(str(img.dtype), "uint8")

    def test_no_es_toda_negra_ni_toda_igual(self):
        img = self.m.imagen()
        self.assertGreater(int(img.max()), 50)
        self.assertGreater(float(img.std()), 5.0)

    def test_ve_la_caja_roja_de_frente(self):
        img = self.m.imagen().astype(int)
        rojos = ((img[..., 0] > 150) & (img[..., 1] < 90) & (img[..., 2] < 90)).sum()
        self.assertGreater(int(rojos), 100, "la caja roja está 1.5 m al frente")

    def test_cambia_cuando_el_robot_se_mueve(self):
        antes = self.m.imagen().copy()
        self.m.comando(0.5, -0.5)
        self.m.avanzar(500)
        despues = self.m.imagen()
        self.assertTrue((antes != despues).any())

    def test_dos_fotos_sin_avanzar_son_iguales(self):
        a = self.m.imagen().copy()
        b = self.m.imagen()
        self.assertTrue((a == b).all())

    def test_otro_tamano_al_construir(self):
        m = _mod.Mundo(ancho=160, alto=120)
        self.addCleanup(m.cerrar)
        self.assertEqual(m.imagen().shape, (120, 160, 3))

    def test_tamano_maximo_del_modelo(self):
        m = _mod.Mundo(ancho=640, alto=480)
        self.addCleanup(m.cerrar)
        self.assertEqual(m.imagen().shape, (480, 640, 3))

    def test_mas_grande_que_el_buffer_es_un_error_claro(self):
        m = _mod.Mundo(ancho=2000, alto=2000)
        self.addCleanup(m.cerrar)
        with self.assertRaises(_mod.ErrorDelMundo) as ctx:
            m.imagen()
        self.assertIn("no puede dar 2000×2000", str(ctx.exception))

    def test_camara_que_no_existe_al_construir(self):
        m = _mod.Mundo(camara="nada")
        self.addCleanup(m.cerrar)
        with self.assertRaises(_mod.ErrorDelMundo) as ctx:
            m.imagen()
        self.assertIn("no tiene la cámara 'nada'", str(ctx.exception))

    def test_imagen_no_cambia_el_entorno(self):
        antes = dict(os.environ)
        self.m.imagen()
        self.m.cerrar()
        self.m.imagen()
        self.assertEqual(dict(os.environ), antes, "dibujar no toca variables de entorno")

    def test_cerrar_y_volver_a_pedir(self):
        self.m.imagen()
        self.m.cerrar()
        self.m.cerrar()   # idempotente
        self.assertEqual(self.m.imagen().shape, (240, 320, 3))
        self.m.cerrar()
        self.assertEqual(self.m.imagen().shape, (240, 320, 3))

    def test_modelo_sin_camara(self):
        xml = ROBOT_XML.read_text(encoding="utf-8").replace('<camera name="camara"', '<camera name="otra"')
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.xml"
            p.write_text(xml, encoding="utf-8")
            m = _mod.Mundo(p)
            self.addCleanup(m.cerrar)
            m.avanzar(10)                              # la física no necesita la cámara
            with self.assertRaises(_mod.ErrorDelMundo) as ctx:
                m.imagen()
            self.assertIn("la cámara 'camara'", str(ctx.exception))


PRELUDIO = (
    "import importlib.util as u\n"
    f"s = u.spec_from_file_location('mundo', r'{FISICA_PY}'); m = u.module_from_spec(s); s.loader.exec_module(m)\n"
)


class TestSinOpenGL(unittest.TestCase):
    """Sin OpenGL la física sigue y la cámara dice por qué no (subproceso: MUJOCO_GL se lee al importar).
    La elección de OpenGL es del NODO (elegir_gl); la clase sola solo avisa."""

    @unittest.skipUnless(HAY_MUJOCO, SIN_MUJOCO)
    def test_clase_con_gl_invalido_da_error_que_menciona_mujoco_gl(self):
        codigo = PRELUDIO + (
            "try:\n"
            "    m.Mundo()\n"
            "except m.ErrorDelMundo as e:\n"
            "    print('ERROR', e)\n"
        )
        r = correr(codigo, [], {"MUJOCO_GL": "no_existe_este_backend"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ERROR", r.stdout)
        self.assertIn("MUJOCO_GL", r.stdout)
        self.assertNotIn("Traceback", r.stderr)

    @unittest.skipUnless(HAY_MUJOCO and sys.platform.startswith("linux"), "sin pantalla: solo en Linux")
    def test_clase_sola_en_linux_sin_pantalla_ni_mujoco_gl(self):
        codigo = PRELUDIO + (
            "w = m.Mundo(); w.comando(0.5, 0.5); w.avanzar(500)\n"
            "print('x>0', w.pose()[0] > 0)\n"
            "try:\n"
            "    w.imagen()\n"
            "except m.ErrorDelMundo as e:\n"
            "    print('ERROR', e)\n"
        )
        r = correr(codigo, [], {"DISPLAY": "", "WAYLAND_DISPLAY": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("x>0 True", r.stdout)
        self.assertIn("ERROR no pude dibujar sin ventana", r.stdout)
        self.assertIn("MUJOCO_GL", r.stdout)
        self.assertRegex(r.stdout, r"egl.*osmesa|osmesa.*egl")
        self.assertNotIn("Traceback", r.stderr)

    @unittest.skipUnless(HAY_MUJOCO, SIN_MUJOCO)
    def test_nodo_con_gl_invalido_sigue_sin_camara(self):
        r = correr("", [RAIZ / "python"], {"MUJOCO_GL": "no_existe_este_backend", "AGMEN_HUB": "127.0.0.1:7394"},
                   args=[str(MUNDO_PY), "--seconds", "1"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[mundo] sin cámara: MUJOCO_GL=no_existe_este_backend no existe aquí (usa", r.stderr)
        self.assertIn("sigo sin cámara", r.stderr)
        self.assertEqual(r.stderr.count("sin cámara:"), 1, "se dice una sola vez")
        self.assertRegex(r.stdout, r"terminé: .* [5-9]\d /imu y 0 /camara publicados")   # la física sigue
        self.assertNotIn("Traceback", r.stderr)

    @unittest.skipUnless(HAY_MUJOCO and sys.platform.startswith("linux"), "DISPLAY falso: solo en Linux")
    def test_nodo_con_ver_sin_pantalla_sigue_sin_ventana(self):
        for display in ("", ":99"):
            with self.subTest(DISPLAY=display):
                r = correr("", [RAIZ / "python"], {"DISPLAY": display, "WAYLAND_DISPLAY": "", "AGMEN_HUB": "127.0.0.1:7394"},
                           args=[str(MUNDO_PY), "--ver", "--seconds", "1"])
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("[mundo] --ver necesita pantalla", r.stdout + r.stderr)
                self.assertIn("sigo sin ventana", r.stdout + r.stderr)
                self.assertIn("terminé:", r.stdout)
                self.assertNotIn("Traceback", r.stderr)


    @unittest.skipUnless(HAY_MUJOCO and sys.platform.startswith("linux"), "DISPLAY falso: solo en Linux")
    def test_ver_sin_pantalla_la_camara_se_intenta_normalmente(self):
        # Si la ventana no abre, no hay choque de GLFW: la cámara funciona o dice su causa real.
        r = correr("", [RAIZ / "python"], {"DISPLAY": ":99", "WAYLAND_DISPLAY": "", "AGMEN_HUB": "127.0.0.1:7394",
                                          "AGMEN_CLOCK": "real"}, args=[str(MUNDO_PY), "--ver", "--seconds", "2"])
        todo = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, todo)
        self.assertIn("sigo sin ventana", todo)
        self.assertNotIn("la cámara y la ventana usarían GLFW a la vez", todo)
        m = re.search(r"terminé: .* (\d+) /camara publicados", r.stdout)
        self.assertIsNotNone(m, todo)
        if int(m[1]) == 0:                       # no dibujó: debe decir por qué (la causa real, no un choque)
            self.assertRegex(r.stderr, r"\[mundo\] sin cámara: (no pude dibujar sin ventana|MUJOCO_GL)")
        self.assertEqual(r.stderr.count("sin cámara:"), 1 if int(m[1]) == 0 else 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)


@unittest.skipUnless(HAY_MUJOCO, SIN_MUJOCO)
class TestNodoCtrlC(unittest.TestCase):
    def test_ctrl_c_no_deja_traceback(self):
        if os.name == "nt":
            self.skipTest("Ctrl+C a un subproceso en Windows se prueba en sim_smoke (agmen run)")
        import signal

        env = {**os.environ, "PYTHONPATH": str(RAIZ / "python"), "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1",
               "AGMEN_HUB": "127.0.0.1:7394"}
        env.pop("MUJOCO_GL", None)
        p = subprocess.Popen([sys.executable, str(MUNDO_PY)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", cwd=tempfile.gettempdir())
        self.addCleanup(p.kill)
        primera = p.stdout.readline()                # ya cargó MuJoCo y se presentó
        self.assertIn("[mundo] Agmen", primera)
        p.send_signal(signal.SIGINT)
        salida = primera + p.communicate(timeout=30)[0]
        self.assertNotIn("Traceback", salida)
        self.assertIn(p.returncode, (0, 130), salida)
        if p.returncode == 130:
            self.assertIn("[mundo] interrumpido (Ctrl+C)", salida)


class TestPreparacion(unittest.TestCase):
    @unittest.skipUnless(HAY_MUJOCO, SIN_MUJOCO)
    def test_elegir_gl_y_cargar_mujoco_no_fallaron(self):
        self.assertEqual(_PREPARACION_FALLO, [], "elegir_gl() o 'import mujoco' fallaron al preparar estas pruebas")


@unittest.skipUnless(HAY_MUJOCO, SIN_MUJOCO)
class TestWithYCerrar(unittest.TestCase):
    def test_with_devuelve_el_mundo_y_cierra(self):
        with _mod.Mundo() as m:
            self.assertIsInstance(m, _mod.Mundo)
            m.comando(0.5, 0.5)
            m.avanzar(10)
            self.assertGreater(m.t, 0)
        self.assertIsNone(m._dibujo)
        self.assertNotIn(m, _mod._ABIERTOS)

    def test_with_cierra_aunque_haya_una_excepcion(self):
        with self.assertRaises(RuntimeError):
            with _mod.Mundo() as m:
                raise RuntimeError("x")
        self.assertIsNone(m._dibujo)

    def test_with_cierra_la_camara_abierta(self):
        motivo = sin_opengl()
        if motivo:
            self.skipTest(motivo)
        with _mod.Mundo() as m:
            m.imagen()
            self.assertIsNotNone(m._dibujo)
            self.assertIn(m, _mod._ABIERTOS)
        self.assertIsNone(m._dibujo)
        self.assertNotIn(m, _mod._ABIERTOS)

    @unittest.skipUnless(sys.platform.startswith("linux"), "osmesa y egl son de Linux (en Windows y Mac MUJOCO_GL es otro)")
    def test_salir_sin_cerrar_no_deja_exception_ignored(self):
        codigo = (
            "import importlib.util as u, sys\n"
            f"s = u.spec_from_file_location('mundo', r'{FISICA_PY}'); m = u.module_from_spec(s); s.loader.exec_module(m)\n"
            "m.elegir_gl()\n"
            "try:\n"
            "    w = m.Mundo()   # sin la biblioteca de ese OpenGL, MuJoCo ni siquiera carga (pasa en ARM64 sin libEGL)\n"
            "    w.imagen()\n"
            "except m.ErrorDelMundo as e:\n"
            "    print('SIN_GL', e); sys.exit(0)\n"
            "print('DIBUJO_OK')\n"
            "# se sale SIN w.cerrar()\n"
        )
        probados = 0
        for gl in ("osmesa", "egl"):
            with self.subTest(MUJOCO_GL=gl):
                r = correr(codigo, [], {"MUJOCO_GL": gl})
                if "SIN_GL" in r.stdout:
                    continue                    # este OpenGL no dibuja aquí
                probados += 1
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("DIBUJO_OK", r.stdout)
                self.assertNotIn("Exception ignored", r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.assertEqual(r.stderr.strip(), "", r.stderr)
        if not probados:
            self.skipTest("ni osmesa ni egl dibujan aquí")


def _modelo_con_timestep(dir_: Path, ms: float) -> Path:
    txt = ROBOT_XML.read_text(encoding="utf-8")
    assert 'timestep="0.002"' in txt
    p = dir_ / f"robot_{ms:g}ms.xml"
    p.write_text(txt.replace('timestep="0.002"', f'timestep="{ms / 1000:.6f}"'), encoding="utf-8")
    return p


@unittest.skipUnless(HAY_MUJOCO, SIN_MUJOCO)
class TestNodoTimestep(unittest.TestCase):
    """/imu cada 10 ms simulados sea cual sea el timestep del modelo."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def corre(self, ms, *extra, seconds="2"):
        modelo = _modelo_con_timestep(self.dir, ms)
        return correr("", [RAIZ / "python"], {"AGMEN_HUB": "127.0.0.1:7394"},
                      args=[str(MUNDO_PY), "--modelo", str(modelo), "--seconds", seconds, "--sin-camara", *extra])

    def test_cien_hz_con_timesteps_que_caben(self):
        for ms in (1.0, 2.5, 5.0, 10.0):
            with self.subTest(timestep_ms=ms):
                r = self.corre(ms)
                self.assertEqual(r.returncode, 0, r.stderr)
                m = re.search(r"terminé: ([\d.]+) s simulados · (\d+) /imu", r.stdout)
                self.assertIsNotNone(m, r.stdout)
                imus, seg = int(m[2]), float(m[1])
                self.assertTrue(150 <= imus <= 210, f"{imus} /imu en ~2 s")     # 100 Hz, no 1 kHz
                self.assertAlmostEqual(seg, imus / 100, delta=0.02)               # y cada uno son 10 ms simulados
                self.assertIn(f"pasos de {ms:g} ms", r.stdout)
                self.assertNotIn("Traceback", r.stderr)

    def test_timestep_que_no_cabe_es_un_error_claro(self):
        for ms in (3.0, 4.0, 20.0, 0.7):
            with self.subTest(timestep_ms=ms):
                r = self.corre(ms, seconds="1")
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertIn(f"[mundo] el timestep de", r.stderr)
                self.assertIn(f"({ms:g} ms) no cabe un número entero de veces en 10 ms", r.stderr)
                self.assertIn("usa 1, 2, 2.5, 5 o 10 ms", r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.assertNotIn("terminé", r.stdout)


@unittest.skipUnless(HAY_MUJOCO, SIN_MUJOCO)
class TestNodoRelojReal(unittest.TestCase):
    AVISO = ("[mundo] ⚠ corro con reloj real (sin --sim): no me juntes con el robot de verdad; "
             "publico /imu{y} simulados")

    def corre(self, *extra, clock_mode=None):
        env = {"AGMEN_HUB": "127.0.0.1:7394", "AGMEN_CLOCK": clock_mode or "real"}
        return correr("", [RAIZ / "python"], env, args=[str(MUNDO_PY), "--seconds", "1", *extra])

    def test_con_camara_una_linea(self):
        r = self.corre()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.count("corro con reloj real"), 1, r.stderr)
        self.assertIn(self.AVISO.format(y=" y /camara"), r.stderr.splitlines())

    def test_sin_camara_solo_imu(self):
        r = self.corre("--sin-camara")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.count("corro con reloj real"), 1, r.stderr)
        self.assertIn(self.AVISO.format(y=""), r.stderr.splitlines())
        self.assertNotIn("/camara simulados", r.stderr)

    def test_con_reloj_de_simulacion_no_sale(self):
        for extra in ((), ("--sin-camara",)):
            with self.subTest(extra=extra):
                r = self.corre(*extra, clock_mode="sim")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotIn("corro con reloj real", r.stdout + r.stderr)
                self.assertNotIn("Traceback", r.stderr)


_DRIVER_HORA = r"""
import json, runpy, sys, time
import agmen
from agmen import clock
P = agmen.Publisher
rec, orig = [], P.publish
def publish(self, msg, t_data=None):
    if type(msg).__name__ == "ImuSample":
        rec.append(t_data)
    return orig(self, msg, t_data=t_data)
P.publish = publish
real_now, T0 = clock.now_ns, time.monotonic()
RES, SALTA = 1, True                            # resolución de la hora del sistema (ns); si la ajustan o no
def now_ns():                                   # la hora del sistema: +5 s a los 0.8 s; -0.3 s a los 1.4 s
    e = time.monotonic() - T0
    return real_now() // RES * RES + (0 if not SALTA or e < 0.8 else 5_000_000_000 if e < 1.4 else 4_700_000_000)
clock.now_ns = now_ns
sys.argv = ["mundo.py", "--seconds", "3", "--sin-camara"]
try:
    runpy.run_path(sys.argv_mundo, run_name="__main__")
except SystemExit:
    pass
print("REC " + json.dumps(rec))
"""

_DRIVER_PASOS = r"""
import json, runpy, sys, threading, time
import agmen
from agmen import clock
from agmen.clock import Clock
P = agmen.Publisher
rec, orig = [], P.publish
def publish(self, msg, t_data=None):
    if type(msg).__name__ == "ImuSample":
        rec.append(t_data)
    return orig(self, msg, t_data=t_data)
P.publish = publish
cuentas = []
def mando():
    time.sleep(1.0)
    clock.update(Clock(t=1_000_000_000, speed=1.0, paused=False))   # el mundo ya corre: tiene su hora
    time.sleep(0.6)
    t = clock.mono_ns()
    clock.update(Clock(t=t, speed=1.0, paused=True))
    time.sleep(0.6)
    cuentas.append(len(rec))
    for seg in (1, 60, 90):                                          # pasos del panel con el reloj en pausa
        t += seg * 1_000_000_000
        clock.update(Clock(t=t, speed=1.0, paused=True))
        time.sleep(0.6 + seg / 40)
        cuentas.append(len(rec))
threading.Thread(target=mando, daemon=True).start()
sys.argv = ["mundo.py", "--seconds", "11", "--sin-camara"]
try:
    runpy.run_path(sys.argv_mundo, run_name="__main__")
except SystemExit:
    pass
print("CUENTAS " + json.dumps(cuentas))
"""


def _driver(codigo: str, extra_env: dict):
    """Corre un driver de Python que sustituye agmen.clock.now_ns o manda /clock y ejecuta mundo.py."""
    pre = f"import sys; sys.argv_mundo = r'{MUNDO_PY}'\n"
    return correr(pre + codigo, [RAIZ / "python", SIM_DIR], {"AGMEN_HUB": "127.0.0.1:7394", **extra_env})


@unittest.skipUnless(HAY_MUJOCO, SIN_MUJOCO)
class TestNodoHoraDelSistema(unittest.TestCase):
    """Con reloj real, t_data sigue la escala de agmen.clock.now_ns() aunque la hora del sistema se ajuste
    después de arrancar. Se sustituye clock.now_ns en un proceso de prueba (no se toca el reloj del sistema)."""

    # time.time_ns() de Python ≤ 3.12 en Windows: avanza en saltos de 15.625 ms, más que un tramo de 10 ms.
    GRUESA = 15_625_000

    def _correr(self, res: int, salta: bool):
        self.assertEqual(_DRIVER_HORA.count("RES, SALTA = 1, True"), 1)
        codigo = _DRIVER_HORA.replace("RES, SALTA = 1, True", f"RES, SALTA = {res}, {salta}")
        r = _driver(codigo, {"AGMEN_CLOCK": "real"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        rec = json.loads(next(l for l in r.stdout.splitlines() if l.startswith("REC "))[4:])
        self.assertGreater(len(rec), 200, len(rec))
        difs = [b - a for a, b in zip(rec, rec[1:])]
        self.assertEqual([d for d in difs if d <= 0], [], "t_data nunca se repite ni baja")
        return r, rec, difs

    def test_adelanta_y_retrocede(self):
        self._comprobar_un_retroceso(1)

    def test_adelanta_y_retrocede_con_hora_de_resolucion_gruesa(self):
        # Lo que falló en el CI de Windows: con la hora en saltos de 15.6 ms, el desfase de un tick a otro baja
        # hasta 15.6 ms y t_data "retrocedía" sin que nadie tocara la hora: un aviso de más por cada vez.
        self._comprobar_un_retroceso(self.GRUESA)

    def test_resolucion_gruesa_sin_ajuste_no_avisa(self):
        r, _, difs = self._correr(self.GRUESA, False)
        self.assertNotIn("retrocedió", r.stderr, "la resolución del reloj no es un retroceso")
        self.assertNotIn("t_data ya va otra vez", r.stdout + r.stderr)
        self.assertLess(max(difs), 10**9, "sin ajuste, t_data no da saltos")

    def _comprobar_un_retroceso(self, res: int):
        r, rec, difs = self._correr(res, True)
        self.assertLess(abs(rec[0] - time.time_ns()), 60 * 10**9, "en la escala de now_ns")
        self.assertTrue(any(4.9e9 <= d <= 5.2e9 for d in difs), "la hora adelantada se sigue de golpe")
        self.assertGreaterEqual(sum(1 for d in difs if d == 1), 10, "al retroceder, avanza de 1 ns en 1 ns")
        self.assertEqual(r.stderr.count("la hora del sistema retrocedió: t_data avanza de 1 ns en 1 ns"), 1, r.stderr)
        self.assertEqual((r.stdout + r.stderr).count("t_data ya va otra vez con la hora del sistema"), 1, r.stdout)


@unittest.skipUnless(HAY_MUJOCO, SIN_MUJOCO)
class TestNodoPasoLargo(unittest.TestCase):
    def test_paso_largo_en_pausa_se_simula_entero(self):
        r = _driver(_DRIVER_PASOS, {"AGMEN_CLOCK": "sim"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        c = json.loads(next(l for l in r.stdout.splitlines() if l.startswith("CUENTAS "))[8:])
        self.assertEqual(len(c), 4, c)
        paso1, paso60, paso90 = c[1] - c[0], c[2] - c[1], c[3] - c[2]
        self.assertTrue(90 <= paso1 <= 110, f"un paso de 1 s: {paso1} /imu")
        self.assertTrue(5900 <= paso60 <= 6100, f"un paso de 60 s: {paso60} /imu")
        self.assertTrue(5900 <= paso90 <= 6100, f"un paso de 90 s: se simulan los últimos 60 s: {paso90} /imu")
        todo = r.stdout + r.stderr
        self.assertIn("⚠ un paso de 90 s: simulo solo los últimos 60 s", todo)
        self.assertNotIn("% del reloj", todo, "un paso en pausa no cuenta para el aviso de lentitud")
        self.assertNotIn("Traceback", r.stderr)


class TestTresArchivos(unittest.TestCase):
    """El simulador son tres archivos (examples/sim/): fisica.py no sabe de Agmen ni carga MuJoCo al importarse;
    mundo.py (el nodo) y ver.py (la ventana) importan a fisica, así que examples/sim tiene que estar en sys.path."""

    CARGA = (
        "import sys, importlib.util as u\n"
        f"sys.path.insert(0, r'{SIM_DIR}')\n"
    )

    def test_importar_fisica_no_deja_agmen_ni_mujoco(self):
        codigo = self.CARGA + (
            "import os, fisica\n"
            "malos = sorted(k for k in sys.modules if k.split('.')[0] in ('agmen', 'mujoco'))\n"
            "print('CARGADOS', malos)\n"
            "print('GL', os.environ.get('MUJOCO_GL'))\n"
        )
        r = correr(codigo, [RAIZ / "python"])        # con agmen al alcance: si lo importara, se vería
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("CARGADOS []", r.stdout)
        self.assertIn("GL None", r.stdout, "importar fisica no toca MUJOCO_GL")
        self.assertEqual(r.stderr.strip(), "", r.stderr)

    def test_mundo_py_se_carga_por_ruta_con_sim_en_sys_path(self):
        codigo = self.CARGA + (
            f"s = u.spec_from_file_location('mundo', r'{MUNDO_PY}'); m = u.module_from_spec(s); s.loader.exec_module(m)\n"
            "print('MAIN', callable(m.main), callable(m.di), m.TRAMO, m.fisica.__name__)\n"
            "malos = sorted(k for k in sys.modules if k.split('.')[0] in ('agmen', 'mujoco'))\n"
            "print('CARGADOS', malos)\n"
        )
        r = correr(codigo, [RAIZ / "python"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("MAIN True True 10000000 fisica", r.stdout)
        self.assertIn("CARGADOS []", r.stdout, "agmen y MuJoCo se cargan dentro de main(), no al importar")

    def test_mundo_py_por_ruta_sin_sim_en_sys_path_falla_con_modulenotfound_de_fisica(self):
        # Documenta la regla: sin examples/sim en sys.path no hay fisica. (Como script, python lo pone solo.)
        codigo = (
            "import importlib.util as u\n"
            f"s = u.spec_from_file_location('mundo', r'{MUNDO_PY}'); m = u.module_from_spec(s)\n"
            "try:\n"
            "    s.loader.exec_module(m)\n"
            "except ModuleNotFoundError as e:\n"
            "    print('FALTA', e.name)\n"
        )
        r = correr(codigo, [])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("FALTA fisica", r.stdout)

    def test_ver_se_importa_sin_pantalla_ni_mujoco(self):
        codigo = self.CARGA + (
            "import ver\n"
            "print('API', callable(ver.abrir), ver.Ventana.__name__)\n"
            "malos = sorted(k for k in sys.modules if k.split('.')[0] in ('agmen', 'mujoco', 'glfw'))\n"
            "print('CARGADOS', malos)\n"
        )
        r = correr(codigo, [RAIZ / "python"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("API True Ventana", r.stdout)
        self.assertIn("CARGADOS []", r.stdout, "ver.py importa glfw y mujoco.viewer solo al abrir")

    @unittest.skipUnless(HAY_MUJOCO and sys.platform.startswith("linux"), "pantalla falsa: solo en Linux, con MuJoCo")
    def test_ver_abrir_sin_pantalla_devuelve_none_y_lo_dice(self):
        codigo = self.CARGA + (
            "import threading, types, ver\n"
            "v = ver.abrir(types.SimpleNamespace(modelo=None, datos=None), threading.Lock())\n"
            "print('RESULTADO', v)\n"
        )
        for display in ("", ":99"):
            with self.subTest(DISPLAY=display):
                r = correr(codigo, [], {"DISPLAY": display, "WAYLAND_DISPLAY": ""})
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("RESULTADO None", r.stdout)
                self.assertIn("[mundo] --ver necesita pantalla (GLFW: ", r.stdout)
                self.assertIn("sigo sin ventana", r.stdout)
                self.assertNotIn("Traceback", r.stderr)


class _VisorFalso:
    def __init__(self, candado=None) -> None:
        self.corriendo, self.syncs, self.cerrado, self.candado, self.con_candado = True, 0, 0, candado, []

    def is_running(self):
        return self.corriendo

    def sync(self):
        self.syncs += 1
        self.con_candado.append(self.candado.locked())

    def close(self):
        self.cerrado += 1


class TestVentana(unittest.TestCase):
    """ver.py con un GLFW y un mujoco.viewer falsos (en sys.modules): no abre ninguna ventana de verdad,
    así que corre igual en cualquier sistema."""

    def setUp(self):
        ctx = mock.patch.object(sys, "path", [str(SIM_DIR), *sys.path])
        ctx.start()
        self.addCleanup(ctx.stop)
        tocados = ("ver", "fisica", "glfw", "mujoco", "mujoco.viewer")     # solo estos: se restauran tal cual
        antes = {k: sys.modules[k] for k in tocados if k in sys.modules}
        self.addCleanup(lambda: ([sys.modules.pop(k, None) for k in tocados], sys.modules.update(antes)))
        for k in ("ver", "fisica"):
            sys.modules.pop(k, None)
        self.glfw = types.ModuleType("glfw")
        self.glfw.init = lambda: True
        self.viewer = types.ModuleType("mujoco.viewer")
        self.visor = _VisorFalso()
        self.lanzado = []

        def launch_passive(modelo, datos):
            self.lanzado.append((modelo, datos, self.candado.locked()))
            self.visor.candado = self.candado
            self.hilo = threading.Thread(target=time.sleep, args=(0.05,))   # un hilo que deja la ventana
            self.hilo.start()
            return self.visor

        self.viewer.launch_passive = launch_passive
        mujoco = types.ModuleType("mujoco")
        mujoco.__path__ = []
        mujoco.viewer = self.viewer
        sys.modules.update({"glfw": self.glfw, "mujoco": mujoco, "mujoco.viewer": self.viewer})
        self.ver = importlib.import_module("ver")
        self.candado = threading.Lock()
        self.mundo = types.SimpleNamespace(modelo="MODELO", datos="DATOS")

    def abrir(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            v = self.ver.abrir(self.mundo, self.candado)
        return v, out.getvalue()

    def test_abre_y_lo_dice(self):
        v, out = self.abrir()
        self.assertIsInstance(v, self.ver.Ventana)
        self.assertEqual(out, "[mundo] ventana abierta: si la cierras, el robot sigue (sin ventana)\n")
        self.assertEqual(self.lanzado, [("MODELO", "DATOS", True)], "mujoco.viewer se lanza con el candado tomado")
        v.cerrar()

    def test_sin_pantalla_glfw_dice_el_motivo_y_devuelve_none(self):
        def init():
            import warnings
            warnings.warn("65550 b'X11: The DISPLAY environment variable is missing'")
            return False

        self.glfw.init = init
        v, out = self.abrir()
        self.assertIsNone(v)
        self.assertEqual(out, "[mundo] --ver necesita pantalla (GLFW: X11: The DISPLAY environment variable is missing); "
                              "sigo sin ventana\n")
        self.assertEqual(self.lanzado, [], "sin pantalla no se lanza el visor")

    def test_sin_pantalla_y_sin_aviso_dice_sin_motivo(self):
        self.glfw.init = lambda: False
        v, out = self.abrir()
        self.assertIsNone(v)
        self.assertIn("(GLFW: sin motivo); sigo sin ventana", out)

    def test_si_falta_glfw_devuelve_none_en_una_linea(self):
        sys.modules["glfw"] = None                 # "import glfw" -> ImportError
        v, out = self.abrir()
        self.assertIsNone(v)
        self.assertEqual(out.count("\n"), 1, out)
        self.assertIn("[mundo] --ver no pudo abrir la ventana (", out)
        self.assertIn("sigo sin ventana", out)

    def test_si_el_visor_truena_devuelve_none_con_la_primera_linea(self):
        def truena(modelo, datos):
            raise RuntimeError("launch_passive necesita mjpython en macOS\nsegunda línea larga")

        self.viewer.launch_passive = truena
        v, out = self.abrir()
        self.assertIsNone(v)
        self.assertEqual(out, "[mundo] --ver no pudo abrir la ventana (launch_passive necesita mjpython en macOS); "
                              "sigo sin ventana\n")
        self.assertFalse(self.candado.locked(), "el candado se suelta aunque el visor truene")

    def test_al_dia_sincroniza_unas_30_veces_por_segundo_de_pared(self):
        v, _ = self.abrir()
        seg = self.ver.SEG
        t0, paso = seg, seg // 30 + 1              # 33.3 ms (el +1 evita el redondeo de SEG / 30)
        v.al_dia(t0, 1.0)                          # la primera vez solo marca la hora
        self.assertEqual(self.visor.syncs, 0)
        v.al_dia(t0 + seg // 100, 1.0)             # 10 ms después: todavía no
        self.assertEqual(self.visor.syncs, 0)
        v.al_dia(t0 + paso, 1.0)                   # 33 ms: sí
        self.assertEqual(self.visor.syncs, 1)
        v.al_dia(t0 + paso + seg // 100, 1.0)
        self.assertEqual(self.visor.syncs, 1)
        v.al_dia(t0 + 2 * paso, 2.0)               # a ×2 se espera el doble: 33 ms ya no bastan
        self.assertEqual(self.visor.syncs, 1)
        v.al_dia(t0 + 3 * paso, 2.0)
        self.assertEqual(self.visor.syncs, 2)
        self.assertEqual(self.visor.con_candado, [True, True], "sync() corre con el candado tomado")
        self.assertFalse(self.candado.locked())
        v.cerrar()

    def test_si_cierras_la_ventana_lo_dice_una_vez_y_el_robot_sigue(self):
        v, _ = self.abrir()
        seg = self.ver.SEG
        v.al_dia(seg, 1.0)
        self.visor.corriendo = False
        with contextlib.redirect_stdout(io.StringIO()) as out:
            v.al_dia(2 * seg, 1.0)
            v.al_dia(3 * seg, 1.0)                 # ya sin ventana: no hace nada ni repite el aviso
        self.assertEqual(out.getvalue(), "[mundo] cerraste la ventana: el robot sigue, sin ventana\n")
        self.assertEqual(self.visor.syncs, 0)
        v.cerrar()                                 # y cerrar después no truena
        self.assertEqual(self.visor.cerrado, 0, "la ventana ya la cerró el usuario: no se cierra otra vez")
        self.assertFalse(self.hilo.is_alive(), "cerrar() espera a los hilos de la ventana")

    def test_cerrar_cierra_el_visor_una_vez_y_espera_a_sus_hilos(self):
        v, _ = self.abrir()
        v.cerrar()
        v.cerrar()                                 # idempotente
        self.assertEqual(self.visor.cerrado, 1)
        self.assertFalse(self.hilo.is_alive())


class TestSinAgmen(unittest.TestCase):
    @unittest.skipUnless(HAY_MUJOCO, SIN_MUJOCO)
    def test_mundo_no_importa_agmen(self):
        # python/ en el PYTHONPATH: si fisica.py importara agmen, lo cargaría y se vería en sys.modules.
        codigo = (
            "import sys, importlib.util as u\n"
            f"s = u.spec_from_file_location('mundo', r'{FISICA_PY}'); m = u.module_from_spec(s); s.loader.exec_module(m)\n"
            "w = m.Mundo(); w.comando(0.3, 0.3); w.avanzar(50); w.imu(); w.pose(); w.reiniciar()\n"
            "try:\n"
            "    w.imagen()\n"
            "except m.ErrorDelMundo:\n"
            "    pass\n"
            "w.cerrar()\n"
            "print('AGMEN_CARGADO', any(k == 'agmen' or k.startswith('agmen.') for k in sys.modules))\n"
        )
        r = correr(codigo, [RAIZ / "python"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("AGMEN_CARGADO False", r.stdout)


class TestFaltaDependencia(unittest.TestCase):
    """Sin MuJoCo (o sin NumPy) el mensaje dice cómo instalarlo. Un paquete falso delante en PYTHONPATH
    que lanza ImportError simula que no están instalados."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.falsos = Path(self._tmp.name)

    def falso(self, nombre: str):
        d = self.falsos / nombre
        d.mkdir()
        (d / "__init__.py").write_text(f'raise ImportError("{nombre} falso: no instalado")\n', encoding="utf-8")

    def test_falta_mujoco_en_la_clase(self):
        self.falso("mujoco")
        codigo = (
            "import importlib.util as u\n"
            f"s = u.spec_from_file_location('mundo', r'{FISICA_PY}'); m = u.module_from_spec(s); s.loader.exec_module(m)\n"
            "try:\n"
            "    m.Mundo()\n"
            "except m.ErrorDelMundo as e:\n"
            "    print('MENSAJE', e)\n"
        )
        r = correr(codigo, [self.falsos, RAIZ / "python"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("MENSAJE falta MuJoCo: instálalo con  pip install agmen[sim]", r.stdout)

    def test_falta_mujoco_en_el_nodo(self):
        self.falso("mujoco")
        r = correr("", [self.falsos, RAIZ / "python"], args=[str(MUNDO_PY), "--seconds", "1"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[mundo] falta MuJoCo: instálalo con  pip install agmen[sim]", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_falta_numpy(self):
        self.falso("numpy")
        codigo = (
            "import importlib.util as u\n"
            f"s = u.spec_from_file_location('mundo', r'{FISICA_PY}'); m = u.module_from_spec(s); s.loader.exec_module(m)\n"
            "try:\n"
            "    m.Mundo()\n"
            "except m.ErrorDelMundo as e:\n"
            "    print('MENSAJE', e)\n"
        )
        r = correr(codigo, [self.falsos])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("MENSAJE falta NumPy: instálalo con  pip install agmen[sim]", r.stdout)


@unittest.skipUnless(HAY_MUJOCO, SIN_MUJOCO)
class TestNodoArgumentos(unittest.TestCase):
    """Errores de la línea de comandos del nodo (no necesitan hub)."""

    def corre(self, *args):
        return correr("", [RAIZ / "python"], args=[str(MUNDO_PY), *args])

    def test_modelo_que_no_existe(self):
        r = self.corre("--modelo", "no_existe.xml", "--seconds", "1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[mundo] no encuentro el modelo", r.stderr)
        self.assertIn("revisa --modelo", r.stderr, "la opción la agrega el nodo, no la clase")
        self.assertNotIn("Traceback", r.stderr)

    def test_modelo_roto(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "roto.xml"
            p.write_text("<mujoco><worldbody>", encoding="utf-8")
            r = self.corre("--modelo", str(p), "--seconds", "1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[mundo] MuJoCo no pudo leer", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()

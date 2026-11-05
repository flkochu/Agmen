"""Memoria compartida para los mensajes grandes (RFC 0009): los bytes del
descriptor y de los avisos, el camino completo entre un Publisher y un
Subscriber (préstamos, topes de D6), el respaldo por TCP y la limpieza de los
bloques de un proceso que murió.

Corre con los dos núcleos (AGMEN_BACKEND=pure|native), y cruzados: publicador
en C++ con suscriptor en Python puro, y al revés. La misma prueba en C++:
tests/test_shm.cpp."""

from __future__ import annotations

import dataclasses
import gc
import io
import multiprocessing
import os
import pickle
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr
from dataclasses import dataclass
from pathlib import Path

import agmen
from agmen import _backend, _shm, pubsub, wire
from agmen import hub as hubmod

try:
    import numpy as np
except ImportError:      # pragma: no cover
    np = None


@dataclass
class Img(wire.Message):
    SCHEMA = "Img{u32 w;u32 h;bytes data}"
    w: int
    h: int
    data: bytes


@dataclass
class Nube(wire.Message):
    """Un tipo arrow escrito a mano, con un struct anidado (llega como dict)."""
    SCHEMA = "Nube{bytes data;P{f64 x;bytes etiqueta}[] puntos}"
    data: bytes
    puntos: list


BIG = bytes(range(256)) * 4000      # 1 MB
ROOT = Path(__file__).resolve().parents[1]


def until_shared(pub, sub, data=BIG, timeout=3.0):
    """Publica hasta que un mensaje llegue por memoria compartida (la prueba
    tarda un momento; mientras tanto llegan por TCP)."""
    end = time.monotonic() + timeout
    k = 0
    while time.monotonic() < end:
        pub.publish(Img(k, 0, data))
        k += 1
        while (s := sub.receive(0.02)) is not None:
            if s.envelope.flags & wire.FLAG_SHM:
                return s
    return None


def drain(sub, timeout=0.3):
    out = []
    while (s := sub.receive(timeout)) is not None:
        out.append(s)
    return out


def is_view(sample) -> bool:
    """¿El campo `data` apunta a la memoria compartida (y no a una copia)?"""
    return not isinstance(sample.message.data.obj, (bytes, bytearray))


def _eco(msg):
    """En el proceso hijo (multiprocessing con spawn): lo regresa tal cual."""
    return msg


def _resumen(msg):
    """En el proceso hijo: lo que llegó, campo por campo."""
    return (type(msg).__name__, msg.timestamp, msg.frame_id, msg.width, msg.height, msg.encoding, msg.step,
            bytes(msg.data))


class EnvVar:
    def __init__(self, name, value):
        self.name, self.value = name, value

    def __enter__(self):
        self.old = os.environ.get(self.name)
        if self.value is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.value

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.old


class TestBytes(unittest.TestCase):
    def test_descriptor(self):
        d = _shm.Descriptor("/agm0000002a0000beef0001", 1 << 20, 64, 921600, 7, 3)
        b = d.pack()
        self.assertEqual(len(b), _shm.DESCRIPTOR_LEN)
        self.assertEqual(b[:24], b"/agm0000002a0000beef0001")
        self.assertEqual(b[24:32], bytes(8))                       # relleno con ceros
        self.assertEqual(int.from_bytes(b[40:48], "little"), 64)   # offset
        self.assertEqual(int.from_bytes(b[64:68], "little"), 3)    # slot
        self.assertEqual(_shm.Descriptor.unpack(b), d)
        bad = bytearray(b)
        bad[48:56] = (1 << 21).to_bytes(8, "little")               # no cabe en el bloque
        with self.assertRaises(wire.WireError):
            _shm.Descriptor.unpack(bad)

    def test_avisos(self):
        r = _shm.record(_shm.OP_RELEASE, 5, 99)
        self.assertEqual(len(r), _shm.RECORD_LEN)
        self.assertEqual(r[:4], b"AGB\x04")
        self.assertEqual(_shm.read_record(r), (_shm.OP_RELEASE, 5, 99))
        with self.assertRaises(wire.WireError):
            _shm.read_record(b"XYZ" + r[3:])

    def test_un_sobre_shm_solo_si_se_pidio(self):
        env = wire.Envelope(wire.ENC_ARROW, wire.FLAG_SHM, payload_len=_shm.DESCRIPTOR_LEN)
        packet = env.pack() + bytes(_shm.DESCRIPTOR_LEN)
        with self.assertRaisesRegex(wire.WireError, "memoria compartida"):
            wire.parse(packet)
        got, payload = wire.parse(packet, shm=True)
        self.assertTrue(got.flags & wire.FLAG_SHM)
        self.assertEqual(len(payload), _shm.DESCRIPTOR_LEN)


@unittest.skipUnless(_shm.enabled(), "sin memoria compartida")
class TestDirecto(unittest.TestCase):
    def setUp(self):
        self.sub = agmen.Subscriber(Img, "127.0.0.1:0")
        self.pub = agmen.Publisher(Img, f"127.0.0.1:{self.sub.port}", topic="/img")

    def tearDown(self):
        self.sub.close()
        self.pub.close()

    def test_un_megabyte_sin_copiar(self):
        s = until_shared(self.pub, self.sub)
        self.assertIsNotNone(s, "llega por memoria compartida")
        self.assertEqual(bytes(s.message.data), BIG)
        self.assertGreater(s.envelope.payload_len, len(BIG))    # el payload arrow, no el descriptor
        self.assertTrue(is_view(s))
        self.assertTrue(s.message.data.readonly)
        self.assertEqual(self.pub.shm_subscribers, 1)
        self.assertEqual(self.pub.shm_fallbacks, 0)

    def test_los_bloques_vuelven(self):
        self.assertIsNotNone(until_shared(self.pub, self.sub))
        shared = 0
        for k in range(100):
            self.pub.publish(Img(k, 0, BIG))
            s = self.sub.receive(1)
            shared += bool(s and s.envelope.flags & wire.FLAG_SHM)
            del s
        self.assertEqual(shared, 100)
        self.assertEqual(self.pub.shm_fallbacks, 0)
        self.assertEqual(self.pub.dropped, 0)

    def test_guardar_muchos_no_deja_a_nadie_sin_datos(self):
        self.assertIsNotNone(until_shared(self.pub, self.sub))
        gc.collect()
        kept = []
        with redirect_stderr(io.StringIO()) as err:
            for k in range(10):
                data = bytes([k]) * len(BIG)
                self.pub.publish(Img(k, 0, data))
                s = self.sub.receive(1)
                self.assertIsNotNone(s)
                kept.append(s)
        self.assertEqual([s.message.w for s in kept], list(range(10)), "los 10 llegan aunque nadie suelte")
        for k, s in enumerate(kept):
            self.assertEqual(bytes(s.message.data), bytes([k]) * len(BIG))
        self.assertEqual(sum(is_view(s) for s in kept), _shm.MAX_LENT, "del 5.º en adelante se copian (D6)")
        self.assertIn("sin soltar", err.getvalue())

    def test_suscriptor_lento_recibe_lo_mas_nuevo(self):
        self.assertIsNotNone(until_shared(self.pub, self.sub))
        before = self.pub.dropped
        t0 = time.perf_counter()
        for k in range(40):
            self.pub.publish(Img(1000 + k, 0, BIG))      # nadie lee
        self.assertLess(time.perf_counter() - t0, 1.0, "publicar no espera al suscriptor")
        with redirect_stderr(io.StringIO()):
            got = [s.message.w for s in drain(self.sub)]
        self.assertEqual(got[-1], 1039, "el último siempre llega")
        self.assertEqual(got, sorted(got), "nada se adelanta")
        self.assertLess(len(got), 40)
        self.assertEqual(len(got) + self.pub.dropped - before, 40)
        self.assertEqual(self.pub.shm_fallbacks, 0)

    def test_chicos_en_linea_grandes_compartidos_y_en_orden(self):
        self.assertIsNotNone(until_shared(self.pub, self.sub))
        got = []
        with redirect_stderr(io.StringIO()):     # se guardan todos: del 5.º en adelante, copiados (D6)
            for k in range(40):
                self.pub.publish(Img(k, 0, b"x" * (100 if k % 2 == 0 else 200_000)))
                got += drain(self.sub, 0.02)
            got += drain(self.sub)
        self.assertEqual([s.message.w for s in got], list(range(40)))
        for s in got:
            self.assertEqual(bool(s.envelope.flags & wire.FLAG_SHM), s.message.w % 2 == 1)

    def test_la_vista_sobrevive_al_suscriptor(self):
        s = until_shared(self.pub, self.sub, bytes([9]) * len(BIG))
        self.assertIsNotNone(s)
        self.sub.close()
        for k in range(20):
            self.pub.publish(Img(k, 0, BIG))                # el publicador sigue
        self.assertEqual(bytes(s.message.data), bytes([9]) * len(BIG), "nadie la reescribió")

    def test_pickle_de_un_mensaje_compartido(self):
        # multiprocessing / DataLoader (0.10.1): la vista se copia solo al hacer pickle.
        from agmen.msgs import ImagenComprimida

        sub = agmen.Subscriber(ImagenComprimida, "127.0.0.1:0")
        pub = agmen.Publisher(ImagenComprimida, f"127.0.0.1:{sub.port}", topic="/jpg")
        try:
            got = None
            end = time.monotonic() + 3
            while got is None and time.monotonic() < end:
                pub.publish(ImagenComprimida(format="jpeg", data=BIG))
                s = sub.receive(0.05)
                if s is not None and s.envelope.flags & wire.FLAG_SHM:
                    got = s
            self.assertIsNotNone(got)
            back = pickle.loads(pickle.dumps(got.message))
            self.assertEqual(bytes(back.data), BIG)
            self.assertEqual(back.format, "jpeg")
        finally:
            sub.close()
            pub.close()

    @unittest.skipIf(np is None, "sin numpy")
    def test_imagen_en_numpy_sin_copiar(self):
        from agmen.msgs import Imagen

        sub = agmen.Subscriber(Imagen, "127.0.0.1:0")
        pub = agmen.Publisher(Imagen, f"127.0.0.1:{sub.port}", topic="/cam")
        try:
            arr = np.arange(480 * 640 * 3, dtype=np.uint8).reshape(480, 640, 3)
            got = None
            end = time.monotonic() + 3
            while got is None and time.monotonic() < end:
                pub.publish(Imagen.from_array(arr, encoding="rgb8"))
                s = sub.receive(0.05)
                if s is not None and s.envelope.flags & wire.FLAG_SHM:
                    got = s
            self.assertIsNotNone(got)
            img = got.message.as_array()
            self.assertEqual(img.shape, (480, 640, 3))
            self.assertFalse(img.flags.writeable)
            self.assertTrue(np.array_equal(img, arr))
        finally:
            sub.close()
            pub.close()


@unittest.skipUnless(_shm.enabled(), "sin memoria compartida")
class TestPickleConMemoriaCompartida(unittest.TestCase):
    """multiprocessing y DataLoader (0.10.1) con una Imagen que llegó por
    memoria compartida: pickle copia la vista a bytes, lo que llega del otro
    lado es igual a lo publicado, y el bloque vuelve al publicador al soltar el
    mensaje original. El suscriptor es el del núcleo que se prueba; el
    publicador, de Python puro, para poder mirar su pool por dentro."""

    def setUp(self):
        from agmen.msgs import Imagen

        self.sub = agmen.Subscriber(Imagen, "127.0.0.1:0")
        self.pub = pubsub._PurePublisher(Imagen, f"127.0.0.1:{self.sub.port}", topic="/camara")
        self.sent = Imagen(timestamp=123_456_789, frame_id="camara", width=640, height=480, encoding="bgr8",
                           step=1920, data=bytes(range(256)) * 3600)

    def tearDown(self):
        self.sub.close()
        self.pub.close()

    def _shared(self):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            self.pub.publish(self.sent)
            s = self.sub.receive(0.05)
            if s is not None and s.envelope.flags & wire.FLAG_SHM:
                return s
        self.fail("la Imagen no llegó por memoria compartida")

    def _busy(self):
        with self.pub._tcp.lock:
            return self.pub._tcp.pool.busy()

    def _wait_free(self, timeout=2.0):
        end = time.monotonic() + timeout
        while self._busy() and time.monotonic() < end:
            gc.collect()
            time.sleep(0.01)
        return self._busy()

    def test_pickle_es_igual_a_lo_publicado(self):
        s = self._shared()
        self.assertTrue(is_view(s), "llegó como vista del bloque")
        for proto in range(2, pickle.HIGHEST_PROTOCOL + 1):
            with self.subTest(protocolo=proto):
                back = pickle.loads(pickle.dumps(s.message, protocol=proto))
                self.assertEqual(back, self.sent)
                self.assertIsInstance(back.data, bytes, "una copia, no una vista del bloque")

    def test_un_tipo_escrito_a_mano_tambien(self):
        # Antes solo los tipos generados (agmen gen, agmen.msgs) sabían hacer
        # pickle de lo recibido: un @dataclass(wire.Message) con bytes lanzaba
        # "cannot pickle 'memoryview' object". Ahora wire.Message se lo da a
        # todos los tipos arrow; también a los structs anidados (dicts).
        sub = agmen.Subscriber(Nube, "127.0.0.1:0")
        pub = pubsub._PurePublisher(Nube, f"127.0.0.1:{sub.port}", topic="/nube")
        sent = Nube(BIG, [{"x": 1.5, "etiqueta": b"a" * 10}])
        try:
            got = None
            end = time.monotonic() + 3
            while got is None and time.monotonic() < end:
                pub.publish(sent)
                s = sub.receive(0.05)
                if s is not None and s.envelope.flags & wire.FLAG_SHM:
                    got = s
            self.assertIsNotNone(got)
            self.assertIsInstance(got.message.data, memoryview)
            back = pickle.loads(pickle.dumps(got.message))
            self.assertEqual(bytes(back.data), BIG)
            self.assertEqual(back.puntos, [{"x": 1.5, "etiqueta": b"a" * 10}])
        finally:
            sub.close()
            pub.close()

    def test_multiprocessing_pool_con_spawn(self):
        s = self._shared()
        with multiprocessing.get_context("spawn").Pool(1) as pool:
            echoed = pool.apply(_eco, (s.message,))
            summary = pool.apply(_resumen, (s.message,))
        self.assertEqual(echoed, self.sent)
        self.assertEqual(summary, ("Imagen", 123_456_789, "camara", 640, 480, "bgr8", 1920, self.sent.data))

    def test_el_bloque_vuelve_aunque_exista_la_copia(self):
        s = self._shared()
        gc.collect()
        time.sleep(0.2)
        self.assertEqual(self._busy(), 1, "mientras exista el mensaje, su bloque sigue prestado")
        copy = pickle.loads(pickle.dumps(s.message))
        del s
        self.assertEqual(self._wait_free(), 0, "al soltar el original, el bloque vuelve")
        # El publicador reutiliza el bloque con otros datos: la copia no cambia.
        other = dataclasses.replace(self.sent, data=bytes(len(self.sent.data)))
        for _ in range(5):
            self.pub.publish(other)
            r = self.sub.receive(1)
            self.assertIsNotNone(r)
            self.assertEqual(bytes(r.message.data), other.data)
            del r
        self.assertEqual(copy, self.sent)


class TestElBloqueVuelveAlSoltarLaUltimaVista(unittest.TestCase):
    """Con el publicador en Python puro se puede mirar su pool por dentro."""

    def test_vuelve_cuando_muere_la_ultima_vista(self):
        sub = agmen.Subscriber(Img, "127.0.0.1:0")
        pub = pubsub._PurePublisher(Img, f"127.0.0.1:{sub.port}", topic="/img")
        try:
            s = until_shared(pub, sub)
            self.assertIsNotNone(s)
            gc.collect()
            view = s.message.data[10:20]          # una vista de la vista
            del s
            gc.collect()

            def busy():
                with pub._tcp.lock:
                    return pub._tcp.pool.busy()

            time.sleep(0.2)
            self.assertEqual(busy(), 1, "mientras exista una vista, el bloque sigue prestado")
            self.assertEqual(bytes(view), BIG[10:20])
            del view
            gc.collect()
            end = time.monotonic() + 2
            while busy() and time.monotonic() < end:
                time.sleep(0.01)
            self.assertEqual(busy(), 0, "al soltar la última vista, el bloque vuelve")
        finally:
            sub.close()
            pub.close()


# --- D6 a′: el tope según la RAM, y el pool que se encoge -------------------------

def fake_ram(n):
    """Finge la RAM de la máquina en los dos núcleos (0 o None = la de verdad).
    Es un gancho interno de las pruebas, no una variable de entorno."""
    _shm._fake_ram = n or None
    if _backend.NATIVE:
        _backend.core._shm_fake_ram(int(n or 0))


def limit():
    """(bytes, de dónde sale) según el núcleo que publica."""
    return _backend.core.shm_limit() if _backend.NATIVE else _shm.limit()


def my_devshm_entries():
    """Linux: los bloques de este proceso que se ven en /dev/shm."""
    if not sys.platform.startswith("linux"):
        return []
    prefix = f"agm{os.getpid() & 0xFFFFFFFF:08x}"
    return [f for f in os.listdir("/dev/shm") if f.startswith(prefix)]


def my_shm_mappings():
    """Linux: cuántos bloques de Agmen tiene mapeados este proceso."""
    if not sys.platform.startswith("linux"):
        return 0
    with open("/proc/self/maps", encoding="utf-8", errors="replace") as fh:
        return sum("/dev/shm/agm" in line for line in fh)


class TestTopeSegunLaRam(unittest.TestCase):
    def tearDown(self):
        fake_ram(0)

    def test_una_maquina_de_1_gb(self):
        with EnvVar("AGMEN_SHM_MB", None):
            fake_ram(1 << 30)
            quarter = 256 << 20
            dev = _shm.devshm_size()
            want = min(quarter, dev // 2) if dev else quarter
            self.assertEqual(limit(), (want, "¼ de la RAM" if want == quarter else "½ de /dev/shm"))
            self.assertEqual(_shm.limit(), limit(), "Python puro y C++ calculan lo mismo")
        with EnvVar("AGMEN_SHM_MB", "600"):                 # manda, aunque pase de ¼ de la RAM
            self.assertEqual(limit(), (600 << 20, "AGMEN_SHM_MB"))

    def test_la_de_verdad(self):
        fake_ram(0)
        with EnvVar("AGMEN_SHM_MB", None):
            cap, _ = limit()
            self.assertLessEqual(cap, 512 << 20)
            if _shm.physical_ram():
                self.assertLessEqual(cap, _shm.physical_ram() // 4)
            if _shm.devshm_size():
                self.assertLessEqual(cap, _shm.devshm_size() // 2)

    @unittest.skipUnless(_shm.enabled(), "sin memoria compartida")
    def test_el_publicador_lo_respeta(self):
        # Con 16 MiB de RAM, el tope es 4 MiB: guardando todo, el resto va por TCP.
        fake_ram(16 << 20)
        sub = agmen.Subscriber(Img, "127.0.0.1:0")
        pub = agmen.Publisher(Img, f"127.0.0.1:{sub.port}", topic="/img")
        try:
            with EnvVar("AGMEN_SHM_MB", None), redirect_stderr(io.StringIO()) as err:
                self.assertIsNotNone(until_shared(pub, sub))
                kept = []
                for k in range(8):
                    pub.publish(Img(k, 0, BIG))
                    kept += drain(sub, 0.2)
                self.assertLessEqual(pub.shm_bytes, 4 << 20)
                self.assertGreater(pub.shm_fallbacks, 0)
            self.assertIn("¼ de la RAM", err.getvalue())
        finally:
            sub.close()
            pub.close()


@unittest.skipUnless(_shm.enabled(), "sin memoria compartida")
class TestPoolSeEncoge(unittest.TestCase):
    """El pool de Python puro, por dentro (tests/test_shm.cpp hace lo mismo en C++)."""

    def test_libera_lo_que_nadie_usa_y_nunca_lo_prestado(self):
        owner = 0xA6E70003
        pool = _shm.Pool(owner)
        try:
            held = []
            for g in range(1, 7):                  # una ráfaga: 6 sin devolver = 6 bloques
                d = pool.write(g, BIG)
                self.assertIsInstance(d, _shm.Descriptor)
                pool.hold(d.slot)
                held.append(d)
            probe = pool.probe()
            self.assertEqual(pool.count(), 6)
            for d in held[1:]:
                pool.release(d.slot)
            now = time.monotonic()
            self.assertEqual(pool.trim(now), 0, "recién usados: nada")
            self.assertGreater(pool.trim(now + _shm.IDLE_FREE + 1), 0)
            self.assertEqual((pool.count(), pool.busy()), (1, 1), "el prestado nunca se libera")
            for d in held[1:] + [probe]:
                with self.assertRaises(_shm.ShmError):
                    _shm.open_readonly(d.name, _shm.PROBE_SIZE)
            _shm.open_readonly(held[0].name, _shm.PROBE_SIZE).close()
            if pool.registry:
                with open(pool.registry, encoding="ascii") as fh:
                    self.assertEqual(fh.read().split(), [held[0].name], "el registro dice lo que existe")
            pool.release(held[0].slot)
            self.assertGreater(pool.trim(time.monotonic() + _shm.IDLE_FREE + 1), 0)
            self.assertEqual((pool.count(), pool.bytes()), (0, 0))
            if pool.registry:
                self.assertFalse(os.path.exists(pool.registry))
            self.assertIsInstance(pool.write(7, BIG), _shm.Descriptor, "y vuelve a crecer")
        finally:
            pool.close()

    def test_reusa_el_mas_reciente(self):
        pool = _shm.Pool(0xA6E70004)
        try:
            a, b = pool.write(1, BIG), pool.write(2, BIG)       # a libre: b es otro bloque solo si a está prestado
            self.assertEqual(a.slot, b.slot)
            pool.hold(a.slot)
            c = pool.write(3, BIG)
            pool.hold(c.slot)
            pool.release(a.slot)
            pool.release(c.slot)                                 # c se soltó al último: se usa c
            self.assertEqual(pool.write(4, BIG).slot, c.slot)
        finally:
            pool.close()


@unittest.skipUnless(_shm.enabled(), "sin memoria compartida")
class TestRafaga4K(unittest.TestCase):
    """Una cámara 4K (24.9 MB por imagen) y un suscriptor que guarda todo lo que
    le llega durante la ráfaga; luego lo suelta y la cámara se calla: a los 5 s,
    el publicador ya no aparta nada y (Linux) /dev/shm queda limpio."""

    def test_el_pool_se_encoge(self):
        frame = bytes([7]) * (3840 * 2160 * 3)
        sub = agmen.Subscriber(Img, "127.0.0.1:0")
        pub = agmen.Publisher(Img, f"127.0.0.1:{sub.port}", topic="/camara")
        try:
            self.assertIsNotNone(until_shared(pub, sub, frame))
            peak = 0
            kept = []
            with redirect_stderr(io.StringIO()):
                for k in range(12):
                    pub.publish(Img(k, 0, frame))
                    kept += drain(sub, 0.03)
                    peak = max(peak, pub.shm_bytes)
            self.assertTrue(kept)
            del kept
            gc.collect()
            t0 = time.monotonic()
            while pub.shm_bytes and time.monotonic() - t0 < 10:
                sub.receive(0.1)             # el suscriptor sigue escuchando: no llega nada
            seconds = time.monotonic() - t0
            after = pub.shm_bytes
            print(f"\n  ráfaga 4K ({_backend.NAME}): {peak / (1 << 20):.0f} MiB apartados → "
                  f"{after / (1 << 20):.0f} MiB a los {seconds:.1f} s sin usar", file=sys.stderr)
            self.assertGreaterEqual(peak, 50 << 20, "al menos dos imágenes 4K")
            self.assertEqual(after, 0)
            self.assertGreaterEqual(seconds, 4.0, "no antes de 5 s (menos el segundo del hilo)")
            if _backend.NATIVE:
                time.sleep(1.1)
                sub.receive(0.1)             # suelta los bloques que tenía abiertos
                self.assertEqual(sub._core.shm_mapped, 0)
            self.assertEqual(my_devshm_entries(), [], "ni el bloque de prueba queda en /dev/shm")
            self.assertEqual(my_shm_mappings(), 0, "nadie en este proceso los tiene mapeados")
        finally:
            sub.close()
            pub.close()


class TestRespaldoPorTcp(unittest.TestCase):
    def test_agmen_shm_0(self):
        with EnvVar("AGMEN_SHM", "0"):
            sub = agmen.Subscriber(Img, "127.0.0.1:0")
            pub = agmen.Publisher(Img, f"127.0.0.1:{sub.port}", topic="/img")
            try:
                got = []
                for k in range(20):
                    pub.publish(Img(k, 0, BIG))
                    got += drain(sub, 0.2)
                self.assertEqual([s.message.w for s in got], list(range(20)))
                self.assertFalse(any(s.envelope.flags & wire.FLAG_SHM for s in got))
                self.assertEqual(pub.shm_subscribers, 0)
            finally:
                sub.close()
                pub.close()

    def _no_puede_abrir(self, publisher_cls):
        """Un suscriptor que no puede abrir la memoria del publicador (como si
        estuviera en otra computadora): todo llega por TCP y se avisa una vez."""
        real = _shm.open_readonly

        def otra_computadora(name, size):
            raise _shm.ShmError(f"no pude abrir {name} (simulado: otra computadora)")

        _shm.open_readonly = otra_computadora
        try:
            sub = pubsub._PureSubscriber(Img, "127.0.0.1:0")
            pub = publisher_cls(Img, f"127.0.0.1:{sub.port}", topic="/img")
            try:
                got = []
                with redirect_stderr(io.StringIO()) as err:
                    for k in range(30):
                        pub.publish(Img(k, 0, BIG))
                        got += drain(sub, 0.05)
                    got += drain(sub)
                self.assertEqual([s.message.w for s in got], list(range(30)), "ninguno se pierde")
                self.assertFalse(any(s.envelope.flags & wire.FLAG_SHM for s in got))
                self.assertEqual(pub.shm_subscribers, 0)
                self.assertEqual(err.getvalue().count("llega por TCP y no por memoria compartida"), 1)
                self.assertIn("otra computadora", err.getvalue())
            finally:
                sub.close()
                pub.close()
        finally:
            _shm.open_readonly = real

    def test_otra_computadora_con_publicador_python(self):
        self._no_puede_abrir(pubsub._PurePublisher)

    @unittest.skipUnless(_backend.NATIVE, "sin el núcleo de C++")
    def test_otra_computadora_con_publicador_cpp(self):
        self._no_puede_abrir(pubsub._NativePublisher)

    @unittest.skipIf(sys.platform == "win32", "en Windows un bloque no tiene nombre que borrar")
    def test_el_bloque_de_prueba_no_existe(self):
        # Cualquier suscriptor (C++ o Python puro): el bloque de prueba ya no está.
        pub = pubsub._PurePublisher(Img, "127.0.0.1:9", topic="/img")   # sin nadie
        sub = agmen.Subscriber(Img, "127.0.0.1:0")
        try:
            pub._tcp.pool = _shm.Pool(pub.source_id)
            d = pub._tcp.pool.probe()
            _shm.unlink(d.name)
            pub._tcp.update([("127.0.0.1", sub.port)], pub._announcement())
            pub.dests = [("127.0.0.1", sub.port)]
            got = []
            with redirect_stderr(io.StringIO()):
                for k in range(20):
                    pub.publish(Img(k, 0, BIG))
                    got += drain(sub, 0.05)
                got += drain(sub)
            self.assertEqual([s.message.w for s in got], list(range(20)))
            self.assertFalse(any(s.envelope.flags & wire.FLAG_SHM for s in got))
            self.assertEqual(pub.shm_subscribers, 0)
        finally:
            sub.close()
            pub.close()

    def test_tope_de_memoria(self):
        with EnvVar("AGMEN_SHM_MB", "2"):     # cabe un bloque de 1 MB, no dos
            sub = agmen.Subscriber(Img, "127.0.0.1:0")
            pub = agmen.Publisher(Img, f"127.0.0.1:{sub.port}", topic="/img")
            try:
                held = until_shared(pub, sub)
                self.assertIsNotNone(held)          # este lo tenemos prestado
                gc.collect()
                with redirect_stderr(io.StringIO()):
                    pub.publish(Img(77, 0, BIG))
                    s = sub.receive(1)
                self.assertIsNotNone(s)
                self.assertEqual(s.message.w, 77)
                self.assertFalse(s.envelope.flags & wire.FLAG_SHM, "no cupo: llegó por TCP")
                self.assertGreaterEqual(pub.shm_fallbacks, 1)
                self.assertEqual(bytes(held.message.data), BIG)
            finally:
                sub.close()
                pub.close()


@unittest.skipUnless(_backend.NATIVE, "sin el núcleo de C++")
class TestCruzado(unittest.TestCase):
    """Los mismos bytes y nombres en C++ y en Python puro."""

    def _cruce(self, pub_cls, sub_cls):
        sub = sub_cls(Img, "127.0.0.1:0")
        pub = pub_cls(Img, f"127.0.0.1:{sub.port}", topic="/img")
        try:
            s = until_shared(pub, sub)
            self.assertIsNotNone(s)
            self.assertEqual(bytes(s.message.data), BIG)
            n = 0
            for k in range(30):
                pub.publish(Img(k, 0, BIG))
                got = sub.receive(1)
                n += bool(got and got.envelope.flags & wire.FLAG_SHM and bytes(got.message.data) == BIG)
            self.assertEqual(n, 30)
        finally:
            sub.close()
            pub.close()

    def test_cpp_a_python(self):
        self._cruce(pubsub._NativePublisher, pubsub._PureSubscriber)

    def test_python_a_cpp(self):
        self._cruce(pubsub._PurePublisher, pubsub._NativeSubscriber)


class TestConHub(unittest.TestCase):
    def setUp(self):
        self.hub = hubmod.Hub("127.0.0.1:0", log=lambda t: None)
        self.old = os.environ.get("AGMEN_HUB")
        os.environ["AGMEN_HUB"] = f"127.0.0.1:{self.hub.sock.getsockname()[1]}"
        self.stop = threading.Event()
        threading.Thread(target=self.hub.serve, args=(self.stop,), daemon=True).start()

    def tearDown(self):
        self.stop.set()
        if self.old is None:
            os.environ.pop("AGMEN_HUB", None)
        else:
            os.environ["AGMEN_HUB"] = self.old

    def test_dos_suscriptores_un_solo_bloque(self):
        a = agmen.Subscriber(Img, "/cam")
        b = agmen.Subscriber(Img, "/cam")
        pub = agmen.Publisher(Img, "/cam")
        try:
            end = time.monotonic() + 3
            while pub.shm_subscribers < 2 and time.monotonic() < end:
                pub.publish(Img(0, 0, BIG))
                drain(a, 0.02)
                drain(b, 0.02)
            self.assertEqual(pub.shm_subscribers, 2)
            for k in range(1, 21):
                pub.publish(Img(k, 0, BIG))
                sa, sb = a.receive(1), b.receive(1)
                self.assertTrue(sa.envelope.flags & wire.FLAG_SHM and sb.envelope.flags & wire.FLAG_SHM)
                self.assertEqual((sa.message.w, sb.message.w), (k, k))
                self.assertEqual(bytes(sa.message.data), BIG)
        finally:
            for x in (a, b, pub):
                x.close()


_CHILD = """
import os, sys, time
sys.path.insert(0, {python!r})
os.environ["AGMEN_BACKEND"] = {backend!r}
import agmen
from agmen import pubsub, wire
from dataclasses import dataclass

@dataclass
class Img(wire.Message):
    SCHEMA = "Img{{u32 w;u32 h;bytes data}}"
    w: int
    h: int
    data: bytes

sub = pubsub._PureSubscriber(Img, "127.0.0.1:0")
pub = agmen.Publisher(Img, f"127.0.0.1:{{sub.port}}", topic="/img")
end = time.monotonic() + 3
while time.monotonic() < end:
    pub.publish(Img(1, 0, b"x" * 1_000_000))
    s = sub.receive(0.05)
    if s is not None and s.envelope.flags & wire.FLAG_SHM:
        print("LISTO", flush=True)
        break
os._exit(0)      # como si se cayera: nadie cierra nada
"""


@unittest.skipIf(sys.platform == "win32", "en Windows el sistema borra los bloques solo")
class TestLimpieza(unittest.TestCase):
    def _registry(self):
        d = _shm._registry_dir()
        return {f for f in os.listdir(d)} if os.path.isdir(d) else set()

    def _muere_y_se_limpia(self, backend):
        before = self._registry()
        code = _CHILD.format(python=str(ROOT / "python"), backend=backend)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertIn("LISTO", out.stdout, out.stderr)
        left = self._registry() - before
        self.assertEqual(len(left), 1, "el proceso que murió dejó su registro")
        path = os.path.join(_shm._registry_dir(), left.pop())
        with open(path, encoding="ascii") as fh:
            names = [ln.strip() for ln in fh if ln.strip()]
        self.assertTrue(names)
        mm = _shm.open_readonly(names[0], 4096)          # sus bloques siguen ahí...
        mm.close()
        _shm.sweep()
        self.assertFalse(os.path.exists(path))
        for name in names:
            with self.assertRaises(_shm.ShmError):      # ...hasta que alguien limpia
                _shm.open_readonly(name, 4096)

    def test_python_puro(self):
        self._muere_y_se_limpia("pure")

    @unittest.skipUnless(_backend.NATIVE, "sin el núcleo de C++")
    def test_cpp(self):
        # Lo que deja C++ lo limpia Python: el mismo registro.
        self._muere_y_se_limpia("native")


if __name__ == "__main__":
    unittest.main()

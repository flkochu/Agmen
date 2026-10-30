"""Pruebas del formato en Python. Deben dar los mismos valores que Rust.

    python -m unittest discover -s python
"""

import unittest

import rfw


class TestRfw(unittest.TestCase):
    def sample(self):
        return rfw.ImuSample(accel=(0.5, -9.75, 0.0), gyro=(0.25, 0.5, -0.125))

    def test_type_id_igual_que_rust(self):
        self.assertEqual(rfw.fnv1a64(b""), 0xCBF29CE484222325)
        self.assertEqual(rfw.fnv1a64(b"a"), 0xAF63DC4C8601EC8C)
        self.assertEqual(rfw.ImuSample.TYPE_ID, 0x14626BEE23E54E09)

    def test_ida_y_vuelta(self):
        data = rfw.encode(self.sample(), 42, 123_456_789)
        self.assertEqual(len(data), 56)
        h, m = rfw.decode(rfw.ImuSample, data)
        self.assertEqual((h.seq, h.stamp_ns, h.payload_len), (42, 123_456_789, 24))
        self.assertEqual(m, self.sample())

    def test_layout_del_header(self):
        data = rfw.encode(self.sample(), 1, 2)
        self.assertEqual(data[0:3], b"RFW")
        self.assertEqual(data[3], 0)
        self.assertEqual(data[4:12], rfw.ImuSample.TYPE_ID.to_bytes(8, "little"))
        self.assertEqual(data[12:20], (1).to_bytes(8, "little"))
        self.assertEqual(data[20:28], (2).to_bytes(8, "little"))
        self.assertEqual(data[28:32], (24).to_bytes(4, "little"))

    def test_rechaza_tipo_incorrecto(self):
        data = bytearray(rfw.encode(self.sample(), 0, 0))
        data[4] ^= 0xFF
        with self.assertRaisesRegex(rfw.WireError, "TIPO INCOMPATIBLE"):
            rfw.decode(rfw.ImuSample, bytes(data))

    def test_rechaza_basura(self):
        with self.assertRaisesRegex(rfw.WireError, "demasiado corto"):
            rfw.decode(rfw.ImuSample, b"hola")
        with self.assertRaisesRegex(rfw.WireError, "magic"):
            rfw.decode(rfw.ImuSample, b"XYZ" + bytes(53))


if __name__ == "__main__":
    unittest.main()

"""Mide Arrow IPC vs codificación compacta (RFC 0001, sección 2).

    pip install pyarrow numpy
    python bench/arrow_vs_compact.py
"""
import struct, timeit, io
import numpy as np, pyarrow as pa, pyarrow.ipc as ipc

def ipc_batch_bytes(batch):
    # Mensaje IPC de un RecordBatch SIN el schema (el schema viaja una vez, por hash)
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, batch.schema) as w:
        w.write_batch(batch)
    full = sink.getvalue().size
    sink2 = pa.BufferOutputStream()
    with ipc.new_stream(sink2, batch.schema) as w: pass
    schema_only = sink2.getvalue().size
    return full - schema_only, schema_only

# --- Mensaje chico: comando de motor (2 x f32) y IMU (6 x f32)
for name, n in [("MotorCommand 2xf32", 2), ("ImuSample 6xf32", 6)]:
    cols = {f"f{i}": pa.array([0.5], pa.float32()) for i in range(n)}
    b = pa.record_batch(cols)
    batch_sz, schema_sz = ipc_batch_bytes(b)
    print(f"{name:22s} compacto={4*n:>4} B   arrow_ipc(batch)={batch_sz:>4} B   (schema aparte={schema_sz} B)")

# Tiempos en Python: construir+serializar y deserializar un IMU
fmt = struct.Struct("<6f")
vals = (0.1,0.2,9.8,0.0,0.0,0.3)
schema = pa.schema([(f"f{i}", pa.float32()) for i in range(6)])
def arrow_encode():
    b = pa.record_batch([pa.array([v], pa.float32()) for v in vals], schema=schema)
    return b.serialize()
enc = arrow_encode()
def arrow_decode():
    return ipc.read_record_batch(enc, schema)
cbytes = fmt.pack(*vals)
dt = np.dtype([(f"f{i}", "<f4") for i in range(6)], align=True)
N=20000
t = lambda f: timeit.timeit(f, number=N)/N*1e6
print(f"\nPython, IMU 1 mensaje:  compacto encode={t(lambda: fmt.pack(*vals)):.2f} µs  decode(numpy zero-copy)={t(lambda: np.frombuffer(cbytes, dt)):.2f} µs")
print(f"                        arrow    encode={t(arrow_encode):.2f} µs  decode={t(arrow_decode):.2f} µs")

# --- Mensaje grande: imagen 640x480x3 u8 como fixed_shape_tensor
img = np.random.randint(0,255,(480,640,3),dtype=np.uint8)
tt = pa.fixed_shape_tensor(pa.uint8(), [480,640,3])
arr = pa.FixedShapeTensorArray.from_numpy_ndarray(np.ascontiguousarray(img.reshape(1,480,640,3)))
b = pa.record_batch([arr], names=["image"])
batch_sz, schema_sz = ipc_batch_bytes(b)
print(f"\nImagen 640x480x3: datos={img.nbytes} B  arrow_ipc(batch)={batch_sz} B  overhead={batch_sz-img.nbytes} B")
ser = b.serialize()
back = ipc.read_record_batch(ser, b.schema)
nd = back.column(0).to_numpy_ndarray()
buf_addr = back.column(0).storage.values.buffers()[1].address
print("numpy de vuelta: shape", nd.shape, " zero-copy sobre el buffer recibido:", nd.__array_interface__['data'][0]==buf_addr)

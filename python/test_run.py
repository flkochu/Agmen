"""Pruebas del lector de robot.toml (agmen run). Sin procesos ni red.

    python -m unittest discover -s python
"""

import io
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agmen.run import ConfigError, Printer, load_config

MINIMO = '[robot]\nname = "r"\n\n[[node]]\nname = "motor"\ncmd = "./build/bin/motor --rate 10"\n'


class TestConfig(unittest.TestCase):
    def load(self, text: str):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "robot.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    def assertConfigError(self, text: str, must_contain: str):
        with self.assertRaises(ConfigError) as ctx:
            self.load(text)
        self.assertIn(must_contain, str(ctx.exception))

    def test_minimo(self):
        cfg = self.load(MINIMO)
        self.assertEqual(cfg.name, "r")
        self.assertEqual(cfg.nodes[0].cmd, ["./build/bin/motor", "--rate", "10"])
        self.assertEqual(cfg.nodes[0].restart, "never")
        self.assertTrue(cfg.nodes[0].autostart)
        self.assertIsNone(cfg.web.teleop)

    def test_python_se_reemplaza_por_el_interprete_actual(self):
        cfg = self.load(MINIMO + '\n[[node]]\nname = "ia"\ncmd = "python cerebro.py"\n')
        self.assertEqual(cfg.nodes[1].cmd, [sys.executable, "cerebro.py"])

    def test_cmd_como_lista(self):
        cfg = self.load('[[node]]\nname = "a"\ncmd = ["python", "-c", "print(1)"]\n')
        self.assertEqual(cfg.nodes[0].cmd, [sys.executable, "-c", "print(1)"])

    def test_before_y_teleop(self):
        cfg = self.load('[robot]\nname = "r"\nbefore = ["cmake --build build"]\n' + MINIMO.split("\n", 2)[2]
                        + '\n[web.teleop]\ntarget = "127.0.0.1:7500"\nmax_speed = 0.5\n')
        self.assertEqual(cfg.before, [["cmake", "--build", "build"]])
        self.assertEqual(cfg.web.teleop.target, "127.0.0.1:7500")
        self.assertEqual(cfg.web.teleop.max_speed, 0.5)

    def test_clave_mal_escrita_sugiere(self):
        self.assertConfigError('[[node]]\nnmae = "a"\ncmd = "x"\n', "¿Quisiste decir 'name'?")

    def test_before_fuera_de_robot(self):
        self.assertConfigError('before = ["x"]\n' + MINIMO, "clave desconocida 'before'")

    def test_restart_invalido(self):
        self.assertConfigError(MINIMO + 'restart = "a veces"\n', "no es válido")

    def test_sin_nodos(self):
        self.assertConfigError('[robot]\nname = "r"\n', "no declara ningún nodo")

    def test_nombres_repetidos(self):
        self.assertConfigError(MINIMO + '\n[[node]]\nname = "motor"\ncmd = "y"\n', "dos nodos llamados 'motor'")

    def test_falta_cmd(self):
        self.assertConfigError('[[node]]\nname = "a"\n', "falta 'cmd'")

    def test_toml_roto(self):
        self.assertConfigError('[robot\nname = "r"\n', "no es TOML válido")

    def test_teleop_sin_target(self):
        self.assertConfigError(MINIMO + '\n[web.teleop]\nmax_speed = 1.0\n', "falta 'target'")

    # --- [clock] (RFC 0006) ---
    def test_reloj_real_por_defecto(self):
        cfg = self.load(MINIMO)
        self.assertEqual(cfg.clock.mode, "real")
        self.assertEqual(cfg.nodes[0].env["AGMEN_CLOCK"], "real")

    def test_reloj_sim_llega_a_todos_los_nodos(self):
        cfg = self.load(MINIMO + '\n[[node]]\nname = "b"\ncmd = "x"\nenv = { AGMEN_CLOCK = "real" }\n'
                        '\n[clock]\nmode = "sim"\nspeed = 2.0\n')
        self.assertEqual((cfg.clock.mode, cfg.clock.speed, cfg.clock.source), ("sim", 2.0, "agmen"))
        for n in cfg.nodes:   # aunque el nodo diga otra cosa: un solo reloj
            self.assertEqual((n.env["AGMEN_CLOCK"], n.env["AGMEN_CLOCK_SOURCE"]), ("sim", "agmen"))

    def test_reloj_fuente_es_un_nodo(self):
        cfg = self.load(MINIMO + '\n[[node]]\nname = "simulador"\ncmd = "x"\n\n[clock]\nmode = "sim"\n'
                        'source = "simulador"\n')
        self.assertEqual(cfg.nodes[0].env["AGMEN_CLOCK_SOURCE"], "simulador")
        self.assertConfigError(MINIMO + '\n[[node]]\nname = "simulador"\ncmd = "x"\n\n[clock]\nmode = "sim"\n'
                               'source = "simuladr"\n', "¿Quisiste decir 'simulador'?")

    def test_reloj_errores(self):
        self.assertConfigError(MINIMO + '\n[clock]\nmode = "rapido"\n', "no es válido")
        self.assertConfigError(MINIMO + '\n[clock]\nmode = "replay"\n', "--play archivo.mcap")
        self.assertConfigError(MINIMO + '\n[clock]\nmode = "sim"\nspeed = 0\n', "mayor que 0")
        self.assertConfigError(MINIMO + '\n[clock]\nspeed = 2.0\n', 'solo aplica con mode = "sim"')
        self.assertConfigError(MINIMO + '\n[clock]\nmodo = "sim"\n', "¿Quisiste decir 'mode'?")

    # --- [record] (RFC 0010) ---
    def test_sin_record_no_graba(self):
        cfg = self.load(MINIMO)
        self.assertIsNone(cfg.record)
        self.assertEqual(cfg.path.name, "robot.toml")

    def test_record(self):
        cfg = self.load(MINIMO + '\n[record]\ntopics = ["/camara", "/imu"]\nsplit = "1GiB"\n')
        r = cfg.record
        self.assertEqual((r.topics, r.dir, r.split, r.compress, r.auto),
                         (["/camara", "/imu"], "grabaciones", "1GiB", None, True))
        r = self.load(MINIMO + '\n[record]\ntopics = "*"\ndir = "datos"\ncompress = "zstd"\nauto = false\n').record
        self.assertEqual((r.topics, r.dir, r.compress, r.auto), (["*"], "datos", "zstd", False))

    def test_record_errores(self):
        self.assertConfigError(MINIMO + '\n[record]\ntopics = ["camara"]\n', "una lista de topics")
        self.assertConfigError(MINIMO + '\n[record]\ncompress = "gzip"\n', '"zstd" o "lz4"')
        self.assertConfigError(MINIMO + '\n[record]\nsplit = "mucho"\n', "[record] split")
        self.assertConfigError(MINIMO + '\n[record]\ntopic = ["/imu"]\n', "¿Quisiste decir 'topics'?")
        self.assertConfigError('record = 1\n' + MINIMO, "[record] es una tabla")


def nodo(name, profile=None, extra=""):
    p = "" if profile is None else f"profile = {profile}\n"
    return f'\n[[node]]\nname = "{name}"\ncmd = "x"\n{p}{extra}'


# El robot del RFC 0011 §3.2
ROBOT_RFC = (nodo("imu", '"real"') + nodo("motor", '"real"') + nodo("mundo", '"sim"') + nodo("cerebro"))


class TestPerfiles(unittest.TestCase):
    """Perfiles en robot.toml (RFC 0011 §3.2, D2 y D3): load_config(path, profile)."""

    def load(self, text: str, profile=None):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "robot.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path, profile)

    def assertConfigError(self, text: str, *must_contain: str, profile=None):
        with self.assertRaises(ConfigError) as ctx:
            self.load(text, profile)
        for frag in must_contain:
            self.assertIn(frag, str(ctx.exception))
        return str(ctx.exception)

    def names(self, cfg):
        return [n.name for n in cfg.nodes]

    # 1. el robot del RFC
    def test_rfc_real_por_defecto(self):
        cfg = self.load(ROBOT_RFC)
        self.assertEqual(cfg.profile, "real")
        self.assertEqual(self.names(cfg), ["imu", "motor", "cerebro"])
        self.assertEqual(cfg.skipped, [("mundo", ("sim",))])
        self.assertEqual(cfg.clock.mode, "real")

    def test_rfc_sim(self):
        cfg = self.load(ROBOT_RFC, "sim")
        self.assertEqual(cfg.profile, "sim")
        self.assertEqual(self.names(cfg), ["mundo", "cerebro"])
        self.assertEqual(cfg.skipped, [("imu", ("real",)), ("motor", ("real",))])

    def test_rfc_replay_no_corren_los_real(self):
        cfg = self.load(ROBOT_RFC, "replay")
        self.assertEqual(cfg.profile, "replay")
        self.assertEqual(self.names(cfg), ["cerebro"])
        self.assertEqual(cfg.skipped, [("imu", ("real",)), ("motor", ("real",)), ("mundo", ("sim",))])
        self.assertEqual(cfg.clock.mode, "real")   # replay: el reloj como hoy (lo cambia --play)

    def test_perfiles_conocidos_orden(self):
        cfg = self.load(ROBOT_RFC)
        self.assertEqual(cfg.profiles, ("real", "sim", "replay"))
        cfg = self.load(nodo("a", '["zeta", "mesa"]') + nodo("b", '"alfa"') + '\n[profile.beta]\nlocal = true\n')
        self.assertEqual(cfg.profiles, ("real", "sim", "replay", "alfa", "beta", "mesa", "zeta"))

    # 2. lista y perfil inventado
    def test_profile_como_lista(self):
        text = nodo("motor", '["real", "mesa"]') + nodo("cerebro")
        self.assertEqual(self.names(self.load(text)), ["motor", "cerebro"])
        cfg = self.load(text, "mesa")
        self.assertEqual(self.names(cfg), ["motor", "cerebro"])
        self.assertIn("mesa", cfg.profiles)
        cfg = self.load(text, "sim")
        self.assertEqual(self.names(cfg), ["cerebro"])
        self.assertEqual(cfg.skipped, [("motor", ("real", "mesa"))])

    def test_profile_en_lista_repetido_no_rompe(self):
        cfg = self.load(nodo("a", '["real", "real"]') + nodo("b"))
        self.assertEqual(self.names(cfg), ["a", "b"])

    def test_perfil_solo_en_tabla_existe(self):
        cfg = self.load(nodo("a") + '\n[profile.mesa]\nlocal = true\n', "mesa")
        self.assertEqual(cfg.profile, "mesa")
        self.assertTrue(cfg.local)
        self.assertEqual(self.names(cfg), ["a"])

    # 3. [robot] profile y el argumento
    def test_robot_profile_por_defecto(self):
        text = '[robot]\nname = "r"\nprofile = "sim"\n' + ROBOT_RFC
        cfg = self.load(text)
        self.assertEqual(cfg.profile, "sim")
        self.assertEqual(self.names(cfg), ["mundo", "cerebro"])

    def test_argumento_gana_a_robot_profile(self):
        text = '[robot]\nname = "r"\nprofile = "sim"\n' + ROBOT_RFC
        cfg = self.load(text, "real")
        self.assertEqual(cfg.profile, "real")
        self.assertEqual(self.names(cfg), ["imu", "motor", "cerebro"])

    def test_robot_profile_desconocido(self):
        msg = self.assertConfigError('[robot]\nprofile = "mesa"\n' + ROBOT_RFC, "mesa")
        self.assertIn("[robot] profile", msg)

    def test_robot_profile_desconocido_se_revisa_aunque_el_argumento_sea_valido(self):
        # RFC 0011 (corrección B): [robot] profile se valida SIEMPRE, también con --profile / --sim
        msg = self.assertConfigError('[robot]\nprofile = "mesa"\n' + ROBOT_RFC, "[robot] profile = 'mesa'", profile="sim")
        self.assertIn("no existe", msg)
        self.assertConfigError('[robot]\nprofile = "siim"\n' + ROBOT_RFC, "¿Quisiste decir 'sim'?", profile="sim")
        self.assertConfigError('[robot]\nprofile = "con espacio"\n' + ROBOT_RFC, "[robot] profile", profile="real")
        self.assertConfigError('[robot]\nprofile = 3\n' + ROBOT_RFC, "[robot] profile", profile="sim")

    def test_robot_profile_valido_el_argumento_gana(self):
        cfg = self.load('[robot]\nprofile = "sim"\n' + ROBOT_RFC, "real")
        self.assertEqual(cfg.profile, "real")
        self.assertEqual(self.names(cfg), ["imu", "motor", "cerebro"])

    # B/D/G/E: correcciones de la tercera ronda
    def test_web_de_arriba_se_revisa_aunque_el_perfil_traiga_su_web(self):
        text = nodo("a") + '\n[web]\nhtpp = "127.0.0.1:8000"\n\n[profile.sim]\nweb = { open_browser = false }\n'
        self.assertConfigError(text, "¿Quisiste decir 'http'?", profile="sim")
        self.assertConfigError(text, "¿Quisiste decir 'http'?")
        # y un [web] bueno de arriba con la tabla del perfil sigue valiendo
        ok = nodo("a") + '\n[web]\nhttp = "127.0.0.1:8000"\n\n[profile.sim]\nweb = { open_browser = false }\n'
        self.assertFalse(self.load(ok, "sim").web.open_browser)

    def test_reloj_source_gramatica_singular_y_plural(self):
        base = nodo("c")
        un = nodo("gazebo", '"sim"') + base + '\n[clock]\nmode = "sim"\nsource = "gazebo"\n'
        msg = self.assertConfigError(un, "es del perfil 'sim'", profile="real")
        self.assertNotIn("de los perfiles", msg)
        varios = nodo("gazebo", '["sim", "mesa2"]') + base + '\n[clock]\nmode = "sim"\nsource = "gazebo"\n'
        msg = self.assertConfigError(varios, "es de los perfiles 'sim', 'mesa2'", profile="real")
        self.assertNotIn("del perfiles", msg)

    def test_clock_mode_replay_no_dice_que_play_pone_todos_en_replay(self):
        msg = self.assertConfigError(nodo("a") + '\n[clock]\nmode = "replay"\n', "--play archivo.mcap")
        self.assertNotIn("pone a todos los nodos en replay", msg)
        self.assertNotIn("a todos los nodos", msg)

    # C: aviso de error de dedo en un perfil
    def test_aviso_perfil_parecido_a_real_con_un_solo_lugar(self):
        cfg = self.load(nodo("a") + nodo("b", '"rael"'))
        self.assertEqual(len(cfg.warnings), 1, cfg.warnings)
        w = cfg.warnings[0]
        for frag in ("[[node]] 'b'", "rael", "¿Quisiste decir 'real'?"):
            self.assertIn(frag, w)
        self.assertEqual(self.load(nodo("a") + nodo("b", '"rael"'), "sim").warnings, cfg.warnings,
                         "sale sea cual sea el perfil activo")

    def test_aviso_perfil_en_mux_y_topic(self):
        mux = ('\n[[mux]]\nname = "m"\noutput = "127.0.0.1:7500"\nprofile = "rael"\n'
               + '\n[[mux.input]]\nname = "t"\nlisten = "127.0.0.1:7510"\npriority = 10\n')
        w = self.load(nodo("a") + mux).warnings
        self.assertEqual(len(w), 1, w)
        self.assertIn("[[mux]] 'm'", w[0])
        topic = '\n[[topic]]\nname = "/x"\nprofile = "siim"\n'
        w = self.load(nodo("a") + topic).warnings
        self.assertEqual(len(w), 1, w)
        self.assertIn("[[topic]] '/x'", w[0])
        self.assertIn("¿Quisiste decir 'sim'?", w[0])

    def test_sin_aviso_mesa_tabla_declarada_o_dos_lugares_o_sin_perfiles(self):
        self.assertEqual(self.load(nodo("a") + nodo("b", '"mesa"')).warnings, [], "mesa no se parece")
        self.assertEqual(self.load(nodo("a") + nodo("b", '"rael"') + '\n[profile.rael]\nlocal = true\n').warnings, [])
        self.assertEqual(self.load(nodo("a") + nodo("b", '"rael"') + nodo("c", '"rael"')).warnings, [],
                         "nombrado en dos lugares: parece a propósito")
        self.assertEqual(self.load(nodo("a") + nodo("b", '"real"') + nodo("c", '"sim"')).warnings, [])
        self.assertEqual(self.load(nodo("a")).warnings, [])
        self.assertEqual(self.load(ROBOT_RFC).warnings, [])

    def test_robot_profile_nombre_invalido(self):
        self.assertConfigError('[robot]\nprofile = "con espacio"\n' + ROBOT_RFC, "[robot] profile")
        self.assertConfigError('[robot]\nprofile = 3\n' + ROBOT_RFC, "[robot] profile")

    # 4. perfil desconocido
    def test_perfil_desconocido_con_sugerencia(self):
        self.assertConfigError(ROBOT_RFC, "¿Quisiste decir 'sim'?", profile="sym")

    def test_perfil_desconocido_sin_parecido_lista_los_conocidos(self):
        msg = self.assertConfigError(ROBOT_RFC, "zzzzzz", profile="zzzzzz")
        for conocido in ("real", "sim", "replay"):
            self.assertIn(conocido, msg)
        self.assertNotIn("Quisiste decir", msg)

    def test_perfil_desconocido_nombre_con_basura(self):
        with self.assertRaises(ConfigError):
            self.load(ROBOT_RFC, "con espacio")
        with self.assertRaises(ConfigError):
            self.load(ROBOT_RFC, "")

    def test_perfil_sugerencia_incluye_los_inventados(self):
        self.assertConfigError(nodo("a", '"mesa"'), "¿Quisiste decir 'mesa'?", profile="mese")

    # 5. profile mal escrito
    def test_profile_invalido(self):
        for valor in ("[]", "3", '"con espacio"', '"sim!"', '"é"', '[1]', '["real", ""]', '""', "true"):
            with self.subTest(valor=valor):
                msg = self.assertConfigError(nodo("motor", valor), "profile")
                self.assertIn("[[node]] 'motor'", msg)

    def test_profile_invalido_en_mux_y_topic_nombra_la_tabla(self):
        mux = ('\n[[mux]]\nname = "m"\noutput = "127.0.0.1:7500"\nprofile = []\n'
               '[[mux.input]]\nname = "a"\nlisten = "127.0.0.1:7510"\npriority = 1\n')
        self.assertConfigError(nodo("a") + mux, "[[mux]] 'm'", "profile")
        self.assertConfigError(nodo("a") + '\n[[topic]]\nname = "/imu"\nprofile = 3\n', "/imu", "profile")
        self.assertConfigError(nodo("a") + '\n[[topic]]\nname = "/imu"\nprofile = "a b"\n', "/imu", "profile")

    def test_profile_en_perfil_no_activo_tambien_se_revisa(self):
        self.assertConfigError(nodo("a") + nodo("b", "[]"), "[[node]] 'b'", profile="sim")

    # 6. nombres repetidos
    def test_repetidos_disjuntos_ok(self):
        text = nodo("imu", '"real"') + nodo("imu", '"sim"') + nodo("cerebro")
        self.assertEqual(self.names(self.load(text)), ["imu", "cerebro"])
        self.assertEqual(self.names(self.load(text, "sim")), ["imu", "cerebro"])
        cfg = self.load(text, "replay")
        self.assertEqual(self.names(cfg), ["cerebro"])
        self.assertEqual(cfg.skipped, [("imu", ("real",)), ("imu", ("sim",))])

    def test_repetidos_listas_disjuntas_ok(self):
        self.load(nodo("imu", '["real", "mesa"]') + nodo("imu", '["sim", "otro"]'))

    def test_repetidos_uno_sin_profile_choca(self):
        self.assertConfigError(nodo("imu") + nodo("imu", '"sim"'), "imu")
        self.assertConfigError(nodo("imu", '"sim"') + nodo("imu"), "imu")

    def test_repetidos_listas_que_se_cruzan(self):
        msg = self.assertConfigError(nodo("imu", '["real", "mesa"]') + nodo("imu", '["sim", "mesa"]'), "imu")
        self.assertIn("mesa", msg)

    def test_repetidos_chocan_aunque_el_activo_no_sea_donde_chocan(self):
        text = nodo("imu", '"sim"') + nodo("imu", '["sim", "mesa"]') + nodo("c")
        for perfil in (None, "real", "replay"):
            with self.subTest(perfil=perfil):
                self.assertConfigError(text, "imu", "sim", profile=perfil)

    def test_repetidos_sin_perfiles_mensaje_de_siempre(self):
        self.assertConfigError(nodo("a") + nodo("a"), "hay dos nodos llamados 'a'")
        self.assertConfigError(nodo("a") + nodo("a"), "hay dos nodos llamados 'a'", profile="sim")

    def test_repetidos_con_perfiles_mensaje_dice_dos_nodos(self):
        self.assertConfigError(nodo("a", '"sim"') + nodo("a", '"sim"'), "hay dos nodos llamados 'a'")

    MUX = ('\n[[mux]]\nname = "{n}"\noutput = "127.0.0.1:7500"\n{p}'
           '[[mux.input]]\nname = "a"\nlisten = "127.0.0.1:7510"\npriority = 1\n')

    def mux(self, n, profile=None):
        return self.MUX.format(n=n, p="" if profile is None else f"profile = {profile}\n")

    def test_nodo_vs_mux_mismo_nombre(self):
        self.assertConfigError(nodo("ruedas") + self.mux("ruedas"), "ruedas")
        self.assertConfigError(nodo("ruedas", '"real"') + self.mux("ruedas", '["real", "sim"]'), "ruedas")
        self.assertConfigError(nodo("ruedas", '"real"') + self.mux("ruedas", '"real"'), "ruedas", profile="sim")

    def test_nodo_vs_mux_disjuntos_ok(self):
        text = nodo("ruedas", '"real"') + self.mux("ruedas", '"sim"')
        cfg = self.load(text, "sim")
        self.assertEqual([m.name for m in cfg.muxes], ["ruedas"])
        self.assertEqual(self.names(cfg), ["ruedas"])
        cfg = self.load(text)
        self.assertEqual(cfg.muxes, [])
        self.assertEqual(self.names(cfg), ["ruedas"])

    def test_mux_vs_mux(self):
        self.load(self.mux("m", '"real"') + self.mux("m", '"sim"') + nodo("a"))
        self.assertConfigError(nodo("a") + self.mux("m") + self.mux("m", '"sim"'), "m")

    # 7. [[topic]] por perfil
    def test_topic_por_perfil(self):
        text = (nodo("a") + '\n[[topic]]\nname = "/imu"\ntype = "ImuSample"\nrequired = true\n'
                '\n[[topic]]\nname = "/bateria"\nprofile = "real"\nrequired = true\n')
        self.assertEqual([t.name for t in self.load(text).topics], ["/imu", "/bateria"])
        self.assertEqual([t.name for t in self.load(text, "sim").topics], ["/imu"])
        self.assertEqual([t.name for t in self.load(text, "replay").topics], ["/imu"])

    def test_topic_por_perfil_no_llega_al_hub(self):
        from agmen.run import prepare_hub
        text = (nodo("a") + '\n[[topic]]\nname = "/imu"\ntype = "ImuSample"\nrequired = true\n'
                '\n[[topic]]\nname = "/bateria"\nprofile = "real"\nrequired = true\n')
        cfg, _ = prepare_hub(self.load(text, "sim"), ping=lambda addr: None)
        hub = next(n for n in cfg.nodes if n.name == "hub")
        self.assertIn("/imu=ImuSample", hub.cmd)
        self.assertNotIn("/bateria", " ".join(hub.cmd))
        cfg, _ = prepare_hub(self.load(text), ping=lambda addr: None)
        hub = next(n for n in cfg.nodes if n.name == "hub")
        self.assertIn("/bateria", " ".join(hub.cmd))

    def test_topic_repetido_en_perfiles_disjuntos(self):
        text = (nodo("a") + '\n[[topic]]\nname = "/imu"\ntype = "ImuSample"\nprofile = "real"\n'
                '\n[[topic]]\nname = "/imu"\ntype = "Otro"\nprofile = "sim"\n')
        self.assertEqual([t.type for t in self.load(text).topics], ["ImuSample"])
        self.assertEqual([t.type for t in self.load(text, "sim").topics], ["Otro"])
        self.assertEqual(self.load(text, "replay").topics, [])

    def test_topic_repetido_que_choca(self):
        base = nodo("a") + '\n[[topic]]\nname = "/imu"\n'
        self.assertConfigError(base + '\n[[topic]]\nname = "/imu"\n', "/imu", "dos veces")
        self.assertConfigError(base + 'profile = "sim"\n\n[[topic]]\nname = "/imu"\n', "/imu", "dos veces")
        for perfil in (None, "real", "replay"):   # choca aunque el activo no sea donde chocan
            with self.subTest(perfil=perfil):
                self.assertConfigError(base + 'profile = ["sim", "mesa"]\n\n[[topic]]\nname = "/imu"\n'
                                       'profile = ["mesa"]\n', "/imu", "mesa", profile=perfil)

    # 8. [[mux]] con profile
    def test_mux_con_profile(self):
        text = nodo("a") + self.mux("ruedas", '"real"')
        cfg = self.load(text)
        self.assertEqual([m.name for m in cfg.muxes], ["ruedas"])
        self.assertIn("ruedas", self.names(cfg))
        cfg = self.load(text, "sim")
        self.assertEqual(cfg.muxes, [])
        self.assertEqual(self.names(cfg), ["a"])
        self.assertEqual(cfg.skipped, [("ruedas", ("real",))])

    # 9. [profile.X]
    def test_profile_tabla_aplica_en_sim_y_no_en_real(self):
        text = (nodo("a") + '\n[web]\nhttp = "127.0.0.1:9000"\n\n[record]\ntopics = ["/imu"]\n'
                '\n[profile.sim]\nlocal = true\nweb = { open_browser = false }\n'
                'record = { topics = ["/camara"], dir = "simdatos" }\n'
                'clock = { mode = "sim", speed = 3.0 }\n')
        real = self.load(text)
        self.assertFalse(real.local)
        self.assertEqual(real.web.http, "127.0.0.1:9000")
        self.assertTrue(real.web.open_browser)
        self.assertEqual(real.record.topics, ["/imu"])
        self.assertEqual(real.clock.mode, "real")
        sim = self.load(text, "sim")
        self.assertTrue(sim.local)
        self.assertFalse(sim.web.open_browser)
        self.assertEqual(sim.web.http, "127.0.0.1:8080")   # tabla entera: no mezcla
        self.assertEqual((sim.record.topics, sim.record.dir), (["/camara"], "simdatos"))
        self.assertEqual((sim.clock.mode, sim.clock.speed), ("sim", 3.0))

    def test_profile_tabla_record_reemplaza_aunque_arriba_no_haya(self):
        cfg = self.load(nodo("a") + '\n[profile.sim]\nrecord = { auto = false }\n', "sim")
        self.assertFalse(cfg.record.auto)
        self.assertIsNone(self.load(nodo("a") + '\n[profile.sim]\nrecord = { auto = false }\n').record)

    def test_profile_tabla_record_reemplaza_entera(self):
        text = nodo("a") + '\n[record]\ncompress = "zstd"\nsplit = "1GiB"\n\n[profile.sim]\nrecord = { dir = "x" }\n'
        sim = self.load(text, "sim")
        self.assertEqual((sim.record.compress, sim.record.split, sim.record.dir), (None, None, "x"))

    def test_profile_tabla_web_reemplaza_teleop_y_token(self):
        text = (nodo("a") + '\n[web]\ntoken = "unaclavelarga"\n[web.teleop]\ntarget = "127.0.0.1:7500"\n'
                '\n[profile.sim]\nweb = { open_browser = false }\n')
        self.assertIsNone(self.load(text, "sim").web.teleop)
        self.assertIsNone(self.load(text, "sim").web.token)
        self.assertEqual(self.load(text).web.token, "unaclavelarga")

    def test_profile_tabla_local_en_perfil_inventado(self):
        text = nodo("a") + '\n[profile.mesa]\nlocal = true\n'
        self.assertTrue(self.load(text, "mesa").local)
        self.assertFalse(self.load(text, "real").local)

    # 10. errores en [profile.X]
    def test_profile_tabla_clave_mal_escrita_aunque_corra_real(self):
        for perfil in (None, "sim", "replay"):
            with self.subTest(perfil=perfil):
                self.assertConfigError(nodo("a") + '\n[profile.sim]\nclok = { mode = "sim" }\n',
                                       "¿Quisiste decir 'clock'?", profile=perfil)

    def test_profile_tabla_local_invalido(self):
        for valor in ('"si"', "1", "[]"):
            with self.subTest(valor=valor):
                self.assertConfigError(nodo("a") + f'\n[profile.sim]\nlocal = {valor}\n', "local")

    def test_profile_tabla_no_es_tabla(self):
        with self.assertRaises(ConfigError):
            self.load(nodo("a") + 'profile = 3\n')   # es del nodo: valida
        with self.assertRaises(ConfigError):
            self.load('profile = "sim"\n' + nodo("a"))   # arriba del archivo no existe

    def test_profile_tabla_nombre_invalido(self):
        self.assertConfigError(nodo("a") + '\n[profile."con espacio"]\nlocal = true\n', "con espacio")

    def test_profile_tabla_contenido_roto_aunque_no_sea_el_activo(self):
        self.assertConfigError(nodo("a") + '\n[profile.sim]\nweb = { http_ = 1 }\n', "web")
        self.assertConfigError(nodo("a") + '\n[profile.sim]\nrecord = { compress = "gzip" }\n', "zstd")
        self.assertConfigError(nodo("a") + '\n[profile.sim]\nclock = { mode = "rapido" }\n', "no es válido")

    # 11. reloj
    def test_reloj_sim_implicito_en_perfil_sim(self):
        cfg = self.load(ROBOT_RFC, "sim")
        self.assertEqual(cfg.clock.mode, "sim")
        self.assertEqual((cfg.clock.speed, cfg.clock.source), (1.0, "agmen"))
        self.assertTrue(cfg.nodes)
        for n in cfg.nodes:
            self.assertEqual(n.env["AGMEN_CLOCK"], "sim")
            self.assertEqual(n.env["AGMEN_CLOCK_SOURCE"], "agmen")

    def test_reloj_real_en_real_y_replay(self):
        for perfil in ("real", "replay"):
            with self.subTest(perfil=perfil):
                cfg = self.load(ROBOT_RFC, perfil)
                self.assertEqual(cfg.clock.mode, "real")
                for n in cfg.nodes:
                    self.assertEqual(n.env["AGMEN_CLOCK"], "real")

    def test_reloj_sim_de_arriba_se_respeta_en_sim(self):
        cfg = self.load(ROBOT_RFC + '\n[clock]\nmode = "sim"\nspeed = 2.5\npaused = true\n', "sim")
        self.assertEqual((cfg.clock.mode, cfg.clock.speed, cfg.clock.paused), ("sim", 2.5, True))
        for n in cfg.nodes:
            self.assertEqual(n.env["AGMEN_CLOCK"], "sim")

    def test_reloj_real_de_arriba_con_sim_se_vuelve_sim_sin_speed(self):
        cfg = self.load(ROBOT_RFC + '\n[clock]\nmode = "real"\n', "sim")
        self.assertEqual(cfg.clock.mode, "sim")

    def test_reloj_profile_sim_clock_gana(self):
        text = ROBOT_RFC + '\n[clock]\nmode = "sim"\nspeed = 2.0\n\n[profile.sim]\nclock = { mode = "sim", speed = 4.0 }\n'
        cfg = self.load(text, "sim")
        self.assertEqual(cfg.clock.speed, 4.0)   # tabla entera, no mezcla
        self.assertEqual(self.load(text, "sim").nodes[0].env["AGMEN_CLOCK"], "sim")

    def test_reloj_profile_sim_clock_real_explicito(self):
        # quien escribe [profile.sim] clock = {mode = "real"} lo pide a propósito
        cfg = self.load(ROBOT_RFC + '\n[profile.sim]\nclock = { mode = "real" }\n', "sim")
        self.assertEqual(cfg.clock.mode, "real")

    def test_reloj_en_perfil_inventado_no_es_sim(self):
        cfg = self.load(nodo("a", '"mesa"'), "mesa")
        self.assertEqual(cfg.clock.mode, "real")

    def test_reloj_source_nodo_que_corre(self):
        text = nodo("gazebo", '"sim"') + nodo("c") + '\n[clock]\nmode = "sim"\nsource = "gazebo"\n'
        cfg = self.load(text, "sim")
        self.assertEqual(cfg.clock.source, "gazebo")
        self.assertEqual(cfg.nodes[0].env["AGMEN_CLOCK_SOURCE"], "gazebo")

    def test_reloj_source_de_otro_perfil_lo_dice(self):
        text = nodo("gazebo", '"sim"') + nodo("c") + '\n[clock]\nmode = "sim"\nsource = "gazebo"\n'
        # en real, con el [clock] de arriba en sim, el nodo gazebo no corre
        msg = self.assertConfigError(text, "gazebo", "no corre", profile="real")
        self.assertIn("real", msg)
        self.assertNotIn("no hay un [[node]] con ese nombre", msg)

    def test_reloj_source_de_otro_perfil_por_defecto(self):
        text = nodo("gazebo", '"sim"') + nodo("c") + '\n[clock]\nmode = "sim"\nsource = "gazebo"\n'
        self.assertConfigError(text, "gazebo", "no corre")   # perfil por defecto: real

    def test_reloj_source_inexistente_sigue_sugiriendo(self):
        text = nodo("gazebo", '"sim"') + '\n[clock]\nmode = "sim"\nsource = "gazbo"\n'
        self.assertConfigError(text, "¿Quisiste decir 'gazebo'?", profile="sim")

    def test_reloj_source_en_profile_sim(self):
        text = nodo("gazebo", '"sim"') + nodo("c") + '\n[profile.sim]\nclock = { mode = "sim", source = "gazebo" }\n'
        cfg = self.load(text, "sim")
        self.assertEqual(cfg.clock.source, "gazebo")
        cfg = self.load(text, "real")   # en real no se usa y no estorba
        self.assertEqual(cfg.clock.mode, "real")

    def test_reloj_profile_sim_source_de_nodo_real_lo_dice(self):
        text = nodo("hw", '"real"') + nodo("w", '"sim"') + '\n[profile.sim]\nclock = { mode = "sim", source = "hw" }\n'
        self.assertConfigError(text, "hw", "no corre", profile="sim")

    # 12. compatibilidad
    def test_sin_perfiles_igual_que_antes(self):
        text = MINIMO + '\n[[node]]\nname = "b"\ncmd = "y"\n\n[[topic]]\nname = "/imu"\n'
        cfg = self.load(text)
        self.assertEqual((cfg.profile, cfg.skipped, cfg.local), ("real", [], False))
        self.assertEqual(cfg.profiles, ("real", "sim", "replay"))
        self.assertEqual(self.names(cfg), ["motor", "b"])
        self.assertEqual([t.name for t in cfg.topics], ["/imu"])
        self.assertEqual(cfg.clock.mode, "real")

    def test_sin_perfiles_pedir_sim_corre_todo_con_reloj_sim(self):
        cfg = self.load(MINIMO, "sim")
        self.assertEqual(self.names(cfg), ["motor"])
        self.assertEqual(cfg.skipped, [])
        self.assertEqual(cfg.clock.mode, "sim")

    def test_load_config_argumento_posicional_y_nombrado(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "robot.toml"
            p.write_text(ROBOT_RFC, encoding="utf-8")
            self.assertEqual(load_config(p, "sim").profile, "sim")
            self.assertEqual(load_config(str(p), profile="sim").profile, "sim")
            self.assertEqual(load_config(p).profile, "real")

    def test_llamar_dos_veces_no_se_contamina(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "robot.toml"
            p.write_text(ROBOT_RFC, encoding="utf-8")
            sim = load_config(p, "sim")
            real = load_config(p, "real")
            self.assertEqual(real.clock.mode, "real")
            for n in real.nodes:
                self.assertEqual(n.env["AGMEN_CLOCK"], "real")
            self.assertEqual(sim.clock.mode, "sim")

    # 13. los ejemplos del repo
    def test_ejemplos_del_repo_cargan(self):
        raiz = Path(__file__).resolve().parent.parent
        vistos = 0
        for rel in ("examples/tutorial/robot.toml", "examples/nodos/robot.toml", "examples/vision/robot.toml"):
            path = raiz / rel
            if not path.exists():
                continue
            with self.subTest(rel=rel):
                cfg = load_config(path)
                vistos += 1
                self.assertEqual((cfg.profile, cfg.skipped, cfg.local), ("real", [], False))
        self.assertGreater(vistos, 0)


class TerminalBloqueada(io.StringIO):
    """Una terminal que no deja escribir hasta que se le diga."""

    def __init__(self):
        super().__init__()
        self.abierta = threading.Event()

    def write(self, text):
        self.abierta.wait(5)
        return super().write(text)


class TestOtroRobot(unittest.TestCase):
    """Dos agmen run en la misma máquina (0.12.1, PENDIENTES de Arca9 #7): el segundo
    dice de quién es el hub que reusa y qué topics usan los dos."""

    TOML = (MINIMO + '\n[[topic]]\nname = "/arca9/estado"\n\n[[topic]]\nname = "/arca9/q"\n'
            '\n[[mux]]\nname = "mando"\noutput = "/arca9/comando"\n'
            '[[mux.input]]\nname = "panel"\ntopic = "/arca9/comando/panel"\npriority = 10\n'
            '\n[web.teleop]\ntarget = "/arca9/teleop"\n')

    def config(self):
        return TestConfig.load(self, self.TOML)

    @staticmethod
    def hub(robot, topics):
        lines = [("state", {"version": "0.12.1", "hub": "127.0.0.1:7390", **({"robot": robot} if robot else {})})]
        lines += [("topic", {"name": t, "pubs": pubs, "subs": "0", "expected": exp}) for t, pubs, exp in topics]
        return lambda hub, timeout: lines

    def test_topics_que_usa(self):
        self.assertEqual(self.config().topics_used(),
                         {"/arca9/estado", "/arca9/q", "/arca9/comando", "/arca9/comando/panel", "/arca9/teleop",
                          "/mux/mando"})     # el estado del mux (RFC 0012): dos mux 'mando' también chocan

    def test_mismos_topics(self):
        from agmen.run import other_robot

        avisos = other_robot(self.config(), query=self.hub("Arca9 (simulado)", [
            ("/arca9/comando", "1", "no"), ("/arca9/estado", "0", "yes"), ("/otro", "1", "no"),
            ("/arca9/q", "0", "no")]))     # declarado allá pero sin nadie: no cuenta como compartido
        self.assertEqual(len(avisos), 1)
        self.assertIn("ya hay otro robot en esta máquina, 'Arca9 (simulado)'", avisos[0])
        self.assertIn("/arca9/comando, /arca9/estado: sus mensajes se van a mezclar", avisos[0])
        self.assertNotIn("/otro", avisos[0])

    def test_hub_sin_nombre_y_sin_topics_compartidos(self):
        from agmen.run import other_robot

        self.assertIn("¿otro robot?", other_robot(self.config(), query=self.hub("", [("/arca9/q", "2", "no")]))[0])
        self.assertEqual(other_robot(self.config(), query=self.hub("", [("/imu", "1", "no")])), [])
        self.assertIn("no usan los mismos topics",
                      other_robot(self.config(), query=self.hub("Otro", [("/imu", "1", "no")]))[0])

    def test_sin_hub_no_dice_nada(self):
        from agmen.run import other_robot

        def no_hay(hub, timeout):
            raise ConnectionError("no hay hub")

        self.assertEqual(other_robot(self.config(), query=no_hay), [])


class TestPrinter(unittest.TestCase):
    def test_una_terminal_atorada_no_frena_a_quien_escribe(self):
        term = TerminalBloqueada()
        p = Printer(["a"], stream=term, max_queue=10)
        t0 = time.perf_counter()
        for i in range(1000):
            p("a", f"línea {i}")
        self.assertLess(time.perf_counter() - t0, 0.5)   # nunca esperó a la terminal
        term.abierta.set()
        p.close()
        salida = term.getvalue()
        self.assertIn("líneas no cupieron en la terminal", salida)

    def test_close_escribe_lo_pendiente(self):
        term = io.StringIO()
        p = Printer(["motor"], stream=term)
        p("motor", "hola")
        p("agmen", "listo. ¡Hasta luego!", "agmen")
        p.close()
        self.assertIn("motor │ hola", term.getvalue())
        self.assertTrue(term.getvalue().rstrip().endswith("listo. ¡Hasta luego!"))

    def test_los_avisos_de_agmen_nunca_se_descartan(self):
        term = TerminalBloqueada()
        p = Printer(["a"], stream=term, max_queue=5)
        for i in range(50):
            p("a", f"línea {i}")
        p("agmen", "aviso importante", "agmen")
        term.abierta.set()
        p.close()
        self.assertIn("aviso importante", term.getvalue())


if __name__ == "__main__":
    unittest.main()

"""Pruebas del lector de robot.toml (agmen run). Sin procesos ni red.

    python -m unittest discover -s python
"""

import io
import json
import os
import re
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
                # El robot del tutorial tiene un nodo `mundo` (perfil sim): en `real` no corre.
                esperado = [("mundo", ("sim",))] if rel == "examples/tutorial/robot.toml" else []
                self.assertEqual((cfg.profile, cfg.skipped, cfg.local), ("real", esperado, False))
        self.assertGreater(vistos, 0)

    # 14. el robot del tutorial por perfil (RFC 0011 §3.3, reto 1)
    def test_tutorial_por_perfil(self):
        path = Path(__file__).resolve().parent.parent / "examples" / "tutorial" / "robot.toml"
        real, sim = load_config(path, "real"), load_config(path, "sim")

        # real: el hardware de verdad; el mundo no corre.
        self.assertTrue({"imu", "motor", "cerebro"} <= set(self.names(real)))
        self.assertNotIn("mundo", self.names(real))
        self.assertEqual(real.clock.mode, "real")

        # sim: corren ruedas (el mux), mundo y cerebro; imu y motor NUNCA (no se mueve el robot de verdad).
        self.assertEqual(sim.profile, "sim")
        self.assertTrue({"mundo", "cerebro"} <= set(self.names(sim)))
        self.assertNotIn("imu", self.names(sim))
        self.assertNotIn("motor", self.names(sim))
        self.assertEqual(sorted(n for n, _ in sim.skipped), ["imu", "motor"])
        self.assertTrue(all(p == ("real",) for _, p in sim.skipped))
        self.assertEqual([m.name for m in real.muxes], ["ruedas"])
        self.assertEqual([m.name for m in sim.muxes], ["ruedas"])
        self.assertEqual(sim.clock.mode, "sim")

        # el cerebro empieza apagado en los dos perfiles
        for cfg in (real, sim):
            self.assertFalse(next(n for n in cfg.nodes if n.name == "cerebro").autostart)

        # topics: /camara solo se espera en sim; /imu es obligatorio en los dos
        for cfg in (real, sim):
            imu = next(t for t in cfg.topics if t.name == "/imu")
            self.assertTrue(imu.required)
        self.assertNotIn("/camara", [t.name for t in real.topics])
        camara = next(t for t in sim.topics if t.name == "/camara")
        self.assertEqual(camara.type, "Imagen")
        self.assertFalse(camara.required)


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


class TestResolverPrograma(unittest.TestCase):
    """Node._resolve_program (c49bd35): la ruta de 'cmd' se usa tal cual, sin seguir enlaces simbólicos
    (el 'python' de un entorno virtual es un enlace al del sistema: si se sigue, se pierde el venv)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def nodo(self, cmd: str, base: Path | None = None):
        from agmen.run import Node

        base = base or self.dir
        (base / "robot.toml").write_text(f'[robot]\nname = "r"\n\n[[node]]\nname = "n"\ncmd = "{cmd}"\n',
                                         encoding="utf-8")
        cfg = load_config(base / "robot.toml")
        return Node(cfg.nodes[0], cfg.base_dir, None, None), base

    def enlace(self, destino: Path, objetivo: Path) -> None:
        try:
            destino.symlink_to(objetivo)
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"aquí no se pueden crear enlaces simbólicos ({e})")

    def test_ruta_relativa_con_enlace_no_se_resuelve(self):
        (self.dir / "real").mkdir()
        (self.dir / "real" / "programa").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.dir / "bin").mkdir()
        self.enlace(self.dir / "bin" / "enlace", Path("..") / "real" / "programa")
        nodo, base = self.nodo("bin/enlace --x 1")
        args = nodo._resolve_program(base)
        self.assertEqual(args[1:], ["--x", "1"])
        self.assertEqual(args[0], str(Path(os.path.abspath(base)) / "bin" / "enlace"))
        self.assertTrue(args[0].endswith("enlace"), "no debe acabar en el destino del enlace")
        self.assertTrue(os.path.isabs(args[0]))

    def test_ruta_con_punto_punto_y_enlace(self):
        (self.dir / "venv" / "bin").mkdir(parents=True)
        (self.dir / "robot").mkdir()
        (self.dir / "real").mkdir()
        (self.dir / "real" / "py").write_text("", encoding="utf-8")
        self.enlace(self.dir / "venv" / "bin" / "python", self.dir / "real" / "py")
        nodo, base = self.nodo("../venv/bin/python nodo.py", self.dir / "robot")
        args = nodo._resolve_program(base)
        self.assertEqual(os.path.basename(args[0]), "python")
        self.assertNotIn("real", Path(args[0]).parts)
        self.assertEqual(os.path.realpath(args[0]), os.path.realpath(self.dir / "real" / "py"))   # sí apunta ahí
        self.assertEqual(args[1], "nodo.py")

    def test_ruta_absoluta_se_respeta(self):
        absoluta = str(self.dir / "bin" / "algo")
        nodo, base = self.nodo(absoluta.replace("\\", "/") + " a b")
        self.assertEqual(nodo._resolve_program(base), [absoluta.replace("\\", "/"), "a", "b"])

    def test_python_a_secas_es_el_interprete_actual(self):
        for nombre in ("python", "python3", "py"):
            with self.subTest(nombre=nombre):
                nodo, base = self.nodo(f"{nombre} cerebro.py --n 2")
                self.assertEqual(nodo._resolve_program(base), [sys.executable, "cerebro.py", "--n", "2"])

    def test_nombre_sin_ruta_no_se_toca(self):
        nodo, base = self.nodo("echo hola")
        self.assertEqual(nodo._resolve_program(base), ["echo", "hola"])

    def test_de_punta_a_punta_con_un_venv_de_verdad(self):
        import subprocess
        import venv

        (self.dir / "robot").mkdir()
        try:
            venv.EnvBuilder(with_pip=False, symlinks=True).create(self.dir / "venv")
        except Exception as e:
            self.skipTest(f"no se pudo crear un entorno virtual ({e})")
        exe = self.dir / "venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
        if not exe.exists():
            self.skipTest("el entorno virtual no trae python")
        rel = "../venv/" + ("Scripts/python" if os.name == "nt" else "bin/python")
        nodo, base = self.nodo(f"{rel} -c 'import sys; print(sys.prefix)'", self.dir / "robot")
        args = nodo._resolve_program(base)
        r = subprocess.run([*args[:1], "-c", "import sys; print(sys.prefix)"], capture_output=True, text=True,
                           timeout=60, cwd=base)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.path.realpath(r.stdout.strip()), os.path.realpath(self.dir / "venv"),
                         "el nodo debe correr dentro del entorno virtual, no en el Python del sistema")


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

    def detenido(self, max_queue):
        """Un Printer con el escritor atorado en la terminal (escribiendo un 'tapón'): lo que llegue después se
        queda en la cola hasta abrirla."""
        term = TerminalBloqueada()
        p = Printer(["a"], stream=term, max_queue=max_queue)
        p("a", "tapón")
        fin = time.monotonic() + 5
        while not (p._writing and not p._live) and time.monotonic() < fin:
            time.sleep(0.001)
        self.assertTrue(p._writing and not p._live, "el escritor no tomó el tapón")
        return term, p

    def salida(self, term, p):
        term.abierta.set()
        p.close()
        return [l.split(" │ ", 1)[1] for l in term.getvalue().splitlines()]

    def test_con_la_cola_llena_se_conservan_las_mas_nuevas(self):
        """Cierre del nivel 10 (decisión 2): una ráfaga mayor que la cola sale con sus ÚLTIMAS líneas; el aviso
        dice cuántas faltan y va antes de ellas; las no descartables están todas y en orden."""
        term, p = self.detenido(max_queue=10)
        enviadas = []
        for i in range(100):
            if i % 25 == 0:
                p("agmen", f"aviso {i}", "agmen")
                enviadas.append(f"aviso {i}")
            p("a", f"línea {i}")
            enviadas.append(f"línea {i}")
        self.assertLessEqual(len(p._q), 2 * p._live + 1, "la cola no crece con las descartadas")
        out = self.salida(term, p)
        self.assertEqual(out[0], "tapón")
        m = re.fullmatch(r"… (\d+) líneas no cupieron en la terminal \(están en el panel web\)", out[1])
        self.assertIsNotNone(m, out[1])
        resto = out[2:]
        self.assertEqual([l for l in resto if l.startswith("aviso")], [f"aviso {i}" for i in (0, 25, 50, 75)])
        lineas = [int(l.split()[1]) for l in resto if l.startswith("línea")]
        self.assertEqual(lineas, list(range(100 - len(lineas), 100)), "salen las últimas, seguidas")
        self.assertEqual(int(m.group(1)) + len(lineas), 100)
        # La cola llena son 10; los 3 avisos que llegaron con ella llena entran igual (nunca se descartan).
        self.assertEqual((len(resto), len(lineas)), (13, 9))
        self.assertEqual(resto, [l for l in enviadas if l in resto], "el orden no cambia")

    def test_cola_llena_solo_de_no_descartables_descarta_la_que_llega(self):
        term, p = self.detenido(max_queue=3)
        for i in range(5):
            p("agmen", f"aviso {i}", "agmen")
        p("a", "nueva")
        p("a", "otra", "error", droppable=False)
        out = self.salida(term, p)
        self.assertEqual(out, ["tapón", "… 1 líneas no cupieron en la terminal (están en el panel web)",
                               *(f"aviso {i}" for i in range(5)), "otra"])

    def test_rafaga_larga_sin_crecer(self):
        """O(1) amortizado y memoria acotada: 20 000 líneas con la terminal atorada."""
        term, p = self.detenido(max_queue=50)
        t0 = time.perf_counter()
        for i in range(20_000):
            p("a", f"línea {i}")
            self.assertLessEqual(len(p._q), 2 * 50 + 1)
        self.assertLess(time.perf_counter() - t0, 5.0)
        out = self.salida(term, p)
        self.assertEqual(out[2:], [f"línea {i}" for i in range(19_950, 20_000)])


def maquina(name, host, extra=""):
    return f'\n[[machine]]\nname = "{name}"\nhost = "{host}"\n{extra}'


MAQUINAS = maquina("robot", "192.168.1.20") + maquina("laptop", "laptop.local:7400")


class TestMaquinas(unittest.TestCase):
    """[[machine]] y machine en [[node]]/[[mux]] (RFC 0011 §3.4, parte 3a)."""

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

    # --- lo que carga bien
    def test_sin_machine_se_carga_como_antes(self):
        cfg = self.load(MINIMO)
        self.assertEqual(cfg.machines, [])
        self.assertEqual(cfg.nodes[0].machine, "")
        self.assertFalse(cfg.local)

    def test_campos_y_puerto_por_defecto(self):
        cfg = self.load(MAQUINAS + nodo("motor", extra='machine = "robot"\n') + nodo("ia"))
        self.assertEqual([(m.name, m.host, m.port) for m in cfg.machines],
                         [("robot", "192.168.1.20", 7391), ("laptop", "laptop.local", 7400)])
        by = {n.name: n for n in cfg.nodes}
        self.assertEqual(by["motor"].machine, "robot")
        self.assertEqual(by["ia"].machine, "")

    def test_machine_declarada_sin_nodos_es_valida(self):
        cfg = self.load(MAQUINAS + nodo("a"))
        self.assertEqual(len(cfg.machines), 2)

    def test_host_con_nombre_no_se_resuelve_al_cargar(self):
        t0 = time.monotonic()
        cfg = self.load(maquina("x", "no-existe.invalid") + nodo("a", extra='machine = "x"\n'))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(cfg.machines[0].host, "no-existe.invalid")

    def test_host_ipv6_con_puerto(self):
        cfg = self.load(maquina("x", "[fe80::1]:7400") + maquina("y", "fe80::2") + nodo("n"))
        self.assertEqual([(m.host, m.port) for m in cfg.machines], [("fe80::1", 7400), ("fe80::2", 7391)])

    def test_mux_con_machine_y_su_nodeconfig_la_hereda(self):
        mux = ('\n[[mux]]\nname = "ruedas"\noutput = "127.0.0.1:7500"\nmachine = "robot"\n'
               '[[mux.input]]\nname = "a"\nlisten = "127.0.0.1:7501"\npriority = 1\n')
        cfg = self.load(MAQUINAS + mux + nodo("ia"))
        self.assertEqual(cfg.muxes[0].machine, "robot")
        nodo_mux = next(n for n in cfg.nodes if n.name == "ruedas")
        self.assertEqual(nodo_mux.machine, "robot")
        self.assertEqual(next(n for n in cfg.nodes if n.name == "ia").machine, "")

    def test_mux_sin_machine_es_vacio(self):
        mux = ('\n[[mux]]\nname = "ruedas"\noutput = "127.0.0.1:7500"\n'
               '[[mux.input]]\nname = "a"\nlisten = "127.0.0.1:7501"\npriority = 1\n')
        cfg = self.load(MAQUINAS + mux + nodo("ia"))
        self.assertEqual(cfg.muxes[0].machine, "")
        self.assertEqual(next(n for n in cfg.nodes if n.name == "ruedas").machine, "")

    def test_mux_con_machine_no_declarada_es_error(self):
        mux = ('\n[[mux]]\nname = "ruedas"\noutput = "127.0.0.1:7500"\nmachine = "robto"\n'
               '[[mux.input]]\nname = "a"\nlisten = "127.0.0.1:7501"\npriority = 1\n')
        self.assertConfigError(MAQUINAS + mux + nodo("ia"), "[[mux]] 'ruedas'", "robto", "¿Quisiste decir 'robot'?")

    def test_raw_es_la_tabla_sin_resolver(self):
        text = (MAQUINAS + '\n[[node]]\nname = "ia"\ncmd = "python cerebro.py"\ncwd = "sub"\n'
                'machine = "laptop"\nenv = { A = "1" }\n')
        n = self.load(text).nodes[0]
        self.assertEqual(n.raw, {"name": "ia", "cmd": "python cerebro.py", "cwd": "sub",
                                 "machine": "laptop", "env": {"A": "1"}})
        self.assertEqual(n.cmd[0], sys.executable)        # lo resuelto sí cambió
        self.assertEqual(n.raw["cmd"], "python cerebro.py")   # y raw no

    def test_raw_es_una_copia_independiente(self):
        n = self.load('[[node]]\nname = "a"\ncmd = "x"\nenv = { A = "1" }\n').nodes[0]
        n.raw["env"]["A"] = "2"
        n.raw["name"] = "otro"
        self.assertEqual(n.env["A"], "1")
        self.assertEqual(n.name, "a")
        otro = self.load('[[node]]\nname = "a"\ncmd = "x"\nenv = { A = "1" }\n').nodes[0]
        self.assertEqual(otro.raw["env"], {"A": "1"})

    def test_local_conserva_el_campo_machine(self):
        text = (MAQUINAS + nodo("motor", extra='machine = "robot"\n')
                + '\n[profile.sim]\nlocal = true\n')
        cfg = self.load(text, "sim")
        self.assertTrue(cfg.local)
        self.assertEqual(cfg.nodes[0].machine, "robot")
        self.assertFalse(self.load(text, "real").local)

    def test_los_nombres_de_nodo_siguen_unicos_entre_computadoras(self):
        text = (MAQUINAS + nodo("motor", extra='machine = "robot"\n')
                + nodo("motor", extra='machine = "laptop"\n'))
        self.assertConfigError(text, "motor")

    # --- errores en machine de un nodo
    def test_machine_mal_escrita_sugiere(self):
        e = self.assertConfigError(MAQUINAS + nodo("a", extra='machine = "robto"\n'),
                                   "[[node]] 'a'", "robto", "¿Quisiste decir 'robot'?")
        self.assertNotIn("Traceback", e)

    def test_machine_sin_parecido_lista_las_declaradas(self):
        self.assertConfigError(MAQUINAS + nodo("a", extra='machine = "zzz"\n'),
                               "'zzz'", "robot, laptop")

    def test_machine_sin_ninguna_machine_declarada(self):
        self.assertConfigError(nodo("a", extra='machine = "robot"\n'),
                               "no declara ninguna [[machine]]", "[[machine]]")

    def test_machine_vacia_o_no_texto(self):
        self.assertConfigError(MAQUINAS + nodo("a", extra='machine = ""\n'), "machine")
        self.assertConfigError(MAQUINAS + nodo("a", extra='machine = 3\n'), "machine")

    def test_machine_mal_escrita_en_un_nodo_de_otro_perfil_tambien_falla(self):
        text = MAQUINAS + nodo("a") + nodo("mundo", '"sim"', 'machine = "robto"\n')
        for perfil in (None, "real", "sim"):
            self.assertConfigError(text, "robto", "¿Quisiste decir 'robot'?", profile=perfil)

    # --- errores en [[machine]]
    def test_name_repetido(self):
        self.assertConfigError(maquina("robot", "1.2.3.4") + maquina("robot", "1.2.3.5"), "'robot'", "únicos")

    def test_host_vacio(self):
        self.assertConfigError(maquina("robot", ""), "'robot'", "vacío")
        self.assertConfigError(maquina("robot", "   "), "'robot'")

    def test_host_con_esquema_o_ruta(self):
        self.assertConfigError(maquina("robot", "http://192.168.1.20"), "http://")
        self.assertConfigError(maquina("robot", "192.168.1.20/x"), "robot")

    def test_host_con_espacios(self):
        self.assertConfigError(maquina("robot", "192.168.1.20 :7391"), "espacios")
        self.assertConfigError(maquina("robot", "mi robot"), "espacios")

    def test_puerto_invalido(self):
        for host in ("1.2.3.4:0", "1.2.3.4:65536", "1.2.3.4:abc", "1.2.3.4:", "1.2.3.4:-1", "1.2.3.4:80.5"):
            with self.subTest(host=host):
                self.assertConfigError(maquina("robot", host), "puerto", "1 y 65535")

    def test_puertos_en_los_limites(self):
        cfg = self.load(maquina("a", "1.2.3.4:1") + maquina("b", "1.2.3.4:65535") + nodo("n"))
        self.assertEqual([m.port for m in cfg.machines], [1, 65535])

    def test_clave_port_aparte(self):
        self.assertConfigError(maquina("robot", "1.2.3.4", "port = 7391\n"), "host", "7391")

    def test_clave_mal_escrita(self):
        self.assertConfigError('\n[[machine]]\nname = "robot"\nhots = "1.2.3.4"\n', "¿Quisiste decir 'host'?")
        self.assertConfigError(maquina("robot", "1.2.3.4", 'nombre = "x"\n'), "desconocida")

    def test_name_invalido(self):
        for nombre in ("ro bot", "róbot", "robot!", "a/b", ""):
            with self.subTest(nombre=nombre):
                self.assertConfigError(maquina(nombre, "1.2.3.4"), "nombre de computadora válido")

    def test_name_valido_con_guiones(self):
        cfg = self.load(maquina("mi_robot-2", "1.2.3.4") + nodo("n"))
        self.assertEqual(cfg.machines[0].name, "mi_robot-2")

    def test_falta_name_o_host(self):
        self.assertConfigError('\n[[machine]]\nhost = "1.2.3.4"\n', "falta 'name'")
        self.assertConfigError('\n[[machine]]\nname = "robot"\n', "falta 'host'")

    def test_host_no_es_texto(self):
        self.assertConfigError('\n[[machine]]\nname = "robot"\nhost = 5\n', "host es un texto")

    def test_machine_no_es_lista_de_tablas(self):
        self.assertConfigError('machine = "robot"\n' + MINIMO, "[[machine]]")
        self.assertConfigError('[machine]\nname = "robot"\nhost = "1.2.3.4"\n' + MINIMO, "[[machine]]")

    def test_machine_dentro_de_robot_o_profile_es_clave_desconocida(self):
        self.assertConfigError('[robot]\nname = "r"\nmachine = "x"\n', "machine")


# [[joints]] (RFC 0013 §3.4, D6): los nombres y el orden de las articulaciones, una vez.
PATAS = ["FR_hip", "FR_thigh", "FR_calf", "FL_hip", "FL_thigh", "FL_calf",
         "RR_hip", "RR_thigh", "RR_calf", "RL_hip", "RL_thigh", "RL_calf"]


def toml_valor(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)   # las secuencias de escape de JSON también valen en TOML
    if isinstance(v, list):
        return "[" + ", ".join(toml_valor(x) for x in v) + "]"
    return str(v)


def joints(name="patas", **campos):
    """Un [[joints]] en TOML. Por defecto, el del RFC; un campo en None no se escribe."""
    valores = {"name": name, "state": f"/{name}/estado", "command": f"/{name}/cmd", "kp": 20.0, "kd": 0.5,
               "names": PATAS}
    valores.update(campos)
    lineas = "".join(f"{k} = {toml_valor(v)}\n" for k, v in valores.items() if v is not None)
    return "\n[[joints]]\n" + lineas


class TestJoints(unittest.TestCase):
    """[[joints]] en robot.toml: se lee, se revisa antes de arrancar y queda en RobotConfig.joints."""

    def load(self, text: str, profile=None):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "robot.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path, profile)

    def assertConfigError(self, text: str, *must_contain: str, profile=None) -> str:
        with self.assertRaises(ConfigError) as ctx:
            self.load(text, profile)
        for frag in must_contain:
            self.assertIn(frag, str(ctx.exception))
        return str(ctx.exception)

    # --- lo que sí vale ---
    def test_el_del_rfc(self):
        cfg = self.load(MINIMO + joints())
        self.assertEqual(len(cfg.joints), 1)
        j = cfg.joints[0]
        self.assertEqual((j.name, j.state, j.command), ("patas", "/patas/estado", "/patas/cmd"))
        self.assertEqual(j.names, tuple(PATAS))
        self.assertEqual(j.kp, (20.0,) * 12)        # un número se expande a uno por articulación
        self.assertEqual(j.kd, (0.5,) * 12)
        self.assertFalse(j.require_names)
        self.assertEqual(j.profiles, ())
        self.assertTrue({"/patas/estado", "/patas/cmd"} <= cfg.topics_used())

    def test_lista_de_ganancias_se_respeta(self):
        kp = [float(i) for i in range(12)]
        j = self.load(MINIMO + joints(kp=kp, kd=[0] * 12)).joints[0]
        self.assertEqual(j.kp, tuple(kp))
        self.assertEqual(j.kd, (0.0,) * 12)
        self.assertTrue(all(isinstance(v, float) for v in j.kd))

    def test_sin_ganancias(self):
        j = self.load(MINIMO + joints(kp=None, kd=None)).joints[0]
        self.assertEqual((j.kp, j.kd), (None, None))

    def test_require_names_y_profile(self):
        cfg = self.load(MINIMO + joints(require_names=True, profile="sim"), "sim")
        j = cfg.joints[0]
        self.assertTrue(j.require_names)
        self.assertEqual(j.profiles, ("sim",))
        self.assertEqual(self.load(MINIMO + joints(profile="sim")).joints, [])   # en real no se usa

    def test_solo_estado_o_solo_comando(self):
        j = self.load(MINIMO + joints(command=None, kp=None, kd=None)).joints[0]
        self.assertEqual((j.state, j.command), ("/patas/estado", ""))
        j = self.load(MINIMO + joints(state=None)).joints[0]
        self.assertEqual((j.state, j.command), ("", "/patas/cmd"))

    def test_nombres_de_mujoco_y_urdf(self):
        nombres = ["FR_hip", "brazo/codo.1", "joint-2", "J3"]
        j = self.load(MINIMO + joints(names=nombres, kp=1, kd=0)).joints[0]
        self.assertEqual(j.names, tuple(nombres))
        self.assertEqual(j.kp, (1.0,) * 4)

    def test_dos_grupos(self):
        cfg = self.load(MINIMO + joints() + joints("brazo", names=["hombro", "codo"]))
        self.assertEqual([j.name for j in cfg.joints], ["patas", "brazo"])

    def test_mismo_name_en_perfiles_distintos_conviven(self):
        texto = MINIMO + joints(profile="real") + joints(profile="sim", state="/sim/estado", command="/sim/cmd")
        real, sim = self.load(texto, "real"), self.load(texto, "sim")
        self.assertEqual([(j.name, j.state) for j in real.joints], [("patas", "/patas/estado")])
        self.assertEqual([(j.name, j.state) for j in sim.joints], [("patas", "/sim/estado")])
        # Hasta con los mismos topics y los mismos nombres: nunca se usan juntos.
        self.load(MINIMO + joints(profile="real") + joints(profile="sim"))

    def test_topic_declarado_con_su_tipo_o_sin_tipo(self):
        self.load(MINIMO + '\n[[topic]]\nname = "/patas/estado"\ntype = "JointState"\n' + joints())
        self.load(MINIMO + '\n[[topic]]\nname = "/patas/cmd"\ntype = "JointCommand"\n' + joints())
        self.load(MINIMO + '\n[[topic]]\nname = "/patas/cmd"\n' + joints())   # sin type: lo revisa el hub

    def test_el_perfil_de_joints_cuenta_como_conocido(self):
        cfg = self.load(MINIMO + joints(profile="mesa"), "mesa")
        self.assertEqual(cfg.joints[0].profiles, ("mesa",))

    def test_aviso_de_perfil_mal_escrito(self):
        cfg = self.load(MINIMO + joints(profile="rael"))
        self.assertTrue(any("[[joints]] 'patas'" in w and "'real'" in w and "se usa" in w
                            and "[[topic]] o [[joints]] nombra" in w for w in cfg.warnings),
                        cfg.warnings)

    def test_lineas_de_arranque(self):
        from agmen.run import joints_lines

        cfg = self.load(MINIMO + joints() + joints("brazo", names=["hombro", "codo"], kp=[1, 2], kd=None,
                                                  require_names=True)
                        + joints("cabeza", command=None, kp=None, kd=None, names=["cuello"]))
        lineas = joints_lines(cfg)
        self.assertEqual(lineas[0], "articulaciones 'patas': 12 (FR_hip, FR_thigh … RL_calf) · estado "
                                    "/patas/estado · comando /patas/cmd · kp 20, kd 0.5")
        self.assertEqual(lineas[1], "articulaciones 'brazo': 2 (hombro, codo) · estado /brazo/estado · comando "
                                    "/brazo/cmd · kp por articulación (1 a 2) · require_names")
        self.assertEqual(lineas[2], "articulaciones 'cabeza': 1 (cuello) · estado /cabeza/estado")
        self.assertIn("sin kp ni kd (se usan las del driver)",
                      joints_lines(self.load(MINIMO + joints(kp=None, kd=None)))[0])
        self.assertEqual(joints_lines(self.load(MINIMO)), [])

    def test_agmen_run_nombra_el_archivo(self):
        import contextlib

        from agmen import cli

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "robot.toml"
            path.write_text(MINIMO + joints(names=["a", "a"], kp=1, kd=1), encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli.main(["run", str(path), "--no-web"])
        self.assertEqual(code, 2)
        self.assertIn(f"error en {path}: [[joints]] 'patas': 'a' está dos veces en names", err.getvalue())

    # --- lo que no vale: no arranca y dice qué pasó y qué hacer ---
    def test_nombre_repetido_en_el_grupo(self):
        self.assertConfigError(MINIMO + joints(names=PATAS[:3] + ["FR_hip"], kp=1, kd=1),
                               "[[joints]] 'patas'", "'FR_hip' está dos veces en names (names[0] y names[3])",
                               "quita una o corrige el nombre")

    def test_nombre_repetido_entre_grupos(self):
        self.assertConfigError(MINIMO + joints() + joints("brazo", names=["hombro", "FL_calf"], kp=1, kd=1),
                               "la articulación 'FL_calf' está en dos grupos", "[[joints]] 'patas'",
                               "[[joints]] 'brazo'", "quítala de uno")

    def test_nombre_repetido_entre_grupos_de_perfiles_distintos_vale(self):
        cfg = self.load(MINIMO + joints(profile="real")
                        + joints("sim_patas", profile="sim", state="/s/estado", command="/s/cmd"), "sim")
        self.assertEqual([j.name for j in cfg.joints], ["sim_patas"])

    def test_nombre_repetido_entre_grupos_que_comparten_un_perfil(self):
        self.assertConfigError(MINIMO + joints(profile=["real", "mesa"])
                               + joints("otro", profile=["sim", "mesa"], state="/o/e", command="/o/c"),
                               "la articulación 'FR_hip' está en dos grupos que se usan juntos en el perfil 'mesa'")

    def test_dos_grupos_con_el_mismo_name(self):
        self.assertConfigError(MINIMO + joints() + joints(state="/b/e", command="/b/c", names=["x"]),
                               "hay dos [[joints]] llamados 'patas'", "ponles perfiles distintos")

    def test_dos_grupos_con_el_mismo_name_en_el_mismo_perfil(self):
        self.assertConfigError(MINIMO + joints(profile="sim") + joints(state="/b/e", command="/b/c", names=["x"]),
                               "hay dos [[joints]] llamados 'patas' que se usan juntos en el perfil 'sim'",
                               "Cambia el name de uno")

    def test_state_y_command_iguales(self):
        self.assertConfigError(MINIMO + joints(state="/patas", command="/patas"),
                               "[[joints]] 'patas': state y command son el mismo topic ('/patas')", "Usa dos topics")

    def test_topic_de_otro_grupo(self):
        for clave in ("state", "command"):
            with self.subTest(clave=clave):
                otro = {"state": "/x/e", "command": "/x/c", clave: "/patas/cmd"}
                self.assertConfigError(MINIMO + joints() + joints("brazo", names=["codo"], **otro),
                                       f"[[joints]] 'brazo': {clave} = '/patas/cmd' ya es el command del "
                                       f"[[joints]] 'patas'", "cambia uno")

    def test_topic_declarado_con_otro_tipo(self):
        self.assertConfigError(MINIMO + '\n[[topic]]\nname = "/patas/estado"\ntype = "Imu"\n' + joints(),
                               "[[joints]] 'patas': state = '/patas/estado' es un topic de JointState, pero "
                               "[[topic]] '/patas/estado' dice type = \"Imu\"", "type = \"JointState\"")
        self.assertConfigError(MINIMO + '\n[[topic]]\nname = "/patas/cmd"\ntype = "JointState"\n' + joints(),
                               "command = '/patas/cmd' es un topic de JointCommand")

    def test_topic_con_otro_tipo_en_otro_perfil_vale(self):
        self.load(MINIMO + '\n[[topic]]\nname = "/patas/estado"\ntype = "Imu"\nprofile = "sim"\n'
                  + joints(profile="real"))

    def test_topic_de_agmen_o_del_mux(self):
        """El estado de un [[mux]] (/mux/<nombre>) es un topic de Agmen: lo rechaza la misma regla que /clock,
        haya o no un [[mux]] con ese nombre."""
        self.assertConfigError(MINIMO + joints(state="/clock"), "state = '/clock' es un topic de Agmen (de tipo Clock)")
        self.assertConfigError(MINIMO + joints(state="/mux/nadie"), "es un topic de Agmen (de tipo MuxEstado)")
        mux = ('\n[[mux]]\nname = "ruedas"\noutput = "127.0.0.1:7500"\n[[mux.input]]\nname = "a"\n'
               'listen = "127.0.0.1:7510"\npriority = 1\n')
        self.assertConfigError(MINIMO + mux + joints(command="/mux/ruedas"), "es un topic de Agmen (de tipo MuxEstado)")

    def test_topic_del_panel_con_otro_tipo(self):
        self.assertConfigError(MINIMO + '\n[web.teleop]\ntarget = "/patas/cmd"\n' + joints(),
                               "[[joints]] 'patas': command = '/patas/cmd' es un topic de JointCommand, pero "
                               "[web.teleop] manda MotorCommand")

    def test_topic_invalido(self):
        self.assertConfigError(MINIMO + joints(state="patas"), "[[joints]] 'patas' state:")
        self.assertConfigError(MINIMO + joints(command=5), "command es el topic de JointCommand")

    def test_sin_state_ni_command(self):
        self.assertConfigError(MINIMO + joints(state=None, command=None),
                               "[[joints]] 'patas': falta 'state'", "o 'command'", "Pon al menos uno")

    def test_falta_name(self):
        self.assertConfigError(MINIMO + joints(name=None, state="/a", command="/b"),
                               "[[joints]] #1: falta 'name'", 'name = "patas"')
        self.assertConfigError(MINIMO + joints(name="mis patas", state="/a", command="/b"),
                               "no es un nombre de grupo válido")

    def test_names_falta_o_vacia(self):
        self.assertConfigError(MINIMO + joints(names=None), "[[joints]] 'patas': falta 'names'", "ESTE es el orden")
        self.assertConfigError(MINIMO + joints(names=[]), "names es una lista no vacía")
        self.assertConfigError(MINIMO + joints(names="FR_hip"), "names es una lista no vacía")

    def test_names_con_algo_que_no_es_texto(self):
        self.assertConfigError(MINIMO + joints(names=["a", 3], kp=1, kd=1),
                               "[[joints]] 'patas': names[1] = 3 no es un texto")

    def test_nombres_invalidos(self):
        casos = {"": "está vacío", "FR hip": "tiene un espacio", "a,b": "tiene una coma", "a=b": "tiene un '='",
                 "a\tb": "carácter de control", "a\x01": "carácter de control", "cadera_izquierdá": "tiene 'á'",
                 "a:b": "tiene ':'"}
        for nombre, por_que in casos.items():
            with self.subTest(nombre=nombre):
                self.assertConfigError(MINIMO + joints(names=["ok", nombre], kp=1, kd=1),
                                       "[[joints]] 'patas': names[1] =", "no es un nombre de articulación válido",
                                       por_que, "'_', '-', '.' o '/'")

    def test_mas_articulaciones_de_las_que_caben(self):
        from agmen.joints import max_joints

        sc, cmd = max_joints("JointState"), max_joints("JointCommand")
        self.assertEqual((sc, cmd), (5453, 3272))
        muchos = [f"j{i}" for i in range(cmd + 1)]
        self.assertConfigError(MINIMO + joints(names=muchos, kp=1, kd=1),
                               f"names tiene {cmd + 1} articulaciones y un JointCommand cabe en un paquete UDP "
                               f"con hasta {cmd}", "varios [[joints]]")
        # JointState sí admite ese número, pero la lista no cabe en el campo `names` del hub (RFC 0013 D6): el tope
        # de caracteres llega antes (con nombres como "j1234", unas 1550 articulaciones).
        self.assertConfigError(MINIMO + joints(names=muchos, command=None, kp=None, kd=None),
                               "names ocupa", "el máximo es 8192", "nombres más cortos", "varios grupos")
        self.assertConfigError(MINIMO + joints(names=muchos[:cmd], kp=1, kd=1), "names ocupa")
        demasiados = [f"j{i}" for i in range(sc + 1)]
        self.assertConfigError(MINIMO + joints(names=demasiados, command=None, kp=None, kd=None),
                               f"un JointState cabe en un paquete UDP con hasta {sc}")

    def test_ganancias_invalidas(self):
        casos = [(-1, "kp = -1 es negativo"), ("nan", "no es un número finito"), ("inf", "no es un número finito"),
                 ("-inf", "no es un número finito"), ('"20"', "kp = '20' no es un número"),
                 ("true", "kp = True no es un número"), ("[1, 2]", "La lista de kp tiene 2 y names tiene 12"),
                 ("[" + ", ".join(["1"] * 11) + ", -0.5]", "kp[11] = -0.5 es negativo"),
                 ("[" + ", ".join(["1"] * 11) + ", nan]", "kp[11] = nan no es un número finito"),
                 ("[" + ", ".join(["1"] * 11) + ", false]", "kp[11] = False no es un número"),
                 ("[]", "La lista de kp tiene 0")]
        for valor, frag in casos:
            with self.subTest(valor=valor):
                texto = MINIMO + joints(kp=None) + f"kp = {valor}\n"
                self.assertConfigError(texto, "[[joints]] 'patas': kp es la ganancia de posición", frag,
                                       "un número ≥ 0 para todas")
        self.assertConfigError(MINIMO + joints(kd=None) + "kd = -0.1\n",
                               "kd es la ganancia de velocidad", "kd = -0.1 es negativo")

    def test_require_names(self):
        self.assertConfigError(MINIMO + joints(require_names="si"), "require_names es true o false")
        self.assertConfigError(MINIMO + joints(require_names=True, command=None),
                               "require_names = true exige la lista", "este grupo no tiene command")

    def test_clave_desconocida(self):
        self.assertConfigError(MINIMO + joints(nmaes=["a"]), "clave desconocida 'nmaes' en [[joints]] #1",
                               "¿Quisiste decir 'names'?")

    def test_joints_no_es_lista_de_tablas(self):
        self.assertConfigError('joints = "patas"\n' + MINIMO, "[[joints]]")
        self.assertConfigError('[joints]\nname = "patas"\n' + MINIMO, "[[joints]]")

    def test_profile_con_las_reglas_de_topic(self):
        self.assertConfigError(MINIMO + joints(profile=[]), "[[joints]] 'patas': profile es un perfil")
        self.assertConfigError(MINIMO + joints(profile="sí"), "no es un nombre de perfil válido")
        self.assertConfigError(MINIMO + joints(), "el perfil 'mesa' no existe", "[[joints]]", profile="mesa")

    # --- ataques de la prueba independiente (RFC 0013 §3.4) ---
    def test_nombres_que_se_parecen_pero_no_valen(self):
        """Un nombre viaja luego en una línea de texto del hub: nada que se vea igual pero no lo sea."""
        casos = {"FR_hіp": "tiene 'і'",            # i cirílica
                 "FR_hip ": "tiene un espacio",     # espacio al final
                 "FR_hip\n": "carácter de control",
                 "a１": "tiene '１'",               # dígito de ancho completo
                 "a\x00b": "carácter de control",
                 "FR\u200bhip": "tiene"}             # espacio de ancho cero
        for nombre, por_que in casos.items():
            with self.subTest(nombre=nombre):
                self.assertConfigError(MINIMO + joints(names=["ok", nombre], kp=1, kd=1),
                                       "[[joints]] 'patas': names[1] =", por_que)

    def test_mayusculas_distintas_son_nombres_distintos(self):
        j = self.load(MINIMO + joints(names=["FR_hip", "fr_hip", "FR_HIP"], kp=1, kd=1)).joints[0]
        self.assertEqual(j.names, ("FR_hip", "fr_hip", "FR_HIP"))

    def test_names_como_texto_o_anidada(self):
        self.assertConfigError(MINIMO + joints(names="FR_hip"), "names es una lista no vacía")
        self.assertConfigError(MINIMO + joints(names=[["a", "b"]], kp=1, kd=1),
                               "names[0] = ['a', 'b'] no es un texto", 'ej. "FR_hip"')

    def test_ganancias_que_no_son_numeros_de_toml(self):
        for valor, frag in (("1979-05-27", "no es un número"), ("{ a = 1 }", "no es un número"),
                            ("[[1], [2]]", "no es un número"), ("[1, true]", "kp[1] = True no es un número"),
                            ("1e999", "no es un número finito")):
            with self.subTest(valor=valor):
                self.assertConfigError(MINIMO + joints(names=["a", "b"], kp=None) + f"kp = {valor}\n",
                                       "[[joints]] 'patas': kp", frag)
        self.assertEqual(self.load(MINIMO + joints(names=["a"], kp=0, kd=0)).joints[0].kp, (0.0,))   # cero vale

    def test_miles_de_articulaciones_y_nombres_largos_cargan_rapido(self):
        t0 = time.monotonic()
        j = self.load(MINIMO + joints(names=[f"j{i}" for i in range(1550)], kp=1, kd=1)).joints[0]
        self.assertEqual(len(j.names), 1550)       # 8189 caracteres: cabe en el campo `names` del hub (8192)
        self.assertEqual(self.load(MINIMO + joints(names=["a" * 64], kp=1, kd=1)).joints[0].names, ("a" * 64,))
        # Más que eso no cabe en el registro del hub (RFC 0013 D6): un error claro, y rápido.
        self.assertConfigError(MINIMO + joints(names=[f"j{i}" for i in range(3000)], kp=1, kd=1),
                               "names ocupa 16889 caracteres", "el máximo es 8192")
        self.assertConfigError(MINIMO + joints(names=["a" * 20000], kp=1, kd=1),
                               "mide 20000 caracteres y el máximo es 64")
        self.assertLess(time.monotonic() - t0, 5)

    def test_el_mux_puede_publicar_en_el_command_del_grupo(self):
        """El mux es quien manda al actuador: su salida (o una entrada) puede ser el command del grupo."""
        def mux(salida, entradas):
            ins = "".join(f'\n[[mux.input]]\nname = "i{k}"\ntopic = "{t}"\npriority = {k + 1}\n'
                          for k, t in enumerate(entradas))
            return f'\n[[mux]]\nname = "m"\noutput = "{salida}"\n{ins}'
        self.load(MINIMO + mux("/patas/cmd", ["/a", "/b"]) + joints())
        self.load(MINIMO + mux("/o", ["/patas/cmd", "/b"]) + joints())
        self.load(MINIMO + mux("/patas/estado", ["/a", "/b"]) + joints())

    def test_el_panel_de_cualquier_perfil_choca_antes_de_arrancar(self):
        """Como las tablas [profile.X]: un choque en el panel de 'sim' no espera a que corras --sim."""
        panel = '\n[profile.sim.web]\nhttp = "127.0.0.1:8080"\n[profile.sim.web.teleop]\ntarget = "/patas/cmd"\n'
        msg = self.assertConfigError(MINIMO + panel + joints(), "[[joints]] 'patas': command = '/patas/cmd'",
                                     "[profile.sim.web.teleop] manda MotorCommand a ese topic (en el perfil 'sim', "
                                     "que se revisa aunque ahora uses 'real')", "usa otro topic")
        self.assertNotIn("(en el perfil", self.assertConfigError(MINIMO + panel + joints(), "[profile.sim.web.teleop]",
                                                                 profile="sim"))   # el activo: sin la nota
        self.assertIn("Un topic, un tipo", msg)
        self.load(MINIMO + panel + joints(profile="real"), "sim")    # el grupo no corre en sim: no choca
        self.load(MINIMO + panel + joints(profile="real"))
        # El [web] de arriba lo usa un perfil sin [profile.X.web]: un grupo solo de 'sim' choca con él.
        arriba = '\n[web.teleop]\ntarget = "/patas/cmd"\n'
        self.assertConfigError(MINIMO + arriba + joints(profile="sim"), "[web.teleop] manda MotorCommand",
                               "(en el perfil 'sim'")
        self.load(MINIMO + arriba + '\n[profile.sim.web]\nhttp = "127.0.0.1:8080"\n' + joints(profile="sim"))

    def test_entero_enorme_en_las_ganancias(self):
        """TOML admite enteros de cualquier tamaño: no revienta, dice que no es un número finito."""
        import contextlib

        from agmen import cli

        enorme = "9" * 400
        for kp in (enorme, "[" + ", ".join(["1"] * 11) + f", {enorme}]"):
            with self.subTest(kp=kp[:20]):
                self.assertConfigError(MINIMO + joints(kp=None) + f"kp = {kp}\n", "kp es la ganancia de posición",
                                       "no es un número finito (es demasiado grande)", "(400 dígitos)")
                with tempfile.TemporaryDirectory() as d:
                    path = Path(d) / "robot.toml"
                    path.write_text(MINIMO + joints(kd=None) + f"kd = {kp}\n", encoding="utf-8")
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        code = cli.main(["run", str(path), "--no-web"])
                self.assertEqual(code, 2)
                self.assertIn(f"error en {path}: [[joints]] 'patas': kd es la ganancia de velocidad", err.getvalue())
                self.assertIn("no es un número finito", err.getvalue())
                self.assertNotIn("Traceback", err.getvalue())

    def test_ganancia_que_no_cabe_en_f32(self):
        self.assertConfigError(MINIMO + joints(kp=None) + "kp = 1e39\n",
                               "kp = 1e+39 no cabe en el f32 del comando (máximo 3.40282e+38); en el driver sería inf")
        self.assertConfigError(MINIMO + joints(kd=None) + "kd = [" + ", ".join(["1"] * 11) + ", 4e38]\n",
                               "kd[11] = 4e+38 no cabe en el f32 del comando")
        self.assertConfigError(MINIMO + joints(kp=None) + f"kp = {10 ** 39}\n", "no cabe en el f32")
        self.assertEqual(self.load(MINIMO + joints(names=["a"], kp=3.4028234663852886e38, kd=0)).joints[0].kp,
                         (3.4028234663852886e38,))   # el máximo de f32 sí cabe

    def test_menos_cero_es_cero(self):
        import math

        from agmen.run import joints_lines

        cfg = self.load(MINIMO + joints(names=["a", "b"], kp=-0.0, kd=[0.0, -0.0]))
        j = cfg.joints[0]
        self.assertEqual([math.copysign(1, v) for v in j.kp + j.kd], [1.0] * 4)
        self.assertIn("kp 0, kd 0", joints_lines(cfg)[0])

    def test_el_nombre_empieza_con_letra_digito_o_guion_bajo(self):
        for nombre in ("/", ".", "-", "-a", ".a", "/a", "--", "./x"):
            with self.subTest(nombre=nombre):
                self.assertConfigError(MINIMO + joints(names=["ok", nombre], kp=1, kd=1),
                                       "[[joints]] 'patas': names[1] =", "no es un nombre de articulación válido",
                                       f"empieza con '{nombre[0]}'; el primer carácter es una letra, un dígito o '_'",
                                       "empieza con letra, dígito o '_'")
        nombres = ["_a", "1", "a-", "b/", "c.", "_"]
        self.assertEqual(self.load(MINIMO + joints(names=nombres, kp=1, kd=1)).joints[0].names, tuple(nombres))

    def test_los_topics_de_joints_llegan_al_hub_con_su_tipo(self):
        from agmen.run import prepare_hub

        topic = '\n[[topic]]\nname = "/imu"\ntype = "Imu"\n'
        cfg, _ = prepare_hub(self.load(MINIMO + topic + joints() + joints("cabeza", command=None, names=["cuello"],
                                                                         kp=None, kd=None)), ping=lambda a: None)
        cmd = next(n for n in cfg.nodes if n.name == "hub").cmd
        for esperado in ("/imu=Imu", "/patas/estado=JointState", "/patas/cmd=JointCommand",
                         "/cabeza/estado=JointState"):
            self.assertIn(esperado, cmd)
        self.assertNotIn("/cabeza/cmd", " ".join(cmd))
        # Sin [[topic]] el hub no revisa nombres (no avisa de los no declarados): [[joints]] no lo cambia.
        cfg, _ = prepare_hub(self.load(MINIMO + joints()), ping=lambda a: None)
        self.assertNotIn("--expect", next(n for n in cfg.nodes if n.name == "hub").cmd)
        # Un [[topic]] sin type con el mismo nombre: el hub recibe el tipo de [[joints]].
        sin_tipo = '\n[[topic]]\nname = "/patas/estado"\nrequired = true\n'
        cfg, _ = prepare_hub(self.load(MINIMO + sin_tipo + joints()), ping=lambda a: None)
        cmd = next(n for n in cfg.nodes if n.name == "hub").cmd
        from agmen import hub as H

        esperados = H.parse_expected([cmd[i + 1] for i, a in enumerate(cmd) if a == "--expect"],
                                     [cmd[i + 1] for i, a in enumerate(cmd) if a == "--require"])
        estado = next(e for e in esperados if e.topic == "/patas/estado")
        self.assertEqual((estado.type, estado.required), ("JointState", True))

    def test_hub_de_verdad_con_joints(self):
        """De punta a punta: un hub con lo que le pasa agmen run. JointState[12] en el state del grupo no
        es "no declarado"; Imu en el command avisa como un [[topic]] con type."""
        import socket

        import agmen
        from agmen import hub as H
        from agmen.msgs import Imu, JointState
        from agmen.run import prepare_hub

        topic = '\n[[topic]]\nname = "/imu"\ntype = "Imu"\n'
        cfg, _ = prepare_hub(self.load(MINIMO + topic + joints()), ping=lambda a: None)
        cmd = next(n for n in cfg.nodes if n.name == "hub").cmd
        esperados = H.parse_expected([cmd[i + 1] for i, a in enumerate(cmd) if a == "--expect"],
                                     [cmd[i + 1] for i, a in enumerate(cmd) if a == "--require"])
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        addr = f"127.0.0.1:{s.getsockname()[1]}"
        s.close()
        logs: list[str] = []
        hub = H.Hub(addr, esperados, log=logs.append, grace_s=0.0)
        stop = threading.Event()
        hilo = threading.Thread(target=hub.serve, args=(stop,), daemon=True)
        hilo.start()
        antes = os.environ.get("AGMEN_HUB")
        os.environ["AGMEN_HUB"] = addr
        try:
            p1 = agmen.Publisher(JointState[12], "/patas/estado")
            p2 = agmen.Publisher(Imu, "/patas/cmd")
            p3 = agmen.Publisher(Imu, "/otro")
            fin = time.monotonic() + 5
            while time.monotonic() < fin and not any("'/otro'" in x for x in logs):
                time.sleep(0.05)
            texto = "\n".join(logs)
            self.assertNotIn("'/patas/estado', que no está declarado", texto)
            self.assertNotIn("'/patas/estado' debería ser", texto)
            self.assertIn("'/patas/cmd' debería ser JointCommand (robot.toml)", texto)
            self.assertIn("usa Imu", texto)
            self.assertIn("'/otro', que no está declarado en robot.toml", texto)   # el hub sí revisa
            for p in (p1, p2, p3):
                p.close()
        finally:
            stop.set()
            hilo.join(2)
            if antes is None:
                os.environ.pop("AGMEN_HUB", None)
            else:
                os.environ["AGMEN_HUB"] = antes

    def test_un_topic_con_perfil_que_se_cruza_choca_y_uno_que_no_se_cruza_no(self):
        topic = '\n[[topic]]\nname = "/patas/estado"\ntype = "Imu"\nprofile = "real"\n'
        self.assertConfigError(MINIMO + topic + joints(), "los dos se usan en el perfil 'real'")
        self.load(MINIMO + topic + joints(profile="sim"), "sim")

    def test_un_archivo_sin_joints_dice_lo_mismo_que_en_el_tutorial(self):
        """docs/tutorial/05: ni el aviso ni el error de perfil cambian si el archivo no tiene [[joints]]."""
        base = MINIMO.replace('name = "motor"', 'name = "motor"\nprofile = "rael"')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "perfiles.toml"
            path.write_text(base, encoding="utf-8")
            aviso, = load_config(path).warnings
            with self.assertRaises(ConfigError) as ctx:
                load_config(path, "mesa")
        self.assertEqual(aviso, '[[node]] \'motor\': profile = "rael" y ningún otro [[node]], [[mux]] o [[topic]] '
                                'nombra ese perfil. ¿Quisiste decir \'real\'? Así, \'motor\' solo corre con --profile '
                                'rael. Si el perfil es a propósito, declara [profile.rael] y este aviso se quita')
        self.assertTrue(str(ctx.exception).endswith("Un perfil existe si algún [[node]], [[mux]] o [[topic]] lo "
                                                    "nombra en su profile, o si tiene su tabla [profile.mesa]"))


# --- el [[mux]] hereda la lista de [[joints]] (RFC 0013 D6 = d) ------------------------------------------------------

def mux(name, output, *entradas, extra=""):
    """Un [[mux]] en TOML; cada entrada es (nombre, topic o dirección, prioridad)."""
    text = f'\n[[mux]]\nname = "{name}"\noutput = "{output}"\n{extra}'
    for nombre, donde, prio in entradas:
        clave = "topic" if donde.startswith("/") else "listen"
        text += f'\n[[mux.input]]\nname = "{nombre}"\n{clave} = "{donde}"\npriority = {prio}\n'
    return text


def hub_args(cfg):
    """--names y --require-names que agmen run le pasa al hub: ({topic: lista}, {topics que la exigen})."""
    from agmen.run import prepare_hub

    cfg, _ = prepare_hub(cfg, ping=lambda a: None)
    cmd = next(n for n in cfg.nodes if n.name == "hub").cmd
    names = dict(cmd[i + 1].split("=", 1) for i, a in enumerate(cmd) if a == "--names")
    return names, {cmd[i + 1] for i, a in enumerate(cmd) if a == "--require-names"}


class TestMuxHeredaLista(unittest.TestCase):
    load = TestJoints.load
    assertConfigError = TestJoints.assertConfigError
    LISTA = ",".join(PATAS)

    @staticmethod
    def mux_cmd(cfg, name):
        return next(n for n in cfg.nodes if n.name == name).cmd

    def test_una_entrada(self):
        cfg = self.load(MINIMO + joints(require_names=True) + mux("ruedas", "/patas/cmd", ("ia", "/cmd/ia", 1)))
        e, = cfg.inherited
        self.assertEqual((e.topic, e.group, e.names, e.require, e.mux, e.input),
                         ("/cmd/ia", "patas", tuple(PATAS), True, "ruedas", "ia"))
        cmd = self.mux_cmd(cfg, "ruedas")
        self.assertEqual(cmd[cmd.index("--names") + 1], self.LISTA)
        names, req = hub_args(cfg)
        self.assertEqual(names, {"/patas/cmd": self.LISTA, "/patas/estado": self.LISTA, "/cmd/ia": self.LISTA})
        self.assertEqual(req, {"/patas/cmd", "/cmd/ia"})
        self.assertEqual(cfg.warnings, [])

    def test_varias_entradas_y_sin_require(self):
        cfg = self.load(MINIMO + joints() + mux("ruedas", "/patas/cmd", ("teleop", "/cmd/teleop", 10),
                                                ("ia", "/cmd/ia", 1)))
        self.assertEqual([(e.topic, e.require) for e in cfg.inherited], [("/cmd/teleop", False), ("/cmd/ia", False)])
        names, req = hub_args(cfg)
        self.assertEqual(names["/cmd/teleop"], self.LISTA)
        self.assertEqual(names["/cmd/ia"], self.LISTA)
        self.assertEqual(req, set())

    def test_transitiva_en_cualquier_orden(self):
        # El mux de abajo se declara ANTES que el que escribe en el comando: igual se resuelve.
        texto = (MINIMO + joints(require_names=True) + mux("abajo", "/cmd/ia", ("x", "/cmd/x", 1))
                 + mux("ruedas", "/patas/cmd", ("ia", "/cmd/ia", 1)))
        cfg = self.load(texto)
        self.assertEqual({e.topic: (e.mux, e.require) for e in cfg.inherited},
                         {"/cmd/ia": ("ruedas", True), "/cmd/x": ("abajo", True)})
        cmd = self.mux_cmd(cfg, "abajo")
        self.assertEqual(cmd[cmd.index("--names") + 1], self.LISTA)   # su salida lleva la lista: la declara
        _, req = hub_args(cfg)
        self.assertEqual(req, {"/patas/cmd", "/cmd/ia", "/cmd/x"})

    def test_desde_el_state_no_se_exige(self):
        cfg = self.load(MINIMO + joints(require_names=True) + mux("eco", "/patas/estado", ("a", "/eco/a", 1)))
        e, = cfg.inherited
        self.assertFalse(e.require)
        self.assertNotIn("/eco/a", hub_args(cfg)[1])

    def test_sin_joints_o_salida_ajena_nada_cambia(self):
        """Quien no usa [[mux]] con [[joints]]: los mismos argumentos al mux y al hub que antes."""
        for texto in (MINIMO + mux("ruedas", "/otro/cmd", ("ia", "/cmd/ia", 1)),
                      MINIMO + joints() + mux("ruedas", "/otro/cmd", ("ia", "/cmd/ia", 1)),
                      MINIMO + joints() + mux("ruedas", "127.0.0.1:7500", ("ia", "/cmd/ia", 1))):
            with self.subTest(texto=texto[-80:]):
                cfg = self.load(texto)
                self.assertEqual(cfg.inherited, [])
                self.assertNotIn("--names", self.mux_cmd(cfg, "ruedas"))
                names, _ = hub_args(cfg)
                self.assertNotIn("/cmd/ia", names)

    def test_solo_el_perfil_activo(self):
        texto = (MINIMO + joints(profile="sim") + mux("ruedas", "/patas/cmd", ("ia", "/cmd/ia", 1))
                 + mux("brazo", "/patas/cmd", ("b", "/cmd/b", 1), extra='profile = "real"\n'))
        sim = self.load(texto, "sim")
        self.assertEqual([e.topic for e in sim.inherited], ["/cmd/ia"])
        self.assertIn("--names", self.mux_cmd(sim, "ruedas"))
        real = self.load(texto, "real")
        self.assertEqual(real.inherited, [])         # en 'real' no hay [[joints]]
        self.assertNotIn("--names", self.mux_cmd(real, "brazo"))
        self.assertNotIn("--names", self.mux_cmd(real, "ruedas"))

    def test_choque_dos_mux_de_grupos_distintos(self):
        brazo = joints("brazo", names=["hombro", "codo"], kp=None, kd=None)
        texto = (MINIMO + joints() + brazo + mux("m1", "/patas/cmd", ("ia", "/cmd/ia", 1))
                 + mux("m2", "/brazo/cmd", ("ia", "/cmd/ia", 1)))
        msg = self.assertConfigError(texto, "'/cmd/ia' llevaría dos listas de nombres distintas",
                                     "la entrada 'ia' del [[mux]] 'm2' (que escribe en '/brazo/cmd') lleva la del "
                                     "[[joints]] 'brazo' (hombro, codo (2 nombres))",
                                     "la entrada 'ia' del [[mux]] 'm1' (que escribe en '/patas/cmd') lleva la del "
                                     "[[joints]] 'patas'", "usa otro topic para la entrada 'ia' del [[mux]] 'm2'")
        self.assertIn("una sola lista", msg)

    def test_choque_con_un_topic_de_otro_joints(self):
        brazo = joints("brazo", names=["hombro", "codo"], kp=None, kd=None)
        self.assertConfigError(MINIMO + joints() + brazo + mux("m1", "/patas/cmd", ("b", "/brazo/cmd", 1)),
                               "'/brazo/cmd' llevaría dos listas de nombres distintas",
                               "como el command del [[joints]] 'brazo' lleva la del [[joints]] 'brazo'",
                               "la entrada 'b' del [[mux]] 'm1'")

    def test_la_misma_lista_no_es_choque(self):
        texto = (MINIMO + joints() + mux("m1", "/patas/cmd", ("ia", "/cmd/ia", 1))
                 + mux("m2", "/patas/estado", ("ia", "/cmd/ia", 1)))
        cfg = self.load(texto)
        self.assertEqual([e.topic for e in cfg.inherited], ["/cmd/ia"])
        # Por otro camino que sí la exige: se exige.
        cfg = self.load(MINIMO + joints(require_names=True) + mux("m1", "/patas/estado", ("ia", "/cmd/ia", 1))
                        + mux("m2", "/patas/cmd", ("ia", "/cmd/ia", 1)))
        self.assertTrue(cfg.inherited[0].require)
        # Una entrada que ya es un topic del mismo grupo, con la misma lista: tampoco.
        self.load(MINIMO + joints() + mux("m1", "/patas/cmd", ("e", "/patas/estado", 1)))

    def test_choque_en_otro_perfil_no_cuenta(self):
        brazo = joints("brazo", names=["hombro", "codo"], kp=None, kd=None, profile="sim")
        texto = (MINIMO + joints() + brazo + mux("m1", "/patas/cmd", ("ia", "/cmd/ia", 1))
                 + mux("m2", "/brazo/cmd", ("ia", "/cmd/ia", 1), extra='profile = "sim"\n'))
        self.load(texto, "real")
        self.assertConfigError(texto, "llevaría dos listas de nombres distintas", profile="sim")

    def test_entrada_por_direccion_avisa(self):
        cfg = self.load(MINIMO + joints() + mux("ruedas", "/patas/cmd", ("teleop", "127.0.0.1:7510", 10),
                                                ("ia", "/cmd/ia", 1)))
        aviso, = cfg.warnings
        self.assertEqual(aviso, "[[mux]] 'ruedas': la entrada 'teleop' escucha en una dirección (127.0.0.1:7510), "
                                "sin hub, y la salida '/patas/cmd' lleva la lista del [[joints]] 'patas': quien "
                                "mande a esa entrada no declara su lista y nadie revisa que use el orden de "
                                "robot.toml. Para que se revise, usa un topic en esa entrada (topic = \"/...\" en "
                                "lugar de listen)")
        self.assertEqual([e.topic for e in cfg.inherited], ["/cmd/ia"])

    def test_entrada_por_direccion_con_require_names_no_arranca(self):
        msg = self.assertConfigError(
            MINIMO + joints(require_names=True) + mux("ruedas", "/patas/cmd", ("teleop", "127.0.0.1:7510", 10)),
            "[[mux]] 'ruedas': la entrada 'teleop' escucha en una dirección (127.0.0.1:7510), sin hub",
            "require_names = true", "Usa un topic en esa entrada", "o quita require_names del [[joints]] 'patas'")
        self.assertIn("nadie puede revisarla", msg)

    def test_la_linea_del_arranque_dice_donde_vale(self):
        from agmen.run import joints_lines

        cfg = self.load(MINIMO + joints() + mux("ruedas", "/patas/cmd", ("teleop", "/cmd/teleop", 10),
                                                ("ia", "/cmd/ia", 1)))
        self.assertTrue(joints_lines(cfg)[0].endswith(
            " · la lista vale también en la entrada 'teleop' del mux 'ruedas' (/cmd/teleop), la entrada 'ia' del "
            "mux 'ruedas' (/cmd/ia)"), joints_lines(cfg))
        self.assertNotIn("vale también", joints_lines(self.load(MINIMO + joints()))[0])


class TestMuxHeredaFormasRaras(unittest.TestCase):
    """La regla de herencia con formas raras (RFC 0013 D6 = d): cadenas, ciclos, repetidos, perfiles y máquinas."""

    load = TestJoints.load
    assertConfigError = TestJoints.assertConfigError
    mux_cmd = staticmethod(TestMuxHeredaLista.mux_cmd)
    LISTA = ",".join(PATAS)

    def load_con_tope(self, text, profile=None, tope=20):
        """load() en un hilo con tope: un ciclo mal resuelto no debe colgar la prueba (ni agmen run)."""
        res: list = []

        def hacer():
            try:
                res.append(self.load(text, profile))
            except Exception as e:     # (se devuelve para que la prueba lo vea)
                res.append(e)
        t = threading.Thread(target=hacer, daemon=True)
        t.start()
        t.join(tope)
        self.assertFalse(t.is_alive(), "load_config no termina (¿ciclo de [[mux]] sin resolver?)")
        return res[0]

    def test_cadena_de_tres_mux_en_cualquier_orden(self):
        import itertools

        piezas = [mux("m1", "/patas/cmd", ("a", "/c/a", 1)), mux("m2", "/c/a", ("b", "/c/b", 1)),
                  mux("m3", "/c/b", ("c", "/c/c", 1), ("d", "/c/d", 2))]
        for orden in itertools.permutations(piezas):
            with self.subTest(orden=[p.split('"')[1] for p in orden]):
                cfg = self.load(MINIMO + joints(require_names=True) + "".join(orden))
                self.assertEqual({e.topic for e in cfg.inherited}, {"/c/a", "/c/b", "/c/c", "/c/d"})
                self.assertTrue(all(e.require and e.group == "patas" for e in cfg.inherited))
                for m in ("m1", "m2", "m3"):
                    cmd = self.mux_cmd(cfg, m)
                    self.assertEqual(cmd.count("--names"), 1, cmd)
                    self.assertEqual(cmd[cmd.index("--names") + 1], self.LISTA)
                names, req = hub_args(cfg)
                self.assertEqual(req, {"/patas/cmd", "/c/a", "/c/b", "/c/c", "/c/d"})
                self.assertEqual(set(names), {"/patas/cmd", "/patas/estado", "/c/a", "/c/b", "/c/c", "/c/d"})

    def test_ciclo_termina_y_no_repite_topics_en_el_hub(self):
        # La salida del mux m1 (el comando) es entrada de m2, y la salida de m2 es entrada de m1: un ciclo.
        texto = (MINIMO + joints(require_names=True) + mux("m1", "/patas/cmd", ("x", "/c/x", 1))
                 + mux("m2", "/c/x", ("vuelta", "/patas/cmd", 1)))
        cfg = self.load_con_tope(texto)
        self.assertNotIsInstance(cfg, Exception, f"un ciclo no debería ser un error de robot.toml: {cfg!r}")
        self.assertEqual([e.topic for e in cfg.inherited], ["/c/x"])       # /patas/cmd ya llevaba la lista
        for m in ("m1", "m2"):
            self.assertEqual(self.mux_cmd(cfg, m).count("--names"), 1)
        # El hub no recibe un topic dos veces (el hub rechaza --names repetido).
        cmd = hub_cmd(cfg)[0]
        topics = [cmd[i + 1].split("=", 1)[0] for i, a in enumerate(cmd) if a == "--names"]
        self.assertEqual(len(topics), len(set(topics)), topics)

    def test_ciclo_de_tres_y_ciclo_sin_joints(self):
        ciclo = (mux("m1", "/c/a", ("x", "/c/b", 1)) + mux("m2", "/c/b", ("x", "/c/c", 1))
                 + mux("m3", "/c/c", ("x", "/c/a", 1)))
        cfg = self.load_con_tope(MINIMO + joints() + ciclo)             # ningún topic del ciclo es de [[joints]]
        self.assertEqual(cfg.inherited, [])
        enganchado = ciclo + mux("m4", "/patas/cmd", ("x", "/c/a", 1))
        cfg = self.load_con_tope(MINIMO + joints() + enganchado)       # y uno que sí entra al ciclo
        self.assertEqual({e.topic for e in cfg.inherited}, {"/c/a", "/c/b", "/c/c"})

    def test_ciclo_con_dos_listas_distintas_es_error_y_no_cuelga(self):
        brazo = joints("brazo", names=["hombro", "codo"], kp=None, kd=None)
        texto = (MINIMO + joints() + brazo + mux("m1", "/patas/cmd", ("x", "/c/x", 1))
                 + mux("m2", "/c/x", ("y", "/brazo/cmd", 1)))
        r = self.load_con_tope(texto)
        self.assertIsInstance(r, ConfigError)
        self.assertIn("llevaría dos listas de nombres distintas", str(r))

    def test_la_salida_es_el_state(self):
        cfg = self.load(MINIMO + joints(require_names=True) + mux("eco", "/patas/estado", ("a", "/eco/a", 1)))
        cmd = self.mux_cmd(cfg, "eco")
        self.assertEqual(cmd[cmd.index("--names") + 1], self.LISTA)
        names, req = hub_args(cfg)
        self.assertEqual(names["/eco/a"], self.LISTA)
        self.assertEqual(req, {"/patas/cmd"})                    # ni la salida (state) ni la entrada se exigen

    def test_la_misma_entrada_en_dos_mux_del_mismo_grupo(self):
        texto = (MINIMO + joints(require_names=True) + mux("m1", "/patas/cmd", ("ia", "/cmd/ia", 1))
                 + mux("m2", "/patas/cmd", ("ia", "/cmd/ia", 2)))
        cfg = self.load(texto)
        for m in ("m1", "m2"):
            self.assertIn("--names", self.mux_cmd(cfg, m))
        cmd = hub_cmd(cfg)[0]
        self.assertEqual([a for a in cmd if a.startswith("/cmd/ia=")], [f"/cmd/ia={self.LISTA}"])
        self.assertEqual(cmd.count("/cmd/ia"), 1)                # un solo --require-names /cmd/ia

    def test_la_entrada_es_la_propia_salida(self):
        for salida in ("/patas/cmd", "/patas/estado"):
            with self.subTest(salida=salida):
                r = self.load_con_tope(MINIMO + joints(require_names=True) + mux("m", salida, ("rizo", salida, 1)))
                self.assertIsInstance(r, ConfigError)      # (ya se rechaza antes de heredar nada, y lo dice claro)
                self.assertIn(f"{salida} se usa dos veces (entradas y salida deben ser puertos distintos)", str(r))

    def test_joints_sin_command(self):
        sin_cmd = joints(command=None)
        cfg = self.load(MINIMO + sin_cmd + mux("m", "/patas/cmd", ("ia", "/cmd/ia", 1)))     # salida ajena al grupo
        self.assertEqual(cfg.inherited, [])
        self.assertNotIn("--names", self.mux_cmd(cfg, "m"))
        cfg = self.load(MINIMO + sin_cmd + mux("m", "/patas/estado", ("ia", "/cmd/ia", 1)))  # salida = su state
        self.assertEqual([e.topic for e in cfg.inherited], ["/cmd/ia"])
        self.assertFalse(cfg.inherited[0].require)
        # require_names sin command no es válido (ya lo dice el grupo), con o sin mux
        self.assertConfigError(MINIMO + joints(command=None, require_names=True)
                               + mux("m", "/patas/estado", ("ia", "/cmd/ia", 1)), "no tiene command")

    def test_joints_solo_command_y_mux_a_otro_perfil(self):
        texto = (MINIMO + joints(profile="sim") + mux("m", "/patas/cmd", ("ia", "/cmd/ia", 1), extra='profile = "real"\n')
                 + mux("n", "/patas/cmd", ("ib", "/cmd/ib", 1)))
        sim = self.load(texto, "sim")        # el mux 'm' no corre en sim; el 'n' sí
        self.assertEqual([e.topic for e in sim.inherited], ["/cmd/ib"])
        real = self.load(texto, "real")      # en real no hay [[joints]]
        self.assertEqual(real.inherited, [])
        self.assertTrue(all("--names" not in n.cmd for n in real.nodes))

    def test_mux_en_otra_maquina_lleva_la_lista(self):
        texto = (MINIMO + maquina("robot", "192.168.1.20") + joints(require_names=True)
                 + mux("m", "/patas/cmd", ("ia", "/cmd/ia", 1), extra='machine = "robot"\n'))
        cfg = self.load(texto)
        self.assertEqual([e.topic for e in cfg.inherited], ["/cmd/ia"])
        m = next(n for n in cfg.nodes if n.name == "m")
        self.assertEqual(m.machine, "robot")
        self.assertEqual(m.cmd[m.cmd.index("--names") + 1], self.LISTA)

    def test_nombres_con_signos_raros_viajan_enteros_al_mux(self):
        raros = ["_a", "b/c.d-1", "9", "x.y"]
        cfg = self.load(MINIMO + joints(names=raros, kp=None, kd=None) + mux("m", "/patas/cmd", ("ia", "/cmd/ia", 1)))
        cmd = self.mux_cmd(cfg, "m")
        self.assertEqual(cmd[cmd.index("--names") + 1].split(","), raros)
        names, _ = hub_args(cfg)
        self.assertEqual(names["/cmd/ia"], ",".join(raros))

    def test_la_linea_de_comandos_enorme_es_un_error_claro(self):
        # 120 nombres de 64 caracteres (cabe en un grupo): con las entradas heredadas, el hub no cabe y lo dice.
        largos = [("n%03d" % i).ljust(64, "x") for i in range(120)]
        base = MINIMO + joints(names=largos, kp=None, kd=None, require_names=True)
        hub_args(self.load(base))                             # sin mux, el hub cabe
        texto = base + mux("m", "/patas/cmd", *[(f"e{k}", f"/entrada/{k}", k + 1) for k in range(6)])
        with self.assertRaises(ConfigError) as ctx:
            hub_args(self.load(texto))
        self.assertIn("no caben en la línea de comandos del agmen hub", str(ctx.exception))
        self.assertIn("entradas de [[mux]]", str(ctx.exception))


def hub_cmd(cfg):
    """[la línea de comandos del agmen hub] (en una lista, para usar next/[0])."""
    from agmen.run import prepare_hub

    cfg, _ = prepare_hub(cfg, ping=lambda a: None)
    return [next(n for n in cfg.nodes if n.name == "hub").cmd]


# --- un hub anterior no compara los nombres: agmen run lo avisa (RFC 0013 D6 = d) ----------------------------------

class TestHubAnteriorAviso(unittest.TestCase):
    load = TestJoints.load

    def propia(self, version):
        from unittest import mock

        p = mock.patch("agmen._version.__version__", version)
        p.start()
        self.addCleanup(p.stop)

    def test_comparar_versiones(self):
        from agmen import hubproto as hp

        self.assertEqual(hp.NAMES_SINCE, "0.15.0")
        si = ["0.15.0", "0.15.1", "0.16.0", "1.0.0", " 0.15.0 ", "0.15.10", "10.0.0"]
        no = ["0.14.0", "0.14.99", "0.15.0-dev", "0.15.0-rc.1", "?", "", "0.15", "v0.15.0", "abc", None, "0.15.0.1",
              "0.15.x", "0.15.0-", "1" * 20 + ".0.0"]
        for v in si:
            self.assertTrue(hp.version_at_least(v, hp.NAMES_SINCE), v)
        for v in no:
            self.assertFalse(hp.version_at_least(v, hp.NAMES_SINCE), v)
        self.assertTrue(hp.version_at_least("0.15.0", "0.15.0-dev"))
        self.assertTrue(hp.version_at_least("0.15.0-dev", "0.15.0-dev"))
        self.assertTrue(hp.version_at_least("0.9.0", "0.10.0") is False)   # números, no texto

    def test_texto(self):
        from agmen.run import old_hub_text

        self.propia("0.15.0")
        t = old_hub_text("0.14.0", "robot (192.168.1.20:7391) tiene", "Actualiza Agmen en robot.")
        self.assertEqual(t, "robot (192.168.1.20:7391) tiene Agmen 0.14.0, anterior a 0.15.0: ese agmen hub no compara "
                            "los nombres de las articulaciones (names=), así que ahí no se revisa que los nodos usen "
                            "el orden de [[joints]] de robot.toml (una lista con el orden cambiado entra sin aviso y "
                            "require_names no se exige). Actualiza Agmen en robot.")
        self.assertIn("una versión de Agmen que no se entiende (?)", old_hub_text("?", "x tiene", "y"))
        self.assertIn("que no se entiende (vacía)", old_hub_text("", "x tiene", "y"))
        self.assertIsNone(old_hub_text("0.15.0", "x tiene", "y"))
        self.assertIsNone(old_hub_text("0.16.2", "x tiene", "y"))
        self.propia("0.14.0")          # la propia todavía es anterior: no se distingue un hub viejo de este
        self.assertIsNone(old_hub_text("0.14.0", "x tiene", "y"))
        self.assertIsNone(old_hub_text("?", "x tiene", "y"))
        self.propia("0.15.0-dev")
        self.assertIsNone(old_hub_text("0.13.0", "x tiene", "y"))

    def test_a_el_hub_local_que_ya_corria(self):
        from agmen.run import prepare_hub

        con = self.load(MINIMO + joints())
        sin = self.load(MINIMO)
        for propia, del_hub, con_joints, avisa in (("0.15.0", "0.14.0", True, True), ("0.15.0", "?", True, True),
                                                   ("0.16.0", "0.15.0", True, False),
                                                   ("0.15.0", "0.14.0", False, False),
                                                   ("0.14.0", "0.13.0", True, False)):
            with self.subTest(propia=propia, hub=del_hub, joints=con_joints):
                self.propia(propia)
                cfg, nota = prepare_hub(con if con_joints else sin, ping=lambda a, v=del_hub: v)
                avisos = [w for w in cfg.warnings if "anterior a 0.15.0" in w]
                self.assertEqual(len(avisos), 1 if avisa else 0, cfg.warnings)
                if avisa:
                    self.assertTrue(avisos[0].startswith("el agmen hub que ya corre en "), avisos[0])
                    self.assertIn("Actualiza Agmen en esta computadora y detén ese agmen hub", avisos[0])
                # La nota de siempre no cambia (sin duplicar: el aviso nuevo dice otra cosa).
                self.assertEqual("(los [[joints]] de este robot.toml no se revisan)" in nota, con_joints)
                self.assertNotIn("anterior", nota)

    def test_b_cada_computadora_una_vez(self):
        from types import SimpleNamespace
        from unittest import mock

        from agmen import agent, cli

        class Enlace:
            def __init__(self, name, version):
                self.name, self.version, self.shown, self.address = name, version, f"10.0.0.{len(name)}:7391", None
                self.waiting, self.on_lost = False, None

            def watch_clock(self, clock):
                pass

            def open(self, key, msg, on_key_ok=None, stop=None):
                pass

            def close(self):
                pass

        def correr(cfg, versiones):
            said: list[str] = []
            launcher = SimpleNamespace(say=lambda t, k="agmen": said.append(t))
            links = {n: Enlace(n, v) for n, v in versiones.items()}
            with mock.patch.object(agent, "session_message", lambda *a, **k: {}):
                err = cli._connect(launcher, cfg, links, {n: ("k", "variable") for n in links}, "real", [],
                                   threading.Event())
            self.assertEqual(err, "")
            return [t for t in said if "anterior a 0.15.0" in t]

        con, sin = self.load(MINIMO + joints()), self.load(MINIMO)
        self.propia("0.15.0")
        avisos = correr(con, {"robot": "0.14.0", "brazo": "0.15.0", "base": "?"})
        self.assertEqual(len(avisos), 2, avisos)
        self.assertTrue(avisos[0].startswith("⚠ robot (10.0.0.5:7391) tiene Agmen 0.14.0, anterior a 0.15.0"))
        self.assertTrue(avisos[0].endswith("Actualiza Agmen en robot (pip install -U agmen) y vuelve a correr agmen "
                                           "run"))
        self.assertTrue(avisos[1].startswith("⚠ base (10.0.0.4:7391) tiene una versión de Agmen que no se entiende"))
        self.assertEqual(correr(sin, {"robot": "0.14.0"}), [])        # sin [[joints]]: nada que comparar
        self.assertEqual(correr(None, {"robot": "0.14.0"}), [])       # (sin config, como en las pruebas de la sonda)
        self.propia("0.14.0")
        self.assertEqual(correr(con, {"robot": "0.13.0"}), [])        # la propia todavía es anterior


class TestVersionesRaras(unittest.TestCase):
    """version_key / version_at_least / old_hub_text con versiones raras (RFC 0013 D6 = d, aviso de hub anterior)."""

    load = TestJoints.load
    propia = TestHubAnteriorAviso.propia

    def test_version_key_nunca_lanza_y_lo_raro_es_anterior(self):
        from agmen import hubproto as hp

        raras = ["0.15.0rc1", "0.15", "v0.15.0", "", "?", "x" * 5000, "0.15.0\x00", "0.15.0\x1b[31m", "\x00" * 10,
                 "٠.١٥.٠", "0.15.0+build5", "0.15.0-é", "1" * 5000 + ".0.0", "9" * 10 + ".0.0", "0.15.0-" + "a" * 5000,
                 b"0.15.0", 15, 0.15, [], {}, object()]
        for v in raras:
            with self.subTest(v=repr(v)[:40]):
                key = hp.version_key(v)
                at_least = hp.version_at_least(v, hp.NAMES_SINCE)
                if key is None:
                    self.assertFalse(at_least)                       # lo que no se entiende cuenta como anterior
        for v in ("0.15.0rc1", "0.15", "v0.15.0", "", "?", "x" * 5000, "0.15.0\x00", b"0.15.0", 15, None):
            self.assertFalse(hp.version_at_least(v, hp.NAMES_SINCE), repr(v)[:30])
        self.assertTrue(hp.version_at_least("0.15.0", "0.15.0"))
        self.assertTrue(hp.version_at_least("1.0.0", "0.15.0"))
        self.assertFalse(hp.version_at_least("0.9.99", "0.15.0"))
        self.assertFalse(hp.version_at_least("0.15.0-dev", "0.15.0"))
        self.assertTrue(hp.version_at_least("0.15.1-dev", "0.15.0"))
        self.assertEqual(hp.version_key("0.15.0"), (0, 15, 0, 1))

    def test_version_de_5000_caracteres_es_rapida(self):
        from agmen import hubproto as hp

        t0 = time.perf_counter()
        for v in ("1" * 5000, "0.15.0-" + "-" * 5000, "x" * 5000, "1." * 2500):
            hp.version_key(v)
            hp.version_at_least(v, hp.NAMES_SINCE)
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_FALLA_REAL_el_texto_del_aviso_no_tiene_caracteres_de_control(self):
        """FALLA REAL. old_hub_text(version="0.14.0\\n") copia la versión tal cual (version_key hace strip() y la
        entiende, pero el texto usa la original): salen \\n, \\r, \\x0b, \\x1c, \\x85 en el aviso. Esperado: nada
        que no se pueda imprimir, como con las versiones que no se entienden (_clip).
        Reproducir: python -m unittest test_run.TestVersionesRaras.test_FALLA_REAL_el_texto_del_aviso_no_tiene_"""
        "caracteres_de_control"
        from agmen.run import old_hub_text

        self.propia("0.15.0")
        # Se entienden después de quitar espacios (str.strip), y entonces el aviso las copia tal cual.
        for v in ("0.14.0\n", "0.14.0\r\n", "\x0b0.14.0", "0.14.0\x1c", "0.14.0-dev\n", "0.14.0\x85"):
            with self.subTest(v=repr(v)):
                t = old_hub_text(v, "x tiene", "y")
                self.assertIsNotNone(t)
                self.assertEqual([c for c in t if not c.isprintable()], [], repr(t))
        for v in ("0.14.0\x1b[2J", "\x00", "a\nb", "0.15.0\x00"):         # y las que no se entienden
            t = old_hub_text(v, "x tiene", "y")
            self.assertEqual([c for c in t if not c.isprintable()], [], repr(t))

    def test_FALLA_REAL_el_texto_del_aviso_no_crece_sin_tope(self):
        """FALLA REAL. Una versión que sí se entiende pero trae un sufijo de 5000 letras ("0.14.0-aaaa…") se copia
        entera al aviso (5272 caracteres). Las que no se entienden sí se recortan a 40 (_clip). Esperado: tope.
        Reproducir: python -m unittest test_run.TestVersionesRaras.test_FALLA_REAL_el_texto_del_aviso_no_crece_"""
        "sin_tope"
        from agmen.run import old_hub_text

        self.propia("0.15.0")
        for v in ("x" * 5000, "0.14.0-" + "a" * 5000, "0.14." + "9" * 5000, "?" * 100_000):
            with self.subTest(v=v[:20]):
                t = old_hub_text(v, "x tiene", "y")
                self.assertLess(len(t), 1000, f"el aviso mide {len(t)} caracteres con una versión de {len(v)}")

    def test_FALLA_REAL_aviso_en_connect_con_version_enorme_o_con_controles(self):
        """FALLA REAL (la misma causa, por el camino de una [[machine]]: cli._connect). Solo falla con "0.14.0-dev\\n";
        con 5000 'x', con escapes y con vacío el aviso sale limpio.
        Reproducir: python -m unittest test_run.TestVersionesRaras.test_FALLA_REAL_aviso_en_connect_con_version_"""
        "enorme_o_con_controles"
        from types import SimpleNamespace
        from unittest import mock

        from agmen import agent, cli

        class Enlace:
            def __init__(self, name, version):
                self.name, self.version, self.shown, self.address = name, version, "10.0.0.5:7391", None
                self.waiting, self.on_lost = False, None

            def watch_clock(self, clock):
                pass

            def open(self, key, msg, on_key_ok=None, stop=None):
                pass

            def close(self):
                pass

        self.propia("0.15.0")
        con = self.load(MINIMO + joints())
        for version in ("0.14.0-dev\n", "x" * 5000, "0.14.0\x1b[2J", ""):
            with self.subTest(version=repr(version)[:30]):
                dicho: list[str] = []
                launcher = SimpleNamespace(say=lambda t, k="agmen": dicho.append(t))
                with mock.patch.object(agent, "session_message", lambda *a, **k: {}):
                    cli._connect(launcher, con, {"robot": Enlace("robot", version)}, {"robot": ("k", "variable")},
                                 "real", [], threading.Event())
                avisos = [t for t in dicho if "anterior a 0.15.0" in t]
                self.assertEqual(len(avisos), 1, dicho)
                self.assertEqual([c for c in avisos[0] if not c.isprintable()], [], repr(avisos[0][:120]))
                self.assertLess(len(avisos[0]), 1000, len(avisos[0]))

    def test_una_vez_por_computadora_y_ni_una_sin_joints(self):
        from types import SimpleNamespace
        from unittest import mock

        from agmen import agent, cli

        class Enlace:
            def __init__(self, name, version):
                self.name, self.version, self.shown, self.address = name, version, "10.0.0.5:7391", None
                self.waiting, self.on_lost = False, None

            def watch_clock(self, clock):
                pass

            def open(self, key, msg, on_key_ok=None, stop=None):
                pass

            def close(self):
                pass

        self.propia("0.15.0")
        con, sin = self.load(MINIMO + joints()), self.load(MINIMO)
        for cfg, esperadas in ((con, 3), (sin, 0)):
            dicho: list[str] = []
            launcher = SimpleNamespace(say=lambda t, k="agmen": dicho.append(t))
            links = {n: Enlace(n, "0.14.0") for n in ("a", "b", "c")}
            with mock.patch.object(agent, "session_message", lambda *a, **k: {}):
                cli._connect(launcher, cfg, links, {n: ("k", "variable") for n in links}, "real", [],
                             threading.Event())
            avisos = [t for t in dicho if "anterior a 0.15.0" in t]
            self.assertEqual(len(avisos), esperadas, dicho)
            self.assertEqual(len({a.split(" ")[1] for a in avisos}), esperadas)    # una por nombre de computadora

    def test_FALLA_REAL_hub_local_viejo_con_version_rara_o_enorme(self):
        """FALLA REAL (la misma causa, por el camino del hub local que ya corría: prepare_hub). Solo falla con
        "0.14.0-dev\\n". Reproducir: python -m unittest test_run.TestVersionesRaras.test_FALLA_REAL_hub_local_viejo_"""
        "con_version_rara_o_enorme"
        from agmen.run import prepare_hub

        self.propia("0.15.0")
        con = self.load(MINIMO + joints())
        for v in ("", "?", "x" * 5000, "0.14.0\x1b[2J", "0.14.0-dev\n"):
            with self.subTest(v=repr(v)[:30]):
                cfg, nota = prepare_hub(con, ping=lambda a, v=v: v)
                avisos = [w for w in cfg.warnings if "anterior a 0.15.0" in w]
                self.assertEqual(len(avisos), 1)
                self.assertEqual([c for c in avisos[0] if not c.isprintable()], [], repr(avisos[0][:120]))
                self.assertLess(len(avisos[0]), 1000, len(avisos[0]))

    def test_la_version_propia_en_0_15_0_con_hub_en_0_14_0_avisa_y_al_reves_no(self):
        from agmen.run import old_hub_text

        self.propia("0.15.0")
        self.assertIsNotNone(old_hub_text("0.14.0", "x tiene", "y"))
        self.assertIsNone(old_hub_text("0.15.0", "x tiene", "y"))
        self.propia("0.14.0")
        self.assertIsNone(old_hub_text("0.15.0", "x tiene", "y"))          # al revés: nada
        self.assertIsNone(old_hub_text("0.14.0", "x tiene", "y"))
        self.propia("garbage")
        self.assertIsNone(old_hub_text("0.1.0", "x tiene", "y"))           # la propia no se entiende: no se avisa


if __name__ == "__main__":
    unittest.main()

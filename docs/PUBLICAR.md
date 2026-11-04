# Cómo apartar el nombre "Agmen" y publicar la primera versión

Un nombre se aparta **publicando algo que funcione**, no registrando
paquetes vacíos. En PyPI, los paquetes vacíos que solo reservan un nombre se
consideran *name squatting* (PEP 541) y se los pueden quitar a su dueño. La
versión 0.1.0 de Agmen ya funciona, así que es el momento de publicarla.

## Estado del nombre (revisado el 2026-10-30)

| Lugar | Estado | Qué hacer |
|---|---|---|
| **PyPI** (`pip install agmen`) | ✅ Libre | Publicar 0.1.0 (pasos 1–4) |
| **GitHub** | ❓ No se pudo revisar | Revisar `github.com/agmen`; si está libre, crear una organización |
| **npm** (`agmen`) | ❌ Tomado (otra persona, "coming soon", proyecto de agentes de programación) | Usar el scope `@agmen/…` (crear organización npm `agmen`) o `agmen-web` cuando exista el decodificador en JavaScript (nivel 3) |
| Dominio (`agmen.dev`, `agmen.org`…) | ❓ Sin revisar | Opcional |
| Marca registrada (IMPI en México) | ❓ Sin revisar | Opcional; consultar a alguien que sepa de propiedad intelectual si el proyecto crece |

## Paso 1 — Repositorio en GitHub

1. Revisa si `https://github.com/agmen` está libre. Si lo está, crea una
   **organización** gratuita llamada `agmen` (*Your organizations → New
   organization*). Si no, usa tu usuario (`github.com/flkochu/Agmen`).
2. Crea el repositorio `agmen` (público) y sube el código:
   ```bash
   cd agmen
   git remote add origin https://github.com/<DUEÑO>/agmen.git
   git push -u origin main
   ```
3. Si el dueño no es `flkochu`, cambia las URLs en `pyproject.toml` y en
   `CHANGELOG.md`.
4. Revisa la pestaña **Actions**: el CI debe salir en verde en Windows, Linux
   y macOS.

## Paso 2 — Cuenta de PyPI

1. Crea una cuenta en <https://pypi.org/account/register/> y activa la
   verificación en dos pasos (2FA). Es obligatoria para publicar.

## Paso 3 — "Trusted Publisher" pendiente en PyPI

Permite que GitHub Actions publique sin contraseñas ni tokens.

1. En PyPI: menú de tu cuenta → **Publishing** → **GitHub**.
2. Llena el formulario:

   | Campo | Valor |
   |---|---|
   | PyPI Project Name | `agmen` |
   | Owner | tu usuario u organización de GitHub |
   | Repository name | `agmen` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

3. En GitHub: *Settings → Environments → New environment* → `pypi`.

> ⚠ El publicador pendiente **no aparta el nombre**. El proyecto se crea con
> la primera publicación; si alguien más registra `agmen` antes, el
> publicador pendiente se invalida. Por eso conviene hacer el paso 4 el
> mismo día.

## Paso 4 — Publicar 0.1.0

```bash
python scripts/check_version.py v0.1.0     # debe decir OK
git tag v0.1.0
git push origin v0.1.0
```

En la pestaña **Actions** vas a ver el workflow **Release**:
verificar → pruebas en todos los sistemas → wheels (desde 0.9.0: una compilada
por sistema con cibuildwheel, más la de Python puro y el sdist; cada una se
instala y se prueba con `scripts/wheel_check.py`) → PyPI → Release de GitHub.
Las wheels tardan más que las pruebas (~10–20 min): las de macOS y Windows
son las más lentas.
Al terminar:

```bash
pip install agmen
python -c "import agmen; print(agmen.__version__)"   # 0.1.0
```

## Después

- Cada nivel terminado es una versión nueva (`docs/VERSIONES.md`). Publicar
  seguido mantiene el proyecto "vivo", y eso también protege el nombre.
- Cuando exista el decodificador en JavaScript (nivel 3), publícalo en npm
  como `@agmen/web` (o `agmen-web`).
- Paquetes de C++ (vcpkg, Conan): cuando la API esté más estable (cerca de la
  1.0), porque esos registros piden proyectos con uso real.

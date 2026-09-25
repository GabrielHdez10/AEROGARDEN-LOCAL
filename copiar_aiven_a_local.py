# ============================================================
# COPIAR AIVEN A LOCAL — 25/09/2026 (v2: compatible con sql_mode ANSI de Aiven)
# Copia la base de datos de Aiven (nube) a MySQL local, con la
# MISMA estructura y los MISMOS IDs (usuarios, dispositivo 6,
# sensores, lecturas, alertas...).
#
# Seguridad:
#   - En Aiven SOLO LEE (nunca borra ni modifica nada allá).
#   - En local SOLO toca la base "mydb". base_cjv no se toca.
#   - Si la "mydb" local tiene datos reales, se detiene sin borrar.
#
# Uso (en la carpeta AEROGARDEN, con el servidor Flask detenido):
#   python copiar_aiven_a_local.py
# ============================================================

import os
import getpass
import mysql.connector
from dotenv import load_dotenv

# ── Opciones ─────────────────────────────────────────────────
# Aiven guarda las horas en UTC (6 h adelante de Xalapa).
# True  = resta 6 h a lecturas y alertas copiadas (todo en hora local).
# False = las copia tal cual (quedan en UTC).
AJUSTAR_HORAS = True
HORAS_DIFERENCIA = 6

BASE_LOCAL = "mydb"                     # única base local que se toca
TABLAS_SEMILLA = {"tipo_cultivo", "sistema"}  # pueden traer filas de ejemplo
TAMANO_LOTE = 1000
# ─────────────────────────────────────────────────────────────

BASEDIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASEDIR, ".env"))


def conectar_aiven():
    host = os.environ.get("DB_HOST", "").strip()
    if host in ("", "127.0.0.1", "localhost"):
        raise SystemExit(
            "ALTO: tu .env ya apunta a la base LOCAL (DB_HOST=%s).\n"
            "Regresa temporalmente el bloque de base de datos de Aiven\n"
            "en el .env, corre este script y después cámbialo a local." % host
        )
    config = dict(
        host=host,
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", ""),
        password=os.environ.get("DB_PASS", ""),
        database=os.environ.get("DB_NAME", "mydb"),
        charset="utf8mb4",
    )
    ca = os.environ.get("DB_SSL_CA", "").strip()
    if not ca:
        ca_defecto = os.path.join(BASEDIR, "certs", "aiven-ca.pem")
        if os.path.exists(ca_defecto):
            ca = ca_defecto
    if ca:
        config["ssl_ca"] = ca
    print("Conectando a Aiven (%s)..." % host)
    return mysql.connector.connect(**config)


def conectar_local(password):
    print("Conectando a MySQL local (127.0.0.1)...")
    return mysql.connector.connect(
        host="127.0.0.1", port=3306, user="root",
        password=password, charset="utf8mb4",
    )


def revisar_local_sin_datos(cur_local):
    """Se detiene si la mydb local ya tiene datos reales."""
    cur_local.execute(
        "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
        (BASE_LOCAL,),
    )
    if cur_local.fetchone()[0] == 0:
        return
    cur_local.execute(
        "SELECT TABLE_NAME FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'",
        (BASE_LOCAL,),
    )
    tablas = [fila[0] for fila in cur_local.fetchall()]
    con_datos = []
    for t in tablas:
        if t in TABLAS_SEMILLA:
            continue
        cur_local.execute("SELECT COUNT(*) FROM `%s`.`%s`" % (BASE_LOCAL, t))
        n = cur_local.fetchone()[0]
        if n > 0:
            con_datos.append("%s (%d filas)" % (t, n))
    if con_datos:
        raise SystemExit(
            "ALTO: la base local '%s' ya tiene datos:\n  %s\n"
            "No se borró nada. Revisa esos datos antes de continuar."
            % (BASE_LOCAL, "\n  ".join(con_datos))
        )


def main():
    print("=" * 60)
    print("COPIAR AIVEN -> LOCAL (base '%s')" % BASE_LOCAL)
    print("=" * 60)

    aiven = conectar_aiven()
    cur_aiven = aiven.cursor(buffered=True)
    base_aiven = aiven.database
    # Aiven usa sql_mode ANSI: SHOW CREATE TABLE sale con comillas
    # dobles ("tabla") y tu MySQL local no las entiende. En ESTA
    # sesión (solo lectura) se usa el modo normal, con `backticks`.
    cur_aiven.execute("SET SESSION sql_mode = 'NO_ENGINE_SUBSTITUTION'")
    cur_aiven.execute("SET time_zone = '+00:00'")

    pwd = getpass.getpass("Contraseña de root de tu MySQL local: ")
    local = conectar_local(pwd)
    cur_local = local.cursor(buffered=True)
    cur_local.execute("SET time_zone = '+00:00'")

    # 1) Seguridad: no pisar datos locales reales
    revisar_local_sin_datos(cur_local)

    # 2) Tablas de Aiven
    cur_aiven.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
    tablas = [fila[0] for fila in cur_aiven.fetchall()]
    print("Tablas en Aiven: %d -> %s" % (len(tablas), ", ".join(tablas)))

    cur_aiven.execute(
        "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
        "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
        (base_aiven,),
    )
    charset, collation = cur_aiven.fetchone()

    # 3) Recrear la base local con la estructura EXACTA de Aiven
    print("Recreando la base local '%s'..." % BASE_LOCAL)
    cur_local.execute("DROP DATABASE IF EXISTS `%s`" % BASE_LOCAL)
    cur_local.execute(
        "CREATE DATABASE `%s` CHARACTER SET %s COLLATE %s"
        % (BASE_LOCAL, charset, collation)
    )
    cur_local.execute("USE `%s`" % BASE_LOCAL)
    cur_local.execute("SET FOREIGN_KEY_CHECKS = 0")

    for t in tablas:
        cur_aiven.execute("SHOW CREATE TABLE `%s`" % t)
        ddl = cur_aiven.fetchone()[1]
        cur_local.execute(ddl)
    print("Estructura creada.")

    # 4) Copiar filas conservando los IDs
    conteos = {}
    for t in tablas:
        cur_lectura = aiven.cursor()
        cur_lectura.execute("SELECT * FROM `%s`" % t)
        columnas = [d[0] for d in cur_lectura.description]
        lista_cols = ", ".join("`%s`" % c for c in columnas)
        marcas = ", ".join(["%s"] * len(columnas))
        sql_insert = "INSERT INTO `%s` (%s) VALUES (%s)" % (t, lista_cols, marcas)

        total = 0
        while True:
            lote = cur_lectura.fetchmany(TAMANO_LOTE)
            if not lote:
                break
            cur_local.executemany(sql_insert, lote)
            total += len(lote)
        cur_lectura.close()
        conteos[t] = total
        print("  %-24s %8d filas" % (t, total))

    # 5) Ajuste de horas (opcional)
    if AJUSTAR_HORAS:
        print("Ajustando horas (-%d h) en lecturas y alertas..." % HORAS_DIFERENCIA)
        if "registro_sensores" in tablas:
            cur_local.execute(
                "UPDATE registro_sensores "
                "SET fecha_hora = fecha_hora - INTERVAL %d HOUR" % HORAS_DIFERENCIA
            )
        if "historial_alertas" in tablas:
            cur_local.execute(
                "UPDATE historial_alertas "
                "SET fecha_hora = fecha_hora - INTERVAL %d HOUR" % HORAS_DIFERENCIA
            )
            cur_local.execute(
                "UPDATE historial_alertas "
                "SET fecha_resolucion = fecha_resolucion - INTERVAL %d HOUR "
                "WHERE fecha_resolucion IS NOT NULL" % HORAS_DIFERENCIA
            )

    cur_local.execute("SET FOREIGN_KEY_CHECKS = 1")
    local.commit()

    # 6) Verificación: mismos conteos en ambos lados
    print("-" * 60)
    print("%-24s %10s %10s" % ("Tabla", "Aiven", "Local"))
    todo_bien = True
    for t in tablas:
        cur_aiven.execute("SELECT COUNT(*) FROM `%s`" % t)
        n_aiven = cur_aiven.fetchone()[0]
        cur_local.execute("SELECT COUNT(*) FROM `%s`" % t)
        n_local = cur_local.fetchone()[0]
        marca = "" if n_aiven == n_local else "  <-- DIFERENTE"
        if marca:
            todo_bien = False
        print("%-24s %10d %10d%s" % (t, n_aiven, n_local, marca))
    print("-" * 60)

    cur_aiven.close()
    aiven.close()
    cur_local.close()
    local.close()

    if todo_bien:
        print("LISTO. La copia coincide tabla por tabla.")
        print("Aiven quedó intacta como respaldo.")
    else:
        print("ATENCIÓN: hay tablas con conteos distintos (ver arriba).")
        print("Si el servidor Flask seguía corriendo, pudieron entrar")
        print("lecturas nuevas a Aiven durante la copia.")


if __name__ == "__main__":
    main()
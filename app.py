# HYDROSENSE - RANGOS INTEGRADOS - 29/09/2026
from flask import Flask, jsonify, request, render_template, render_template_string, session, redirect, url_for
from functools import wraps
from contextlib import contextmanager
import math
import unicodedata
from flask_cors import CORS
from dotenv import load_dotenv
import mysql.connector
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import threading
import os
import random
import string
import smtplib
from email.mime.text import MIMEText
import tempfile
import hashlib
import json

load_dotenv()

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

app.secret_key = os.environ.get("SECRET_KEY", "ag_s3cr3t_2024_xK9")

# ── Certificado SSL para bases de datos en la nube (Aiven, PlanetScale, etc.) ──
# Ruta portable: por defecto busca certs/aiven-ca.pem DENTRO del proyecto,
# así funciona igual en cualquier máquina sin rutas absolutas hardcodeadas.
# Se puede sobreescribir con DB_SSL_CA en el .env si alguien quiere otra ubicación.
BASEDIR             = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SSL_CA_PATH = os.path.join(BASEDIR, "certs", "aiven-ca.pem")
_DB_SSL_CA_ORIGEN   = os.environ.get("DB_SSL_CA", "").strip() or (
    DEFAULT_SSL_CA_PATH if os.path.exists(DEFAULT_SSL_CA_PATH) else ""
)


def _preparar_certificado_ssl(ruta_original):
    """
    Copia el certificado a la carpeta temporal del sistema (fuera de
    OneDrive/Google Drive) y usa esa copia. Carpetas sincronizadas a veces
    bloquean el archivo original de forma intermitente (funciona una vez,
    falla la siguiente) mientras el cliente de sincronización lo toca;
    la copia local evita ese problema.
    """
    if not ruta_original or not os.path.exists(ruta_original):
        return ""
    try:
        destino = os.path.join(tempfile.gettempdir(), "aerogarden_aiven_ca.pem")
        with open(ruta_original, "rb") as f_origen:
            contenido = f_origen.read()
        with open(destino, "wb") as f_destino:
            f_destino.write(contenido)
        return destino
    except Exception as e:
        print("⚠️ No se pudo copiar el certificado SSL a una ruta temporal, usando el original:", e)
        return ruta_original


DB_SSL_CA = _preparar_certificado_ssl(_DB_SSL_CA_ORIGEN)

# ── Configuración de correo (SMTP genérico: Brevo, SendGrid, etc.) ──
EMAIL_HOST          = os.environ.get("EMAIL_HOST", "smtp-relay.brevo.com")
EMAIL_PORT          = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS       = os.environ.get("EMAIL_USE_TLS", "true").lower() == "true"
EMAIL_HOST_USER     = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL  = os.environ.get("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER)
CODIGO_EXP_MINUTOS  = int(os.environ.get("CODIGO_EXP_MINUTOS", "15"))


def generar_codigo(n=6):
    return ''.join(random.choices(string.digits, k=n))


def enviar_correo_codigo(destino, codigo, proposito):
    """
    Envía el código de verificación por correo (registro o restablecimiento).
    Si no hay credenciales SMTP configuradas, no detiene el flujo: solo lo
    registra en consola (útil en desarrollo local).
    """
    if not EMAIL_HOST_USER or not EMAIL_HOST_PASSWORD:
        print(f"⚠️ EMAIL_HOST_USER/EMAIL_HOST_PASSWORD no configurados. "
              f"Código para {destino} ({proposito}): {codigo}")
        return False

    if proposito == 'reset':
        asunto = "Restablece tu contraseña - AeroGarden"
        motivo = "Recibimos una solicitud para restablecer la contraseña de tu cuenta."
    else:
        asunto = "Verifica tu correo - AeroGarden"
        motivo = "Gracias por registrarte en AeroGarden."

    cuerpo = (
        f"Hola 👋\n\n{motivo}\n\n"
        f"Tu código de verificación es: {codigo}\n\n"
        f"Este código vence en {CODIGO_EXP_MINUTOS} minutos.\n\n"
        f"Si tú no solicitaste esto, ignora este mensaje.\n\n"
        f"AeroGarden"
    )

    try:
        msg = MIMEText(cuerpo, "plain", "utf-8")
        msg["Subject"] = asunto
        msg["From"]    = DEFAULT_FROM_EMAIL
        msg["To"]      = destino

        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=15) as server:
            if EMAIL_USE_TLS:
                server.starttls()
            server.login(EMAIL_HOST_USER, EMAIL_HOST_PASSWORD)
            server.sendmail(DEFAULT_FROM_EMAIL, [destino], msg.as_string())
        return True
    except Exception as e:
        print("❌ Error enviando correo:", str(e))
        return False


def crear_codigo_verificacion(cursor, correo, proposito):
    """Invalida códigos previos no usados y crea uno nuevo para (correo, proposito)."""
    cursor.execute(
        "UPDATE verificaciones_email SET usado=1 WHERE correo=%s AND proposito=%s AND usado=0",
        (correo, proposito)
    )
    codigo = generar_codigo(6)
    ahora  = datetime.now()
    expira = ahora + timedelta(minutes=CODIGO_EXP_MINUTOS)
    cursor.execute(
        """INSERT INTO verificaciones_email (correo, codigo, proposito, usado, verificado, creado_en, expira_en)
           VALUES (%s,%s,%s,0,0,%s,%s)""",
        (correo, codigo, proposito, ahora, expira)
    )
    return codigo


def conectar_bd():
    config = dict(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASS", ""),
        database=os.environ.get("DB_NAME", "mydb"),
        charset="utf8mb4"
    )
    if DB_SSL_CA:
        config["ssl_ca"] = DB_SSL_CA
        config["ssl_verify_cert"] = True
    return mysql.connector.connect(**config)


def login_requerido(f):
    @wraps(f)
    def verificar(*args, **kwargs):
        if 'usuario_logueado' not in session:
            if request.path.startswith('/api/'):
                return jsonify({"error": "No autorizado"}), 401
            return redirect(url_for('home'))
        return f(*args, **kwargs)
    return verificar


def get_id_usuario():
    correo = session.get('usuario_logueado')
    if not correo:
        return None
    try:
        cx  = conectar_bd()
        cur = cx.cursor()
        cur.execute("SELECT idUsuario FROM usuarios WHERE correo = %s", (correo,))
        row = cur.fetchone()
        cur.close()
        cx.close()
        return row[0] if row else None
    except:
        return None


relay_configs: dict = {}   
relay_lock = threading.Lock()

wifi_pendiente: dict = {}  
wifi_actual:    dict = {}  
wifi_historial: dict = {}  
wifi_lock = threading.Lock()


def _relay_default():
    return {"tiempo_on": 30, "tiempo_off": 60,
            "modo": "automatico", "estado_manual": "apagado"}


def get_relay(device_id: int) -> dict:
    with relay_lock:
        if device_id in relay_configs:
            return relay_configs[device_id].copy()
    try:
        cx  = conectar_bd()
        cur = cx.cursor(dictionary=True)
        cur.execute("""
            SELECT tiempo_on, tiempo_off, modo, estado_manual
            FROM config_relay
            WHERE idDispositivo = %s
            LIMIT 1
        """, (device_id,))
        row = cur.fetchone()
        cur.close(); cx.close()
        if row:
            config = {
                "tiempo_on":     row["tiempo_on"],
                "tiempo_off":    row["tiempo_off"],
                "modo":          row["modo"],
                "estado_manual": row["estado_manual"],
            }
        else:
            config = _relay_default()
    except Exception as e:
        print(f"[RELAY get_relay] Error BD: {e} — usando defaults")
        config = _relay_default()
    with relay_lock:
        relay_configs[device_id] = config
    return config.copy()


# Validacion y guardado de la configuracion de riego - 21/10/2026.
# Estos limites coinciden con los que acepta el Mega; no son rangos agronomicos.
def _relay_validar_entero(valor, nombre, minimo=1, maximo=3600):
    """Acepta enteros y cadenas de enteros, sin truncar decimales ni booleanos."""
    if type(valor) is int:
        numero = valor
    elif isinstance(valor, str):
        texto = valor.strip()
        if not texto or not texto.isascii() or not texto.isdigit():
            raise ValueError(f"{nombre} debe ser un numero entero.")
        numero = int(texto)
    else:
        raise ValueError(f"{nombre} debe ser un numero entero.")
    if not minimo <= numero <= maximo:
        raise ValueError(f"{nombre} debe estar entre {minimo} y {maximo}.")
    return numero


def _relay_validar_config(data):
    """Valida las cuatro claves actuales y conserva los valores predeterminados."""
    if not isinstance(data, dict):
        raise ValueError("Envia un objeto JSON con la configuracion de la bomba.")
    predeterminada = _relay_default()
    config = {
        "tiempo_on": _relay_validar_entero(
            data.get("tiempo_on", predeterminada["tiempo_on"]), "tiempo_on"),
        "tiempo_off": _relay_validar_entero(
            data.get("tiempo_off", predeterminada["tiempo_off"]), "tiempo_off"),
        "modo": data.get("modo", predeterminada["modo"]),
        "estado_manual": data.get("estado_manual", predeterminada["estado_manual"]),
    }
    if config["modo"] not in ("manual", "automatico"):
        raise ValueError("modo debe ser 'manual' o 'automatico'.")
    if config["estado_manual"] not in ("encendido", "apagado"):
        raise ValueError("estado_manual debe ser 'encendido' o 'apagado'.")
    return config


def _relay_cerrar_recursos(cursor, conexion):
    """Intenta cerrar ambos recursos, incluso si el cierre del primero falla."""
    for nombre, recurso in (("cursor", cursor), ("conexion", conexion)):
        if recurso is not None:
            try:
                recurso.close()
            except Exception:
                # Un fallo de cierre posterior a commit no deshace un guardado confirmado.
                app.logger.warning("No se pudo cerrar %s del relevador", nombre, exc_info=True)


def set_relay(device_id: int, data: dict, id_usuario=None):
    device_id = _relay_validar_entero(device_id, "device_id", maximo=2147483647)
    config = _relay_validar_config(data)
    cx = cur = None
    try:
        cx = conectar_bd()
        cur = cx.cursor()
        cur.execute("""
            INSERT INTO config_relay (idDispositivo, tiempo_on, tiempo_off, modo, estado_manual)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                tiempo_on     = VALUES(tiempo_on),
                tiempo_off    = VALUES(tiempo_off),
                modo          = VALUES(modo),
                estado_manual = VALUES(estado_manual)
        """, (device_id, config["tiempo_on"], config["tiempo_off"],
              config["modo"], config["estado_manual"]))
        # Misma transaccion: una configuracion fallida nunca deja un evento guardado.
        _control_registrar_en_cursor(
            cur, device_id, 'config_guardada', config,
            {'origen': 'usuario', 'motivo': 'Configuracion guardada desde el sistema web.',
             'detalle': {}}, id_usuario=id_usuario)
        cx.commit()
    except Exception:
        if cx is not None:
            try:
                cx.rollback()
            except Exception:
                app.logger.warning("No se pudo revertir la transaccion del relevador", exc_info=True)
        # El llamador debe conocer el fallo; no actualizar la configuracion en memoria.
        raise
    finally:
        _relay_cerrar_recursos(cur, cx)
    # Solo publicar en memoria despues de que MySQL confirme commit.
    with relay_lock:
        relay_configs[device_id] = config.copy()
    return config.copy()


def convertir_valor(raw, tipo_conv):
    if isinstance(raw, bool) or raw is None:
        raise ValueError("Lectura no numerica")
    v = float(raw)
    if not math.isfinite(v):
        raise ValueError("La lectura debe ser un numero finito")
    if tipo_conv in ('ph', 'luz') and not 0 <= v <= 1023:
        raise ValueError("Lectura analogica fuera de 0-1023")
    if tipo_conv == "ph":
        voltaje = v * (5.0 / 1023.0)
        ph = 7.0 + ((2.5 - voltaje) / 0.18)
        return round(max(0.0, min(14.0, ph)), 2)
    elif tipo_conv == "luz":
        return round((v / 1023.0) * 5000, 1)
    return round(v, 2)


def guardar_lectura_bd(id_sensor, valor):
    conexion = cursor = None
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
        # Serializa lecturas del mismo sensor, incluso con varios procesos Flask.
        cursor.execute("SELECT idSensore FROM sensores WHERE idSensore=%s FOR UPDATE", (id_sensor,))
        if not cursor.fetchone():
            raise ValueError("Sensor no encontrado")
        cursor.execute(
            "INSERT INTO registro_sensores (idSensor, valor, fecha_hora) VALUES (%s, %s, NOW())",
            (id_sensor, valor)
        )
        cursor.execute("""
            SELECT idParametro, nombre, condicion, valor_umbral, prioridad
            FROM parametros_alerta
            WHERE idSensor = %s AND activo = 1
        """, (id_sensor,))
        for p in cursor.fetchall():
            umbral  = float(p['valor_umbral'])
            cond    = p['condicion']
            disparo = cumple_condicion(valor, cond, umbral)
            if disparo:
                cursor.execute("""
                    SELECT idHistorial FROM historial_alertas
                    WHERE idParametro=%s AND estado IN ('nueva', 'vista')
                    ORDER BY idHistorial LIMIT 1
                """, (p['idParametro'],))
                if cursor.fetchone():
                    continue
                msg = f"Sensor {id_sensor}: valor {valor} {cond.replace('_',' ')} umbral {umbral}"
                cursor.execute("""
                    INSERT INTO historial_alertas
                        (idParametro, idSensor, valor_detectado, prioridad, estado, mensaje)
                    VALUES (%s, %s, %s, %s, 'nueva', %s)
                """, (p['idParametro'], id_sensor, valor, p['prioridad'], msg))
            else:
                resolver_incidentes(cursor, p['idParametro'])
        conexion.commit()
    except Exception:
        if conexion is not None:
            conexion.rollback()
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if conexion is not None:
            conexion.close()


ARDUINO_TOKEN = os.environ.get("ARDUINO_TOKEN", "ag_hw_tk_8f2a")

TIPO_CONV_MAP = {
    "temperatura": None,
    "humedad":     None,
    "ph":          "ph",
    "luz":         "luz",
    "nivel agua":  None,
    "distancia":   None,
    "ec":          None,  # El firmware debe enviar EC calibrada en la unidad registrada.
    "temperatura_agua": None,
}


@app.route('/api/arduino/registrar', methods=['POST'])
def arduino_registrar():
    data = request.json or {}
    if data.get("token") != ARDUINO_TOKEN:
        return jsonify({"error": "No autorizado"}), 401

    nombre = data.get("nombre", "Arduino").strip() or "Arduino"

    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)

        cursor.execute(
            "SELECT idDispositivo FROM dispositivos WHERE nombre=%s AND idUsuario IS NULL LIMIT 1",
            (nombre,)
        )
        existente = cursor.fetchone()

        if existente:
            device_id = existente['idDispositivo']
        else:
            cursor.execute(
                "INSERT INTO dispositivos (nombre, tipo, idSistema, idUsuario) VALUES (%s, %s, 1, NULL)",
                (nombre, "Arduino Mega + ESP-01")
            )
            conexion.commit()
            device_id = cursor.lastrowid

        cursor.close()
        conexion.close()
        print(f"[ARDUINO REGISTRAR] nombre='{nombre}' → device_id={device_id}")
        return jsonify({"status": "ok", "device_id": device_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/arduino/datos', methods=['POST'])
def arduino_datos():
    data = request.json or {}
    if data.get("token") != ARDUINO_TOKEN:
        return jsonify({"error": "No autorizado"}), 401

    device_id = data.get("device_id")
    sensores  = data.get("sensores", {})

    if not device_id:
        return jsonify({"error": "Falta device_id"}), 400
    if not sensores:
        return jsonify({"error": "Sin datos de sensores"}), 400

    guardados = []
    errores   = []

    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)

        for tipo_raw, valor_raw in sensores.items():
            tipo = tipo_raw.lower().strip()
            cursor.execute("""
                SELECT s.idSensore
                FROM sensores s
                WHERE s.idDispositivo = %s
                  AND LOWER(s.tipo_sensor) = %s
                LIMIT 1
            """, (device_id, tipo))
            row = cursor.fetchone()

            if not row:
                errores.append(f"Sensor '{tipo}' no registrado para dispositivo {device_id}")
                continue

            id_sensor = row['idSensore']
            conv      = TIPO_CONV_MAP.get(tipo, None)
            try:
                valor = convertir_valor(valor_raw, conv)
                guardar_lectura_bd(id_sensor, valor)
                guardados.append({"tipo": tipo, "id": id_sensor, "valor": valor})
                print(f"[ARDUINO dev={device_id}] {tipo}: {valor}")
            except Exception as e:
                errores.append(f"{tipo}: {e}")

        cursor.close()
        conexion.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"status": "ok", "guardados": guardados, "errores": errores})


@app.route('/api/arduino/config', methods=['GET'])
def arduino_config():
    if request.args.get("token") != ARDUINO_TOKEN:
        return jsonify({"error": "No autorizado"}), 401

    device_id = request.args.get("device_id", type=int)
    if not device_id:
        return jsonify({"error": "Falta device_id"}), 400

    relay, contexto = get_relay(device_id, incluir_contexto=True)
    # Registra la respuesta preparada, no una confirmacion de recepcion o ejecucion.
    _control_registrar_respuesta(device_id, relay, contexto)

    with wifi_lock:
        wifi = wifi_pendiente.pop(device_id, {"ssid": None, "password": None})

    respuesta = {"relay": relay, "wifi": wifi}
    if request.args.get('registro_riego') == '1':
        # Extension solicitada por el nuevo Mega. El firmware anterior recibe
        # el mismo contrato de configuracion que antes de esta actualizacion.
        respuesta['relay'] = dict(relay, config_version=_control_version(relay, contexto))
        transporte = _eventos_protocolo(device_id, request.args.get('arranque_id'))
        if transporte is not None:
            respuesta['riego_eventos'] = transporte
    return jsonify(respuesta)


@app.route('/api/arduino/emparejar', methods=['POST'])
def arduino_emparejar():
    data = request.json or {}
    if data.get("token") != ARDUINO_TOKEN:
        return jsonify({"error": "No autorizado"}), 401

    codigo = data.get("pairing_code", "").strip().upper()
    if not codigo:
        return jsonify({"error": "Falta pairing_code"}), 400

    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
        cursor.execute("""
            SELECT idDispositivo FROM dispositivos
            WHERE pairing_code = %s AND pairing_usado = 0
            LIMIT 1
        """, (codigo,))
        row = cursor.fetchone()

        if not row:
            cursor.close(); conexion.close()
            return jsonify({"error": "Código inválido o ya usado"}), 404

        device_id = row['idDispositivo']

        cursor.execute("""
            UPDATE dispositivos SET pairing_usado = 1 WHERE idDispositivo = %s
        """, (device_id,))
        conexion.commit()
        cursor.close(); conexion.close()

        print(f"[EMPAREJAR] device_id={device_id} emparejado con código {codigo}")
        return jsonify({"status": "ok", "device_id": device_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/')
def home():
    return render_template('login.html')

@app.route('/registro')
def pagina_registro():
    return render_template('registro.html')

@app.route('/dashboard')
@login_requerido
def dashboard():
    return render_template('dashboard.html')

@app.route('/logout')
def logout():
    session.pop('usuario_logueado', None)
    return redirect(url_for('home'))


@app.route('/api/auth/registrar', methods=['POST'])
def registrar_usuario():
    try:
        data               = request.json
        nombre             = data.get('nombre', '').strip()
        apellido_paterno   = data.get('apellido_paterno', '').strip()
        apellido_materno   = data.get('apellido_materno', '').strip()
        correo             = data.get('correo', '').strip().lower()
        contrasena         = data.get('contrasena')

        if not nombre or not apellido_paterno or not apellido_materno or not correo or not contrasena:
            return jsonify({"status": "error", "mensaje": "Datos incompletos"}), 400

        conexion = conectar_bd()
        cursor   = conexion.cursor()
        cursor.execute(
            "INSERT INTO `usuarios` (`nombre`, `apellido_paterno`, `apellido_materno`, `correo`, `contraseña`) VALUES (%s, %s, %s, %s, %s)",
            (nombre, apellido_paterno, apellido_materno, correo, generate_password_hash(contrasena))
        )
        conexion.commit()

        # Genera y envía el código de verificación de registro
        codigo = crear_codigo_verificacion(cursor, correo, 'registro')
        conexion.commit()
        cursor.close()
        conexion.close()

        enviar_correo_codigo(correo, codigo, 'registro')

        return jsonify({
            "status": "success",
            "mensaje": "Cuenta creada. Revisa tu correo para verificarla.",
            "requiere_verificacion": True,
            "correo": correo
        })
    except mysql.connector.Error as err:
        return jsonify({"status": "error", "mensaje": str(err)}), 500


@app.route('/api/usuario/actualizar_nombre', methods=['POST'])
@login_requerido
def actualizar_nombre():
    try:
        data             = request.json or {}
        nombre           = data.get('nombre', '').strip()
        apellido_paterno = data.get('apellido_paterno', '').strip()
        apellido_materno = data.get('apellido_materno', '').strip()

        if not nombre or not apellido_paterno or not apellido_materno:
            return jsonify({"status": "error", "mensaje": "Todos los campos son requeridos"}), 400

        conexion = conectar_bd()
        cursor   = conexion.cursor()
        cursor.execute(
            "UPDATE usuarios SET nombre=%s, apellido_paterno=%s, apellido_materno=%s WHERE correo=%s",
            (nombre, apellido_paterno, apellido_materno, session['usuario_logueado'])
        )
        conexion.commit()
        cursor.close()
        conexion.close()

        session['usuario_nombre'] = nombre
        return jsonify({"status": "success", "mensaje": "Nombre actualizado correctamente"})
    except Exception as e:
        return jsonify({"status": "error", "mensaje": str(e)}), 500


@app.route('/api/auth/verificar-correo', methods=['POST'])
def verificar_correo():
    """
    Paso 1 de 'olvidé mi contraseña': confirma que el correo existe y,
    si es así, dispara el envío del código de restablecimiento.
    No revela por sí mismo si el envío ocurrió, solo si el correo existe,
    para permitir avanzar al paso 2 en el frontend.
    """
    data   = request.json or {}
    correo = data.get('correo', '').strip().lower()
    if not correo:
        return jsonify({"existe": False, "mensaje": "Correo requerido"}), 400
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        cursor.execute("SELECT idUsuario FROM usuarios WHERE correo = %s", (correo,))
        row = cursor.fetchone()

        if not row:
            cursor.close(); conexion.close()
            return jsonify({"existe": False, "mensaje": "No existe una cuenta con ese correo"}), 404

        codigo = crear_codigo_verificacion(cursor, correo, 'reset')
        conexion.commit()
        cursor.close(); conexion.close()

        enviar_correo_codigo(correo, codigo, 'reset')
        return jsonify({"existe": True, "mensaje": "Te enviamos un código a tu correo."})
    except Exception as e:
        return jsonify({"existe": False, "mensaje": str(e)}), 500


@app.route('/api/auth/enviar-codigo', methods=['POST'])
def enviar_codigo():
    """Reenvía / genera un nuevo código de verificación (registro o reset)."""
    data       = request.json or {}
    correo     = data.get('correo', '').strip().lower()
    proposito  = data.get('proposito', 'registro').strip()

    if not correo:
        return jsonify({"status": "error", "mensaje": "Correo requerido"}), 400
    if proposito not in ('registro', 'reset'):
        return jsonify({"status": "error", "mensaje": "Propósito inválido"}), 400

    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        cursor.execute("SELECT idUsuario FROM usuarios WHERE correo = %s", (correo,))
        if not cursor.fetchone():
            cursor.close(); conexion.close()
            return jsonify({"status": "error", "mensaje": "No existe una cuenta con ese correo"}), 404

        codigo = crear_codigo_verificacion(cursor, correo, proposito)
        conexion.commit()
        cursor.close(); conexion.close()

        enviar_correo_codigo(correo, codigo, proposito)
        return jsonify({"status": "success", "mensaje": "Código enviado. Revisa tu correo."})
    except Exception as e:
        return jsonify({"status": "error", "mensaje": str(e)}), 500


@app.route('/api/auth/verificar-codigo', methods=['POST'])
def verificar_codigo():
    """
    Valida un código de 6 dígitos para 'registro' o 'reset'.
    - registro: marca el correo como verificado y deja al usuario logueado.
    - reset: solo marca el código como verificado (no lo consume aún);
      restablecer_password lo vuelve a validar antes de cambiar la contraseña.
    """
    data      = request.json or {}
    correo    = data.get('correo', '').strip().lower()
    codigo    = data.get('codigo', '').strip()
    proposito = data.get('proposito', 'registro').strip()

    if not correo or not codigo:
        return jsonify({"status": "error", "mensaje": "Correo y código son requeridos"}), 400

    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
        cursor.execute(
            """SELECT * FROM verificaciones_email
               WHERE correo=%s AND codigo=%s AND proposito=%s
               ORDER BY idVerificacion DESC LIMIT 1""",
            (correo, codigo, proposito)
        )
        fila = cursor.fetchone()

        if not fila:
            cursor.close(); conexion.close()
            return jsonify({"status": "error", "mensaje": "Código inválido."}), 400
        if fila['usado']:
            cursor.close(); conexion.close()
            return jsonify({"status": "error", "mensaje": "El código ya fue utilizado. Solicita uno nuevo."}), 400
        if datetime.now() >= fila['expira_en']:
            cursor.close(); conexion.close()
            return jsonify({"status": "error", "mensaje": "El código ha expirado. Solicita uno nuevo."}), 400

        if proposito == 'registro':
            cursor.execute(
                "UPDATE verificaciones_email SET usado=1, verificado=1 WHERE idVerificacion=%s",
                (fila['idVerificacion'],)
            )
            cursor.execute(
                "UPDATE usuarios SET email_verified=1, verified_at=%s WHERE correo=%s",
                (datetime.now(), correo)
            )
            cursor.execute("SELECT nombre FROM usuarios WHERE correo=%s", (correo,))
            usuario_row = cursor.fetchone()
            conexion.commit()
            cursor.close(); conexion.close()

            # Auto-login tras verificar el registro
            session['usuario_logueado'] = correo
            if usuario_row:
                session['usuario_nombre'] = usuario_row['nombre']

            return jsonify({"status": "success", "mensaje": "Correo verificado.", "logueado": True})

        else:  # reset: solo se marca como verificado, se consume al cambiar la contraseña
            cursor.execute(
                "UPDATE verificaciones_email SET verificado=1 WHERE idVerificacion=%s",
                (fila['idVerificacion'],)
            )
            conexion.commit()
            cursor.close(); conexion.close()
            return jsonify({"status": "success", "mensaje": "Código verificado."})

    except Exception as e:
        return jsonify({"status": "error", "mensaje": str(e)}), 500


@app.route('/api/auth/restablecer-password', methods=['POST'])
def restablecer_password():
    """
    Restablece la contraseña de un usuario. Requiere un código de
    proposito='reset' ya verificado (vía /api/auth/verificar-codigo),
    no usado y no expirado. Sin código válido, no cambia nada.
    """
    data           = request.json or {}
    correo         = data.get('correo', '').strip().lower()
    codigo         = data.get('codigo', '').strip()
    nueva_password = data.get('nueva_password', '')

    if not correo or not codigo or not nueva_password:
        return jsonify({"status": "error", "mensaje": "Datos incompletos"}), 400
    if len(nueva_password) < 6:
        return jsonify({"status": "error", "mensaje": "La contraseña debe tener al menos 6 caracteres"}), 400

    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
        cursor.execute(
            """SELECT * FROM verificaciones_email
               WHERE correo=%s AND codigo=%s AND proposito='reset'
               ORDER BY idVerificacion DESC LIMIT 1""",
            (correo, codigo)
        )
        fila = cursor.fetchone()

        if not fila or not fila['verificado']:
            cursor.close(); conexion.close()
            return jsonify({"status": "error", "mensaje": "Debes verificar el código antes de cambiar la contraseña."}), 400
        if fila['usado']:
            cursor.close(); conexion.close()
            return jsonify({"status": "error", "mensaje": "El código ya fue utilizado. Solicita uno nuevo."}), 400
        if datetime.now() >= fila['expira_en']:
            cursor.close(); conexion.close()
            return jsonify({"status": "error", "mensaje": "El código ha expirado. Solicita uno nuevo."}), 400

        cursor.execute(
            "UPDATE usuarios SET `contraseña` = %s WHERE correo = %s",
            (generate_password_hash(nueva_password), correo)
        )
        cursor.execute(
            "UPDATE verificaciones_email SET usado=1 WHERE idVerificacion=%s",
            (fila['idVerificacion'],)
        )
        conexion.commit()
        cursor.close(); conexion.close()
        return jsonify({"status": "success", "mensaje": "Contraseña actualizada correctamente"})
    except Exception as e:
        return jsonify({"status": "error", "mensaje": str(e)}), 500


@app.route('/api/auth/login', methods=['POST'])
def login():
    data             = request.json
    correo           = data.get('correo')
    contrasena_plana = data.get('contrasena')

    conexion = conectar_bd()
    cursor   = conexion.cursor(dictionary=True)
    cursor.execute("SELECT * FROM usuarios WHERE correo = %s", (correo,))
    usuario  = cursor.fetchone()
    cursor.close()
    conexion.close()

    if not usuario:
        return jsonify({"status": "error", "mensaje": "El correo no está registrado"}), 404

    if not check_password_hash(usuario['contraseña'], contrasena_plana):
        return jsonify({"status": "error", "mensaje": "Contraseña incorrecta"}), 401

    if not usuario.get('email_verified'):
        return jsonify({
            "status": "error",
            "mensaje": "Debes verificar tu correo antes de iniciar sesión.",
            "requiere_verificacion": True,
            "correo": correo
        }), 403

    session['usuario_logueado'] = correo
    session['usuario_nombre']   = usuario['nombre']
    return jsonify({"status": "success", "mensaje": "Login exitoso"})


@app.route('/api/usuario/info', methods=['GET'])
@login_requerido
def obtener_usuario():
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
        cursor.execute(
            "SELECT nombre, apellido_paterno, apellido_materno, correo, foto_perfil FROM usuarios WHERE correo = %s",
            (session['usuario_logueado'],)
        )
        usuario = cursor.fetchone()
        cursor.close()
        conexion.close()

        if not usuario:
            return jsonify({"error": "Usuario no encontrado"}), 404

        return jsonify({
            "nombre":           usuario['nombre'],
            "apellido_paterno": usuario.get('apellido_paterno') or '',
            "apellido_materno": usuario.get('apellido_materno') or '',
            "correo":           usuario['correo'],
            "foto_perfil":      usuario.get('foto_perfil') or None
        })
    except Exception:
        try:
            conexion = conectar_bd()
            cursor   = conexion.cursor(dictionary=True)
            cursor.execute(
                "SELECT nombre, correo FROM usuarios WHERE correo = %s",
                (session['usuario_logueado'],)
            )
            usuario = cursor.fetchone()
            cursor.close()
            conexion.close()
            return jsonify({"nombre": usuario['nombre'], "correo": usuario['correo'], "foto_perfil": None})
        except Exception as e2:
            return jsonify({"error": str(e2)}), 500


@app.route('/api/usuario/password', methods=['POST'])
@login_requerido
def cambiar_password():
    data  = request.json
    nueva = data.get('password', '').strip()

    if not nueva or len(nueva) < 6:
        return jsonify({"error": "La contraseña debe tener al menos 6 caracteres"}), 400

    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        cursor.execute(
            "UPDATE usuarios SET `contraseña` = %s WHERE correo = %s",
            (generate_password_hash(nueva), session['usuario_logueado'])
        )
        conexion.commit()
        filas = cursor.rowcount
        cursor.close()
        conexion.close()
        if filas == 0:
            return jsonify({"error": "No se encontró el usuario"}), 404
        return jsonify({"status": "success", "mensaje": "Contraseña actualizada"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/usuario/foto', methods=['POST'])
@login_requerido
def actualizar_foto():
    data = request.json
    foto = data.get('foto')

    if not foto:
        return jsonify({"error": "No se recibió imagen"}), 400

    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        try:
            cursor.execute("ALTER TABLE usuarios ADD COLUMN foto_perfil LONGTEXT NULL")
            conexion.commit()
        except Exception:
            pass
        cursor.execute(
            "UPDATE usuarios SET foto_perfil = %s WHERE correo = %s",
            (foto, session['usuario_logueado'])
        )
        conexion.commit()
        cursor.close()
        conexion.close()
        return jsonify({"status": "success", "mensaje": "Foto actualizada"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _generar_pairing_code():
    sufijo = ''.join(random.choices(string.digits, k=4))
    return f"HIDRO-{sufijo}"


@app.route('/api/dispositivos/agregar', methods=['POST'])
@login_requerido
def agregar_dispositivo():
    data   = request.json
    nombre = data.get('nombre')
    tipo   = data.get('tipo')

    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        id_u     = get_id_usuario()

        for _ in range(10):
            codigo = _generar_pairing_code()
            cursor.execute("SELECT idDispositivo FROM dispositivos WHERE pairing_code = %s", (codigo,))
            if not cursor.fetchone():
                break

        cursor.execute(
            "INSERT INTO dispositivos (nombre, tipo, idSistema, idUsuario, pairing_code) VALUES (%s, %s, 1, %s, %s)",
            (nombre, tipo, id_u, codigo)
        )
        conexion.commit()
        nuevo_id = cursor.lastrowid
        cursor.close()
        conexion.close()
        return jsonify({"status": "success", "idDispositivo": nuevo_id, "pairing_code": codigo})
    except Exception as e:
        return jsonify({"status": "error", "mensaje": str(e)}), 500


@app.route('/api/dispositivos/eliminar/<int:id_dispositivo>', methods=['DELETE'])
@login_requerido
def eliminar_dispositivo(id_dispositivo):
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        id_u     = get_id_usuario()
        cursor.execute(
            "SELECT idDispositivo FROM dispositivos WHERE idDispositivo=%s AND idUsuario=%s",
            (id_dispositivo, id_u)
        )
        if not cursor.fetchone():
            return jsonify({"status": "error", "mensaje": "Dispositivo no encontrado"}), 404
        cursor.execute("SELECT idSensore FROM sensores WHERE idDispositivo=%s", (id_dispositivo,))
        ids_sensores = [row[0] for row in cursor.fetchall()]
        for sid in ids_sensores:
            cursor.execute("DELETE FROM historial_alertas WHERE idSensor=%s", (sid,))
            cursor.execute("DELETE FROM parametros_alerta WHERE idSensor=%s", (sid,))
            cursor.execute("DELETE FROM registro_sensores WHERE idSensor=%s", (sid,))
        cursor.execute("DELETE FROM config_relay WHERE idDispositivo=%s", (id_dispositivo,))
        cursor.execute("DELETE FROM sensores WHERE idDispositivo=%s", (id_dispositivo,))
        cursor.execute("DELETE FROM dispositivos WHERE idDispositivo=%s", (id_dispositivo,))
        conexion.commit()
        cursor.close()
        conexion.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "mensaje": str(e)}), 500


@app.route('/api/dispositivos/lista', methods=['GET'])
@login_requerido
def obtener_lista_dispositivos():
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
        id_u     = get_id_usuario()
        cursor.execute("""
            SELECT d.idDispositivo, d.nombre, d.tipo, 'dueno' AS rol
            FROM dispositivos d
            WHERE d.idUsuario = %s

            UNION

            SELECT d.idDispositivo, d.nombre, d.tipo, dm.permiso AS rol
            FROM dispositivo_miembros dm
            JOIN dispositivos d ON dm.idDispositivo = d.idDispositivo
            WHERE dm.idUsuario = %s
        """, (id_u, id_u))
        dispositivos = cursor.fetchall()
        cursor.close()
        conexion.close()
        return jsonify(dispositivos)
    except Exception as e:
        return jsonify({"status": "error", "mensaje": str(e)}), 500


@app.route('/api/sensores/agregar', methods=['POST'])
@login_requerido
def agregar_sensor():
    data = request.json
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        id_u     = get_id_usuario()
        cursor.execute(
            "SELECT idDispositivo FROM dispositivos WHERE idDispositivo=%s AND idUsuario=%s",
            (data['idDispositivo'], id_u)
        )
        if not cursor.fetchone():
            cursor.close(); conexion.close()
            return jsonify({"status": "error", "mensaje": "Dispositivo no encontrado"}), 404
        cursor.execute(
            "INSERT INTO sensores (tipo_sensor, unidad_medida, idDispositivo) VALUES (%s, %s, %s)",
            (data['tipo'], data['unidad'], data['idDispositivo'])
        )
        conexion.commit()
        cursor.close()
        conexion.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "mensaje": str(e)}), 500


@app.route('/api/sensores/editar/<int:id_sensor>', methods=['PUT'])
@login_requerido
def editar_sensor(id_sensor):
    data = request.json
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        id_u     = get_id_usuario()
        cursor.execute("""
            SELECT s.idSensore FROM sensores s
            INNER JOIN dispositivos d ON s.idDispositivo = d.idDispositivo
            WHERE s.idSensore=%s AND d.idUsuario=%s
        """, (id_sensor, id_u))
        if not cursor.fetchone():
            cursor.close(); conexion.close()
            return jsonify({"status": "error", "mensaje": "Sensor no encontrado"}), 404
        cursor.execute(
            "UPDATE sensores SET tipo_sensor=%s, unidad_medida=%s WHERE idSensore=%s",
            (data['tipo'], data['unidad'], id_sensor)
        )
        conexion.commit()
        cursor.close()
        conexion.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "mensaje": str(e)}), 500


@app.route('/api/sensores/eliminar/<int:id_sensor>', methods=['DELETE'])
@login_requerido
def eliminar_sensor(id_sensor):
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        id_u     = get_id_usuario()
        cursor.execute("""
            SELECT s.idSensore FROM sensores s
            INNER JOIN dispositivos d ON s.idDispositivo = d.idDispositivo
            WHERE s.idSensore=%s AND d.idUsuario=%s
        """, (id_sensor, id_u))
        if not cursor.fetchone():
            return jsonify({"status": "error", "mensaje": "Sensor no encontrado"}), 404
        cursor.execute("DELETE FROM historial_alertas WHERE idSensor=%s", (id_sensor,))
        cursor.execute("DELETE FROM parametros_alerta WHERE idSensor=%s", (id_sensor,))
        cursor.execute("DELETE FROM registro_sensores WHERE idSensor=%s", (id_sensor,))
        cursor.execute("DELETE FROM sensores WHERE idSensore=%s", (id_sensor,))
        conexion.commit()
        cursor.close()
        conexion.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "mensaje": str(e)}), 500


@app.route('/api/sensores/lista', methods=['GET'])
@login_requerido
def obtener_lista_sensores():
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
        id_u     = get_id_usuario()
        cursor.execute("""
            SELECT s.idSensore, s.tipo_sensor, s.unidad_medida, s.idDispositivo,
                   d.nombre AS nombre_dispositivo
            FROM sensores s
            INNER JOIN dispositivos d ON s.idDispositivo = d.idDispositivo
            WHERE d.idUsuario = %s

            UNION

            SELECT s.idSensore, s.tipo_sensor, s.unidad_medida, s.idDispositivo,
                   d.nombre AS nombre_dispositivo
            FROM sensores s
            INNER JOIN dispositivos d ON s.idDispositivo = d.idDispositivo
            INNER JOIN dispositivo_miembros dm ON dm.idDispositivo = d.idDispositivo
            WHERE dm.idUsuario = %s
        """, (id_u, id_u))
        sensores = cursor.fetchall()
        cursor.close()
        conexion.close()
        return jsonify(sensores)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/sensores/estado/<int:id_sensor>', methods=['GET'])
@login_requerido
def verificar_estado(id_sensor):
    try:
        conexion  = conectar_bd()
        cursor    = conexion.cursor(dictionary=True)
        cursor.execute(
            "SELECT idSensore, tipo_sensor, unidad_medida FROM sensores WHERE idSensore = %s",
            (id_sensor,)
        )
        sensor = cursor.fetchone()
        if not sensor:
            cursor.close(); conexion.close()
            return jsonify({"error": "Sensor no encontrado"}), 404

        hace_5min = datetime.now() - timedelta(minutes=5)
        cursor.execute(
            "SELECT valor, fecha_hora FROM registro_sensores WHERE idSensor = %s ORDER BY fecha_hora DESC LIMIT 1",
            (id_sensor,)
        )
        ultima = cursor.fetchone()
        cursor.close(); conexion.close()

        activo         = False
        ultima_lectura = None

        if ultima:
            if isinstance(ultima['fecha_hora'], datetime):
                activo = ultima['fecha_hora'] >= hace_5min
                ultima_lectura = {
                    "valor":      ultima['valor'],
                    "unidad":     sensor['unidad_medida'],
                    "fecha_hora": ultima['fecha_hora'].strftime('%Y-%m-%d %H:%M:%S')
                }
            else:
                activo = True
                ultima_lectura = {
                    "valor":      ultima['valor'],
                    "unidad":     sensor['unidad_medida'],
                    "fecha_hora": str(ultima['fecha_hora'])
                }

        return jsonify({"activo": activo, "ultima_lectura": ultima_lectura})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/sensores/datos-actuales/<int:id_sensor>', methods=['GET'])
@login_requerido
def datos_actuales(id_sensor):
    try:
        id_u = get_id_usuario()
        with sesion_rangos() as (_, cursor):
            cursor.execute("SELECT idDispositivo, unidad_medida FROM sensores WHERE idSensore=%s", (id_sensor,))
            sensor = cursor.fetchone()
            if not sensor or not permiso_rangos(cursor, sensor['idDispositivo'], id_u):
                return jsonify(error='Sensor no encontrado'), 404
            cursor.execute("""SELECT valor, fecha_hora FROM registro_sensores WHERE idSensor=%s
                              ORDER BY fecha_hora DESC, idRegistro_sensor DESC LIMIT 1""", (id_sensor,))
            dato = cursor.fetchone()
            if not dato:
                return jsonify(error='Sin datos disponibles para este sensor'), 404
            resultado = evaluar_lectura(dato['valor'], dato['fecha_hora'], reglas_sensor(cursor, id_sensor))
            return jsonify(dict(resultado, valor=dato['valor'], unidad=sensor['unidad_medida']))
    except Exception:
        app.logger.exception('Error consultando lectura actual')
        return jsonify(error='No se pudo consultar la lectura actual.'), 500


@app.route('/api/sensores/analitica/<int:id_sensor>/<rango>', methods=['GET'])
@login_requerido
def obtener_analitica(id_sensor, rango):
    try:
        id_u     = get_id_usuario()
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
        cursor.execute("""
            SELECT s.idSensore FROM sensores s
            INNER JOIN dispositivos d ON s.idDispositivo = d.idDispositivo
            WHERE s.idSensore = %s AND (
                d.idUsuario = %s OR
                EXISTS (SELECT 1 FROM dispositivo_miembros dm
                        WHERE dm.idDispositivo = d.idDispositivo AND dm.idUsuario = %s)
            )
        """, (id_sensor, id_u, id_u))
        if not cursor.fetchone():
            cursor.close(); conexion.close()
            return jsonify({"error": "No autorizado"}), 403

        if rango == '24h':
            fecha_inicio = datetime.now() - timedelta(hours=24)
        elif rango == 'semana':
            fecha_inicio = datetime.now() - timedelta(days=7)
        else:
            fecha_inicio = datetime.now() - timedelta(hours=1)

        cursor.execute(
            "SELECT valor, fecha_hora FROM registro_sensores WHERE idSensor = %s AND fecha_hora >= %s ORDER BY fecha_hora ASC",
            (id_sensor, fecha_inicio)
        )
        datos = cursor.fetchall()
        cursor.close(); conexion.close()

        for fila in datos:
            if isinstance(fila['fecha_hora'], datetime):
                fila['fecha_hora'] = fila['fecha_hora'].strftime('%Y-%m-%d %H:%M:%S')

        return jsonify(datos)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Cultivos permitidos ─────────────────────────────────────────────
# El sistema solo maneja estos dos cultivos. Se valida aquí (backend) y
# no solo en la interfaz, para que nadie pueda saltarse la restricción.
CULTIVOS_PERMITIDOS = ('cilantro', 'perejil')
_SQL_PERMITIDOS = "(" + ",".join("'%s'" % c for c in CULTIVOS_PERMITIDOS) + ")"


def tipo_cultivo_permitido(cursor, id_tipo):
    """True si el idTipo_Cultivo corresponde a Cilantro o Perejil."""
    cursor.execute(
        "SELECT 1 FROM tipo_cultivo WHERE idTipo_Cultivo = %s "
        "AND LOWER(nombre_planta) IN " + _SQL_PERMITIDOS,
        (id_tipo,)
    )
    return cursor.fetchone() is not None


@app.route('/api/cultivos/sembrar', methods=['POST'])
@login_requerido
def sembrar_cultivo():
    data = request.json
    if not all(k in data for k in ['nombre', 'fecha', 'cantidad', 'tamano', 'idTipo', 'idSistema']):
        return jsonify({"error": "Faltan datos requeridos"}), 400

    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        id_u     = get_id_usuario()
        if not tipo_cultivo_permitido(cursor, data['idTipo']):
            cursor.close(); conexion.close()
            return jsonify({"error": "Solo se pueden sembrar Cilantro o Perejil"}), 400
        cursor.execute(
            """INSERT INTO cultivos (nombreCultivo, fecha_siembra, cantidad, tamano_planta,
                                     idTipo_Cultivo, idSistema, idUsuario)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (data['nombre'], data['fecha'], data['cantidad'], data['tamano'],
             data['idTipo'], data['idSistema'], id_u)
        )
        conexion.commit()
        cursor.close(); conexion.close()
        return jsonify({"status": "success", "message": "Siembra registrada correctamente"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/cultivos/lista', methods=['GET'])
@login_requerido
def obtener_cultivos():
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
        id_u     = get_id_usuario()
        cursor.execute("""
            SELECT c.idCultivo, c.nombreCultivo, c.fecha_siembra, c.cantidad,
                   c.tamano_planta, t.nombre_planta AS tipo_cultivo
            FROM cultivos c
            LEFT JOIN tipo_cultivo t ON c.idTipo_Cultivo = t.idTipo_Cultivo
            WHERE c.idUsuario = %s
        """, (id_u,))
        cultivos = cursor.fetchall()
        for row in cultivos:
            if hasattr(row.get('fecha_siembra'), 'strftime'):
                row['fecha_siembra'] = row['fecha_siembra'].strftime('%Y-%m-%d')
        cursor.close(); conexion.close()
        return jsonify(cultivos)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/cultivos/editar/<int:id_cultivo>', methods=['PUT'])
@login_requerido
def editar_cultivo(id_cultivo):
    data = request.json
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        id_u     = get_id_usuario()
        if not tipo_cultivo_permitido(cursor, data['idTipo']):
            cursor.close(); conexion.close()
            return jsonify({"error": "Solo se pueden manejar Cilantro o Perejil"}), 400
        cursor.execute("""
            UPDATE cultivos
            SET nombreCultivo=%s, fecha_siembra=%s, cantidad=%s,
                tamano_planta=%s, idTipo_Cultivo=%s
            WHERE idCultivo=%s AND idUsuario=%s
        """, (data['nombre'], data['fecha'], data['cantidad'],
              data['tamano'], data['idTipo'], id_cultivo, id_u))
        conexion.commit()
        cursor.close(); conexion.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/cultivos/eliminar/<int:id_cultivo>', methods=['DELETE'])
@login_requerido
def eliminar_cultivo(id_cultivo):
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        id_u     = get_id_usuario()
        cursor.execute("DELETE FROM cosecha WHERE idCultivo=%s", (id_cultivo,))
        cursor.execute("DELETE FROM cultivos WHERE idCultivo=%s AND idUsuario=%s", (id_cultivo, id_u))
        conexion.commit()
        cursor.close(); conexion.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/tipo_cultivo/lista', methods=['GET'])
@login_requerido
def obtener_tipos_cultivo():
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
        cursor.execute(
            "SELECT idTipo_Cultivo, nombre_planta FROM tipo_cultivo "
            "WHERE LOWER(nombre_planta) IN " + _SQL_PERMITIDOS + " ORDER BY nombre_planta"
        )
        tipos = cursor.fetchall()
        cursor.close(); conexion.close()
        return jsonify(tipos)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/tipo_cultivo/agregar', methods=['POST'])
@login_requerido
def agregar_tipo_cultivo():
    # Deshabilitado: el catálogo es fijo (Cilantro y Perejil, ver migración 002).
    return jsonify({"error": "El catálogo de cultivos es fijo: solo Cilantro y Perejil"}), 403


@app.route('/api/cosechas/registrar', methods=['POST'])
@login_requerido
def registrar_cosecha():
    data = request.json
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        cursor.execute(
            "INSERT INTO cosecha (fecha, cantidad, calidad, observaciones, idCultivo) VALUES (%s, %s, %s, %s, %s)",
            (data['fecha'], data['cantidad'], data['calidad'], data.get('observaciones', ''), data['idCultivo'])
        )
        conexion.commit()
        cursor.close(); conexion.close()
        return jsonify({"status": "success", "message": "Cosecha registrada con éxito"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/cosechas/lista', methods=['GET'])
@login_requerido
def obtener_cosechas():
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
        id_u     = get_id_usuario()
        cursor.execute("""
            SELECT cs.idCosecha, cs.fecha, cs.cantidad, cs.calidad,
                   cs.observaciones, c.nombreCultivo
            FROM cosecha cs
            INNER JOIN cultivos c ON cs.idCultivo = c.idCultivo
            WHERE c.idUsuario = %s
            ORDER BY cs.fecha DESC
        """, (id_u,))
        cosechas = cursor.fetchall()
        for row in cosechas:
            if hasattr(row.get('fecha'), 'strftime'):
                row['fecha'] = row['fecha'].strftime('%Y-%m-%d')
        cursor.close(); conexion.close()
        return jsonify(cosechas)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/cosechas/editar/<int:id_cosecha>', methods=['PUT'])
@login_requerido
def editar_cosecha(id_cosecha):
    data = request.json
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        cursor.execute("""
            UPDATE cosecha
            SET fecha=%s, cantidad=%s, calidad=%s, observaciones=%s
            WHERE idCosecha=%s
        """, (data['fecha'], data['cantidad'], data['calidad'],
              data.get('observaciones', ''), id_cosecha))
        conexion.commit()
        cursor.close(); conexion.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/cosechas/eliminar/<int:id_cosecha>', methods=['DELETE'])
@login_requerido
def eliminar_cosecha(id_cosecha):
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        cursor.execute("DELETE FROM cosecha WHERE idCosecha=%s", (id_cosecha,))
        conexion.commit()
        cursor.close(); conexion.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/alertas/parametros/lista', methods=['GET'])
@login_requerido
def listar_parametros():
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
        id_u     = get_id_usuario()
        cursor.execute("""
            SELECT p.idParametro, p.nombre, p.condicion, p.valor_umbral,
                   p.prioridad, p.activo,
                   s.tipo_sensor, s.unidad_medida, s.idSensore AS idSensor
            FROM parametros_alerta p
            JOIN sensores s ON p.idSensor = s.idSensore
            JOIN dispositivos d ON s.idDispositivo = d.idDispositivo
            WHERE (d.idUsuario = %s OR EXISTS (
                SELECT 1 FROM dispositivo_miembros dm
                WHERE dm.idDispositivo = d.idDispositivo AND dm.idUsuario = %s
            ))
            ORDER BY p.prioridad DESC, p.idParametro DESC
        """, (id_u, id_u))
        datos = cursor.fetchall()
        cursor.close(); conexion.close()
        return jsonify(datos)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/alertas/parametros/agregar', methods=['POST'])
@login_requerido
def agregar_parametro():
    data   = request.json
    campos = ['idSensor', 'nombre', 'condicion', 'valor_umbral', 'prioridad']
    if not all(k in data for k in campos):
        return jsonify({"error": "Faltan campos requeridos"}), 400

    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        cursor.execute(
            """INSERT INTO parametros_alerta (idSensor, nombre, condicion, valor_umbral, prioridad, activo)
               VALUES (%s, %s, %s, %s, %s, 1)""",
            (data['idSensor'], data['nombre'], data['condicion'], data['valor_umbral'], data['prioridad'])
        )
        conexion.commit()
        nuevo_id = cursor.lastrowid
        cursor.close(); conexion.close()
        return jsonify({"status": "success", "idParametro": nuevo_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/alertas/parametros/editar/<int:id_param>', methods=['PUT'])
@login_requerido
def editar_parametro(id_param):
    data = request.json
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        cursor.execute(
            """UPDATE parametros_alerta
               SET nombre=%s, condicion=%s, valor_umbral=%s, prioridad=%s, activo=%s
               WHERE idParametro=%s""",
            (data['nombre'], data['condicion'], data['valor_umbral'],
             data['prioridad'], data.get('activo', 1), id_param)
        )
        conexion.commit()
        cursor.close(); conexion.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/alertas/parametros/eliminar/<int:id_param>', methods=['DELETE'])
@login_requerido
def eliminar_parametro(id_param):
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        cursor.execute("DELETE FROM historial_alertas WHERE idParametro=%s", (id_param,))
        cursor.execute("DELETE FROM parametros_alerta WHERE idParametro=%s", (id_param,))
        conexion.commit()
        cursor.close(); conexion.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/alertas/historial', methods=['GET'])
@login_requerido
def historial_alertas():
    try:
        prioridad = request.args.get('prioridad')
        estado    = request.args.get('estado')
        id_sensor = request.args.get('idSensor')
        limite    = int(request.args.get('limite', 200))

        condiciones = []
        valores     = []

        if prioridad:
            condiciones.append("h.prioridad = %s"); valores.append(prioridad)
        if estado:
            condiciones.append("h.estado = %s");    valores.append(estado)
        if id_sensor:
            condiciones.append("h.idSensor = %s");  valores.append(id_sensor)

        id_u = get_id_usuario()
        condiciones.append("(d.idUsuario = %s OR EXISTS (SELECT 1 FROM dispositivo_miembros dm WHERE dm.idDispositivo = d.idDispositivo AND dm.idUsuario = %s))")
        valores.append(id_u)
        valores.append(id_u)
        where = "WHERE " + " AND ".join(condiciones)

        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
        cursor.execute(f"""
            SELECT h.idHistorial, h.valor_detectado, h.prioridad, h.estado,
                   h.mensaje, h.fecha_hora, h.fecha_resolucion,
                   s.tipo_sensor, s.unidad_medida,
                   p.nombre AS nombre_parametro, p.condicion, p.valor_umbral
            FROM historial_alertas h
            JOIN sensores s ON h.idSensor = s.idSensore
            JOIN dispositivos d ON s.idDispositivo = d.idDispositivo
            JOIN parametros_alerta p ON h.idParametro = p.idParametro
            {where}
            ORDER BY h.fecha_hora DESC
            LIMIT %s
        """, valores + [limite])

        datos = cursor.fetchall()
        for row in datos:
            if isinstance(row.get('fecha_hora'), datetime):
                row['fecha_hora'] = row['fecha_hora'].strftime('%Y-%m-%d %H:%M:%S')
            if isinstance(row.get('fecha_resolucion'), datetime):
                row['fecha_resolucion'] = row['fecha_resolucion'].strftime('%Y-%m-%d %H:%M:%S')

        cursor.close(); conexion.close()
        return jsonify(datos)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/alertas/historial/registrar', methods=['POST'])
@login_requerido
def registrar_alerta():
    data   = request.json
    campos = ['idParametro', 'idSensor', 'valor_detectado', 'prioridad']
    if not all(k in data for k in campos):
        return jsonify({"error": "Faltan campos requeridos"}), 400

    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        cursor.execute(
            """INSERT INTO historial_alertas (idParametro, idSensor, valor_detectado, prioridad, estado, mensaje)
               VALUES (%s, %s, %s, %s, 'nueva', %s)""",
            (data['idParametro'], data['idSensor'], data['valor_detectado'],
             data['prioridad'], data.get('mensaje', ''))
        )
        conexion.commit()
        cursor.close(); conexion.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/alertas/historial/estado/<int:id_historial>', methods=['PUT'])
@login_requerido
def actualizar_estado_alerta(id_historial):
    data         = request.json
    nuevo_estado = data.get('estado')

    if nuevo_estado not in ('vista', 'resuelta'):
        return jsonify({"error": "Estado inválido"}), 400

    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        if nuevo_estado == 'resuelta':
            cursor.execute(
                "UPDATE historial_alertas SET estado=%s, fecha_resolucion=NOW() WHERE idHistorial=%s",
                (nuevo_estado, id_historial)
            )
        else:
            cursor.execute(
                "UPDATE historial_alertas SET estado=%s WHERE idHistorial=%s",
                (nuevo_estado, id_historial)
            )
        conexion.commit()
        cursor.close(); conexion.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/alertas/conteo', methods=['GET'])
@login_requerido
def conteo_alertas_nuevas():
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor()
        id_u     = get_id_usuario()
        cursor.execute("""
            SELECT COUNT(*) FROM historial_alertas h
            JOIN sensores s ON h.idSensor = s.idSensore
            JOIN dispositivos d ON s.idDispositivo = d.idDispositivo
            WHERE h.estado='nueva' AND (d.idUsuario=%s OR EXISTS (
                SELECT 1 FROM dispositivo_miembros dm
                WHERE dm.idDispositivo = d.idDispositivo AND dm.idUsuario = %s
            ))
        """, (id_u, id_u))
        total = cursor.fetchone()[0]
        cursor.close(); conexion.close()
        return jsonify({"nuevas": total})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/relevador/config', methods=['GET'])
@login_requerido
def obtener_config_relevador():
    device_id = request.args.get("device_id", type=int)
    if not device_id:
        return jsonify({"error": "Falta device_id"}), 400
    id_u = get_id_usuario()
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
        cursor.execute("""
            SELECT d.idDispositivo FROM dispositivos d
            WHERE d.idDispositivo = %s AND (
                d.idUsuario = %s OR
                EXISTS (SELECT 1 FROM dispositivo_miembros dm
                        WHERE dm.idDispositivo = d.idDispositivo AND dm.idUsuario = %s)
            )
        """, (device_id, id_u, id_u))
        if not cursor.fetchone():
            cursor.close(); conexion.close()
            return jsonify({"error": "No autorizado"}), 403
        cursor.close(); conexion.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(get_relay(device_id, incluir_guardada=True))


@app.route('/api/relevador/config', methods=['POST'])
@login_requerido
def guardar_config_relevador():
    data = request.get_json(silent=True)
    try:
        if not isinstance(data, dict):
            raise ValueError("Envia un objeto JSON valido.")
        device_id = _relay_validar_entero(data.get("device_id"), "device_id", maximo=2147483647)
        config = _relay_validar_config(data)
    except ValueError as error:
        return jsonify({"status": "error", "error": str(error)}), 400

    id_u = get_id_usuario()
    conexion = cursor = None
    try:
        conexion = conectar_bd()
        cursor = conexion.cursor(dictionary=True)
        cursor.execute("""
            SELECT d.idDispositivo FROM dispositivos d
            WHERE d.idDispositivo = %s AND (
                d.idUsuario = %s OR
                EXISTS (SELECT 1 FROM dispositivo_miembros dm
                        WHERE dm.idDispositivo = %s
                          AND dm.idUsuario = %s AND dm.permiso = 'controlar')
            )
        """, (device_id, id_u, device_id, id_u))
        if not cursor.fetchone():
            return jsonify({"status": "error", "error": "No autorizado"}), 403
    except Exception:
        app.logger.exception("Error verificando permiso de control del relevador")
        return jsonify({"status": "error", "error": "No se pudo verificar el permiso de control. Revisa la conexion con MySQL."}), 503
    finally:
        _relay_cerrar_recursos(cursor, conexion)

    try:
        guardada = set_relay(device_id, config, id_usuario=id_u)
    except ValueError as error:
        return jsonify({"status": "error", "error": str(error)}), 400
    except Exception:
        app.logger.exception("Error guardando configuracion del relevador")
        return jsonify({"status": "error", "error": "No se pudo confirmar el guardado en MySQL. La configuracion en memoria no se actualizo."}), 503

    print(f"[RELAY dev={device_id}] Configuracion guardada: modo={guardada['modo']} "
          f"on={guardada['tiempo_on']}s off={guardada['tiempo_off']}s")
    # 'config' conserva el contrato anterior: tiempos efectivos con perfil/etapa.
    # 'config_guardada' permite distinguirlos de los valores persistidos en MySQL.
    return jsonify({"status": "ok", "config": get_relay(device_id),
                    "config_guardada": guardada})


@app.route('/api/relevador/estado', methods=['GET'])
@login_requerido
def estado_relevador():
    device_id = request.args.get("device_id", type=int)
    if not device_id:
        return jsonify({"error": "Falta device_id"}), 400
    id_u = get_id_usuario()
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
        cursor.execute("""
            SELECT d.idDispositivo FROM dispositivos d
            WHERE d.idDispositivo = %s AND (
                d.idUsuario = %s OR
                EXISTS (SELECT 1 FROM dispositivo_miembros dm
                        WHERE dm.idDispositivo = d.idDispositivo AND dm.idUsuario = %s)
            )
        """, (device_id, id_u, id_u))
        if not cursor.fetchone():
            cursor.close(); conexion.close()
            return jsonify({"error": "No autorizado"}), 403
        cursor.close(); conexion.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(get_relay(device_id))


@app.route('/api/arduino/reportar_wifi', methods=['POST'])
def arduino_reportar_wifi():
    data = request.json or {}
    if data.get("token") != ARDUINO_TOKEN:
        return jsonify({"error": "No autorizado"}), 401

    device_id = data.get("device_id")
    ssid      = data.get("ssid", "").strip()

    if not device_id or not ssid:
        return jsonify({"error": "Faltan datos"}), 400

    fecha = datetime.now().strftime("%d/%m/%Y %H:%M")
    with wifi_lock:
        wifi_actual[device_id] = {"ssid": ssid, "fecha": fecha}
        hist = wifi_historial.setdefault(device_id, [])
        hist[:] = [h for h in hist if h["ssid"] != ssid]
        hist.append({"ssid": ssid, "fecha": fecha})

    print(f"[WiFi dev={device_id}] Arduino conectado a: {ssid}")
    return jsonify({"status": "ok"})


@app.route('/api/wifi/estado', methods=['GET'])
@login_requerido
def wifi_estado():
    device_id = request.args.get("device_id", type=int)
    if not device_id:
        return jsonify({"ssid": None, "fecha": None})
    return jsonify(wifi_actual.get(device_id, {"ssid": None, "fecha": None}))


@app.route('/api/wifi/historial', methods=['GET'])
@login_requerido
def wifi_historial_endpoint():
    device_id = request.args.get("device_id", type=int)
    hist = wifi_historial.get(device_id, []) if device_id else []
    return jsonify({"historial": hist[-10:]})


@app.route('/api/wifi/configurar', methods=['POST'])
@login_requerido
def wifi_configurar():
    data      = request.json or {}
    device_id = data.get("device_id")
    ssid      = data.get("ssid", "").strip()
    password  = data.get("password", "")

    if not device_id:
        return jsonify({"status": "error", "mensaje": "Falta device_id"}), 400
    if not ssid or not password:
        return jsonify({"status": "error", "mensaje": "SSID y contraseña requeridos"}), 400

    with wifi_lock:
        wifi_pendiente[device_id] = {"ssid": ssid, "password": password}

    fecha = datetime.now().strftime("%d/%m/%Y %H:%M")
    wifi_actual[device_id] = {"ssid": ssid, "fecha": fecha}

    hist = wifi_historial.setdefault(device_id, [])
    hist[:] = [h for h in hist if h["ssid"] != ssid]
    hist.append({"ssid": ssid, "fecha": fecha})

    print(f"[WiFi dev={device_id}] Config pendiente → SSID: {ssid}")
    return jsonify({"status": "ok", "ssid": ssid})


@app.route('/api/miembros/invitar', methods=['POST'])
@login_requerido
def invitar_miembro():
    data      = request.json or {}
    correo    = data.get('correo', '').strip().lower()
    device_id = data.get('device_id')
    permiso   = data.get('permiso', 'ver')

    if not correo or not device_id:
        return jsonify({"error": "Faltan datos"}), 400
    if permiso not in ('ver', 'controlar'):
        return jsonify({"error": "Permiso inválido"}), 400

    try:
        id_u     = get_id_usuario()
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)

        cursor.execute(
            "SELECT idDispositivo, nombre FROM dispositivos WHERE idDispositivo=%s AND idUsuario=%s",
            (device_id, id_u)
        )
        disp = cursor.fetchone()
        if not disp:
            cursor.close(); conexion.close()
            return jsonify({"error": "Dispositivo no encontrado"}), 404

        cursor.execute("SELECT idUsuario, nombre FROM usuarios WHERE correo=%s", (correo,))
        invitado = cursor.fetchone()
        if not invitado:
            cursor.close(); conexion.close()
            return jsonify({"error": "No existe un usuario con ese correo"}), 404

        if invitado['idUsuario'] == id_u:
            cursor.close(); conexion.close()
            return jsonify({"error": "No puedes invitarte a ti mismo"}), 400

        cursor.execute("""
            INSERT INTO dispositivo_miembros (idDispositivo, idUsuario, permiso)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE permiso = VALUES(permiso)
        """, (device_id, invitado['idUsuario'], permiso))
        conexion.commit()
        cursor.close(); conexion.close()

        return jsonify({"status": "ok", "mensaje": f"{invitado['nombre']} agregado como miembro"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/miembros/lista', methods=['GET'])
@login_requerido
def listar_miembros():
    """Lista los miembros de un dispositivo del usuario."""
    device_id = request.args.get('device_id', type=int)
    if not device_id:
        return jsonify({"error": "Falta device_id"}), 400
    try:
        id_u     = get_id_usuario()
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)

        cursor.execute(
            "SELECT idDispositivo FROM dispositivos WHERE idDispositivo=%s AND idUsuario=%s",
            (device_id, id_u)
        )
        if not cursor.fetchone():
            cursor.close(); conexion.close()
            return jsonify({"error": "No autorizado"}), 403

        cursor.execute("""
            SELECT u.idUsuario, u.nombre, u.apellido_paterno, u.correo, dm.permiso
            FROM dispositivo_miembros dm
            JOIN usuarios u ON dm.idUsuario = u.idUsuario
            WHERE dm.idDispositivo = %s
        """, (device_id,))
        miembros = cursor.fetchall()
        cursor.close(); conexion.close()
        return jsonify(miembros)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/miembros/eliminar', methods=['DELETE'])
@login_requerido
def eliminar_miembro():
    """Elimina a un miembro de un dispositivo."""
    data      = request.json or {}
    device_id = data.get('device_id')
    id_miembro = data.get('idUsuario')
    if not device_id or not id_miembro:
        return jsonify({"error": "Faltan datos"}), 400
    try:
        id_u     = get_id_usuario()
        conexion = conectar_bd()
        cursor   = conexion.cursor()

        cursor.execute(
            "SELECT idDispositivo FROM dispositivos WHERE idDispositivo=%s AND idUsuario=%s",
            (device_id, id_u)
        )
        if not cursor.fetchone():
            cursor.close(); conexion.close()
            return jsonify({"error": "No autorizado"}), 403

        cursor.execute(
            "DELETE FROM dispositivo_miembros WHERE idDispositivo=%s AND idUsuario=%s",
            (device_id, id_miembro)
        )
        conexion.commit()
        cursor.close(); conexion.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/miembros/mis-accesos', methods=['GET'])
@login_requerido
def mis_accesos():
    """Devuelve los dispositivos a los que el usuario fue invitado."""
    try:
        id_u     = get_id_usuario()
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
        cursor.execute("""
            SELECT d.idDispositivo, d.nombre, d.tipo, dm.permiso,
                   u.nombre AS nombre_dueno, u.correo AS correo_dueno
            FROM dispositivo_miembros dm
            JOIN dispositivos d ON dm.idDispositivo = d.idDispositivo
            JOIN usuarios u ON d.idUsuario = u.idUsuario
            WHERE dm.idUsuario = %s
        """, (id_u,))
        accesos = cursor.fetchall()
        cursor.close(); conexion.close()
        return jsonify(accesos)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =====================================================================
# HYDROSENSE: perfiles por dispositivo, rangos y evaluacion de lecturas.
# El catalogo existente conserva solo cultivo/etapa/variable/min/max/unidad.
# =====================================================================

ETAPAS_RANGO = ('general', 'inicial', 'desarrollo', 'media', 'final')
PERFILES_RANGO = ('cilantro', 'perejil', 'compartido')
VARIABLE_SENSOR = {
    'temperatura': 'temperatura', 'humedad': 'humedad', 'ph': 'ph',
    'luz': 'luz_ldr', 'distancia': 'distancia', 'ec': 'ec',
    'temperatura_agua': 'temperatura_agua',
}
VARIABLES_COMUNES = {'temperatura', 'humedad', 'temperatura_agua', 'luz_ldr', 'distancia'}
NOMBRES_RANGO = {
    'temperatura': ('Hydrosense: temperatura baja', 'Hydrosense: temperatura alta'),
    'humedad': ('Hydrosense: humedad baja', 'Hydrosense: humedad alta'),
    'ph': ('Hydrosense: pH bajo', 'Hydrosense: pH alto'),
    'luz': ('Hydrosense: luz baja', 'Hydrosense: luz alta'),
    'distancia': ('Hydrosense: agua muy alta', 'Hydrosense: agua baja'),
    'ec': ('Hydrosense: EC baja', 'Hydrosense: EC alta'),
    'temperatura_agua': ('Hydrosense: agua fria', 'Hydrosense: agua caliente'),
}
NOMBRES_ANTERIORES = {
    'luz': ('Hydrosense: luz baja (pendiente)', 'Hydrosense: luz alta (pendiente)'),
    'distancia': ('Hydrosense: agua muy alta (pendiente)', 'Hydrosense: agua baja (pendiente)'),
    'ec': ('Hydrosense: EC baja etapa media', 'Hydrosense: EC alta etapa media'),
    'temperatura_agua': ('Hydrosense: agua fria (guia general)', 'Hydrosense: agua caliente (guia general)'),
}


@contextmanager
def sesion_rangos():
    conexion = conectar_bd()
    cursor = None
    try:
        cursor = conexion.cursor(dictionary=True)
        yield conexion, cursor
    except Exception:
        conexion.rollback()
        raise
    finally:
        if cursor is not None:
            cursor.close()
        conexion.close()


def cumple_condicion(valor, condicion, umbral):
    return ((condicion == 'menor_que' and valor < umbral) or
            (condicion == 'mayor_que' and valor > umbral) or
            (condicion == 'igual_a' and valor == umbral))


def resolver_incidentes(cursor, id_parametro):
    cursor.execute("""
        UPDATE historial_alertas SET estado='resuelta', fecha_resolucion=NOW()
        WHERE idParametro=%s AND estado IN ('nueva', 'vista')
    """, (id_parametro,))


def evaluar_reglas(valor, reglas):
    activas = [p for p in reglas if p['activo']]
    inferiores = [float(p['valor_umbral']) for p in activas if p['condicion'] == 'menor_que']
    superiores = [float(p['valor_umbral']) for p in activas if p['condicion'] == 'mayor_que']
    minimo = max(inferiores) if inferiores else None
    maximo = min(superiores) if superiores else None
    resultado = {'minimo': minimo, 'maximo': maximo,
                 'alertas_activas': bool(activas), 'reglas_disparadas': []}
    if valor is None:
        estado = 'sin_datos'
    elif not math.isfinite(float(valor)):
        estado = 'lectura_invalida'
    elif minimo is not None and maximo is not None and minimo > maximo:
        estado = 'rango_inconsistente'
    elif not activas:
        estado = 'sin_alertas_activas'
    else:
        resultado['reglas_disparadas'] = [p['idParametro'] for p in activas
            if cumple_condicion(float(valor), p['condicion'], float(p['valor_umbral']))]
        estado = ('bajo' if minimo is not None and float(valor) < minimo else
                  'alto' if maximo is not None and float(valor) > maximo else
                  'alerta' if resultado['reglas_disparadas'] else 'en_rango')
    resultado['estado_rango'] = estado
    return resultado


def evaluar_lectura(valor, fecha, reglas):
    resultado = evaluar_reglas(valor, reglas)
    if isinstance(fecha, datetime):
        if fecha < datetime.now() - timedelta(minutes=5):
            resultado['estado_rango'] = 'sin_datos_recientes'
        fecha = fecha.strftime('%Y-%m-%d %H:%M:%S')
    resultado['fecha_hora'] = fecha
    return resultado


def reglas_sensor(cursor, id_sensor):
    cursor.execute("""SELECT idParametro, nombre, condicion, valor_umbral, prioridad, activo
                      FROM parametros_alerta WHERE idSensor=%s ORDER BY idParametro""", (id_sensor,))
    return cursor.fetchall()


def permiso_rangos(cursor, device_id, id_usuario):
    if id_usuario is None:
        return None
    cursor.execute("""
        SELECT d.idDispositivo, d.nombre,
               (d.idUsuario=%s OR EXISTS (
                   SELECT 1 FROM dispositivo_miembros dm
                   WHERE dm.idDispositivo=d.idDispositivo AND dm.idUsuario=%s
                     AND dm.permiso='controlar')) AS puede_editar
        FROM dispositivos d
        WHERE d.idDispositivo=%s AND (d.idUsuario=%s OR EXISTS (
            SELECT 1 FROM dispositivo_miembros dm
            WHERE dm.idDispositivo=d.idDispositivo AND dm.idUsuario=%s))
    """, (id_usuario, id_usuario, device_id, id_usuario, id_usuario))
    return cursor.fetchone()


def validar_perfil(cultivo, etapa):
    if cultivo not in PERFILES_RANGO or etapa not in ETAPAS_RANGO:
        raise ValueError('Selecciona un cultivo y una etapa validos.')


def leer_catalogo(cursor):
    try:
        cursor.execute("""SELECT cultivo, etapa, variable, minimo, maximo, unidad
                          FROM rangos_referencia_hydrosense""")
        return cursor.fetchall()
    except mysql.connector.Error as error:
        if error.errno == 1146:
            raise ValueError('Falta el catalogo de rangos. Ejecuta la migracion 003.') from error
        raise


def leer_perfil(cursor, device_id):
    try:
        cursor.execute("SELECT cultivo, etapa FROM perfil_rangos_dispositivo WHERE idDispositivo=%s", (device_id,))
        return cursor.fetchone()
    except mysql.connector.Error as error:
        if error.errno == 1146:
            return None
        raise


def asegurar_tabla_perfil(cursor):
    # DDL solo al guardar expresamente un perfil; no se ejecuta al arrancar Flask.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS perfil_rangos_dispositivo (
            idDispositivo INT NOT NULL PRIMARY KEY,
            cultivo VARCHAR(20) NOT NULL,
            etapa VARCHAR(25) NOT NULL,
            actualizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (idDispositivo) REFERENCES dispositivos(idDispositivo) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def unidad_normalizada(unidad):
    unidad = str(unidad or '').replace('µ', 'u').replace('μ', 'u').lower()
    unidad = ''.join(c for c in unicodedata.normalize('NFKD', unidad) if not unicodedata.combining(c))
    return unidad.replace('°', '').replace(' ', '')


def factor_unidades(origen, destino):
    origen, destino = unidad_normalizada(origen), unidad_normalizada(destino)
    if origen and origen == destino:
        return 1.0
    return {('us/cm', 'ms/cm'): 0.001, ('ms/cm', 'us/cm'): 1000.0}.get((origen, destino))


def rango_referencia(filas, cultivo, etapa, variable):
    indice = {(r['cultivo'], r['etapa'], r['variable']): r for r in filas}

    def buscar(planta):
        exacto = indice.get((planta, etapa, variable))
        return exacto if exacto is not None else indice.get((planta, 'general', variable))

    fila = buscar(cultivo)
    if fila is None and variable in VARIABLES_COMUNES:
        fila = buscar('compartido')
    if fila is None and cultivo == 'compartido' and variable not in VARIABLES_COMUNES:
        a, b = buscar('cilantro'), buscar('perejil')
        if a is not None and b is not None:
            ra = rango_referencia(filas, 'cilantro', etapa, variable)
            rb = rango_referencia(filas, 'perejil', etapa, variable)
            factor = factor_unidades(rb['unidad'], ra['unidad'])
            if ra['disponible'] and rb['disponible'] and factor is not None:
                minimo = max(ra['minimo'], rb['minimo'] * factor)
                maximo = min(ra['maximo'], rb['maximo'] * factor)
                if minimo <= maximo:
                    return {'minimo': minimo, 'maximo': maximo, 'unidad': ra['unidad'],
                            'disponible': True, 'detalle': 'Intervalo comun de ambas plantas.'}
                return {'minimo': None, 'maximo': None, 'unidad': ra['unidad'],
                        'disponible': False, 'detalle': 'Los intervalos no coinciden en esta etapa.'}
    if fila is None:
        return {'minimo': None, 'maximo': None, 'unidad': '', 'disponible': False,
                'detalle': 'No hay limites para esta combinacion de cultivo y etapa.'}
    minimo, maximo = fila['minimo'], fila['maximo']
    valido = (minimo is not None and maximo is not None and
              math.isfinite(float(minimo)) and math.isfinite(float(maximo)) and minimo <= maximo)
    return {'minimo': float(minimo) if valido else None, 'maximo': float(maximo) if valido else None,
            'unidad': fila['unidad'], 'disponible': valido,
            'detalle': '' if valido else 'Limites pendientes o inconsistentes en el catalogo.'}


def nombres_gestionados(tipo):
    return NOMBRES_RANGO.get(tipo, ()) + NOMBRES_ANTERIORES.get(tipo, ())


def construir_previa(cursor, device_id, cultivo, etapa):
    filas = leer_catalogo(cursor)
    cursor.execute("SELECT idSensore, tipo_sensor, unidad_medida FROM sensores WHERE idDispositivo=%s ORDER BY idSensore", (device_id,))
    sensores = cursor.fetchall()
    cursor.execute("""SELECT p.* FROM parametros_alerta p JOIN sensores s ON p.idSensor=s.idSensore
                      WHERE s.idDispositivo=%s ORDER BY p.idParametro""", (device_id,))
    parametros = cursor.fetchall()
    items = []
    for s in sensores:
        tipo = s['tipo_sensor'].strip().lower()
        variable = VARIABLE_SENSOR.get(tipo)
        ref = rango_referencia(filas, cultivo, etapa, variable) if variable else {
            'minimo': None, 'maximo': None, 'unidad': '', 'disponible': False,
            'detalle': 'Este tipo de sensor no tiene una variable asociada.'}
        if ref['disponible']:
            factor = factor_unidades(ref['unidad'], s['unidad_medida'])
            if factor is None:
                ref.update(disponible=False, minimo=None, maximo=None,
                           detalle='La unidad del sensor no coincide con la del catalogo.')
            else:
                ref.update(minimo=round(ref['minimo'] * factor, 2), maximo=round(ref['maximo'] * factor, 2))
        reglas = [p for p in parametros if p['idSensor'] == s['idSensore']]
        propias = [p for p in reglas if p['nombre'] in nombres_gestionados(tipo)]
        otras = [p['nombre'] for p in reglas if p['activo'] and p not in propias]
        items.append(dict(ref, idSensor=s['idSensore'], tipo=tipo, unidad=s['unidad_medida'],
                          activo_actual=any(p['activo'] for p in propias), otras_alertas=otras))
    for tipo, variable in VARIABLE_SENSOR.items():
        if not any(i['tipo'] == tipo for i in items):
            ref = rango_referencia(filas, cultivo, etapa, variable)
            items.append(dict(ref, idSensor=None, tipo=tipo, disponible=False,
                              detalle='Sensor no registrado en este dispositivo.', activo_actual=False, otras_alertas=[]))
    return {'cultivo': cultivo, 'etapa': etapa, 'sensores': items,
            'iluminacion': {v: rango_referencia(filas, cultivo, etapa, v) for v in ('fotoperiodo', 'ppfd')}}


def aplicar_previa(cursor, previa, activar):
    resumen = []
    for item in previa['sensores']:
        sid, tipo = item['idSensor'], item['tipo']
        if sid is None or tipo not in NOMBRES_RANGO:
            continue
        existentes = [p for p in reglas_sensor(cursor, sid) if p['nombre'] in nombres_gestionados(tipo)]
        activo = bool(item['disponible'] and (sid in activar if activar is not None else item['activo_actual']))
        usados = set()
        if item['disponible']:
            for condicion, nombre, limite in zip(('menor_que', 'mayor_que'), NOMBRES_RANGO[tipo], (item['minimo'], item['maximo'])):
                candidatos = [p for p in existentes if p['condicion'] == condicion]
                previo = next((p for p in candidatos if p['nombre'] == nombre), candidatos[0] if candidatos else None)
                if previo:
                    if float(previo['valor_umbral']) != limite or bool(previo['activo']) != activo:
                        resolver_incidentes(cursor, previo['idParametro'])
                    cursor.execute("UPDATE parametros_alerta SET nombre=%s, valor_umbral=%s, activo=%s WHERE idParametro=%s",
                                   (nombre, limite, int(activo), previo['idParametro']))
                    usados.add(previo['idParametro'])
                else:
                    cursor.execute("""INSERT INTO parametros_alerta (idSensor,nombre,condicion,valor_umbral,prioridad,activo)
                                      VALUES (%s,%s,%s,%s,%s,%s)""",
                                   (sid, nombre, condicion, limite, 'alta' if tipo in ('ph', 'distancia') else 'media', int(activo)))
        for previo in existentes:
            if previo['idParametro'] not in usados:
                cursor.execute("UPDATE parametros_alerta SET activo=0 WHERE idParametro=%s", (previo['idParametro'],))
                resolver_incidentes(cursor, previo['idParametro'])
        resumen.append({'idSensor': sid, 'activo': activo, 'aplicado': item['disponible'], 'detalle': item['detalle']})
    return resumen


def estado_dispositivo(cursor, device_id):
    cursor.execute("""
        SELECT s.idSensore AS idSensor, s.tipo_sensor, s.unidad_medida, r.valor, r.fecha_hora
        FROM sensores s LEFT JOIN registro_sensores r ON r.idRegistro_sensor=(
            SELECT rr.idRegistro_sensor FROM registro_sensores rr WHERE rr.idSensor=s.idSensore
            ORDER BY rr.fecha_hora DESC, rr.idRegistro_sensor DESC LIMIT 1)
        WHERE s.idDispositivo=%s ORDER BY s.idSensore
    """, (device_id,))
    datos = cursor.fetchall()
    for dato in datos:
        dato.update(evaluar_lectura(dato['valor'], dato['fecha_hora'], reglas_sensor(cursor, dato['idSensor'])))
    return datos


@app.route('/api/rangos/dispositivo/<int:device_id>')
@login_requerido
def obtener_rangos_dispositivo(device_id):
    try:
        uid = get_id_usuario()
        with sesion_rangos() as (_, cursor):
            permiso = permiso_rangos(cursor, device_id, uid)
            if not permiso:
                return jsonify(error='Dispositivo no encontrado o sin permiso.'), 403
            return jsonify(perfil=leer_perfil(cursor, device_id), puede_editar=bool(permiso['puede_editar']),
                           sensores=estado_dispositivo(cursor, device_id))
    except Exception:
        app.logger.exception('Error consultando rangos')
        return jsonify(error='No se pudieron consultar los rangos. Revisa la conexion con MySQL.'), 500


@app.route('/api/rangos/previa/<int:device_id>')
@login_requerido
def obtener_previa_rangos(device_id):
    try:
        cultivo, etapa = request.args.get('cultivo'), request.args.get('etapa')
        validar_perfil(cultivo, etapa)
        uid = get_id_usuario()
        with sesion_rangos() as (_, cursor):
            if not permiso_rangos(cursor, device_id, uid):
                return jsonify(error='Dispositivo no encontrado o sin permiso.'), 403
            return jsonify(construir_previa(cursor, device_id, cultivo, etapa))
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except Exception:
        app.logger.exception('Error preparando rangos')
        return jsonify(error='No se pudo leer el catalogo de rangos.'), 500


@app.route('/api/rangos/aplicar/<int:device_id>', methods=['POST'])
@login_requerido
def guardar_perfil_rangos(device_id):
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('Envia un objeto JSON.')
        cultivo, etapa = data.get('cultivo'), data.get('etapa')
        validar_perfil(cultivo, etapa)
        activar = data.get('activar_sensores')
        if activar is not None and (not isinstance(activar, list) or any(type(x) is not int or x <= 0 for x in activar)):
            raise ValueError('activar_sensores debe ser una lista de IDs de sensores.')
        uid = get_id_usuario()
        with sesion_rangos() as (conexion, cursor):
            permiso = permiso_rangos(cursor, device_id, uid)
            if not permiso or not permiso['puede_editar']:
                return jsonify(error='Necesitas permiso de control sobre este dispositivo.'), 403
            asegurar_tabla_perfil(cursor)
            cursor.execute('SELECT idDispositivo FROM dispositivos WHERE idDispositivo=%s FOR UPDATE', (device_id,))
            cursor.fetchone()
            cursor.execute('SELECT idSensore FROM sensores WHERE idDispositivo=%s ORDER BY idSensore FOR UPDATE', (device_id,))
            cursor.fetchall()
            previa = construir_previa(cursor, device_id, cultivo, etapa)
            disponibles = {x['idSensor'] for x in previa['sensores'] if x['idSensor'] is not None and x['disponible']}
            if activar is not None and not set(activar).issubset(disponibles):
                raise ValueError('Solo puedes activar sensores de este dispositivo con limites y unidades validos.')
            resumen = aplicar_previa(cursor, previa, set(activar) if activar is not None else None)
            cursor.execute("""
                INSERT INTO perfil_rangos_dispositivo (idDispositivo,cultivo,etapa) VALUES (%s,%s,%s)
                ON DUPLICATE KEY UPDATE cultivo=%s, etapa=%s, actualizado_en=NOW()
            """, (device_id, cultivo, etapa, cultivo, etapa))
            conexion.commit()
            return jsonify(status='success', perfil={'cultivo': cultivo, 'etapa': etapa}, sensores=resumen)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except Exception:
        app.logger.exception('Error guardando perfil de rangos')
        return jsonify(error='No se guardaron los rangos. Revisa MySQL y los permisos para crear la tabla de perfil.'), 500


@app.route('/api/rangos/simular/<int:id_sensor>', methods=['POST'])
@login_requerido
def simular_rangos(id_sensor):
    # Solo calcula; no inserta lecturas, alertas ni ordenes para actuadores.
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('Envia un objeto JSON.')
        cultivo, etapa = data.get('cultivo'), data.get('etapa')
        validar_perfil(cultivo, etapa)
        valores = data.get('valores')
        if not isinstance(valores, list) or not 1 <= len(valores) <= 30:
            raise ValueError('Envia entre 1 y 30 valores numericos.')
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in valores):
            raise ValueError('Todos los valores deben ser numeros finitos.')
        uid = get_id_usuario()
        with sesion_rangos() as (_, cursor):
            cursor.execute('SELECT idDispositivo FROM sensores WHERE idSensore=%s', (id_sensor,))
            sensor = cursor.fetchone()
            if not sensor or not permiso_rangos(cursor, sensor['idDispositivo'], uid):
                return jsonify(error='Sensor no encontrado o sin permiso.'), 403
            previa = construir_previa(cursor, sensor['idDispositivo'], cultivo, etapa)
            item = next(x for x in previa['sensores'] if x['idSensor'] == id_sensor)
            if not item['disponible']:
                raise ValueError(item['detalle'])
            reglas = [{'idParametro': 0, 'activo': 1, 'condicion': cond, 'valor_umbral': limite}
                      for cond, limite in (('menor_que', item['minimo']), ('mayor_que', item['maximo']))]
            return jsonify(simulacion=True, unidad=item['unidad'],
                           resultados=[dict(evaluar_reglas(v, reglas), valor=v) for v in valores])
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except Exception:
        app.logger.exception('Error simulando rangos')
        return jsonify(error='No se pudo realizar la simulacion.'), 500


PAGINA_RANGOS = r'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rangos · Hydrosense</title>
<style>
:root{font-family:system-ui,sans-serif;color:#213b30;background:#f3f7f4;color-scheme:light}
*{box-sizing:border-box}body{margin:0}main{max-width:1200px;margin:auto;padding:28px 20px}
header{display:flex;align-items:center;justify-content:space-between;gap:20px}
h1{margin:10px 0;font-size:1.8rem}h2{font-size:1.1rem;margin-top:0}a{color:#166347}
p{line-height:1.55}section{background:white;border:1px solid #d9e5dd;border-radius:12px;padding:22px;margin-top:20px}
.selectores{display:flex;flex-wrap:wrap;gap:18px}label{display:grid;gap:7px;flex:1;min-width:190px;font-weight:600}
select,button{font:inherit;padding:10px 12px;border-radius:7px;border:1px solid #a9bfb0}
select{width:100%;background:white;color:#213b30}button{cursor:pointer;background:#176448;color:white;font-weight:600}
button:disabled{cursor:default;opacity:.45}button.secundario{background:white;color:#176448}
.acciones{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-top:20px}
.nota,small{color:#526d5e;font-size:.9rem}small{display:block;margin-top:5px;max-width:300px}
.tabla{overflow:auto}table{width:100%;border-collapse:collapse;margin-top:12px;font-size:.94rem}
th,td{text-align:left;padding:12px 10px;border-bottom:1px solid #e5ece7;vertical-align:top}
th{font-size:.8rem;text-transform:uppercase;letter-spacing:.025em;white-space:nowrap}
td.numero{white-space:nowrap}input[type=checkbox]{width:20px;height:20px;accent-color:#176448}
.estado{display:inline-block;padding:4px 8px;border-radius:5px;background:#edf2ef;white-space:nowrap}
.estado.en_rango{background:#e3f4e7;color:#18502c}.estado.bajo,.estado.alto,.estado.alerta,.estado.rango_inconsistente{background:#fff0db;color:#7c4700}
#mensaje{min-height:1.5em;margin-bottom:0}#mensaje.error{color:#a22626}#mensaje.exito{color:#176448}
#perfilActual{margin-bottom:0}#prueba{white-space:pre-line}footer{margin-top:20px}
@media(max-width:600px){main{padding:18px 12px}section{padding:16px}header{align-items:flex-start}h1{font-size:1.5rem}}
</style>
</head>
<body><main>
<header><div><span class="nota">HYDROSENSE</span><h1>Rangos y alertas</h1></div><a href="/dashboard">Volver al panel</a></header>
<p>Elige el cultivo y la etapa para aplicar sus límites al dispositivo. Activa las alertas cuando hayas verificado las lecturas de cada sensor.</p>
<section aria-label="Selección del perfil">
<div class="selectores">
<label>Dispositivo<select id="dispositivo"><option value="">Cargando…</option></select></label>
<label>Cultivo<select id="cultivo" disabled><option value="">Selecciona…</option><option value="cilantro">Cilantro</option><option value="perejil">Perejil</option><option value="compartido">Cilantro y perejil — depósito compartido</option></select></label>
<label>Etapa<select id="etapa" disabled><option value="">Selecciona…</option><option value="general">General</option><option value="inicial">Inicial</option><option value="desarrollo">Desarrollo</option><option value="media">Media</option><option value="final">Final</option></select></label>
</div>
<p class="nota">La EC del cilantro necesita una etapa. En depósito compartido se usa esa etapa del cilantro y el intervalo general del perejil.</p>
<p id="perfilActual" class="nota"></p>
<p id="mensaje" role="status" aria-live="polite"></p>
</section>
<section>
<h2>Límites que se aplicarán</h2>
<p class="nota">“Estado actual” usa las alertas guardadas. “Probar” evalúa los límites seleccionados con valores de ejemplo.</p>
<div class="tabla"><table>
<thead><tr><th>Sensor</th><th>Mínimo</th><th>Máximo</th><th>Unidad</th><th>Última lectura</th><th>Estado actual</th><th>Activar alertas</th><th>Prueba</th></tr></thead>
<tbody id="filas"><tr><td colspan="8">Selecciona un dispositivo, un cultivo y una etapa.</td></tr></tbody>
</table></div>
<div class="acciones"><button id="guardar" disabled>Guardar rangos y alertas</button><span class="nota" id="permiso"></span></div>
<p class="nota">Temperatura del aire y humedad: límites provisionales de prueba. Temperatura del agua: referencia de estudios en aeroponía. La sonda de pH requiere calibración; luz y distancia requieren límites medidos en tu instalación.</p>
</section>
<section><h2>Iluminación</h2><p id="iluminacion">Selecciona un perfil para ver sus referencias.</p><p class="nota">El LDR actual entrega un índice relativo. Fotoperiodo y PPFD requieren mediciones específicas.</p></section>
<section id="panelPrueba" hidden><h2>Resultado de la simulación</h2><p id="prueba" role="status"></p></section>
<footer class="nota">Las lecturas se actualizan cada 10 segundos. Una lectura con más de 5 minutos se muestra como antigua.</footer>
</main>
<script>
'use strict';
const $ = id => document.getElementById(id);
const estadoTexto = {sin_calibrar:'Sin calibrar',sin_metadatos:'Calibración no verificada',sin_temperatura:'Sin temperatura válida',sin_sensor:'Sensor sin lectura',iniciando:'Iniciando',senal_baja:'Señal muy baja',fuera_rango:'Fuera del rango TDS',calibrando:'Calibrando',en_rango:'En rango',bajo:'Bajo',alto:'Alto',alerta:'Alerta',sin_datos:'Sin datos',sin_datos_recientes:'Sin datos recientes',sin_alertas_activas:'Alertas inactivas',lectura_invalida:'Lectura inválida',rango_inconsistente:'Revisar límites'};
const sensorTexto = {temperatura:'DHT22 · temperatura',humedad:'DHT22 · humedad',ph:'Sonda de pH',luz:'LDR',distancia:'HC-SR04',ec:'Conductividad eléctrica',temperatura_agua:'DS18B20 · agua'};
const formato = valor => valor === null || valor === undefined ? 'Pendiente' : Number(valor).toLocaleString('es-MX',{maximumFractionDigits:2});
let dispositivo = '', revision = 0, puedeEditar = false, guardando = false, consultando = false, previa = null;
let lecturas = new Map(), celdas = new Map(), casillas = new Map();
function mensaje(texto, tipo='') { $('mensaje').textContent = texto; $('mensaje').className = tipo; }
async function api(url, opciones={}) {
  const respuesta = await fetch(url,{credentials:'same-origin',...opciones});
  if (respuesta.status === 401) { window.location.assign('/'); throw new Error('Inicia sesión.'); }
  const dato = await respuesta.json();
  if (!respuesta.ok) throw new Error(dato.error || dato.mensaje || 'No se pudo completar la solicitud.');
  return dato;
}
function bloqueo() {
  $('guardar').disabled = guardando || !puedeEditar || !previa;
  $('dispositivo').disabled = guardando;
  $('cultivo').disabled = guardando || !dispositivo;
  $('etapa').disabled = guardando || !dispositivo;
  for (const {casilla, disponible} of casillas.values()) casilla.disabled = guardando || !puedeEditar || !disponible;
}
function limpiar() {
  previa = null; celdas.clear(); casillas.clear(); $('filas').replaceChildren();
  const fila = $('filas').insertRow(), celda = fila.insertCell(); celda.colSpan = 8;
  celda.textContent = 'Selecciona un dispositivo, un cultivo y una etapa.';
  $('iluminacion').textContent = 'Selecciona un perfil para ver sus referencias.';
  $('panelPrueba').hidden = true; bloqueo();
}
function mostrarLecturas() {
  for (const [id, {lectura, estado}] of celdas) {
    const dato = lecturas.get(id); lectura.replaceChildren();
    lectura.textContent = dato && dato.valor !== null ? formato(dato.valor) : 'Sin datos';
    if (dato && dato.fecha_hora) { const fecha = document.createElement('small'); fecha.textContent = dato.fecha_hora; lectura.append(fecha); }
    const nombre = dato ? dato.estado_rango : 'sin_datos';
    estado.textContent = estadoTexto[nombre] || nombre; estado.className = 'estado ' + nombre;
  }
}
function mostrarPrevia(dato) {
  previa = dato; $('filas').replaceChildren(); celdas.clear(); casillas.clear();
  for (const item of dato.sensores) {
    const fila = $('filas').insertRow(), sensor = fila.insertCell();
    sensor.textContent = sensorTexto[item.tipo] || item.tipo;
    const nota = document.createElement('small');
    nota.textContent = (item.idSensor === null ? 'Sin sensor registrado.' : 'ID ' + item.idSensor) + (item.detalle ? ' · ' + item.detalle : '');
    sensor.append(nota);
    if (item.otras_alertas.length) { const otras = document.createElement('small'); otras.textContent = 'Otras alertas activas: ' + item.otras_alertas.join(', '); sensor.append(otras); }
    for (const valor of [item.minimo,item.maximo]) { const celda = fila.insertCell(); celda.className = 'numero'; celda.textContent = formato(valor); }
    fila.insertCell().textContent = item.unidad || '—';
    const lectura = fila.insertCell(), estado = document.createElement('span'); fila.insertCell().append(estado);
    const activar = fila.insertCell(), prueba = fila.insertCell();
    if (item.idSensor !== null) {
      celdas.set(item.idSensor,{lectura,estado});
      const casilla = document.createElement('input'); casilla.type = 'checkbox';
      casilla.checked = item.disponible && item.activo_actual;
      casilla.setAttribute('aria-label','Activar alertas de ' + (sensorTexto[item.tipo] || item.tipo) + ', ID ' + item.idSensor);
      activar.append(casilla); casillas.set(item.idSensor,{casilla,disponible:item.disponible});
      const boton = document.createElement('button'); boton.textContent = 'Probar'; boton.className = 'secundario'; boton.disabled = !item.disponible;
      boton.addEventListener('click',()=>simular(item,boton)); prueba.append(boton);
    } else { lectura.textContent = 'Sin datos'; estado.textContent = 'Sin sensor'; estado.className = 'estado'; activar.textContent = '—'; prueba.textContent = '—'; }
  }
  const texto = [];
  for (const [variable, nombre] of [['fotoperiodo','Fotoperiodo'],['ppfd','PPFD']]) {
    const r = dato.iluminacion[variable];
    texto.push(nombre + ': ' + (r.disponible ? formato(r.minimo) + (r.minimo === r.maximo ? '' : '–' + formato(r.maximo)) + ' ' + r.unidad : 'sin límites para este perfil'));
  }
  $('iluminacion').textContent = texto.join(' · '); mostrarLecturas(); bloqueo();
}
async function cargarPrevia() {
  const turno = ++revision; limpiar(); mensaje('');
  if (!dispositivo || !$('cultivo').value || !$('etapa').value) return;
  mensaje('Consultando límites…');
  try {
    const consulta = new URLSearchParams({cultivo:$('cultivo').value,etapa:$('etapa').value});
    const dato = await api('/api/rangos/previa/' + dispositivo + '?' + consulta);
    if (turno !== revision) return;
    mostrarPrevia(dato); mensaje('Revisa los límites y las casillas antes de guardar.');
  } catch (error) { if (turno === revision) mensaje(error.message,'error'); }
}
function recibirEstado(dato) {
  puedeEditar = dato.puede_editar; lecturas = new Map(dato.sensores.map(s=>[s.idSensor,s]));
  $('permiso').textContent = puedeEditar ? 'Puedes modificar este dispositivo.' : 'Acceso de consulta.';
  $('perfilActual').textContent = dato.perfil ? 'Perfil guardado: ' + dato.perfil.cultivo + ' · ' + dato.perfil.etapa : 'Todavía no hay un perfil guardado.';
  mostrarLecturas(); bloqueo();
}
async function cambiarDispositivo() {
  const turno = ++revision; dispositivo = $('dispositivo').value; puedeEditar = false; lecturas.clear();
  $('cultivo').value = ''; $('etapa').value = ''; $('perfilActual').textContent = ''; $('permiso').textContent = ''; limpiar(); mensaje('');
  if (!dispositivo) return;
  try {
    const dato = await api('/api/rangos/dispositivo/' + dispositivo);
    if (turno !== revision) return;
    recibirEstado(dato);
    if (dato.perfil) { $('cultivo').value = dato.perfil.cultivo; $('etapa').value = dato.perfil.etapa; await cargarPrevia(); }
  } catch (error) { if (turno === revision) mensaje(error.message,'error'); }
}
async function refrescar() {
  if (!dispositivo || guardando || consultando || document.hidden) return;
  const seleccionado = dispositivo; consultando = true;
  try { const dato = await api('/api/rangos/dispositivo/' + seleccionado); if (seleccionado === dispositivo && !guardando) recibirEstado(dato); }
  catch (error) { if (seleccionado === dispositivo) mensaje(error.message,'error'); }
  finally { consultando = false; }
}
async function guardar() {
  if (!previa || !puedeEditar || guardando) return;
  const seleccion = {cultivo:previa.cultivo,etapa:previa.etapa,activar_sensores:[...casillas].filter(([,v])=>v.casilla.checked && v.disponible).map(([id])=>id)};
  guardando = true; bloqueo(); mensaje('Guardando…');
  try {
    await api('/api/rangos/aplicar/' + dispositivo,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(seleccion)});
    recibirEstado(await api('/api/rangos/dispositivo/' + dispositivo));
    await cargarPrevia(); mensaje('Rangos y alertas guardados. Se evaluarán con las próximas lecturas.','exito');
  } catch (error) { mensaje(error.message,'error'); }
  finally { guardando = false; bloqueo(); }
}
async function simular(item, boton) {
  if (!previa) return;
  const turno = revision, perfil = {cultivo:previa.cultivo,etapa:previa.etapa};
  const paso = Math.max(.01,(item.maximo-item.minimo)/10);
  const valores = [item.minimo-paso,(item.minimo+item.maximo)/2,item.maximo+paso].map(v=>Number(v.toFixed(2)));
  boton.disabled = true;
  try {
    const dato = await api('/api/rangos/simular/' + item.idSensor,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...perfil,valores})});
    if (turno !== revision) return;
    $('panelPrueba').hidden = false;
    $('prueba').textContent = (sensorTexto[item.tipo] || item.tipo) + '\n' + dato.resultados.map(r=>formato(r.valor) + ' ' + dato.unidad + ' → ' + estadoTexto[r.estado_rango]).join('\n') + '\nValores de ejemplo; el historial conserva las lecturas reales.';
  } catch (error) { if (turno === revision) mensaje(error.message,'error'); }
  finally { boton.disabled = !item.disponible; }
}
async function iniciar() {
  try {
    const lista = await api('/api/dispositivos/lista'); $('dispositivo').replaceChildren(new Option('Selecciona…',''));
    for (const d of lista) $('dispositivo').add(new Option(d.nombre + ' · ID ' + d.idDispositivo,String(d.idDispositivo)));
    if (!lista.length) mensaje('Tu cuenta no tiene dispositivos disponibles.');
    if (lista.length === 1) { $('dispositivo').value = String(lista[0].idDispositivo); await cambiarDispositivo(); }
  } catch (error) { mensaje(error.message,'error'); }
}
$('dispositivo').addEventListener('change',cambiarDispositivo);
$('cultivo').addEventListener('change',cargarPrevia);
$('etapa').addEventListener('change',cargarPrevia);
$('guardar').addEventListener('click',guardar);
iniciar(); setInterval(refrescar,10000);
</script></body></html>'''


@app.route('/rangos')
@login_requerido
def pagina_rangos():
    return render_template_string(PAGINA_RANGOS)


# ============================================================
# SENSORES DE AGUA — 06/10/2026
# DS18B20 y EC/TDS del módulo TDS Meter V1.0. Protocolo 2.
# Implementación incluida aquí: no necesita sensores_agua.py.
# Preparación de BD: python app.py --preparar-agua
# El alta de sensores es explícita, idempotente y solo del propietario.
# ============================================================
TIPOS_AGUA = ('ec', 'temperatura_agua')
ESTADOS_AGUA = {'ok','sin_calibrar','sin_temperatura','sin_sensor','iniciando',
           'senal_baja','fuera_rango','calibrando','lectura_invalida','sin_metadatos'}


def _agua_unidad(u):
    s=str(u or '').lower().replace('µ','u').replace('μ','u')
    return ''.join(c for c in unicodedata.normalize('NFKD',s) if not unicodedata.combining(c)).replace('°','').replace(' ','')


def _agua_numero(v):
    if v is None or isinstance(v,bool): raise ValueError('Lectura no numérica')
    n=float(v)
    if not math.isfinite(n): raise ValueError('Lectura no finita')
    return n


def _agua_validar_unidad(tipo,u):
    if _agua_unidad(u) not in ({'ms/cm','us/cm'} if tipo=='ec' else {'c'}):
        raise ValueError('EC requiere mS/cm o µS/cm; temperatura del agua requiere °C.')


def _agua_preparar(tipo,valor,meta,origen,destino):
    _agua_validar_unidad(tipo,destino)
    d=dict(estado='sin_metadatos',calibrado=False,voltaje=None,tds_ppm=None,factor_k=None,temperatura_c=None)
    if meta is None:
        # El firmware anterior enviaba el valor en la unidad del registro.
        # Si declara una unidad de origen, convertirla también sin diagnóstico.
        if valor is None:
            return None,d
        v=_agua_numero(valor)
        if tipo=='temperatura_agua':
            if origen is not None:_agua_validar_unidad(tipo,origen)
            if not -55<=v<=125 or v==85:
                d['estado']='lectura_invalida';return None,d
            return round(v,2),d
        if v<0:
            d['estado']='lectura_invalida';return None,d
        if origen is not None:
            _agua_validar_unidad(tipo,origen)
            v*=1 if _agua_unidad(origen)==_agua_unidad(destino) else (1000 if _agua_unidad(destino)=='us/cm' else 0.001)
        return round(v,4),d
    if not isinstance(meta,dict) or meta.get('estado') not in ESTADOS_AGUA:
        raise ValueError('Diagnóstico de agua inválido')
    d['estado']=meta['estado']
    if tipo=='temperatura_agua':
        if _agua_unidad(origen)!='c': raise ValueError('Declarar temperatura_agua en °C')
        if d['estado']!='ok': return None,d
        v=_agua_numero(valor)
        if not -55<=v<=125 or v==85:
            d['estado']='lectura_invalida';return None,d
        return round(v,2),d
    if _agua_unidad(origen)!='ms/cm': raise ValueError('El protocolo v2 declara EC en mS/cm')
    d['calibrado']=meta.get('calibrado') is True
    for k in ('voltaje','tds_ppm','factor_k','temperatura_c'):
        if meta.get(k) is not None:
            d[k]=_agua_numero(meta[k])
            if abs(d[k])>100000: raise ValueError('Diagnóstico fuera de límites físicos')
    if d['estado'] not in ('ok','sin_calibrar'): return None,d
    if not d['calibrado']: d['estado']='sin_calibrar'
    v=_agua_numero(valor)
    if d['temperatura_c'] is None or not 0<=d['temperatura_c']<=55:
        d['estado']='sin_temperatura';return None,d
    if d['voltaje'] is None or d['factor_k'] is None or not 0.5<=d['factor_k']<=2:
        d['estado']='lectura_invalida';return None,d
    if d['voltaje']<=0.03:
        d['estado']='senal_baja';return None,d
    if d['voltaje']>=2.3 or not 0<=v<2 or d['tds_ppm'] is None or not 0<=d['tds_ppm']<1000:
        d['estado']='fuera_rango';return None,d
    if abs(d['tds_ppm']-v*500)>1: raise ValueError('EC/TDS incompatibles con el factor 0.5')
    return round(v*(1000 if _agua_unidad(destino)=='us/cm' else 1),4),d


def _agua_instalar(n):
    """Se instala al terminar de definir las rutas existentes de app.py."""
    app=n['app'];login=n['login_requerido']
    @contextmanager
    def bd():
        cx=n['conectar_bd']();cur=None
        try:
            cur=cx.cursor(dictionary=True);yield cx,cur
        except Exception:
            cx.rollback();raise
        finally:
            if cur is not None:cur.close()
            cx.close()

    def guardar(id_sensor,v,d):
        with bd() as (cx,c):
            c.execute('SELECT idSensore FROM sensores WHERE idSensore=%s FOR UPDATE',(id_sensor,))
            if not c.fetchone():raise ValueError('Sensor no encontrado')
            fecha=datetime.now();rid=None
            if v is not None:
                c.execute('INSERT INTO registro_sensores (idSensor,valor,fecha_hora) VALUES (%s,%s,%s)',(id_sensor,v,fecha));rid=c.lastrowid
            c.execute('''INSERT INTO mediciones_agua
                (idSensor,idRegistro_sensor,estado,calibrado,voltaje,tds_ppm,factor_k,temperatura_c,fecha_hora)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (id_sensor,rid,d['estado'],int(d['calibrado']),d['voltaje'],d['tds_ppm'],d['factor_k'],d['temperatura_c'],fecha))
            # Una muestra provisional o fallida no abre ni resuelve incidentes agronómicos.
            if d['estado']=='ok' and v is not None:
                c.execute('SELECT idParametro,nombre,condicion,valor_umbral,prioridad FROM parametros_alerta WHERE idSensor=%s AND activo=1',(id_sensor,))
                for p in c.fetchall():
                    if n['cumple_condicion'](v,p['condicion'],float(p['valor_umbral'])):
                        c.execute("SELECT idHistorial FROM historial_alertas WHERE idParametro=%s AND estado IN ('nueva','vista') LIMIT 1",(p['idParametro'],))
                        if not c.fetchone():
                            msg=f"Sensor {id_sensor}: valor {v} {p['condicion'].replace('_',' ')} umbral {p['valor_umbral']}"
                            c.execute("INSERT INTO historial_alertas (idParametro,idSensor,valor_detectado,prioridad,estado,mensaje) VALUES (%s,%s,%s,%s,'nueva',%s)",
                                      (p['idParametro'],id_sensor,v,p['prioridad'],msg))
                    else:n['resolver_incidentes'](c,p['idParametro'])
            cx.commit()

    def ultima(c,sid):
        try:
            c.execute('''SELECT m.estado AS estado_medicion,m.calibrado,m.voltaje,m.tds_ppm,m.factor_k,m.temperatura_c,m.fecha_hora,r.valor
                FROM mediciones_agua m LEFT JOIN registro_sensores r ON r.idRegistro_sensor=m.idRegistro_sensor
                WHERE m.idSensor=%s ORDER BY m.fecha_hora DESC,m.idMedicion DESC LIMIT 1''',(sid,))
            return c.fetchone()
        except mysql.connector.Error as e:
            if e.errno==1146:return None
            raise

    def calidad(c,sid,dato):
        salida=dict(dato)
        salida.update(n['evaluar_lectura'](dato.get('valor'),dato.get('fecha_hora'),n['reglas_sensor'](c,sid)))
        salida['estado_medicion']=dato.get('estado_medicion') or 'sin_metadatos'
        fecha=dato.get('fecha_hora')
        salida['edad_segundos']=max(0,(datetime.now()-fecha).total_seconds()) if isinstance(fecha,datetime) else None
        if salida['edad_segundos'] is not None and salida['edad_segundos']>30:
            salida['estado_rango']='sin_datos_recientes'
        elif salida['estado_medicion']!='ok':salida['estado_rango']=salida['estado_medicion']
        return salida

    def sensor_permitido(c,sid):
        c.execute('SELECT idSensore,idDispositivo,tipo_sensor,unidad_medida FROM sensores WHERE idSensore=%s',(sid,))
        s=c.fetchone()
        if not s or not n['permiso_rangos'](c,s['idDispositivo'],n['get_id_usuario']()):return None
        return s

    def datos_arduino():
        data=request.get_json(silent=True)
        if not isinstance(data,dict) or data.get('token')!=n['ARDUINO_TOKEN']:
            return jsonify(error='No autorizado'),401
        did=data.get('device_id');sens=data.get('sensores')
        if isinstance(did,bool) or not isinstance(did,int) or did<=0:return jsonify(error='device_id inválido'),400
        if not isinstance(sens,dict) or not sens:return jsonify(error='Sin datos de sensores'),400
        metas=data.get('diagnostico',{});units=data.get('unidades',{})
        if not isinstance(metas,dict) or not isinstance(units,dict):return jsonify(error='Diagnóstico o unidades inválidos'),400
        if data.get('protocolo') not in (None,2):
            return jsonify(error='Versión de protocolo no compatible'),400
        if 'ec' in sens and 'temperatura_agua' in sens and isinstance(metas.get('ec'),dict):
            ecmeta=dict(metas['ec'])
            if ecmeta.get('estado') in ('ok','sin_calibrar'):
                tmeta=metas.get('temperatura_agua')
                try:
                    t=_agua_numero(sens['temperatura_agua'])
                    tc=_agua_numero(ecmeta.get('temperatura_c'))
                    valida=isinstance(tmeta,dict) and tmeta.get('estado')=='ok' and 0<=t<=55 and abs(t-tc)<=0.2
                except (ValueError,TypeError,OverflowError):
                    valida=False
                if not valida:
                    ecmeta['estado']='sin_temperatura'
                    metas=dict(metas,ec=ecmeta)
        guardados=[];errores=[]
        try:
            with bd() as (_,c):
                for raw,vraw in sens.items():
                    tipo=raw.strip().lower()
                    c.execute('SELECT idSensore,unidad_medida FROM sensores WHERE idDispositivo=%s AND LOWER(TRIM(tipo_sensor))=%s ORDER BY idSensore',(did,tipo))
                    filas=c.fetchall()
                    if len(filas)!=1:
                        errores.append(tipo+(': sensor no registrado' if not filas else ': registros duplicados; revisar Hardware'));continue
                    s=filas[0]
                    try:
                        estado='ok'
                        if tipo in TIPOS_AGUA:
                            v,d=_agua_preparar(tipo,vraw,metas.get(tipo),units.get(tipo),s['unidad_medida'])
                            guardar(s['idSensore'],v,d);estado=d['estado']
                        else:
                            if vraw is None:continue
                            v=n['convertir_valor'](vraw,n['TIPO_CONV_MAP'].get(tipo))
                            n['guardar_lectura_bd'](s['idSensore'],v)
                        guardados.append(dict(tipo=tipo,id=s['idSensore'],valor=v,estado=estado))
                    except mysql.connector.Error as e:
                        errores.append(tipo+(': ejecutar python app.py --preparar-agua' if e.errno==1146 else ': error al almacenar'))
                    except (ValueError,TypeError,OverflowError) as e:errores.append(f'{tipo}: {e}')
            return jsonify(status='parcial' if errores else 'ok',guardados=guardados,errores=errores)
        except Exception:
            app.logger.exception('Error recibiendo sensores');return jsonify(error='No se pudieron recibir los datos'),500
    app.view_functions['arduino_datos']=datos_arduino

    @app.post('/api/dispositivos/<int:device_id>/integrar-sensores-agua')
    @login
    def integrar_agua(device_id):
        try:
            with bd() as (cx,c):
                c.execute('SELECT idDispositivo FROM dispositivos WHERE idDispositivo=%s AND idUsuario=%s FOR UPDATE',(device_id,n['get_id_usuario']()))
                if not c.fetchone():return jsonify(error='Solo el propietario puede registrar estos sensores'),403
                c.execute('SELECT idMedicion FROM mediciones_agua LIMIT 0');c.fetchall()
                c.execute('SELECT idSensore,tipo_sensor,unidad_medida FROM sensores WHERE idDispositivo=%s',(device_id,));todos=c.fetchall();out=[]
                for tipo,u in (('ec','mS/cm'),('temperatura_agua','°C')):
                    filas=[s for s in todos if s['tipo_sensor'].strip().lower()==tipo]
                    if len(filas)>1:raise ValueError(f'Hay varios sensores {tipo}; revisar IDs antes de continuar')
                    if filas:
                        s=filas[0];_agua_validar_unidad(tipo,s['unidad_medida']);sid=s['idSensore'];accion='existente'
                    else:
                        c.execute('INSERT INTO sensores (tipo_sensor,unidad_medida,idDispositivo) VALUES (%s,%s,%s)',(tipo,u,device_id));sid=c.lastrowid;accion='creado'
                    out.append(dict(tipo=tipo,id=sid,accion=accion))
                cx.commit();return jsonify(status='success',sensores=out)
        except ValueError as e:return jsonify(error=str(e)),409
        except mysql.connector.Error as e:
            if e.errno==1146:return jsonify(error='Primero ejecuta: python app.py --preparar-agua'),503
            app.logger.exception('Error al integrar agua');return jsonify(error='No se pudieron registrar los sensores'),500

    original_actual=app.view_functions['datos_actuales']
    @login
    def actual(id_sensor):
        with bd() as (_,c):
            s=sensor_permitido(c,id_sensor)
            if not s:return jsonify(error='Sensor no encontrado'),404
            if s['tipo_sensor'].strip().lower() not in TIPOS_AGUA:return original_actual(id_sensor)
            dato=ultima(c,id_sensor)
            if not dato:
                c.execute('SELECT valor,fecha_hora FROM registro_sensores WHERE idSensor=%s ORDER BY fecha_hora DESC,idRegistro_sensor DESC LIMIT 1',(id_sensor,));dato=c.fetchone()
            if not dato:return jsonify(error='Sin datos disponibles'),404
            return jsonify(dict(calidad(c,id_sensor,dato),unidad=s['unidad_medida']))
    app.view_functions['datos_actuales']=actual
    original_estado_sensor=app.view_functions['verificar_estado']
    @login
    def estado(id_sensor):
        with bd() as (_,c):
            s=sensor_permitido(c,id_sensor)
            if not s:return jsonify(error='Sensor no encontrado'),404
            if s['tipo_sensor'].strip().lower() not in TIPOS_AGUA:
                return original_estado_sensor(id_sensor)
        r=app.make_response(actual(id_sensor))
        if r.status_code!=200:return r
        d=r.get_json();activo=d.get('valor') is not None and d.get('estado_rango')!='sin_datos_recientes' and d.get('estado_medicion') in (None,'ok','sin_calibrar')
        return jsonify(activo=activo,ultima_lectura=d,estado_medicion=d.get('estado_medicion'))
    app.view_functions['verificar_estado']=estado

    original_analitica=app.view_functions['obtener_analitica']
    @login
    def analitica(id_sensor,rango):
        try:
            with bd() as (_,c):
                s=sensor_permitido(c,id_sensor)
                if not s:return jsonify(error='No autorizado'),403
                if s['tipo_sensor'].strip().lower() not in TIPOS_AGUA:
                    return original_analitica(id_sensor,rango)
                from datetime import timedelta
                inicio=datetime.now()-timedelta(hours={'24h':24,'semana':168}.get(rango,1))
                try:
                    c.execute('''SELECT r.idRegistro_sensor,r.fecha_hora,r.valor,
                        COALESCE(m.estado,'sin_metadatos') AS estado_medicion,m.idMedicion,
                        m.calibrado,m.tds_ppm,m.voltaje,m.temperatura_c
                        FROM registro_sensores r LEFT JOIN mediciones_agua m
                        ON m.idRegistro_sensor=r.idRegistro_sensor
                        WHERE r.idSensor=%s AND r.fecha_hora>=%s
                        ORDER BY r.fecha_hora,r.idRegistro_sensor''',(id_sensor,inicio))
                    datos=c.fetchall()
                    c.execute('''SELECT fecha_hora,NULL AS valor,estado AS estado_medicion,
                        calibrado,tds_ppm,voltaje,temperatura_c,idMedicion
                        FROM mediciones_agua WHERE idSensor=%s AND fecha_hora>=%s
                        AND idRegistro_sensor IS NULL ORDER BY fecha_hora,idMedicion''',(id_sensor,inicio))
                    datos+=c.fetchall()
                except mysql.connector.Error as e:
                    if e.errno!=1146:raise
                    c.execute('''SELECT fecha_hora,valor FROM registro_sensores
                        WHERE idSensor=%s AND fecha_hora>=%s ORDER BY fecha_hora,idRegistro_sensor''',(id_sensor,inicio))
                    datos=[dict(d,estado_medicion='sin_metadatos') for d in c.fetchall()]
                datos.sort(key=lambda d:(d['fecha_hora'],d.get('idMedicion') or 0,d.get('idRegistro_sensor') or 0))
                for d in datos:
                    d.pop('idRegistro_sensor',None);d.pop('idMedicion',None)
                    if isinstance(d['fecha_hora'],datetime):
                        d['fecha_hora']=d['fecha_hora'].strftime('%Y-%m-%d %H:%M:%S')
                return jsonify(datos)
        except Exception:
            app.logger.exception('Error consultando historial de agua')
            return jsonify(error='No se pudo consultar el historial de agua'),500
    app.view_functions['obtener_analitica']=analitica

    original_estado=n['estado_dispositivo']
    def estado_dispositivo(c,did):
        datos=original_estado(c,did)
        for d in datos:
            if d['tipo_sensor'].strip().lower() in TIPOS_AGUA:
                nueva=ultima(c,d['idSensor'])
                if nueva:d.update(calidad(c,d['idSensor'],nueva))
                else:d.update(estado_medicion='sin_metadatos',estado_rango='sin_metadatos')
        return datos
    n['estado_dispositivo']=estado_dispositivo
    original_previa=n['construir_previa']
    def previa(c,did,cultivo,etapa):
        r=original_previa(c,did,cultivo,etapa)
        for s in r['sensores']:
            if s['tipo']=='ec' and s['disponible']:
                factor=n['factor_unidades'](s['unidad'],'mS/cm')
                if factor is not None and s['maximo']*factor>=2:
                    s.update(disponible=False,detalle='El perfil alcanza o supera 2.0 mS/cm: requiere un instrumento de mayor rango. No se modificaron los límites del cultivo.')
        return r
    n['construir_previa']=previa

    original_agregar=app.view_functions['agregar_sensor']
    @login
    def agregar():
        d=request.get_json(silent=True) or {};tipo=str(d.get('tipo','')).strip().lower()
        if tipo not in TIPOS_AGUA:return original_agregar()
        try:
            _agua_validar_unidad(tipo,d.get('unidad'))
            with bd() as (cx,c):
                c.execute('SELECT idDispositivo FROM dispositivos WHERE idDispositivo=%s AND idUsuario=%s FOR UPDATE',(d.get('idDispositivo'),n['get_id_usuario']()))
                if not c.fetchone():return jsonify(status='error',mensaje='Dispositivo no encontrado'),404
                c.execute('SELECT idSensore FROM sensores WHERE idDispositivo=%s AND LOWER(TRIM(tipo_sensor))=%s',(d['idDispositivo'],tipo))
                if c.fetchall():return jsonify(status='error',mensaje='Ese sensor ya existe en el dispositivo'),409
                c.execute('INSERT INTO sensores (tipo_sensor,unidad_medida,idDispositivo) VALUES (%s,%s,%s)',(tipo,d['unidad'],d['idDispositivo']));cx.commit()
                return jsonify(status='success')
        except ValueError as e:return jsonify(status='error',mensaje=str(e)),400
    app.view_functions['agregar_sensor']=agregar
    original_editar=app.view_functions['editar_sensor']
    @login
    def editar(id_sensor):
        d=request.get_json(silent=True) or {}
        with bd() as (_,c):
            s=sensor_permitido(c,id_sensor)
            if not s:return jsonify(status='error',mensaje='Sensor no encontrado'),404
            old=s['tipo_sensor'].strip().lower();new=str(d.get('tipo','')).strip().lower()
            if old in TIPOS_AGUA or new in TIPOS_AGUA:
                if old!=new or _agua_unidad(s['unidad_medida'])!=_agua_unidad(d.get('unidad')):
                    return jsonify(status='error',mensaje='Se conserva el tipo y la unidad del historial. Usa Integrar sensores de agua para agregar el sensor faltante.'),409
        return original_editar(id_sensor)
    app.view_functions['editar_sensor']=editar


SQL_TABLA_MEDICIONES_AGUA = 'CREATE TABLE IF NOT EXISTS mediciones_agua (\n idMedicion BIGINT NOT NULL AUTO_INCREMENT,\n idSensor INT NOT NULL,\n idRegistro_sensor INT NULL,\n estado VARCHAR(32) NOT NULL,\n calibrado TINYINT(1) NOT NULL DEFAULT 0,\n voltaje DECIMAL(8,5) NULL,\n tds_ppm DECIMAL(12,4) NULL,\n factor_k DECIMAL(10,6) NULL,\n temperatura_c DECIMAL(7,3) NULL,\n fecha_hora DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,\n PRIMARY KEY (idMedicion),\n UNIQUE KEY uq_agua_registro (idRegistro_sensor),\n KEY idx_agua_sensor_fecha (idSensor,fecha_hora,idMedicion),\n CONSTRAINT fk_agua_sensor FOREIGN KEY (idSensor) REFERENCES sensores(idSensore) ON DELETE CASCADE,\n CONSTRAINT fk_agua_registro FOREIGN KEY (idRegistro_sensor) REFERENCES registro_sensores(idRegistro_sensor) ON DELETE CASCADE\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;'


def preparar_bd_agua():
    """Crear la tabla adicional de diagnósticos. Solo por comando explícito."""
    conexion=cursor=None
    try:
        conexion=conectar_bd()
        cursor=conexion.cursor()
        cursor.execute(SQL_TABLA_MEDICIONES_AGUA)
        conexion.commit()
    except Exception:
        if conexion is not None:conexion.rollback()
        raise
    finally:
        if cursor is not None:cursor.close()
        if conexion is not None:conexion.close()


_agua_instalar(globals())


# =====================================================================
# ACTIVIDAD 2 — CICLO DE NEBULIZACIÓN POR ETAPA + AJUSTE CLIMÁTICO
# (10/10/2026). Se instala sobre get_relay(): el Arduino y el dashboard
# reciben los tiempos efectivos SIN cambiar el formato de la respuesta.
# Si algo falla, se usa el temporizador guardado (nunca rompe el riego).
# Revisado 11/10/2026: altitud oficial de INEGI y fuentes de aeroponía
# con su página.
#
# Fuentes:
#  [Y25]   Yang, Xiao, Jiang y Luo (2025). Calculation and Prediction of
#          Water Requirements for Aeroponic Cultivation of Crops in
#          Greenhouses. Horticulturae 11, 1034.
#          https://doi.org/10.3390/horticulturae11091034
#  [H19]   Hoyos, Candelo-Becerra y Chavarria (2019). Automation of
#          pesticide-free cilantro aeroponic crops. INGE CUC 15(1), 123-132.
#          https://doi.org/10.17981/ingecuc.15.1.2019.11
#  [R22]   Rincón, Candelo-Becerra y Hoyos (2022). Modelling of phosphorus
#          accumulation in an aeroponic coriander crop. Int J Agric & Biol
#          Eng 15(6), 73-79. https://doi.org/10.25165/j.ijabe.20221506.6760
#  [G26]   Gong et al. (2026). Multi-Temporal Spectral Characteristics of
#          Evapotranspiration... Agronomy 16, 1040.
#  [B24]   Evaluating Evapotranspiration in a Commercial Greenhouse...
#          bioRxiv, doi 10.1101/2024.01.11.575151 (2024).
#  [FAO56] Allen et al. (1998), Irrigation and Drainage Paper 56,
#          citado por Y25 (ref. 22) y G26 (ref. 28).
#  [INEGI] INEGI (2018). Anuario estadístico y geográfico de Veracruz,
#          cuadro 1.2: altitud de las cabeceras municipales.
# =====================================================================
import time

# [Y25] §2.4, p. 5: intervalos de nebulización por etapa, (ON s, OFF s).
# Los autores los fijaron con ensayos preliminares de cilantro aeropónico.
CICLO_ETAPA_Y25 = {
    'inicial':    (60, 600),   # 1 min ON / 10 min OFF
    'desarrollo': (60, 300),   # 1 min ON /  5 min OFF
    'media':      (120, 300),  # 2 min ON /  5 min OFF
    'final':      (60, 300),   # 1 min ON /  5 min OFF
}
# [Y25] solo trabajó con cilantro. Ninguna fuente del proyecto reporta
# un ciclo para perejil: con perfil 'perejil' se conserva el temporizador.
CULTIVOS_CICLO_Y25 = ('cilantro', 'compartido')

# [H19] §II.C, p. 125: 20 s ON / 160 s OFF durante todo el cultivo, en
# Medellín (1 479-1 900 msnm, clima subtropical estable todo el año).
# [R22] §3.1, p. 74: el mismo ciclo sostuvo 42 días de cilantro en el
# invernadero de 1 479 msnm; p. 77: hasta 5 h sin riego estresan la planta.
# Es el apagado más corto validado en las fuentes: piso del ajuste climático.
OFF_MINIMO_H19 = 160

# [Y25] §2.5, p. 6 (Figs. 2-4): clima medio del invernadero en que se validaron
# los ciclos: 11.5 °C, 77.7 % HR y 0.1017 MPa (= 101.7 kPa).
CLIMA_REF_Y25 = (11.5, 77.7, 101.7)

# Altitud del módulo: Xalapa-Enríquez, 1 392 msnm [INEGI]. Solo afecta a γ
# ([FAO56] ec. 7-8). Se puede cambiar con ALTITUD_M en el .env.
ALTITUD_M = float(os.environ.get('ALTITUD_M', '1392'))

# [G26]: Rn y DPV dominan la ET a escala diaria. [B24]: con valores
# momentáneos la estimación empeora por histéresis; la diaria es mejor.
# Por eso el ajuste usa el promedio de las últimas 24 h, no la lectura actual.
VENTANA_CLIMA_H = 24
# Criterio de ingeniería (sin fuente): las lecturas deben cubrir al menos
# 12 h de la ventana para que el promedio incluya día y noche.
COBERTURA_MIN_H = 12
CACHE_CICLO_S = 600            # el ajuste es diario: recalcular cada 10 min basta
AJUSTE_CLIMA_ACTIVO = os.environ.get('AJUSTE_CLIMA', '1') != '0'


def presion_por_altitud_kpa(z_m):
    """[FAO56] ec. 7."""
    return 101.3 * ((293.0 - 0.0065 * z_m) / 293.0) ** 5.26


def demanda_aerodinamica(t, hr, p_kpa):
    """Término aerodinámico de la ec. 7 de [Y25] (Penman-Monteith revisada
    para viento ~0 dentro del invernadero). Devuelve (término, DPV en kPa).
    SUPUESTO: se omite el término de radiación porque el módulo no mide
    radiación neta (el LDR no es un radiómetro). Solo se usa como razón
    entre el clima actual y el del ensayo de Y25."""
    es = 0.6108 * math.exp(17.27 * t / (t + 237.3))   # [FAO56] ec. 11 / [G26] ec. 1
    ea = es * hr / 100.0                              # [G26] ec. 2
    dpv = max(0.0, es - ea)                           # [G26] ec. 3
    delta = 4098.0 * es / (t + 237.3) ** 2            # [FAO56] ec. 13
    gamma = 0.000665 * p_kpa                          # [FAO56] ec. 8
    return gamma * 1713.0 * dpv / (t + 273.0) / (delta + 1.64 * gamma), dpv


DEMANDA_REF_Y25 = demanda_aerodinamica(*CLIMA_REF_Y25)[0]


def clima_ultimas_24h(cursor, device_id):
    """Promedios de temperatura y HR del DHT22 en la ventana diaria.
    Descarta lecturas físicamente imposibles. None si no hay cobertura."""
    cursor.execute("""
        SELECT LOWER(TRIM(s.tipo_sensor)) AS tipo, COUNT(*) AS n, AVG(r.valor) AS media,
               TIMESTAMPDIFF(MINUTE, MIN(r.fecha_hora), MAX(r.fecha_hora)) AS minutos
        FROM sensores s JOIN registro_sensores r ON r.idSensor = s.idSensore
        WHERE s.idDispositivo = %s
          AND r.fecha_hora >= NOW() - INTERVAL %s HOUR
          AND ((LOWER(TRIM(s.tipo_sensor)) = 'temperatura' AND r.valor BETWEEN -10 AND 60)
            OR (LOWER(TRIM(s.tipo_sensor)) = 'humedad' AND r.valor BETWEEN 1 AND 100))
        GROUP BY LOWER(TRIM(s.tipo_sensor))
    """, (device_id, VENTANA_CLIMA_H))
    filas = {f['tipo']: f for f in cursor.fetchall()}
    t, h = filas.get('temperatura'), filas.get('humedad')
    if not t or not h:
        return None
    if min(t['minutos'] or 0, h['minutos'] or 0) < COBERTURA_MIN_H * 60:
        return None
    return {'temperatura': round(float(t['media']), 2), 'humedad': round(float(h['media']), 2),
            'lecturas': int(min(t['n'], h['n'])),
            'horas_cubiertas': round(min(t['minutos'], h['minutos']) / 60.0, 1)}


def calcular_ciclo(etapa, clima):
    """Ciclo [Y25] de la etapa, con el OFF ajustado a la demanda del clima.
    factor = demanda del ensayo / demanda actual, nunca mayor que 1:
    el apagado solo se ACORTA (nunca se deja la raíz más tiempo sin
    niebla que en el ensayo validado) y nunca baja de 160 s [H19]."""
    on, off_base = CICLO_ETAPA_Y25[etapa]
    detalle = {'tiempo_on': on, 'tiempo_off_base': off_base, 'factor_clima': 1.0,
               'clima_24h': clima, 'ajuste': 'sin_ajuste'}
    off = off_base
    if clima:
        demanda, dpv = demanda_aerodinamica(clima['temperatura'], clima['humedad'],
                                            presion_por_altitud_kpa(ALTITUD_M))
        clima['dpv_kpa'] = round(dpv, 3)
        if demanda > 0:
            factor = min(1.0, DEMANDA_REF_Y25 / demanda)
            off = max(min(OFF_MINIMO_H19, off_base), int(round(off_base * factor)))
            detalle.update(factor_clima=round(factor, 3),
                           ajuste='piso_H19' if off == OFF_MINIMO_H19 and off_base * factor < OFF_MINIMO_H19
                           else ('demanda_mayor' if factor < 1 else 'igual_o_menor_que_Y25'))
    detalle['tiempo_off'] = off
    return detalle


_ciclo_cache = {}
_ciclo_lock = threading.Lock()


def ciclo_riego_efectivo(device_id):
    """Ciclo por etapa del dispositivo, o None si su perfil no tiene uno
    documentado (sin perfil, etapa 'general' o cultivo 'perejil')."""
    ahora = time.monotonic()
    with _ciclo_lock:
        guardado = _ciclo_cache.get(device_id)
        if guardado and ahora - guardado[0] < CACHE_CICLO_S:
            return guardado[1]
    with sesion_rangos() as (_, cursor):
        perfil = leer_perfil(cursor, device_id)
        if (not perfil or perfil['cultivo'] not in CULTIVOS_CICLO_Y25
                or perfil['etapa'] not in CICLO_ETAPA_Y25):
            resultado = None
        else:
            clima = clima_ultimas_24h(cursor, device_id) if AJUSTE_CLIMA_ACTIVO else None
            resultado = dict(calcular_ciclo(perfil['etapa'], clima),
                             cultivo=perfil['cultivo'], etapa=perfil['etapa'])
    with _ciclo_lock:
        _ciclo_cache[device_id] = (ahora, resultado)
    return resultado


def _riego_instalar(n):
    original_get_relay = n['get_relay']

    def get_relay(device_id, incluir_guardada=False, incluir_contexto=False):
        config = original_get_relay(device_id)
        guardada = config.copy()
        contexto = {'origen': 'temporizador',
                    'motivo': 'Temporizador guardado; sin ciclo por etapa aplicable.',
                    'detalle': {'config_guardada': guardada}}
        try:
            ciclo = ciclo_riego_efectivo(int(device_id))
        except Exception:
            app.logger.exception('Ciclo por etapa no disponible; se usa el temporizador guardado')
            ciclo = None
            contexto.update(origen='temporizador_respaldo',
                            motivo='No se pudo calcular el ciclo por etapa; temporizador guardado.')
        if ciclo:
            # Solo cambian los valores: el JSON que recibe el Mega conserva sus 4 claves.
            config['tiempo_on'], config['tiempo_off'] = ciclo['tiempo_on'], ciclo['tiempo_off']
            contexto['detalle']['ciclo'] = ciclo
            ajustado = ciclo.get('ajuste') not in (None, 'sin_ajuste', 'igual_o_menor_que_Y25')
            contexto.update(
                origen='etapa_clima' if ajustado else 'etapa',
                motivo='Ciclo por etapa con ajuste climatico.' if ajustado else
                       'Ciclo por etapa; sin cambio de tiempos por clima.')
        if config['modo'] == 'manual':
            contexto.update(origen='manual',
                            motivo='Orden manual de ' + config['estado_manual'] +
                                   '; los tiempos no gobiernan el relevador en este modo.')
        if incluir_guardada:
            # Solo el panel recibe la configuración base; el contrato del Arduino no cambia.
            config['config_guardada'] = guardada
        if incluir_contexto:
            return config, contexto
        return config
    n['get_relay'] = get_relay

    @app.route('/api/riego/ciclo/<int:device_id>')
    @login_requerido
    def ver_ciclo_riego(device_id):
        try:
            with sesion_rangos() as (_, cursor):
                if not permiso_rangos(cursor, device_id, get_id_usuario()):
                    return jsonify(error='Dispositivo no encontrado o sin permiso.'), 403
            with _ciclo_lock:
                _ciclo_cache.pop(device_id, None)      # al consultarlo, recalcular
            ciclo = ciclo_riego_efectivo(device_id)
        except Exception:
            app.logger.exception('Error calculando el ciclo de riego')
            return jsonify(error='No se pudo calcular el ciclo. Revisa la conexion con MySQL.'), 500
        if not ciclo:
            return jsonify(origen='temporizador',
                           motivo='El perfil del dispositivo no tiene ciclo documentado '
                                  '(sin perfil, etapa general o cultivo perejil).')
        return jsonify(dict(ciclo, origen='etapa', altitud_m=ALTITUD_M,
                            fuentes={'ciclo_por_etapa': 'Yang et al. 2025, Horticulturae 11:1034, sec. 2.4, p. 5',
                                     'piso_off': 'Hoyos et al. 2019, INGE CUC 15(1), p. 125; Rincon et al. 2022, IJABE 15(6), p. 74',
                                     'demanda': 'Yang et al. 2025, ec. 7 (termino aerodinamico)',
                                     'escala_diaria': 'Gong et al. 2026; bioRxiv 10.1101/2024.01.11.575151',
                                     'altitud': 'INEGI 2018, cuadro 1.2: Xalapa-Enriquez, 1392 msnm'}))


_riego_instalar(globals())


# =====================================================================
# ACTIVIDAD 2 - HISTORIAL DE CONFIGURACIONES DE RIEGO - 26/10/2026
# config_guardada: MySQL confirmo el guardado junto con su evento.
# config_enviada: configuracion incluida en una respuesta para el Mega.
# Ninguno confirma recepcion, cambio fisico del relevador ni caudal.
# No se reconstruyen encendidos a partir del reloj del servidor.
# Preparacion explicita: python app.py --preparar-control
# =====================================================================
SQL_TABLA_CONTROL_RIEGO = '''CREATE TABLE IF NOT EXISTS historial_control_riego (
 idControl_riego BIGINT NOT NULL AUTO_INCREMENT,
 idDispositivo INT NOT NULL,
 idUsuario INT NULL,
 tipo_evento VARCHAR(24) NOT NULL,
 config_version CHAR(64) NOT NULL,
 modo VARCHAR(20) NOT NULL,
 estado_manual VARCHAR(20) NOT NULL,
 tiempo_on INT NOT NULL,
 tiempo_off INT NOT NULL,
 origen VARCHAR(32) NOT NULL,
 motivo VARCHAR(255) NOT NULL,
 detalle_json TEXT NOT NULL,
 fecha_hora DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY (idControl_riego),
 KEY idx_control_dispositivo_fecha (idDispositivo,fecha_hora,idControl_riego),
 KEY idx_control_dispositivo_tipo (idDispositivo,tipo_evento,idControl_riego),
 CONSTRAINT fk_control_dispositivo FOREIGN KEY (idDispositivo)
  REFERENCES dispositivos(idDispositivo) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'''

_control_avisos = {}
_control_avisos_lock = threading.Lock()


def _control_avisar(clave, mensaje):
    """Evita repetir el mismo aviso en cada consulta del Mega."""
    ahora = time.monotonic()
    with _control_avisos_lock:
        anterior = _control_avisos.get(clave)
        if anterior is not None and ahora - anterior < 300:
            return
        _control_avisos[clave] = ahora
    app.logger.warning(mensaje)


def _control_version(config, contexto):
    """Huella del contenido y su origen, no numero de ciclo ni prueba de aplicacion."""
    ciclo = contexto.get('detalle', {}).get('ciclo') or {}
    contenido = {'config': _relay_validar_config(config),
                 'origen': contexto['origen'],
                 'cultivo': ciclo.get('cultivo'), 'etapa': ciclo.get('etapa')}
    texto = json.dumps(contenido, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(texto.encode('utf-8')).hexdigest()


def _control_registrar_en_cursor(cursor, device_id, tipo, config, contexto,
                                id_usuario=None, deduplicar=False):
    """No hace commit. El llamador conserva el control de la transaccion."""
    if tipo not in ('config_guardada', 'config_enviada'):
        raise ValueError('Tipo de evento de control no permitido.')
    config = _relay_validar_config(config)
    version = _control_version(config, contexto)
    # Serializa escrituras del mismo dispositivo incluso con varios workers.
    cursor.execute('SELECT idDispositivo FROM dispositivos WHERE idDispositivo=%s FOR UPDATE',
                   (device_id,))
    if not cursor.fetchone():
        return None
    try:
        if deduplicar:
            cursor.execute('''SELECT config_version FROM historial_control_riego
                              WHERE idDispositivo=%s AND tipo_evento=%s
                              ORDER BY idControl_riego DESC LIMIT 1''', (device_id, tipo))
            anterior = cursor.fetchone()
            anterior = (anterior.get('config_version') if isinstance(anterior, dict)
                        else anterior[0]) if anterior else None
            if anterior == version:
                return None
        detalle = json.dumps(contexto.get('detalle') or {}, ensure_ascii=False,
                             sort_keys=True, separators=(',', ':'), allow_nan=False)
        cursor.execute('''INSERT INTO historial_control_riego
                          (idDispositivo,idUsuario,tipo_evento,config_version,modo,
                           estado_manual,tiempo_on,tiempo_off,origen,motivo,detalle_json)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (device_id, id_usuario, tipo, version, config['modo'],
                        config['estado_manual'], config['tiempo_on'], config['tiempo_off'],
                        contexto['origen'], contexto['motivo'], detalle))
        return cursor.lastrowid
    except mysql.connector.Error as error:
        if error.errno != 1146:
            raise
        # Compatibilidad con instalaciones que aun no preparan la tabla.
        _control_avisar('tabla_faltante',
                        'Historial de riego no disponible. Ejecuta python app.py --preparar-control. '
                        'La configuracion se conserva; este periodo no tendra historial de control.')
        return None


def _control_registrar_respuesta(device_id, config, contexto):
    """Registra cambios, no las consultas repetidas cada diez segundos."""
    conexion = cursor = None
    try:
        conexion = conectar_bd()
        cursor = conexion.cursor()
        _control_registrar_en_cursor(cursor, device_id, 'config_enviada', config,
                                    contexto, deduplicar=True)
        conexion.commit()
    except Exception:
        if conexion is not None:
            try:
                conexion.rollback()
            except Exception:
                pass
        _control_avisar('registro_respuesta',
                        'No se pudo registrar la configuracion enviada al Mega. '
                        'Se devuelve la configuracion; el historial puede tener un hueco.')
    finally:
        _relay_cerrar_recursos(cursor, conexion)


def preparar_bd_control():
    """Prepara los historiales; conserva lecturas, configuracion y eventos existentes."""
    conexion = cursor = None
    try:
        conexion = conectar_bd()
        cursor = conexion.cursor()
        cursor.execute(SQL_TABLA_CONTROL_RIEGO)
        cursor.execute(SQL_TABLA_EVENTOS_RELEVADOR)
        conexion.commit()
    finally:
        _relay_cerrar_recursos(cursor, conexion)


def _control_leer_historial(cursor, device_id, desde, hasta, max_filas):
    """Incluye la ultima configuracion enviada anterior al intervalo como contexto."""
    columnas = '''idControl_riego,idDispositivo,idUsuario,tipo_evento,config_version,
                  modo,estado_manual,tiempo_on,tiempo_off,origen,motivo,detalle_json,fecha_hora'''
    try:
        cursor.execute('SELECT ' + columnas + ''' FROM historial_control_riego
                          WHERE idDispositivo=%s AND fecha_hora>=%s AND fecha_hora<%s
                          ORDER BY fecha_hora,idControl_riego LIMIT %s''',
                       (device_id, desde, hasta, max_filas + 1))
        eventos = cursor.fetchall()
        if len(eventos) > max_filas:
            raise _ErrorExportacion('El intervalo supera el limite de eventos de riego. '
                                    'Divide la consulta en intervalos mas cortos.', 422)
        cursor.execute('SELECT ' + columnas + ''' FROM historial_control_riego
                          WHERE idDispositivo=%s AND tipo_evento='config_enviada' AND fecha_hora<%s
                          ORDER BY fecha_hora DESC,idControl_riego DESC LIMIT 1''',
                       (device_id, desde))
        return {'disponible': True, 'eventos': eventos, 'configuracion_previa': cursor.fetchone()}
    except mysql.connector.Error as error:
        if error.errno != 1146:
            raise
        return {'disponible': False, 'eventos': [], 'configuracion_previa': None}


@app.route('/api/riego/historial', methods=['GET'])
@login_requerido
def historial_control_riego():
    try:
        device_id, desde, hasta = _exportar_validar_filtros(request.args)
        id_usuario = get_id_usuario()
        if id_usuario is None:
            return jsonify(error='La sesion ya no es valida.'), 401
        with sesion_rangos() as (_, cursor):
            if not permiso_rangos(cursor, device_id, id_usuario):
                return jsonify(error='Dispositivo no encontrado o sin permiso.'), 403
            historial = _control_leer_historial(cursor, device_id, desde, hasta, 5000)
        if not historial['disponible']:
            return jsonify(error='Historial no disponible. Ejecuta python app.py --preparar-control.'), 503
        filas = historial['eventos'] + ([historial['configuracion_previa']]
                                        if historial['configuracion_previa'] else [])
        for fila in filas:
            fila['fecha_hora'] = fila['fecha_hora'].strftime('%Y-%m-%d %H:%M:%S')
            fila['idControl_riego'] = str(fila['idControl_riego'])
        return jsonify(dict(historial, device_id=device_id,
                            alcance='Configuraciones guardadas y respuestas preparadas para el Mega. '
                                    'No confirma recepcion, estado fisico de la bomba ni volumen suministrado.'))
    except _ErrorExportacion as error:
        return jsonify(error=str(error)), error.codigo
    except Exception:
        app.logger.exception('No se pudo consultar el historial de control de riego')
        return jsonify(error='No se pudo consultar el historial. Revisa la conexion con MySQL.'), 500


# =====================================================================
# ACTIVIDAD 2 - EVENTOS INFORMADOS POR EL MEGA - 26/10/2026
# Se registra la escritura de la salida segun el firmware, no caudal medido.
# fecha_recepcion pertenece al servidor; uptime_ms al reloj relativo del Mega.
# =====================================================================
SQL_TABLA_EVENTOS_RELEVADOR = '''CREATE TABLE IF NOT EXISTS eventos_relevador (
 idEvento BIGINT NOT NULL AUTO_INCREMENT,
 idDispositivo INT NOT NULL,
 arranque_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 secuencia BIGINT UNSIGNED NOT NULL,
 tipo_evento VARCHAR(24) NOT NULL,
 estado_relevador VARCHAR(12) NOT NULL,
 modo VARCHAR(20) NOT NULL,
 estado_manual VARCHAR(12) NOT NULL,
 tiempo_on INT NOT NULL,
 tiempo_off INT NOT NULL,
 config_version CHAR(64) NULL,
 motivo VARCHAR(24) NOT NULL,
 uptime_ms BIGINT UNSIGNED NOT NULL,
 duracion_anterior_ms BIGINT UNSIGNED NULL,
 perdidos_total BIGINT UNSIGNED NOT NULL,
 firma CHAR(64) NOT NULL,
 fecha_recepcion DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
 PRIMARY KEY (idEvento),
 UNIQUE KEY uq_evento_origen (idDispositivo,arranque_id,secuencia),
 KEY idx_evento_dispositivo_fecha (idDispositivo,fecha_recepcion,idEvento),
 CONSTRAINT fk_evento_dispositivo FOREIGN KEY (idDispositivo)
  REFERENCES dispositivos(idDispositivo) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'''


class _ErrorEventoRiego(ValueError):
    def __init__(self, mensaje, codigo=400):
        super().__init__(mensaje)
        self.codigo = codigo


def _eventos_protocolo(device_id, arranque_id):
    """Anuncia el protocolo solo si el dispositivo y su tabla estan disponibles.

    La primera respuesta asigna una sesion aleatoria. El Mega la conserva en
    RAM y la devuelve en las consultas siguientes; cada reinicio pide otra.
    """
    import re
    import uuid
    if arranque_id is not None and (not isinstance(arranque_id, str)
                                    or not re.fullmatch('[0-9a-f]{32}', arranque_id)):
        return None
    conexion = cursor = None
    try:
        conexion = conectar_bd()
        cursor = conexion.cursor()
        cursor.execute('SELECT idDispositivo FROM dispositivos WHERE idDispositivo=%s',
                       (device_id,))
        if not cursor.fetchone():
            return None
        # Solo comprueba disponibilidad. Las tablas se crean con el comando
        # --preparar-control, nunca como efecto de una consulta del hardware.
        cursor.execute('SELECT idEvento FROM eventos_relevador LIMIT 0')
        cursor.fetchall()
        return dict(protocolo=1, arranque_id=arranque_id or uuid.uuid4().hex)
    except Exception:
        _control_avisar('protocolo_eventos',
                        'Receptor de eventos no disponible. Ejecuta python app.py --preparar-control '
                        'y verifica MySQL. Se conserva la respuesta habitual de riego.')
        return None
    finally:
        _relay_cerrar_recursos(cursor, conexion)


def _eventos_validar(data):
    """No acepta booleanos como enteros ni usa horas del PC como horas del Mega."""
    import re

    def entero(objeto, campo, minimo=0, maximo=4294967295):
        valor = objeto.get(campo)
        if type(valor) is not int or not minimo <= valor <= maximo:
            raise _ErrorEventoRiego('Campo entero no valido: ' + campo)
        return valor

    if set(data) != {'token', 'device_id', 'arranque_id', 'evento'}:
        raise _ErrorEventoRiego('El mensaje no tiene los campos esperados.')
    device_id = entero(data, 'device_id', 1, 2147483647)
    arranque = data.get('arranque_id')
    if not isinstance(arranque, str) or not re.fullmatch('[0-9a-f]{32}', arranque):
        raise _ErrorEventoRiego('Identificador de arranque no valido.')
    e = data.get('evento')
    campos = {'secuencia', 'tipo_evento', 'estado_relevador', 'modo', 'estado_manual',
              'tiempo_on', 'tiempo_off', 'config_version', 'motivo', 'uptime_ms',
              'duracion_anterior_ms', 'perdidos_total'}
    if not isinstance(e, dict) or set(e) != campos:
        raise _ErrorEventoRiego('El evento no tiene los campos esperados.')
    e = dict(e)
    entero(e, 'secuencia', 1)
    entero(e, 'uptime_ms')
    entero(e, 'perdidos_total')
    if e['tipo_evento'] not in ('inicio', 'config_aplicada', 'relevador_on', 'relevador_off',
                              'bloqueo_nivel', 'recuperacion_nivel'):
        raise _ErrorEventoRiego('Tipo de evento no valido.')
    if e['estado_relevador'] not in ('encendido', 'apagado'):
        raise _ErrorEventoRiego('Estado de relevador no valido.')
    if e['modo'] not in ('manual', 'automatico', 'espera'):
        raise _ErrorEventoRiego('Modo de evento no valido.')
    if e['estado_manual'] not in ('encendido', 'apagado'):
        raise _ErrorEventoRiego('Orden manual no valida.')
    if e['motivo'] not in ('arranque', 'configuracion', 'manual', 'ciclo_automatico',
                            'calibracion_ec', 'sin_configuracion', 'nivel_sin_lectura',
                            'nivel_bajo', 'nivel_invalido', 'nivel_vencido', 'nivel_recuperado'):
        raise _ErrorEventoRiego('Motivo de evento no valido.')
    minimo = 0 if e['modo'] == 'espera' else 1
    entero(e, 'tiempo_on', minimo, 3600)
    entero(e, 'tiempo_off', minimo, 3600)
    version = e['config_version']
    if version is not None and (not isinstance(version, str) or not re.fullmatch('[0-9a-f]{64}', version)):
        raise _ErrorEventoRiego('Version de configuracion no valida.')
    if e['modo'] == 'espera' and (version is not None or e['estado_relevador'] != 'apagado'):
        raise _ErrorEventoRiego('Un evento en espera debe estar apagado y sin version aplicada.')
    if e['modo'] == 'espera' and (e['tiempo_on'] != 0 or e['tiempo_off'] != 0):
        raise _ErrorEventoRiego('Un evento en espera no tiene tiempos de riego aceptados.')
    if e['tipo_evento'] in ('relevador_on', 'relevador_off'):
        esperado = 'encendido' if e['tipo_evento'] == 'relevador_on' else 'apagado'
        if e['estado_relevador'] != esperado:
            raise _ErrorEventoRiego('El estado no coincide con el cambio informado.')
        entero(e, 'duracion_anterior_ms')
    elif e['duracion_anterior_ms'] is not None:
        raise _ErrorEventoRiego('Solo los cambios de salida incluyen duracion anterior.')
    if e['tipo_evento'] == 'inicio' and (e['estado_relevador'] != 'apagado' or e['modo'] != 'espera'
                                         or e['motivo'] != 'arranque' or e['secuencia'] != 1
                                         or e['estado_manual'] != 'apagado'):
        raise _ErrorEventoRiego('El evento de inicio no es coherente.')
    if e['tipo_evento'] == 'config_aplicada' and (e['modo'] == 'espera' or e['motivo'] != 'configuracion'):
        raise _ErrorEventoRiego('La confirmacion no incluye una configuracion aceptada.')
    motivos_bloqueo = ('nivel_sin_lectura', 'nivel_bajo', 'nivel_invalido', 'nivel_vencido')
    if e['tipo_evento'] == 'bloqueo_nivel' and (e['estado_relevador'] != 'apagado'
                                               or e['motivo'] not in motivos_bloqueo):
        raise _ErrorEventoRiego('El bloqueo de nivel debe informar salida apagada y su motivo.')
    if e['tipo_evento'] == 'recuperacion_nivel' and (e['estado_relevador'] != 'apagado'
                                                    or e['motivo'] != 'nivel_recuperado'):
        raise _ErrorEventoRiego('La recuperacion debe preceder a cualquier nuevo encendido.')
    if e['motivo'] in motivos_bloqueo and e['tipo_evento'] not in ('bloqueo_nivel', 'relevador_off'):
        raise _ErrorEventoRiego('Un motivo de bloqueo no puede ordenar encendido.')
    if e['motivo'] == 'nivel_recuperado' and e['tipo_evento'] != 'recuperacion_nivel':
        raise _ErrorEventoRiego('El motivo de recuperacion requiere su aviso de nivel.')
    firma = hashlib.sha256(json.dumps(e, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()
    return device_id, arranque, e, firma


@app.route('/api/arduino/riego/evento', methods=['POST'])
def arduino_evento_riego():
    if request.content_length is not None and request.content_length > 4096:
        return jsonify(error='Evento demasiado grande.'), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error='Se requiere un objeto JSON.'), 400
    if data.get('token') != ARDUINO_TOKEN:
        return jsonify(error='No autorizado'), 401
    conexion = cursor = None
    try:
        device_id, arranque, evento, firma = _eventos_validar(data)
        conexion = conectar_bd()
        cursor = conexion.cursor(dictionary=True)
        # Mismo bloqueo por dispositivo que usa el historial de configuraciones.
        cursor.execute('SELECT idDispositivo FROM dispositivos WHERE idDispositivo=%s FOR UPDATE',
                       (device_id,))
        if not cursor.fetchone():
            raise _ErrorEventoRiego('Dispositivo no encontrado.', 404)
        cursor.execute('''SELECT idEvento,firma FROM eventos_relevador
                          WHERE idDispositivo=%s AND arranque_id=%s AND secuencia=%s''',
                       (device_id, arranque, evento['secuencia']))
        anterior = cursor.fetchone()
        if anterior and anterior['firma'] != firma:
            raise _ErrorEventoRiego('Identificador de evento reutilizado con otro contenido.', 409)
        if not anterior:
            campos = ('secuencia', 'tipo_evento', 'estado_relevador', 'modo', 'estado_manual',
                      'tiempo_on', 'tiempo_off', 'config_version', 'motivo', 'uptime_ms',
                      'duracion_anterior_ms', 'perdidos_total')
            cursor.execute('''INSERT INTO eventos_relevador
                              (idDispositivo,arranque_id,secuencia,tipo_evento,estado_relevador,
                               modo,estado_manual,tiempo_on,tiempo_off,config_version,motivo,
                               uptime_ms,duracion_anterior_ms,perdidos_total,firma)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                           (device_id, arranque) + tuple(evento[c] for c in campos) + (firma,))
        # Solo confirma despues del commit: si el ACK se pierde, el reintento no duplica la fila.
        conexion.commit()
        return jsonify(status='ok', arranque_id=arranque, secuencia=evento['secuencia'],
                       duplicado=bool(anterior))
    except _ErrorEventoRiego as error:
        if conexion is not None:
            conexion.rollback()
        return jsonify(error=str(error)), error.codigo
    except mysql.connector.Error as error:
        if conexion is not None:
            conexion.rollback()
        if error.errno == 1146:
            return jsonify(error='Prepara las tablas con python app.py --preparar-control.'), 503
        app.logger.exception('No se pudo guardar el evento de riego')
        return jsonify(error='No se pudo guardar el evento de riego.'), 503
    except Exception:
        if conexion is not None:
            conexion.rollback()
        app.logger.exception('No se pudo procesar el evento de riego')
        return jsonify(error='No se pudo procesar el evento de riego.'), 500
    finally:
        _relay_cerrar_recursos(cursor, conexion)


def _eventos_leer(cursor, device_id, desde, hasta, max_filas):
    try:
        cursor.execute('''SELECT idEvento,idDispositivo,arranque_id,secuencia,tipo_evento,
                                 estado_relevador,modo,estado_manual,tiempo_on,tiempo_off,
                                 config_version,motivo,uptime_ms,duracion_anterior_ms,
                                 perdidos_total,fecha_recepcion
                          FROM eventos_relevador
                          WHERE idDispositivo=%s AND fecha_recepcion>=%s AND fecha_recepcion<%s
                          ORDER BY fecha_recepcion,idEvento LIMIT %s''',
                       (device_id, desde, hasta, max_filas + 1))
        eventos = cursor.fetchall()
        if len(eventos) > max_filas:
            raise _ErrorExportacion('El intervalo supera el limite de eventos del relevador.', 422)
        return dict(disponible=True, eventos=eventos)
    except mysql.connector.Error as error:
        if error.errno != 1146:
            raise
        return dict(disponible=False, eventos=[])


@app.route('/api/riego/eventos', methods=['GET'])
@login_requerido
def consultar_eventos_riego():
    try:
        device_id, desde, hasta = _exportar_validar_filtros(request.args)
        usuario = get_id_usuario()
        if usuario is None:
            return jsonify(error='La sesion ya no es valida.'), 401
        with sesion_rangos() as (_, cursor):
            if not permiso_rangos(cursor, device_id, usuario):
                return jsonify(error='Dispositivo no encontrado o sin permiso.'), 403
            datos = _eventos_leer(cursor, device_id, desde, hasta, 5000)
        if not datos['disponible']:
            return jsonify(error='Ejecuta python app.py --preparar-control.'), 503
        for evento in datos['eventos']:
            evento['fecha_recepcion'] = evento['fecha_recepcion'].isoformat(sep=' ', timespec='milliseconds')
            evento['idEvento'] = str(evento['idEvento'])
        return jsonify(device_id=device_id, eventos=datos['eventos'],
                       alcance='Salida informada por el firmware. No mide caudal ni volumen. '
                               'fecha_recepcion es hora del servidor; uptime_ms es el reloj relativo del Mega.')
    except _ErrorExportacion as error:
        return jsonify(error=str(error)), error.codigo
    except Exception:
        app.logger.exception('No se pudieron consultar eventos de riego')
        return jsonify(error='No se pudieron consultar eventos de riego.'), 500


# =====================================================================
# EXPORTACIÓN HISTÓRICA A EXCEL — 21/10/2026
# Consulta independiente: no cambia la adquisición ni el control de riego.
# Dependencia opcional: python -m pip install "openpyxl>=3.1,<4"
# =====================================================================

EXPORTACION_MAX_FILAS = 50000  # Por hoja. Si se excede, no se entrega un archivo parcial.


class _ErrorExportacion(ValueError):
    def __init__(self, mensaje, codigo=400):
        super().__init__(mensaje)
        self.codigo = codigo


def _exportar_validar_filtros(argumentos):
    """Intervalo [desde, hasta), expresado en la hora almacenada en MySQL."""
    import re
    campos = ('device_id', 'desde', 'hasta')
    for campo in campos:
        if len(argumentos.getlist(campo)) != 1:
            raise _ErrorExportacion('Indica una sola vez dispositivo, fecha inicial y fecha final.')
    identificador = argumentos['device_id']
    if (not re.fullmatch(r'[0-9]{1,10}', identificador)
            or not 1 <= int(identificador) <= 2147483647):
        raise _ErrorExportacion('Selecciona un dispositivo válido.')
    fechas = []
    for campo in ('desde', 'hasta'):
        texto = argumentos[campo]
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2})?', texto):
            raise _ErrorExportacion('Usa fechas con hora: AAAA-MM-DDTHH:MM, con segundos opcionales.')
        try:
            fecha = datetime.fromisoformat(texto)
        except ValueError:
            raise _ErrorExportacion('La fecha o la hora indicada no existe.') from None
        if fecha.year < 1900:
            raise _ErrorExportacion('La exportación admite fechas desde el año 1900.')
        fechas.append(fecha)
    if fechas[0] >= fechas[1]:
        raise _ErrorExportacion('La fecha final debe ser posterior a la inicial.')
    return int(identificador), fechas[0], fechas[1]


def _exportar_leer_datos(device_id, desde, hasta):
    """Lee ambas tablas en una misma instantánea de InnoDB, sin escrituras."""
    conexion = cursor = None
    try:
        conexion = conectar_bd()
        conexion.start_transaction(isolation_level='REPEATABLE READ',
                                   consistent_snapshot=True, readonly=True)
        cursor = conexion.cursor(dictionary=True, buffered=True)
        cursor.execute('SELECT idUsuario FROM usuarios WHERE correo=%s LIMIT 1',
                       (session.get('usuario_logueado'),))
        usuario = cursor.fetchone()
        if not usuario:
            raise _ErrorExportacion('La sesión ya no es válida. Inicia sesión nuevamente.', 401)
        dispositivo = permiso_rangos(cursor, device_id, usuario['idUsuario'])
        if not dispositivo:
            raise _ErrorExportacion('Dispositivo no encontrado o sin permiso de consulta.', 403)
        cursor.execute('''SELECT NOW() AS hora_mysql, UTC_TIMESTAMP() AS hora_utc_mysql,
                                 @@session.time_zone AS zona_sesion_mysql,
                                 @@system_time_zone AS zona_sistema_mysql''')
        relojes = cursor.fetchone()
        relojes['hora_flask'] = datetime.now()

        # La tabla es opcional en instalaciones anteriores. No se crea al exportar.
        diagnosticos_disponibles = True
        try:
            cursor.execute('SELECT idMedicion FROM mediciones_agua LIMIT 0')
            cursor.fetchall()
        except mysql.connector.Error as error:
            if error.errno != 1146:
                raise
            diagnosticos_disponibles = False

        parametros = (device_id, desde, hasta, EXPORTACION_MAX_FILAS + 1)
        cursor.execute('''SELECT r.idRegistro_sensor, s.idDispositivo, r.idSensor,
                                 s.tipo_sensor, s.unidad_medida, r.valor, r.fecha_hora
                          FROM registro_sensores r
                          JOIN sensores s ON s.idSensore=r.idSensor
                          WHERE s.idDispositivo=%s AND r.fecha_hora>=%s AND r.fecha_hora<%s
                          ORDER BY r.fecha_hora, r.idSensor, r.idRegistro_sensor
                          LIMIT %s''', parametros)
        lecturas = cursor.fetchall()
        if len(lecturas) > EXPORTACION_MAX_FILAS:
            raise _ErrorExportacion('El intervalo supera 50 000 lecturas. Divídelo en intervalos más cortos.', 422)

        diagnosticos = []
        if diagnosticos_disponibles:
            cursor.execute('''SELECT m.idMedicion, s.idDispositivo, m.idSensor, s.tipo_sensor,
                                     m.idRegistro_sensor, m.estado, m.calibrado, m.voltaje,
                                     m.tds_ppm, m.factor_k, m.temperatura_c,
                                     m.fecha_hora AS fecha_hora_diagnostico,
                                     r.valor AS valor_registrado, s.unidad_medida,
                                     r.fecha_hora AS fecha_hora_lectura
                              FROM mediciones_agua m
                              JOIN sensores s ON s.idSensore=m.idSensor
                              LEFT JOIN registro_sensores r ON r.idRegistro_sensor=m.idRegistro_sensor
                              WHERE s.idDispositivo=%s AND m.fecha_hora>=%s AND m.fecha_hora<%s
                              ORDER BY m.fecha_hora, m.idMedicion
                              LIMIT %s''', parametros)
            diagnosticos = cursor.fetchall()
            if len(diagnosticos) > EXPORTACION_MAX_FILAS:
                raise _ErrorExportacion('El intervalo supera 50 000 diagnósticos. Divídelo en intervalos más cortos.', 422)
        control = _control_leer_historial(cursor, device_id, desde, hasta, EXPORTACION_MAX_FILAS)
        if len(control['eventos']) + bool(control['configuracion_previa']) > EXPORTACION_MAX_FILAS:
            raise _ErrorExportacion('El intervalo y su referencia previa superan 50 000 filas de control. '
                                    'Dividelo en intervalos mas cortos.', 422)
        eventos_relevador = _eventos_leer(cursor, device_id, desde, hasta, EXPORTACION_MAX_FILAS)
        if not lecturas and not diagnosticos and not control['eventos'] and not eventos_relevador['eventos']:
            detalle = '' if diagnosticos_disponibles else ' La tabla de diagnósticos de agua no está disponible.'
            raise _ErrorExportacion('No hay registros disponibles para ese dispositivo e intervalo.' + detalle, 404)
        return dict(dispositivo=dispositivo, desde=desde, hasta=hasta, relojes=relojes,
                    lecturas=lecturas, diagnosticos=diagnosticos,
                    diagnosticos_disponibles=diagnosticos_disponibles, control=control,
                    eventos_relevador=eventos_relevador)
    finally:
        # Termina la instantánea y libera ambos recursos incluso si uno falla.
        if conexion is not None:
            try:
                conexion.rollback()
            except Exception:
                app.logger.exception('No se pudo cerrar la transacción de exportación')
        for recurso in (cursor, conexion):
            if recurso is not None:
                try:
                    recurso.close()
                except Exception:
                    app.logger.exception('No se pudo liberar un recurso de exportación')


def _exportar_generar_excel(datos):
    """Genera XLSX con fechas/números reales y textos que nunca son fórmulas."""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    libro = Workbook(write_only=True)
    libro.properties.creator = 'HYDROSENSE'
    libro.properties.title = 'Historial de sensores HYDROSENSE'
    fuente = Font(name='Calibri', size=11)
    fuente_cabecera = Font(name='Calibri', size=11, bold=True, color='FFFFFF')
    fondo = PatternFill('solid', fgColor='216B50')

    def celda(hoja, valor, identificador=False):
        # Los identificadores se guardan como texto para conservar también BIGINT.
        if identificador and valor is not None:
            valor = str(valor)
        resultado = WriteOnlyCell(hoja, value=valor)
        resultado.font = fuente
        if isinstance(valor, str):
            resultado.data_type = 's'  # Incluso nombres que comiencen con '='.
        elif isinstance(valor, datetime):
            resultado.number_format = 'yyyy-mm-dd hh:mm:ss'
        elif valor is not None:
            resultado.number_format = '0.######'
        return resultado

    def hoja_datos(nombre, columnas, filas):
        hoja = libro.create_sheet(nombre)
        hoja.freeze_panes = 'A2'
        hoja.sheet_view.showGridLines = False
        hoja.auto_filter.ref = f'A1:{get_column_letter(len(columnas))}{len(filas) + 1}'
        hoja.row_dimensions[1].height = 34
        encabezados = []
        for numero, (clave, titulo, ancho) in enumerate(columnas, 1):
            hoja.column_dimensions[get_column_letter(numero)].width = ancho
            c = celda(hoja, titulo)
            c.font, c.fill = fuente_cabecera, fondo
            c.alignment = Alignment(vertical='center', wrap_text=True)
            encabezados.append(c)
        hoja.append(encabezados)
        for numero_fila, fila in enumerate(filas, 2):
            celdas = [celda(hoja, fila.get(clave), clave.startswith('id'))
                      for clave, _, _ in columnas]
            if nombre in ('Informacion', 'Control_riego', 'Eventos_relevador'):
                for c in celdas:
                    c.alignment = Alignment(vertical='top', wrap_text=True)
                hoja.row_dimensions[numero_fila].height = 90 if nombre in ('Control_riego', 'Eventos_relevador') else 44
            if nombre == 'Eventos_relevador':
                for c in celdas:
                    if isinstance(c.value, datetime):
                        c.number_format = 'yyyy-mm-dd hh:mm:ss.000'
            hoja.append(celdas)
        return hoja

    relojes, dispositivo = datos['relojes'], datos['dispositivo']
    control = datos['control']
    eventos_relevador = datos.get('eventos_relevador', dict(disponible=False, eventos=[]))
    informacion = [
        ('Dispositivo ID', str(dispositivo['idDispositivo'])),
        ('Nombre del dispositivo', dispositivo['nombre']),
        ('Desde, incluido', datos['desde']),
        ('Hasta, excluido', datos['hasta']),
        ('Filas en Lecturas', len(datos['lecturas'])),
        ('Filas en Diagnosticos_agua', len(datos['diagnosticos'])),
        ('Tabla de diagnósticos', 'Disponible' if datos['diagnosticos_disponibles'] else
         'No disponible. La hoja vacía no demuestra ausencia de fallos.'),
        ('Eventos de control dentro del intervalo', len(control['eventos'])),
        ('Eventos del relevador recibidos dentro del intervalo', len(eventos_relevador['eventos'])),
        ('Tabla de eventos del relevador', 'Disponible' if eventos_relevador['disponible'] else
         'No disponible. Ejecuta python app.py --preparar-control.'),
        ('Eventos_relevador', 'El Mega informa inicio, configuracion aceptada y cambios de su salida. '
         'No comprueba contactos del relevador, caudal ni volumen. El intervalo se filtra por '
         'fecha de recepcion; no se deduce el estado de la bomba al inicio del intervalo.'),
        ('Reloj del Mega', 'uptime_ms son milisegundos desde el arranque (se repite cada 49.7 dias). '
         'No es una fecha. La duracion anterior corresponde al estado de salida anterior y solo '
         'se informa en cambios ON/OFF. No se calcula restando fechas de recepcion.'),
        ('Continuidad de eventos', 'arranque_id distingue reinicios; secuencia identifica cada aviso. '
         'La cola del Mega conserva hasta 8 avisos en RAM. perdidos_total cuenta descartes por '
         'cola llena en ese arranque, informados en un aviso posterior. Un reinicio pierde los '
         'avisos pendientes: un contador de perdidas en cero no garantiza historial completo.'),
        ('Recepcion de eventos', 'fecha_recepcion es la hora de MySQL al guardar por primera vez. '
         'Puede ser posterior al cambio de salida si hubo una interrupcion de red. Los reintentos '
         'confirmados no duplican filas ni cambian esta fecha.'),
        ('Referencia de control anterior al intervalo',
         'Incluida, marcada con 1 en Referencia previa.' if control['configuracion_previa'] else
         'Sin registro previo disponible. No se deduce un estado inicial de la bomba.'),
        ('Tabla de control de riego', 'Disponible' if control['disponible'] else
         'No disponible. Ejecuta python app.py --preparar-control; no existe historial retroactivo.'),
        ('Control de riego', 'config_guardada: guardado confirmado en MySQL. config_enviada: '
         'respuesta preparada para el Mega. No confirman recepcion ni ejecucion. '
         'Solo se registran cambios de configuracion enviada, no cada consulta.'),
        ('Version de configuracion', 'Huella SHA-256 de los tiempos, modo, orden manual, origen, '
         'cultivo y etapa. Identifica el contenido; no demuestra que el Mega lo aplico.'),
        ('Hora de MySQL al exportar', relojes['hora_mysql']),
        ('Hora UTC de MySQL al exportar', relojes['hora_utc_mysql']),
        ('Hora de Flask al exportar', relojes['hora_flask']),
        ('Zona de sesión MySQL', relojes['zona_sesion_mysql']),
        ('Zona del sistema MySQL', relojes['zona_sistema_mysql']),
        ('Interpretación de fechas', 'Fechas originales del servidor, sin conversión de zona horaria. '
         'No son marcas de tiempo enviadas por Arduino. Verifica los relojes antes de filtrar.'),
        ('Lecturas', 'Una fila por registro almacenado. Sin promediar, rellenar ni eliminar valores por rango agronómico.'),
        ('Diagnósticos', 'Una fila por diagnóstico dentro del intervalo. Se conservan los que no tienen valor numérico.'),
        ('Relación entre hojas', 'Relaciona idRegistro_sensor cuando exista. No sumes sus filas como muestras independientes.'),
        ('Calibrado', '1: calibrado; 0: no calibrado; vacío: no disponible. Se conserva el estado registrado.'),
        ('Unidades', 'Se conserva unidad_medida de la base. La luz es una escala relativa, aunque figure lux; '
         'la distancia del ultrasonido no equivale a volumen de agua.'),
        ('Valores vacíos', 'Representan datos no disponibles. Un cero numérico se conserva como cero.'),
        ('Alcance', 'Copia del historial disponible. No sustituye un respaldo completo de la base de datos.'),
    ]
    hoja_datos('Informacion', [('campo', 'HYDROSENSE · Exportación de sensores', 42),
                               ('contenido', 'Detalle', 115)],
               [dict(campo=k, contenido=v) for k, v in informacion])
    hoja_datos('Lecturas', [
        ('idRegistro_sensor', 'idRegistro_sensor', 23), ('idDispositivo', 'idDispositivo', 18),
        ('idSensor', 'idSensor', 15), ('tipo_sensor', 'Sensor', 24),
        ('unidad_medida', 'Unidad registrada', 22), ('valor', 'Valor', 18),
        ('fecha_hora', 'Fecha y hora del servidor', 26),
    ], datos['lecturas'])
    hoja_datos('Diagnosticos_agua', [
        ('idMedicion', 'idMedicion', 24), ('idDispositivo', 'idDispositivo', 18),
        ('idSensor', 'idSensor', 15), ('tipo_sensor', 'Sensor', 24),
        ('idRegistro_sensor', 'idRegistro_sensor', 23), ('estado', 'Estado de medición', 25),
        ('calibrado', 'Calibrado (1/0)', 20), ('voltaje', 'Voltaje (V)', 18),
        ('tds_ppm', 'TDS (ppm)', 18), ('factor_k', 'Factor K registrado', 23),
        ('temperatura_c', 'Temperatura (°C)', 22),
        ('fecha_hora_diagnostico', 'Fecha y hora del diagnóstico', 29),
        ('valor_registrado', 'Valor asociado', 20), ('unidad_medida', 'Unidad registrada', 22),
        ('fecha_hora_lectura', 'Fecha y hora de la lectura', 29),
    ], datos['diagnosticos'])
    filas_control = []
    if control['configuracion_previa']:
        filas_control.append(dict(control['configuracion_previa'], referencia_previa=1))
    filas_control.extend(dict(fila, referencia_previa=0) for fila in control['eventos'])
    hoja_datos('Control_riego', [
        ('idControl_riego', 'idControl_riego', 24),
        ('idDispositivo', 'idDispositivo', 18),
        ('tipo_evento', 'Tipo de evento', 24),
        ('fecha_hora', 'Fecha y hora del servidor', 26),
        ('modo', 'Modo solicitado', 20),
        ('tiempo_on', 'Tiempo ON configurado (s)', 24),
        ('tiempo_off', 'Tiempo OFF configurado (s)', 24),
        ('estado_manual', 'Orden manual (solo modo manual)', 25),
        ('origen', 'Origen', 24),
        ('motivo', 'Motivo', 64),
        ('config_version', 'Version de configuracion (SHA-256)', 70),
        ('idUsuario', 'Usuario que guardo', 22),
        ('referencia_previa', 'Referencia previa al intervalo (1/0)', 25),
        ('detalle_json', 'Detalle de configuracion y calculo (JSON)', 85),
    ], filas_control)
    hoja_datos('Eventos_relevador', [
        ('idEvento', 'idEvento', 22),
        ('idDispositivo', 'idDispositivo', 18),
        ('arranque_id', 'Identificador de arranque', 38),
        ('secuencia', 'Secuencia en el arranque', 23),
        ('tipo_evento', 'Tipo de evento', 25),
        ('fecha_recepcion', 'Fecha y hora de recepcion (servidor)', 32),
        ('uptime_ms', 'Tiempo desde arranque (ms)', 27),
        ('duracion_anterior_ms', 'Duracion del estado anterior (ms)', 30),
        ('estado_relevador', 'Salida informada por el Mega', 28),
        ('modo', 'Modo de la configuracion aceptada', 30),
        ('tiempo_on', 'Tiempo ON configurado (s)', 24),
        ('tiempo_off', 'Tiempo OFF configurado (s)', 24),
        ('estado_manual', 'Orden manual (solo modo manual)', 28),
        ('motivo', 'Motivo del aviso', 27),
        ('config_version', 'Version de configuracion (SHA-256)', 70),
        ('perdidos_total', 'Avisos descartados en este arranque', 32),
    ], eventos_relevador['eventos'])
    archivo = BytesIO()
    libro.save(archivo)
    archivo.seek(0)
    return archivo


@app.route('/api/exportaciones/lecturas.xlsx', methods=['GET'])
@login_requerido
def exportar_lecturas_excel():
    """Descarga autenticada. La página /exportar permite usarla sin editar el dashboard."""
    from flask import send_file
    try:
        device_id, desde, hasta = _exportar_validar_filtros(request.args)
        try:
            import openpyxl  # Opcional: su ausencia no impide iniciar ni usar HYDROSENSE.
        except ImportError:
            raise _ErrorExportacion('Falta la dependencia para Excel. Ejecuta: '
                                   'python -m pip install "openpyxl>=3.1,<4" y reinicia Flask.', 503) from None
        datos = _exportar_leer_datos(device_id, desde, hasta)
        archivo = _exportar_generar_excel(datos)
        nombre = f'HYDROSENSE_D{device_id}_{desde:%Y%m%d_%H%M%S}_{hasta:%Y%m%d_%H%M%S}.xlsx'
        respuesta = send_file(archivo,
                              mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                              as_attachment=True, download_name=nombre,
                              conditional=False, etag=False, max_age=0)
        respuesta.headers['Cache-Control'] = 'no-store, private'
        respuesta.headers['X-Content-Type-Options'] = 'nosniff'
        return respuesta
    except _ErrorExportacion as error:
        respuesta = jsonify(status='error', mensaje=str(error))
        respuesta.status_code = error.codigo
    except mysql.connector.Error:
        app.logger.exception('Error de MySQL al exportar lecturas')
        respuesta = jsonify(status='error', mensaje='No se pudo consultar el historial. Revisa la conexión con MySQL.')
        respuesta.status_code = 503
    except Exception:
        app.logger.exception('Error al generar el Excel de sensores')
        respuesta = jsonify(status='error', mensaje='No se pudo generar el archivo Excel. Revisa el registro del servidor.')
        respuesta.status_code = 500
    respuesta.headers['Cache-Control'] = 'no-store, private'
    return respuesta


@app.route('/exportar', methods=['GET'])
@login_requerido
def pagina_exportar_lecturas():
    respuesta = app.make_response(render_template_string(EXPORTACION_HTML))
    respuesta.headers['Cache-Control'] = 'no-store, private'
    return respuesta


EXPORTACION_HTML = r'''<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Exportar lecturas · HYDROSENSE</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f7f4;color:#183b2c;font:16px/1.55 system-ui,sans-serif}
main{max-width:760px;margin:48px auto;padding:28px;background:white;border:1px solid #dce8df;border-radius:16px}
h1{font-size:28px;margin:12px 0}a{color:#216b50}label{display:block;font-weight:600;margin:16px 0 6px}
input,select,button{font:inherit;width:100%;padding:11px;border:1px solid #aabeb0;border-radius:8px}
button{background:#216b50;color:white;border:0;margin-top:24px;cursor:pointer}button:disabled{opacity:.6;cursor:wait}
.fechas{display:grid;grid-template-columns:1fr 1fr;gap:16px}.nota{color:#4c6255;font-size:14px}
#mensaje{min-height:24px;overflow-wrap:anywhere}.error{color:#a22b21}@media(max-width:600px){main{margin:16px;padding:20px}.fechas{grid-template-columns:1fr}}
</style></head><body><main>
<a href="{{ url_for('dashboard') }}">Volver al panel</a>
<h1>Exportar lecturas a Excel</h1>
<p>Descarga el historial de un dispositivo y los diagnósticos disponibles del agua.</p>
<form id="exportacion" action="{{ url_for('exportar_lecturas_excel') }}" method="get">
<label for="device_id">Dispositivo</label><select id="device_id" name="device_id" required disabled><option value="">Cargando dispositivos…</option></select>
<div class="fechas"><div><label for="desde">Fecha y hora inicial</label><input id="desde" name="desde" type="datetime-local" step="1" required></div>
<div><label for="hasta">Fecha y hora final</label><input id="hasta" name="hasta" type="datetime-local" step="1" required></div></div>
<p class="nota">Usa la hora almacenada en el servidor. Se incluye la hora inicial y se excluye la final.
El archivo conserva los valores registrados y los datos faltantes.</p>
<button id="descargar" type="submit" disabled>Descargar Excel</button>
</form><p id="mensaje" role="status" aria-live="polite">Consultando los dispositivos disponibles…</p>
<p class="nota">El archivo contiene Lecturas, Diagnosticos_agua e Informacion. La última incluye conteos y los relojes del servidor para revisar el intervalo.</p>
<noscript>Activa JavaScript para seleccionar el dispositivo y descargar el archivo.</noscript>
</main><script>
(() => {
  const formulario = document.getElementById('exportacion');
  const selector = document.getElementById('device_id');
  const boton = document.getElementById('descargar');
  const mensaje = document.getElementById('mensaje');
  const desde = document.getElementById('desde'), hasta = document.getElementById('hasta');
  let descargando = false;
  const mostrar = (texto, error = false) => { mensaje.textContent = texto; mensaje.className = error ? 'error' : ''; };
  async function errorRespuesta(respuesta) {
    if (respuesta.status === 401 || respuesta.redirected) return 'Tu sesión terminó. Vuelve al panel e inicia sesión.';
    try { const datos = await respuesta.json(); return datos.mensaje || datos.error || 'No se pudo completar la solicitud.'; }
    catch (_) { return 'El servidor no devolvió una respuesta válida.'; }
  }
  async function cargarDispositivos() {
    try {
      const respuesta = await fetch({{ url_for('obtener_lista_dispositivos')|tojson }}, {credentials:'same-origin', cache:'no-store'});
      if (!respuesta.ok || respuesta.redirected) throw new Error(await errorRespuesta(respuesta));
      const datos = await respuesta.json();
      if (!Array.isArray(datos)) throw new Error('La lista de dispositivos no es válida.');
      selector.replaceChildren(new Option('Selecciona un dispositivo', ''));
      const ids = new Set();
      for (const dispositivo of datos) {
        const id = Number(dispositivo.idDispositivo);
        if (!Number.isInteger(id) || id < 1 || id > 2147483647) throw new Error('La lista contiene un dispositivo no válido.');
        if (!ids.has(id)) selector.add(new Option(`${dispositivo.nombre || 'Dispositivo'} (ID ${id})`, String(id)));
        ids.add(id);
      }
      if (!ids.size) { mostrar('No tienes dispositivos disponibles para exportar.'); return; }
      selector.disabled = false; boton.disabled = false;
      mostrar('Selecciona el dispositivo y el intervalo que deseas descargar.');
    } catch (error) { mostrar(error.message, true); }
  }
  formulario.addEventListener('submit', async evento => {
    evento.preventDefault();
    if (descargando || !formulario.reportValidity()) return;
    if (desde.value >= hasta.value) { mostrar('La fecha final debe ser posterior a la inicial.', true); return; }
    const parametros = new URLSearchParams(new FormData(formulario));
    const controles = Array.from(formulario.elements);
    const estados = controles.map(control => control.disabled);
    descargando = true; controles.forEach(control => { control.disabled = true; });
    mostrar('Preparando el Excel con los registros almacenados…');
    try {
      const respuesta = await fetch(formulario.action + '?' + parametros, {credentials:'same-origin', cache:'no-store'});
      if (!respuesta.ok || respuesta.redirected) throw new Error(await errorRespuesta(respuesta));
      if (!(respuesta.headers.get('Content-Type') || '').includes('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')) {
        throw new Error('El servidor no devolvió un archivo Excel.');
      }
      const archivo = await respuesta.blob();
      if (!archivo.size) throw new Error('El archivo recibido está vacío.');
      const url = URL.createObjectURL(archivo), enlace = document.createElement('a');
      const nombre = (respuesta.headers.get('Content-Disposition') || '').match(/filename="?([^";]+)"?/i);
      enlace.href = url; enlace.download = nombre ? nombre[1] : 'HYDROSENSE_lecturas.xlsx';
      document.body.append(enlace); enlace.click(); enlace.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      mostrar('Descarga iniciada. Revisa Informacion para comprobar fechas y cantidades.');
    } catch (error) { mostrar(error.message || 'No se pudo descargar el archivo.', true); }
    finally { controles.forEach((control, i) => { control.disabled = estados[i]; }); descargando = false; }
  });
  cargarDispositivos();
})();
</script></body></html>'''


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Servidor AeroGarden')
    parser.add_argument('--preparar-agua',action='store_true',
                        help='Crear la tabla adicional de sensores de agua y salir')
    parser.add_argument('--preparar-control',action='store_true',
                        help='Crear las tablas de configuraciones y eventos del relevador y salir')
    argumentos=parser.parse_args()
    if argumentos.preparar_agua and argumentos.preparar_control:
        parser.error('Prepara cada tabla por separado: --preparar-agua o --preparar-control.')
    if argumentos.preparar_agua:
        try:
            preparar_bd_agua()
        except Exception as error:
            print('No se pudo preparar la tabla de agua:',error)
            raise SystemExit(1)
        print('Tabla mediciones_agua lista. Los sensores y las lecturas existentes se conservan.')
    elif argumentos.preparar_control:
        try:
            preparar_bd_control()
        except Exception as error:
            print('No se pudo preparar el historial de control:', error)
            raise SystemExit(1)
        print('Tablas historial_control_riego y eventos_relevador listas. '
              'El registro empieza con las nuevas operaciones. '
              'Las lecturas y la configuracion existentes se conservan.')
    else:
        app.run(
            debug=os.environ.get('FLASK_DEBUG', 'False') == 'True',
            host='0.0.0.0',
            port=int(os.environ.get('PORT', 5000))
        )



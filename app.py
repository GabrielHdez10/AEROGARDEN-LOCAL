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


def set_relay(device_id: int, data: dict):
    config = {
        "tiempo_on":     int(data.get("tiempo_on",     30)),
        "tiempo_off":    int(data.get("tiempo_off",    60)),
        "modo":          data.get("modo",          "automatico"),
        "estado_manual": data.get("estado_manual", "apagado"),
    }
    try:
        cx  = conectar_bd()
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
        cx.commit()
        cur.close(); cx.close()
    except Exception as e:
        print(f"[RELAY set_relay] Error BD: {e}")
    with relay_lock:
        relay_configs[device_id] = config


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

    relay = get_relay(device_id)

    with wifi_lock:
        wifi = wifi_pendiente.pop(device_id, {"ssid": None, "password": None})

    return jsonify({"relay": relay, "wifi": wifi})


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
    return jsonify(get_relay(device_id))


@app.route('/api/relevador/config', methods=['POST'])
@login_requerido
def guardar_config_relevador():
    data      = request.json or {}
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "Falta device_id"}), 400
    device_id = int(device_id)
    id_u = get_id_usuario()
    print(f"[RELAY POST] device_id={device_id} id_u={id_u}")
    try:
        conexion = conectar_bd()
        cursor   = conexion.cursor(dictionary=True)
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
            cursor.close(); conexion.close()
            return jsonify({"error": "No autorizado"}), 403
        cursor.close(); conexion.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    set_relay(device_id, data)
    print(f"[RELAY dev={device_id}] modo={data.get('modo')} on={data.get('tiempo_on')}s")
    return jsonify({"status": "ok", "config": get_relay(device_id)})


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
<p class="nota">Temperatura del aire y humedad: límites provisionales de prueba. Temperatura del agua: guía hidropónica general. La sonda de pH requiere calibración; luz y distancia requieren límites medidos en tu instalación.</p>
</section>
<section><h2>Iluminación</h2><p id="iluminacion">Selecciona un perfil para ver sus referencias.</p><p class="nota">El LDR actual entrega un índice relativo. Fotoperiodo y PPFD requieren mediciones específicas.</p></section>
<section id="panelPrueba" hidden><h2>Resultado de la simulación</h2><p id="prueba" role="status"></p></section>
<footer class="nota">Las lecturas se actualizan cada 10 segundos. Una lectura con más de 5 minutos se muestra como antigua.</footer>
</main>
<script>
'use strict';
const $ = id => document.getElementById(id);
const estadoTexto = {en_rango:'En rango',bajo:'Bajo',alto:'Alto',alerta:'Alerta',sin_datos:'Sin datos',sin_datos_recientes:'Sin datos recientes',sin_alertas_activas:'Alertas inactivas',lectura_invalida:'Lectura inválida',rango_inconsistente:'Revisar límites'};
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


if __name__ == '__main__':
    app.run(
        debug=os.environ.get('FLASK_DEBUG', 'False') == 'True',
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 5000))
    )


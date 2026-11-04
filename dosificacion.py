# HYDROSENSE - DOSIFICACIÓN ASISTIDA - 03/11/2026
# =====================================================================
# Actividad 2, parte de nutrientes.
#
# Qué hace:
#   Compara la CE y el pH de la cubeta con el rango del cultivo y su etapa
#   (los mismos rangos de la página Rangos) y dice qué agregar y cuánto por
#   litro. Una persona agrega el producto a mano y lo anota; cada ajuste
#   queda en la bitácora (tabla ajustes_solucion) y en una hoja nueva del
#   Excel de exportación.
#
# Qué NO hace:
#   - No enciende bombas ni manda órdenes al Arduino.
#   - No cambia el riego, los rangos ni las alertas existentes.
#   - No calcula litros de la cubeta: si se conocen, los escribe la persona.
#   - No da cantidad para el pH: depende de la capacidad buffer del agua,
#     que no se mide; solo indica si hay que subirlo o bajarlo.
#
# Requisitos:
#   - migraciones/005_bitacora_solucion.sql ya ejecutada en mydb.
#   - app.py debe llamar a instalar(globals()) (archivo 3 de la actividad).
#     Si este archivo falla al cargar, app.py sigue funcionando sin él.
#
# Fórmulas (mezcla de soluciones, la CE se suma de forma aproximada):
#   CE baja -> fertilizante por litro = (CE objetivo - CE actual) / aumento_ce
#              aumento_ce: cuánto sube la CE con 1 unidad del producto por litro.
#   CE alta -> agua por litro = (CE actual - CE objetivo) / (CE objetivo - CE del agua)
#   CE objetivo = centro del rango del cultivo y su etapa.
#   Ejemplo: cilantro en desarrollo, rango 1.7-1.9 -> objetivo 1.8 mS/cm
#   (Yang et al., 2025, Horticulturae 11:1034, p. 5: 1.8 +/- 0.1 mS/cm).
# =====================================================================
import json
import math
import statistics
from datetime import timedelta

import mysql.connector
from flask import jsonify, request, render_template_string

# ── Criterios de ingeniería (sin fuente bibliográfica; ajustables) ──────
# El Mega envía lecturas cada 10 s; una lectura con más de 2 min ya no se
# considera "actual" (tolera algunos envíos perdidos).
EDAD_MAX_LECTURA_S = 120
# La CE debe estar estable antes de recomendar: últimas 6 lecturas (~1 min)
# con una variación máxima de 0.05 mS/cm (la mitad de la tolerancia de
# +/- 0.1 que usan Yang et al., 2025).
LECTURAS_ESTABILIDAD = 6
VARIACION_MAX_CE = 0.05
# Tras un ajuste, esperar a que se mezcle antes de recomendar otro.
ESPERA_MEZCLA_MIN = 15
# "CE y pH después": mediana de las lecturas entre 15 y 30 min tras el ajuste.
VENTANA_DESPUES_MIN = (15, 30)
# El firmware y app.py descartan CE >= 2.0 mS/cm (módulo de 0 a 1000 ppm).
LIMITE_SENSOR_CE = 2.0

TIPOS_PRODUCTO = ('fertilizante', 'agua', 'ph_bajar', 'ph_subir', 'otro')
TIPOS_AJUSTE = ('solucion_nueva', 'fertilizante', 'agua', 'ph_bajar', 'ph_subir', 'otro')
TIPOS_CON_CANTIDAD = ('fertilizante', 'agua', 'ph_bajar', 'ph_subir')
UNIDADES = ('mL', 'g', 'L')
NOMBRE_TIPO = {'solucion_nueva': 'Solución nueva', 'fertilizante': 'Fertilizante', 'agua': 'Agua',
               'ph_bajar': 'Bajar pH', 'ph_subir': 'Subir pH', 'otro': 'Otro'}


class ErrorDosis(ValueError):
    """Error con mensaje para la persona y código HTTP."""
    def __init__(self, mensaje, codigo=400):
        super().__init__(mensaje)
        self.codigo = codigo


# ── Utilidades ───────────────────────────────────────────────────────────
def _numero(valor):
    """Convierte a float finito; None si no es número."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return numero if math.isfinite(numero) else None


def _texto_fecha(valor):
    return valor.strftime('%Y-%m-%d %H:%M:%S') if hasattr(valor, 'strftime') else valor


def _redondear(valor, decimales=2):
    return None if valor is None else round(valor, decimales)


def _formato(valor, decimales=2):
    """Número para mostrar en un texto: 1.4 -> '1.4', 0.0994 -> '0.1'."""
    texto = f'{valor:.{decimales}f}'.rstrip('0').rstrip('.')
    return texto if texto not in ('', '-0') else '0'


def _hace(segundos):
    """Antigüedad fácil de leer: 'hace 10 min', 'hace 5 h', 'hace 7 días'."""
    segundos = max(0, int(segundos))
    if segundos < 60:
        return 'hace menos de 1 min'
    minutos = segundos // 60
    if minutos < 60:
        return f'hace {minutos} min'
    horas = minutos // 60
    if horas < 48:
        return f'hace {horas} h'
    return f'hace {horas // 24} días'


def _cuando(edad_s):
    """Frase con la antigüedad de la última lectura, solo si ya no es reciente."""
    if edad_s is None or edad_s <= EDAD_MAX_LECTURA_S:
        return ''
    return f' La última es de {_hace(edad_s)}.'


def _entero_positivo(valor, nombre):
    try:
        numero = int(str(valor).strip())
    except (TypeError, ValueError):
        raise ErrorDosis(f'{nombre} no es válido.') from None
    if not 1 <= numero <= 2147483647:
        raise ErrorDosis(f'{nombre} no es válido.')
    return numero


def _decimal_opcional(valor, nombre, minimo, maximo, incluir_minimo=False):
    if valor in (None, ''):
        return None
    numero = _numero(valor)
    fuera = numero is None or numero > maximo or (numero < minimo if incluir_minimo else numero <= minimo)
    if fuera:
        limite = f'de {minimo} a {maximo}' if incluir_minimo else f'mayor que {minimo} y hasta {maximo}'
        raise ErrorDosis(f'{nombre} debe ser un número {limite}.')
    return numero


def _texto_opcional(valor, nombre, maximo):
    if valor is None:
        return None
    texto = str(valor).strip()
    if len(texto) > maximo:
        raise ErrorDosis(f'{nombre} admite hasta {maximo} caracteres.')
    return texto or None


def _unidad_valida(valor):
    texto = str(valor or '').strip()
    for unidad in UNIDADES:
        if texto.lower() == unidad.lower():
            return unidad
    raise ErrorDosis('La unidad debe ser mL, g o L.')


def _a_ms_cm(valor, unidad, factor_unidades):
    """Convierte CE a mS/cm usando la función de app.py (acepta uS/cm y mS/cm)."""
    numero = _numero(valor)
    if numero is None:
        return None
    factor = factor_unidades(unidad or 'mS/cm', 'mS/cm')
    return None if factor is None else numero * factor


# ── Cálculo de la recomendación (función pura: no usa la base) ──────────
def recomendar(ctx):
    """Devuelve la lista de recomendaciones en orden de atención.

    ctx contiene: perfil, rangos (ce, ph, temperatura_agua en mS/cm, pH, °C),
    lecturas (ce, ph, temperatura_agua, nivel), productos activos y
    ultimo_ajuste. Cada elemento: variable, estado (bien, accion, espera,
    aviso, sin_dato), titulo, texto y, si aplica, dosis_por_litro, unidad,
    tipo_ajuste e id_producto.
    Orden: nivel -> mezcla -> CE -> pH -> temperatura del agua. El pH va
    después de la CE porque el fertilizante también cambia el pH.
    """
    items = []

    def agregar(variable, estado, titulo, texto, **extra):
        item = dict(variable=variable, estado=estado, titulo=titulo, texto=texto,
                    dosis_por_litro=None, unidad=None, tipo_ajuste=None, id_producto=None)
        item.update(extra)
        items.append(item)
        return item

    lecturas = ctx.get('lecturas') or {}
    rangos = ctx.get('rangos') or {}
    productos = [p for p in (ctx.get('productos') or []) if p.get('activo', 1)]
    perfil = ctx.get('perfil')

    # 1) Nivel: si falta agua, se rellena primero porque eso diluye la CE.
    nivel = lecturas.get('nivel') or {}
    if nivel.get('vigente'):
        if nivel.get('estado') == 'bajo':
            agregar('nivel', 'accion', 'Nivel de agua bajo',
                    'Rellena primero con agua hasta tu marca de lleno. Eso diluye la CE, '
                    'así que después vuelve a revisar esta página.', tipo_ajuste='agua')
        elif nivel.get('estado') in ('invalido', 'vencido'):
            agregar('nivel', 'aviso', 'No se puede comprobar el nivel',
                    'El sensor de nivel no da una lectura válida. Revisa la cubeta antes de agregar algo.')

    # 2) Espera de mezcla tras un ajuste reciente.
    esperando = False
    ultimo = ctx.get('ultimo_ajuste')
    if ultimo and ultimo.get('hace_s') is not None and ultimo['hace_s'] < ESPERA_MEZCLA_MIN * 60:
        esperando = True
        faltan = max(1, math.ceil((ESPERA_MEZCLA_MIN * 60 - ultimo['hace_s']) / 60))
        minutos = max(0, int(ultimo['hace_s'] // 60))
        hace = 'hace menos de 1 min' if minutos == 0 else f'hace {minutos} min'
        agregar('mezcla', 'espera', 'Se está mezclando',
                f'Se agregó algo {hace}. Espera unos {faltan} min antes de agregar más, '
                'para que la lectura ya incluya lo que se agregó.')

    # 3) CE
    accion_ce = False
    ce = lecturas.get('ce') or {}
    rango_ce = rangos.get('ce') or {}
    if not perfil:
        agregar('ce', 'sin_dato', 'Falta el cultivo y la etapa',
                'Elige el cultivo y la etapa en la página Rangos para saber qué CE buscar.')
    elif not rango_ce.get('disponible'):
        detalle = rango_ce.get('detalle') or 'Sin rango definido.'
        agregar('ce', 'aviso', 'Sin rango de CE para esta etapa',
                f'No hay rango de CE para {perfil["cultivo"]} en etapa {perfil["etapa"]}. {detalle} '
                'Mide con el conductímetro de mano y anota lo que agregues.')
    else:
        minimo, maximo = rango_ce['minimo'], rango_ce['maximo']
        objetivo = (minimo + maximo) / 2
        estado_ce = ce.get('estado')
        valor = ce.get('valor')
        rango_txt = f'{_formato(minimo)} a {_formato(maximo)} mS/cm'
        if valor is None:
            if estado_ce == 'fuera_rango':
                agregar('ce', 'aviso', 'CE fuera del alcance del sensor',
                        f'La CE está en {_formato(LIMITE_SENSOR_CE)} mS/cm o más y el sensor no alcanza a '
                        'medirla. Mídela con el conductímetro de mano antes de agregar algo.')
            elif estado_ce == 'calibrando':
                agregar('ce', 'espera', 'Sensor de CE en calibración',
                        'Termina la calibración (CAL_SALIR) para volver a recomendar.')
            elif estado_ce == 'senal_baja':
                agregar('ce', 'aviso', 'Señal de CE muy baja',
                        'Revisa que la sonda de CE esté dentro de la solución.')
            elif estado_ce == 'sin_temperatura':
                agregar('ce', 'aviso', 'Falta la temperatura del agua',
                        'La CE se corrige con la temperatura del DS18B20 y no hay lectura válida.')
            else:
                agregar('ce', 'sin_dato', 'Sin lectura reciente de CE',
                        'No hay una lectura de CE de los últimos 2 minutos.' + _cuando(ce.get('edad_s')))
        elif not ce.get('estable', False):
            agregar('ce', 'espera', 'La CE todavía cambia',
                    f'En el último minuto varió {_formato(ce.get("variacion") or 0, 3)} mS/cm. '
                    'Espera a que se estabilice para calcular la dosis.')
        elif esperando:
            agregar('ce', 'espera', f'CE {_formato(valor)} mS/cm',
                    'Revisa de nuevo cuando termine la espera de mezcla.')
        elif valor < minimo:
            accion_ce = True
            fertilizantes = [p for p in productos if p.get('tipo') == 'fertilizante']
            con_dato = [p for p in fertilizantes if _numero(p.get('aumento_ce'))]
            if con_dato:
                producto = con_dato[0]
                dosis = (objetivo - valor) / _numero(producto['aumento_ce'])
                agregar('ce', 'accion', 'CE baja',
                        f'La CE está en {_formato(valor)} mS/cm y el rango es {rango_txt}. Agrega ≈'
                        f'{_formato(dosis)} {producto["unidad"]} de {producto["nombre"]} por cada litro '
                        f'de solución para llegar a {_formato(objetivo)} mS/cm.',
                        dosis_por_litro=round(dosis, 4), unidad=producto['unidad'],
                        tipo_ajuste='fertilizante', id_producto=producto['idProducto'], objetivo=objetivo)
            elif fertilizantes:
                agregar('ce', 'accion', 'CE baja',
                        f'La CE está en {_formato(valor)} mS/cm y el rango es {rango_txt}. Falta medir '
                        'cuánto sube la CE tu fertilizante (ve a Mis productos). Mientras, agrega poco, '
                        'revuelve y vuelve a revisar.', tipo_ajuste='fertilizante',
                        id_producto=fertilizantes[0]['idProducto'], objetivo=objetivo)
            else:
                agregar('ce', 'accion', 'CE baja',
                        f'La CE está en {_formato(valor)} mS/cm y el rango es {rango_txt}. Registra tu '
                        'fertilizante en Mis productos para calcular la dosis.',
                        tipo_ajuste='fertilizante', objetivo=objetivo)
        elif valor > maximo:
            accion_ce = True
            aguas = [p for p in productos if p.get('tipo') == 'agua']
            agua = aguas[0] if aguas else None
            ce_agua = _numero(agua.get('ce_ms_cm')) if agua else None
            supuesto = ''
            if ce_agua is None:
                ce_agua = 0.0
                supuesto = (' Como no está registrada la CE de tu agua, se calculó como si fuera 0; '
                            'regístrala en Mis productos para afinar.')
            if objetivo <= ce_agua:
                agregar('ce', 'aviso', 'CE alta',
                        f'La CE está en {_formato(valor)} mS/cm, pero el agua registrada tiene '
                        f'{_formato(ce_agua)} mS/cm: no sirve para bajarla a {_formato(objetivo)}.')
            else:
                proporcion = (valor - objetivo) / (objetivo - ce_agua)
                agregar('ce', 'accion', 'CE alta',
                        f'La CE está en {_formato(valor)} mS/cm y el rango es {rango_txt}. Agrega ≈'
                        f'{_formato(proporcion)} L de agua por cada litro de solución para llegar a '
                        f'{_formato(objetivo)} mS/cm.' + supuesto,
                        dosis_por_litro=round(proporcion, 4), unidad='L', tipo_ajuste='agua',
                        id_producto=agua['idProducto'] if agua else None, objetivo=objetivo)
        else:
            agregar('ce', 'bien', f'CE {_formato(valor)} mS/cm', f'Dentro del rango ({rango_txt}).')

        if items and items[-1]['variable'] == 'ce':
            notas = []
            if estado_ce == 'sin_calibrar' and valor is not None:
                notas.append('La CE está sin calibrar: la dosis es aproximada.')
            if maximo >= LIMITE_SENSOR_CE:
                notas.append(f'El rango de esta etapa llega a {_formato(maximo)} mS/cm y el sensor no mide '
                             f'desde {_formato(LIMITE_SENSOR_CE)}: confirma con el medidor de mano.')
            if notas:
                items[-1]['texto'] += ' ' + ' '.join(notas)

    # 4) pH: solo dirección, sin cantidad.
    ph = lecturas.get('ph') or {}
    rango_ph = rangos.get('ph') or {}
    if perfil and rango_ph.get('disponible'):
        valor = ph.get('valor')
        minimo, maximo = rango_ph['minimo'], rango_ph['maximo']
        rango_txt = f'{_formato(minimo)} a {_formato(maximo)}'
        aviso_ph = ' El pH no está calibrado: confírmalo con tira o medidor antes de ajustar.'
        if valor is None:
            agregar('ph', 'sin_dato', 'Sin lectura reciente de pH',
                    'No hay una lectura de pH de los últimos 2 minutos.' + _cuando(ph.get('edad_s')))
        elif minimo <= valor <= maximo:
            agregar('ph', 'bien', f'pH {_formato(valor, 1)}', f'Dentro del rango ({rango_txt}).' + aviso_ph)
        else:
            sube = valor < minimo
            tipo = 'ph_subir' if sube else 'ph_bajar'
            producto = next((p for p in productos if p.get('tipo') == tipo), None)
            nombre = producto['nombre'] if producto else ('pH+' if sube else 'pH−')
            titulo = f'pH {"bajo" if sube else "alto"} ({_formato(valor, 1)})'
            if esperando or accion_ce:
                agregar('ph', 'espera', titulo,
                        f'El rango es {rango_txt}. Primero termina con la CE y la espera de mezcla; '
                        'después revisa el pH, porque el fertilizante también lo cambia.' + aviso_ph,
                        tipo_ajuste=tipo, id_producto=producto['idProducto'] if producto else None)
            else:
                agregar('ph', 'accion', titulo,
                        f'El rango es {rango_txt}. {"Sube" if sube else "Baja"} el pH con {nombre} en pasos '
                        'pequeños: agrega poco, revuelve, espera y vuelve a medir.' + aviso_ph,
                        tipo_ajuste=tipo, id_producto=producto['idProducto'] if producto else None)

    # 5) Temperatura del agua: solo informativa (no hay calentador).
    temp = lecturas.get('temperatura_agua') or {}
    rango_temp = rangos.get('temperatura_agua') or {}
    if temp.get('valor') is not None and rango_temp.get('disponible'):
        valor, minimo, maximo = temp['valor'], rango_temp['minimo'], rango_temp['maximo']
        if not minimo <= valor <= maximo:
            agregar('temperatura_agua', 'aviso', f'Agua a {_formato(valor, 1)} °C',
                    f'El rango de referencia es {_formato(minimo)} a {_formato(maximo)} °C. '
                    'No cambia la dosis, pero conviene anotarlo.')
    return items


def resumen_recomendacion(items, tipo):
    """Texto y dosis por litro que se guardan en la bitácora para el tipo de ajuste."""
    for item in items:
        if item.get('tipo_ajuste') == tipo and item['estado'] in ('accion', 'espera'):
            if item.get('dosis_por_litro') is not None:
                texto = f'{_formato(item["dosis_por_litro"])} {item["unidad"]} por litro'
            else:
                texto = item['titulo']
            return texto[:255], item.get('dosis_por_litro')
    estados = [f'{i["variable"]}: {i["titulo"]}' for i in items if i['variable'] in ('ce', 'ph')]
    return ('Sin recomendación de este tipo. ' + '; '.join(estados))[:255], None


# ── Instalación sobre app.py ─────────────────────────────────────────────
def instalar(n):
    """Registra rutas, página y hoja de Excel. n = globals() de app.py."""
    app = n['app']
    login_requerido = n['login_requerido']
    conectar_bd = n['conectar_bd']
    get_id_usuario = n['get_id_usuario']
    permiso_rangos = n['permiso_rangos']
    leer_perfil = n['leer_perfil']
    leer_catalogo = n['leer_catalogo']
    rango_referencia = n['rango_referencia']
    factor_unidades = n['factor_unidades']
    bomba_estado_leer = n.get('_bomba_estado_leer')

    class _Sesion:
        """Conexión y cursor de diccionario; deshace si hay error."""
        def __enter__(self):
            self.conexion = conectar_bd()
            self.cursor = self.conexion.cursor(dictionary=True, buffered=True)
            return self.conexion, self.cursor

        def __exit__(self, tipo, error, rastro):
            if tipo is not None:
                try:
                    self.conexion.rollback()
                except Exception:
                    pass
            for recurso in (self.cursor, self.conexion):
                try:
                    recurso.close()
                except Exception:
                    pass
            return False

    def respuesta_error(error):
        if isinstance(error, ErrorDosis):
            return jsonify(error=str(error)), error.codigo
        if isinstance(error, mysql.connector.Error) and error.errno == 1146:
            return jsonify(error='Falta ejecutar la migración 005 (bitácora de la solución).'), 503
        app.logger.exception('Error en dosificación asistida')
        return jsonify(error='No se pudo completar la solicitud. Revisa la consola del servidor.'), 500

    def permiso(cursor, device_id, escribir=False):
        usuario = get_id_usuario()
        if usuario is None:
            raise ErrorDosis('La sesión ya no es válida. Inicia sesión de nuevo.', 401)
        acceso = permiso_rangos(cursor, device_id, usuario)
        if not acceso:
            raise ErrorDosis('Dispositivo no encontrado o sin permiso.', 403)
        if escribir and not acceso.get('puede_editar'):
            raise ErrorDosis('Tu acceso a este dispositivo es solo de consulta.', 403)
        return usuario, acceso

    # ── Lecturas ────────────────────────────────────────────────────────
    def sensores(cursor, device_id):
        cursor.execute("""SELECT idSensore, LOWER(TRIM(tipo_sensor)) AS tipo, unidad_medida
                          FROM sensores WHERE idDispositivo=%s ORDER BY idSensore""", (device_id,))
        mapa = {}
        for fila in cursor.fetchall():
            mapa.setdefault(fila['tipo'], []).append(fila)
        return mapa

    def recientes(cursor, filas_sensor, cuantas):
        # Orden por la llave primaria: sigue el orden de llegada y usa el índice de idSensor.
        if not filas_sensor:
            return []
        ids = [f['idSensore'] for f in filas_sensor]
        marcas = ','.join(['%s'] * len(ids))
        cursor.execute(f"""SELECT r.valor, r.fecha_hora, r.idSensor,
                                  TIMESTAMPDIFF(SECOND, r.fecha_hora, NOW()) AS edad_s
                           FROM registro_sensores r
                           WHERE r.idSensor IN ({marcas})
                           ORDER BY r.idRegistro_sensor DESC LIMIT %s""", (*ids, cuantas))
        return cursor.fetchall()

    def lectura_simple(cursor, filas_sensor):
        filas = recientes(cursor, filas_sensor, 1)
        if not filas or filas[0]['edad_s'] is None or filas[0]['edad_s'] > EDAD_MAX_LECTURA_S:
            return dict(valor=None, fecha=_texto_fecha(filas[0]['fecha_hora']) if filas else None,
                        edad_s=filas[0]['edad_s'] if filas else None)
        return dict(valor=_numero(filas[0]['valor']), fecha=_texto_fecha(filas[0]['fecha_hora']),
                    edad_s=filas[0]['edad_s'])

    def lectura_ce(cursor, filas_sensor):
        ce = dict(valor=None, estado='sin_lectura', calibrado=False, estable=False,
                  variacion=None, fecha=None, edad_s=None)
        if not filas_sensor:
            ce['estado'] = 'sin_sensor'
            return ce
        unidades = {f['idSensore']: f['unidad_medida'] for f in filas_sensor}
        diagnostico = None
        try:
            ids = list(unidades)
            marcas = ','.join(['%s'] * len(ids))
            cursor.execute(f"""SELECT estado, calibrado, fecha_hora,
                                      TIMESTAMPDIFF(SECOND, fecha_hora, NOW()) AS edad_s
                               FROM mediciones_agua WHERE idSensor IN ({marcas})
                               ORDER BY fecha_hora DESC, idMedicion DESC LIMIT 1""", ids)
            diagnostico = cursor.fetchone()
        except mysql.connector.Error as error:
            if error.errno != 1146:
                raise
        filas = recientes(cursor, filas_sensor, LECTURAS_ESTABILIDAD)
        if filas:
            ce.update(fecha=_texto_fecha(filas[0]['fecha_hora']), edad_s=filas[0]['edad_s'])
        if diagnostico:
            ce['calibrado'] = bool(diagnostico['calibrado'])
            # Un diagnóstico reciente no válido (p. ej. CE >= 2.0) manda sobre el último valor guardado.
            if (diagnostico['edad_s'] is not None and diagnostico['edad_s'] <= EDAD_MAX_LECTURA_S
                    and diagnostico['estado'] not in ('ok', 'sin_calibrar')):
                ce.update(estado=diagnostico['estado'], fecha=_texto_fecha(diagnostico['fecha_hora']),
                          edad_s=diagnostico['edad_s'])
                return ce
        validas = [_a_ms_cm(f['valor'], unidades.get(f['idSensor']), factor_unidades)
                   for f in filas if f['edad_s'] is not None and f['edad_s'] <= EDAD_MAX_LECTURA_S]
        validas = [v for v in validas if v is not None]
        if not validas:
            ce['estado'] = 'sin_lectura_reciente'
            return ce
        ce['valor'] = validas[0]
        ce['estado'] = diagnostico['estado'] if diagnostico else 'sin_metadatos'
        if len(validas) >= 3:
            ce['variacion'] = max(validas) - min(validas)
            ce['estable'] = ce['variacion'] <= VARIACION_MAX_CE
        return ce

    def lectura_nivel(device_id):
        if not callable(bomba_estado_leer):
            return dict(vigente=False)
        try:
            dato = bomba_estado_leer(device_id)
        except Exception:
            app.logger.exception('No se pudo leer el estado de la bomba para dosificación')
            return dict(vigente=False)
        reporte = dato.get('reporte') or {}
        return dict(vigente=bool(dato.get('vigente')), estado=reporte.get('nivel_estado'),
                    distancia_cm=reporte.get('distancia_cm'))

    def productos(cursor, device_id, solo_activos=False):
        filtro = ' AND activo=1' if solo_activos else ''
        cursor.execute(f"""SELECT idProducto, nombre, tipo, unidad, aumento_ce, ce_ms_cm, nota, activo,
                                  creado_en
                           FROM productos_solucion WHERE idDispositivo=%s{filtro}
                           ORDER BY activo DESC, creado_en DESC, idProducto DESC""", (device_id,))
        lista = []
        for fila in cursor.fetchall():
            lista.append(dict(idProducto=fila['idProducto'], nombre=fila['nombre'], tipo=fila['tipo'],
                              unidad=fila['unidad'], aumento_ce=_numero(fila['aumento_ce']),
                              ce_ms_cm=_numero(fila['ce_ms_cm']), nota=fila['nota'],
                              activo=int(fila['activo']), creado_en=_texto_fecha(fila['creado_en'])))
        return lista

    def ultimos_ajustes(cursor, device_id, limite=15):
        cursor.execute("""SELECT a.idAjuste, a.fecha_hora, a.tipo, a.producto_nombre, a.cantidad, a.unidad,
                                 a.litros_cubeta, a.ce_antes, a.ce_estado, a.ph_antes, a.recomendacion,
                                 a.nota, TIMESTAMPDIFF(SECOND, a.fecha_hora, NOW()) AS hace_s,
                                 TRIM(CONCAT(IFNULL(u.nombre, ''), ' ', IFNULL(u.apellido_paterno, ''))) AS usuario
                          FROM ajustes_solucion a LEFT JOIN usuarios u ON u.idUsuario=a.idUsuario
                          WHERE a.idDispositivo=%s
                          ORDER BY a.fecha_hora DESC, a.idAjuste DESC LIMIT %s""", (device_id, limite))
        lista = []
        for fila in cursor.fetchall():
            lista.append(dict(idAjuste=fila['idAjuste'], fecha_hora=_texto_fecha(fila['fecha_hora']),
                              tipo=fila['tipo'], tipo_texto=NOMBRE_TIPO.get(fila['tipo'], fila['tipo']),
                              producto=fila['producto_nombre'], cantidad=_numero(fila['cantidad']),
                              unidad=fila['unidad'], litros_cubeta=_numero(fila['litros_cubeta']),
                              ce_antes=_numero(fila['ce_antes']), ce_estado=fila['ce_estado'],
                              ph_antes=_numero(fila['ph_antes']), recomendacion=fila['recomendacion'],
                              nota=fila['nota'], hace_s=fila['hace_s'], usuario=fila['usuario'] or None))
        return lista

    def rangos_mS(cursor, perfil):
        vacio = dict(disponible=False, minimo=None, maximo=None, detalle='Sin perfil.')
        if not perfil:
            return dict(ce=vacio, ph=vacio, temperatura_agua=vacio)
        try:
            filas = leer_catalogo(cursor)
        except ValueError as error:
            falta = dict(vacio, detalle=str(error))
            return dict(ce=falta, ph=falta, temperatura_agua=falta)
        salida = {}
        for variable, destino in (('ec', 'mS/cm'), ('ph', None), ('temperatura_agua', None)):
            ref = rango_referencia(filas, perfil['cultivo'], perfil['etapa'], variable)
            disponible = bool(ref.get('disponible'))
            minimo, maximo = ref.get('minimo'), ref.get('maximo')
            if disponible and destino:
                factor = factor_unidades(ref.get('unidad'), destino)
                if factor is None:
                    disponible = False
                else:
                    minimo, maximo = minimo * factor, maximo * factor
            salida['ce' if variable == 'ec' else variable] = dict(
                disponible=disponible, minimo=_redondear(minimo, 4) if disponible else None,
                maximo=_redondear(maximo, 4) if disponible else None,
                unidad=destino or ref.get('unidad'), detalle=ref.get('detalle') or '')
        return salida

    def contexto(cursor, device_id):
        perfil = leer_perfil(cursor, device_id)
        mapa = sensores(cursor, device_id)
        lecturas = dict(ce=lectura_ce(cursor, mapa.get('ec')),
                        ph=lectura_simple(cursor, mapa.get('ph')),
                        temperatura_agua=lectura_simple(cursor, mapa.get('temperatura_agua')),
                        nivel=lectura_nivel(device_id))
        cursor.execute("""SELECT fecha_hora, TIMESTAMPDIFF(SECOND, fecha_hora, NOW()) AS hace_s
                          FROM ajustes_solucion WHERE idDispositivo=%s
                          ORDER BY fecha_hora DESC, idAjuste DESC LIMIT 1""", (device_id,))
        ultimo = cursor.fetchone()
        ctx = dict(perfil=perfil, rangos=rangos_mS(cursor, perfil), lecturas=lecturas,
                   productos=productos(cursor, device_id, solo_activos=True),
                   ultimo_ajuste=dict(fecha=_texto_fecha(ultimo['fecha_hora']), hace_s=ultimo['hace_s'])
                   if ultimo else None)
        ctx['recomendaciones'] = recomendar(ctx)
        return ctx

    # ── Rutas ───────────────────────────────────────────────────────────
    def device_de(fuente):
        return _entero_positivo(fuente.get('device_id'), 'El dispositivo')

    @app.get('/dosificacion', endpoint='dosificacion_pagina')
    @login_requerido
    def dosificacion_pagina():
        respuesta = app.make_response(render_template_string(PAGINA_DOSIFICACION))
        respuesta.headers['Cache-Control'] = 'no-store, private'
        return respuesta

    @app.get('/api/dosificacion/estado', endpoint='dosificacion_estado')
    @login_requerido
    def dosificacion_estado():
        try:
            device_id = device_de(request.args)
            with _Sesion() as (_, cursor):
                _, acceso = permiso(cursor, device_id)
                ctx = contexto(cursor, device_id)
                ajustes = ultimos_ajustes(cursor, device_id)
                todos = productos(cursor, device_id)
            respuesta = jsonify(dispositivo=dict(idDispositivo=device_id, nombre=acceso['nombre'],
                                                 puede_editar=bool(acceso['puede_editar'])),
                                perfil=ctx['perfil'], rangos=ctx['rangos'], lecturas=ctx['lecturas'],
                                recomendaciones=ctx['recomendaciones'], productos=todos, ajustes=ajustes,
                                ultimo_ajuste=ctx['ultimo_ajuste'],
                                criterios=dict(edad_max_lectura_s=EDAD_MAX_LECTURA_S,
                                               espera_mezcla_min=ESPERA_MEZCLA_MIN,
                                               variacion_max_ce=VARIACION_MAX_CE,
                                               ventana_despues_min=list(VENTANA_DESPUES_MIN),
                                               limite_sensor_ce=LIMITE_SENSOR_CE))
            respuesta.headers['Cache-Control'] = 'no-store'
            return respuesta
        except Exception as error:
            return respuesta_error(error)

    def validar_producto(datos, existente=None):
        tipo = existente['tipo'] if existente else datos.get('tipo')
        if tipo not in TIPOS_PRODUCTO:
            raise ErrorDosis('Elige el tipo de producto.')
        campos = {}
        if existente is None:
            nombre = _texto_opcional(datos.get('nombre'), 'El nombre', 80)
            if not nombre:
                raise ErrorDosis('Escribe el nombre del producto.')
            campos.update(nombre=nombre, tipo=tipo, unidad=_unidad_valida(datos.get('unidad')))
        if 'aumento_ce' in datos or existente is None:
            valor = _decimal_opcional(datos.get('aumento_ce'), 'El aumento de CE', 0, 100)
            if valor is not None and tipo != 'fertilizante':
                raise ErrorDosis('El aumento de CE solo aplica a fertilizantes.')
            campos['aumento_ce'] = valor
        if 'ce_ms_cm' in datos or existente is None:
            valor = _decimal_opcional(datos.get('ce_ms_cm'), 'La CE del agua', 0, 20, incluir_minimo=True)
            if valor is not None and tipo != 'agua':
                raise ErrorDosis('La CE propia solo aplica al agua.')
            campos['ce_ms_cm'] = valor
        if 'nota' in datos or existente is None:
            campos['nota'] = _texto_opcional(datos.get('nota'), 'La nota', 255)
        if 'activo' in datos:
            if datos['activo'] not in (0, 1, True, False):
                raise ErrorDosis('El estado del producto no es válido.')
            campos['activo'] = 1 if datos['activo'] else 0
        return campos

    @app.post('/api/dosificacion/productos', endpoint='dosificacion_crear_producto')
    @login_requerido
    def dosificacion_crear_producto():
        try:
            datos = request.get_json(silent=True)
            if not isinstance(datos, dict):
                raise ErrorDosis('Envía los datos del producto en formato JSON.')
            device_id = device_de(datos)
            campos = validar_producto(datos)
            with _Sesion() as (conexion, cursor):
                permiso(cursor, device_id, escribir=True)
                cursor.execute("""INSERT INTO productos_solucion
                                  (idDispositivo, nombre, tipo, unidad, aumento_ce, ce_ms_cm, nota)
                                  VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                               (device_id, campos['nombre'], campos['tipo'], campos['unidad'],
                                campos['aumento_ce'], campos['ce_ms_cm'], campos['nota']))
                nuevo = cursor.lastrowid
                conexion.commit()
            return jsonify(status='ok', idProducto=nuevo), 201
        except Exception as error:
            return respuesta_error(error)

    @app.put('/api/dosificacion/productos/<int:id_producto>', endpoint='dosificacion_editar_producto')
    @login_requerido
    def dosificacion_editar_producto(id_producto):
        try:
            datos = request.get_json(silent=True)
            if not isinstance(datos, dict):
                raise ErrorDosis('Envía los cambios del producto en formato JSON.')
            with _Sesion() as (conexion, cursor):
                cursor.execute('SELECT idProducto, idDispositivo, tipo FROM productos_solucion WHERE idProducto=%s',
                               (id_producto,))
                existente = cursor.fetchone()
                if not existente:
                    raise ErrorDosis('Producto no encontrado.', 404)
                permiso(cursor, existente['idDispositivo'], escribir=True)
                campos = validar_producto(datos, existente)
                if not campos:
                    raise ErrorDosis('No hay cambios que guardar.')
                columnas = ', '.join(f'{c}=%s' for c in campos)
                cursor.execute(f'UPDATE productos_solucion SET {columnas} WHERE idProducto=%s',
                               (*campos.values(), id_producto))
                conexion.commit()
            return jsonify(status='ok')
        except Exception as error:
            return respuesta_error(error)

    @app.post('/api/dosificacion/ajustes', endpoint='dosificacion_registrar_ajuste')
    @login_requerido
    def dosificacion_registrar_ajuste():
        try:
            datos = request.get_json(silent=True)
            if not isinstance(datos, dict):
                raise ErrorDosis('Envía los datos del ajuste en formato JSON.')
            device_id = device_de(datos)
            tipo = datos.get('tipo')
            if tipo not in TIPOS_AJUSTE:
                raise ErrorDosis('Elige qué se hizo.')
            cantidad = _decimal_opcional(datos.get('cantidad'), 'La cantidad', 0, 100000)
            if tipo in TIPOS_CON_CANTIDAD and cantidad is None:
                raise ErrorDosis('Escribe la cantidad que agregaste.')
            litros = _decimal_opcional(datos.get('litros_cubeta'), 'Los litros de la cubeta', 0, 1000)
            nota = _texto_opcional(datos.get('nota'), 'La nota', 255)
            with _Sesion() as (conexion, cursor):
                usuario, _ = permiso(cursor, device_id, escribir=True)
                producto = None
                if datos.get('id_producto') not in (None, ''):
                    id_producto = _entero_positivo(datos['id_producto'], 'El producto')
                    cursor.execute("""SELECT idProducto, nombre, tipo, unidad FROM productos_solucion
                                      WHERE idProducto=%s AND idDispositivo=%s AND activo=1""",
                                   (id_producto, device_id))
                    producto = cursor.fetchone()
                    if not producto:
                        raise ErrorDosis('El producto no existe en este dispositivo o está desactivado.')
                    if tipo in TIPOS_CON_CANTIDAD and producto['tipo'] != tipo:
                        raise ErrorDosis('El producto no corresponde a lo que se hizo.')
                unidad = None
                if cantidad is not None:
                    unidad = producto['unidad'] if producto else _unidad_valida(datos.get('unidad'))
                # Copia de lo que había y de lo que recomendó el sistema justo antes de anotar.
                ctx = contexto(cursor, device_id)
                ce, ph, temp = ctx['lecturas']['ce'], ctx['lecturas']['ph'], ctx['lecturas']['temperatura_agua']
                texto, dosis = resumen_recomendacion(ctx['recomendaciones'], tipo)
                detalle = dict(perfil=ctx['perfil'], rangos=ctx['rangos'], lecturas=ctx['lecturas'],
                               recomendaciones=ctx['recomendaciones'],
                               criterios=dict(espera_mezcla_min=ESPERA_MEZCLA_MIN,
                                              variacion_max_ce=VARIACION_MAX_CE))
                fecha_lecturas = ce.get('fecha') if ce.get('valor') is not None else ph.get('fecha')
                cursor.execute("""INSERT INTO ajustes_solucion
                    (idDispositivo, idUsuario, tipo, idProducto, producto_nombre, cantidad, unidad,
                     litros_cubeta, ce_antes, ce_estado, ph_antes, temp_agua_antes, lecturas_fecha,
                     cultivo, etapa, dosis_recomendada_por_litro, recomendacion, detalle_json, nota)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (device_id, usuario, tipo, producto['idProducto'] if producto else None,
                     producto['nombre'] if producto else None, cantidad, unidad, litros,
                     _redondear(ce.get('valor'), 4), ce.get('estado'), _redondear(ph.get('valor'), 2),
                     _redondear(temp.get('valor'), 2), fecha_lecturas,
                     ctx['perfil']['cultivo'] if ctx['perfil'] else None,
                     ctx['perfil']['etapa'] if ctx['perfil'] else None,
                     dosis, texto, json.dumps(detalle, ensure_ascii=False, default=str), nota))
                nuevo = cursor.lastrowid
                conexion.commit()
            return jsonify(status='ok', idAjuste=nuevo), 201
        except Exception as error:
            return respuesta_error(error)

    # ── Hoja "Ajustes_solucion" del Excel ───────────────────────────────
    def hoja_ajustes(hoja_datos, datos):
        """Se llama desde _exportar_generar_excel (archivo 3). Lee la bitácora del
        mismo intervalo y calcula "después" con las lecturas ya exportadas."""
        device_id = datos['dispositivo']['idDispositivo']
        desde, hasta = datos['desde'], datos['hasta']
        conexion = conectar_bd()
        cursor = conexion.cursor(dictionary=True, buffered=True)
        try:
            cursor.execute("""SELECT a.*, TRIM(CONCAT(IFNULL(u.nombre, ''), ' ',
                                     IFNULL(u.apellido_paterno, ''))) AS usuario
                              FROM ajustes_solucion a LEFT JOIN usuarios u ON u.idUsuario=a.idUsuario
                              WHERE a.idDispositivo=%s AND a.fecha_hora>=%s AND a.fecha_hora<%s
                              ORDER BY a.fecha_hora, a.idAjuste""", (device_id, desde, hasta))
            ajustes = cursor.fetchall()
        except mysql.connector.Error as error:
            if error.errno == 1146:  # Sin migración 005: el Excel sale igual que antes.
                return
            raise
        finally:
            for recurso in (cursor, conexion):
                try:
                    recurso.close()
                except Exception:
                    pass

        # Lecturas ya exportadas en la hoja Lecturas (mismo intervalo).
        series = {'ec': [], 'ph': []}
        for lectura in datos.get('lecturas') or []:
            tipo = str(lectura.get('tipo_sensor') or '').strip().lower()
            if tipo not in series or lectura.get('fecha_hora') is None:
                continue
            valor = (_a_ms_cm(lectura.get('valor'), lectura.get('unidad_medida'), factor_unidades)
                     if tipo == 'ec' else _numero(lectura.get('valor')))
            if valor is not None:
                series[tipo].append((lectura['fecha_hora'], valor))

        inicio_min, fin_min = VENTANA_DESPUES_MIN
        filas = []
        for indice, ajuste in enumerate(ajustes):
            momento = ajuste['fecha_hora']
            inicio = momento + timedelta(minutes=inicio_min)
            fin = momento + timedelta(minutes=fin_min)
            notas = []
            ce_despues = ph_despues = None
            if fin > hasta:
                notas.append(f'La ventana de {inicio_min} a {fin_min} min queda fuera del intervalo '
                             'exportado; amplía la fecha final.')
            else:
                ce_vals = [v for f, v in series['ec'] if inicio <= f <= fin]
                ph_vals = [v for f, v in series['ph'] if inicio <= f <= fin]
                ce_despues = round(statistics.median(ce_vals), 4) if ce_vals else None
                ph_despues = round(statistics.median(ph_vals), 2) if ph_vals else None
                notas.append(f'Mediana de las lecturas entre {inicio_min} y {fin_min} min después '
                             f'({len(ce_vals)} de CE, {len(ph_vals)} de pH).')
                if not ce_vals:
                    notas.append('Sin lecturas válidas de CE en la ventana (posible CE >= 2.0 o sensor sin lectura).')
            siguiente = ajustes[indice + 1]['fecha_hora'] if indice + 1 < len(ajustes) else None
            if siguiente is not None and siguiente <= fin:
                notas.append('Hubo otro ajuste antes de terminar la ventana: el "después" incluye ambos.')
            filas.append(dict(
                idAjuste=ajuste['idAjuste'], fecha_hora=momento,
                tipo=NOMBRE_TIPO.get(ajuste['tipo'], ajuste['tipo']),
                producto_nombre=ajuste['producto_nombre'], cantidad=_numero(ajuste['cantidad']),
                unidad=ajuste['unidad'], litros_cubeta=_numero(ajuste['litros_cubeta']),
                cultivo=ajuste['cultivo'], etapa=ajuste['etapa'],
                ce_antes=_numero(ajuste['ce_antes']), ce_estado=ajuste['ce_estado'],
                ph_antes=_numero(ajuste['ph_antes']), temp_agua_antes=_numero(ajuste['temp_agua_antes']),
                dosis_recomendada_por_litro=_numero(ajuste['dosis_recomendada_por_litro']),
                recomendacion=ajuste['recomendacion'], ce_despues=ce_despues, ph_despues=ph_despues,
                nota_despues=' '.join(notas), usuario=ajuste['usuario'] or None, nota=ajuste['nota']))

        hoja_datos('Ajustes_solucion', [
            ('idAjuste', 'idAjuste', 12), ('fecha_hora', 'Fecha y hora del servidor', 24),
            ('tipo', 'Qué se hizo', 16), ('producto_nombre', 'Producto', 26),
            ('cantidad', 'Cantidad agregada', 17), ('unidad', 'Unidad', 9),
            ('litros_cubeta', 'Litros en la cubeta', 17), ('cultivo', 'Cultivo', 13),
            ('etapa', 'Etapa', 13), ('ce_antes', 'CE antes (mS/cm)', 16),
            ('ce_estado', 'Estado de la CE', 16), ('ph_antes', 'pH antes (sin calibrar)', 18),
            ('temp_agua_antes', 'Temp. del agua antes (°C)', 20),
            ('dosis_recomendada_por_litro', 'Dosis recomendada por litro', 20),
            ('recomendacion', 'Lo que recomendó el sistema', 42),
            ('ce_despues', 'CE después (mS/cm)', 17), ('ph_despues', 'pH después', 12),
            ('nota_despues', 'Cómo se calculó "después"', 48), ('usuario', 'Registró', 22),
            ('nota', 'Nota', 32),
        ], filas)

    extras = n.get('HOJAS_EXTRA_EXCEL')
    if isinstance(extras, list) and hoja_ajustes not in extras:
        extras.append(hoja_ajustes)
    print('Dosificación asistida lista: página /dosificacion'
          + (' y hoja Ajustes_solucion en el Excel.' if isinstance(extras, list) else '.'))


# ── Página /dosificacion (también se muestra dentro del panel) ──────────
PAGINA_DOSIFICACION = r'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nutrientes · Hydrosense</title>
<style>
:root{font-family:system-ui,sans-serif;color:#213b30;background:#f3f7f4;color-scheme:light}
*{box-sizing:border-box}body{margin:0}main{max-width:1100px;margin:auto;padding:24px 20px}
header{display:flex;align-items:center;justify-content:space-between;gap:20px}
h1{margin:6px 0;font-size:1.7rem}h2{font-size:1.1rem;margin:0 0 12px}a{color:#166347}
p{line-height:1.5}section{background:white;border:1px solid #d9e5dd;border-radius:12px;padding:20px;margin-top:18px}
.fila{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end}
label{display:grid;gap:6px;font-weight:600;flex:1;min-width:170px}
select,input,button,textarea{font:inherit;padding:9px 11px;border-radius:7px;border:1px solid #a9bfb0;background:white;color:#213b30}
button{cursor:pointer;background:#176448;color:white;font-weight:600;border-color:#176448}
button.secundario{background:white;color:#176448}button:disabled{opacity:.45;cursor:default}
.nota,small{color:#526d5e;font-size:.9rem}
.tarjetas{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.tarjeta{border:1px solid #e1ebe4;border-radius:10px;padding:12px;background:#f9fbfa}
.tarjeta b{display:block;font-size:1.35rem;margin-top:4px}
ul.recs{list-style:none;padding:0;margin:14px 0 0;display:grid;gap:10px}
ul.recs li{border:1px solid #e1ebe4;border-left:5px solid #9bb3a4;border-radius:8px;padding:11px 13px}
ul.recs li.accion{border-left-color:#176448}ul.recs li.espera,ul.recs li.aviso{border-left-color:#8a6d1f}
ul.recs li strong{display:block;margin-bottom:3px}.total{margin-top:6px;font-weight:600}
.tabla{overflow:auto}table{width:100%;border-collapse:collapse;font-size:.92rem}
th,td{text-align:left;padding:9px 8px;border-bottom:1px solid #e5ece7;vertical-align:top}
th{font-size:.78rem;text-transform:uppercase;letter-spacing:.03em;white-space:nowrap}
#mensaje{min-height:1.4em;margin:12px 0 0;font-weight:600}#mensaje.error{color:#a22626}#mensaje.exito{color:#176448}
td small{display:block;margin-top:4px}
details{margin-top:12px}summary{cursor:pointer;color:#166347;font-weight:600}
.acciones{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
td input{width:110px}.inactivo{color:#8a9a90}
@media(max-width:600px){main{padding:16px 10px}section{padding:14px}}
</style>
</head>
<body><main>
<header><div><span class="nota">HYDROSENSE · Actividad 2</span><h1>Solución nutritiva</h1></div>
<a id="volver" href="/dashboard">Volver al panel</a></header>
<p class="nota">El sistema compara la CE y el pH con el rango del cultivo y su etapa y dice cuánto agregar por litro.
La persona agrega el producto a mano en la cubeta y lo anota aquí.</p>

<section aria-label="Dispositivo">
<div class="fila">
<label>Dispositivo<select id="dispositivo"><option value="">Cargando…</option></select></label>
<label>Litros en la cubeta (opcional)<input id="litros" type="number" min="0.1" max="1000" step="0.1" placeholder="Ej. 15"></label>
</div>
<p id="perfil" class="nota"></p>
</section>

<section aria-label="Estado de la cubeta">
<h2>Estado de la cubeta</h2>
<div class="tarjetas">
<div class="tarjeta"><small>CE</small><b id="vCe">—</b><small id="dCe"></small></div>
<div class="tarjeta"><small>pH (sin calibrar)</small><b id="vPh">—</b><small id="dPh"></small></div>
<div class="tarjeta"><small>Temperatura del agua</small><b id="vTemp">—</b><small id="dTemp"></small></div>
<div class="tarjeta"><small>Nivel de agua</small><b id="vNivel">—</b><small id="dNivel"></small></div>
</div>
<ul class="recs" id="recomendaciones"></ul>
<div class="acciones"><button id="abrirAnotar" type="button">Anotar lo que agregué</button>
<button id="actualizar" class="secundario" type="button">Actualizar</button></div>
<p class="nota" id="actualizado"></p>
</section>

<section id="panelAnotar" hidden aria-label="Anotar ajuste">
<h2>Anotar lo que agregué</h2>
<div class="fila">
<label>Qué se hizo<select id="aTipo">
<option value="fertilizante">Fertilizante</option><option value="agua">Agua</option>
<option value="ph_bajar">Bajar pH</option><option value="ph_subir">Subir pH</option>
<option value="solucion_nueva">Solución nueva (cambio completo)</option><option value="otro">Otro</option>
</select></label>
<label>Producto<select id="aProducto"></select></label>
<label>Cantidad agregada<input id="aCantidad" type="number" min="0" step="0.01"></label>
<label>Unidad<select id="aUnidad"><option>mL</option><option>g</option><option>L</option></select></label>
</div>
<div class="fila" style="margin-top:12px">
<label>Litros en la cubeta<input id="aLitros" type="number" min="0.1" max="1000" step="0.1"></label>
<label style="flex:3">Nota (opcional)<input id="aNota" maxlength="255"></label>
</div>
<p class="nota">Al guardar, el sistema copia la CE, el pH y la temperatura de ese momento y lo que recomendaba.</p>
<div class="acciones"><button id="guardarAjuste" type="button">Guardar</button>
<button id="cancelarAjuste" class="secundario" type="button">Cancelar</button></div>
</section>

<p id="mensaje" role="status"></p>

<section aria-label="Bitácora">
<h2>Bitácora</h2>
<div class="tabla"><table><thead><tr><th>Fecha</th><th>Qué se hizo</th><th>Producto</th><th>Cantidad</th>
<th>Litros</th><th>CE antes</th><th>pH antes</th><th>Recomendación</th><th>Registró</th></tr></thead>
<tbody id="bitacora"></tbody></table></div>
<p class="nota">La CE y el pH de "después" salen en la hoja Ajustes_solucion del Excel de exportación.</p>
</section>

<section aria-label="Mis productos">
<h2>Mis productos</h2>
<div class="tabla"><table><thead><tr><th>Nombre</th><th>Tipo</th><th>Unidad</th><th>Dato para el cálculo</th><th></th></tr></thead>
<tbody id="productos"></tbody></table></div>
<details><summary>Agregar producto</summary>
<div class="fila" style="margin-top:12px">
<label>Nombre<input id="pNombre" maxlength="80" placeholder="Ej. Fertilizante A+B"></label>
<label>Tipo<select id="pTipo"><option value="fertilizante">Fertilizante</option><option value="agua">Agua</option>
<option value="ph_bajar">Bajar pH (pH−)</option><option value="ph_subir">Subir pH (pH+)</option><option value="otro">Otro</option></select></label>
<label>Unidad<select id="pUnidad"><option>mL</option><option>g</option><option>L</option></select></label>
<label id="pDatoEtiqueta">Aumento de CE (mS/cm por unidad por litro)<input id="pDato" type="number" min="0" step="0.0001"></label>
</div>
<label style="margin-top:12px">Nota (opcional)<input id="pNota" maxlength="255"></label>
<div class="acciones"><button id="guardarProducto" type="button">Guardar producto</button></div>
</details>
<details><summary>¿Cómo medir el dato del fertilizante?</summary>
<p>En 1 litro de agua de la llave mide la CE. Agrega 1 mL (o 1 g) del fertilizante, revuelve y mide otra vez.
La diferencia es el dato, en mS/cm por cada mL por litro. Si tu fertilizante es A y B, regístralo como un solo
producto y mide agregando 1 mL de cada parte; entonces la dosis que dé el sistema será "mL de cada parte".</p>
<p>Otra forma: al preparar una solución nueva, anota los litros y los mL. Con la CE de antes y la de después
(hoja Ajustes_solucion del Excel): dato = (CE después − CE antes) ÷ (mL ÷ litros).</p>
<p>Para el agua, registra un producto de tipo Agua con la CE del agua de la llave sola.</p>
</details>
</section>
</main>
<script>
'use strict';
const $ = id => document.getElementById(id);
const fmt = (v, d = 2) => v === null || v === undefined ? '—' : Number(v).toLocaleString('es-MX', {maximumFractionDigits: d});
const TIPOS = {solucion_nueva:'Solución nueva', fertilizante:'Fertilizante', agua:'Agua', ph_bajar:'Bajar pH', ph_subir:'Subir pH', otro:'Otro'};
const NIVEL = {listo:'Suficiente', bajo:'Bajo', invalido:'Lectura no válida', vencido:'Lectura vencida', sin_lectura:'Esperando lectura', desactivada:'Protección desactivada'};
const ESTADO_CE = {ok:'calibrada', sin_calibrar:'sin calibrar', fuera_rango:'≥ 2.0 mS/cm, fuera del sensor', calibrando:'en calibración', senal_baja:'señal muy baja', sin_temperatura:'sin temperatura del agua', sin_lectura_reciente:'sin lectura reciente', sin_sensor:'sin sensor registrado', sin_lectura:'sin lectura', sin_metadatos:'sin diagnóstico'};
let dispositivo = '', datos = null, ocupado = false, temporizador = null;
if (window.top !== window.self) $('volver').hidden = true;

function mensaje(texto, tipo = '') { $('mensaje').textContent = texto; $('mensaje').className = tipo; }
async function api(url, opciones = {}) {
  const respuesta = await fetch(url, {credentials: 'same-origin', cache: 'no-store', ...opciones});
  if (respuesta.status === 401) { window.top.location.assign('/'); throw new Error('Inicia sesión.'); }
  let dato = {};
  try { dato = await respuesta.json(); } catch (e) { dato = {}; }
  if (!respuesta.ok) throw new Error(dato.error || dato.mensaje || 'No se pudo completar la solicitud.');
  return dato;
}
function enviar(url, metodo, cuerpo) {
  return api(url, {method: metodo, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(cuerpo)});
}
function litros() { const v = parseFloat($('litros').value); return v > 0 ? v : null; }
function guardarLitros() { try { localStorage.setItem('hs_litros_' + dispositivo, $('litros').value); } catch (e) {} }
function leerLitros() { try { $('litros').value = localStorage.getItem('hs_litros_' + dispositivo) || ''; } catch (e) {} }
function celda(fila, texto) { const c = fila.insertCell(); c.textContent = texto === null || texto === undefined || texto === '' ? '—' : texto; return c; }

function pintarLecturas() {
  const l = datos.lecturas, r = datos.rangos;
  const p = datos.perfil;
  $('perfil').textContent = p ? 'Perfil: ' + p.cultivo + ' · etapa ' + p.etapa + (r.ce.disponible ? ' · CE ideal ' + fmt(r.ce.minimo) + ' a ' + fmt(r.ce.maximo) + ' mS/cm' : '') + (r.ph.disponible ? ' · pH ' + fmt(r.ph.minimo, 1) + ' a ' + fmt(r.ph.maximo, 1) : '') + '. Se cambia en la página Rangos.' : 'Sin perfil: elige cultivo y etapa en la página Rangos.';
  $('vCe').textContent = l.ce.valor === null ? '—' : fmt(l.ce.valor) + ' mS/cm';
  $('dCe').textContent = ESTADO_CE[l.ce.estado] || l.ce.estado || '';
  $('vPh').textContent = l.ph.valor === null ? '—' : fmt(l.ph.valor, 1);
  $('dPh').textContent = l.ph.fecha ? 'Lectura: ' + l.ph.fecha : '';
  $('vTemp').textContent = l.temperatura_agua.valor === null ? '—' : fmt(l.temperatura_agua.valor, 1) + ' °C';
  $('dTemp').textContent = l.temperatura_agua.fecha ? 'Lectura: ' + l.temperatura_agua.fecha : '';
  const n = l.nivel || {};
  $('vNivel').textContent = n.vigente ? (NIVEL[n.estado] || n.estado || '—') : 'Sin reporte';
  $('dNivel').textContent = n.vigente && n.distancia_cm !== null && n.distancia_cm !== undefined ? 'Distancia: ' + fmt(n.distancia_cm, 1) + ' cm' : '';
}
function pintarRecomendaciones() {
  const lista = $('recomendaciones'); lista.replaceChildren();
  const L = litros();
  for (const item of datos.recomendaciones) {
    const li = document.createElement('li'); li.className = item.estado;
    const titulo = document.createElement('strong'); titulo.textContent = item.titulo;
    const texto = document.createElement('span'); texto.textContent = item.texto;
    li.append(titulo, texto);
    if (item.dosis_por_litro !== null && item.estado === 'accion') {
      const total = document.createElement('div'); total.className = 'total';
      total.textContent = L ? 'Con ' + fmt(L, 1) + ' L en la cubeta: ≈ ' + fmt(item.dosis_por_litro * L, item.unidad === 'L' ? 2 : 1) + ' ' + item.unidad + '.' : 'Escribe arriba los litros de la cubeta para ver el total.';
      li.append(total);
    }
    if (item.estado === 'accion' && item.tipo_ajuste && datos.dispositivo.puede_editar) {
      const boton = document.createElement('button'); boton.type = 'button'; boton.className = 'secundario';
      boton.textContent = 'Anotar esto'; boton.style.marginTop = '8px';
      boton.addEventListener('click', () => abrirAnotar(item));
      li.append(document.createElement('br'), boton);
    }
    lista.append(li);
  }
  if (!datos.recomendaciones.length) { const li = document.createElement('li'); li.textContent = 'Sin recomendaciones.'; lista.append(li); }
}
function pintarBitacora() {
  const cuerpo = $('bitacora'); cuerpo.replaceChildren();
  for (const a of datos.ajustes) {
    const fila = cuerpo.insertRow();
    celda(fila, a.fecha_hora); celda(fila, a.tipo_texto); celda(fila, a.producto);
    celda(fila, a.cantidad === null ? '' : fmt(a.cantidad, 3) + ' ' + (a.unidad || ''));
    celda(fila, a.litros_cubeta === null ? '' : fmt(a.litros_cubeta, 2));
    celda(fila, a.ce_antes === null ? '' : fmt(a.ce_antes) + (a.ce_estado === 'sin_calibrar' ? ' (sin calibrar)' : ''));
    celda(fila, a.ph_antes === null ? '' : fmt(a.ph_antes, 1));
    celda(fila, a.recomendacion); celda(fila, a.usuario);
  }
  if (!datos.ajustes.length) { const fila = cuerpo.insertRow(); const c = fila.insertCell(); c.colSpan = 9; c.textContent = 'Todavía no hay ajustes anotados.'; }
}
function textoDato(p) {
  if (p.tipo === 'fertilizante') return p.aumento_ce === null ? 'Falta medir' : fmt(p.aumento_ce, 4) + ' mS/cm por ' + p.unidad + '/L';
  if (p.tipo === 'agua') return p.ce_ms_cm === null ? 'Falta su CE' : 'CE ' + fmt(p.ce_ms_cm, 3) + ' mS/cm';
  return 'No aplica';
}
function pintarProductos() {
  const cuerpo = $('productos'); cuerpo.replaceChildren();
  for (const p of datos.productos) {
    const fila = cuerpo.insertRow(); if (!p.activo) fila.className = 'inactivo';
    celda(fila, p.nombre + (p.activo ? '' : ' (desactivado)')); celda(fila, TIPOS[p.tipo] || p.tipo); celda(fila, p.unidad);
    const dato = fila.insertCell();
    if (p.activo && (p.tipo === 'fertilizante' || p.tipo === 'agua') && datos.dispositivo.puede_editar) {
      const entrada = document.createElement('input'); entrada.type = 'number'; entrada.min = '0'; entrada.step = '0.0001';
      const actual = p.tipo === 'fertilizante' ? p.aumento_ce : p.ce_ms_cm; if (actual !== null) entrada.value = actual;
      const guardar = document.createElement('button'); guardar.type = 'button'; guardar.className = 'secundario'; guardar.textContent = 'Guardar';
      guardar.style.marginLeft = '6px';
      guardar.addEventListener('click', () => editarProducto(p.idProducto, {[p.tipo === 'fertilizante' ? 'aumento_ce' : 'ce_ms_cm']: entrada.value === '' ? null : entrada.value}));
      const ayuda = document.createElement('small'); ayuda.textContent = textoDato(p);
      dato.append(entrada, guardar, ayuda);
    } else dato.textContent = textoDato(p);
    const accion = fila.insertCell();
    if (datos.dispositivo.puede_editar) {
      const boton = document.createElement('button'); boton.type = 'button'; boton.className = 'secundario';
      boton.textContent = p.activo ? 'Desactivar' : 'Activar';
      boton.addEventListener('click', () => editarProducto(p.idProducto, {activo: p.activo ? 0 : 1}));
      accion.append(boton);
    }
  }
  if (!datos.productos.length) { const fila = cuerpo.insertRow(); const c = fila.insertCell(); c.colSpan = 5; c.textContent = 'Agrega tu fertilizante y el agua que usas para que el sistema calcule las dosis.'; }
}
function llenarProductos(tipo, seleccionado) {
  const select = $('aProducto'); select.replaceChildren();
  const ninguno = document.createElement('option'); ninguno.value = ''; ninguno.textContent = 'Sin producto'; select.append(ninguno);
  for (const p of datos.productos.filter(p => p.activo && (p.tipo === tipo || tipo === 'solucion_nueva' || tipo === 'otro'))) {
    const op = document.createElement('option'); op.value = p.idProducto; op.textContent = p.nombre + ' (' + p.unidad + ')'; select.append(op);
  }
  select.value = seleccionado ? String(seleccionado) : (select.options.length > 1 ? select.options[1].value : '');
  ajustarUnidad();
}
function ajustarUnidad() {
  const p = datos.productos.find(p => String(p.idProducto) === $('aProducto').value);
  if (p) { $('aUnidad').value = p.unidad; $('aUnidad').disabled = true; } else $('aUnidad').disabled = false;
}
function abrirAnotar(item) {
  $('panelAnotar').hidden = false;
  $('aTipo').value = item && item.tipo_ajuste ? item.tipo_ajuste : 'fertilizante';
  llenarProductos($('aTipo').value, item ? item.id_producto : null);
  const L = litros(); $('aLitros').value = L || '';
  $('aCantidad').value = item && item.dosis_por_litro !== null && L ? (Math.round(item.dosis_por_litro * L * 100) / 100) : '';
  if (item && item.unidad && !$('aProducto').value) $('aUnidad').value = item.unidad;
  $('aNota').value = '';
  $('panelAnotar').scrollIntoView({behavior: 'smooth', block: 'start'});
}
async function guardarAjuste() {
  if (ocupado) return; ocupado = true; $('guardarAjuste').disabled = true;
  try {
    await enviar('/api/dosificacion/ajustes', 'POST', {device_id: dispositivo, tipo: $('aTipo').value,
      id_producto: $('aProducto').value || null, cantidad: $('aCantidad').value === '' ? null : $('aCantidad').value,
      unidad: $('aUnidad').value, litros_cubeta: $('aLitros').value === '' ? null : $('aLitros').value, nota: $('aNota').value});
    $('panelAnotar').hidden = true; mensaje('Ajuste anotado. Espera a que se mezcle antes de agregar más.', 'exito');
    await cargarEstado();
  } catch (e) { mensaje(e.message, 'error'); }
  finally { ocupado = false; $('guardarAjuste').disabled = false; }
}
async function guardarProducto() {
  const tipo = $('pTipo').value, dato = $('pDato').value;
  const cuerpo = {device_id: dispositivo, nombre: $('pNombre').value, tipo, unidad: $('pUnidad').value, nota: $('pNota').value};
  if (tipo === 'fertilizante') cuerpo.aumento_ce = dato === '' ? null : dato;
  if (tipo === 'agua') cuerpo.ce_ms_cm = dato === '' ? null : dato;
  try {
    await enviar('/api/dosificacion/productos', 'POST', cuerpo);
    for (const id of ['pNombre', 'pDato', 'pNota']) $(id).value = '';
    mensaje('Producto guardado.', 'exito'); await cargarEstado();
  } catch (e) { mensaje(e.message, 'error'); }
}
async function editarProducto(id, cambios) {
  try { await enviar('/api/dosificacion/productos/' + id, 'PUT', cambios); mensaje('Producto actualizado.', 'exito'); await cargarEstado(); }
  catch (e) { mensaje(e.message, 'error'); }
}
function ajustarFormularioProducto() {
  const tipo = $('pTipo').value, etiqueta = $('pDatoEtiqueta');
  etiqueta.hidden = !(tipo === 'fertilizante' || tipo === 'agua');
  etiqueta.firstChild.textContent = tipo === 'agua' ? 'CE del agua sola (mS/cm)' : 'Aumento de CE (mS/cm por unidad por litro)';
  if (tipo === 'agua') $('pUnidad').value = 'L';
}
async function cargarEstado() {
  if (!dispositivo) return;
  try {
    datos = await api('/api/dosificacion/estado?device_id=' + encodeURIComponent(dispositivo));
    pintarLecturas(); pintarRecomendaciones(); pintarBitacora(); pintarProductos();
    const editar = datos.dispositivo.puede_editar;
    $('abrirAnotar').disabled = !editar; $('guardarProducto').disabled = !editar;
    $('actualizado').textContent = 'Actualizado: ' + new Date().toLocaleTimeString('es-MX') + (editar ? '' : ' · Tu acceso es solo de consulta.');
  } catch (e) { mensaje(e.message, 'error'); }
}
async function iniciar() {
  try {
    const lista = await api('/api/dispositivos/lista');
    const select = $('dispositivo'); select.replaceChildren();
    if (!Array.isArray(lista) || !lista.length) { const op = document.createElement('option'); op.textContent = 'Sin dispositivos'; select.append(op); return; }
    for (const d of lista) { const op = document.createElement('option'); op.value = d.idDispositivo; op.textContent = d.nombre + ' (ID ' + d.idDispositivo + ')'; select.append(op); }
    dispositivo = select.value; leerLitros(); await cargarEstado();
  } catch (e) { mensaje(e.message, 'error'); }
  temporizador = setInterval(() => { if (document.visibilityState === 'visible' && $('panelAnotar').hidden) cargarEstado(); }, 30000);
}
$('dispositivo').addEventListener('change', () => { dispositivo = $('dispositivo').value; $('panelAnotar').hidden = true; leerLitros(); cargarEstado(); });
$('litros').addEventListener('input', () => { guardarLitros(); if (datos) pintarRecomendaciones(); });
$('abrirAnotar').addEventListener('click', () => abrirAnotar(null));
$('cancelarAjuste').addEventListener('click', () => { $('panelAnotar').hidden = true; });
$('guardarAjuste').addEventListener('click', guardarAjuste);
$('aTipo').addEventListener('change', () => llenarProductos($('aTipo').value, null));
$('aProducto').addEventListener('change', ajustarUnidad);
$('guardarProducto').addEventListener('click', guardarProducto);
$('pTipo').addEventListener('change', ajustarFormularioProducto);
$('actualizar').addEventListener('click', cargarEstado);
ajustarFormularioProducto();
iniciar();
</script>
</body>
</html>'''
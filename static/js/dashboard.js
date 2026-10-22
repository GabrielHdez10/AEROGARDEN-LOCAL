// Sensores de agua: DS18B20 y EC/TDS. Compatible con app.py del paso 2.
// TDS se muestra como estimación asociada a EC, no como otro sensor físico.
const AGUA_ESTADOS_TEXTO = Object.freeze({
    sin_calibrar: 'EC estimada · sin calibrar',
    sin_metadatos: 'Medición sin verificación de calibración',
    sin_temperatura: 'Sin temperatura válida para compensar la EC',
    sin_sensor: 'Sensor sin lectura válida',
    iniciando: 'Esperando la primera lectura',
    senal_baja: 'Señal muy baja · revisar inmersión y conexiones',
    fuera_rango: 'Fuera del rango del módulo TDS',
    calibrando: 'Calibración en curso',
    lectura_invalida: 'Lectura inválida'
});
const AGUA_REGISTROS_PENDIENTES = new Set();
let revisionPanelSensores = 0;
let revisionVisualizacionSensores = 0;
let revisionHistorialSensores = 0;
let intervaloPanelSensoresAgua = null;

function escaparTextoSensor(texto) {
    return String(texto ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[c]);
}

function numeroLecturaSensor(valor) {
    if (valor === null || valor === undefined || typeof valor === 'boolean' ||
        (typeof valor === 'string' && !valor.trim())) return null;
    if (typeof valor !== 'number' && typeof valor !== 'string') return null;
    const numero = Number(valor);
    return Number.isFinite(numero) ? numero : null;
}

function nombreSensorAgua(tipo) {
    return ({ec: 'EC estimada', temperatura_agua: 'Temperatura del agua (DS18B20)'})[
        String(tipo || '').trim().toLowerCase()] || tipo || 'Sensor';
}

function textoCalidadAgua(dato) {
    if (!dato?.estado_medicion) return '';
    if (dato.estado_medicion === 'ok') {
        return dato.factor_k != null || dato.tds_ppm != null
            ? 'EC estimada · calibrada a un punto' : 'Lectura válida';
    }
    return AGUA_ESTADOS_TEXTO[dato.estado_medicion] || 'Revisar estado de la medición';
}

function valorGraficaSensor(dato) {
    if (dato?.estado_medicion && !['ok', 'sin_calibrar', 'sin_metadatos'].includes(dato.estado_medicion)) return null;
    return numeroLecturaSensor(dato?.valor);
}

function edadLecturaSensor(dato) {
    const edad = numeroLecturaSensor(dato?.edad_segundos);
    if (edad !== null) return Math.max(0, edad);
    if (!dato?.fecha_hora) return null;
    const fecha = new Date(String(dato.fecha_hora).replace(' ', 'T')).getTime();
    return Number.isFinite(fecha) ? Math.max(0, (Date.now() - fecha) / 1000) : null;
}

function lecturaSensorAntigua(dato) {
    const edad = edadLecturaSensor(dato);
    return dato?.estado_rango === 'sin_datos_recientes' || edad === null || edad > 30;
}

function colorCalidadSensor(dato) {
    if (valorGraficaSensor(dato) === null) return '#9b6500';
    return dato?.estado_medicion && dato.estado_medicion !== 'ok' ? '#b8790b' : '#287454';
}

function detalleTDSSensor(dato) {
    const tds = numeroLecturaSensor(dato?.tds_ppm);
    return valorGraficaSensor(dato) !== null && tds !== null &&
        ['ok', 'sin_calibrar'].includes(dato?.estado_medicion)
        ? `TDS estimado: ${tds.toLocaleString('es-MX', {maximumFractionDigits: 1})} ppm · factor 0.5` : '';
}

async function pedirDatosSensor(url, opciones = {}) {
    const respuesta = await fetch(url, {credentials: 'same-origin', ...opciones});
    let dato;
    try { dato = await respuesta.json(); }
    catch (_) { throw new Error('El servidor no devolvió una respuesta válida.'); }
    if (!respuesta.ok) throw new Error(dato?.error || dato?.mensaje || 'No se pudo completar la solicitud.');
    if (dato?.error) throw new Error(dato.error);
    return dato;
}

function montarOpcionesSensoresAgua() {
    for (const id of ['tipoSensor', 'editTipoSensor']) {
        const select = document.getElementById(id);
        if (!select) continue;
        if (![...select.options].some(o => o.value === 'temperatura_agua')) {
            select.add(new Option('Temperatura del agua (DS18B20)', 'temperatura_agua'));
        }
        const ec = [...select.options].find(o => o.value === 'ec');
        if (ec) ec.textContent = 'EC estimada (módulo TDS)';
    }
    for (const id of ['unidadMedida', 'editUnidadMedida']) {
        const select = document.getElementById(id);
        if (!select) continue;
        for (const [valor, texto] of [['°C', '°C (Temperatura)'], ['mS/cm', 'mS/cm (EC)'],
            ['µS/cm', 'µS/cm (EC)'], ['uS/cm', 'uS/cm (EC)'], ['μS/cm', 'μS/cm (EC)']]) {
            if (![...select.options].some(o => o.value === valor)) select.add(new Option(texto, valor));
        }
    }
    const tipo = document.getElementById('tipoSensor');
    if (tipo && !tipo.dataset.aguaPreparada) {
        tipo.dataset.aguaPreparada = '1';
        tipo.addEventListener('change', () => {
            if (tipo.value === 'temperatura_agua') document.getElementById('unidadMedida').value = '°C';
        });
    }
    const logica = document.getElementById('selectSensorLogica');
    if (logica && !logica.dataset.aguaPreparada) {
        logica.dataset.aguaPreparada = '1';
        logica.addEventListener('change', limpiarVisualizacion);
    }
    const analitica = document.getElementById('selectSensorAnalitica');
    if (analitica && !analitica.dataset.aguaPreparada) {
        analitica.dataset.aguaPreparada = '1';
        analitica.addEventListener('change', () => {
            ++revisionHistorialSensores;
            if (miGrafica) miGrafica.destroy();
            miGrafica = null;
        });
    }
    if (intervaloPanelSensoresAgua === null) {
        let consultando = false;
        intervaloPanelSensoresAgua = setInterval(async () => {
            const inicio = document.getElementById('dashboard-contenido');
            if (consultando || document.hidden || !inicio || getComputedStyle(inicio).display === 'none') return;
            consultando = true;
            try { await _panelLecturas(); }
            finally { consultando = false; }
        }, 10000);
    }
}

async function integrarSensoresAgua(idDispositivo) {
    const id = Number(idDispositivo);
    if (!Number.isInteger(id) || id <= 0 || AGUA_REGISTROS_PENDIENTES.has(id)) return;
    AGUA_REGISTROS_PENDIENTES.add(id);
    const botones = [...document.querySelectorAll(`[data-agua-dispositivo="${id}"]`)];
    botones.forEach(b => { b.disabled = true; b.textContent = 'Preparando sensores…'; });
    try {
        await pedirDatosSensor(`/api/dispositivos/${id}/integrar-sensores-agua`, {method: 'POST'});
        toast('Sensores de agua registrados. Los sensores existentes conservaron sus datos.', 'success');
        const resultados = await Promise.allSettled([
            actualizarListaDispositivos(), cargarSensoresEnLogica(), cargarSensoresEnAnalitica(), _panelLecturas(), _kpiDispSensores()
        ]);
        if (resultados.some(r => r.status === 'rejected')) {
            toast('El registro terminó. Recarga la página para actualizar las listas.', 'info');
        }
    } catch (error) {
        toast(escaparTextoSensor(error.message), 'error');
    } finally {
        AGUA_REGISTROS_PENDIENTES.delete(id);
        botones.forEach(b => { b.disabled = false; b.textContent = 'Integrar sensores de agua'; });
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', montarOpcionesSensoresAgua);
} else {
    montarOpcionesSensoresAgua();
}


// HYDROSENSE - HISTORIAL DE ALERTAS INTEGRADO - 29/09/2026
function toast(mensaje, tipo = 'info', duracion = 3500) {
    let contenedor = document.getElementById('toast-contenedor');
    if (!contenedor) {
        contenedor = document.createElement('div');
        contenedor.id = 'toast-contenedor';
        contenedor.style.cssText = `
            position: fixed; top: 20px; right: 20px;
            z-index: 9999; display: flex; flex-direction: column; gap: 10px;
            pointer-events: none;
        `;
        document.body.appendChild(contenedor);
    }

    const colores = {
        success: { bg: '#1e7e4a', icon: '✅', borde: '#27ae60' },
        error:   { bg: '#c0392b', icon: '❌', borde: '#e74c3c' },
        warning: { bg: '#d68910', icon: '⚠️', borde: '#f39c12' },
        info:    { bg: '#1a5276', icon: 'ℹ️', borde: '#2980b9' }
    };
    const c = colores[tipo] || colores.info;

    const t = document.createElement('div');
    t.style.cssText = `
        background: ${c.bg};
        color: white;
        padding: 14px 18px;
        border-radius: 10px;
        font-size: 14px;
        font-family: 'Segoe UI', sans-serif;
        box-shadow: 0 6px 24px rgba(0,0,0,0.25);
        display: flex; align-items: center; gap: 10px;
        min-width: 260px; max-width: 380px;
        border-left: 4px solid ${c.borde};
        pointer-events: all;
        cursor: pointer;
        opacity: 0;
        transform: translateX(40px);
        transition: opacity 0.3s ease, transform 0.3s ease;
    `;
    t.innerHTML = `<span style="font-size:18px;flex-shrink:0">${c.icon}</span><span style="flex:1;line-height:1.4">${mensaje}</span><span style="opacity:0.6;font-size:18px;flex-shrink:0">×</span>`;
    t.onclick = () => cerrarToast(t);
    contenedor.appendChild(t);

    requestAnimationFrame(() => {
        t.style.opacity = '1';
        t.style.transform = 'translateX(0)';
    });

    setTimeout(() => cerrarToast(t), duracion);
}

function cerrarToast(t) {
    t.style.opacity = '0';
    t.style.transform = 'translateX(40px)';
    setTimeout(() => t.remove(), 300);
}
function confirmar(mensaje) {
    return new Promise(function(resolve) {
        var prev = document.getElementById('modal-confirmar-custom');
        if (prev) prev.remove();

        var overlay = document.createElement('div');
        overlay.id = 'modal-confirmar-custom';
        overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:10000;display:flex;align-items:center;justify-content:center;';

        var box = document.createElement('div');
        box.style.cssText = 'background:white;border-radius:14px;padding:28px 30px;max-width:400px;width:90%;box-shadow:0 20px 60px rgba(0,0,0,0.3);';

        var header = document.createElement('div');
        header.style.cssText = 'display:flex;align-items:flex-start;gap:14px;margin-bottom:20px;';
        header.innerHTML = '<div style="width:42px;height:42px;border-radius:50%;background:#fff3e0;display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0;">⚠️</div><div><div style="font-weight:700;font-size:16px;color:#1a2e22;margin-bottom:6px;">¿Estás seguro?</div><div style="font-size:14px;color:#666;line-height:1.5;">' + mensaje + '</div></div>';

        var botones = document.createElement('div');
        botones.style.cssText = 'display:flex;gap:10px;justify-content:flex-end;';

        var btnNo = document.createElement('button');
        btnNo.textContent = 'Cancelar';
        btnNo.style.cssText = 'padding:9px 20px;border-radius:8px;border:1.5px solid #ddd;background:white;color:#555;font-size:14px;cursor:pointer;font-family:inherit;font-weight:500;';
        btnNo.onmouseover = function(){ this.style.background='#f5f5f5'; };
        btnNo.onmouseout  = function(){ this.style.background='white'; };

        var btnSi = document.createElement('button');
        btnSi.textContent = 'Sí, eliminar';
        btnSi.style.cssText = 'padding:9px 20px;border-radius:8px;border:none;background:#e74c3c;color:white;font-size:14px;cursor:pointer;font-family:inherit;font-weight:600;';
        btnSi.onmouseover = function(){ this.style.background='#c0392b'; };
        btnSi.onmouseout  = function(){ this.style.background='#e74c3c'; };

        botones.appendChild(btnNo);
        botones.appendChild(btnSi);
        box.appendChild(header);
        box.appendChild(botones);
        overlay.appendChild(box);
        document.body.appendChild(overlay);

        btnSi.onclick = function() { overlay.remove(); resolve(true); };
        btnNo.onclick = function() { overlay.remove(); resolve(false); };
        overlay.onclick = function(e) { if (e.target === overlay) { overlay.remove(); resolve(false); } };
    });
}
let dispositivoActualId = null;
let dispositivosGlobal = [];
let revisionDispositivosGlobal = 0;

function hayOrdenBombaPendiente() {
    return typeof bombaSolicitudActiva !== 'undefined' && bombaSolicitudActiva;
}

function sincronizarSelectoresControl() {
    for (const id of ['select-wifi-dispositivo', 'select-bomba-dispositivo']) {
        const selector = document.getElementById(id);
        if (selector) selector.value = dispositivoActualId ? String(dispositivoActualId) : '';
    }
}

function invalidarBombaAlCambiarDispositivo() {
    if (typeof ID_SENSOR_NIVEL !== 'undefined') ID_SENSOR_NIVEL = null;
    if (typeof bombaConfigConfirmada !== 'undefined') bombaConfigConfirmada = null;
    if (typeof bombaRevisionCarga !== 'undefined') ++bombaRevisionCarga;
    if (typeof actualizarIndicadorBomba === 'function') actualizarIndicadorBomba(null, null);
    if (typeof bombaPintarInicio === 'function') bombaPintarInicio(null);
    for (const id of ['input-tiempo-on', 'input-tiempo-off']) {
        const campo = document.getElementById(id);
        if (campo) campo.value = '';
    }
    const detalle = document.getElementById('detalle-config-bomba');
    if (detalle) detalle.textContent = 'Consulta la configuración del dispositivo seleccionado.';
    if (typeof actualizarPreviewBomba === 'function') actualizarPreviewBomba();
}

async function cargarDispositivosGlobal() {
    if (hayOrdenBombaPendiente()) return false;
    const revision = ++revisionDispositivosGlobal;
    try {
        const datos = await pedirDatosSensor('/api/dispositivos/lista', {cache: 'no-store'});
        if (revision !== revisionDispositivosGlobal || hayOrdenBombaPendiente()) return false;
        if (!Array.isArray(datos)) throw new Error('La lista de dispositivos no tiene un formato válido.');
        const ids = new Set();
        const nuevos = datos.map(dispositivo => {
            const idOriginal = dispositivo?.idDispositivo;
            const id = Number(idOriginal);
            if (!['number', 'string'].includes(typeof idOriginal) || !/^\d+$/.test(String(idOriginal)) ||
                !Number.isInteger(id) || id < 1 || id > 2147483647 || ids.has(id)) {
                throw new Error('La lista contiene un identificador de dispositivo inválido o repetido.');
            }
            ids.add(id);
            return {...dispositivo, idDispositivo: id};
        });
        const anterior = dispositivoActualId;
        dispositivosGlobal = nuevos;
        dispositivoActualId = ids.has(Number(anterior)) ? Number(anterior) : (nuevos[0]?.idDispositivo ?? null);
        if (anterior !== dispositivoActualId) invalidarBombaAlCambiarDispositivo();
        _llenarSelectoresDispositivo();
        return true;
    } catch (error) {
        if (revision === revisionDispositivosGlobal && !hayOrdenBombaPendiente()) {
            dispositivosGlobal = [];
            dispositivoActualId = null;
            invalidarBombaAlCambiarDispositivo();
            _llenarSelectoresDispositivo('No se pudieron cargar los dispositivos');
            if (typeof bombaMostrarMensaje === 'function') {
                bombaMostrarMensaje('No se pudieron cargar los dispositivos. Revisa la conexión y vuelve a consultar.', true);
            }
            console.error('Error cargando dispositivos globales:', error);
        }
        return false;
    }
}

function _llenarSelectoresDispositivo(mensajeVacio = 'Sin dispositivos disponibles') {
    for (const id of ['select-wifi-dispositivo', 'select-bomba-dispositivo']) {
        const selector = document.getElementById(id);
        if (!selector) continue;
        // Los nombres son texto; nunca se interpretan como etiquetas HTML.
        const opciones = dispositivosGlobal.map(dispositivo => new Option(
            String(dispositivo.nombre ?? ('Dispositivo ' + dispositivo.idDispositivo)),
            String(dispositivo.idDispositivo)
        ));
        if (!opciones.length) opciones.push(new Option(mensajeVacio, ''));
        selector.replaceChildren(...opciones);
    }
    sincronizarSelectoresControl();
}

function seleccionarDispositivoControl(idSelector) {
    const selector = document.getElementById(idSelector);
    if (!selector) return false;
    if (hayOrdenBombaPendiente()) {
        sincronizarSelectoresControl();
        if (typeof bombaMostrarMensaje === 'function') bombaMostrarMensaje('Espera a que termine la orden de la bomba antes de cambiar de dispositivo.');
        return false;
    }
    const id = Number(selector.value);
    if (!Number.isInteger(id) || !dispositivosGlobal.some(dispositivo => dispositivo.idDispositivo === id)) {
        sincronizarSelectoresControl();
        if (typeof bombaMostrarMensaje === 'function') bombaMostrarMensaje('Selecciona un dispositivo disponible en la lista.', true);
        return false;
    }
    if (id !== dispositivoActualId) {
        dispositivoActualId = id;
        invalidarBombaAlCambiarDispositivo();
    }
    sincronizarSelectoresControl();
    return true;
}

function cambiarDispositivoWifi() {
    if (!seleccionarDispositivoControl('select-wifi-dispositivo')) return false;
    return cargarRedActual();
}

function cambiarDispositivoBomba() {
    if (!seleccionarDispositivoControl('select-bomba-dispositivo')) return false;
    return cargarSeccionRelevador();
}

function mostrarSeccion(idSeccion) {
    const elementos = document.querySelectorAll('.seccion, #dashboard-contenido');
    elementos.forEach(s => { s.style.display = 'none'; });
    const target = document.getElementById(idSeccion);
    if (target) target.style.display = 'block';
    if (idSeccion === 'dashboard-contenido') cargarDashboardInicio();
    if (idSeccion === 'seccion-hardware')  actualizarListaDispositivos();
    if (idSeccion === 'seccion-wifi')      cargarDispositivosGlobal().then(cargarRedActual);
    if (idSeccion === 'seccion-logica')    cargarSensoresEnLogica();
    if (idSeccion === 'seccion-analitica') cargarSensoresEnAnalitica();
    if (idSeccion === 'seccion-cultivos')  cargarSeccionCultivos();
    if (idSeccion === 'seccion-configuracion') { cargarSeccionMiembros(); cargarInfoUsuario(); }
}

async function cargarSeccionMiembros() {
    const sel = document.getElementById('select-miembros-dispositivo');
    if (!sel) return;
    try {
        const res  = await fetch('/api/dispositivos/lista');
        const devs = await res.json();
        sel.innerHTML = devs.length === 0
            ? '<option value="">Sin dispositivos</option>'
            : devs.map(d => `<option value="${d.idDispositivo}">${d.nombre}</option>`).join('');
        if (devs.length > 0) cargarMiembros();
    } catch(e) { console.error('Error cargando dispositivos miembros:', e); }
    cargarAccesosPropios();
}

async function cargarMiembros() {
    const sel = document.getElementById('select-miembros-dispositivo');
    if (!sel || !sel.value) return;
    const deviceId = parseInt(sel.value);
    const cont = document.getElementById('lista-miembros');
    cont.innerHTML = `<p style="color:#aaa; font-size:13px;">${t('cargando')}</p>`;
    try {
        const res  = await fetch('/api/miembros/lista?device_id=' + deviceId);
        const data = await res.json();
        if (data.error) { cont.innerHTML = `<p style="color:#e74c3c;">${data.error}</p>`; return; }
        if (data.length === 0) {
            cont.innerHTML = `<p style="color:#aaa; font-size:13px;">${t('miembro_sin_miembros')}</p>`;
            return;
        }
        cont.innerHTML = `
            <table style="width:100%; border-collapse:collapse; font-size:14px;">
                <thead>
                    <tr style="border-bottom:2px solid #eee; color:#888; text-align:left;">
                        <th style="padding:8px 6px;">Usuario</th>
                        <th style="padding:8px 6px;">Correo</th>
                        <th style="padding:8px 6px;">Permiso</th>
                        <th style="padding:8px 6px;"></th>
                    </tr>
                </thead>
                <tbody>
                    ${data.map(m => `
                    <tr style="border-bottom:1px solid #f0f0f0;">
                        <td style="padding:8px 6px; font-weight:500;">${m.nombre} ${m.apellido_paterno || ''}</td>
                        <td style="padding:8px 6px; color:#666;">${m.correo}</td>
                        <td style="padding:8px 6px;">
                            <span style="
                                background:${m.permiso === 'controlar' ? '#27ae6022' : '#2980b922'};
                                color:${m.permiso === 'controlar' ? '#27ae60' : '#2980b9'};
                                padding:3px 10px; border-radius:20px; font-size:12px; font-weight:600;">
                                ${m.permiso === 'controlar' ? '🎮 Controlar' : '👁 Solo ver'}
                            </span>
                        </td>
                        <td style="padding:8px 6px; text-align:right;">
                            <button onclick="eliminarMiembro(${m.idUsuario}, ${deviceId})"
                                style="background:none; border:none; color:#e74c3c;
                                       cursor:pointer; font-size:18px;" title="Eliminar miembro">🗑</button>
                        </td>
                    </tr>`).join('')}
                </tbody>
            </table>`;
    } catch(e) { cont.innerHTML = '<p style="color:#e74c3c;">Error cargando miembros.</p>'; }
}

async function invitarMiembro() {
    const sel     = document.getElementById('select-miembros-dispositivo');
    const correo  = document.getElementById('input-correo-invitado').value.trim();
    const permiso = document.getElementById('select-permiso-invitado').value;
    if (!sel || !sel.value) { toast('Selecciona un dispositivo', 'warning'); return; }
    if (!correo) { toast('Ingresa el correo del usuario', 'warning'); return; }
    try {
        const res  = await fetch('/api/miembros/invitar', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ correo, device_id: parseInt(sel.value), permiso })
        });
        const data = await res.json();
        if (data.error) { toast(data.error, 'error'); return; }
        toast(data.mensaje, 'success');
        document.getElementById('input-correo-invitado').value = '';
        cargarMiembros();
    } catch(e) { toast('Error al invitar', 'error'); }
}

async function eliminarMiembro(idUsuario, deviceId) {
    if (!confirm('¿Eliminar a este miembro del dispositivo?')) return;
    try {
        const res  = await fetch('/api/miembros/eliminar', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device_id: deviceId, idUsuario })
        });
        const data = await res.json();
        if (data.error) { toast(data.error, 'error'); return; }
        toast('Miembro eliminado', 'success');
        cargarMiembros();
    } catch(e) { toast('Error al eliminar', 'error'); }
}

async function cargarAccesosPropios() {
    const cont = document.getElementById('lista-accesos-propios');
    if (!cont) return;
    try {
        const res  = await fetch('/api/miembros/mis-accesos');
        const data = await res.json();
        if (!data.length) {
            cont.innerHTML = '<p style="color:#aaa; font-size:13px;">No tienes dispositivos compartidos contigo aún.</p>';
            return;
        }
        cont.innerHTML = data.map(d => `
            <div style="display:flex; align-items:center; justify-content:space-between;
                        padding:10px 0; border-bottom:1px solid #f0f0f0; flex-wrap:wrap; gap:8px;">
                <div>
                    <div style="font-weight:600;">${d.nombre}</div>
                    <div style="font-size:12px; color:#888;">Dueño: ${d.nombre_dueno} · ${d.correo_dueno}</div>
                </div>
                <span style="
                    background:${d.permiso === 'controlar' ? '#27ae6022' : '#2980b922'};
                    color:${d.permiso === 'controlar' ? '#27ae60' : '#2980b9'};
                    padding:3px 10px; border-radius:20px; font-size:12px; font-weight:600;">
                    ${d.permiso === 'controlar' ? '🎮 Controlar' : '👁 Solo ver'}
                </span>
            </div>`).join('');
    } catch(e) { cont.innerHTML = '<p style="color:#e74c3c;">Error cargando accesos.</p>'; }
}
async function abrirModal(idModal) {
    const modal = document.getElementById(idModal);

    if (idModal === 'modal-siembra')     await cargarTiposCultivo();
    if (idModal === 'modal-cosecha')     await cargarCultivosActivos();
    if (idModal === 'modal-cultivo')     await cargarCultivosEnModal();

    if (idModal === 'modal-sensor') {
        const response = await fetch('/api/dispositivos/lista');
        const dispositivos = await response.json();
        if (dispositivos.length === 0) { toast("No hay dispositivos registrados aún", "warning"); return; }
        cargarListaDispositivos();
    }

    if (idModal === 'modal-parametro') {
        await cargarSensoresEnSelectParametro();
    }

    if (modal) modal.classList.add('activo');
}

function cerrarModal(idModal) {
    const modal = document.getElementById(idModal);
    if (modal) modal.classList.remove('activo');
}

function mostrarModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.classList.add('activo');
}

document.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal-overlay')) e.target.classList.remove('activo');
});
async function guardarDispositivo() {
    const data = {
        nombre: document.getElementById('nombreDispositivo').value,
        tipo: `${document.getElementById('tipoControlador').value} (${document.getElementById('tipoConexion').value})`
    };
    if (!data.nombre) return toast("Por favor ingresa un nombre", "warning");

    const response = await fetch('/api/dispositivos/agregar', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    });

    const result = await response.json();
    if (response.ok && result.status === 'success') {
        cerrarModal('modal-dispositivo');
        document.getElementById('nombreDispositivo').value = '';
        await cargarDispositivosGlobal();
        actualizarListaDispositivos();
        mostrarModalPairing(result.pairing_code);
    }
}

function mostrarModalPairing(codigo) {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay activo';
    overlay.id = 'modal-pairing-temp';
    overlay.innerHTML = `
        <div class="modal" style="text-align:center;">
            <h3>✅ Dispositivo creado</h3>
            <p style="color:#666; margin:10px 0 20px;">
                Copia este código y pégalo en el sketch del Arduino:
            </p>
            <div style="background:#f4f4f4; border-radius:8px; padding:16px; margin-bottom:16px;">
                <code style="font-size:13px; color:#555; display:block; margin-bottom:6px;">
                </code>
                <div style="font-size:28px; font-weight:800; color:#2c3e50; letter-spacing:4px;">
                    ${codigo}
                </div>
            </div>
            <p style="font-size:12px; color:#e74c3c; margin-bottom:16px;">
                ⚠️ Este código solo funciona una vez. Úsalo antes de encender el Arduino.
            </p>
            <button onclick="
                navigator.clipboard.writeText('${codigo}');
                toast('Código copiado', 'success');
            " class="btn-verde" style="margin-bottom:8px; width:100%;">
                📋 Copiar código
            </button>
            <button onclick="document.getElementById('modal-pairing-temp').remove()"
                    style="width:100%; background:#95a5a6;">
                Cerrar
            </button>
        </div>
    `;
    document.body.appendChild(overlay);
}
async function actualizarListaDispositivos() {
    const contenedor = document.getElementById('lista-hardware');
    const btnSensor  = document.getElementById('btn-agregar-sensor');

    contenedor.innerHTML = '<p style="color:#888;">Cargando...</p>';

    try {
        const [resDis, resSen] = await Promise.all([
            fetch('/api/dispositivos/lista'),
            fetch('/api/sensores/lista')
        ]);
        const dispositivos = await resDis.json();
        const sensores     = await resSen.json();

        if (btnSensor) btnSensor.disabled = (dispositivos.length === 0);

        if (dispositivos.length === 0) {
            contenedor.innerHTML = `
                <div class="card" style="text-align:center; padding:30px; color:#888;">
                    No hay dispositivos registrados aún.<br>
                    <button style="margin-top:12px;" onclick="abrirModal('modal-dispositivo')">+ Agregar primer dispositivo</button>
                </div>`;
            return;
        }

        let html = '<h3 style="margin-bottom:12px;">Dispositivos y Sensores Conectados</h3>';

        dispositivos.forEach(d => {
            const sensoresDev = sensores.filter(s => String(s.idDispositivo) === String(d.idDispositivo));

            html += `
            <div class="card" style="margin-bottom:12px;">
                <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
                    <div>
                        <strong style="font-size:15px;">🔌 ${d.nombre}</strong>
                        <span class="badge badge-azul" style="margin-left:8px;">${d.tipo || 'Sin tipo'}</span>
                        <span class="badge badge-gris" style="margin-left:6px; font-family:monospace;"
                              title="Pon este número en el sketch: #define DEVICE_ID ${d.idDispositivo}">
                            ID: ${d.idDispositivo}
                        </span>
                    </div>
                    <div style="display:flex; align-items:center; gap:8px;">
                        <span class="badge ${sensoresDev.length > 0 ? 'badge-verde' : 'badge-gris'}">
                            ${sensoresDev.length} sensor${sensoresDev.length !== 1 ? 'es' : ''}
                        </span>
                        ${d.rol === 'dueno' ? `<button type="button" class="btn-verde btn-sm"
                            data-agua-dispositivo="${d.idDispositivo}" onclick="integrarSensoresAgua(${d.idDispositivo})">
                            Integrar sensores de agua</button>` : ''}
                        <button onclick="eliminarDispositivo(${d.idDispositivo}, '${d.nombre}')"
                            style="padding:4px 10px; border-radius:6px; border:1px solid
                                   color:#e74c3c; background:white; cursor:pointer; font-size:12px;">
                            🗑 Eliminar
                        </button>
                    </div>
                </div>`;

            if (sensoresDev.length > 0) {
                html += `<div style="margin-top:10px; display:flex; flex-wrap:wrap; gap:8px;">`;
                sensoresDev.forEach(s => {
                    html += `
                    <div style="background:#f8f9fa; border:1px solid #e0e0e0; border-radius:6px; padding:8px 12px; min-width:160px; display:flex; justify-content:space-between; align-items:center; gap:8px;">
                        <div>
                            <div style="font-weight:600; font-size:13px;">📡 ${s.tipo_sensor}</div>
                            <div style="font-size:12px; color:#666;">Unidad: ${s.unidad_medida || '—'}</div>
                            <div style="font-size:11px; color:#999;">ID: ${s.idSensore}</div>
                        </div>
                        <div style="display:flex; flex-direction:column; gap:4px;">
                            <button onclick="abrirEditarSensor(${s.idSensore}, '${s.tipo_sensor}', '${s.unidad_medida || ''}', ${s.idDispositivo})"
                                style="padding:3px 8px; border-radius:5px; border:1px solid
                                       color:#3498db; background:white; cursor:pointer; font-size:11px;">
                                ✏️
                            </button>
                            <button onclick="eliminarSensor(${s.idSensore}, '${s.tipo_sensor}')"
                                style="padding:3px 8px; border-radius:5px; border:1px solid
                                       color:#e74c3c; background:white; cursor:pointer; font-size:11px;">
                                🗑
                            </button>
                        </div>
                    </div>`;
                });
                html += `</div>`;
            } else {
                html += `<p style="color:#aaa; font-size:13px; margin-top:8px;">Sin sensores vinculados aún.</p>`;
            }

            html += `</div>`;
        });

        contenedor.innerHTML = html;
    } catch (e) {
        contenedor.innerHTML = '<p style="color:red;">Error al cargar dispositivos.</p>';
        console.error(e);
    }
}


async function eliminarDispositivo(id, nombre) {
    const ok = await confirmar(`¿Eliminar el dispositivo <strong>${nombre}</strong>?<br><span style="color:#e74c3c;font-size:13px;">⚠️ También se eliminarán todos sus sensores vinculados.</span>`);
    if (!ok) return;
    try {
        const r = await fetch(`/api/dispositivos/eliminar/${id}`, { method: 'DELETE' });
        const d = await r.json();
        if (r.ok && d.status === 'success') {
            toast(`Dispositivo "${nombre}" eliminado`, 'success');
            await cargarDispositivosGlobal();
            actualizarListaDispositivos();
        } else {
            toast('Error: ' + (d.mensaje || 'No se pudo eliminar'), 'error');
        }
    } catch(e) { toast('Error de conexión', 'error'); }
}

function abrirEditarSensor(id, tipo, unidad, idDispositivo) {
    montarOpcionesSensoresAgua();
    document.getElementById('editSensorId').value = id;
    document.getElementById('editSensorIdDispositivo').value = idDispositivo;
    const tipoSel = document.getElementById('editTipoSensor');
    const unidadSel = document.getElementById('editUnidadMedida');
    for (const [select, valor] of [[tipoSel, tipo], [unidadSel, unidad]]) {
        if (![...select.options].some(o => o.value === valor)) select.add(new Option(valor, valor));
        select.value = valor;
    }
    const esAgua = ['ec', 'temperatura_agua'].includes(String(tipo).trim().toLowerCase());
    tipoSel.disabled = unidadSel.disabled = esAgua;
    let nota = document.getElementById('nota-editar-sensor-agua');
    if (!nota) {
        nota = document.createElement('p'); nota.id = 'nota-editar-sensor-agua';
        nota.style.cssText = 'font-size:12px;color:#68756b;';
        unidadSel.insertAdjacentElement('afterend', nota);
    }
    nota.textContent = esAgua ? 'El tipo y la unidad se conservan para mantener comparable el historial.' : '';
    nota.hidden = !esAgua;
    document.getElementById('modal-editar-sensor').style.display = 'flex';
}

async function guardarEditarSensor() {
    const id    = document.getElementById('editSensorId').value;
    const tipo  = document.getElementById('editTipoSensor').value;
    const unidad = document.getElementById('editUnidadMedida').value;
    try {
        const r = await fetch(`/api/sensores/editar/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tipo, unidad })
        });
        const d = await r.json();
        if (r.ok && d.status === 'success') {
            toast('✅ Sensor actualizado correctamente', 'success');
            document.getElementById('modal-editar-sensor').style.display = 'none';
            actualizarListaDispositivos();
            cargarSensoresEnLogica();
            cargarSensoresEnAnalitica();
        } else {
            toast('Error: ' + (d.mensaje || 'No se pudo actualizar'), 'error');
        }
    } catch(e) { toast('Error de conexión', 'error'); }
}

async function eliminarSensor(id, tipo) {
    const ok = await confirmar(`¿Eliminar el sensor <strong>${tipo}</strong> (ID: ${id})?`);
    if (!ok) return;
    try {
        const r = await fetch(`/api/sensores/eliminar/${id}`, { method: 'DELETE' });
        const d = await r.json();
        if (r.ok && d.status === 'success') {
            toast(`Sensor "${tipo}" eliminado`, 'success');
            actualizarListaDispositivos();
        } else {
            toast('Error: ' + (d.mensaje || 'No se pudo eliminar'), 'error');
        }
    } catch(e) { toast('Error de conexión', 'error'); }
}
async function cargarListaDispositivos() {
    const select = document.getElementById('selectDispositivoVinculado');
    const response = await fetch('/api/dispositivos/lista');
    const dispositivos = await response.json();
    select.innerHTML = '';
    dispositivos.forEach(d => {
        let o = document.createElement('option');
        o.value = d.idDispositivo; o.text = d.nombre; select.add(o);
    });
}

async function guardarSensor() {
    const data = {
        idDispositivo: document.getElementById('selectDispositivoVinculado').value,
        tipo:    document.getElementById('tipoSensor').value,

        unidad:  document.getElementById('unidadMedida').value
    };
    const response = await fetch('/api/sensores/agregar', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    });
    const result = await response.json();
    if (response.ok && result.status === 'success') {
        toast('✅ Sensor guardado correctamente', "success");
        cerrarModal('modal-sensor');
        actualizarListaDispositivos();
        cargarSensoresEnLogica();
        cargarSensoresEnAnalitica();
    } else {
        toast(escaparTextoSensor(result.mensaje || result.error || 'No se pudo guardar el sensor'), 'error');
    }
}

async function cargarSensoresEnAnalitica() {
    const select = document.getElementById('selectSensorAnalitica');
    if (!select) return;
    const seleccionado = select.value;
    try {
        const response = await fetch('/api/sensores/lista');
        const sensores = await response.json();
        select.innerHTML = '<option value="">-- Seleccione un sensor --</option>';
        sensores.forEach(s => {
            let o = document.createElement('option');
            o.value = s.idSensore;
            o.text = `${nombreSensorAgua(s.tipo_sensor)} – ${s.unidad_medida || ''} (ID: ${s.idSensore})`;
            select.add(o);
        });
        if ([...select.options].some(o => o.value === seleccionado)) select.value = seleccionado;
    } catch (e) { console.error("Error cargando sensores analítica:", e); }
}
async function cargarSensoresEnLogica() {
    const select = document.getElementById('selectSensorLogica');
    if (!select) return;
    const seleccionado = select.value;
    try {
        const response = await fetch('/api/sensores/lista');
        const sensores = await response.json();
        select.innerHTML = '<option value="">-- Seleccione un sensor --</option>';
        sensores.forEach(s => {
            let o = document.createElement('option');
            o.value = s.idSensore;
            o.text = `${nombreSensorAgua(s.tipo_sensor)} – ${s.unidad_medida || ''} (ID: ${s.idSensore})`;
            select.add(o);
        });
        if ([...select.options].some(o => o.value === seleccionado)) select.value = seleccionado;
    } catch (e) {
        console.error("Error cargando sensores en lógica:", e);
    }
}

async function verificarEstadoSensor() {
    const idSensor = document.getElementById('selectSensorLogica').value;
    if (!idSensor) return toast('Selecciona un sensor primero', 'warning');
    limpiarVisualizacion();
    const turno = revisionVisualizacionSensores;
    const contenedor = document.getElementById('resultado-logica');
    contenedor.textContent = 'Consultando lectura…';
    try {
        const data = await pedirDatosSensor(`/api/sensores/estado/${idSensor}`);
        if (turno !== revisionVisualizacionSensores || document.getElementById('selectSensorLogica').value !== idSensor) return;
        const dato = data.ultima_lectura;
        const valor = valorGraficaSensor(dato);
        const antiguo = lecturaSensorAntigua(dato);
        const estado = !dato ? 'Sin datos disponibles' : antiguo ? 'Sin datos recientes' :
            valor === null ? 'Sin lectura válida' : 'Lectura recibida';
        const calidad = textoCalidadAgua(dato);
        const tds = detalleTDSSensor(dato);
        contenedor.innerHTML = `<div style="line-height:1.7;">
            <strong>${estado}</strong><div>Sensor ID: ${escaparTextoSensor(idSensor)}</div>
            ${calidad ? `<div style="color:${colorCalidadSensor(dato)};">${escaparTextoSensor(calidad)}</div>` : ''}
            <div>${antiguo ? 'Último dato' : 'Lectura'}: <strong>${valor === null ? '—' : escaparTextoSensor(valor)} ${valor === null ? '' : escaparTextoSensor(dato?.unidad)}</strong></div>
            ${tds ? `<div>${escaparTextoSensor(tds)}</div>` : ''}
            ${dato?.fecha_hora ? `<div>${escaparTextoSensor(dato.fecha_hora)}</div>` : ''}
        </div>`;
    } catch (error) {
        if (turno === revisionVisualizacionSensores) contenedor.textContent = error.message;
    }
}

let intervaloVisualizacion = null;
let graficaTiempoReal      = null;
const MAX_PUNTOS_GRAFICA   = 30;

function visualizarInformacion() {
    const select = document.getElementById('selectSensorLogica');
    const idSensor = select.value;
    if (!idSensor) return toast('Selecciona un sensor primero', 'warning');
    limpiarVisualizacion();
    const turno = revisionVisualizacionSensores;
    const contenedor = document.getElementById('resultado-logica');
    const etiqueta = select.options[select.selectedIndex]?.text || `Sensor ${idSensor}`;
    contenedor.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px;">
        <div><strong>${escaparTextoSensor(etiqueta)}</strong>
            <span id="valor-actual-badge" style="margin-left:10px;font-size:18px;font-weight:700;">—</span>
            <div id="tds-actual-detalle" style="font-size:12px;color:#68756b;"></div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;">
            <span id="estado-live" role="status" style="font-size:12px;">Consultando…</span>
            <button class="btn-rojo btn-sm" onclick="limpiarVisualizacion()">⏹ Detener</button>
        </div></div><canvas id="graficaTiempoReal" height="120"></canvas>`;
    const estados = [];
    const grafica = new Chart(document.getElementById('graficaTiempoReal').getContext('2d'), {
        type: 'line',
        data: {labels: [], datasets: [{label: etiqueta, data: [], borderColor: '#2c3e50',
            backgroundColor: 'rgba(44,62,80,0.08)', pointBackgroundColor: [], borderWidth: 2,
            tension: 0.25, pointRadius: 3, fill: true, spanGaps: false}]},
        options: {responsive: true, animation: {duration: 300},
            scales: {x: {ticks: {maxTicksLimit: 8}}, y: {beginAtZero: false}},
            plugins: {legend: {display: false}, tooltip: {callbacks: {
                afterLabel: ctx => textoCalidadAgua(estados[ctx.dataIndex])
            }}}}
    });
    graficaTiempoReal = grafica;
    let ultimaClave = null, consultando = false;
    const sigueActiva = () => turno === revisionVisualizacionSensores && graficaTiempoReal === grafica;
    async function obtenerYGraficar() {
        if (document.getElementById('selectSensorLogica').value !== idSensor) {
            if (sigueActiva()) limpiarVisualizacion();
            return;
        }
        if (!sigueActiva() || consultando) return;
        consultando = true;
        try {
            const dato = await pedirDatosSensor(`/api/sensores/datos-actuales/${idSensor}`);
            if (!sigueActiva() || select.value !== idSensor) return;
            const valor = valorGraficaSensor(dato);
            const antiguo = lecturaSensorAntigua(dato);
            document.getElementById('valor-actual-badge').textContent = valor === null ? '—' : `${valor} ${dato.unidad || ''}`;
            document.getElementById('tds-actual-detalle').textContent = detalleTDSSensor(dato);
            const estado = document.getElementById('estado-live');
            estado.textContent = (antiguo ? 'Sin datos recientes · ' : '') +
                (textoCalidadAgua(dato) || (valor === null ? 'Sin lectura válida' : 'Lectura recibida')) +
                (dato.fecha_hora ? ' · ' + dato.fecha_hora : '');
            estado.style.color = antiguo ? '#777' : colorCalidadSensor(dato);
            const clave = `${dato.fecha_hora}|${dato.estado_medicion || ''}`;
            if (!dato.fecha_hora || clave === ultimaClave || antiguo) return;
            ultimaClave = clave;
            grafica.data.labels.push(dato.fecha_hora);
            grafica.data.datasets[0].data.push(valor);
            grafica.data.datasets[0].pointBackgroundColor.push(colorCalidadSensor(dato));
            estados.push(dato);
            if (grafica.data.labels.length > MAX_PUNTOS_GRAFICA) {
                grafica.data.labels.shift(); grafica.data.datasets[0].data.shift();
                grafica.data.datasets[0].pointBackgroundColor.shift(); estados.shift();
            }
            grafica.update();
        } catch (_) {
            if (!sigueActiva()) return;
            document.getElementById('valor-actual-badge').textContent = '—';
            document.getElementById('tds-actual-detalle').textContent = '';
            document.getElementById('estado-live').textContent = 'No se pudo consultar la lectura';
            document.getElementById('estado-live').style.color = '#9b6500';
        } finally { consultando = false; }
    }
    obtenerYGraficar();
    intervaloVisualizacion = setInterval(obtenerYGraficar, 2000);
}

function limpiarVisualizacion() {
    ++revisionVisualizacionSensores;
    if (intervaloVisualizacion !== null) clearInterval(intervaloVisualizacion);
    intervaloVisualizacion = null;
    if (graficaTiempoReal) graficaTiempoReal.destroy();
    graficaTiempoReal = null;
    const contenedor = document.getElementById('resultado-logica');
    if (contenedor) contenedor.replaceChildren();
}

let miGrafica = null;

async function cargarGrafica(rango) {
    const select = document.getElementById('selectSensorAnalitica');
    const idSensor = select.value;
    if (!idSensor) return toast('Selecciona un sensor primero', 'warning');
    const turno = ++revisionHistorialSensores;
    try {
        const datos = await pedirDatosSensor(`/api/sensores/analitica/${idSensor}/${rango}`);
        if (turno !== revisionHistorialSensores || select.value !== idSensor) return;
        if (!Array.isArray(datos)) throw new Error('Historial no válido.');
        if (miGrafica) miGrafica.destroy();
        const etiqueta = select.options[select.selectedIndex]?.text || `Sensor ${idSensor}`;
        const periodo = {ahora: 'Última hora', '24h': 'Últimas 24 horas', semana: 'Última semana'}[rango] || rango;
        miGrafica = new Chart(document.getElementById('graficaSensor').getContext('2d'), {
            type: 'line',
            data: {labels: datos.map(d => d.fecha_hora), datasets: [{label: `${etiqueta} · ${periodo}`,
                data: datos.map(valorGraficaSensor), spanGaps: false,
                pointBackgroundColor: datos.map(colorCalidadSensor), borderColor: '#2c3e50',
                backgroundColor: 'rgba(44,62,80,0.08)', borderWidth: 2, tension: 0.25,
                fill: true, pointRadius: datos.length > 50 ? 0 : 3}]},
            options: {responsive: true, plugins: {legend: {display: true},
                subtitle: {display: datos.some(d => d.estado_medicion),
                    text: 'Puntos ámbar: lecturas provisionales. Los huecos indican lecturas no válidas.'},
                tooltip: {mode: 'index', intersect: false, callbacks: {afterLabel: ctx =>
                    [textoCalidadAgua(datos[ctx.dataIndex]), detalleTDSSensor(datos[ctx.dataIndex])].filter(Boolean)}}},
                scales: {x: {ticks: {maxTicksLimit: 10}}}}
        });
    } catch (error) {
        if (turno === revisionHistorialSensores) toast(escaparTextoSensor(error.message), 'error');
    }
}

async function cargarTiposCultivo() {
    const select = document.getElementById('selectTipoCultivo');
    if (!select) return;
    try {
        const response = await fetch('/api/tipo_cultivo/lista');
        const tipos    = await response.json();
        select.innerHTML = '<option value="">-- Selecciona el tipo --</option>';
        tipos.forEach(t => {
            let o = document.createElement('option');
            o.value = t.idTipo_Cultivo; o.textContent = t.nombre_planta; select.appendChild(o);
        });
    } catch (error) { console.error("Error tipos cultivo:", error); }
}

async function guardarSiembra() {
    const nombre   = document.getElementById('nombreCultivo').value;
    const fecha    = document.getElementById('fechaSiembra').value;
    const cantidad = document.getElementById('cantidadPlantas').value;
    const idTipo   = document.getElementById('selectTipoCultivo').value;

    if (!nombre || !fecha || !cantidad || !idTipo) {
        return toast("Completa todos los campos", "warning");
    }
    try {
        const response = await fetch('/api/cultivos/sembrar', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ nombre, fecha, cantidad, tamano: 0, idTipo, idSistema: 1 })
        });
        const result = await response.json();
        if (response.ok && result.status === 'success') {
            toast("¡Siembra guardada correctamente!", "success");
            cerrarModal('modal-siembra');
            await cargarTablaCultivosSeccion();
            await cargarTablaCultivos();
        }
        else toast("Error al guardar siembra: " + (result.error || "Revisa los datos"), "error");
    } catch (error) { toast("Error de conexión: " + error.message, "error"); }
}

async function guardarTipoCultivo() {
    const data = {
        nombre:      document.getElementById('nombrePlanta').value,
        descripcion: document.getElementById('descPlanta').value
    };
    if (!data.nombre) return toast("El nombre es obligatorio", "warning");
    const response = await fetch('/api/tipo_cultivo/agregar', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    });
    const result = await response.json();
    if (response.ok && result.status === 'success') {
        toast("¡Tipo de cultivo registrado!", "success");
        cerrarModal('modal-tipo-cultivo');
        document.getElementById('nombrePlanta').value = '';
        document.getElementById('descPlanta').value   = '';
        await cargarTiposCultivo();
        _tiposCultivoCache = [];
    } else toast("Error al guardar tipo de cultivo", "error");
}
async function cargarCultivosActivos() {
    const select = document.getElementById('selectCultivoCosecha');
    if (!select) return;
    try {
        const response = await fetch('/api/cultivos/lista');
        const lista    = await response.json();
        select.innerHTML = '<option value="">-- Selecciona el cultivo --</option>';
        lista.forEach(c => {
            let o = document.createElement('option');
            o.value = c.idCultivo; o.textContent = c.nombreCultivo; select.appendChild(o);
        });
    } catch (e) { console.error("Error cultivos:", e); }
}

async function guardarCosecha() {
    const elFecha         = document.getElementById('fechaCosecha');
    const elCantidad      = document.getElementById('cantidadCosechada');
    const elCalidad       = document.getElementById('calidadCosecha');
    const elObservaciones = document.getElementById('observacionesCosecha');
    const elIdCultivo     = document.getElementById('selectCultivoCosecha');

    if (!elIdCultivo.value) return toast("Selecciona un cultivo primero", "warning");

    const data = {
        fecha:         elFecha.value,
        cantidad:      elCantidad.value,
        calidad:       elCalidad.value || 'N/A',
        observaciones: elObservaciones ? elObservaciones.value : '',
        idCultivo:     elIdCultivo.value
    };
    try {
        const response = await fetch('/api/cosechas/registrar', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        const result = await response.json();
        if (response.ok && result.status === 'success') {
            toast("¡Cosecha guardada correctamente!", "success");
            cerrarModal('modal-cosecha');
            await cargarTablaCosechasSeccion();
            await cargarTablaCosechas();
            await cargarCultivosActivos();
        }
        else toast("Error al guardar cosecha: " + (result.error || "Revisa los datos"), "error");
    } catch (e) { toast("Error de conexión: " + e.message, "error"); }
}
function validarTextoSoloLetras(input) {
    input.value = input.value.replace(/[^a-zA-ZáéíóúÁÉÍÓÚñÑ\s]/g, '');
    input.setCustomValidity(input.value.length > 0 && input.value.length < 3
        ? "La calidad debe tener al menos 3 letras." : "");
}
function cambiarTab(idTab, btn) {
    document.querySelectorAll('.tab-contenido').forEach(t => t.classList.remove('activo'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('activo'));
    document.getElementById(idTab).classList.add('activo');
    btn.classList.add('activo');
}

function cambiarTabAlerta(idTab, btn) {
    const seccion = document.getElementById('seccion-alertas');
    seccion.querySelectorAll('.tab-contenido').forEach(t => t.classList.remove('activo'));
    seccion.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('activo'));
    document.getElementById(idTab).classList.add('activo');
    btn.classList.add('activo');

    if (idTab === 'tab-historial')  cargarHistorialAlertas();
    if (idTab === 'tab-parametros') cargarParametrosAlerta();
}
async function cargarCultivosEnModal() {
    await Promise.all([cargarTablaCultivos(), cargarTablaCosechas()]);
}

async function cargarTablaCultivos() {
    const contenedor = document.getElementById('tabla-cultivos-modal');
    contenedor.innerHTML = '<p style="color:#888;">Cargando...</p>';
    try {
        const cultivos = await (await fetch('/api/cultivos/lista')).json();
        if (cultivos.length === 0) {
            contenedor.innerHTML = '<p style="color:#888;">No hay cultivos registrados.</p>'; return;
        }
        let html = `<table><thead><tr>
            <th>#</th><th>Nombre</th><th>Tipo</th>
            <th>Fecha Siembra</th><th>Cantidad</th>
            <th>Acciones</th>
        </tr></thead><tbody>`;
        cultivos.forEach(c => {
            html += `<tr id="fila-cultivo-${c.idCultivo}">
                <td>${c.idCultivo}</td>
                <td><strong>${c.nombreCultivo}</strong></td>
                <td>${c.tipo_cultivo || '—'}</td>
                <td>${c.fecha_siembra || '—'}</td>
                <td>${c.cantidad} plantas</td>
                <td style="white-space:nowrap;">
                    <button class="btn-naranja btn-sm" onclick='abrirEditarCultivo(${JSON.stringify(c)})'>✏️ Editar</button>
                    <button class="btn-rojo btn-sm"    onclick="eliminarCultivo(${c.idCultivo})">🗑 Eliminar</button>
                </td>
            </tr>`;
        });
        html += '</tbody></table>';
        contenedor.innerHTML = html;
    } catch (e) { contenedor.innerHTML = '<p style="color:red;">Error al cargar cultivos.</p>'; }
}
let _tiposCultivoCache = [];

async function abrirEditarCultivo(c) {
    if (_tiposCultivoCache.length === 0) {
        const res = await fetch('/api/tipo_cultivo/lista');
        _tiposCultivoCache = await res.json();
    }
    document.getElementById('editCultivoId').value       = c.idCultivo;
    document.getElementById('editNombreCultivo').value   = c.nombreCultivo;
    document.getElementById('editFechaSiembra').value    = c.fecha_siembra || '';
    document.getElementById('editCantidadPlantas').value = c.cantidad;

    const sel = document.getElementById('editTipoCultivo');
    sel.innerHTML = _tiposCultivoCache.map(t =>
        `<option value="${t.idTipo_Cultivo}" ${t.idTipo_Cultivo == c.idTipo_Cultivo ? 'selected' : ''}>${t.nombre_planta}</option>`
    ).join('');

    abrirModal('modal-editar-cultivo');
}

async function eliminarCultivo(id) {
    if (!await confirmar('¿Eliminar este cultivo? También se eliminarán sus cosechas asociadas.')) return;
    try {
        const res  = await fetch(`/api/cultivos/eliminar/${id}`, { method: 'DELETE' });
        const data = await res.json();
        if (data.status === 'success') {
            toast('Cultivo eliminado', 'success');
            await cargarTablaCultivos();
            await cargarTablaCultivosSeccion();
            await cargarTablaCosechasSeccion();
            await cargarCultivosActivos();
        } else { toast('Error: ' + (data.error || 'desconocido'), 'error'); }
    } catch(e) { toast('Error de red: ' + e, 'error'); }
}
async function cargarTablaCosechas() {
    const contenedor = document.getElementById('tabla-cosechas-modal');
    contenedor.innerHTML = '<p style="color:#888;">Cargando...</p>';
    try {
        const cosechas = await (await fetch('/api/cosechas/lista')).json();
        if (cosechas.length === 0) {
            contenedor.innerHTML = '<p style="color:#888;">No hay cosechas registradas.</p>'; return;
        }
        let html = `<table><thead><tr>
            <th>#</th><th>Cultivo</th><th>Fecha</th>
            <th>Cantidad</th><th>Calidad</th><th>Observaciones</th><th>Acciones</th>
        </tr></thead><tbody>`;
        cosechas.forEach(cs => {
            html += `<tr id="fila-cosecha-${cs.idCosecha}">
                <td>${cs.idCosecha}</td>
                <td><strong>${cs.nombreCultivo || '—'}</strong></td>
                <td>${cs.fecha || '—'}</td>
                <td>${cs.cantidad}</td>
                <td><span class="badge badge-verde">${cs.calidad || '—'}</span></td>
                <td style="max-width:180px;white-space:pre-wrap;">${cs.observaciones || '—'}</td>
                <td style="white-space:nowrap;">
                    <button class="btn-naranja btn-sm" onclick='abrirEditarCosecha(${JSON.stringify(cs)})'>✏️ Editar</button>
                    <button class="btn-rojo btn-sm"    onclick="eliminarCosecha(${cs.idCosecha})">🗑 Eliminar</button>
                </td>
            </tr>`;
        });
        html += '</tbody></table>';
        contenedor.innerHTML = html;
    } catch (e) { contenedor.innerHTML = '<p style="color:red;">Error al cargar cosechas.</p>'; }
}
function abrirEditarCosecha(cs) {
    document.getElementById('editCosechaId').value           = cs.idCosecha;
    document.getElementById('editFechaCosecha').value        = cs.fecha || '';
    document.getElementById('editCantidadCosecha').value     = cs.cantidad;
    document.getElementById('editCalidadCosecha').value      = cs.calidad || '';
    document.getElementById('editObservacionesCosecha').value= cs.observaciones || '';
    abrirModal('modal-editar-cosecha');
}

async function eliminarCosecha(id) {
    if (!await confirmar('¿Seguro que quieres eliminar esta cosecha?')) return;
    try {
        const res  = await fetch(`/api/cosechas/eliminar/${id}`, { method: 'DELETE' });
        const data = await res.json();
        if (data.status === 'success') {
            toast('Cosecha eliminada', 'success');
            await cargarTablaCosechas();
            await cargarTablaCosechasSeccion();
        } else { toast('Error: ' + (data.error || 'desconocido'), 'error'); }
    } catch(e) { toast('Error de red: ' + e, 'error'); }
}
async function cargarInfoUsuario() {
    try {
        const res  = await fetch('/api/usuario/info');
        const user = await res.json();
        const nombreCompleto = [user.nombre, user.apellido_paterno, user.apellido_materno]
            .filter(Boolean).join(' ');
        document.getElementById('userNombre').textContent = nombreCompleto || '—';
        document.getElementById('userCorreo').textContent = user.correo || '—';
        const eN = document.getElementById('editNombre');
        const eP = document.getElementById('editApellidoPaterno');
        const eM = document.getElementById('editApellidoMaterno');
        if (eN) eN.value = user.nombre || '';
        if (eP) eP.value = user.apellido_paterno || '';
        if (eM) eM.value = user.apellido_materno || '';
        const fotoEl = document.getElementById('fotoPerfil');
        const initEl = document.getElementById('fotoInicial');
        if (user.foto_perfil && fotoEl) {
            fotoEl.src = user.foto_perfil;
            fotoEl.style.display = 'block';
            if (initEl) initEl.style.display = 'none';
        } else if (fotoEl) {
            fotoEl.style.display = 'none';
            if (initEl) {
                initEl.style.display = 'flex';
                initEl.textContent = (user.nombre || 'U')[0].toUpperCase();
            }
        }
    } catch (e) { console.error("Error usuario:", e); }
}

async function actualizarNombrePerfil() {
    const nombre           = document.getElementById('editNombre')?.value.trim();
    const apellido_paterno = document.getElementById('editApellidoPaterno')?.value.trim();
    const apellido_materno = document.getElementById('editApellidoMaterno')?.value.trim();
    const msgEl            = document.getElementById('msgNombre');
    const btnEl            = document.getElementById('btnActualizarNombre');

    if (!nombre || !apellido_paterno || !apellido_materno) {
        if (msgEl) { msgEl.textContent = '⚠️ Completa todos los campos.'; msgEl.style.color = '#c0392b'; msgEl.style.display = 'inline'; }
        return;
    }

    if (btnEl) { btnEl.disabled = true; btnEl.textContent = 'Guardando...'; }

    try {
        const res  = await fetch('/api/usuario/actualizar_nombre', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ nombre, apellido_paterno, apellido_materno })
        });
        const data = await res.json();

        if (res.ok && data.status === 'success') {
            if (msgEl) { msgEl.textContent = '✅ Nombre actualizado.'; msgEl.style.color = '#1e7e4a'; msgEl.style.display = 'inline'; }
            const nombreCompleto = [nombre, apellido_paterno, apellido_materno].join(' ');
            const uN = document.getElementById('userNombre');
            if (uN) uN.textContent = nombreCompleto;
            const iN = document.getElementById('inicio-nombre');
            if (iN) iN.textContent = nombre;
            setTimeout(() => { if (msgEl) msgEl.style.display = 'none'; }, 3000);
        } else {
            if (msgEl) { msgEl.textContent = '⚠️ ' + (data.mensaje || 'Error al guardar.'); msgEl.style.color = '#c0392b'; msgEl.style.display = 'inline'; }
        }
    } catch {
        if (msgEl) { msgEl.textContent = '⚠️ Error de conexión.'; msgEl.style.color = '#c0392b'; msgEl.style.display = 'inline'; }
    } finally {
        if (btnEl) { btnEl.disabled = false; btnEl.textContent = 'Guardar cambios'; }
    }
}

async function cambiarPassword() {
    const passEl = document.getElementById('nuevaPass');
    const pass   = passEl ? passEl.value.trim() : '';

    if (!pass) return toast("Escribe una nueva contraseña", "warning");
    if (pass.length < 6) return toast("La contraseña debe tener al menos 6 caracteres", "warning");

    const btnEl = document.getElementById('btnCambiarPass');
    if (btnEl) { btnEl.disabled = true; btnEl.textContent = 'Guardando...'; }

    try {
        const res  = await fetch('/api/usuario/password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: pass })
        });
        const data = await res.json();
        if (res.ok && data.status === 'success') {
            toast("✅ Contraseña actualizada correctamente", "success");
            if (passEl) passEl.value = '';
        } else {
            toast("Error: " + (data.error || "No se pudo actualizar"), "error");
        }
    } catch (e) {
        toast("Error de conexión: " + e.message, "error");
    } finally {
        if (btnEl) { btnEl.disabled = false; btnEl.textContent = 'Actualizar Contraseña'; }
    }
}

async function subirFotoPerfil(input) {
    const file = input.files[0];
    if (!file) return;

    if (file.size > 2 * 1024 * 1024) {
        toast("La imagen debe pesar menos de 2 MB", "warning");
        return;
    }

    const reader = new FileReader();
    reader.onload = async (e) => {
        const base64 = e.target.result;
        const fotoEl = document.getElementById('fotoPerfil');
        const initEl = document.getElementById('fotoInicial');
        if (fotoEl) { fotoEl.src = base64; fotoEl.style.display = 'block'; }
        if (initEl) initEl.style.display = 'none';

        try {
            const res = await fetch('/api/usuario/foto', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ foto: base64 })
            });
            const data = await res.json();
            if (!res.ok) toast("Error al guardar foto: " + (data.error || "Error desconocido"), "error");
        } catch (err) {
            toast("Error de red al subir foto", "error");
        }
    };
    reader.readAsDataURL(file);
}

function badgePrioridad(p) {
    const mapa  = { critica: 'badge-rojo', alta: 'badge-naranja', media: 'badge-amarillo', baja: 'badge-verde' };
    const emoji = { critica: '🔴', alta: '🟠', media: '🟡', baja: '🟢' };
    return `<span class="badge ${mapa[p] || 'badge-gris'}">${emoji[p] || ''} ${p}</span>`;
}

function badgeEstado(e) {
    const mapa = { nueva: 'badge-rojo', vista: 'badge-amarillo', resuelta: 'badge-verde' };
    return `<span class="badge ${mapa[e] || 'badge-gris'}">${e}</span>`;
}

function labelCondicion(c) {
    return { mayor_que: '>', menor_que: '<', igual_a: '=' }[c] || c;
}

async function actualizarBadgeNav() {
    try {
        const res   = await fetch('/api/alertas/conteo');
        const data  = await res.json();
        const badge = document.getElementById('badge-alertas-nav');
        if (badge) {
            badge.textContent   = data.nuevas;
            badge.style.display = data.nuevas > 0 ? 'flex' : 'none';
        }
    } catch (e) { /* silencioso */ }
}

async function cargarAlertasSeccion() {
    try {
        await Promise.all([
            cargarHistorialAlertas(),
            cargarParametrosAlerta(),
            cargarSensoresEnFiltro()
        ]);
    } catch(e) { console.error('Error cargando sección alertas:', e); }
}

async function cargarSensoresEnFiltro() {
    return historialHydrosense.cargarSensores();
}

async function cargarSensoresEnSelectParametro() {
    const select = document.getElementById('selectSensorParametro');
    if (!select) return;
    try {
        const sensores = await (await fetch('/api/sensores/lista')).json();
        select.innerHTML = '<option value="">-- Selecciona sensor --</option>';
        sensores.forEach(s => {
            let o = document.createElement('option');
            o.value = s.idSensore;
            o.textContent = `${s.tipo_sensor} – ${s.unidad_medida || ''} (ID:${s.idSensore})`;
            select.appendChild(o);
        });
    } catch (e) { console.error("Error cargando sensores parámetro:", e); }
}

function aplicarFiltros() {
    return historialHydrosense.filtrar();
}

async function cargarHistorialAlertas() {
    return historialHydrosense.cargar();
}

async function verDetalleAlerta(idHistorial) {
    return historialHydrosense.detalle(idHistorial);
}

async function cambiarEstadoAlerta(idHistorial, nuevoEstado, desdeModal = false) {
    return historialHydrosense.cambiarEstado(idHistorial, nuevoEstado, desdeModal);
}

async function cargarParametrosAlerta() {
    rangosHydrosense.abrir();
    const contenedor = document.getElementById('tabla-parametros-alerta');
    contenedor.innerHTML = '<p style="color:#888;">Cargando...</p>';
    try {
        const params = await (await fetch('/api/alertas/parametros/lista')).json();

        if (params.length === 0) {
            contenedor.innerHTML = `<div class="card" style="text-align:center; padding:30px; color:#888;">
                Todavía no hay reglas guardadas. Puedes generarlas con los rangos de arriba o crear un parámetro manual.<br>
                <button class="btn-verde" style="margin-top:12px;" onclick="abrirModal('modal-parametro')">+ Crear el primero</button>
            </div>`;
            return;
        }

        let html = `<table>
            <thead><tr>
                <th>ID</th><th>Sensor</th><th>Nombre</th><th>Condición</th>
                <th>Umbral</th><th>Prioridad</th><th>Estado</th><th>Acciones</th>
            </tr></thead><tbody>`;

        params.forEach(p => {
            html += `<tr>
                <td>${p.idParametro}</td>
                <td>${p.tipo_sensor} <small style="color:#888;">${p.unidad_medida || ''}</small></td>
                <td>${p.nombre}</td>
                <td>${labelCondicion(p.condicion)}</td>
                <td><strong>${p.valor_umbral}</strong></td>
                <td>${badgePrioridad(p.prioridad)}</td>
                <td>
                    <span class="badge ${p.activo ? 'badge-verde' : 'badge-gris'}">
                        ${p.activo ? '● Activo' : '○ Inactivo'}
                    </span>
                </td>
                <td style="white-space:nowrap;">
                    <button class="btn-sm" onclick="editarParametro(${p.idParametro})">✏️ Editar</button>
                    <button class="btn-rojo btn-sm" onclick="eliminarParametro(${p.idParametro})">🗑 Eliminar</button>
                </td>
            </tr>`;
        });

        html += '</tbody></table>';
        contenedor.innerHTML = html;
    } catch (e) {
        contenedor.innerHTML = '<p style="color:red;">Error al cargar parámetros.</p>';
        console.error(e);
    }
}

async function guardarParametro() {
    const idParam   = document.getElementById('editParamId').value;
    const idSensor  = document.getElementById('selectSensorParametro').value;
    const nombre    = document.getElementById('paramNombre').value.trim();
    const condicion = document.getElementById('paramCondicion').value;
    const umbral    = document.getElementById('paramUmbral').value;
    const prioridad = document.getElementById('paramPrioridad').value;
    const activo    = document.getElementById('paramActivo').value;

    if (!idSensor || !nombre || umbral === '') {
        return toast("Completa todos los campos obligatorios.", "info");
    }

    const data = { idSensor: parseInt(idSensor), nombre, condicion, valor_umbral: parseFloat(umbral), prioridad, activo: parseInt(activo) };

    try {
        let res;
        if (idParam) {
            res = await fetch(`/api/alertas/parametros/editar/${idParam}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
        } else {
            res = await fetch('/api/alertas/parametros/agregar', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
        }

        if (res.ok) {
            toast(idParam ? "✅ Parámetro actualizado." : "✅ Parámetro creado.", "success");
            cerrarModal('modal-parametro');
            limpiarFormParametro();
            cargarParametrosAlerta();
            rangosHydrosense.actualizar();
        } else {
            const err = await res.json();
            toast("Error: " + (err.error || "Intenta de nuevo"), "error");
        }
    } catch (e) { toast("Error de red: " + e.message, "error"); }
}

async function editarParametro(idParam) {
    await abrirModal('modal-parametro');
    try {
        const params = await (await fetch('/api/alertas/parametros/lista')).json();
        const p = params.find(x => x.idParametro === idParam);
        if (!p) return;

        document.getElementById('titulo-modal-parametro').textContent = 'Editar Parámetro';
        document.getElementById('editParamId').value      = p.idParametro;
        document.getElementById('paramNombre').value      = p.nombre;
        document.getElementById('paramCondicion').value   = p.condicion;
        document.getElementById('paramUmbral').value      = p.valor_umbral;
        document.getElementById('paramPrioridad').value   = p.prioridad;
        document.getElementById('paramActivo').value      = p.activo ? '1' : '0';

        const sel = document.getElementById('selectSensorParametro');
        sel.value = p.idSensor;
    } catch (e) { console.error("Error al cargar parámetro:", e); }
}

async function eliminarParametro(idParam) {
    if (!await confirmar("¿Eliminar este parámetro? Se borrará también su historial de alertas.")) return;
    try {
        const res = await fetch(`/api/alertas/parametros/eliminar/${idParam}`, { method: 'DELETE' });
        if (res.ok) { toast("Parámetro eliminado", "success"); cargarParametrosAlerta(); rangosHydrosense.actualizar(); }
        else toast("Error al eliminar", "error");
    } catch (e) { toast("Error de conexión con el servidor", "error"); }
}

function limpiarFormParametro() {
    document.getElementById('titulo-modal-parametro').textContent = 'Nuevo Parámetro de Alerta';
    document.getElementById('editParamId').value    = '';
    document.getElementById('paramNombre').value    = '';
    document.getElementById('paramUmbral').value    = '';
    document.getElementById('paramCondicion').value = 'mayor_que';
    document.getElementById('paramPrioridad').value = 'media';
    document.getElementById('paramActivo').value    = '1';
}

function cambiarTabCultivos(idTab, btn) {
    const seccion = document.getElementById('seccion-cultivos');
    seccion.querySelectorAll('.tab-contenido').forEach(t => t.classList.remove('activo'));
    seccion.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('activo'));
    document.getElementById(idTab).classList.add('activo');
    btn.classList.add('activo');
}

async function cargarSeccionCultivos() {
    await Promise.all([cargarTablaCultivosSeccion(), cargarTablaCosechasSeccion()]);
}

async function cargarTablaCultivosSeccion() {
    const contenedor = document.getElementById('tabla-cultivos-seccion');
    if (!contenedor) return;
    contenedor.innerHTML = '<p style="color:#888;">Cargando...</p>';
    try {
        const cultivos = await (await fetch('/api/cultivos/lista')).json();
        if (!Array.isArray(cultivos) || cultivos.length === 0) {
            contenedor.innerHTML = `<div class="card" style="text-align:center;padding:30px;color:#888;">
                No hay cultivos registrados aún.<br>
                <button style="margin-top:12px;" onclick="abrirModal('modal-siembra')">🌱 Registrar primera siembra</button>
            </div>`;
            return;
        }
        let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
            <span style="font-size:13px;color:#666;">${cultivos.length} cultivo(s) registrado(s)</span>
            <button class="btn-verde btn-sm" onclick="cargarTablaCultivosSeccion()">↺ Actualizar</button>
        </div>
        <table><thead><tr>
            <th>#</th><th>Nombre</th><th>Tipo</th>
            <th>Fecha Siembra</th><th>Cantidad</th>
            <th>Acciones</th>
        </tr></thead><tbody>`;
        cultivos.forEach(c => {
            html += `<tr id="fila-cultivo-sec-${c.idCultivo}">
                <td>${c.idCultivo}</td>
                <td><strong>${c.nombreCultivo}</strong></td>
                <td>${c.tipo_cultivo || '—'}</td>
                <td>${c.fecha_siembra || '—'}</td>
                <td>${c.cantidad} plantas</td>
                <td style="white-space:nowrap;">
                    <button class="btn-naranja btn-sm" onclick='abrirEditarCultivo(${JSON.stringify(c)})'>✏️ Editar</button>
                    <button class="btn-rojo btn-sm" onclick="eliminarCultivoSeccion(${c.idCultivo})">🗑 Eliminar</button>
                </td>
            </tr>`;
        });
        html += '</tbody></table>';
        contenedor.innerHTML = html;
    } catch (e) {
        contenedor.innerHTML = '<p style="color:red;">Error al cargar cultivos.</p>';
    }
}

async function eliminarCultivoSeccion(id) {
    if (!await confirmar('¿Eliminar este cultivo? También se eliminarán sus cosechas asociadas.')) return;
    try {
        const res  = await fetch(`/api/cultivos/eliminar/${id}`, { method: 'DELETE' });
        const data = await res.json();
        if (data.status === 'success') {
            await cargarTablaCultivosSeccion();
            await cargarTablaCosechasSeccion();
        } else { toast('Error: ' + (data.error || 'desconocido'), 'error'); }
    } catch(e) { toast('Error de red: ' + e, "error"); }
}
async function guardarEdicionCultivo() {
    const id = document.getElementById('editCultivoId').value;
    const body = {
        nombre:  document.getElementById('editNombreCultivo').value.trim(),
        fecha:   document.getElementById('editFechaSiembra').value,
        cantidad:document.getElementById('editCantidadPlantas').value,
        tamano:  document.getElementById('editTamanoPlantas').value,
        idTipo:  document.getElementById('editTipoCultivo').value
    };
    if (!body.nombre || !body.fecha || !body.cantidad) {
        toast("Completa todos los campos obligatorios", "warning"); return;
    }
    try {
        const res = await fetch(`/api/cultivos/editar/${id}`, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });
        const data = await res.json();
        if (data.status === 'success') {
            cerrarModal('modal-editar-cultivo');
            await cargarTablaCultivosSeccion();
            await cargarTablaCultivos();
            await cargarCultivosActivos();
        } else { toast('Error: ' + (data.error || 'desconocido'), 'error'); }
    } catch(e) { toast('Error de red: ' + e, "error"); }
}

async function cargarTablaCosechasSeccion() {
    const contenedor = document.getElementById('tabla-cosechas-seccion');
    if (!contenedor) return;
    contenedor.innerHTML = '<p style="color:#888;">Cargando...</p>';
    try {
        const cosechas = await (await fetch('/api/cosechas/lista')).json();
        if (!Array.isArray(cosechas) || cosechas.length === 0) {
            contenedor.innerHTML = `<div class="card" style="text-align:center;padding:30px;color:#888;">
                No hay cosechas registradas aún.<br>
                <button style="margin-top:12px;" onclick="abrirModal('modal-cosecha')">🌾 Registrar cosecha</button>
            </div>`;
            return;
        }
        let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
            <span style="font-size:13px;color:#666;">${cosechas.length} cosecha(s) registrada(s)</span>
            <button class="btn-verde btn-sm" onclick="cargarTablaCosechasSeccion()">↺ Actualizar</button>
        </div>
        <table><thead><tr>
            <th>#</th><th>Cultivo</th><th>Fecha</th>
            <th>Cantidad</th><th>Calidad</th><th>Observaciones</th><th>Acciones</th>
        </tr></thead><tbody>`;
        cosechas.forEach(cs => {
            html += `<tr>
                <td>${cs.idCosecha}</td>
                <td><strong>${cs.nombreCultivo || '—'}</strong></td>
                <td>${cs.fecha || '—'}</td>
                <td>${cs.cantidad}</td>
                <td><span class="badge badge-verde">${cs.calidad || '—'}</span></td>
                <td style="max-width:180px;white-space:pre-wrap;">${cs.observaciones || '—'}</td>
                <td style="white-space:nowrap;">
                    <button class="btn-naranja btn-sm" onclick='abrirEditarCosecha(${JSON.stringify(cs)})'>✏️ Editar</button>
                    <button class="btn-rojo btn-sm" onclick="eliminarCosechaSeccion(${cs.idCosecha})">🗑 Eliminar</button>
                </td>
            </tr>`;
        });
        html += '</tbody></table>';
        contenedor.innerHTML = html;
    } catch (e) {
        contenedor.innerHTML = '<p style="color:red;">Error al cargar cosechas.</p>';
    }
}

async function eliminarCosechaSeccion(id) {
    if (!await confirmar('¿Seguro que quieres eliminar esta cosecha?')) return;
    try {
        const res  = await fetch(`/api/cosechas/eliminar/${id}`, { method: 'DELETE' });
        const data = await res.json();
        if (data.status === 'success') {
            await cargarTablaCosechasSeccion();
            await cargarTablaCosechas();
        } else { toast('Error: ' + (data.error || 'desconocido'), 'error'); }
    } catch(e) { toast('Error de red: ' + e, "error"); }
}
async function guardarEdicionCosecha() {
    const id = document.getElementById('editCosechaId').value;
    const body = {
        fecha:         document.getElementById('editFechaCosecha').value,
        cantidad:      document.getElementById('editCantidadCosecha').value,
        calidad:       document.getElementById('editCalidadCosecha').value.trim(),
        observaciones: document.getElementById('editObservacionesCosecha').value.trim()
    };
    if (!body.fecha || !body.cantidad) { toast("Completa todos los campos obligatorios", "warning"); return; }
    try {
        const res  = await fetch(`/api/cosechas/editar/${id}`, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });
        const data = await res.json();
        if (data.status === 'success') {
            cerrarModal('modal-editar-cosecha');
            await cargarTablaCosechasSeccion();
            await cargarTablaCosechas();
        } else { toast('Error: ' + (data.error || 'desconocido'), 'error'); }
    } catch(e) { toast('Error de red: ' + e, "error"); }
}

function cambiarTabAnalitica(idTab, btn) {
    const seccion = document.getElementById('seccion-analitica');
    seccion.querySelectorAll('.tab-contenido').forEach(t => t.classList.remove('activo'));
    seccion.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('activo'));
    document.getElementById(idTab).classList.add('activo');
    btn.classList.add('activo');
    if (idTab === 'tab-an-cultivos')  cargarReporteCultivos();
    if (idTab === 'tab-an-cosechas')  cargarReporteCosechas();
    if (idTab === 'tab-an-sensores')  cargarSensoresEnAnalitica();
}
let _grafCultivosTipo = null;
let _grafCultivosMes  = null;
let _grafCosechasCult = null;
let _grafCosechasMes  = null;

async function cargarReporteCultivos() {
    try {
        const tipos = await (await fetch('/api/tipo_cultivo/lista')).json();
        const sel = document.getElementById('filtro-an-tipo-cultivo');
        if (sel && sel.options.length === 1) {
            tipos.forEach(t => {
                const o = document.createElement('option');
                o.value = t.idTipo_Cultivo;
                o.textContent = t.nombre_planta;
                sel.appendChild(o);
            });
        }
    } catch(e) {}

    let cultivos;
    try {
        cultivos = await (await fetch('/api/cultivos/lista')).json();
    } catch(e) {
        document.getElementById('tabla-reporte-cultivos').innerHTML = '<p style="color:red;">Error al cargar datos.</p>';
        return;
    }
    const tipoFiltro  = document.getElementById('filtro-an-tipo-cultivo')?.value || '';
    const desdeStr    = document.getElementById('filtro-an-fecha-desde')?.value || '';
    const hastaStr    = document.getElementById('filtro-an-fecha-hasta')?.value || '';

    let filtrados = cultivos;
    if (tipoFiltro) filtrados = filtrados.filter(c => String(c.idTipo_Cultivo) === tipoFiltro);
    if (desdeStr)   filtrados = filtrados.filter(c => c.fecha_siembra >= desdeStr);
    if (hastaStr)   filtrados = filtrados.filter(c => c.fecha_siembra <= hastaStr);
    const totalPlantas = filtrados.reduce((s, c) => s + (parseInt(c.cantidad) || 0), 0);
    const tipos = [...new Set(filtrados.map(c => c.tipo_cultivo).filter(Boolean))];
    document.getElementById('kpi-reporte-cultivos').innerHTML = `
        <div class="kpi-card" style="cursor:default;">
            <div class="kpi-icono kpi-icono-naranja">🌱</div>
            <div><div class="kpi-valor">${filtrados.length}</div><div class="kpi-label">Cultivos</div></div>
        </div>
        <div class="kpi-card" style="cursor:default;">
            <div class="kpi-icono kpi-icono-verde">🪴</div>
            <div><div class="kpi-valor">${totalPlantas.toLocaleString()}</div><div class="kpi-label">Total plantas</div></div>
        </div>
        <div class="kpi-card" style="cursor:default;">
            <div class="kpi-icono kpi-icono-azul">🏷️</div>
            <div><div class="kpi-valor">${tipos.length}</div><div class="kpi-label">Tipos distintos</div></div>
        </div>`;
    const porTipo = {};
    filtrados.forEach(c => {
        const t = c.tipo_cultivo || 'Sin tipo';
        porTipo[t] = (porTipo[t] || 0) + (parseInt(c.cantidad) || 0);
    });
    const coloresTipo = ['#27ae60','#3498db','#e67e22','#9b59b6','#e74c3c','#1abc9c'];
    const ctx1 = document.getElementById('graficaCultivosTipo')?.getContext('2d');
    if (ctx1) {
        if (_grafCultivosTipo) _grafCultivosTipo.destroy();
        _grafCultivosTipo = new Chart(ctx1, {
            type: 'doughnut',
            data: {
                labels: Object.keys(porTipo),
                datasets: [{ data: Object.values(porTipo), backgroundColor: coloresTipo, borderWidth: 2 }]
            },
            options: { responsive: true, plugins: { legend: { position: 'bottom' } } }
        });
    }
    const porMes = {};
    filtrados.forEach(c => {
        if (!c.fecha_siembra) return;
        const mes = c.fecha_siembra.substring(0, 7);
        porMes[mes] = (porMes[mes] || 0) + 1;
    });
    const mesesOrden = Object.keys(porMes).sort();
    const ctx2 = document.getElementById('graficaCultivosMes')?.getContext('2d');
    if (ctx2) {
        if (_grafCultivosMes) _grafCultivosMes.destroy();
        _grafCultivosMes = new Chart(ctx2, {
            type: 'bar',
            data: {
                labels: mesesOrden,
                datasets: [{ label: 'Siembras', data: mesesOrden.map(m => porMes[m]), backgroundColor: '#2c3e50' }]
            },
            options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { stepSize: 1 } } } }
        });
    }
    const tabla = document.getElementById('tabla-reporte-cultivos');
    if (filtrados.length === 0) {
        tabla.innerHTML = '<p style="color:#888; text-align:center; padding:20px;">No hay cultivos con los filtros aplicados.</p>';
        return;
    }
    let html = `<table><thead><tr>
        <th>#</th><th>Nombre</th><th>Tipo</th><th>Fecha Siembra</th><th>Cantidad</th>
    </tr></thead><tbody>`;
    filtrados.forEach(c => {
        html += `<tr>
            <td>${c.idCultivo}</td>
            <td><strong>${c.nombreCultivo}</strong></td>
            <td>${c.tipo_cultivo || '—'}</td>
            <td>${c.fecha_siembra || '—'}</td>
            <td>${c.cantidad} plantas</td>
        </tr>`;
    });
    html += '</tbody></table>';
    tabla.innerHTML = html;
}

async function cargarReporteCosechas() {
    try {
        const cultivos = await (await fetch('/api/cultivos/lista')).json();
        const sel = document.getElementById('filtro-an-cultivo-cosecha');
        if (sel && sel.options.length === 1) {
            cultivos.forEach(c => {
                const o = document.createElement('option');
                o.value = c.idCultivo;
                o.textContent = c.nombreCultivo;
                sel.appendChild(o);
            });
        }
    } catch(e) {}

    let cosechas;
    try {
        cosechas = await (await fetch('/api/cosechas/lista')).json();
    } catch(e) {
        document.getElementById('tabla-reporte-cosechas').innerHTML = '<p style="color:red;">Error al cargar datos.</p>';
        return;
    }
    const cultFiltro = document.getElementById('filtro-an-cultivo-cosecha')?.value || '';
    const desdeStr   = document.getElementById('filtro-an-cosecha-desde')?.value || '';
    const hastaStr   = document.getElementById('filtro-an-cosecha-hasta')?.value || '';

    let filtradas = cosechas;
    if (cultFiltro) filtradas = filtradas.filter(c => String(c.idCultivo) === cultFiltro);
    if (desdeStr)   filtradas = filtradas.filter(c => c.fecha >= desdeStr);
    if (hastaStr)   filtradas = filtradas.filter(c => c.fecha <= hastaStr);
    const totalCant = filtradas.reduce((s, c) => s + (parseFloat(c.cantidad) || 0), 0);
    const cultivos  = [...new Set(filtradas.map(c => c.nombreCultivo).filter(Boolean))];
    document.getElementById('kpi-reporte-cosechas').innerHTML = `
        <div class="kpi-card" style="cursor:default;">
            <div class="kpi-icono kpi-icono-verde">🌾</div>
            <div><div class="kpi-valor">${filtradas.length}</div><div class="kpi-label">Cosechas</div></div>
        </div>
        <div class="kpi-card" style="cursor:default;">
            <div class="kpi-icono kpi-icono-naranja">📦</div>
            <div><div class="kpi-valor">${totalCant.toLocaleString()}</div><div class="kpi-label">Total cosechado</div></div>
        </div>
        <div class="kpi-card" style="cursor:default;">
            <div class="kpi-icono kpi-icono-azul">🌱</div>
            <div><div class="kpi-valor">${cultivos.length}</div><div class="kpi-label">Cultivos involucrados</div></div>
        </div>`;
    const porCultivo = {};
    filtradas.forEach(c => {
        const k = c.nombreCultivo || 'Sin nombre';
        porCultivo[k] = (porCultivo[k] || 0) + (parseFloat(c.cantidad) || 0);
    });
    const colores = ['#27ae60','#3498db','#e67e22','#9b59b6','#e74c3c','#1abc9c'];
    const ctx3 = document.getElementById('graficaCosechasCultivo')?.getContext('2d');
    if (ctx3) {
        if (_grafCosechasCult) _grafCosechasCult.destroy();
        _grafCosechasCult = new Chart(ctx3, {
            type: 'bar',
            data: {
                labels: Object.keys(porCultivo),
                datasets: [{ label: 'Cosechado', data: Object.values(porCultivo), backgroundColor: colores }]
            },
            options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true } } }
        });
    }
    const porMes = {};
    filtradas.forEach(c => {
        if (!c.fecha) return;
        const mes = c.fecha.substring(0, 7);
        porMes[mes] = (porMes[mes] || 0) + (parseFloat(c.cantidad) || 0);
    });
    const meses = Object.keys(porMes).sort();
    const ctx4 = document.getElementById('graficaCosechasMes')?.getContext('2d');
    if (ctx4) {
        if (_grafCosechasMes) _grafCosechasMes.destroy();
        _grafCosechasMes = new Chart(ctx4, {
            type: 'line',
            data: {
                labels: meses,
                datasets: [{ label: 'Cosecha total', data: meses.map(m => porMes[m]),
                    borderColor: '#27ae60', backgroundColor: 'rgba(39,174,96,0.1)',
                    borderWidth: 2, fill: true, tension: 0.4 }]
            },
            options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true } } }
        });
    }
    const tabla = document.getElementById('tabla-reporte-cosechas');
    if (filtradas.length === 0) {
        tabla.innerHTML = '<p style="color:#888; text-align:center; padding:20px;">No hay cosechas con los filtros aplicados.</p>';
        return;
    }
    let html = `<table><thead><tr>
        <th>#</th><th>Cultivo</th><th>Fecha</th><th>Cantidad</th><th>Calidad</th><th>Observaciones</th>
    </tr></thead><tbody>`;
    filtradas.forEach(cs => {
        html += `<tr>
            <td>${cs.idCosecha}</td>
            <td><strong>${cs.nombreCultivo || '—'}</strong></td>
            <td>${cs.fecha || '—'}</td>
            <td>${cs.cantidad}</td>
            <td><span class="badge badge-verde">${cs.calidad || '—'}</span></td>
            <td style="max-width:200px;">${cs.observaciones || '—'}</td>
        </tr>`;
    });
    html += '</tbody></table>';
    tabla.innerHTML = html;
}

async function cargarDashboardInicio() {
    const fechaEl = document.getElementById('inicio-fecha');
    if (fechaEl) {
        const ahora = new Date();
        fechaEl.textContent = ahora.toLocaleDateString('es-MX', {
            weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'
        });
    }
    const nombreEl  = document.getElementById('inicio-nombre');
    const avatarEl  = document.getElementById('inicio-avatar');
    try {
        const res  = await fetch('/api/usuario/info');
        const user = await res.json();
        if (nombreEl) nombreEl.textContent = user.nombre?.split(' ')[0] || '—';
        if (avatarEl) {
            if (user.foto_perfil) {
                avatarEl.innerHTML = '<img src="' + user.foto_perfil + '" alt="Foto" style="width:100%;height:100%;object-fit:cover;border-radius:50%;display:block;">';
                avatarEl.style.background = 'transparent';
                avatarEl.style.padding = '0';
            } else {
                avatarEl.textContent = (user.nombre || 'U')[0].toUpperCase();
                avatarEl.style.background = '';
            }
        }
    } catch (e) {}
    await Promise.allSettled([
        _kpiDispSensores(),
        _kpiCultivos(),
        _kpiAlertas(),
        _panelLecturas(),
        _panelAlertasRecientes(),
        _panelCultivos(),
        _panelBomba()
    ]);
}

async function _kpiDispSensores() {
    try {
        const [rd, rs] = await Promise.all([
            fetch('/api/dispositivos/lista'),
            fetch('/api/sensores/lista')
        ]);
        const disp = await rd.json();
        const sens = await rs.json();
        const dEl = document.getElementById('kpi-dispositivos');
        const sEl = document.getElementById('kpi-sensores');
        if (dEl) dEl.textContent = Array.isArray(disp) ? disp.length : '—';
        if (sEl) sEl.textContent = Array.isArray(sens) ? sens.length : '—';
    } catch (e) {}
}

async function _kpiCultivos() {
    try {
        const res = await fetch('/api/cultivos/lista');
        const data = await res.json();
        const el = document.getElementById('kpi-cultivos');
        if (el) el.textContent = Array.isArray(data) ? data.length : '—';
    } catch (e) {}
}

async function _kpiAlertas() {
    try {
        const res  = await fetch('/api/alertas/conteo');
        const data = await res.json();
        const el   = document.getElementById('kpi-alertas');
        if (el) el.textContent = data.nuevas ?? '—';
    } catch (e) {}
}

async function _panelLecturas() {
    const cont = document.getElementById('inicio-lecturas');
    if (!cont) return;
    const turno = ++revisionPanelSensores;
    try {
        const sensores = await pedirDatosSensor('/api/sensores/lista');
        if (!Array.isArray(sensores)) throw new Error('Lista de sensores no válida.');
        const lecturas = await Promise.allSettled(sensores.map(s =>
            pedirDatosSensor(`/api/sensores/datos-actuales/${s.idSensore}`)));
        if (turno !== revisionPanelSensores) return;
        if (!sensores.length) {
            cont.innerHTML = `<div class="inicio-vacio">${escaparTextoSensor(t('inicio_sin_sensores'))}</div>`;
            return;
        }
        cont.innerHTML = sensores.map((sensor, i) => {
            const dato = lecturas[i].status === 'fulfilled' ? lecturas[i].value : null;
            const valor = valorGraficaSensor(dato);
            const antiguo = lecturaSensorAntigua(dato);
            const provisional = dato?.estado_medicion && dato.estado_medicion !== 'ok';
            const color = antiguo ? '#777' : colorCalidadSensor(dato);
            const estado = !dato ? 'Sin datos disponibles' : antiguo ? 'Sin datos recientes' :
                valor === null ? 'Sin lectura válida' : provisional ? 'Lectura provisional' : 'Lectura recibida';
            const calidad = textoCalidadAgua(dato);
            const tds = detalleTDSSensor(dato);
            const nombreDispositivo = sensor.nombre_dispositivo || `Dispositivo ${sensor.idDispositivo}`;
            const valorTexto = valor === null ? '—' : valor.toLocaleString('es-MX', {maximumFractionDigits: 4});
            return `<div class="sensor-fila">
                <div style="display:flex;align-items:center;gap:8px;min-width:0;">
                    <div class="sensor-dot ${!antiguo && !provisional && valor !== null ? 'sensor-dot-on' : 'sensor-dot-off'}"></div>
                    <div>
                        <div class="sensor-nombre">${escaparTextoSensor(nombreSensorAgua(sensor.tipo_sensor))}</div>
                        <div style="font-size:11px;color:#68756b;">${escaparTextoSensor(nombreDispositivo)} · ID ${escaparTextoSensor(sensor.idSensore)}</div>
                        <div style="font-size:11px;color:${color};font-weight:600;">${estado}</div>
                        ${calidad ? `<div style="font-size:11px;color:${color};">${escaparTextoSensor(calidad)}</div>` : ''}
                        ${dato?.fecha_hora ? `<div style="font-size:10px;color:#777;">${escaparTextoSensor(dato.fecha_hora)}</div>` : ''}
                    </div>
                </div>
                <div style="text-align:right;">
                    <span class="sensor-valor-big" style="color:${color};">${valorTexto}</span>
                    <span class="sensor-unidad">${valor === null ? '' : escaparTextoSensor(dato?.unidad || sensor.unidad_medida)}</span>
                    ${antiguo && valor !== null ? '<div style="font-size:10px;color:#777;">Último dato recibido</div>' : ''}
                    ${tds ? `<div style="font-size:11px;color:${color};">${escaparTextoSensor(tds)}</div>` : ''}
                </div>
            </div>`;
        }).join('');
    } catch (_) {
        if (turno === revisionPanelSensores) cont.innerHTML = '<div class="inicio-vacio" style="color:#b23b3b;">No se pudieron consultar las lecturas.</div>';
    }
}

async function _panelAlertasRecientes() {
    const cont = document.getElementById('inicio-alertas');
    if (!cont) return;
    try {
        const res    = await fetch('/api/alertas/historial?limite=5');
        const alertas = await res.json();
        if (!Array.isArray(alertas) || alertas.length === 0) {
            cont.innerHTML = `<div class="inicio-vacio">${t('inicio_sin_alertas')}</div>`; return;
        }
        const colores = { critica:'badge-rojo', alta:'badge-naranja', media:'badge-amarillo', baja:'badge-verde' };
        cont.innerHTML = alertas.map(a => `
            <div class="alerta-fila">
                <span class="badge ${colores[a.prioridad] || 'badge-gris'}" style="flex-shrink:0">${a.prioridad}</span>
                <div style="flex:1;min-width:0;">
                    <div style="font-weight:600;color:#2c3e50;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${a.tipo_sensor || 'Sensor'}</div>
                    <div style="color:#999;font-size:11px;">${a.fecha_hora || ''}</div>
                </div>
                <span class="badge ${a.estado === 'nueva' ? 'badge-rojo' : 'badge-gris'}" style="flex-shrink:0;font-size:11px;">${a.estado}</span>
            </div>`).join('');
    } catch (e) {
        cont.innerHTML = '<div class="inicio-vacio" style="color:#e74c3c;">Error al cargar.</div>';
    }
}

async function _panelCultivos() {
    const cont = document.getElementById('inicio-cultivos');
    if (!cont) return;
    try {
        const res     = await fetch('/api/cultivos/lista');
        const cultivos = await res.json();
        if (!Array.isArray(cultivos) || cultivos.length === 0) {
            cont.innerHTML = `<div class="inicio-vacio">${t('inicio_sin_cultivos')}<br>
                <button style="margin-top:10px;" onclick="mostrarSeccion('seccion-cultivos')">${t('inicio_nueva_siembra')}</button>
            </div>`; return;
        }
        cont.innerHTML = cultivos.slice(0, 5).map(c => `
            <div class="cultivo-fila">
                <div>
                    <div style="font-weight:600;color:#2c3e50;">${c.nombreCultivo}</div>
                    <div style="color:#aaa;font-size:11px;">${c.tipo_cultivo || 'Sin tipo'} · Sembrado ${c.fecha_siembra || '—'}</div>
                </div>
                <span class="badge badge-verde">${c.cantidad} plantas</span>
            </div>`).join('') +
            (cultivos.length > 5 ? `<div class="inicio-vacio" style="padding:10px;">+${cultivos.length - 5} más</div>` : '');
    } catch (e) {
        cont.innerHTML = '<div class="inicio-vacio" style="color:#e74c3c;">Error al cargar.</div>';
    }
}

// El panel de la bomba utiliza _panelBomba(), definido en dashboard.html revisado.
// Así hay una sola implementación para confirmar órdenes y mostrar la configuración.

document.addEventListener("DOMContentLoaded", async () => {
    await cargarDispositivosGlobal();
    cargarDashboardInicio();
    await actualizarListaDispositivos();
    await cargarSensoresEnLogica();
    await cargarSensoresEnAnalitica();
    cargarInfoUsuario();
    actualizarBadgeNav();

        setInterval(actualizarBadgeNav, 60000);
});


// Compatibilidad con llamadas antiguas: se delega en el controlador único del HTML.
// Requiere dashboard.html revisado el 21/10/2026 y el app.py con config_guardada.
function guardarConfigRelevador() {
    if (typeof bombaEnviarConfiguracion !== 'function') {
        toast('Instala también el dashboard.html revisado para usar el control de la bomba.', 'warning');
        return Promise.resolve(false);
    }
    return guardarConfigBomba();
}

function actualizarVistaRelevador(data) {
    if (typeof bombaValidarConfig !== 'function' || typeof actualizarIndicadorBomba !== 'function') return false;
    try {
        const config = bombaValidarConfig(data);
        actualizarIndicadorBomba(config.modo, config.estado_manual);
        return true;
    } catch (error) {
        if (typeof bombaMostrarMensaje === 'function') bombaMostrarMensaje(error.message, true);
        return false;
    }
}


async function cargarRedActual() {
    if (!dispositivoActualId) {
        await cargarDispositivosGlobal();
    }
    try {
        const r = await fetch('/api/wifi/estado?device_id=' + dispositivoActualId);
        const d = await r.json();
        const icon  = document.getElementById('wifi-estado-icon');
        const red   = document.getElementById('wifi-red-actual');
        const texto = document.getElementById('wifi-estado-texto');

        if (d.ssid) {
            icon.textContent  = '✅';
            red.textContent   = 'Red: ' + d.ssid;
            texto.textContent = 'Última configuración enviada: ' + (d.fecha || 'desconocida');
        } else {
            icon.textContent  = '❓';
            red.textContent   = t('wifi_sin_config');
            texto.textContent = 'Usa el formulario para enviar una red al Arduino';
        }

        cargarHistorialWifi();
    } catch(e) {
        document.getElementById('wifi-red-actual').textContent = 'Error al consultar estado';
    }
}

async function cargarHistorialWifi() {
    try {
        const r = await fetch('/api/wifi/historial?device_id=' + dispositivoActualId);
        const d = await r.json();
        const cont = document.getElementById('wifi-historial');

        if (!d.historial || d.historial.length === 0) {
            cont.innerHTML = `<p>${t('wifi_sin_redes')}</p>`;
            return;
        }

        cont.innerHTML = d.historial.map(item => `
            <div style="display:flex; justify-content:space-between; align-items:center;
                        padding:10px 0; border-bottom:1px solid #eee;">
                <div>
                    <div style="font-weight:600;">📶 ${item.ssid}</div>
                    <div style="font-size:12px; color:#999;">${item.fecha}</div>
                </div>
                <button onclick="usarRedGuardada('${item.ssid}')"
                    style="padding:5px 12px; border-radius:6px; border:1px solid
                           color:#27ae60; background:white; cursor:pointer; font-size:13px;">
                    ${t('wifi_usar_esta')}
                </button>
            </div>
        `).join('');
    } catch(e) {
        document.getElementById('wifi-historial').textContent = 'Error al cargar historial';
    }
}

function usarRedGuardada(ssid) {
    document.getElementById('wifi-ssid').value = ssid;
    document.getElementById('wifi-pass').value = '';
    document.getElementById('wifi-pass').focus();
    toast('Red seleccionada. Ingresa la contraseña y envía.', 'info');
}

function toggleVerPass() {
    const input = document.getElementById('wifi-pass');
    input.type = input.type === 'password' ? 'text' : 'password';
}

async function enviarRedWifi() {
    if (!dispositivoActualId) {
        toast('Selecciona un dispositivo primero', 'warning');
        return;
    }

    const ssid = document.getElementById('wifi-ssid').value.trim();
    const pass = document.getElementById('wifi-pass').value;

    if (!ssid) { toast('Ingresa el nombre de la red (SSID)', 'warning'); return; }
    if (!pass)  { toast('Ingresa la contraseña de la red', 'warning'); return; }

    const btn = document.querySelector('#seccion-wifi .btn-verde');
    btn.disabled = true;
    btn.textContent = 'Enviando...';

    try {
        const r = await fetch('/api/wifi/configurar', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ssid, password: pass, device_id: dispositivoActualId })
        });
        const d = await r.json();

        if (r.ok && d.status === 'ok') {
            toast('¡Red enviada al Arduino correctamente!', 'success');
            document.getElementById('wifi-ssid').value = '';
            document.getElementById('wifi-pass').value = '';
            cargarRedActual();
        } else {
            toast('Error: ' + (d.mensaje || 'No se pudo enviar'), 'error');
        }
    } catch(e) {
        toast('Error de conexión al enviar la red', 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = '📡 Enviar al Arduino';
    }
}



// Rangos del cultivo dentro de Alertas > Parametros / Umbrales.
// Usa las API del app.py HYDROSENSE - RANGOS INTEGRADOS - 29/09/2026.
const rangosHydrosense = (() => {
'use strict';
let montado = false, inicio = null;
const $ = id => document.getElementById('hs-' + id);
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
  $('actualizar').disabled = guardando;
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
    fila.insertCell().textContent = item.tipo === 'luz' ? 'índice LDR' : (item.unidad || '—');
    const lectura = fila.insertCell(), estado = document.createElement('span'); fila.insertCell().append(estado);
    const activar = fila.insertCell(), prueba = fila.insertCell();
    if (item.idSensor !== null) {
      celdas.set(item.idSensor,{lectura,estado});
      const casilla = document.createElement('input'); casilla.type = 'checkbox';
      casilla.checked = item.disponible && item.activo_actual;
      casilla.setAttribute('aria-label','Activar alertas de ' + (sensorTexto[item.tipo] || item.tipo) + ', ID ' + item.idSensor);
      activar.append(casilla); casillas.set(item.idSensor,{casilla,disponible:item.disponible});
      const boton = document.createElement('button'); boton.type = 'button'; boton.textContent = 'Probar'; boton.className = 'secundario'; boton.disabled = !item.disponible;
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
    mostrarPrevia(dato); mensaje('Revisa los límites y las casillas. Al guardar, se crearán o actualizarán las reglas de la lista inferior.');
    return true;
  } catch (error) { if (turno === revision) mensaje(error.message,'error'); return false; }
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
  const tab = document.getElementById('tab-parametros');
  const seccion = document.getElementById('seccion-alertas');
  if (!tab?.classList.contains('activo') || !seccion || getComputedStyle(seccion).display === 'none') return;
  const seleccionado = dispositivo; consultando = true;
  try { const dato = await api('/api/rangos/dispositivo/' + seleccionado); if (seleccionado === dispositivo && !guardando) recibirEstado(dato); }
  catch (error) { if (seleccionado === dispositivo) mensaje(error.message,'error'); }
  finally { consultando = false; }
}
async function guardar() {
  if (!previa || !puedeEditar || guardando) return;
  const seleccion = {cultivo:previa.cultivo,etapa:previa.etapa,activar_sensores:[...casillas].filter(([,v])=>v.casilla.checked && v.disponible).map(([id])=>id)};
  guardando = true; bloqueo(); mensaje('Guardando…');
  let aplicado = false;
  try {
    await api('/api/rangos/aplicar/' + dispositivo,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(seleccion)});
    aplicado = true;
    recibirEstado(await api('/api/rangos/dispositivo/' + dispositivo));
    await cargarParametrosAlerta();
    await actualizarBadgeNav();
    if (await cargarPrevia()) mensaje('Rangos guardados. Las próximas lecturas se evaluarán con estos límites.','exito');
    else mensaje('Los rangos se guardaron, pero no se pudo actualizar la vista. Recarga la página.','error');
  } catch (error) { mensaje((aplicado ? 'Los rangos se guardaron, pero no se pudo actualizar la vista: ' : '') + error.message,'error'); }
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

function abrir() {
  if (montado) return inicio;
  const destino = document.getElementById('tabla-parametros-alerta');
  if (!destino) return Promise.resolve();
  const estilo = document.createElement('style');
  estilo.id = 'hs-rangos-estilo';
  estilo.textContent = `
#hs-rangos-panel{margin:0 0 22px;min-width:0}
#hs-rangos-panel h3{margin:0 0 10px;color:#1a3d2b;font-size:18px}
#hs-rangos-panel p{line-height:1.5}
#hs-rangos-panel .hs-nota,#hs-rangos-panel small{color:#526d5e;font-size:13px}
#hs-rangos-panel small{display:block;margin-top:4px;max-width:260px}
#hs-rangos-panel .hs-selectores{display:grid;grid-template-columns:1fr 1.6fr 1fr;gap:14px;margin:18px 0 12px}
#hs-rangos-panel label{margin:0;font-weight:600;font-size:14px;min-width:0}
#hs-rangos-panel select{display:block;width:100%;margin:7px 0 0;padding:10px;border:1px solid #bcd4c6;border-radius:8px;background:white;box-sizing:border-box}
#hs-rangos-panel .hs-tabla{overflow-x:auto;margin-top:16px}
#hs-rangos-panel table{margin:0;width:100%;font-size:13px}
#hs-rangos-panel th,#hs-rangos-panel td{padding:11px 9px;vertical-align:top}
#hs-rangos-panel th{font-size:12px}
#hs-rangos-panel .numero{white-space:nowrap}
#hs-rangos-panel input[type=checkbox]{width:19px;height:19px;margin:2px;accent-color:#2d6a4f;cursor:pointer}
#hs-rangos-panel button{font-family:inherit}
#hs-rangos-panel .secundario{padding:6px 10px;border:1px solid #bcd4c6;border-radius:7px;background:white;color:#245c40;cursor:pointer}
#hs-rangos-panel button:disabled,#hs-rangos-panel input:disabled{opacity:.45;cursor:default}
#hs-rangos-panel .hs-acciones{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin:18px 0 12px}
#hs-rangos-panel .estado{display:inline-block;padding:4px 7px;border-radius:5px;background:#edf2ef;white-space:nowrap;font-size:12px}
#hs-rangos-panel .estado.en_rango{background:#e3f4e7;color:#18502c}
#hs-rangos-panel .estado.bajo,#hs-rangos-panel .estado.alto,#hs-rangos-panel .estado.alerta,#hs-rangos-panel .estado.rango_inconsistente{background:#fff0db;color:#7c4700}
#hs-rangos-panel #hs-mensaje{font-size:14px;min-height:21px;color:#355d46}
#hs-rangos-panel #hs-mensaje.error{color:#b02a2a}
#hs-rangos-panel #hs-mensaje.exito{color:#176448;font-weight:600}
#hs-rangos-panel .hs-detalles{font-size:13px;color:#526d5e;padding:12px 0;border-top:1px solid #e1ebe4}
#hs-rangos-panel summary{cursor:pointer;font-weight:600}
#hs-rangos-panel #hs-panelPrueba{padding:12px 16px;background:#f0f7f2;border-radius:8px;margin-top:12px}
#hs-rangos-panel #hs-prueba{white-space:pre-line;font-size:14px}
#hs-rangos-panel .hs-pie{margin-bottom:0}
@media(max-width:760px){#hs-rangos-panel .hs-selectores{grid-template-columns:1fr}#hs-rangos-panel{padding:18px}}
`;
  document.head.append(estilo);
  const panel = document.createElement('section');
  panel.id = 'hs-rangos-panel'; panel.className = 'card';
  panel.setAttribute('aria-label','Rangos por cultivo y etapa');
  panel.innerHTML = `
<h3>Rangos por cultivo y etapa</h3>
<p class="hs-nota">Selecciona un perfil para preparar los límites de tus sensores. Las casillas indican qué alertas activarás al guardar.</p>
<div class="hs-selectores">
<label for="hs-dispositivo">Dispositivo<select id="hs-dispositivo"><option value="">Cargando…</option></select></label>
<label for="hs-cultivo">Cultivo<select id="hs-cultivo" disabled><option value="">Selecciona…</option><option value="cilantro">Cilantro</option><option value="perejil">Perejil</option><option value="compartido">Cilantro y perejil — depósito compartido</option></select></label>
<label for="hs-etapa">Etapa<select id="hs-etapa" disabled><option value="">Selecciona…</option><option value="general">General</option><option value="inicial">Inicial</option><option value="desarrollo">Desarrollo</option><option value="media">Media</option><option value="final">Final</option></select></label>
</div>
<p class="hs-nota">La EC del cilantro necesita una etapa. En depósito compartido se toma esa etapa del cilantro y el intervalo general del perejil.</p>
<p id="hs-perfilActual" class="hs-nota"></p>
<p id="hs-mensaje" role="status" aria-live="polite"></p>
<div class="hs-tabla"><table>
<thead><tr><th>Sensor</th><th>Mínimo</th><th>Máximo</th><th>Unidad</th><th>Última lectura</th><th>Estado actual</th><th>Activar al guardar</th><th>Prueba</th></tr></thead>
<tbody id="hs-filas"><tr><td colspan="8">Selecciona un dispositivo, un cultivo y una etapa.</td></tr></tbody>
</table></div>
<div class="hs-acciones"><button type="button" id="hs-guardar" class="btn-verde" disabled>Guardar rangos y alertas</button><button type="button" id="hs-actualizar" class="btn-sm">Actualizar lecturas</button><span id="hs-permiso" class="hs-nota"></span></div>
<p class="hs-nota">El estado actual corresponde a las reglas guardadas. “Probar” usa valores de ejemplo. Cada rango genera dos reglas: una para valores bajos y otra para valores altos.</p>
<details class="hs-detalles"><summary>Referencias y límites pendientes</summary>
<p id="hs-iluminacion">Selecciona un perfil para ver las referencias de iluminación.</p>
<p>Temperatura del aire y humedad: límites provisionales de prueba. Temperatura del agua: guía hidropónica general. Activa las alertas del pH después de calibrar la sonda.</p>
<p>El LDR entrega un índice relativo; no mide PPFD. Los límites de luz y de distancia al agua requieren mediciones en tu instalación.</p>
</details>
<div id="hs-panelPrueba" hidden><h4>Resultado de la simulación</h4><p id="hs-prueba" role="status"></p></div>
<p class="hs-nota hs-pie">Las lecturas se actualizan cada 10 segundos mientras esta pestaña está abierta.</p>
`;
  destino.before(panel);
  const titulo = document.createElement('h3');
  titulo.id = 'hs-reglas-guardadas'; titulo.textContent = 'Reglas guardadas';
  destino.before(titulo);
  montado = true;
$('dispositivo').addEventListener('change',cambiarDispositivo);
$('cultivo').addEventListener('change',cargarPrevia);
$('etapa').addEventListener('change',cargarPrevia);
$('guardar').addEventListener('click',guardar);

  $('actualizar').addEventListener('click', async () => {
    if (!dispositivo) { await iniciar(); return; }
    await refrescar();
  });
  inicio = iniciar();
  setInterval(refrescar,10000);
  return inicio;
}
return {abrir, actualizar: refrescar};
})();

// Historial: consulta los registros existentes y mantiene los ejemplos solo en memoria.
const historialHydrosense = (() => {
    const $ = id => document.getElementById(id);
    const filtros = ['filtro-prioridad', 'filtro-estado', 'filtro-sensor-historial'];
    const nombres = {temperatura:'DHT22 · temperatura', humedad:'DHT22 · humedad', ph:'Sonda de pH', luz:'LDR', distancia:'HC-SR04', ec:'Conductividad eléctrica', temperatura_agua:'DS18B20 · agua'};
    const prioridades = {critica:'Crítica', alta:'Alta', media:'Media', baja:'Baja'};
    const estados = {nueva:'Nueva', vista:'Vista', resuelta:'Resuelta'};
    let montado = false, ejemplo = false, limite = 200, revision = 0;
    let consultando = false, mutando = false;
    let registros = new Map(), sensores = [], ejemplos = [], ultimaConsulta = '';
    let detalleActual = null, claveMostrada = null, sensoresPendientes = null;
    const numero = valor => valor === null || valor === undefined || valor === '' || !Number.isFinite(Number(valor))
        ? '—' : Number(valor).toLocaleString('es-MX', {maximumFractionDigits:2});
    const unidad = a => a.tipo_sensor === 'luz' ? 'índice LDR' : (a.unidad_medida || '');
    const crear = (tag, texto, clase) => {
        const nodo = document.createElement(tag);
        if (texto !== undefined) nodo.textContent = texto;
        if (clase) nodo.className = clase;
        return nodo;
    };
    function fecha(valor) {
        if (!valor) return '—';
        const m = String(valor).match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}:\d{2}:\d{2})/);
        return m ? `${m[3]}/${m[2]}/${m[1]} ${m[4]}` : String(valor);
    }
    function boton(texto, accion, clase = 'btn-sm') {
        const b = crear('button', texto, clase); b.type = 'button';
        b.addEventListener('click', accion); return b;
    }
    function seleccion() { return filtros.map(id => $(id)?.value || ''); }
    function firma() { return JSON.stringify([ejemplo, ...seleccion()]); }
    function criterio() {
        return filtros.filter(id => $(id)?.value).map(id => $(id).selectedOptions[0]?.textContent || $(id).value).join(' · ');
    }
    function mensaje(texto, error = false) {
        $('hh-mensaje').textContent = texto;
        $('hh-mensaje').className = error ? 'hh-error' : 'hh-nota';
        $('hh-mensaje').setAttribute('role', error ? 'alert' : 'status');
    }
    async function pedir(url, options = {}) {
        const controlador = new AbortController();
        const temporizador = setTimeout(() => controlador.abort(), 12000);
        try {
            const respuesta = await fetch(url, {...options, signal:controlador.signal, cache:'no-store'});
            if (respuesta.status === 401 || respuesta.redirected) throw new Error('Tu sesión terminó. Inicia sesión nuevamente.');
            if (respuesta.status === 403) throw new Error('Tu cuenta no tiene permiso para realizar esta acción.');
            if (!respuesta.ok) throw new Error('El servidor no pudo completar la solicitud. Revisa Flask y la conexión con MySQL.');
            try { return await respuesta.json(); }
            catch (_) { throw new Error('La respuesta del servidor no es válida. Recarga la página e inicia sesión si es necesario.'); }
        } catch (error) {
            if (error.name === 'AbortError') throw new Error('El servidor tardó demasiado en responder. Comprueba que Flask siga encendido.');
            if (error instanceof TypeError) throw new Error('No se pudo conectar con el servidor. Comprueba que Flask siga encendido y abre la dirección actual del sistema.');
            throw error;
        } finally { clearTimeout(temporizador); }
    }
    function condicionRegistrada(a) {
        // El umbral del parámetro puede cambiar. Solo se muestra como histórico
        // el que quedó escrito en el mensaje del evento, no el umbral actual.
        const m = String(a.mensaje || '').match(/^Sensor\s+\d+:\s+valor\s+([-+\d.eE]+)\s+(mayor que|menor que|igual a)\s+umbral\s+([-+\d.eE]+)\s*$/);
        if (!m || !Number.isFinite(Number(m[1])) || !Number.isFinite(Number(m[3])) || Number(m[1]) !== Number(a.valor_detectado)) return null;
        return `${{'mayor que':'>','menor que':'<','igual a':'='}[m[2]]} ${numero(m[3])} ${unidad(a)}`.trim();
    }
    function insignia(tipo, valor) {
        const mapa = tipo === 'estado' ? estados : prioridades;
        const nodo = crear('span', mapa[valor] || 'Sin especificar', 'hh-badge');
        if (Object.hasOwn(mapa, valor)) nodo.classList.add('hh-' + valor);
        return nodo;
    }
    function resumen(lista, hayMas) {
        const contenedor = $('hh-resumen'); contenedor.replaceChildren();
        for (const [titulo, valor] of [
            ['Mostradas',lista.length], ['Nuevas',lista.filter(a => a.estado === 'nueva').length],
            ['Vistas',lista.filter(a => a.estado === 'vista').length], ['Resueltas',lista.filter(a => a.estado === 'resuelta').length]
        ]) {
            const tarjeta = crear('div', undefined, 'hh-contador');
            tarjeta.append(crear('strong', String(valor)), crear('span', titulo)); contenedor.append(tarjeta);
        }
        $('hh-alcance').textContent = (ejemplo ? 'Vista de ejemplo. ' : 'Registros guardados. ') +
            (criterio() ? 'Filtros: ' + criterio() + '. ' : 'Todas las prioridades, estados y sensores. ') +
            (hayMas ? `Se muestran las ${lista.length} alertas más recientes que coinciden. Usa “Mostrar más” para consultar anteriores.` : `${lista.length} alerta(s) encontradas.`);
    }
    function actualizarModo() {
        $('hh-demo').hidden = !ejemplo;
        $('hh-ejemplo').textContent = ejemplo ? 'Volver al historial real' : 'Ver ejemplo';
        $('hh-ejemplo').setAttribute('aria-pressed', String(ejemplo));
        $('tabla-historial-alertas').classList.toggle('hh-ejemplo', ejemplo);
    }
    function abrirParametros() {
        const b = document.querySelector('#seccion-alertas [onclick*="tab-parametros"]');
        if (b) b.click();
    }
    function dibujar(lista, hayMas = false) {
        registros = new Map(lista.map(a => [String(a.idHistorial), a]));
        claveMostrada = firma(); actualizarModo(); resumen(lista, hayMas);
        const destino = $('tabla-historial-alertas'); destino.replaceChildren();
        if (!lista.length) {
            const vacio = crear('div', undefined, 'hh-vacio');
            vacio.append(crear('strong', criterio() ? 'No hay alertas con estos filtros.' : ejemplo ? 'No hay ejemplos para mostrar.' : 'Todavía no hay alertas registradas.'));
            vacio.append(crear('p', criterio()
                ? 'Prueba con todas las prioridades, estados y sensores para consultar el historial completo.'
                : 'Las próximas lecturas que cumplan una regla activa generarán una alerta. Configurar rangos o simular umbrales no crea alertas reales.'));
            vacio.append(boton(criterio() ? 'Quitar filtros' : 'Configurar umbrales', criterio() ? quitarFiltros : abrirParametros));
            destino.append(vacio); return;
        }
        const envoltura = crear('div', undefined, 'hh-tabla');
        const tabla = crear('table'); tabla.setAttribute('aria-label', ejemplo ? 'Ejemplo de historial de alertas, datos simulados' : 'Historial de alertas registradas');
        const cabecera = tabla.createTHead().insertRow();
        for (const titulo of ['Fecha y hora', 'Sensor', 'Valor registrado', 'Condición registrada', 'Prioridad', 'Estado', 'Acciones']) {
            const th = crear('th', titulo); th.scope = 'col'; cabecera.append(th);
        }
        const cuerpo = tabla.createTBody();
        for (const a of lista) {
            const fila = cuerpo.insertRow(); fila.dataset.alerta = String(a.idHistorial);
            if (Object.hasOwn(prioridades,a.prioridad)) fila.classList.add('prioridad-' + a.prioridad);
            const f = fila.insertCell(); f.append(crear('span',fecha(a.fecha_hora)),crear('small',ejemplo ? 'Ejemplo · ' + a.idHistorial : 'Registro #' + a.idHistorial));
            fila.insertCell().textContent = nombres[a.tipo_sensor] || a.tipo_sensor || 'Sensor';
            fila.insertCell().append(crear('strong',numero(a.valor_detectado) + ' ' + unidad(a)));
            const condicion = fila.insertCell(); condicion.textContent = condicionRegistrada(a) || 'Consultar mensaje';
            condicion.title = 'Condición conservada en el mensaje original de la alerta.';
            fila.insertCell().append(insignia('prioridad',a.prioridad));
            fila.insertCell().append(insignia('estado',a.estado));
            const acciones = fila.insertCell(); acciones.className = 'hh-acciones';
            acciones.append(boton('Ver detalle',() => detalle(a.idHistorial)));
            if (!ejemplo && a.estado === 'nueva') {
                const vista = boton('Marcar vista',() => cambiarEstado(a.idHistorial,'vista'),'btn-sm btn-naranja');
                vista.dataset.hhCambio = '1'; vista.disabled = mutando; acciones.append(vista);
            }
        }
        envoltura.append(tabla); destino.append(envoltura);
        if (hayMas && !ejemplo) {
            const mas = boton('Mostrar más',() => { if (!consultando) { limite += 200; cargar({mas:true}); } });
            mas.id = 'hh-mas'; destino.append(mas);
        }
    }
    function montar() {
        if (montado) return true;
        const destino = $('tabla-historial-alertas'); if (!destino) return false;
        const estilo = crear('style'); estilo.textContent = `
#tab-historial .hh-cabecera{padding:18px 20px;background:#fff;border:1px solid #cfe2d7;border-radius:12px;margin-bottom:16px}
#tab-historial .hh-cabecera h3{margin:0 0 8px;color:#1a3d2b;font-size:18px}
#tab-historial .hh-nota,#tab-historial .hh-cabecera p{font-size:14px;line-height:1.5;color:#52675b;margin:8px 0}
#tab-historial .hh-barra{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
#tab-historial .hh-error{padding:12px 14px;color:#963623;background:#fff2ed;border-radius:8px}
#tab-historial #hh-demo{padding:14px;background:#fff2ca;color:#674c10;border:1px solid #e3c46d;border-radius:9px;margin:12px 0;font-weight:600;line-height:1.5}
#tab-historial #hh-resumen{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:16px 0 10px}
#tab-historial .hh-contador{padding:14px 16px;border:1px solid #cfe2d7;border-radius:10px;background:#fff}
#tab-historial .hh-contador strong{display:block;font-size:25px;color:#1a3d2b}
#tab-historial .hh-contador span{display:block;color:#607165;font-size:13px;margin-top:4px}
#tab-historial .hh-vacio{padding:28px;background:#fff;border:1px solid #cfe2d7;border-radius:12px;line-height:1.6}
#tab-historial .hh-vacio p{color:#627368;max-width:740px}
#tab-historial .hh-tabla{overflow-x:auto;border:1px solid #cfe2d7;border-radius:10px;background:#fff}
#tab-historial .hh-tabla table{width:100%;margin:0;border-collapse:collapse;min-width:850px}
#tab-historial .hh-tabla th,#tab-historial .hh-tabla td{padding:13px 12px;text-align:left;vertical-align:top}
#tab-historial .hh-tabla th{background:#1a3d2b;color:#fff}
#tab-historial .hh-tabla td{border-bottom:1px solid #edf2ee;font-size:14px}
#tab-historial .hh-tabla small{display:block;color:#6b7c70;margin-top:5px}
#tab-historial .hh-tabla .hh-acciones button{display:block;margin:0 0 7px;white-space:nowrap}
.hh-badge{display:inline-block;padding:4px 8px;border-radius:6px;background:#edf2ee;color:#334e3e;font-size:12px;white-space:nowrap}
.hh-badge.hh-nueva,.hh-badge.hh-critica{background:#fde6e4;color:#982d25}
.hh-badge.hh-vista,.hh-badge.hh-media{background:#fff0c5;color:#705315}
.hh-badge.hh-resuelta,.hh-badge.hh-baja{background:#e2f2e8;color:#256044}
.hh-badge.hh-alta{background:#ffebd8;color:#8b491d}
#tab-historial #hh-mas{margin-top:14px}
#tab-historial .hh-ejemplo .hh-tabla{border:2px solid #d7b45c}
#tab-historial button:disabled{opacity:.6;cursor:wait}
@media(max-width:600px){#tab-historial #hh-resumen{grid-template-columns:repeat(2,minmax(0,1fr))}}
`;
        document.head.append(estilo);
        const encabezado = crear('section',undefined,'hh-cabecera'); encabezado.id = 'hh-intro';
        encabezado.append(crear('h3','Seguimiento de alertas'),crear('p','Consulta qué lectura disparó cada alerta y cuándo se registró. El estado “Vista” indica que alguien la revisó; “Resuelta” indica que el registro se cerró.'));
        const barra = crear('div',undefined,'hh-barra');
        const demo = boton('Ver ejemplo',alternarEjemplo); demo.id = 'hh-ejemplo';
        const limpiar = boton('Quitar filtros',quitarFiltros); limpiar.id = 'hh-limpiar';
        barra.append(demo,limpiar); encabezado.append(barra);
        $('tab-historial').prepend(encabezado);
        const aviso = crear('div','VISTA DE EJEMPLO · Datos simulados para revisar la pantalla. No son mediciones del Arduino y no se guardan.'); aviso.id = 'hh-demo'; aviso.hidden = true;
        const contadores = crear('div'); contadores.id = 'hh-resumen';
        const alcance = crear('p',undefined,'hh-nota'); alcance.id = 'hh-alcance';
        const estado = crear('p',undefined,'hh-nota'); estado.id = 'hh-mensaje'; estado.setAttribute('role','status'); estado.setAttribute('aria-live','polite');
        destino.before(aviso,contadores,alcance,estado);
        const nota = crear('p','Se actualiza cada 15 segundos mientras este apartado está visible. Los estados corresponden al historial; revisa las últimas lecturas en Parámetros / Umbrales.', 'hh-nota');
        destino.after(nota);
        filtros.forEach((id,i) => $(id)?.setAttribute('aria-label',['Filtrar por prioridad','Filtrar por estado','Filtrar por sensor'][i]));
        montado = true;
        setInterval(() => {
            if (!ejemplo && !consultando && !mutando && visible()) return cargar({silencioso:true});
        },15000);
        return true;
    }
    function visible() {
        const seccion = $('seccion-alertas'), tab = $('tab-historial');
        return seccion && tab && !document.hidden && seccion.style.display !== 'none' &&
            getComputedStyle(seccion).display !== 'none' && tab.classList.contains('activo');
    }
    async function cargar({silencioso = false, mas = false} = {}) {
        if (!montar()) return false;
        const turno = ++revision, clave = firma();
        const [prioridad,estado,idSensor] = seleccion();
        if (clave !== claveMostrada && !mas) limite = 200;
        if (ejemplo) {
            consultando = false;
            $('tabla-historial-alertas').setAttribute('aria-busy','false');
            const lista = ejemplos.filter(a => (!prioridad || a.prioridad === prioridad) && (!estado || a.estado === estado) && (!idSensor || String(a.idSensor) === idSensor));
            dibujar(lista); mensaje('Ejemplo local: puedes revisar los filtros y el detalle sin conectar el prototipo.'); return true;
        }
        consultando = true; $('tabla-historial-alertas').setAttribute('aria-busy','true');
        if (!silencioso) mensaje('Consultando historial…');
        if (clave !== claveMostrada) {
            registros.clear(); $('tabla-historial-alertas').replaceChildren(); $('hh-resumen').replaceChildren(); $('hh-alcance').textContent = '';
        }
        actualizarModo();
        const qs = new URLSearchParams({limite:String(limite + 1)});
        if (prioridad) qs.set('prioridad',prioridad);
        if (estado) qs.set('estado',estado);
        if (idSensor) qs.set('idSensor',idSensor);
        try {
            const datos = await pedir('/api/alertas/historial?' + qs);
            if (turno !== revision) return false;
            if (!Array.isArray(datos)) throw new Error('El servidor devolvió un historial no válido.');
            dibujar(datos.slice(0,limite),datos.length > limite);
            ultimaConsulta = new Date().toLocaleTimeString('es-MX');
            mensaje('Última consulta al servidor: ' + ultimaConsulta + '.'); return true;
        } catch (error) {
            if (turno !== revision) return false;
            mensaje(error.message + (claveMostrada === clave && ultimaConsulta ? ' Se conserva la consulta anterior de las ' + ultimaConsulta + '.' : ''),true);
            return false;
        } finally {
            if (turno === revision) { consultando = false; $('tabla-historial-alertas').setAttribute('aria-busy','false'); }
        }
    }
    function filtrar() { limite = 200; return cargar(); }
    function quitarFiltros() { filtros.forEach(id => { if ($(id)) $(id).value = ''; }); return filtrar(); }
    async function cargarSensores() {
        if (sensoresPendientes) return sensoresPendientes;
        const select = $('filtro-sensor-historial'); if (!select) return;
        sensoresPendientes = (async () => {
            try {
                const datos = await pedir('/api/sensores/lista');
                if (!Array.isArray(datos)) throw new Error('Lista de sensores no válida.');
                sensores = datos;
                const anterior = select.value;
                select.replaceChildren(new Option('Todos los sensores',''));
                for (const s of sensores) select.add(new Option(`${nombres[s.tipo_sensor] || s.tipo_sensor} · ID ${s.idSensore}${s.nombre_dispositivo ? ' · ' + s.nombre_dispositivo : ''}`,String(s.idSensore)));
                if ([...select.options].some(o => o.value === anterior)) select.value = anterior;
                else if (anterior) await filtrar();
                select.title = '';
            } catch (_) { select.title = 'No se pudo actualizar la lista de sensores. Las opciones anteriores se conservan.'; }
            finally { sensoresPendientes = null; }
        })();
        return sensoresPendientes;
    }
    function crearEjemplos() {
        const ahora = Date.now(), instante = minutos => {
            const d = new Date(ahora - minutos * 60000), pad = n => String(n).padStart(2,'0');
            return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
        };
        const datos = [
            {tipo_sensor:'temperatura',unidad_medida:'°C',valor_detectado:31.5,condicion:'mayor_que',valor_umbral:30,estado:'nueva',nombre_parametro:'Ejemplo: temperatura alta'},
            {tipo_sensor:'humedad',unidad_medida:'%',valor_detectado:38,condicion:'menor_que',valor_umbral:40,estado:'vista',nombre_parametro:'Ejemplo: humedad baja'},
            {tipo_sensor:'temperatura',unidad_medida:'°C',valor_detectado:9,condicion:'menor_que',valor_umbral:10,estado:'resuelta',nombre_parametro:'Ejemplo: temperatura baja'}
        ];
        return datos.map((a,i) => {
            const idSensor = sensores.find(s => s.tipo_sensor === a.tipo_sensor)?.idSensore || 0;
            return {...a,idHistorial:'E' + (i+1),idSensor,prioridad:'media',fecha_hora:instante((i+1)*20),fecha_resolucion:a.estado === 'resuelta' ? instante(10) : null,
                mensaje:`Sensor ${idSensor}: valor ${a.valor_detectado} ${a.condicion.replaceAll('_',' ')} umbral ${a.valor_umbral}`};
        });
    }
    function alternarEjemplo() {
        if (mutando) return;
        ejemplo = !ejemplo; limite = 200; detalleActual = null;
        $('modal-detalle-alerta')?.classList.remove('activo');
        if (ejemplo) ejemplos = crearEjemplos();
        return cargar();
    }
    function detalle(idHistorial) {
        if (!montar()) return;
        const a = registros.get(String(idHistorial));
        if (!a) { mensaje('La alerta ya no está en esta consulta. Actualiza el historial.',true); return; }
        detalleActual = a;
        const cont = $('contenido-detalle-alerta'); cont.replaceChildren();
        const agregar = (titulo,valor) => { const p = crear('p'); p.append(crear('strong',titulo + ': '),document.createTextNode(valor || '—')); cont.append(p); };
        if (ejemplo) agregar('Vista de ejemplo','Datos simulados, sin registros nuevos en el sistema.');
        agregar('Registro',ejemplo ? 'Ejemplo ' + a.idHistorial : '#' + a.idHistorial);
        agregar('Sensor',nombres[a.tipo_sensor] || a.tipo_sensor);
        agregar('Valor registrado',numero(a.valor_detectado) + ' ' + unidad(a));
        agregar('Condición registrada',condicionRegistrada(a) || 'No se puede recuperar del mensaje original.');
        agregar('Prioridad',prioridades[a.prioridad]); agregar('Estado',estados[a.estado]);
        agregar('Fecha y hora',fecha(a.fecha_hora));
        if (a.fecha_resolucion) agregar('Fecha de cierre',fecha(a.fecha_resolucion));
        agregar('Mensaje guardado',a.mensaje);
        const actual = crear('details'); actual.append(crear('summary','Consultar la regla actual'));
        actual.append(crear('p',(a.nombre_parametro || 'Sin nombre') + ' · ' + labelCondicion(a.condicion) + ' ' + numero(a.valor_umbral) + ' ' + unidad(a)));
        actual.append(crear('p','La regla puede haberse modificado después de esta alerta. El mensaje y el valor registrado conservan la información del evento.'));
        cont.append(actual);
        const btnVista = $('btn-marcar-vista'), btnCerrar = $('btn-marcar-resuelta');
        btnVista.style.display = !ejemplo && a.estado === 'nueva' ? 'inline-block' : 'none';
        btnCerrar.style.display = !ejemplo && a.estado !== 'resuelta' ? 'inline-block' : 'none';
        btnVista.disabled = btnCerrar.disabled = mutando;
        btnVista.textContent = 'Marcar como vista'; btnCerrar.textContent = 'Cerrar registro';
        btnVista.onclick = () => cambiarEstado(a.idHistorial,'vista',true);
        btnCerrar.onclick = () => cambiarEstado(a.idHistorial,'resuelta',true);
        if (!ejemplo && a.estado !== 'resuelta') cont.append(crear('p','Cerrar el registro manualmente no confirma que el sensor haya vuelto al rango. Si otra lectura vuelve a cumplir la regla, se generará una nueva alerta.'));
        const estadoModal = crear('p'); estadoModal.id = 'hh-mensaje-detalle'; estadoModal.setAttribute('role','status'); cont.append(estadoModal);
        $('modal-detalle-alerta').classList.add('activo');
    }
    async function cambiarEstado(idHistorial, estado, desdeModal = false) {
        if (!montar() || ejemplo || mutando || !['vista','resuelta'].includes(estado)) return false;
        const a = registros.get(String(idHistorial)) || (String(detalleActual?.idHistorial) === String(idHistorial) ? detalleActual : null);
        if (!a || !Number.isInteger(Number(idHistorial)) || Number(idHistorial) <= 0 || a.estado === 'resuelta' || (estado === 'vista' && a.estado !== 'nueva')) return false;
        mutando = true;
        const botones = [...document.querySelectorAll('[data-hh-cambio],#btn-marcar-vista,#btn-marcar-resuelta,#hh-ejemplo')];
        botones.forEach(b => { b.disabled = true; });
        let guardado = false;
        try {
            await pedir('/api/alertas/historial/estado/' + Number(idHistorial),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({estado})});
            guardado = true; a.estado = estado;
            if (desdeModal) $('modal-detalle-alerta').classList.remove('activo');
            const cargado = await cargar();
            await actualizarBadgeNav();
            if (cargado) mensaje(estado === 'vista' ? 'Alerta marcada como vista.' : 'Registro cerrado. Consulta las últimas lecturas para comprobar el estado del sensor.');
            else mensaje('El cambio se guardó, pero no se pudo actualizar el historial. Pulsa Actualizar.',true);
            return true;
        } catch (error) {
            const texto = (guardado ? 'El cambio se guardó. ' : '') + error.message;
            mensaje(texto,true);
            if (desdeModal && $('hh-mensaje-detalle')) $('hh-mensaje-detalle').textContent = texto;
            return false;
        } finally { mutando = false; botones.forEach(b => { b.disabled = false; }); document.querySelectorAll('[data-hh-cambio]').forEach(b => { b.disabled = false; }); }
    }
    return {cargar,cargarSensores,filtrar,detalle,cambiarEstado};
})();

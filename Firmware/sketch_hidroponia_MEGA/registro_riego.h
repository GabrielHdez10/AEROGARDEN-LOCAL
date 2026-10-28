#ifndef HYDROSENSE_REGISTRO_RIEGO_H
#define HYDROSENSE_REGISTRO_RIEGO_H

#include <stdint.h>
#include <string.h>

// HYDROSENSE - Actividad 2: registro local de la salida del relevador.
// Guarda avisos en RAM. No escribe pines, no usa la red ni guarda en MySQL.
// El .ino debe llamar iniciar(), configurar() y salida(); el transporte debe
// enviar pendiente() y llamar confirmar() solo tras un ACK del servidor.
// La secuencia es local a este arranque: al enviar, debe acompanarse de un
// identificador de dispositivo Y de arranque para distinguir reinicios.
// La cola es finita: una caida de energia pierde los avisos sin confirmar.
// Un cambio de salida informado no confirma caudal, volumen ni contactos.
class RegistroRiego {
public:
    enum Tipo : uint8_t {
        INICIO, CONFIG_APLICADA, RELEVADOR_ON, RELEVADOR_OFF,
        BLOQUEO_NIVEL, RECUPERACION_NIVEL
    };
    enum Motivo : uint8_t {
        ARRANQUE, CONFIGURACION, MANUAL, CICLO, CALIBRACION, ESPERA,
        NIVEL_SIN_LECTURA, NIVEL_BAJO, NIVEL_INVALIDO, NIVEL_VENCIDO, NIVEL_RECUPERADO
    };
    struct Evento {
        // uptime_ms es millis(), no una fecha. Se repite cada 49.7 dias.
        uint32_t secuencia, uptime_ms, duracion_anterior_ms, perdidos_total;
        uint16_t tiempo_on, tiempo_off;
        uint8_t version[32];
        Tipo tipo;
        Motivo motivo;
        bool encendido, manual, orden_manual, tiene_config, tiene_version;
    };
    static const uint8_t CAPACIDAD = 8;

    // Llamar en setup(), despues de escribir RELAY_OFF en el pin.
    // Una segunda llamada no borra avisos pendientes ni reinicia la secuencia.
    void iniciar(uint32_t ahora) {
        if (iniciado_) return;
        iniciado_ = true;
        encendido_ = false;
        ultimoCambio_ = ahora;
        agregar(INICIO, ARRANQUE, ahora, 0);
    }

    // Se llama solo DESPUES de validar y aceptar la configuracion de app.py.
    // on/off son segundos; version es SHA-256 hexadecimal en minusculas.
    // Si el servidor aun no envia version, pasar nullptr: tiene_version=false.
    void configurar(uint16_t on, uint16_t off, bool manual, bool orden,
                    const char *version, uint32_t ahora) {
        if (!iniciado_) return;
        uint8_t nueva[32] = {};
        bool valida = decodificarVersion(version, nueva);
        bool cambio = !tieneConfig_ || on != on_ || off != off_ ||
                      manual != manual_ || orden != orden_ || valida != tieneVersion_ ||
                      (valida && memcmp(nueva, version_, sizeof(nueva)) != 0);
        on_ = on; off_ = off; manual_ = manual; orden_ = orden;
        tieneConfig_ = true; tieneVersion_ = valida;
        memcpy(version_, nueva, sizeof(version_));
        if (cambio) agregar(CONFIG_APLICADA, CONFIGURACION, ahora, 0);
    }

    // Se llama tras digitalWrite(). Una escritura repetida no genera otra fila.
    void salida(bool encendido, Motivo motivo, uint32_t ahora) {
        if (!iniciado_ || encendido == encendido_) return;
        uint32_t duracion = ahora - ultimoCambio_; // resta modular de millis()
        encendido_ = encendido;
        ultimoCambio_ = ahora;
        agregar(encendido ? RELEVADOR_ON : RELEVADOR_OFF, motivo, ahora, duracion);
    }

    // Llamar con la salida ya apagada al bloquear, y ANTES de reanudar
    // el control al recuperar. Registra tambien bloqueos con bomba ya OFF.
    // No modifica la salida ni reinicia la duracion de su estado registrado.
    void proteccionNivel(bool bloqueado, Motivo motivo, uint32_t ahora) {
        if (!iniciado_) return;
        bool cambio = !nivelConocido_ || bloqueado != nivelBloqueado_ ||
                      (bloqueado && motivo != motivoNivel_);
        nivelConocido_ = true; nivelBloqueado_ = bloqueado; motivoNivel_ = motivo;
        if (cambio) agregar(bloqueado ? BLOQUEO_NIVEL : RECUPERACION_NIVEL,
                            motivo, ahora, 0);
    }

    // Devuelve el mas antiguo; sigue pendiente mientras no se confirme.
    const Evento *pendiente() const { return cantidad_ ? &cola_[inicio_] : 0; }
    bool confirmar(uint32_t secuencia) {
        if (!cantidad_ || cola_[inicio_].secuencia != secuencia) return false;
        inicio_ = (inicio_ + 1) % CAPACIDAD;
        --cantidad_;
        return true;
    }
    uint8_t cantidad() const { return cantidad_; }
    uint32_t perdidos() const { return perdidos_; }

    // La duracion es del estado ANTERIOR, medida entre cambios de salida.
    // Es valida si ese intervalo es menor que una vuelta completa de millis().
    // Arranque, configuracion y avisos de nivel llevan null, no una duracion.
    static bool tieneDuracion(const Evento &evento) {
        return evento.tipo == RELEVADOR_ON || evento.tipo == RELEVADOR_OFF;
    }

    static const char *tipoTexto(Tipo tipo) {
        switch (tipo) {
            case INICIO: return "inicio";
            case CONFIG_APLICADA: return "config_aplicada";
            case RELEVADOR_ON: return "relevador_on";
            case BLOQUEO_NIVEL: return "bloqueo_nivel";
            case RECUPERACION_NIVEL: return "recuperacion_nivel";
            default: return "relevador_off";
        }
    }
    static const char *motivoTexto(Motivo motivo) {
        switch (motivo) {
            case ARRANQUE: return "arranque";
            case CONFIGURACION: return "configuracion";
            case MANUAL: return "manual";
            case CICLO: return "ciclo_automatico";
            case CALIBRACION: return "calibracion_ec";
            case NIVEL_SIN_LECTURA: return "nivel_sin_lectura";
            case NIVEL_BAJO: return "nivel_bajo";
            case NIVEL_INVALIDO: return "nivel_invalido";
            case NIVEL_VENCIDO: return "nivel_vencido";
            case NIVEL_RECUPERADO: return "nivel_recuperado";
            default: return "sin_configuracion";
        }
    }
    static void versionTexto(const Evento &evento, char texto[65]) {
        if (!evento.tiene_version) { texto[0] = '\0'; return; }
        const char *hex = "0123456789abcdef";
        for (uint8_t i = 0; i < 32; ++i) {
            texto[i * 2] = hex[evento.version[i] >> 4];
            texto[i * 2 + 1] = hex[evento.version[i] & 15];
        }
        texto[64] = '\0';
    }

private:
    Evento cola_[CAPACIDAD] = {};
    uint8_t inicio_ = 0, cantidad_ = 0;
    uint32_t secuencia_ = 0, perdidos_ = 0, ultimoCambio_ = 0;
    uint16_t on_ = 0, off_ = 0;
    uint8_t version_[32] = {};
    bool encendido_ = false, manual_ = false, orden_ = false;
    bool tieneConfig_ = false, tieneVersion_ = false, iniciado_ = false;
    bool nivelConocido_ = false, nivelBloqueado_ = false;
    Motivo motivoNivel_ = NIVEL_SIN_LECTURA;

    static int nibble(char c) {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        return -1;
    }
    static bool decodificarVersion(const char *texto, uint8_t *bytes) {
        if (!texto || strlen(texto) != 64) return false;
        for (uint8_t i = 0; i < 32; ++i) {
            int a = nibble(texto[i * 2]), b = nibble(texto[i * 2 + 1]);
            if (a < 0 || b < 0) return false;
            bytes[i] = (a << 4) | b;
        }
        return true;
    }
    void agregar(Tipo tipo, Motivo motivo, uint32_t ahora, uint32_t duracion) {
        // Incluso al llenarse, la secuencia avanza para que sea visible el hueco.
        if (secuencia_ == UINT32_MAX) {
            if (perdidos_ != UINT32_MAX) ++perdidos_;
            return;
        }
        ++secuencia_;
        if (cantidad_ == CAPACIDAD) {
            if (perdidos_ != UINT32_MAX) ++perdidos_;
            return;
        }
        Evento &e = cola_[(inicio_ + cantidad_) % CAPACIDAD];
        e.secuencia = secuencia_; e.uptime_ms = ahora;
        e.duracion_anterior_ms = duracion; e.perdidos_total = perdidos_;
        e.tiempo_on = on_; e.tiempo_off = off_;
        memcpy(e.version, version_, sizeof(version_));
        e.tipo = tipo; e.motivo = motivo; e.encendido = encendido_;
        e.manual = manual_; e.orden_manual = orden_;
        e.tiene_config = tieneConfig_; e.tiene_version = tieneVersion_;
        ++cantidad_;
    }
};

#endif

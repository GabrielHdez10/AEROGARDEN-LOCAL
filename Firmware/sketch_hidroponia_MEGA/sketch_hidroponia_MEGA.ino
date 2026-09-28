// ============================================================
// VERSIÓN 28/09/2026
//  - ARRANQUE: se elimina el delay fijo de 6 s. Ahora el Mega hace
//    PING al ESP8266 y continúa en cuanto éste confirma WiFi
//    conectado (máximo ESPERA_ESP_MAX). Requiere el sketch del
//    ESP8266 actualizado (responde a "PING").
//  - La primera lectura/envío ocurre de inmediato al terminar el
//    arranque (antes esperaba hasta llegar a 10 s de uptime).
// VERSIÓN 25/09/2026
//  - Relevador en estado seguro ANTES de configurar el pin
//    (evita el pulso de encendido de la bomba al arrancar).
//  - pedirConfig(): si la respuesta no trae la config del relay
//    (error de red, 404, etc.), se conserva la configuración
//    anterior en vez de caer a modo automático.
// ============================================================

#include <DHT.h>
#include <EEPROM.h>
#include "config.h"
#include "secrets.h"
// ─────────────────────────────────────────────────────────────

DHT dht(DHTPIN, DHTTYPE);

int deviceId = -1;

// ── Config relay (se actualiza desde el servidor) ─────────────
int  tiempoOn      = RIEGO_ON_DEFAULT;
int  tiempoOff     = RIEGO_OFF_DEFAULT;
bool modoManual    = false;
bool estadoManual  = false;
bool bombaEncendida = false;
unsigned long tCambioRelay = 0;
// ─────────────────────────────────────────────────────────────

unsigned long ultimoCiclo = 0;

// ================================================================
// EEPROM
// ================================================================
int leerDeviceIdEEPROM() {
    if (EEPROM.read(EEPROM_MAGIC_ADDR) != EEPROM_MAGIC) return -1;
    int id;
    EEPROM.get(EEPROM_ADDR, id);
    return id;
}

void guardarDeviceIdEEPROM(int id) {
    EEPROM.put(EEPROM_ADDR, id);
    EEPROM.write(EEPROM_MAGIC_ADDR, EEPROM_MAGIC);
    Serial.println(F("device_id guardado en EEPROM"));
}

// ================================================================
// COMUNICACIÓN CON ESP32
// El Mega envía comandos por Serial1, el ESP32 responde con JSON
// ================================================================

String enviarAlESP32(String cmd, int timeoutMs = TIMEOUT_ESP) {
    // Limpiar buffer
    while (Serial1.available()) Serial1.read();

    Serial1.println(cmd);
    Serial.print(F("[->ESP32] ")); Serial.println(cmd);

    String resp = "";
    unsigned long t = millis();
    while (millis() - t < (unsigned long)timeoutMs) {
        while (Serial1.available()) {
            char c = Serial1.read();
            resp += c;
            if (c == '\n') goto done;
        }
    }
    done:
    resp.trim();
    Serial.print(F("[ESP32->] ")); Serial.println(resp);
    return resp;
}

// ================================================================
// ESPERAR AL ESP8266 (reemplaza el delay fijo de arranque)
// El ESP responde a "PING" con {"pong":true,"wifi":true|false}.
// Devuelve true en cuanto el ESP reporta WiFi conectado.
// ================================================================
bool esperarESP(unsigned long maxMs) {
    unsigned long t0 = millis();
    while (millis() - t0 < maxMs) {
        while (Serial1.available()) Serial1.read();
        Serial1.println(F("PING"));

        String resp = "";
        unsigned long t = millis();
        while (millis() - t < PING_ESP_INTERVALO) {
            while (Serial1.available()) {
                char c = Serial1.read();
                resp += c;
                if (c == '\n' && resp.indexOf("\"pong\"") != -1) goto respondio;
            }
        }
        respondio:
        if (resp.indexOf("\"pong\"") != -1 && resp.indexOf("\"wifi\":true") != -1) {
            return true;
        }
    }
    return false;
}

// ================================================================
// EMPAREJAMIENTO
// ================================================================
int emparejar() {
    Serial.println(F("Emparejando con el servidor..."));

    String body = "{\"token\":\"" + String(ARDUINO_TOKEN) + "\","
                  "\"pairing_code\":\"" + String(PAIRING_CODE) + "\"}";

    String cmd = "POST:/api/arduino/emparejar:" + body;
    String resp = enviarAlESP32(cmd, 10000);

    // Parsear device_id de la respuesta JSON
    int idx = resp.indexOf("\"device_id\":");
    if (idx == -1) return -1;
    int ini = resp.indexOf(":", idx) + 1;
    int fin = resp.indexOf("}", ini);
    String idStr = resp.substring(ini, fin);
    idStr.trim();
    int id = idStr.toInt();
    return (id > 0) ? id : -1;
}

// ================================================================
// ENVÍO DE DATOS
// ================================================================
void enviarDatos(float temp, float humedad, float dist) {

    int luz = 0;
    for (int i = 0; i < MUESTRAS_ANALOG; i++) { luz += analogRead(LUZ_PIN); delay(DELAY_MUESTRA); }
    luz = luz / MUESTRAS_ANALOG;

    int ph = 0;
    for (int i = 0; i < MUESTRAS_ANALOG; i++) { ph += analogRead(PH_PIN); delay(DELAY_MUESTRA); }
    ph = ph / MUESTRAS_ANALOG;

    String body = "{\"token\":\"" + String(ARDUINO_TOKEN) + "\","
                  "\"device_id\":" + String(deviceId) + ","
                  "\"sensores\":{"
                  "\"temperatura\":" + String(temp, 1) + ","
                  "\"humedad\":"     + String(humedad, 1) + ","
                  "\"distancia\":"   + String(dist, 1) + ","
                  "\"luz\":"         + String(luz) + ","
                  "\"ph\":"          + String(ph) +
                  "}}";

    String cmd = "POST:/api/arduino/datos:" + body;
    String r = enviarAlESP32(cmd, 10000);
    Serial.print(F("Resp datos: ")); Serial.println(r);
}

// ================================================================
// PEDIR CONFIG DEL RELAY
// ================================================================
void pedirConfig() {
    String url = "/api/arduino/config?token=" + String(ARDUINO_TOKEN)
                 + "&device_id=" + String(deviceId);
    String cmd = "GET:" + url;
    String r = enviarAlESP32(cmd, TIMEOUT_ESP);
    Serial.print(F("Config: ")); Serial.println(r);

    if (r.length() == 0) return;

    // Si la respuesta no trae la config del relay (error de red,
    // 404, "No autorizado", etc.), conservar la configuración
    // anterior. Antes, cualquier error dejaba modoManual en false
    // y la bomba pasaba sola a modo automático.
    if (r.indexOf("\"relay\"") == -1) {
        Serial.println(F("Config no valida: se conserva la anterior"));
        return;
    }

    // Parsear tiempo_on
    int idx = r.indexOf("\"tiempo_on\":");
    if (idx != -1) {
        int ini = idx + 12;
        int fin = r.indexOf(",", ini);
        if (fin == -1) fin = r.indexOf("}", ini);
        tiempoOn = r.substring(ini, fin).toInt();
    }

    // Parsear tiempo_off
    idx = r.indexOf("\"tiempo_off\":");
    if (idx != -1) {
        int ini = idx + 13;
        int fin = r.indexOf(",", ini);
        if (fin == -1) fin = r.indexOf("}", ini);
        tiempoOff = r.substring(ini, fin).toInt();
    }

    // Parsear modo
    modoManual = (r.indexOf("\"modo\":\"manual\"") != -1 ||
                  r.indexOf("\"modo\": \"manual\"") != -1);

    // Parsear estado_manual
    idx = r.indexOf("\"estado_manual\":\"");
    if (idx != -1) estadoManual = r.indexOf("\"encendido\"", idx) != -1;
}

// ================================================================
// CONTROL RELAY
// ================================================================
void controlarRelay() {
    if (modoManual) {
        digitalWrite(RELAY_PIN, estadoManual ? RELAY_ON : RELAY_OFF);
        bombaEncendida = estadoManual;
        return;
    }
    unsigned long ahora = millis();
    if (bombaEncendida && ahora - tCambioRelay >= (unsigned long)tiempoOn * 1000UL) {
        bombaEncendida = false;
        digitalWrite(RELAY_PIN, RELAY_OFF);
        tCambioRelay = ahora;
        Serial.println(F("Bomba OFF (auto)"));
    } else if (!bombaEncendida && ahora - tCambioRelay >= (unsigned long)tiempoOff * 1000UL) {
        bombaEncendida = true;
        digitalWrite(RELAY_PIN, RELAY_ON);
        tCambioRelay = ahora;
        Serial.println(F("Bomba ON (auto)"));
    }
}

// ================================================================
// SENSOR ULTRASÓNICO
// ================================================================
float leerUltrasonico() {
    digitalWrite(TRIG_PIN, LOW);  delayMicroseconds(2);
    digitalWrite(TRIG_PIN, HIGH); delayMicroseconds(10);
    digitalWrite(TRIG_PIN, LOW);
    return pulseIn(ECHO_PIN, HIGH, 30000) * 0.034 / 2.0;
}

// ================================================================
// SETUP
// ================================================================
void setup() {
    // Relevador en estado seguro ANTES de configurar el pin.
    // Al hacer pinMode(OUTPUT), el pin toma el valor que ya tenga
    // escrito; con HIGH primero, la bomba nunca recibe un pulso
    // de encendido al arrancar (el módulo es activo en bajo).
    digitalWrite(RELAY_PIN, RELAY_OFF);
    pinMode(RELAY_PIN, OUTPUT);

    Serial.begin(BAUD_DEBUG);
    Serial1.begin(BAUD_ESP);   // comunicación con ESP32

    dht.begin();
    pinMode(TRIG_PIN, OUTPUT);
    pinMode(ECHO_PIN, INPUT);

    Serial.println(F("=== Iniciando sistema ==="));
    Serial.println(F("Esperando que el ESP8266 conecte al WiFi..."));

    // Espera activa: sale en cuanto el ESP confirma WiFi (o al agotar el máximo)
    unsigned long tEsp = millis();
    if (esperarESP(ESPERA_ESP_MAX)) {
        Serial.print(F("ESP listo en ")); Serial.print(millis() - tEsp); Serial.println(F(" ms"));
    } else {
        Serial.println(F("ESP sin respuesta/WiFi tras la espera maxima; se continua (reintentara solo)"));
    }

    // El DHT22 necesita ~2 s desde el encendido para dar una lectura valida
    if (millis() < ESPERA_MIN_DHT) delay(ESPERA_MIN_DHT - millis());

    // Intentar leer device_id guardado en EEPROM
    deviceId = leerDeviceIdEEPROM();

    if (deviceId > 0) {
        Serial.print(F("device_id en EEPROM: ")); Serial.println(deviceId);
    } else {
        deviceId = emparejar();
        if (deviceId > 0) {
            guardarDeviceIdEEPROM(deviceId);
            Serial.print(F("Emparejado OK. device_id=")); Serial.println(deviceId);
        } else {
            Serial.println(F("ERROR: no se pudo emparejar. Verifica el PAIRING_CODE."));
        }
    }

    // Que el primer envio ocurra ya, sin esperar los 10 s del intervalo
    ultimoCiclo = millis() - INTERVALO_DATOS;
}

// ================================================================
// LOOP
// ================================================================
void loop() {
    if (deviceId <= 0) {
        Serial.println(F("Sin device_id. Reintentando en 30s..."));
        delay(30000);
        deviceId = emparejar();
        if (deviceId > 0) guardarDeviceIdEEPROM(deviceId);
        return;
    }

    unsigned long ahora = millis();
    if (ahora - ultimoCiclo >= INTERVALO_DATOS) {
        ultimoCiclo = ahora;

        float temp    = dht.readTemperature();
        float humedad = dht.readHumidity();
        float dist    = leerUltrasonico();

        if (!isnan(temp) && !isnan(humedad)) {
            enviarDatos(temp, humedad, dist);
        } else {
            Serial.println(F("Error leyendo DHT22"));
        }
        pedirConfig();
    }

    controlarRelay();
}
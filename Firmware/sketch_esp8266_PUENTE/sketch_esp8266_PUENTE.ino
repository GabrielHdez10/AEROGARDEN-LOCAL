// CORRECCION 13/11/2026: respuestas identificadas para el Mega actualizado.
#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClient.h>

// AeroGarden: puente Wi-Fi entre el Arduino Mega y el servidor.
const char* WIFI_SSID = "linksys_SUIP";
const char* WIFI_PASS = "e0h6i2e5Suip";

// Dirección del servidor AeroGarden.
const char* SERVER_HOST = "http://172.195.106.4";

unsigned long tUltimoIntento = 0;
const unsigned long REINTENTO_WIFI_MS = 10000UL;

const uint16_t HTTP_TIMEOUT_MS = 3000;
const unsigned int MAX_RESPUESTA_BYTES = 1400;

// Compatible con comandos anteriores y con @<id>:<comando>.
// El Mega actualizado verificara este identificador en cada respuesta.
bool leerCabeceraESP(const char *texto, uint32_t &id, uint8_t &longitud) {
    if (!texto || texto[0] != '@') return false;
    uint32_t valor = 0;
    uint8_t i = 1;
    while (texto[i] >= '0' && texto[i] <= '9') {
        uint8_t digito = (uint8_t)(texto[i] - '0');
        if (i > 10 || valor > (UINT32_MAX - digito) / 10U) return false;
        valor = valor * 10U + digito;
        ++i;
    }
    if (i == 1 || texto[i] != ':' || valor == 0) return false;
    id = valor;
    longitud = i + 1;
    return true;
}

String procesarComando(const String &cmd);

void setup() {
    // Enlace con el Mega: debe coincidir con BAUD_ESP.
    Serial.begin(115200);
    delay(200);

    // Este puerto es el protocolo con el Mega: sin mensajes libres de arranque.

    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    tUltimoIntento = millis();
}

void loop() {
    // Reintentar la conexión cada 10 segundos.
    if (WiFi.status() != WL_CONNECTED) {
        if (millis() - tUltimoIntento >= REINTENTO_WIFI_MS) {
            WiFi.disconnect();
            WiFi.begin(WIFI_SSID, WIFI_PASS);
            tUltimoIntento = millis();
        }
    }

    // Serial es el enlace con el Mega: responder una línea por comando.
    // Evitar mensajes de depuración adicionales dentro del loop.
    if (Serial.available()) {
        String linea = Serial.readStringUntil('\n');
        linea.trim();
        if (linea.length() == 0) return;

        if (linea == "PING") {
            Serial.println(WiFi.status() == WL_CONNECTED
                           ? "{\"pong\":true,\"wifi\":true,\"rpc\":1}"
                           : "{\"pong\":true,\"wifi\":false,\"rpc\":1}");
            return;
        }

        // Mantiene compatibilidad con el Mega anterior durante la actualizacion.
        uint32_t solicitud = 0;
        uint8_t cabecera = 0;
        bool conId = linea.startsWith("@");
        if (conId) {
            if (!leerCabeceraESP(linea.c_str(), solicitud, cabecera)) {
                Serial.println("{\"error\":\"cabecera_rpc_invalida\"}");
                return;
            }
            linea.remove(0, cabecera);
        }
        String respuesta = procesarComando(linea);
        if (conId) {
            Serial.print('@'); Serial.print(solicitud); Serial.print(':');
        }
        Serial.println(respuesta);
    }
}

// Comandos del Mega:
// GET:/ruta
// POST:/ruta:{"campo":"valor"}
String procesarComando(const String &cmd) {
    if (WiFi.status() != WL_CONNECTED) {
        return "{\"error\":\"sin_wifi\"}";
    }

    String metodo = "";
    String endpoint = "";
    String body = "";

    int sep1 = cmd.indexOf(':');
    if (sep1 == -1) return "{\"error\":\"cmd_invalido\"}";

    metodo = cmd.substring(0, sep1);

    if (metodo == "POST") {
        int sep2 = cmd.indexOf(':', sep1 + 1);
        if (sep2 == -1) return "{\"error\":\"cmd_invalido\"}";
        endpoint = cmd.substring(sep1 + 1, sep2);
        body = cmd.substring(sep2 + 1);
    } else if (metodo == "GET") {
        endpoint = cmd.substring(sep1 + 1);
    } else {
        return "{\"error\":\"metodo_invalido\"}";
    }

    String url = String(SERVER_HOST) + endpoint;

    // El servidor configurado utiliza HTTP.
    WiFiClient cliente;
    HTTPClient http;

    if (!http.begin(cliente, url)) {
        return "{\"error\":\"http_begin\"}";
    }
    http.setTimeout(HTTP_TIMEOUT_MS);

    int httpCode = -1;
    String payload = "";

    if (metodo == "POST") {
        http.addHeader("Content-Type", "application/json");
        httpCode = http.POST(body);
    } else {
        httpCode = http.GET();
    }

    if (httpCode >= 200 && httpCode < 300) {
        payload = http.getString();
        payload.replace("\n", "");
        payload.replace("\r", "");
        if (payload.length() == 0) {
            payload = "{\"error\":\"http_sin_contenido\"}";
        } else if (payload.length() > MAX_RESPUESTA_BYTES) {
            payload = "{\"error\":\"respuesta_demasiado_grande\"}";
        }
    } else if (httpCode > 0) {
        payload = "{\"error\":\"http_status_" + String(httpCode) + "\"}";
    } else {
        payload = "{\"error\":\"http_" + String(httpCode) + "\"}";
    }

    http.end();
    return payload;
}
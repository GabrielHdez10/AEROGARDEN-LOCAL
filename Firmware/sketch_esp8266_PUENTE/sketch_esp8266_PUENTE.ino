#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClient.h>

// AeroGarden: puente ESP8266 para Flask en la computadora.
// Escribe la contraseña real de linksys_SUIP antes de cargar.
const char* WIFI_SSID = "linksys_SUIP";
const char* WIFI_PASS = "e0h6i2e5Suip";

// Esta es la IP de la computadora, no la del ESP8266.
// Mantén python app.py ejecutándose mientras uses el sistema.
const char* SERVER_HOST = "http://192.168.2.108:5000";

unsigned long tUltimoIntento = 0;
const unsigned long REINTENTO_WIFI_MS = 10000UL;

String procesarComando(String cmd);

void setup() {
    // Enlace con el Mega: debe coincidir con BAUD_ESP.
    Serial.begin(115200);
    delay(200);

    Serial.println("=== ESP8266 Puente WiFi LOCAL ===");
    Serial.print("Conectando a ");
    Serial.println(WIFI_SSID);

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
                           ? "{\"pong\":true,\"wifi\":true}"
                           : "{\"pong\":true,\"wifi\":false}");
            return;
        }

        Serial.println(procesarComando(linea));
    }
}

// Comandos del Mega:
// GET:/ruta
// POST:/ruta:{"campo":"valor"}
String procesarComando(String cmd) {
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

    // Flask local utiliza HTTP: cliente sin TLS.
    WiFiClient cliente;
    HTTPClient http;

    if (!http.begin(cliente, url)) {
        return "{\"error\":\"http_begin\"}";
    }
    http.setTimeout(8000);

    int httpCode = -1;
    String payload = "";

    if (metodo == "POST") {
        http.addHeader("Content-Type", "application/json");
        httpCode = http.POST(body);
    } else {
        httpCode = http.GET();
    }

    if (httpCode > 0) {
        payload = http.getString();
        payload.replace("\n", "");
        payload.replace("\r", "");
    } else {
        payload = "{\"error\":\"http_" + String(httpCode) + "\"}";
    }

    http.end();
    return payload;
}

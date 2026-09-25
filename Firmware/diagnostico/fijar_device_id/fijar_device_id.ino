// ============================================================
// FIJAR DEVICE_ID — sketch de una sola vez (25/09/2026)
// Escribe el device_id en la EEPROM del Mega, con el mismo
// formato que usa sketch_hidroponia_MEGA (ver config.h):
//   EEPROM_ADDR = 0 (int), EEPROM_MAGIC_ADDR = 10, MAGIC = 0xAB
// Después de correrlo hay que volver a subir el firmware
// principal (este sketch lo reemplaza en el Mega).
// ============================================================

#include <EEPROM.h>

const int NUEVO_DEVICE_ID   = 6;
const int EEPROM_ADDR       = 0;
const int EEPROM_MAGIC_ADDR = 10;
const byte EEPROM_MAGIC     = 0xAB;
const int RELAY_PIN         = 2;

void setup() {
    // Estado seguro del relevador ANTES de configurar el pin
    digitalWrite(RELAY_PIN, HIGH);
    pinMode(RELAY_PIN, OUTPUT);

    Serial.begin(9600);
    delay(500);

    int anterior = -1;
    if (EEPROM.read(EEPROM_MAGIC_ADDR) == EEPROM_MAGIC) {
        EEPROM.get(EEPROM_ADDR, anterior);
    }
    Serial.print("device_id anterior: "); Serial.println(anterior);

    EEPROM.put(EEPROM_ADDR, NUEVO_DEVICE_ID);
    EEPROM.write(EEPROM_MAGIC_ADDR, EEPROM_MAGIC);

    int leido = -1;
    EEPROM.get(EEPROM_ADDR, leido);
    Serial.print("device_id escrito: "); Serial.println(NUEVO_DEVICE_ID);
    Serial.print("Leido de vuelta:   "); Serial.println(leido);

    if (leido == NUEVO_DEVICE_ID) {
        Serial.println("OK. Ahora sube de nuevo sketch_hidroponia_MEGA.");
    } else {
        Serial.println("ERROR: no coincide. No subas el firmware aun.");
    }
}

void loop() {
    // Nada: el trabajo se hace una sola vez en setup()
}

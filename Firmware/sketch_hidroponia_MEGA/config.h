#ifndef CONFIG_H
#define CONFIG_H

// ============================================================
// Configuración de hardware — AeroGarden
// Aquí van los pines, tiempos y constantes del sistema.
// Los datos sensibles (token, servidor) están en secrets.h
//
// VERIFICADO 22/09/2026 — Mapa de pines confirmado por barrido
// físico (17/09/2026). Ver nota en DHTPIN.
// ============================================================

// ── Sensores ────────────────────────────────────────────────
// DHTPIN: la documentación heredada decía pin 8, pero el DHT22
// está cableado físicamente al pin 6 (confirmado por barrido el
// 17/09/2026). Se decidió ajustar el código y no mover el cable.
// NO regresar a 8 sin revisar primero el cableado real.
#define DHTPIN        6      // DHT22: temperatura y humedad del aire
#define DHTTYPE       DHT22
#define TRIG_PIN      53     // HC-SR04: disparo
#define ECHO_PIN      51     // HC-SR04: eco
#define PH_PIN        A0     // Sonda de pH (valor crudo, 0-1023)
#define LUZ_PIN       A1     // LDR (valor crudo, 0-1023)

// ── Sensores de agua añadidos 06/10/2026 ─────────────────────
#define AGUA_PIN      7      // DS18B20: temperatura del agua
#define TDS_PIN       A2     // Salida analógica del módulo TDS Meter V1.0
#define ADC_VREF      5.0f   // Referencia ADC nominal del Mega, en voltios
// EC y TDS se calculan desde la misma señal; requieren calibración.
// La lógica y la calibración están en el .ino actualizado del paso 1.

// ── Actuadores ──────────────────────────────────────────────
#define RELAY_PIN     2      // Bomba de riego

// El módulo de relevador es activo en bajo:
// LOW enciende la bomba, HIGH la apaga.
#define RELAY_ON      LOW
#define RELAY_OFF     HIGH

// ── Muestreo ────────────────────────────────────────────────
#define INTERVALO_DATOS   10000UL  // ms entre envíos al servidor
#define MUESTRAS_ANALOG   10       // lecturas a promediar
#define DELAY_MUESTRA     10       // ms entre cada muestra

// ── Protección de la bomba por nivel de agua ────────────────
// Requiere el .ino actualizado con la logica de proteccion por nivel.
// Agregar este bloque por sí solo NO protege ni apaga la bomba.
//
// El HC-SR04 debe estar encima del agua y apuntar a su superficie:
// mayor distancia medida = menor nivel de agua.
// LIMITES PROVISIONALES: 25 cm de corte y 20 cm de recuperacion.
// Estimados a partir de las dimensiones aproximadas del deposito.
// Validar que la succion conserve agua suficiente al alcanzar el corte.
// Probar bajo supervision; estos valores aun no estan calibrados.
// No reutilizar el aviso de 20 cm del dashboard como límite físico.
#define PROTECCION_NIVEL_ACTIVA       1     // Habilitada para prueba supervisada
#define NIVEL_DIST_CORTE_CM         25.0f  // Distancia >= 25 cm: bloquear riego
#define NIVEL_DIST_RECUPERACION_CM  20.0f  // Distancia <= 20 cm: permitir recuperacion

// Valores iniciales de software; validar durante la integración.
// La recuperación requiere lecturas válidas consecutivas dentro
// del límite de recuperación. Una lectura no válida rompe la racha.
#define NIVEL_MUESTREO_MS            1000UL // ms entre mediciones de nivel
#define NIVEL_MAX_ANTIGUEDAD_MS      5000UL // Edad máxima de la última lectura válida
#define NIVEL_LECTURAS_RECUPERACION  3      // Lecturas consecutivas para recuperar

// Politica del .ino actualizado cuando la proteccion esta activa:
// - Nivel bajo, lectura no válida o lectura vencida: bomba apagada.
// - El bloqueo tiene prioridad sobre los modos manual y automático.
// - Recuperar habilita el control normal; no ordena encender la bomba.
// Confirmar el bloqueo y la recuperacion con el montaje real.
// Al arrancar, espera lecturas validas <= 20 cm para permitir el riego.

// Comprobaciones de configuración; no sustituyen la lógica del .ino.
#if (PROTECCION_NIVEL_ACTIVA != 0) && (PROTECCION_NIVEL_ACTIVA != 1)
#error "PROTECCION_NIVEL_ACTIVA debe ser 0 o 1."
#endif

#if NIVEL_MUESTREO_MS == 0
#error "NIVEL_MUESTREO_MS debe ser mayor que cero."
#endif

#if NIVEL_MAX_ANTIGUEDAD_MS <= NIVEL_MUESTREO_MS
#error "La antiguedad maxima debe superar el intervalo de muestreo."
#endif

#if NIVEL_LECTURAS_RECUPERACION < 1
#error "Se requiere al menos una lectura para recuperar."
#endif

#if PROTECCION_NIVEL_ACTIVA
static_assert(NIVEL_DIST_RECUPERACION_CM > 0.0f,
              "Mide y configura la distancia de recuperacion.");
static_assert(NIVEL_DIST_CORTE_CM > NIVEL_DIST_RECUPERACION_CM,
              "La distancia de corte debe superar la de recuperacion.");
#endif

// ── Comunicación ────────────────────────────────────────────
#define BAUD_DEBUG        9600     // Serial: monitor USB
#define BAUD_ESP          115200   // Serial1: enlace con el ESP8266
#define TIMEOUT_ESP       8000     // ms de espera por respuesta
#define ESPERA_ESP_MAX    20000    // ms MÁXIMOS esperando a que el ESP conecte al WiFi
                                   // (ya no es una espera fija: se sale en cuanto el ESP responde "listo")
#define PING_ESP_INTERVALO 500     // ms entre PING al ESP mientras se espera
#define ESPERA_MIN_DHT    2500     // ms desde el encendido antes de la 1a lectura del DHT22

// ── EEPROM ──────────────────────────────────────────────────
// El byte mágico distingue una EEPROM virgen (0xFF) de una
// que ya contiene datos válidos.
#define EEPROM_ADDR        0       // device_id
#define EEPROM_MAGIC_ADDR  10
#define EEPROM_MAGIC       0xAB

// ── Valores iniciales del riego ─────────────────────────────
// Inicializan las variables del programa. El firmware mantiene
// la bomba apagada hasta recibir una configuración válida del servidor.
#define RIEGO_ON_DEFAULT   30      // segundos encendida
#define RIEGO_OFF_DEFAULT  60      // segundos apagada

#endif
// AEROGARDEN: paso 1, DS18B20 y TDS dentro de este mismo archivo.
// ACTIVIDAD 2 - 26/10/2026: integra registro_riego.h y prepara el envio
// de avisos confirmados. Requiere ese .h junto a este .ino.
// El envio se activa SOLO si app.py anuncia el protocolo riego_eventos=1.
// Hasta actualizar el receptor, solo se conservan hasta 8 avisos en RAM.
// La proteccion de nivel se prepara en config.h y permanece desactivada
// hasta medir el deposito. Requiere registro_riego.h y app.py con avisos de nivel.
// Con proteccion activa, el nivel puede bloquear la bomba en cualquier modo.
// La calibracion y el procesamiento de pH, CE y temperatura se conservan.
// ============================================================
// AJUSTE BOMBA 16/10/2026: consulta de configuración al arrancar y
// diagnóstico de órdenes rechazadas. Se conserva la lógica de riego.
// VERSIÓN 09/10/2026 — FIX comunicación Mega <-> ESP8266
//  - enviarDatos(): el POST se armaba con serializeJson() sobre un
//    String que ya traía "POST:/api/arduino/datos:". ArduinoJson 7
//    BORRA el String antes de escribir (la 6 agregaba al final), así
//    que el ESP recibía solo el JSON y contestaba "metodo_invalido".
//    Ahora se serializa aparte y luego se concatena (sirve con v6 y v7).
//  - enviarAlESP32(): el debug se imprime ANTES de enviar y se espera a
//    que salga. Imprimirlo a 9600 baudios mientras el ESP contestaba
//    dejaba al Mega ~100 ms sin leer Serial1; su buffer (63 bytes) se
//    llenaba y la respuesta llegaba cortada (la config nunca se aplicaba).
//    Mientras llega la respuesta solo se atiende el relay.
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
#include "registro_riego.h"
#include "secrets.h"
// PASO 1 — integración en un único .ino (06/10/2026).
// Usar config.h con parametros de nivel, registro_riego.h actualizado y secrets.h.
// Bibliotecas: DHT, OneWire, DallasTemperature y ArduinoJson (6.21.5 o 7.x).
// Monitor serie: BAUD_DEBUG de config.h (antes: 9600), Nueva línea.
// Los datos de agua se prueban localmente hasta revisar servidor/unidades.
// NO activar envío de agua hasta instalar el soporte del protocolo 2.
#define ENVIAR_AGUA_AL_SERVIDOR 1
#define DEBUG_SERIAL_ANTERIOR 1
#ifndef AGUA_PIN
#define AGUA_PIN 7
#endif
#ifndef TDS_PIN
#define TDS_PIN A2
#endif
#ifndef ADC_VREF
#define ADC_VREF 5.0f
#endif
// Calibrar: CAL_INICIAR -> esperar >=30 s estable con patrón 1413 uS/cm
// -> CAL_1413 -> CAL_GUARDAR -> devolver sondas al depósito -> CAL_SALIR.
// Bomba ordenada OFF durante calibración; K persiste en EEPROM 64..74.

#ifndef AEROGARDEN_AGUA_H
#define AEROGARDEN_AGUA_H
#include <OneWire.h>
#include <DallasTemperature.h>
#include <ArduinoJson.h>
#include <EEPROM.h>
#include <math.h>

// Referencia provisional: https://wiki.dfrobot.com/sen0244/docs/20305
// K es una corrección de un punto; no amplía el rango del módulo genérico.
inline float ecReferencia(float v,float t) {
    if (!isfinite(v) || !isfinite(t) || v<0 || t<0 || t>55) return NAN;
    v/=1.0f+0.02f*(t-25.0f);
    return (133.42f*v*v*v-255.86f*v*v+857.39f*v)/1000.0f;
}
inline bool factorValido(float k) {return isfinite(k) && k>=0.5f && k<=2.0f;}
void controlarRelay();

class SensoresAgua {
    OneWire wire;
    DallasTemperature ds;
    DeviceAddress direccion;
    uint16_t muestras[31];
    uint8_t indice=0,cantidad=0;
    bool encontrado=false,convirtiendo=false,descartar=false;
    unsigned long muestreo=0,busqueda=0,inicioConversion=0,ultimaTemp=0;
    unsigned long inicioCal=0,candidatoDesde=0,ultimoInforme=0;
    String comando;
    float candidato=NAN;
    static const int EEPROM_CAL=64;

    static uint8_t checksum(const uint8_t *bytes) {
        uint8_t c=0x5D;
        for(uint8_t i=0;i<9;++i)c=(c*33U)^bytes[i];
        return c;
    }
    void log(const String &s) {
        for(unsigned int i=0;i<s.length();++i){
            while(Serial.availableForWrite()==0)controlarRelay();
            Serial.write(s[i]);controlarRelay();
        }
        Serial.println();
    }
    void guardar() {
        uint8_t datos[9];datos[0]=1;
        float ref=ADC_VREF;
        memcpy(datos+1,&k,4);memcpy(datos+5,&ref,4);
        EEPROM.update(EEPROM_CAL,0);
        for(uint8_t i=0;i<9;++i)EEPROM.update(EEPROM_CAL+1+i,datos[i]);
        EEPROM.update(EEPROM_CAL+10,checksum(datos));
        EEPROM.update(EEPROM_CAL,0xC7);calibrado=true;
    }
    void leerCal() {
        if(EEPROM.read(EEPROM_CAL)!=0xC7)return;
        uint8_t datos[9];
        for(uint8_t i=0;i<9;++i)datos[i]=EEPROM.read(EEPROM_CAL+1+i);
        if(datos[0]!=1 || EEPROM.read(EEPROM_CAL+10)!=checksum(datos))return;
        float previo,ref;memcpy(&previo,datos+1,4);memcpy(&ref,datos+5,4);
        if(factorValido(previo) && isfinite(ref) && fabs(ref-ADC_VREF)<0.0001f){k=previo;calibrado=true;}
    }
    void procesar(const String &cmd) {
        if(cmd=="CAL_INICIAR"){
            calibrando=true;candidato=NAN;inicioCal=millis();controlarRelay();
            log(F("Bomba OFF. Ambas sondas en patron 1413 uS/cm. Espera 30 s y estabilidad; CAL_1413."));
        }else if(cmd=="CAL_1413"){
            uint16_t dispersion=0;float v=voltaje(&dispersion),base=ecReferencia(v,temperatura);
            candidato=NAN;
            if(!calibrando || millis()-inicioCal<30000UL || !tempValida() || temperatura<20 || temperatura>30 ||
               !isfinite(v) || v<=0.03f || v>=2.3f || !isfinite(base) || base<=0){
                log(F("No listo: iniciar CAL, esperar 30 s, temperatura 20-30 C y senal valida."));return;
            }
            if(dispersion>max(6.0f,v*1024.0f/ADC_VREF*0.04f)){
                log(F("Senal inestable: revisar burbujas/interferencia y repetir CAL_1413."));return;
            }
            float nuevo=1.413f/base;
            if(!factorValido(nuevo)){log(F("K fuera de 0.5-2.0: revisar patron y cableado. No se guardo."));return;}
            candidato=nuevo;candidatoDesde=millis();
            log("K propuesto="+String(candidato,6)+F(". Confirma patron 1413 uS/cm a 25 C: CAL_GUARDAR."));
        }else if(cmd=="CAL_GUARDAR"){
            if(!calibrando || !factorValido(candidato) || millis()-candidatoDesde>180000UL){
                log(F("Primero CAL_1413 para obtener un candidato reciente."));return;
            }
            k=candidato;guardar();candidato=NAN;
            log(F("K guardado. Bomba sigue OFF. Enjuaga y devuelve sondas al deposito; despues CAL_SALIR."));
        }else if(cmd=="CAL_SALIR" || cmd=="CAL_CANCELAR"){
            calibrando=false;candidato=NAN;cantidad=0;temperatura=NAN;
            // El control de riego detecta la salida y reinicia la fase apagada.
            log(F("Medicion reanudada. Se aplicara la ultima configuracion valida de riego."));
        }else if(cmd=="CAL_BORRAR" && calibrando){
            EEPROM.update(EEPROM_CAL,0);k=1;calibrado=false;candidato=NAN;
            log(F("Calibracion borrada; device_id conservado. Bomba sigue OFF."));
        }else if(cmd=="AYUDA" || cmd=="ESTADO"){
            log("K="+String(k,6)+" calibrado="+String(calibrado?"si_1_punto":"no")+
                F(" | CAL_INICIAR / CAL_1413 / CAL_GUARDAR / CAL_SALIR / CAL_CANCELAR / CAL_BORRAR"));
        }else log(F("Comando desconocido. Escribe AYUDA con Nueva linea."));
    }
public:
    bool calibrando=false,calibrado=false;
    float k=1.0f,temperatura=NAN;
    SensoresAgua():wire(AGUA_PIN),ds(&wire){}
    void begin(){pinMode(TDS_PIN,INPUT);comando.reserve(40);leerCal();busqueda=millis()-5000UL;}
    bool tempValida(){return isfinite(temperatura) && millis()-ultimaTemp<=4000UL;}
    float voltaje(uint16_t *dispersion=NULL){
        if(cantidad<31 || millis()-muestreo>250UL)return NAN;
        uint16_t orden[31];memcpy(orden,muestras,sizeof(orden));
        for(uint8_t i=1;i<31;++i){uint16_t v=orden[i];int j=i-1;
            while(j>=0 && orden[j]>v){orden[j+1]=orden[j];--j;}orden[j+1]=v;}
        if(dispersion)*dispersion=orden[30]-orden[0];
        return orden[15]*(ADC_VREF/1024.0f);
    }
    const char *estado(float v,float ec){
        if(calibrando)return "calibrando";
        if(!tempValida() || temperatura<0 || temperatura>55)return "sin_temperatura";
        if(!isfinite(v))return "iniciando";
        if(v<=0.03f)return "senal_baja";
        if(!isfinite(ec) || v>=2.3f || ec<0 || ec>=2.0f)return "fuera_rango";
        return calibrado?"ok":"sin_calibrar";
    }
    void update(){
        unsigned long ahora=millis();
        if(ahora-muestreo>=40UL){muestreo=ahora;analogRead(TDS_PIN);muestras[indice]=analogRead(TDS_PIN);
            indice=(indice+1)%31;if(cantidad<31)++cantidad;}
        if(!encontrado && ahora-busqueda>=5000UL){
            busqueda=ahora;ds.begin();ds.setWaitForConversion(false);
            encontrado=ds.getDeviceCount()==1 && ds.getAddress(direccion,0);
            if(encontrado)ds.setResolution(direccion,12);
        }
        if(encontrado){
            if(convirtiendo && ahora-inicioConversion>=750UL){
                float t=ds.getTempC(direccion);convirtiendo=false;
                if(isfinite(t) && t!=DEVICE_DISCONNECTED_C && t!=85 && t>=-55 && t<=125){temperatura=t;ultimaTemp=ahora;}
                else{temperatura=NAN;encontrado=false;}
            }
            if(encontrado && !convirtiendo && ahora-inicioConversion>=1000UL){ds.requestTemperatures();inicioConversion=ahora;convirtiendo=true;}
        }
        while(Serial.available()){
            char c=Serial.read();
            if(c=='\r' || c=='\n'){
                if(!descartar && comando.length()){comando.trim();procesar(comando);}
                comando="";descartar=false;
            }else if(!descartar && comando.length()<40)comando+=c;
            else{descartar=true;comando="";}
            controlarRelay();
        }
    }
    void agregar(JsonObject s,JsonObject unidades,JsonObject diagnostico){
        float v=voltaje(),ec=ecReferencia(v,temperatura)*k;
        const char *e=estado(v,ec);
        s["temperatura_agua"]=nullptr;s["ec"]=nullptr;
        if(tempValida() && !calibrando)s["temperatura_agua"]=temperatura;
        if(!strcmp(e,"ok") || !strcmp(e,"sin_calibrar"))s["ec"]=ec;
        unidades["temperatura_agua"]="°C";unidades["ec"]="mS/cm";
        diagnostico["temperatura_agua"]["estado"]=calibrando?"calibrando":(tempValida()?"ok":"sin_sensor");
        JsonObject d=diagnostico.createNestedObject("ec");
        d["estado"]=e;d["calibrado"]=calibrado;d["factor_k"]=k;
        if(isfinite(v))d["voltaje"]=v;
        if(tempValida())d["temperatura_c"]=temperatura;
        if(tempValida() && isfinite(ec))d["tds_ppm"]=ec*500.0f;
    }
    void informar(){
        if(millis()-ultimoInforme<2000UL)return;ultimoInforme=millis();
        float v=voltaje(),ec=ecReferencia(v,temperatura)*k;
        log("Agua="+(tempValida()?String(temperatura,2):String("ND"))+" C | A2="+String(v,3)+
            " V | EC estimada="+String(ec,3)+" mS/cm | TDS="+String(ec*500,1)+" ppm | "+estado(v,ec));
    }
};
#endif

SensoresAgua agua;
bool configRecibida = false;
bool calibrandoAnterior = false;
void atenderLocal();
void esperarAtendiendo(unsigned long ms);
void imprimirSeguro(const String &mensaje);
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

// La cola guarda avisos; controlarRelay() nunca hace peticiones de red.
RegistroRiego registroRiego;
char arranqueRiego[33] = ""; // Sesion de 32 caracteres asignada por app.py.
bool eventosRiegoHabilitados = false;
uint32_t ultimoIntentoRiego = 0;
uint32_t esperaIntentoRiego = 1000UL;
bool errorEventoRiegoAvisado = false;

// ================================================================
// NIVEL DE AGUA: decision local y captura del eco sin pulseIn().
// Solo se compila al habilitar PROTECCION_NIVEL_ACTIVA en config.h.
// ECHO 51 = PB2/PCINT2 en Mega 2560; no se mueve ningun cable.
// Mapa: github.com/arduino/ArduinoCore-avr/blob/master/variants/mega/pins_arduino.h
// HC-SR04: cdn.sparkfun.com/datasheets/Sensors/Proximity/HCSR04.pdf
// ================================================================
#ifndef PROTECCION_NIVEL_ACTIVA
#error "Instala el config.h que incluye los parametros de nivel."
#endif

#if PROTECCION_NIVEL_ACTIVA
#if !defined(__AVR_ATmega2560__) || ECHO_PIN != 51
#error "La captura de nivel requiere Mega 2560 con ECHO_PIN 51."
#endif
#include <avr/interrupt.h>
#include <util/atomic.h>
static_assert(NIVEL_DIST_RECUPERACION_CM >= 2.0f &&
              NIVEL_DIST_CORTE_CM <= 400.0f,
              "Los limites deben estar dentro de 2 a 400 cm del HC-SR04.");
static_assert(NIVEL_MUESTREO_MS > 60UL && NIVEL_LECTURAS_RECUPERACION <= 255,
              "Revisa el intervalo de nivel y las lecturas de recuperacion.");

// No depende de pines, red ni EEPROM. Una muestra mala corta de inmediato;
// para recuperar se exigen N muestras buenas consecutivas <= recuperacion.
class ProteccionNivel {
public:
    enum Estado : uint8_t { SIN_LECTURA, BAJO, INVALIDO, VENCIDO, LISTO };
    void evaluar(uint32_t ahora) {
        if (tieneMuestra_ &&
            (uint32_t)(ahora - ultimaMuestra_) >= NIVEL_MAX_ANTIGUEDAD_MS) {
            estado_ = VENCIDO;
            consecutivas_ = 0;
        }
    }
    void muestra(float cm, uint32_t ahora) {
        evaluar(ahora); // Una pausa larga rompe una recuperacion incompleta.
        ultimaMuestra_ = ahora;
        tieneMuestra_ = true;
        distancia_ = cm;
        valida_ = isfinite(cm) && cm >= 2.0f && cm <= 400.0f;
        if (!valida_) {
            estado_ = INVALIDO;
            consecutivas_ = 0;
        } else if (cm >= NIVEL_DIST_CORTE_CM) {
            estado_ = BAJO;
            consecutivas_ = 0;
        } else if (estado_ == LISTO) {
            consecutivas_ = 0; // En la banda intermedia conserva el permiso.
        } else if (cm <= NIVEL_DIST_RECUPERACION_CM) {
            if (consecutivas_ < NIVEL_LECTURAS_RECUPERACION) ++consecutivas_;
            if (consecutivas_ >= NIVEL_LECTURAS_RECUPERACION) estado_ = LISTO;
        } else {
            consecutivas_ = 0; // En la banda intermedia conserva el bloqueo.
        }
    }
    bool bloqueado() const { return estado_ != LISTO; }
    Estado estado() const { return estado_; }
    float distancia(uint32_t ahora) const {
        return tieneMuestra_ && valida_ &&
               (uint32_t)(ahora - ultimaMuestra_) < NIVEL_MAX_ANTIGUEDAD_MS
               ? distancia_ : NAN;
    }
private:
    Estado estado_ = SIN_LECTURA;
    uint8_t consecutivas_ = 0;
    uint32_t ultimaMuestra_ = 0;
    float distancia_ = NAN;
    bool tieneMuestra_ = false, valida_ = false;
};

ProteccionNivel nivelAgua;
bool nivelIniciado = false, nivelTareaLenta = false;
bool nivelMidiendo = false, nivelBloqueoAnterior = true;
uint32_t nivelUltimoDisparoMs = 0, nivelInicioUs = 0;
enum : uint8_t { ECO_INACTIVO, ECO_SUBIDA, ECO_BAJADA, ECO_COMPLETO };
volatile uint8_t nivelEstadoEco = ECO_INACTIVO;
volatile uint32_t nivelSubidaUs = 0, nivelAnchoUs = 0;
volatile uint32_t nivelFinUs = 0, nivelFinMs = 0;
static const uint32_t NIVEL_TIMEOUT_ECO_US = 30000UL;

// Se reserva PCINT0_vect para este eco. La ISR solo captura tiempos;
// no imprime, no lee otros sensores y no usa JSON ni la red.
ISR(PCINT0_vect) {
    bool alto = (PINB & _BV(PB2)) != 0;
    if (nivelEstadoEco == ECO_SUBIDA && alto) {
        nivelSubidaUs = (uint32_t)micros();
        nivelEstadoEco = ECO_BAJADA;
    } else if (nivelEstadoEco == ECO_BAJADA && !alto) {
        nivelFinUs = (uint32_t)micros();
        nivelAnchoUs = nivelFinUs - nivelSubidaUs;
        nivelFinMs = (uint32_t)millis();
        nivelEstadoEco = ECO_COMPLETO;
    }
}

// Se atiende tambien mientras se espera la respuesta del ESP8266.
// Solo el pulso TRIG ocupa 12 us; no se espera al eco.
void actualizarNivel() {
    if (!nivelIniciado) return;
    uint32_t ahora = (uint32_t)millis();
    bool lista = false, vencida = false;
    uint32_t ancho = 0, finUs = 0, finMs = 0;
    if (nivelMidiendo) {
        ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
            lista = nivelEstadoEco == ECO_COMPLETO;
            vencida = (uint32_t)((uint32_t)micros() - nivelInicioUs) >= NIVEL_TIMEOUT_ECO_US;
            if (lista || vencida) {
                ancho = nivelAnchoUs; finUs = nivelFinUs; finMs = nivelFinMs;
                nivelEstadoEco = ECO_INACTIVO;
                PCMSK0 &= (uint8_t)~_BV(PCINT2);
            }
        }
        if (lista || vencida) {
            nivelMidiendo = false;
            float cm = lista &&
                       (uint32_t)(finUs - nivelInicioUs) < NIVEL_TIMEOUT_ECO_US
                       ? ancho * 0.034f / 2.0f : NAN;
            nivelAgua.muestra(cm, lista ? finMs : ahora);
        }
    }
    ahora = (uint32_t)millis(); // La ISR pudo terminar en el siguiente tick.
    nivelAgua.evaluar(ahora);
    if (nivelMidiendo || nivelTareaLenta ||
        (uint32_t)(ahora - nivelUltimoDisparoMs) < NIVEL_MUESTREO_MS) return;
    nivelUltimoDisparoMs = ahora;
    // Un eco ya alto antes de disparar no se interpreta como agua suficiente.
    if (digitalRead(ECHO_PIN) != LOW) {
        nivelAgua.muestra(NAN, ahora);
        return;
    }
    digitalWrite(TRIG_PIN, LOW);
    delayMicroseconds(2);
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        nivelEstadoEco = ECO_SUBIDA;
        nivelInicioUs = (uint32_t)micros();
        PCIFR = _BV(PCIF0);
        PCMSK0 |= _BV(PCINT2);
        PCICR |= _BV(PCIE0);
    }
    nivelMidiendo = true;
    digitalWrite(TRIG_PIN, HIGH);
    delayMicroseconds(10);
    digitalWrite(TRIG_PIN, LOW);
}

RegistroRiego::Motivo motivoNivel() {
    switch (nivelAgua.estado()) {
        case ProteccionNivel::BAJO: return RegistroRiego::NIVEL_BAJO;
        case ProteccionNivel::INVALIDO: return RegistroRiego::NIVEL_INVALIDO;
        case ProteccionNivel::VENCIDO: return RegistroRiego::NIVEL_VENCIDO;
        default: return RegistroRiego::NIVEL_SIN_LECTURA;
    }
}
#endif

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
// COMUNICACIÓN CON ESP8266
// El Mega envía comandos por Serial1, el ESP8266 responde con JSON
// ================================================================

// Espera a que el monitor USB termine de transmitir, sin descuidar el riego.
void vaciarDebug() {
    while (Serial.availableForWrite() < SERIAL_TX_BUFFER_SIZE - 1) controlarRelay();
}

String enviarAlESP32(String cmd, int timeoutMs = TIMEOUT_ESP) {
    // FIX 09/10/2026: el debug va ANTES de enviar. Así, cuando el ESP
    // empieza a contestar, el Mega ya no está ocupado imprimiendo a 9600.
    if (DEBUG_SERIAL_ANTERIOR) {
        Serial.print(F("[->ESP32] "));
        imprimirSeguro(cmd);
    }
    vaciarDebug();

    // Limpiar buffer
    while (Serial1.available()) Serial1.read();

    for (unsigned int i=0; i<cmd.length(); ++i) {
        while (Serial1.availableForWrite()==0) atenderLocal();
        Serial1.write(cmd[i]);
    }
    Serial1.println();

    // Mientras llega la respuesta, NADA que bloquee: solo el relay.
    // agua.update() puede tardar más de 5 ms (búsqueda del DS18B20)
    // y en ese tiempo se desbordaría el buffer de 63 bytes de Serial1.
    String resp = "";
    unsigned long t = millis();
    while (millis() - t < (unsigned long)timeoutMs) {
        controlarRelay();
        while (Serial1.available()) {
            char c = Serial1.read();
            if (resp.length() < 1500) resp += c;
            else return "";
            if (c == '\n') goto done;
        }
    }
    done:
    resp.trim();
    if (DEBUG_SERIAL_ANTERIOR) {
        Serial.print(F("[ESP32->] "));
        Serial.println(resp);
    }
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
            atenderLocal();
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
    long luz=0,ph=0;
    for(int i=0;i<MUESTRAS_ANALOG;++i){
        analogRead(LUZ_PIN);luz+=analogRead(LUZ_PIN);
        analogRead(PH_PIN);ph+=analogRead(PH_PIN);
        esperarAtendiendo(DELAY_MUESTRA);
    }
    StaticJsonDocument<1152> doc;
    if (DEBUG_SERIAL_ANTERIOR) {
        Serial.print(F("Lecturas -> temperatura="));
        if (isfinite(temp)) Serial.print(temp, 2); else Serial.print(F("ND"));
        Serial.print(F(" C | humedad="));
        if (isfinite(humedad)) Serial.print(humedad, 2); else Serial.print(F("ND"));
        Serial.print(F(" % | distancia="));
        if (isfinite(dist) && dist > 0) Serial.print(dist, 2); else Serial.print(F("ND"));
        Serial.print(F(" cm | luz="));
        Serial.print(luz / MUESTRAS_ANALOG);
        Serial.print(F(" | ph="));
        Serial.println(ph / MUESTRAS_ANALOG);
    }
    doc["token"]=ARDUINO_TOKEN;doc["device_id"]=deviceId;doc["protocolo"]=2;
    JsonObject sensores=doc.createNestedObject("sensores");
    if(isfinite(temp))sensores["temperatura"]=temp;
    if(isfinite(humedad))sensores["humedad"]=humedad;
    if(isfinite(dist) && dist>0)sensores["distancia"]=dist;
    sensores["luz"]=luz/MUESTRAS_ANALOG;sensores["ph"]=ph/MUESTRAS_ANALOG;
    JsonObject unidades=doc.createNestedObject("unidades");
    JsonObject diagnostico=doc.createNestedObject("diagnostico");
    if (ENVIAR_AGUA_AL_SERVIDOR) {
        agua.agregar(sensores,unidades,diagnostico);
    }
    if(doc.overflowed()){Serial.println(F("Paquete demasiado grande"));return;}
    // FIX 09/10/2026: serializar aparte. ArduinoJson 7 borra el String
    // destino antes de escribir; con la 6 se agregaba al final.
    String json;
    serializeJson(doc,json);
    String cmd="POST:/api/arduino/datos:"+json;
    String respuesta=enviarAlESP32(cmd,10000);
    if (DEBUG_SERIAL_ANTERIOR) {
        Serial.print(F("Resp datos: "));
        Serial.println(respuesta);
    }
    doc.clear();
    if (deserializeJson(doc,respuesta)) {imprimirSeguro(F("Sin confirmacion del servidor"));return;}
    imprimirSeguro("Servidor: "+String(doc["status"] | "error"));
    if (doc["error"].is<const char*>()) imprimirSeguro(doc["error"].as<String>());
    for (JsonVariant e : doc["errores"].as<JsonArray>()) imprimirSeguro(e.as<String>());
}

// ================================================================
// REGISTRO DE RIEGO - TRANSPORTE AL SERVIDOR
// Protocolo previsto para la siguiente actualizacion de app.py:
// GET config?registro_riego=1[&arranque_id=<sesion>]
//   -> {"relay":{...},"riego_eventos":{"protocolo":1,"arranque_id":"<32 hex>"}}
// POST /api/arduino/riego/evento con token, device_id, arranque_id y evento.
//   -> {"status":"ok","arranque_id":"<misma sesion>","secuencia":<mismo aviso>}
// Un servidor anterior puede seguir enviando la configuracion normal: no se
// intentan POST de eventos si no anuncia este protocolo. La cola es finita.
// ================================================================
bool identificadorRiegoValido(const char *identificador) {
    if (!identificador || strlen(identificador) != 32) return false;
    for (uint8_t i = 0; i < 32; ++i) {
        char c = identificador[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
    }
    return true;
}

bool configurarTransporteRiego(const char *identificador, int protocolo) {
    eventosRiegoHabilitados = false;
    if (protocolo != 1 || !identificadorRiegoValido(identificador)) return false;
    if (arranqueRiego[0] == '\0') {
        memcpy(arranqueRiego, identificador, sizeof(arranqueRiego));
    } else if (strcmp(arranqueRiego, identificador) != 0) {
        // Una sesion distinta no puede confirmar los avisos de este arranque.
        return false;
    }
    eventosRiegoHabilitados = true;
    return true;
}

bool confirmarEventoRiego(const char *identificador, uint32_t secuencia) {
    return eventosRiegoHabilitados && identificadorRiegoValido(identificador) &&
           strcmp(arranqueRiego, identificador) == 0 && registroRiego.confirmar(secuencia);
}

void enviarEventoRiego() {
    const RegistroRiego::Evento *e = registroRiego.pendiente();
    if (deviceId <= 0 || !eventosRiegoHabilitados || !e) return;
    uint32_t ahora = (uint32_t)millis();
    if ((uint32_t)(ahora - ultimoIntentoRiego) < esperaIntentoRiego) return;
    ultimoIntentoRiego = ahora;
    esperaIntentoRiego = 10000UL; // Fallo o ACK perdido: reintentar sin retirar el aviso.

    // Solo un aviso por peticion. No agranda el paquete de sensores.
    StaticJsonDocument<768> doc;
    doc["token"] = ARDUINO_TOKEN;
    doc["device_id"] = deviceId;
    doc["arranque_id"] = arranqueRiego;
    JsonObject evento = doc.createNestedObject("evento");
    evento["secuencia"] = e->secuencia;
    evento["tipo_evento"] = RegistroRiego::tipoTexto(e->tipo);
    evento["estado_relevador"] = e->encendido ? "encendido" : "apagado";
    evento["modo"] = e->tiene_config ? (e->manual ? "manual" : "automatico") : "espera";
    evento["estado_manual"] = e->orden_manual ? "encendido" : "apagado";
    evento["tiempo_on"] = e->tiempo_on;
    evento["tiempo_off"] = e->tiempo_off;
    char version[65];
    if (e->tiene_version) {
        RegistroRiego::versionTexto(*e, version);
        evento["config_version"] = version;
    } else evento["config_version"] = nullptr;
    evento["motivo"] = RegistroRiego::motivoTexto(e->motivo);
    evento["uptime_ms"] = e->uptime_ms;
    if (RegistroRiego::tieneDuracion(*e)) evento["duracion_anterior_ms"] = e->duracion_anterior_ms;
    else evento["duracion_anterior_ms"] = nullptr;
    evento["perdidos_total"] = e->perdidos_total;
    if (doc.overflowed()) {
        if (!errorEventoRiegoAvisado) imprimirSeguro(F("Riego: aviso demasiado grande; sigue pendiente."));
        errorEventoRiegoAvisado = true;
        return;
    }

    uint32_t secuencia = e->secuencia;
    String respuesta;
    {
        String json;
        serializeJson(doc, json);
        respuesta = enviarAlESP32("POST:/api/arduino/riego/evento:" + json, 10000);
    }
    doc.clear();
    bool confirmado = false;
    if (!deserializeJson(doc, respuesta) &&
        strcmp(doc["status"] | "", "ok") == 0 &&
        doc["arranque_id"].is<const char*>() && doc["secuencia"].is<uint32_t>() &&
        doc["secuencia"].as<uint32_t>() == secuencia) {
        confirmado = confirmarEventoRiego(doc["arranque_id"].as<const char*>(), secuencia);
    }
    if (confirmado) {
        // El siguiente aviso podra enviarse en un segundo.
        ultimoIntentoRiego = (uint32_t)millis();
        esperaIntentoRiego = 1000UL;
        if (errorEventoRiegoAvisado) imprimirSeguro(F("Riego: recepcion de avisos reanudada."));
        errorEventoRiegoAvisado = false;
    } else {
        // Ningun error de red cambia la orden de la bomba ni retira avisos.
        if (!errorEventoRiegoAvisado) imprimirSeguro(F("Riego: sin confirmacion; aviso conservado para reintento."));
        errorEventoRiegoAvisado = true;
    }
}

// ================================================================
// PEDIR CONFIG DEL RELAY
// ================================================================
void pedirConfig() {
    String cmd="GET:/api/arduino/config?token="+String(ARDUINO_TOKEN)+"&device_id="+String(deviceId);
    cmd += "&registro_riego=1";
    if (arranqueRiego[0] != '\0') cmd += "&arranque_id=" + String(arranqueRiego);
    String respuesta=enviarAlESP32(cmd,TIMEOUT_ESP);
    if (DEBUG_SERIAL_ANTERIOR) {
        Serial.print(F("Config: "));
        Serial.println(respuesta);
    }
    StaticJsonDocument<768> doc;
    if(respuesta.length()==0){
        imprimirSeguro(F("Bomba: sin respuesta de configuracion. Se conserva el estado de control anterior."));
        return;
    }
    DeserializationError error=deserializeJson(doc,respuesta);
    if(error){
        imprimirSeguro(String(F("Bomba: configuracion JSON no valida: "))+error.c_str());
        return;
    }
    JsonObject r=doc["relay"];
    if(r.isNull()){
        imprimirSeguro(F("Bomba: respuesta sin configuracion relay."));
        if(doc["error"].is<const char*>())
            imprimirSeguro(String(F("Bomba: error del servidor/puente: "))+doc["error"].as<String>());
        return;
    }
    if(!r["tiempo_on"].is<int>() || !r["tiempo_off"].is<int>()){
        imprimirSeguro(F("Bomba: tiempo_on y tiempo_off deben ser enteros. Orden rechazada."));
        return;
    }
    int on=r["tiempo_on"],off=r["tiempo_off"];
    const char *modo=r["modo"]|"", *estado=r["estado_manual"]|"";
    if(on<1 || on>3600 || off<1 || off>3600){
        imprimirSeguro(F("Bomba: tiempos fuera de 1 a 3600 segundos. Orden rechazada."));
        return;
    }
    if(strcmp(modo,"manual") && strcmp(modo,"automatico")){
        imprimirSeguro(F("Bomba: modo no valido. Debe ser manual o automatico."));
        return;
    }
    if(strcmp(estado,"encendido") && strcmp(estado,"apagado")){
        imprimirSeguro(F("Bomba: estado_manual no valido. Debe ser encendido o apagado."));
        return;
    }
    bool nuevoManual=strcmp(modo,"manual")==0;
    bool cambioConfig=!configRecibida || nuevoManual!=modoManual ||
        (strcmp(estado,"encendido")==0)!=estadoManual || on!=tiempoOn || off!=tiempoOff;
    if(!configRecibida || nuevoManual!=modoManual){
        tCambioRelay=millis();bombaEncendida=false;digitalWrite(RELAY_PIN,RELAY_OFF);
        registroRiego.salida(false, RegistroRiego::CONFIGURACION, (uint32_t)tCambioRelay);
    }
    tiempoOn=on;tiempoOff=off;modoManual=nuevoManual;estadoManual=strcmp(estado,"encendido")==0;
    configRecibida=true;
    const char *version = r["config_version"].is<const char*>()
                          ? r["config_version"].as<const char*>() : nullptr;
    registroRiego.configurar((uint16_t)tiempoOn, (uint16_t)tiempoOff,
                            modoManual, estadoManual, version, (uint32_t)millis());
    JsonObject transporte = doc["riego_eventos"];
    configurarTransporteRiego(transporte["arranque_id"].as<const char*>(),
                             transporte["protocolo"].is<int>() ? transporte["protocolo"].as<int>() : 0);
    controlarRelay();
    if(cambioConfig){
        imprimirSeguro(String(F("Bomba: config aceptada, modo="))+modo+
            F(", estado_manual=")+estado+F(", ON=")+String(tiempoOn)+
            F(" s, OFF=")+String(tiempoOff)+F(" s"));
    }
}

// ================================================================
// CONTROL RELAY
// app.py configura tiempos y modo; el Mega ejecuta y aplica los bloqueos.
// Con proteccion activa, nivel bajo/invalido/vencido impide cualquier ON.
// Al recuperar, manual sigue la orden vigente; automatico espera su fase OFF.
// VERSIÓN 16/10/2026 — control base conservado cuando nivel esta desactivado
//  - Sin decisión de app.py (aún no llega una config válida o no hay
//    device_id) la bomba queda en OFF: el Mega no decide por su cuenta.
//  - Al llegar la decisión de app.py en automático (primera config o
//    cambio de manual a automático) la bomba arranca REGANDO y luego
//    alterna con tiempo_on / tiempo_off de app.py. Antes arrancaba con
//    la fase apagada: con el ciclo por etapa (5-10 min OFF) parecía
//    que la bomba no encendía.
//  - Manual: ejecuta estado_manual de app.py, igual que antes.
//  - Calibración de EC: OFF, y al salir reinicia la fase apagada,
//    igual que antes.
//  - Cada cambio se avisa por el monitor serie solo con el buffer USB
//    vacío, para no bloquear la lectura de la respuesta del ESP8266.
// ================================================================
void controlarRelay() {
#if PROTECCION_NIVEL_ACTIVA
    actualizarNivel(); // Sin esperas de eco; tambien durante las respuestas del ESP.
#endif
    // modo: 0 calibracion, 1 esperando app.py, 2 manual, 3 auto, 4 bloqueo por nivel
    static uint8_t modoPrevio = 1;
    static uint8_t avisado = 0xFF;              // último estado impreso
    static int onAvisado = -1, offAvisado = -1;
    unsigned long ahora = millis();

    // Al entrar o salir de calibración se reinicia la fase (igual que antes).
    if (agua.calibrando != calibrandoAnterior) {
        calibrandoAnterior = agua.calibrando;
        tCambioRelay = ahora;
    }

    uint8_t modo;
    if (agua.calibrando)                        modo = 0;
    else if (!configRecibida || deviceId <= 0)  modo = 1;
    else if (modoManual)                        modo = 2;
    else                                        modo = 3;

#if PROTECCION_NIVEL_ACTIVA
    bool bloqueadoNivel = nivelAgua.bloqueado();
    if (nivelBloqueoAnterior && !bloqueadoNivel) {
        // Se registra OFF antes de que una orden manual pueda volver a encender.
        registroRiego.proteccionNivel(false, RegistroRiego::NIVEL_RECUPERADO,
                                     (uint32_t)ahora);
        bombaEncendida = false;
        tCambioRelay = ahora; // Automatico retoma con una fase OFF completa.
    }
    nivelBloqueoAnterior = bloqueadoNivel;
    if (bloqueadoNivel) modo = 4;
#endif

    if (modo == 4) {
        bombaEncendida = false;
        tCambioRelay = ahora; // No acumular tiempo de una fase mientras esta bloqueada.
    } else if (modo <= 1) {
        bombaEncendida = false;                 // calibrando o sin decisión de app.py
    } else if (modo == 2) {
        bombaEncendida = estadoManual;          // lo que ordena app.py
    } else if (modoPrevio == 1 || modoPrevio == 2) {
        bombaEncendida = true;                  // llegó la decisión de app.py: regar ya
        tCambioRelay = ahora;
    } else {
        unsigned long fase = (unsigned long)(bombaEncendida ? tiempoOn : tiempoOff) * 1000UL;
        if (ahora - tCambioRelay >= fase) {     // alterna con los tiempos de app.py
            bombaEncendida = !bombaEncendida;
            tCambioRelay = ahora;
        }
    }
    modoPrevio = modo;
    digitalWrite(RELAY_PIN, bombaEncendida ? RELAY_ON : RELAY_OFF);
    RegistroRiego::Motivo motivo = modo == 0 ? RegistroRiego::CALIBRACION :
                                  modo == 1 ? RegistroRiego::ESPERA :
                                  modo == 2 ? RegistroRiego::MANUAL : RegistroRiego::CICLO;
#if PROTECCION_NIVEL_ACTIVA
    if (modo == 4) motivo = motivoNivel();
#endif
    registroRiego.salida(bombaEncendida, motivo, (uint32_t)ahora);
#if PROTECCION_NIVEL_ACTIVA
    if (modo == 4) registroRiego.proteccionNivel(true, motivo, (uint32_t)ahora);
#endif

    // Aviso por monitor serie: solo si algo cambió y el buffer USB está
    // vacío; si no, se reintenta en la siguiente llamada.
    uint8_t estado = modo * 2 + (bombaEncendida ? 1 : 0);
#if PROTECCION_NIVEL_ACTIVA
    if (modo == 4) estado = 8 + (uint8_t)nivelAgua.estado();
#endif
    bool cambio = estado != avisado ||
                  (modo == 3 && (tiempoOn != onAvisado || tiempoOff != offAvisado));
    if (!cambio || Serial.availableForWrite() < SERIAL_TX_BUFFER_SIZE - 1) return;
    avisado = estado; onAvisado = tiempoOn; offAvisado = tiempoOff;

    Serial.print(bombaEncendida ? F("Bomba ON") : F("Bomba OFF"));
#if PROTECCION_NIVEL_ACTIVA
    if (modo == 4) {
        switch (nivelAgua.estado()) {
            case ProteccionNivel::BAJO: Serial.println(F(" (bloqueo: nivel bajo)")); break;
            case ProteccionNivel::INVALIDO: Serial.println(F(" (bloqueo: eco no valido)")); break;
            case ProteccionNivel::VENCIDO: Serial.println(F(" (bloqueo: nivel vencido)")); break;
            default: Serial.println(F(" (bloqueo: nivel pendiente de validar)")); break;
        }
        return;
    }
#endif
    if (modo == 0) { Serial.println(F(" (calibracion EC)")); return; }
    if (modo == 1) {
        Serial.println(deviceId <= 0 ? F(": sin device_id valido")
                                     : F(": esperando config valida de app.py"));
        return;
    }
    if (modo == 2) { Serial.println(F(" (manual, app.py)")); return; }
    unsigned long dur = bombaEncendida ? tiempoOn : tiempoOff;
    unsigned long pasados = (ahora - tCambioRelay) / 1000UL;
    Serial.print(bombaEncendida ? F(" (auto, app.py): se apaga en ")
                                : F(" (auto, app.py): enciende en "));
    Serial.print(dur > pasados ? dur - pasados : 0UL);
    Serial.println(F(" s"));
}

// ================================================================
// SENSOR ULTRASÓNICO
// ================================================================
float leerUltrasonico() {
#if PROTECCION_NIVEL_ACTIVA
    // Usa la misma medicion que protege la bomba; no dispara un segundo eco.
    return nivelAgua.distancia((uint32_t)millis());
#else
    digitalWrite(TRIG_PIN, LOW);  delayMicroseconds(2);
    digitalWrite(TRIG_PIN, HIGH); delayMicroseconds(10);
    digitalWrite(TRIG_PIN, LOW);
    return pulseIn(ECHO_PIN, HIGH, 30000) * 0.034 / 2.0;
#endif
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
    registroRiego.iniciar((uint32_t)millis());

    Serial.begin(BAUD_DEBUG);
    Serial1.begin(BAUD_ESP);   // comunicación con ESP8266

    dht.begin();
    agua.begin();
    pinMode(TRIG_PIN, OUTPUT);
    pinMode(ECHO_PIN, INPUT);
#if PROTECCION_NIVEL_ACTIVA
    digitalWrite(TRIG_PIN, LOW);
    nivelUltimoDisparoMs = (uint32_t)millis() - NIVEL_MUESTREO_MS;
    nivelIniciado = true;
    controlarRelay(); // OFF hasta completar las lecturas de recuperacion.
#endif

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
    if (millis() < ESPERA_MIN_DHT) esperarAtendiendo(ESPERA_MIN_DHT - millis());

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

    // Consultar la orden de la bomba antes del primer envio de sensores.
    // Si falla, pedirConfig conserva el control anterior y loop reintenta.
    if (deviceId > 0) pedirConfig();

    // Que el primer envio ocurra ya, sin esperar los 10 s del intervalo
    ultimoCiclo = millis() - INTERVALO_DATOS;
}

// ================================================================
// LOOP
// ================================================================
void loop() {
    atenderLocal();
    agua.informar();
    if (deviceId <= 0) {
        Serial.println(F("Sin device_id. Reintentando en 30s..."));
        esperarAtendiendo(30000);
        deviceId = emparejar();
        if (deviceId > 0) guardarDeviceIdEEPROM(deviceId);
        return;
    }

    unsigned long ahora = millis();
    if (ahora - ultimoCiclo >= INTERVALO_DATOS) {
        ultimoCiclo = ahora;

#if PROTECCION_NIVEL_ACTIVA
        // El DHT deshabilita interrupciones mientras mide. Terminar el eco
        // pendiente y no disparar otro durante esa lectura evita corromperlo.
        nivelTareaLenta = true;
        while (nivelMidiendo) controlarRelay();
#endif
        float temp    = dht.readTemperature();
        float humedad = dht.readHumidity();
#if PROTECCION_NIVEL_ACTIVA
        nivelTareaLenta = false;
        controlarRelay();
#endif
        float dist    = leerUltrasonico();

        // Una falla del DHT22 no detiene los demás sensores.
        enviarDatos(temp, humedad, dist);
        pedirConfig();
    }

    enviarEventoRiego(); // Red fuera de controlarRelay(); un aviso por iteracion.
    controlarRelay();
}
// Las esperas de red y muestreo mantienen el riego y el DS18B20 activos.
void atenderLocal(){
    controlarRelay();
#if PROTECCION_NIVEL_ACTIVA
    // OneWire necesita tiempos precisos: no hacerlo coincidir con el eco.
    if (!nivelMidiendo) {
        nivelTareaLenta = true;
        agua.update();
        nivelTareaLenta = false;
    }
#else
    agua.update();
#endif
    controlarRelay();
}
void esperarAtendiendo(unsigned long ms){unsigned long t=millis();while(millis()-t<ms)atenderLocal();}

void imprimirSeguro(const String &mensaje){
    for(unsigned int i=0;i<mensaje.length();++i){
        while(Serial.availableForWrite()==0)controlarRelay();
        Serial.write(mensaje[i]);controlarRelay();
    }
    Serial.println();
}

// AEROGARDEN: paso 1, DS18B20 y TDS dentro de este mismo archivo.
// ============================================================
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
#include "secrets.h"
// PASO 1 — integración en un único .ino (06/10/2026).
// Mantener config.h y secrets.h actuales junto a este archivo.
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
// PEDIR CONFIG DEL RELAY
// ================================================================
void pedirConfig() {
    String cmd="GET:/api/arduino/config?token="+String(ARDUINO_TOKEN)+"&device_id="+String(deviceId);
    String respuesta=enviarAlESP32(cmd,TIMEOUT_ESP);
    if (DEBUG_SERIAL_ANTERIOR) {
        Serial.print(F("Config: "));
        Serial.println(respuesta);
    }
    StaticJsonDocument<768> doc;
    if(deserializeJson(doc,respuesta))return;
    JsonObject r=doc["relay"];
    if(!r["tiempo_on"].is<int>() || !r["tiempo_off"].is<int>())return;
    int on=r["tiempo_on"],off=r["tiempo_off"];
    const char *modo=r["modo"]|"", *estado=r["estado_manual"]|"";
    if(on<1 || on>3600 || off<1 || off>3600 ||
       (strcmp(modo,"manual") && strcmp(modo,"automatico")) ||
       (strcmp(estado,"encendido") && strcmp(estado,"apagado")))return;
    bool nuevoManual=strcmp(modo,"manual")==0;
    if(!configRecibida || nuevoManual!=modoManual){
        tCambioRelay=millis();bombaEncendida=false;digitalWrite(RELAY_PIN,RELAY_OFF);
    }
    tiempoOn=on;tiempoOff=off;modoManual=nuevoManual;estadoManual=strcmp(estado,"encendido")==0;
    configRecibida=true;controlarRelay();
}

// ================================================================
// CONTROL RELAY
// ================================================================
void controlarRelay() {
    if (agua.calibrando != calibrandoAnterior) {
        calibrandoAnterior=agua.calibrando;tCambioRelay=millis();
    }
    if (!configRecibida || deviceId<=0 || agua.calibrando) {
        bombaEncendida=false;digitalWrite(RELAY_PIN,RELAY_OFF);return;
    }
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
    Serial1.begin(BAUD_ESP);   // comunicación con ESP8266

    dht.begin();
    agua.begin();
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

        float temp    = dht.readTemperature();
        float humedad = dht.readHumidity();
        float dist    = leerUltrasonico();

        // Una falla del DHT22 no detiene los demás sensores.
        enviarDatos(temp, humedad, dist);
        pedirConfig();
    }

    controlarRelay();
}
// Las esperas de red y muestreo mantienen el riego y el DS18B20 activos.
void atenderLocal(){controlarRelay();agua.update();controlarRelay();}
void esperarAtendiendo(unsigned long ms){unsigned long t=millis();while(millis()-t<ms)atenderLocal();}

void imprimirSeguro(const String &mensaje){
    for(unsigned int i=0;i<mensaje.length();++i){
        while(Serial.availableForWrite()==0)controlarRelay();
        Serial.write(mensaje[i]);controlarRelay();
    }
    Serial.println();
}
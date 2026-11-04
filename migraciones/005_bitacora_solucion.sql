-- ============================================================
-- Migración 005 — Bitácora de la solución nutritiva (dosificación asistida)
-- 03/11/2026 · Hydrosense · Actividad 2, parte de nutrientes
--
-- Qué hace:
--   Crea DOS tablas nuevas. No modifica ni borra ninguna tabla existente;
--   solo las enlaza con `dispositivos`.
--     1) productos_solucion: lo que se agrega a la cubeta (fertilizante,
--        agua, pH- o pH+) y el dato que usa el cálculo de la dosis.
--     2) ajustes_solucion: la bitácora. Una fila cada vez que una persona
--        agrega algo a la cubeta, con las lecturas de antes y lo que
--        recomendó el sistema en ese momento.
--
-- Por qué:
--   En el montaje acordado, la solución se prepara y se corrige a mano.
--   El sistema calcula la dosis y la persona la agrega; esta bitácora deja
--   evidencia de cada ajuste para el análisis de la actividad 5.
--   Los valores "CE y pH después" NO se guardan aquí: se calculan al
--   exportar el Excel a partir de las lecturas de registro_sensores.
--
-- Se puede correr más de una vez: CREATE TABLE IF NOT EXISTS no toca
-- tablas que ya existan. Probada en MySQL 8.0 con la estructura de mydb
-- exportada el 03/11/2026 (servidor 9.7.1).
-- ============================================================
USE mydb;

-- 1) Productos que se agregan a la cubeta (se registran una sola vez)
CREATE TABLE IF NOT EXISTS `productos_solucion` (
  `idProducto`    INT NOT NULL AUTO_INCREMENT,
  `idDispositivo` INT NOT NULL,
  `nombre`        VARCHAR(80) NOT NULL,          -- Ej.: Fertilizante A+B, Agua de la llave
  `tipo`          VARCHAR(20) NOT NULL,          -- fertilizante | agua | ph_bajar | ph_subir | otro
  `unidad`        VARCHAR(4)  NOT NULL,          -- mL | g | L (como se mide al agregarlo)
  `aumento_ce`    DECIMAL(8,4) NULL,             -- Solo fertilizante: mS/cm que sube la CE al
                                                 -- agregar 1 unidad por cada litro de agua.
                                                 -- NULL = aún no se mide; no se calcula cantidad.
  `ce_ms_cm`      DECIMAL(8,4) NULL,             -- Solo agua: CE propia del agua, en mS/cm.
  `nota`          VARCHAR(255) NULL,
  `activo`        TINYINT(1) NOT NULL DEFAULT 1, -- 0 = ya no se usa; la bitácora conserva su nombre
  `creado_en`     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`idProducto`),
  KEY `idx_producto_dispositivo` (`idDispositivo`, `activo`),
  CONSTRAINT `fk_producto_dispositivo` FOREIGN KEY (`idDispositivo`)
    REFERENCES `dispositivos` (`idDispositivo`) ON DELETE CASCADE,
  CONSTRAINT `chk_producto_tipo`    CHECK (`tipo` IN ('fertilizante', 'agua', 'ph_bajar', 'ph_subir', 'otro')),
  CONSTRAINT `chk_producto_unidad`  CHECK (`unidad` IN ('mL', 'g', 'L')),
  CONSTRAINT `chk_producto_aumento` CHECK (`aumento_ce` IS NULL OR `aumento_ce` > 0),
  CONSTRAINT `chk_producto_ce_agua` CHECK (`ce_ms_cm` IS NULL OR `ce_ms_cm` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2) Bitácora: una fila por cada ajuste manual de la solución
CREATE TABLE IF NOT EXISTS `ajustes_solucion` (
  `idAjuste`        BIGINT NOT NULL AUTO_INCREMENT,
  `idDispositivo`   INT NOT NULL,
  `idUsuario`       INT NULL,                    -- Quién lo anotó (sin llave, igual que historial_control_riego)
  `fecha_hora`      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, -- Mismo reloj que registro_sensores
  `tipo`            VARCHAR(20) NOT NULL,        -- solucion_nueva | fertilizante | agua | ph_bajar | ph_subir | otro
  `idProducto`      INT NULL,
  `producto_nombre` VARCHAR(80) NULL,            -- Copia del nombre en ese momento
  `cantidad`        DECIMAL(10,3) NULL,          -- Lo que de verdad se agregó
  `unidad`          VARCHAR(4) NULL,             -- mL | g | L
  `litros_cubeta`   DECIMAL(6,2) NULL,           -- Litros en la cubeta, si se conocen
  -- Lecturas de antes: copia de la última lectura válida al anotar
  `ce_antes`        DECIMAL(8,4) NULL,           -- mS/cm
  `ce_estado`       VARCHAR(32) NULL,            -- ok | sin_calibrar | fuera_rango | ...
  `ph_antes`        DECIMAL(5,2) NULL,
  `temp_agua_antes` DECIMAL(6,2) NULL,           -- °C
  `lecturas_fecha`  DATETIME NULL,               -- Fecha de esas lecturas
  -- Perfil y recomendación del sistema en ese momento
  `cultivo`         VARCHAR(20) NULL,
  `etapa`           VARCHAR(25) NULL,
  `dosis_recomendada_por_litro` DECIMAL(10,4) NULL,
  `recomendacion`   VARCHAR(255) NULL,           -- Texto que mostró el panel
  `detalle_json`    TEXT NULL,                   -- Datos del cálculo: rango, objetivo y fórmula
  `nota`            VARCHAR(255) NULL,
  PRIMARY KEY (`idAjuste`),
  KEY `idx_ajuste_dispositivo_fecha` (`idDispositivo`, `fecha_hora`, `idAjuste`),
  KEY `idx_ajuste_producto` (`idProducto`),
  CONSTRAINT `fk_ajuste_dispositivo` FOREIGN KEY (`idDispositivo`)
    REFERENCES `dispositivos` (`idDispositivo`) ON DELETE CASCADE,
  CONSTRAINT `fk_ajuste_producto` FOREIGN KEY (`idProducto`)
    REFERENCES `productos_solucion` (`idProducto`) ON DELETE SET NULL,
  CONSTRAINT `chk_ajuste_tipo`     CHECK (`tipo` IN ('solucion_nueva', 'fertilizante', 'agua', 'ph_bajar', 'ph_subir', 'otro')),
  CONSTRAINT `chk_ajuste_unidad`   CHECK (`unidad` IS NULL OR `unidad` IN ('mL', 'g', 'L')),
  CONSTRAINT `chk_ajuste_cantidad` CHECK (`cantidad` IS NULL OR `cantidad` > 0),
  CONSTRAINT `chk_ajuste_litros`   CHECK (`litros_cubeta` IS NULL OR `litros_cubeta` > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3) Revisión: debe mostrar 2 tablas y luego 3 llaves.
SELECT TABLE_NAME AS tabla_nueva, ENGINE AS motor
  FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE()
   AND TABLE_NAME IN ('productos_solucion', 'ajustes_solucion');

SELECT CONSTRAINT_NAME AS llave, TABLE_NAME AS tabla, REFERENCED_TABLE_NAME AS enlaza_con
  FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS
 WHERE CONSTRAINT_SCHEMA = DATABASE()
   AND TABLE_NAME IN ('productos_solucion', 'ajustes_solucion');

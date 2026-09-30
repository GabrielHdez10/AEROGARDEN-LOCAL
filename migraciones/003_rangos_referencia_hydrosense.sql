-- ============================================================
-- Migración 003 — Catálogo de rangos de Hydrosense (cilantro y perejil)
-- Crea las tablas de la sección Rangos de app.py y carga los
-- límites de referencia por cultivo, etapa y variable.
-- Para bases YA creadas (local o nube). Se puede correr más de una vez:
-- no borra nada y no modifica filas que ya existan.
-- Funciona en MySQL 8/9 y en MariaDB.
-- ============================================================
USE mydb;

-- 1) Catálogo de referencia (lo leen las rutas /api/rangos/*)
CREATE TABLE IF NOT EXISTS `rangos_referencia_hydrosense` (
  `idRango`  INT NOT NULL AUTO_INCREMENT,
  `cultivo`  VARCHAR(20) NOT NULL,
  `etapa`    VARCHAR(25) NOT NULL,
  `variable` VARCHAR(35) NOT NULL,
  `minimo`   DECIMAL(10,2) NULL,
  `maximo`   DECIMAL(10,2) NULL,
  `unidad`   VARCHAR(30) NOT NULL,
  PRIMARY KEY (`idRango`),
  UNIQUE KEY `uq_cultivo_etapa_variable` (`cultivo`, `etapa`, `variable`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2) Perfil (cultivo y etapa) elegido para cada dispositivo.
--    app.py también la crea al guardar el primer perfil; aquí queda
--    explícita para no depender del permiso CREATE del usuario de la app.
CREATE TABLE IF NOT EXISTS `perfil_rangos_dispositivo` (
  `idDispositivo`  INT NOT NULL PRIMARY KEY,
  `cultivo`        VARCHAR(20) NOT NULL,
  `etapa`          VARCHAR(25) NOT NULL,
  `actualizado_en` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (`idDispositivo`) REFERENCES `dispositivos`(`idDispositivo`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3) Límites de referencia. NULL = límite pendiente (falta medir o calibrar).
--    INSERT IGNORE: si la combinación cultivo/etapa/variable ya existe,
--    se deja como está.
INSERT IGNORE INTO `rangos_referencia_hydrosense`
  (`cultivo`, `etapa`, `variable`, `minimo`, `maximo`, `unidad`)
VALUES
  ('cilantro',   'general',    'ph',               6.20,    6.80,    'pH'),
  ('perejil',    'general',    'ph',               6.00,    6.50,    'pH'),
  ('compartido', 'general',    'ph',               6.20,    6.50,    'pH'),
  ('cilantro',   'inicial',    'ec',               1500.00, 1700.00, 'uS/cm'),
  ('cilantro',   'desarrollo', 'ec',               1700.00, 1900.00, 'uS/cm'),
  ('cilantro',   'media',      'ec',               1900.00, 2100.00, 'uS/cm'),
  ('cilantro',   'final',      'ec',               2100.00, 2300.00, 'uS/cm'),
  ('perejil',    'general',    'ec',               1800.00, 2200.00, 'uS/cm'),
  ('compartido', 'inicial',    'ec',               NULL,    NULL,    'uS/cm'),
  ('compartido', 'media',      'ec',               1900.00, 2100.00, 'uS/cm'),
  ('compartido', 'general',    'temperatura',      10.00,   30.00,   'C'),
  ('compartido', 'general',    'humedad',          40.00,   90.00,   '%'),
  ('compartido', 'general',    'temperatura_agua', 22.22,   23.89,   'C'),
  ('cilantro',   'inicial',    'fotoperiodo',      12.00,   12.00,   'h/dia'),
  ('cilantro',   'desarrollo', 'fotoperiodo',      14.00,   14.00,   'h/dia'),
  ('cilantro',   'media',      'fotoperiodo',      16.00,   16.00,   'h/dia'),
  ('cilantro',   'final',      'fotoperiodo',      14.00,   14.00,   'h/dia'),
  ('cilantro',   'general',    'ppfd',             130.00,  150.00,  'umol/m2/s'),
  ('compartido', 'general',    'luz_ldr',          NULL,    NULL,    'indice 0-5000'),
  ('compartido', 'general',    'distancia',        NULL,    NULL,    'cm');

-- 4) Revisión: debe mostrar 20 (o más, si ya había filas agregadas a mano).
SELECT COUNT(*) AS filas_catalogo FROM `rangos_referencia_hydrosense`;
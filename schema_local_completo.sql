-- ============================================================
-- AeroGarden — Esquema COMPLETO para entorno LOCAL
-- Combina: Esquema mydb.sql + migraciones/001_verificacion_email.sql
--
-- Uso:
--   mysql -u root -p < schema_local_completo.sql
-- o desde phpMyAdmin: Importar > seleccionar este archivo.
--
-- Este archivo existe porque el "Esquema mydb.sql" original NO
-- incluye la tabla `verificaciones_email` ni las columnas
-- `email_verified` / `verified_at` de `usuarios`, que app.py
-- necesita para que el login y el registro funcionen.
-- ============================================================

CREATE DATABASE IF NOT EXISTS `mydb`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE `mydb`;

-- ── 1) Esquema base ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS `usuarios` (
  `idUsuario` INT NOT NULL AUTO_INCREMENT,
  `nombre` VARCHAR(100) NOT NULL,
  `apellido_paterno` VARCHAR(100) NOT NULL,
  `apellido_materno` VARCHAR(100) NOT NULL,
  `correo` VARCHAR(150) NOT NULL,
  `contraseña` VARCHAR(255) NOT NULL,
  `foto_perfil` LONGTEXT NULL,
  `creado_en` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`idUsuario`),
  UNIQUE KEY `correo` (`correo`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `sistema` (
  `idSistema` INT NOT NULL AUTO_INCREMENT,
  `nombre` VARCHAR(100) NOT NULL,
  `descripcion` TEXT,
  `creado_en` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`idSistema`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `sistema` (`idSistema`, `nombre`, `descripcion`)
VALUES (1, 'Sistema Principal', 'Sistema por defecto de AeroGarden')
ON DUPLICATE KEY UPDATE `nombre` = VALUES(`nombre`);

CREATE TABLE IF NOT EXISTS `dispositivos` (
  `idDispositivo` INT NOT NULL AUTO_INCREMENT,
  `nombre` VARCHAR(100) NOT NULL,
  `tipo` VARCHAR(100),
  `idSistema` INT NOT NULL DEFAULT 1,
  `idUsuario` INT NULL,
  `pairing_code` VARCHAR(20) NULL,
  `pairing_usado` TINYINT(1) NOT NULL DEFAULT 0,
  `registrado_en` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`idDispositivo`),
  KEY `idSistema` (`idSistema`),
  KEY `idUsuario` (`idUsuario`),
  FOREIGN KEY (`idSistema`) REFERENCES `sistema`(`idSistema`) ON DELETE RESTRICT ON UPDATE CASCADE,
  FOREIGN KEY (`idUsuario`) REFERENCES `usuarios`(`idUsuario`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `dispositivo_miembros` (
  `idDispositivo` INT NOT NULL,
  `idUsuario` INT NOT NULL,
  `permiso` ENUM('ver','controlar') NOT NULL DEFAULT 'ver',
  `agregado_en` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`idDispositivo`, `idUsuario`),
  KEY `idUsuario` (`idUsuario`),
  FOREIGN KEY (`idDispositivo`) REFERENCES `dispositivos`(`idDispositivo`) ON DELETE CASCADE ON UPDATE CASCADE,
  FOREIGN KEY (`idUsuario`) REFERENCES `usuarios`(`idUsuario`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `config_relay` (
  `idDispositivo` INT NOT NULL,
  `tiempo_on` INT NOT NULL DEFAULT 30,
  `tiempo_off` INT NOT NULL DEFAULT 60,
  `modo` VARCHAR(20) NOT NULL DEFAULT 'automatico',
  `estado_manual` VARCHAR(20) NOT NULL DEFAULT 'apagado',
  PRIMARY KEY (`idDispositivo`),
  FOREIGN KEY (`idDispositivo`) REFERENCES `dispositivos`(`idDispositivo`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `sensores` (
  `idSensore` INT NOT NULL AUTO_INCREMENT,
  `tipo_sensor` VARCHAR(100) NOT NULL,
  `unidad_medida` VARCHAR(30),
  `idDispositivo` INT NOT NULL,
  `registrado_en` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`idSensore`),
  KEY `idDispositivo` (`idDispositivo`),
  FOREIGN KEY (`idDispositivo`) REFERENCES `dispositivos`(`idDispositivo`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `registro_sensores` (
  `idRegistro_sensor` INT NOT NULL AUTO_INCREMENT,
  `valor` DECIMAL(10,4) NOT NULL,
  `fecha_hora` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `idSensor` INT NOT NULL,
  PRIMARY KEY (`idRegistro_sensor`),
  KEY `idSensor` (`idSensor`),
  FOREIGN KEY (`idSensor`) REFERENCES `sensores`(`idSensore`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `parametros_alerta` (
  `idParametro` INT NOT NULL AUTO_INCREMENT,
  `idSensor` INT NOT NULL,
  `nombre` VARCHAR(80) NOT NULL,
  `condicion` ENUM('mayor_que','menor_que','igual_a') NOT NULL DEFAULT 'mayor_que',
  `valor_umbral` DECIMAL(10,2) NOT NULL,
  `prioridad` ENUM('baja','media','alta','critica') NOT NULL DEFAULT 'media',
  `activo` TINYINT(1) NOT NULL DEFAULT 1,
  `creado_en` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`idParametro`),
  KEY `idSensor` (`idSensor`),
  FOREIGN KEY (`idSensor`) REFERENCES `sensores`(`idSensore`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `historial_alertas` (
  `idHistorial` INT NOT NULL AUTO_INCREMENT,
  `idParametro` INT NOT NULL,
  `idSensor` INT NOT NULL,
  `valor_detectado` DECIMAL(10,2) NOT NULL,
  `prioridad` ENUM('baja','media','alta','critica') NOT NULL,
  `estado` ENUM('nueva','vista','resuelta') NOT NULL DEFAULT 'nueva',
  `mensaje` VARCHAR(255),
  `fecha_hora` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `fecha_resolucion` DATETIME NULL,
  PRIMARY KEY (`idHistorial`),
  KEY `idParametro` (`idParametro`),
  KEY `idSensor` (`idSensor`),
  FOREIGN KEY (`idParametro`) REFERENCES `parametros_alerta`(`idParametro`) ON DELETE CASCADE ON UPDATE CASCADE,
  FOREIGN KEY (`idSensor`) REFERENCES `sensores`(`idSensore`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `tipo_cultivo` (
  `idTipo_Cultivo` INT NOT NULL AUTO_INCREMENT,
  `nombre_planta` VARCHAR(100) NOT NULL,
  `descripcion` TEXT,
  PRIMARY KEY (`idTipo_Cultivo`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `tipo_cultivo` (`nombre_planta`, `descripcion`) VALUES
  ('Lechuga', 'Hoja verde de ciclo corto'),
  ('Albahaca', 'Hierba aromática'),
  ('Tomate', 'Fruto de ciclo medio'),
  ('Espinaca', 'Hoja verde rica en hierro'),
  ('Cilantro', 'Hierba aromática de uso culinario');

CREATE TABLE IF NOT EXISTS `cultivos` (
  `idCultivo` INT NOT NULL AUTO_INCREMENT,
  `nombreCultivo` VARCHAR(150) NOT NULL,
  `fecha_siembra` DATE NOT NULL,
  `cantidad` INT NOT NULL,
  `tamano_planta` VARCHAR(50),
  `idTipo_Cultivo` INT NOT NULL,
  `idSistema` INT NOT NULL DEFAULT 1,
  `idUsuario` INT NOT NULL,
  `creado_en` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`idCultivo`),
  KEY `idTipo_Cultivo` (`idTipo_Cultivo`),
  KEY `idSistema` (`idSistema`),
  KEY `idUsuario` (`idUsuario`),
  FOREIGN KEY (`idTipo_Cultivo`) REFERENCES `tipo_cultivo`(`idTipo_Cultivo`) ON DELETE RESTRICT ON UPDATE CASCADE,
  FOREIGN KEY (`idSistema`) REFERENCES `sistema`(`idSistema`) ON DELETE RESTRICT ON UPDATE CASCADE,
  FOREIGN KEY (`idUsuario`) REFERENCES `usuarios`(`idUsuario`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `cosecha` (
  `idCosecha` INT NOT NULL AUTO_INCREMENT,
  `fecha` DATE NOT NULL,
  `cantidad` DECIMAL(10,2) NOT NULL,
  `calidad` VARCHAR(50),
  `observaciones` TEXT,
  `idCultivo` INT NOT NULL,
  PRIMARY KEY (`idCosecha`),
  KEY `idCultivo` (`idCultivo`),
  FOREIGN KEY (`idCultivo`) REFERENCES `cultivos`(`idCultivo`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 2) Migración 001: verificación de correo ───────────────
-- (necesaria para que /api/auth/registrar y /api/auth/login funcionen)

ALTER TABLE `usuarios`
  ADD COLUMN `email_verified` TINYINT(1) NOT NULL DEFAULT 0 AFTER `contraseña`,
  ADD COLUMN `verified_at` DATETIME NULL AFTER `email_verified`;

CREATE TABLE IF NOT EXISTS `verificaciones_email` (
  `idVerificacion` INT NOT NULL AUTO_INCREMENT,
  `correo`      VARCHAR(150) NOT NULL,
  `codigo`      VARCHAR(6)   NOT NULL,
  `proposito`   ENUM('registro','reset') NOT NULL DEFAULT 'registro',
  `usado`       TINYINT(1) NOT NULL DEFAULT 0,
  `verificado`  TINYINT(1) NOT NULL DEFAULT 0,
  `creado_en`   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `expira_en`   DATETIME NOT NULL,
  PRIMARY KEY (`idVerificacion`),
  KEY `idx_correo_proposito` (`correo`, `proposito`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 3) Dispositivo de prueba para emparejar tu Mega ─────────
-- Opcional: crea un dispositivo con pairing_code fijo para que
-- puedas emparejar el Mega sin pasar por la interfaz web.
-- Descomenta y ajusta si lo quieres usar:
--
-- INSERT INTO dispositivos (nombre, tipo, idSistema, idUsuario, pairing_code)
-- VALUES ('AeroGarden Local', 'Arduino Mega + ESP8266', 1, NULL, 'HIDRO-0001');

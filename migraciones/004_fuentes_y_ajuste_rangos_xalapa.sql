-- ============================================================
-- Migración 004 — Fuentes del catálogo y ajuste de rangos (aeroponía, Xalapa)
-- 11/10/2026 · Hydrosense · Actividad 2 · revisión con estudios de aeroponía
-- Requiere la 003 (tabla rangos_referencia_hydrosense con sus 20 filas).
--
-- Qué hace:
--   1) Agrega la columna `fuente` (solo si no existe) para documentar
--      de dónde sale cada rango.
--   2) Ajusta 4 rangos:
--        perejil    · general · ec                1800-2200   -> 1500-1900 uS/cm
--        compartido · inicial · ec                vacío       -> 1500-1700 uS/cm
--        compartido · media   · ec                1900-2100   -> vacío (pendiente)
--        compartido · general · temperatura_agua  22.22-23.89 -> 20.00-25.00 C
--   3) Escribe la fuente de los 20 rangos. Los que no tienen estudio
--      en aeroponía quedan marcados como [Extrapolado de hidroponia].
--
-- Se puede correr más de una vez: no borra filas y siempre deja los
-- mismos valores. Funciona en MySQL 8/9 y en MariaDB.
-- Los textos de `fuente` van sin acentos, igual que los datos de la 003.
--
-- Después de correrla: re-aplica el perfil del dispositivo en la
-- página Rangos. Los límites que ya estaban aplicados no cambian solos.
--
-- Nota para Xalapa: con medias mensuales de 13.3 a 20.0 C (normales
-- SMN 1991-2020, estación Briones), sin calefacción el agua del depósito
-- quedará bajo 20 C buena parte del año. La alerta es correcta: Bantis
-- et al. (2026) resolvieron ese caso calentando el depósito con sensores
-- DS18B20, Arduino y relevadores.
--
-- Referencias completas:
--   Yang, X., Xiao, F., Jiang, P. y Luo, Y. (2025). Calculation and
--     prediction of water requirements for aeroponic cultivation of crops
--     in greenhouses. Horticulturae, 11(9), 1034.
--     https://doi.org/10.3390/horticulturae11091034
--   Rincón, A., Candelo-Becerra, J. E. y Hoyos, F. E. (2022). Modelling of
--     phosphorus accumulation in an aeroponic coriander crop.
--     Int J Agric & Biol Eng, 15(6), 73-79.
--     https://doi.org/10.25165/j.ijabe.20221506.6760
--   Bantis, F., Tostsidis, N., Zervoudakis, G., Koukounaras, A. y
--     Koulopoulos, A. (2026). Root-zone heating boosts the production of
--     mini Romaine lettuce grown in nutrient film technique and
--     aeroponics systems. Plants, 15(3), 422.
--     https://doi.org/10.3390/plants15030422
--   Nguyen, D. T. P., Lu, N., Kagawa, N. y Takagaki, M. (2019).
--     Agronomy, 9(5), 224. https://doi.org/10.3390/agronomy9050224
--     (hidroponía)
--   Currey, C. J., Walters, K. J. y Flax, N. J. (2019). Agronomy, 9(7),
--     389. https://doi.org/10.3390/agronomy9070389 (NFT, hidroponía)
--   Dunn, B. y Singh, H. (2017). Electrical conductivity and pH guide for
--     hydroponics (HLA-6722). Oklahoma State University Extension.
-- ============================================================
USE mydb;

-- 1) Columna `fuente`. MySQL no acepta ADD COLUMN IF NOT EXISTS,
--    así que primero se revisa si ya existe.
SET @existe := (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'rangos_referencia_hydrosense'
                  AND COLUMN_NAME = 'fuente');
SET @sql := IF(@existe = 0,
  'ALTER TABLE `rangos_referencia_hydrosense` ADD COLUMN `fuente` VARCHAR(255) NULL AFTER `unidad`',
  'SELECT ''La columna fuente ya existe'' AS aviso');
PREPARE paso FROM @sql;
EXECUTE paso;
DEALLOCATE PREPARE paso;

-- 2) pH (sin cambio de valor)
UPDATE `rangos_referencia_hydrosense`
   SET `fuente` = 'Yang et al. 2025, Horticulturae 11:1034, p. 5 (aeroponia, cilantro): pH 6.2-6.8. Rincon et al. 2022, IJABE 15(6), pp. 74-75 (aeroponia, cilantro): 5.5-6.5'
 WHERE `cultivo` = 'cilantro' AND `etapa` = 'general' AND `variable` = 'ph';

UPDATE `rangos_referencia_hydrosense`
   SET `fuente` = '[Extrapolado de hidroponia] Dunn y Singh 2017, OSU HLA-6722: pH 6.0-6.5. Currey et al. 2019, Agronomy 9:389 (NFT): pH 6.0. No hay estudio de perejil en aeroponia'
 WHERE `cultivo` = 'perejil' AND `etapa` = 'general' AND `variable` = 'ph';

UPDATE `rangos_referencia_hydrosense`
   SET `fuente` = 'Traslape de dos estudios de cilantro en aeroponia: Yang 2025 p. 5 (6.2-6.8) y Rincon 2022 pp. 74-75 (5.5-6.5). Compatible con perejil 6.0-6.5 (HLA-6722, hidroponia)'
 WHERE `cultivo` = 'compartido' AND `etapa` = 'general' AND `variable` = 'ph';

-- 3) EC del cilantro por etapa (sin cambio de valor)
UPDATE `rangos_referencia_hydrosense`
   SET `fuente` = 'Yang et al. 2025, Horticulturae 11:1034, p. 5 (aeroponia, cilantro): 1.6 +/- 0.1 mS/cm en etapa inicial'
 WHERE `cultivo` = 'cilantro' AND `etapa` = 'inicial' AND `variable` = 'ec';

UPDATE `rangos_referencia_hydrosense`
   SET `fuente` = 'Yang et al. 2025, Horticulturae 11:1034, p. 5 (aeroponia, cilantro): 1.8 +/- 0.1 mS/cm en etapa de desarrollo'
 WHERE `cultivo` = 'cilantro' AND `etapa` = 'desarrollo' AND `variable` = 'ec';

UPDATE `rangos_referencia_hydrosense`
   SET `fuente` = 'Yang et al. 2025, Horticulturae 11:1034, p. 5 (aeroponia, cilantro): 2.0 +/- 0.1 mS/cm en etapa media. El sensor actual no mide desde 2.0 mS/cm'
 WHERE `cultivo` = 'cilantro' AND `etapa` = 'media' AND `variable` = 'ec';

UPDATE `rangos_referencia_hydrosense`
   SET `fuente` = 'Yang et al. 2025, Horticulturae 11:1034, p. 5 (aeroponia, cilantro): 2.2 +/- 0.1 mS/cm en etapa final. El sensor actual no mide desde 2.0 mS/cm'
 WHERE `cultivo` = 'cilantro' AND `etapa` = 'final' AND `variable` = 'ec';

-- 4) EC del perejil y del perfil compartido (CAMBIAN de valor)
UPDATE `rangos_referencia_hydrosense`
   SET `minimo` = 1500.00, `maximo` = 1900.00,
       `fuente` = '[Extrapolado de hidroponia] Currey et al. 2019, Agronomy 9:389 (NFT): EC de 0.5 a 4.0 sin efecto en el peso. Tope 1.9 por el rango del sensor. Antes 1.8-2.2 (HLA-6722)'
 WHERE `cultivo` = 'perejil' AND `etapa` = 'general' AND `variable` = 'ec';

UPDATE `rangos_referencia_hydrosense`
   SET `minimo` = 1500.00, `maximo` = 1700.00,
       `fuente` = 'Traslape de cilantro inicial (Yang 2025 p. 5, aeroponia: 1.5-1.7) y perejil (1.5-1.9, extrapolado de hidroponia)'
 WHERE `cultivo` = 'compartido' AND `etapa` = 'inicial' AND `variable` = 'ec';

UPDATE `rangos_referencia_hydrosense`
   SET `minimo` = NULL, `maximo` = NULL,
       `fuente` = 'Pendiente: cilantro pide 1.9-2.1 (Yang 2025 p. 5) y perejil hasta 1.9; el sensor actual no mide desde 2.0 mS/cm'
 WHERE `cultivo` = 'compartido' AND `etapa` = 'media' AND `variable` = 'ec';

-- 5) Aire (sin cambio de valor)
UPDATE `rangos_referencia_hydrosense`
   SET `fuente` = 'Yang 2025 p. 6 (aeroponia, cilantro): max 29.6 C, min -1.4 C. Rincon 2022 p. 74 (aeroponia, cilantro): 16.9-27.6 C. El piso de 10 C avisa crecimiento lento, no dano'
 WHERE `cultivo` = 'compartido' AND `etapa` = 'general' AND `variable` = 'temperatura';

UPDATE `rangos_referencia_hydrosense`
   SET `fuente` = 'Yang 2025 p. 6 (aeroponia, cilantro): HR de 28 a 95 %, media 77.7 %. Sin umbral publicado para estos cultivos. Valido solo si el DHT22 mide el aire de las hojas'
 WHERE `cultivo` = 'compartido' AND `etapa` = 'general' AND `variable` = 'humedad';

-- 6) Temperatura del agua del depósito (CAMBIA de valor)
UPDATE `rangos_referencia_hydrosense`
   SET `minimo` = 20.00, `maximo` = 25.00,
       `fuente` = 'Bantis et al. 2026, Plants 15(3):422, Tabla 1 (aeroponia, lechuga): mejor crecimiento con minimos de 18 C o mas y promedio de 24.9 C sin trastornos. Nguyen et al. 2019, Agronomy 9:224 (hidroponia): 25 C optimo en cilantro'
 WHERE `cultivo` = 'compartido' AND `etapa` = 'general' AND `variable` = 'temperatura_agua';

-- 7) Luz (sin cambio de valor)
UPDATE `rangos_referencia_hydrosense`
   SET `fuente` = 'Yang et al. 2025, Horticulturae 11:1034, p. 5 (aeroponia, cilantro): 12 h de luz en etapa inicial'
 WHERE `cultivo` = 'cilantro' AND `etapa` = 'inicial' AND `variable` = 'fotoperiodo';

UPDATE `rangos_referencia_hydrosense`
   SET `fuente` = 'Yang et al. 2025, Horticulturae 11:1034, p. 5 (aeroponia, cilantro): 14 h de luz en etapa de desarrollo'
 WHERE `cultivo` = 'cilantro' AND `etapa` = 'desarrollo' AND `variable` = 'fotoperiodo';

UPDATE `rangos_referencia_hydrosense`
   SET `fuente` = 'Yang et al. 2025, Horticulturae 11:1034, p. 5 (aeroponia, cilantro): 16 h de luz en etapa media'
 WHERE `cultivo` = 'cilantro' AND `etapa` = 'media' AND `variable` = 'fotoperiodo';

UPDATE `rangos_referencia_hydrosense`
   SET `fuente` = 'Yang et al. 2025, Horticulturae 11:1034, p. 5 (aeroponia, cilantro): 14 h de luz en etapa final'
 WHERE `cultivo` = 'cilantro' AND `etapa` = 'final' AND `variable` = 'fotoperiodo';

UPDATE `rangos_referencia_hydrosense`
   SET `fuente` = 'Yang et al. 2025, Horticulturae 11:1034, p. 5 (aeroponia, cilantro): PPFD de 140 +/- 10 umol/m2/s'
 WHERE `cultivo` = 'cilantro' AND `etapa` = 'general' AND `variable` = 'ppfd';

-- 8) Pendientes (siguen vacíos)
UPDATE `rangos_referencia_hydrosense`
   SET `fuente` = 'Pendiente: el LDR no mide luz fotosintetica (PAR) y no hay equivalencia publicada con PPFD'
 WHERE `cultivo` = 'compartido' AND `etapa` = 'general' AND `variable` = 'luz_ldr';

UPDATE `rangos_referencia_hydrosense`
   SET `fuente` = 'Pendiente: depende de la geometria del deposito de este prototipo'
 WHERE `cultivo` = 'compartido' AND `etapa` = 'general' AND `variable` = 'distancia';

-- 9) Revisión: debe mostrar filas = 20 y sin_fuente = 0,
--    y después la tabla completa con su fuente.
SELECT COUNT(*) AS filas, SUM(`fuente` IS NULL) AS sin_fuente
  FROM `rangos_referencia_hydrosense`;

SELECT `idRango`, `cultivo`, `etapa`, `variable`, `minimo`, `maximo`, `unidad`, `fuente`
  FROM `rangos_referencia_hydrosense`
 ORDER BY `idRango`;

-- ============================================================
-- Migración 002 — El catálogo de cultivos queda en Cilantro y Perejil
-- Para bases YA creadas (local o Aiven). Se puede correr más de una vez.
-- ============================================================
USE mydb;

-- 1) Asegurar que existan los dos tipos
INSERT INTO tipo_cultivo (nombre_planta, descripcion)
SELECT 'Cilantro', 'Hierba aromática de uso culinario' FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM tipo_cultivo WHERE LOWER(nombre_planta) = 'cilantro');

INSERT INTO tipo_cultivo (nombre_planta, descripcion)
SELECT 'Perejil', 'Hierba aromática de hoja lisa o rizada' FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM tipo_cultivo WHERE LOWER(nombre_planta) = 'perejil');

-- 2) Borrar los demás tipos que NO tengan cultivos sembrados
DELETE FROM tipo_cultivo
WHERE LOWER(nombre_planta) NOT IN ('cilantro', 'perejil')
  AND idTipo_Cultivo NOT IN (SELECT DISTINCT idTipo_Cultivo FROM cultivos);

-- 3) Revisión: si aquí aparece algún renglón, hay cultivos ya sembrados
--    de otro tipo. Se conservan (por integridad) pero ya no se muestran
--    en la lista de tipos ni se pueden crear nuevos.
SELECT t.idTipo_Cultivo, t.nombre_planta, COUNT(c.idCultivo) AS cultivos_sembrados
FROM tipo_cultivo t
LEFT JOIN cultivos c ON c.idTipo_Cultivo = t.idTipo_Cultivo
WHERE LOWER(t.nombre_planta) NOT IN ('cilantro', 'perejil')
GROUP BY t.idTipo_Cultivo, t.nombre_planta;

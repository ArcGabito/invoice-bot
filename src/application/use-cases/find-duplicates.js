/**
 * Caso de uso HU-04: lista duplicados exactos (mismo SHA-256 en varias rutas)
 * y posibles duplicados (misma clave de negocio en archivos distintos).
 * Nunca elimina nada: el usuario decide.
 */
export function findDuplicates({ deps }) {
  const { repository } = deps;
  return {
    exact: groupBy(repository.listExactDuplicates(), (r) => r.document_id),
    possible: groupBy(repository.listPossibleDuplicates(), (r) => r.business_key),
  };
}

function groupBy(rows, keyFn) {
  const groups = new Map();
  for (const row of rows) {
    const key = keyFn(row);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  return [...groups.entries()].map(([key, items]) => ({ key, items }));
}

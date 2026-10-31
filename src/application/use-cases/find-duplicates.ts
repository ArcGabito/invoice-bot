import type { Deps } from '../deps.ts';

/**
 * Caso de uso HU-04: lista duplicados exactos (mismo SHA-256 en varias rutas)
 * y posibles duplicados (misma clave de negocio en archivos distintos).
 * Nunca elimina nada: el usuario decide.
 */
export async function findDuplicates({ deps }: { deps: Pick<Deps, 'repository'> }) {
  const { repository } = deps;
  return {
    exact: groupBy(await repository.listExactDuplicates(), (r) => r.document_id),
    possible: groupBy(await repository.listPossibleDuplicates(), (r) => r.business_key),
  };
}

function groupBy<T, K>(rows: readonly T[], keyFn: (row: T) => K): { key: K; items: T[] }[] {
  const groups = new Map<K, T[]>();
  for (const row of rows) {
    const key = keyFn(row);
    const items = groups.get(key) ?? [];
    items.push(row);
    groups.set(key, items);
  }
  return [...groups.entries()].map(([key, items]) => ({ key, items }));
}

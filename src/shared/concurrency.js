/**
 * Ejecuta `fn` sobre cada elemento con como máximo `limit` tareas en curso.
 * Los errores de una tarea no detienen a las demás: `fn` debe capturarlos.
 */
export async function mapWithConcurrency(items, limit, fn) {
  const results = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const index = next++;
      results[index] = await fn(items[index], index);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}

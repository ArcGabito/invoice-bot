import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    // Borra las bases de pruebas de ejecuciones anteriores (tests/global-setup.ts).
    globalSetup: ['tests/global-setup.ts'],
    // Cierra la conexión a SQL Server de cada archivo de pruebas.
    setupFiles: ['tests/setup.ts'],
    // Las pruebas de integración hablan con SQL Server: más margen que los 5 s por defecto.
    testTimeout: 30_000,
    hookTimeout: 60_000,
  },
});

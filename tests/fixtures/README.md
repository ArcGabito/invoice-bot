# Archivos de prueba

- `mail-app-cert.pem`: certificado autofirmado y su clave privada, generados solo para las pruebas del
  conector de Microsoft 365 con certificado (`tests/integration/mail-enterprise.test.ts`). No da acceso
  a nada: no está registrado en ninguna aplicación.
- `mail-app-cert-wrong-key.pem`: el mismo certificado con una clave que no le corresponde.

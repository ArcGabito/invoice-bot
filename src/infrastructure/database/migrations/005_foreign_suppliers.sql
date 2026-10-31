-- Comprobantes de proveedores del exterior (tipo 91, no domiciliados):
-- no tienen RUC, así que se guarda su identificador fiscal y su país.
ALTER TABLE invoices ADD COLUMN supplier_tax_id TEXT;
ALTER TABLE invoices ADD COLUMN supplier_country TEXT;

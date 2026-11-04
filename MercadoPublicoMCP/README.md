# Servidor MCP de Mercado Público (v1.2)

Permite que Claude Desktop consulte en lenguaje normal las APIs públicas de ChileCompra:
licitaciones, órdenes de compra, proveedores, organismos y Compra Ágil.

- **Archivo del servidor:** [mercadopublico_mcp.py](mercadopublico_mcp.py). Usa solo la biblioteca estándar de Python 3.10 o superior (no hay que instalar nada con pip).
- **Manual paso a paso:** [Guia_Claude_MercadoPublico_MCP.docx](Guia_Claude_MercadoPublico_MCP.docx).

## Cómo descargar el archivo del servidor

1. Abre [mercadopublico_mcp.py](mercadopublico_mcp.py).
2. Arriba a la derecha del recuadro con el código, haz clic en el botón de la flecha hacia abajo («Download raw file»).
3. El archivo queda en tu carpeta Descargas. Sigue el manual desde el Paso 4.2.

## ¿Sirve para cualquier organismo?

Sí. Nada en el archivo está fijado a un organismo en particular: cada consulta recibe el código del
organismo (`codigo_organismo`) o del proveedor (`codigo_proveedor`). Los códigos se obtienen con
`buscar_organismo` y `buscar_proveedor`.

**Unidad de compra:** la API de ChileCompra no permite filtrar por unidad de compra, así que el
servidor tampoco tiene ese filtro. Se consulta por organismo y la unidad se ve en el detalle de
cada licitación u orden de compra (`detalle_licitacion`, `detalle_orden_compra`). En Compra Ágil
la unidad aparece en el resumen (`unidad_compra`).

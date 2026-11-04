"""
Servidor MCP de Mercado Público (ChileCompra) para Claude Desktop.

Permite que Claude consulte, en lenguaje normal, las APIs públicas de:
  - Licitaciones            (api.mercadopublico.cl, v1)
  - Órdenes de compra       (api.mercadopublico.cl, v1)
  - Proveedores y organismos (api.mercadopublico.cl, v1)
  - Compra Ágil             (api2.mercadopublico.cl, v2)

El ticket NO se escribe en este archivo: se lee de la variable de entorno
MP_TICKET, que se define en claude_desktop_config.json.

No necesita instalar nada con pip: usa solo la biblioteca estándar de Python 3.10+.

Uso:
  python mercadopublico_mcp.py            -> inicia el servidor (lo hace Claude)
  python mercadopublico_mcp.py --probar   -> prueba el ticket y la conexión

Versión 1.2 · noviembre 2026 (sin dependencias externas)
"""

from __future__ import annotations

import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Optional

import json
import traceback
import urllib.error
import urllib.parse
import urllib.request

import inspect
import typing

VERSION = "1.2"
TICKET = os.environ.get("MP_TICKET", "").strip()

URL_V1 = "https://api.mercadopublico.cl/servicios/v1/publico"
URL_V1_EMPRESAS = "https://api.mercadopublico.cl/servicios/v1/Publico/Empresas"
URL_CA = "https://api2.mercadopublico.cl/v2/compra-agil"

TIMEOUT = 45  # segundos
PAUSA_V1 = 1.2  # la API v1 rechaza peticiones simultáneas: se espacian las consultas
MAX_DIAS_RANGO = 31

ESTADOS_LICITACION = ["publicada", "cerrada", "desierta", "adjudicada", "revocada", "suspendida", "todos", "activas"]
ESTADOS_OC = [
    "enviadaproveedor", "aceptada", "cancelada", "recepcionconforme", "pendienterecepcion",
    "recepcionaceptadacialmente", "recepecionconformeincompleta", "todos",
]
ESTADOS_CA = ["publicada", "cerrada", "desierta", "cancelada", "proveedor_seleccionado"]

REGIONES = {
    1: "Tarapacá", 2: "Antofagasta", 3: "Atacama", 4: "Coquimbo", 5: "Valparaíso",
    6: "O'Higgins", 7: "Maule", 8: "Biobío", 9: "Araucanía", 10: "Los Lagos",
    11: "Aysén", 12: "Magallanes y Antártica", 13: "Metropolitana", 14: "Los Ríos",
    15: "Arica y Parinacota", 16: "Ñuble",
}

INSTRUCCIONES = (
        "Herramientas para consultar Mercado Público (ChileCompra): licitaciones, órdenes de compra (OC), "
        "proveedores, organismos públicos y Compra Ágil. Las fechas se pueden escribir como dd-mm-aaaa. "
        "Para filtrar por proveedor u organismo primero obtén su código con buscar_proveedor o "
        "buscar_organismo. Las búsquedas por fecha devuelven listados resumidos; para el detalle "
        "completo usa las herramientas detalle_*. Cada consulta gasta cuota del ticket (10.000 por día "
        "en licitaciones/OC): evita recorrer muchos días o muchos detalles si no es necesario. "
        "Nunca muestres el ticket."
)

# ---------------------------------------------------------------------------
# Mini servidor MCP (protocolo JSON-RPC por entrada/salida estándar).
# Usa solo la biblioteca estándar de Python: no requiere instalar nada con pip.
# ---------------------------------------------------------------------------

PROTOCOLOS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
_HERRAMIENTAS: dict[str, dict] = {}


def _tipo_json(anotacion: Any) -> dict:
    origen = typing.get_origin(anotacion)
    if origen is typing.Union or str(origen) == "<class 'types.UnionType'>":
        args = [a for a in typing.get_args(anotacion) if a is not type(None)]
        return _tipo_json(args[0]) if args else {}
    return {str: {"type": "string"}, int: {"type": "integer"}, bool: {"type": "boolean"},
            float: {"type": "number"}}.get(anotacion, {"type": "string"})


def herramienta(funcion):
    """Registra una función como herramienta MCP y arma su esquema a partir de la firma."""
    hints = typing.get_type_hints(funcion)
    propiedades, requeridos = {}, []
    for nombre, par in inspect.signature(funcion).parameters.items():
        esquema = dict(_tipo_json(hints.get(nombre, str)))
        if par.default is inspect.Parameter.empty:
            requeridos.append(nombre)
        elif par.default is not None:
            esquema["default"] = par.default
        propiedades[nombre] = esquema
    esquema_entrada = {"type": "object", "properties": propiedades}
    if requeridos:
        esquema_entrada["required"] = requeridos
    _HERRAMIENTAS[funcion.__name__] = {
        "funcion": funcion,
        "definicion": {
            "name": funcion.__name__,
            "description": inspect.cleandoc(funcion.__doc__ or ""),
            "inputSchema": esquema_entrada,
            "annotations": {"readOnlyHint": True, "openWorldHint": True},
        },
    }
    return funcion


def _enviar(mensaje: dict) -> None:
    sys.stdout.buffer.write((json.dumps(mensaje, ensure_ascii=True) + "\n").encode("ascii"))
    sys.stdout.buffer.flush()


def _log(texto: str) -> None:
    sys.stderr.write(_ocultar(texto) + "\n")
    sys.stderr.flush()


def _atender(msg: dict) -> Optional[dict]:
    metodo = msg.get("method")
    params = msg.get("params") or {}
    if metodo == "initialize":
        pedido = params.get("protocolVersion")
        return {
            "protocolVersion": pedido if pedido in PROTOCOLOS else PROTOCOLOS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mercadopublico", "version": VERSION},
            "instructions": INSTRUCCIONES,
        }
    if metodo == "ping":
        return {}
    if metodo == "tools/list":
        return {"tools": [h["definicion"] for h in _HERRAMIENTAS.values()]}
    if metodo == "tools/call":
        nombre = params.get("name")
        h = _HERRAMIENTAS.get(nombre)
        if not h:
            raise LookupError(f"Herramienta desconocida: {nombre}")
        argumentos = params.get("arguments") or {}
        validos = inspect.signature(h["funcion"]).parameters
        argumentos = {k: v for k, v in argumentos.items() if k in validos}
        try:
            resultado = h["funcion"](**argumentos)
            es_error = isinstance(resultado, dict) and "error" in resultado
        except Exception as e:  # nunca botar el servidor por una consulta
            _log(traceback.format_exc())
            resultado, es_error = {"error": f"Error interno: {_ocultar(str(e))}"}, True
        texto = json.dumps(resultado, ensure_ascii=False, indent=1, default=str)
        return {"content": [{"type": "text", "text": texto}], "isError": es_error}
    raise NotImplementedError(metodo)


def ejecutar_servidor() -> None:
    _log(f"Servidor MCP Mercado Público v{VERSION} iniciado (Python {sys.version.split()[0]}).")
    if not TICKET:
        _log("Aviso: falta MP_TICKET en la configuración; las consultas responderán con error.")
    while True:
        linea = sys.stdin.buffer.readline()
        if not linea:
            break
        linea = linea.strip()
        if not linea:
            continue
        try:
            msg = json.loads(linea.decode("utf-8"))
        except ValueError:
            _enviar({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON no válido"}})
            continue
        mensajes = msg if isinstance(msg, list) else [msg]
        for m in mensajes:
            if not isinstance(m, dict) or "method" not in m:
                continue  # respuestas o basura: se ignoran
            es_notificacion = "id" not in m
            try:
                resultado = _atender(m)
                if not es_notificacion:
                    _enviar({"jsonrpc": "2.0", "id": m["id"], "result": resultado})
            except NotImplementedError:
                if not es_notificacion:
                    _enviar({"jsonrpc": "2.0", "id": m["id"],
                             "error": {"code": -32601, "message": f"Método no soportado: {m.get('method')}"}})
            except Exception as e:
                _log(traceback.format_exc())
                if not es_notificacion:
                    _enviar({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32603, "message": _ocultar(str(e))}})


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------



class _Respuesta:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def json(self) -> Any:
        return json.loads(self.text)


def _http_get(url: str, params: dict[str, Any], headers: Optional[dict[str, str]] = None) -> _Respuesta:
    """GET con la biblioteca estándar. Devuelve también las respuestas de error (4xx/5xx)."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    cab = {"User-Agent": f"mercadopublico-mcp/{VERSION}", "Accept": "application/json"}
    cab.update(headers or {})
    req = urllib.request.Request(url, headers=cab)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return _Respuesta(r.status, r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return _Respuesta(e.code, e.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ErrorRed(str(getattr(e, "reason", e)))

_ultima_v1 = 0.0
_cache_organismos: list[dict] | None = None


class ErrorConsulta(Exception):
    pass


class ErrorRed(Exception):
    pass


def _ocultar(texto: str) -> str:
    return texto.replace(TICKET, "***") if TICKET else texto


def _exigir_ticket() -> None:
    if not TICKET:
        raise ErrorConsulta(
            "No hay ticket configurado. Agrega \"MP_TICKET\" en la sección \"env\" del servidor "
            "en claude_desktop_config.json y reinicia Claude Desktop."
        )


def _get_v1(url: str, params: dict[str, Any]) -> dict:
    """Consulta la API v1 (ticket en la URL) respetando la pausa entre peticiones."""
    global _ultima_v1
    _exigir_ticket()
    params = {k: v for k, v in params.items() if v not in (None, "")}
    params["ticket"] = TICKET
    for intento in range(3):
        espera = PAUSA_V1 - (time.monotonic() - _ultima_v1)
        if espera > 0:
            time.sleep(espera)
        try:
            r = _http_get(url, params)
        except ErrorRed as e:
            _ultima_v1 = time.monotonic()
            if intento == 2:
                raise ErrorConsulta(f"No se pudo conectar con Mercado Público: {_ocultar(str(e))}")
            time.sleep(3)
            continue
        _ultima_v1 = time.monotonic()
        texto = r.text
        if "simult" in texto.lower() and intento < 2:  # "peticiones simultáneas"
            time.sleep(3)
            continue
        try:
            datos = r.json()
        except ValueError:
            raise ErrorConsulta(f"Respuesta no válida de Mercado Público (HTTP {r.status_code}): {_ocultar(texto[:300])}")
        if isinstance(datos, dict) and "Codigo" in datos and "Mensaje" in datos and "Listado" not in datos:
            raise ErrorConsulta(f"Mercado Público respondió: {datos.get('Mensaje')} (código {datos.get('Codigo')})")
        if r.status_code >= 400:
            raise ErrorConsulta(f"Mercado Público respondió HTTP {r.status_code}: {_ocultar(texto[:300])}")
        return datos
    raise ErrorConsulta("Mercado Público no respondió después de 3 intentos.")


def _get_ca(ruta: str = "", params: Optional[dict[str, Any]] = None) -> dict:
    """Consulta la API de Compra Ágil v2 (ticket en el header)."""
    _exigir_ticket()
    params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    ultimo_error = ""
    for intento in range(2):
        try:
            r = _http_get(URL_CA + ruta, params, {"ticket": TICKET})
        except ErrorRed as e:
            ultimo_error = _ocultar(str(e))
            time.sleep(3)
            continue
        if r.status_code in (502, 503, 504) and intento == 0:
            ultimo_error = f"HTTP {r.status_code}"
            time.sleep(3)
            continue
        try:
            datos = r.json()
        except ValueError:
            raise ErrorConsulta(f"Respuesta no válida de Compra Ágil (HTTP {r.status_code}).")
        if r.status_code >= 400 or datos.get("success") == "NOK":
            errores = datos.get("errors") or []
            msg = "; ".join(str(e.get("mensaje")) for e in errores if isinstance(e, dict)) or f"HTTP {r.status_code}"
            if r.status_code == 404:
                msg = "No existe una Compra Ágil con ese código (o no es pública)."
            if r.status_code == 429:
                msg = "Se agotó la cuota diaria del ticket para Compra Ágil. Intenta mañana."
            raise ErrorConsulta(f"Compra Ágil respondió: {msg}")
        return datos.get("payload") or {}
    raise ErrorConsulta(f"No se pudo obtener respuesta de Compra Ágil ({ultimo_error}). Revisa tu conexión a Internet; si la conexión está bien, la API puede estar saturada: reintenta en unos minutos.")


def _fecha(texto: Optional[str]) -> Optional[date]:
    """Acepta 'hoy', 'ayer', dd-mm-aaaa, dd/mm/aaaa, aaaa-mm-dd o ddmmaaaa."""
    if not texto:
        return None
    t = texto.strip().lower()
    if t == "hoy":
        return date.today()
    if t == "ayer":
        return date.today() - timedelta(days=1)
    for formato in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d%m%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(t, formato).date()
        except ValueError:
            pass
    raise ErrorConsulta(f"No entendí la fecha «{texto}». Usa el formato dd-mm-aaaa, por ejemplo 15-10-2026.")


def _ddmmaaaa(d: date) -> str:
    return d.strftime("%d%m%Y")


def _rut_formato_mp(rut: str) -> str:
    """Convierte cualquier RUT al formato que exige la API: 76.123.456-7"""
    limpio = re.sub(r"[^0-9kK]", "", rut).upper()
    if len(limpio) < 2:
        raise ErrorConsulta(f"RUT no válido: «{rut}».")
    cuerpo, dv = limpio[:-1], limpio[-1]
    cuerpo_puntos = f"{int(cuerpo):,}".replace(",", ".")
    return f"{cuerpo_puntos}-{dv}"


def _sin_tildes(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def _recortar(lista: list, maximo: int) -> tuple[list, bool]:
    maximo = max(1, min(int(maximo or 100), 1000))
    return lista[:maximo], len(lista) > maximo


def _error(e: Exception) -> dict:
    return {"error": str(e)}


# ---------------------------------------------------------------------------
# Herramientas: proveedores y organismos
# ---------------------------------------------------------------------------

@herramienta
def buscar_proveedor(rut: str) -> dict:
    """Obtiene el código de empresa (CodigoProveedor) y el nombre de un proveedor a partir de su RUT.

    El RUT puede venir con o sin puntos y guion (ej: 76123456-7 o 761234567).
    El código obtenido se usa en buscar_licitaciones y buscar_ordenes_compra (parámetro codigo_proveedor).
    """
    try:
        rut_mp = _rut_formato_mp(rut)
        datos = _get_v1(f"{URL_V1_EMPRESAS}/BuscarProveedor", {"rutempresaproveedor": rut_mp})
        empresas = datos.get("listaEmpresas") or []
        if not empresas and rut_mp.endswith("K"):
            datos = _get_v1(f"{URL_V1_EMPRESAS}/BuscarProveedor", {"rutempresaproveedor": rut_mp[:-1] + "k"})
            empresas = datos.get("listaEmpresas") or []
        if not empresas:
            return {"rut": rut_mp, "encontrado": False, "mensaje": "El RUT no aparece como proveedor en Mercado Público."}
        return {"rut": rut_mp, "encontrado": True, "proveedores": empresas}
    except ErrorConsulta as e:
        return _error(e)


@herramienta
def buscar_organismo(texto: str, max_resultados: int = 30) -> dict:
    """Busca organismos públicos compradores por nombre (ej: 'carabineros', 'hospital de talca')
    y devuelve su código (CodigoOrganismo), que sirve para filtrar licitaciones y órdenes de compra.
    """
    global _cache_organismos
    try:
        if _cache_organismos is None:
            datos = _get_v1(f"{URL_V1_EMPRESAS}/BuscarComprador", {})
            _cache_organismos = datos.get("listaEmpresas") or []
        palabras = _sin_tildes(texto).split()
        hallados = [
            o for o in _cache_organismos
            if all(p in _sin_tildes(str(o.get("NombreEmpresa", ""))) for p in palabras)
        ]
        lista, recortado = _recortar(hallados, max_resultados)
        return {"total_encontrados": len(hallados), "recortado": recortado, "organismos": lista}
    except ErrorConsulta as e:
        return _error(e)


# ---------------------------------------------------------------------------
# Herramientas: licitaciones
# ---------------------------------------------------------------------------

@herramienta
def buscar_licitaciones(
    fecha: Optional[str] = None,
    estado: Optional[str] = None,
    codigo_organismo: Optional[str] = None,
    codigo_proveedor: Optional[str] = None,
    max_resultados: int = 100,
) -> dict:
    """Lista licitaciones de un día (resumen: código, nombre, estado, fecha de cierre).

    - fecha: dd-mm-aaaa, 'hoy' o 'ayer'. Si se omite, se usa el día de hoy.
    - estado: publicada, cerrada, desierta, adjudicada, revocada, suspendida, todos,
      o 'activas' (todas las publicadas vigentes; en ese caso se ignora la fecha).
    - codigo_organismo: código del comprador (obtenerlo con buscar_organismo).
    - codigo_proveedor: código de empresa del proveedor (obtenerlo con buscar_proveedor).
    Para ver montos, ítems, adjudicación y fechas completas usa detalle_licitacion.
    """
    try:
        params: dict[str, Any] = {}
        if estado:
            estado = estado.strip().lower()
            if estado not in ESTADOS_LICITACION:
                return {"error": f"Estado no válido. Usa uno de: {', '.join(ESTADOS_LICITACION)}"}
        if estado == "activas":
            params["estado"] = "activas"
        else:
            d = _fecha(fecha) or date.today()
            params["fecha"] = _ddmmaaaa(d)
            if estado:
                params["estado"] = estado
        params["CodigoOrganismo"] = codigo_organismo
        params["CodigoProveedor"] = codigo_proveedor
        datos = _get_v1(f"{URL_V1}/licitaciones.json", params)
        listado = datos.get("Listado") or []
        lista, recortado = _recortar(listado, max_resultados)
        return {
            "consulta": {k: v for k, v in params.items() if v},
            "cantidad_total": datos.get("Cantidad", len(listado)),
            "recortado": recortado,
            "licitaciones": lista,
        }
    except ErrorConsulta as e:
        return _error(e)


@herramienta
def detalle_licitacion(codigo: str) -> dict:
    """Devuelve la ficha completa de una licitación a partir de su código (ej: 1509-5-L114):
    comprador, fechas, montos, ítems, estado y adjudicación."""
    try:
        datos = _get_v1(f"{URL_V1}/licitaciones.json", {"codigo": codigo.strip()})
        listado = datos.get("Listado") or []
        if not listado:
            return {"codigo": codigo, "encontrada": False}
        return {"codigo": codigo, "encontrada": True, "licitacion": listado[0]}
    except ErrorConsulta as e:
        return _error(e)


# ---------------------------------------------------------------------------
# Herramientas: órdenes de compra
# ---------------------------------------------------------------------------

def _consulta_oc(params: dict[str, Any]) -> dict:
    return _get_v1(f"{URL_V1}/ordenesdecompra.json", params)


@herramienta
def buscar_ordenes_compra(
    fecha: Optional[str] = None,
    estado: Optional[str] = None,
    codigo_organismo: Optional[str] = None,
    codigo_proveedor: Optional[str] = None,
    max_resultados: int = 100,
) -> dict:
    """Lista órdenes de compra (OC) emitidas en un día (resumen: código, nombre, estado).

    - fecha: dd-mm-aaaa, 'hoy' o 'ayer'. Si se omite, se usa hoy.
    - estado: enviadaproveedor, aceptada, cancelada, recepcionconforme, pendienterecepcion,
      recepcionaceptadacialmente, recepecionconformeincompleta o todos.
    - codigo_organismo / codigo_proveedor: ver buscar_organismo y buscar_proveedor.
    Para varios días seguidos usa ordenes_compra_por_rango. Para el detalle usa detalle_orden_compra.
    Las OC de Compra Ágil tienen el sufijo AG en el código (ej: 1057532-156-AG26).
    """
    try:
        if estado:
            estado = estado.strip().lower()
            if estado not in ESTADOS_OC:
                return {"error": f"Estado no válido. Usa uno de: {', '.join(ESTADOS_OC)}"}
        d = _fecha(fecha) or date.today()
        params = {
            "fecha": _ddmmaaaa(d),
            "estado": estado,
            "CodigoOrganismo": codigo_organismo,
            "CodigoProveedor": codigo_proveedor,
        }
        datos = _consulta_oc(params)
        listado = datos.get("Listado") or []
        lista, recortado = _recortar(listado, max_resultados)
        return {
            "consulta": {k: v for k, v in params.items() if v},
            "cantidad_total": datos.get("Cantidad", len(listado)),
            "recortado": recortado,
            "ordenes_compra": lista,
        }
    except ErrorConsulta as e:
        return _error(e)


@herramienta
def ordenes_compra_por_rango(
    fecha_desde: str,
    fecha_hasta: str,
    codigo_organismo: Optional[str] = None,
    codigo_proveedor: Optional[str] = None,
    max_resultados: int = 300,
) -> dict:
    """Junta las órdenes de compra de varios días (máximo 31) de un organismo y/o un proveedor.

    Hace una consulta por día, por lo que tarda unos segundos por día y gasta cuota del ticket.
    Se recomienda indicar codigo_organismo o codigo_proveedor para no traer miles de OC.
    Fechas en formato dd-mm-aaaa.
    """
    try:
        d1, d2 = _fecha(fecha_desde), _fecha(fecha_hasta)
        if d1 is None or d2 is None:
            return {"error": "Indica fecha_desde y fecha_hasta."}
        if d2 < d1:
            d1, d2 = d2, d1
        dias = (d2 - d1).days + 1
        if dias > MAX_DIAS_RANGO:
            return {"error": f"El rango tiene {dias} días; el máximo es {MAX_DIAS_RANGO}. Divide la consulta por meses."}
        todas: list[dict] = []
        dias_con_error: list[str] = []
        d = d1
        while d <= d2:
            try:
                datos = _consulta_oc({
                    "fecha": _ddmmaaaa(d),
                    "CodigoOrganismo": codigo_organismo,
                    "CodigoProveedor": codigo_proveedor,
                })
                for oc in datos.get("Listado") or []:
                    oc = dict(oc)
                    oc["FechaConsulta"] = d.strftime("%d-%m-%Y")
                    todas.append(oc)
            except ErrorConsulta as e:
                if "icket" in str(e):  # ticket inválido o bloqueado: no tiene sentido seguir
                    return _error(e)
                dias_con_error.append(f"{d.strftime('%d-%m-%Y')}: {e}")
            d += timedelta(days=1)
        lista, recortado = _recortar(todas, max_resultados)
        return {
            "desde": d1.strftime("%d-%m-%Y"),
            "hasta": d2.strftime("%d-%m-%Y"),
            "dias_consultados": dias,
            "cantidad_total": len(todas),
            "recortado": recortado,
            "dias_con_error": dias_con_error,
            "ordenes_compra": lista,
        }
    except ErrorConsulta as e:
        return _error(e)


@herramienta
def detalle_orden_compra(codigo: str) -> dict:
    """Devuelve la ficha completa de una orden de compra a partir de su código (ej: 2097-241-SE14):
    comprador, proveedor, montos, ítems, fechas y estado."""
    try:
        datos = _consulta_oc({"codigo": codigo.strip()})
        listado = datos.get("Listado") or []
        if not listado:
            return {"codigo": codigo, "encontrada": False}
        return {"codigo": codigo, "encontrada": True, "orden_compra": listado[0]}
    except ErrorConsulta as e:
        return _error(e)


# ---------------------------------------------------------------------------
# Herramientas: Compra Ágil (API v2)
# ---------------------------------------------------------------------------

def _iso(d: date, fin: bool = False) -> str:
    return d.strftime("%Y-%m-%dT23:59:59Z" if fin else "%Y-%m-%dT00:00:00Z")


def _resumen_ca(item: dict) -> dict:
    est = item.get("estado") or {}
    fechas = item.get("fechas") or {}
    montos = item.get("montos") or {}
    inst = item.get("institucion") or {}
    resumen = item.get("resumen") or {}
    return {
        "codigo": item.get("codigo"),
        "nombre": item.get("nombre"),
        "estado": est.get("glosa") or est.get("codigo"),
        "llamado": (item.get("convocatoria") or {}).get("descripcion"),
        "fecha_publicacion": fechas.get("fecha_publicacion"),
        "fecha_cierre": fechas.get("fecha_cierre"),
        "monto_disponible_clp": montos.get("monto_disponible_clp"),
        "organismo": inst.get("organismo_comprador"),
        "rut_organismo": inst.get("rut"),
        "unidad_compra": inst.get("unidad_compra"),
        "region": inst.get("nombre_region"),
        "ofertas_recibidas": resumen.get("total_ofertas_recibidas"),
    }


@herramienta
def buscar_compras_agiles(
    texto: Optional[str] = None,
    codigo: Optional[str] = None,
    estado: Optional[str] = None,
    region: Optional[str] = None,
    publicado_desde: Optional[str] = None,
    publicado_hasta: Optional[str] = None,
    cambios_ultimas_horas: Optional[int] = None,
    texto_organismo: Optional[str] = None,
    pagina: int = 1,
    tamano_pagina: int = 15,
    ordenar_por: str = "FechaPublicacion",
) -> dict:
    """Busca procesos de Compra Ágil (compras menores a 100 UTM) en la API v2.

    - texto: palabras clave en nombre/descripción (ej: 'calzado', 'uniformes').
    - codigo: código exacto (ej: 1057539-228-COT26). No combinar con texto.
    - estado: publicada, cerrada, desierta, cancelada, proveedor_seleccionado (varios separados por coma).
    - region: números de región 1 a 16 separados por coma (13 = Metropolitana).
    - publicado_desde / publicado_hasta: dd-mm-aaaa.
    - cambios_ultimas_horas: procesos con cambios en las últimas N horas.
    - texto_organismo: filtra el resultado por nombre de organismo (ej: 'carabineros'); la API no
      permite filtrar por organismo, así que se filtra sobre la página obtenida.
    - pagina / tamano_pagina (entre 10 y 50; páginas grandes pueden fallar por tiempo de espera).
    Si no se indica ningún filtro, se usan los procesos publicados en los últimos 7 días.
    La API puede tardar 10 a 25 segundos en responder.
    """
    try:
        params: dict[str, Any] = {
            "numero_pagina": max(1, int(pagina or 1)),
            "tamano_pagina": max(10, min(int(tamano_pagina or 15), 50)),  # la API rechaza menos de 10
            "ordenar_por": ordenar_por if ordenar_por in ("FechaPublicacion", "FechaUltimaModificacion") else "FechaPublicacion",
        }
        if codigo:
            params["id"] = codigo.strip()
        elif texto:
            params["q"] = texto.strip()
        if estado:
            estados = [e.strip().lower() for e in estado.split(",") if e.strip()]
            malos = [e for e in estados if e not in ESTADOS_CA]
            if malos:
                return {"error": f"Estado no válido: {', '.join(malos)}. Usa: {', '.join(ESTADOS_CA)}"}
            params["estado"] = ",".join(estados)
        if region:
            params["region"] = ",".join(r.strip() for r in str(region).split(",") if r.strip())
        d1, d2 = _fecha(publicado_desde), _fecha(publicado_hasta)
        if d1:
            params["publicado_desde"] = _iso(d1)
        if d2:
            params["publicado_hasta"] = _iso(d2, fin=True)
        if cambios_ultimas_horas:
            params["ttl_cambio_ms"] = int(cambios_ultimas_horas) * 3_600_000
        filtros = ("id", "q", "estado", "region", "publicado_desde", "publicado_hasta", "ttl_cambio_ms")
        if not any(k in params for k in filtros):
            params["publicado_desde"] = _iso(date.today() - timedelta(days=7))
        payload = _get_ca("", params)
        items = [_resumen_ca(i) for i in payload.get("items") or []]
        if texto_organismo:
            claves = _sin_tildes(texto_organismo).split()
            items = [i for i in items if all(c in _sin_tildes(str(i.get("organismo") or "")) for c in claves)]
        return {
            "consulta": params,
            "paginacion": payload.get("paginacion"),
            "compras_agiles": items,
        }
    except ErrorConsulta as e:
        return _error(e)


@herramienta
def detalle_compra_agil(codigo: str, incluir_cotizaciones: bool = True) -> dict:
    """Ficha completa de una Compra Ágil (ej: 1057539-228-COT26): descripción, presupuesto,
    productos solicitados, proveedores que cotizaron con sus montos, orden de compra asociada y motivos.
    Nota: la API no informa qué cotización ganó; las cotizaciones son visibles desde el estado Cerrada.
    """
    try:
        payload = _get_ca("/" + codigo.strip())
        if not incluir_cotizaciones and "proveedores_cotizando" in payload:
            payload = dict(payload)
            payload["proveedores_cotizando"] = f"{len(payload['proveedores_cotizando'])} cotizaciones (omitidas)"
        return {"codigo": codigo, "compra_agil": payload}
    except ErrorConsulta as e:
        return _error(e)


# ---------------------------------------------------------------------------
# Referencia (no consulta la API)
# ---------------------------------------------------------------------------

@herramienta
def referencia_codigos() -> dict:
    """Tablas de referencia: estados de licitación, OC y Compra Ágil, códigos de región y
    tipos de orden de compra. No consulta la API ni gasta cuota."""
    return {
        "estados_licitacion": {"5": "Publicada", "6": "Cerrada", "7": "Desierta", "8": "Adjudicada",
                               "18": "Revocada", "19": "Suspendida"},
        "estados_orden_compra": {"4": "Enviada a proveedor", "5": "En proceso", "6": "Aceptada",
                                 "9": "Cancelada", "12": "Recepción conforme", "13": "Pendiente de recepcionar",
                                 "14": "Recepcionada parcialmente", "15": "Recepción conforme incompleta"},
        "estados_compra_agil": ESTADOS_CA,
        "regiones": REGIONES,
        "sufijos_orden_compra": {"AG": "Compra Ágil", "SE": "Sin emisión automática", "CM": "Convenio Marco",
                                 "D1": "Trato directo proveedor único", "R1": "OC menor a 3 UTM"},
        "formato_fechas": "Las herramientas aceptan dd-mm-aaaa, 'hoy' o 'ayer'.",
    }


# ---------------------------------------------------------------------------
# Prueba manual desde la consola
# ---------------------------------------------------------------------------

def _probar() -> int:
    print(f"Servidor MCP Mercado Público v{VERSION}")
    print(f"Python {sys.version.split()[0]} · archivo: {os.path.abspath(__file__)}")
    if not TICKET:
        print("\n[X] Falta el ticket. En esta ventana escribe:  set MP_TICKET=TU_TICKET  y vuelve a probar.")
        return 1
    print(f"Ticket: ****{TICKET[-4:]}\n")
    ok = True
    print("1) API de Órdenes de compra (ChileCompra, hoy) ... ", end="", flush=True)
    r = buscar_ordenes_compra(fecha="hoy", codigo_organismo="6945", max_resultados=1)
    if "error" in r:
        ok = False
        print("ERROR\n   " + r["error"])
    else:
        print(f"OK ({r['cantidad_total']} OC)")
    print("2) API de Compra Ágil (últimas 24 h) ............. ", end="", flush=True)
    r = buscar_compras_agiles(cambios_ultimas_horas=24, tamano_pagina=10)
    if "error" in r:
        ok = False
        print("ERROR\n   " + r["error"])
    else:
        total = (r.get("paginacion") or {}).get("total_resultados", "?")
        print(f"OK ({total} procesos)")
    print("\nTodo listo. Puedes cerrar esta ventana." if ok else "\nRevisa los errores de arriba.")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--probar" in sys.argv:
        sys.exit(_probar())
    ejecutar_servidor()

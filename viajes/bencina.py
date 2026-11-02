"""Consulta y normalización de los precios de combustibles de Chile.

Fuente por defecto: `combustible.json` de RX Noticias (recopilación CC BY 4.0 de datos de la CNE / Bencina en Línea), que se
descarga de FUENTE_URL. Ese archivo no trae identificador ni coordenadas de estación: el id es una clave interna (comuna + marca
+ dirección) y las coordenadas salen de una tabla (ver `_tabla_coordenadas`). La API directa de Bencina en Línea
(GET {API_BASE}/busqueda_estacion_filtro y /marca_ciudadano) sigue disponible con BENCINA_FUENTE=api, pero no es la opción por defecto.

Formato de salida de cada estación (el mismo para cualquier fuente):

    {"id", "marca", "region", "comuna", "direccion", "lat", "lon",
     "precios": {"93": {"precio": 1586.0, "fecha": "2026-10-01 08:45:00", "atencion": "AutoServicio"}}}

`lat`/`lon` son None si no hay coordenadas confiables; `precios` solo incluye los precios 93/95/97 que pasaron la
validación (los demás se consideran "sin dato"). Todos los combustibles quedan en `combustibles`.
"""
import hashlib
import math
import statistics
import time
from collections import Counter, defaultdict
import json
import logging
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as zona
from pathlib import Path
from functools import lru_cache

import requests
import urllib3
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

log = logging.getLogger(__name__)

# Fuente de los datos: "rx" (por defecto, RX Noticias) o "api" (API directa de Bencina en Línea, ya no recomendada).
FUENTE = os.environ.get("BENCINA_FUENTE", "rx").strip().lower()
FUENTE_URL = os.environ.get("BENCINA_FUENTE_URL", "https://rxnoticias.com/datos/combustible.json")
API_BASE = os.environ.get("BENCINA_API_BASE", "https://api.bencinaenlinea.cl/api")
TIMEOUT = float(os.environ.get("BENCINA_TIMEOUT", "15"))
CACHE_SEGUNDOS = int(os.environ.get("BENCINA_CACHE_SEGUNDOS", "3600"))  # datos "frescos"
REINTENTO_SEGUNDOS = 60  # tras un fallo no se vuelve a consultar durante este tiempo
ARCHIVO_LOCAL = os.environ.get("BENCINA_JSON_LOCAL", "")  # JSON con el formato de la muestra
# Verificación del certificado TLS de la API. Solo se desactiva a propósito (BENCINA_VERIFICAR_TLS=0), por ejemplo si el
# certificado del servidor está vencido; mientras tanto la página avisa que la conexión no está verificada.
VERIFICAR_TLS = os.environ.get("BENCINA_VERIFICAR_TLS", "1") != "0"
USER_AGENT = "proyectobencina/1.0 (proyecto universitario USM; uso ciudadano)"

# Respaldo permanente: respaldo.json (y respaldo.previo.json con la copia anterior) dentro de la carpeta de la caché. No caduca.
CARPETA_RESPALDO = Path(getattr(settings, "BENCINA_RESPALDO_DIR", ".cache_bencina"))
MAX_BYTES = int(os.environ.get("BENCINA_MAX_BYTES", str(25 * 1024 * 1024)))  # tamaño máximo de una respuesta de la API (ya descomprimida)
VERSION_RESPALDO = 1
MIN_ESTACIONES = 10  # una descarga con menos estaciones que esto no se considera válida
FRACCION_MIN = 0.5   # ni la que pierde más de la mitad de las estaciones (o de las estaciones con precio) de la copia anterior
FRACCION_MIN_RX = 0.8  # con RX Noticias se exige más: no se acepta una descarga que pierda más del 20 % de las estaciones, de las que tienen
                       # precio o de los registros de precio de la referencia (el respaldo o, si no hay, los datos iniciales del proyecto)

COMBUSTIBLES = ("93", "95", "97")
RANGO_PRECIO = (500, 2500)  # $/L; fuera de este rango se descarta (ceros, 99999, etc.)
MAX_DIAS = int(os.environ.get("BENCINA_MAX_DIAS", "30"))  # precios más antiguos no se usan
# Datos que viajan con el proyecto (solo lectura): copia de combustible.json (último recurso si no hay respaldo ni descarga)
# y tabla inicial de coordenadas por estación.
ARCHIVO_INICIAL = Path(__file__).parent / "data" / "combustible_inicial.json"
ARCHIVO_SEMILLA = Path(__file__).parent / "data" / "coordenadas_semilla.json"
MAX_KM_COMUNA = 150  # una coordenada a más de esto de la mediana de las demás estaciones de su comuna se considera errónea
# Geocodificación opcional con Nominatim (OpenStreetMap), apagada por defecto. Política de uso: 1 consulta/s como máximo,
# User-Agent propio y guardar los resultados para no repetir consultas.
NOMINATIM_ACTIVO = os.environ.get("BENCINA_NOMINATIM", "0") == "1"
NOMINATIM_URL = os.environ.get("BENCINA_NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
NOMINATIM_MAX = int(os.environ.get("BENCINA_NOMINATIM_MAX", "5"))  # estaciones que se consultan como máximo en cada descarga
NOMINATIM_CONTACTO = os.environ.get("BENCINA_NOMINATIM_CONTACTO", "")  # correo o URL de contacto para el User-Agent
RANGO_LAT = (-56.5, -17.0)  # Chile (incluye Isla de Pascua e Isla Robinson Crusoe)
RANGO_LON = (-110.0, -66.0)

if not VERIFICAR_TLS:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    log.warning("BENCINA_VERIFICAR_TLS=0: las consultas a la API no verifican el certificado TLS.")

K_FRESCO, K_ERROR = "bencina:fresco", "bencina:error"


@dataclass
class Datos:
    estaciones: list = field(default_factory=list)
    fuente: str = "error"  # api | respaldo | archivo | error
    consultado: object = None  # datetime (UTC) de la consulta a la fuente
    aviso: str = ""
    descartados: dict = field(default_factory=dict)
    completa: bool = True  # False: una parte (p. ej. las marcas) no se pudo actualizar y se reutilizó de la copia anterior
    generado: object = None  # datetime (UTC) en que la fuente generó el archivo (RX Noticias); no es la fecha de los precios

    @property
    def ok(self):
        return bool(self.estaciones)

    @property
    def n_precios_descartados(self):
        return sum(v for k, v in self.descartados.items() if k.startswith("precio"))


# ---------- normalización ----------
def ahora():
    """Hora actual (UTC, sin zona). Aparte para poder fijarla en las pruebas."""
    return timezone.now().replace(tzinfo=None)


def parsear_fecha(texto):
    try:
        return datetime.fromisoformat(str(texto).strip()).replace(tzinfo=None)
    except ValueError:
        return None


@lru_cache(maxsize=8192)
def normalizar_texto(texto):
    """Minúsculas y sin tildes, para comparar nombres de comunas/estaciones."""
    s = unicodedata.normalize("NFD", str(texto or ""))
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


def a_float(valor):
    """Acepta número o texto (con coma decimal y espacios). None si no es válido."""
    if isinstance(valor, bool) or valor is None:
        return None
    try:
        n = float(str(valor).strip().replace(",", "."))
    except ValueError:
        return None
    return n if n == n and n not in (float("inf"), float("-inf")) else None


def normalizar_coordenadas(lat, lon):
    la, lo = a_float(lat), a_float(lon)
    if la is None or lo is None:
        return None, None
    if not (RANGO_LAT[0] <= la <= RANGO_LAT[1] and RANGO_LON[0] <= lo <= RANGO_LON[1]):
        return None, None
    return la, lo


def grupo_combustible(nombre_corto):
    """'A93' (asistido) y '93' (autoservicio) son el mismo combustible: '93'."""
    c = str(nombre_corto or "").strip().upper()
    c = c[1:] if c.startswith("A") and c[1:] in COMBUSTIBLES else c
    return c if c in COMBUSTIBLES else None


CAMPOS_ESTACION = {"id", "marca", "region", "comuna", "direccion", "latitud", "longitud", "combustibles"}
CAMPOS_COMBUSTIBLE = {"nombre_corto", "nombre_largo", "tipo_atencion_nombre", "unidad_cobro", "precio", "precio_fecha"}


def normalizar_estacion(raw, marcas, descartados=None, ahora_=None):
    """Convierte una estación cruda de la API al formato común; None si no sirve."""
    descartados = descartados if descartados is not None else {}
    if not isinstance(raw, dict) or raw.get("id") is None:
        descartados["estacion_invalida"] = descartados.get("estacion_invalida", 0) + 1
        return None

    precios = {}
    todos = []  # todos los combustibles tal como llegan (DI, KE, GNC, A93...), sin filtros: el recomendador usa solo `precios`
    for c in raw.get("combustibles") or []:
        if not isinstance(c, dict):
            continue
        todos.append({
            "codigo": str(c.get("nombre_corto") or "").strip(), "nombre": c.get("nombre_largo"),
            "atencion": c.get("tipo_atencion_nombre") or "", "unidad": c.get("unidad_cobro"),
            "precio": a_float(c.get("precio")), "fecha": c.get("precio_fecha"),
            "extra": {k: v for k, v in c.items() if k not in CAMPOS_COMBUSTIBLE},
        })
        grupo = grupo_combustible(c.get("nombre_corto"))
        if not grupo:
            continue
        precio = a_float(c.get("precio"))
        if precio is None or not (RANGO_PRECIO[0] <= precio <= RANGO_PRECIO[1]):
            descartados["precio_invalido"] = descartados.get("precio_invalido", 0) + 1
            continue
        fecha = parsear_fecha(c.get("precio_fecha"))
        if fecha and ahora_ and (ahora_ - fecha).days > MAX_DIAS:  # sin fecha: no se puede saber, se conserva
            descartados["precio_desactualizado"] = descartados.get("precio_desactualizado", 0) + 1
            continue
        actual = precios.get(grupo)
        if actual is None or precio < actual["precio"]:  # 93 vs A93: se conserva el más barato
            precios[grupo] = {
                "precio": precio,
                "fecha": (str(c.get("precio_fecha")).strip() or None) if c.get("precio_fecha") else None,
                "atencion": c.get("tipo_atencion_nombre") or "",
            }

    lat, lon = normalizar_coordenadas(raw.get("latitud"), raw.get("longitud"))
    if lat is None:
        descartados["sin_coordenadas"] = descartados.get("sin_coordenadas", 0) + 1

    return {
        "id": raw["id"],
        "marca": marcas.get(raw.get("marca")) or "Sin marca",
        "region": str(raw.get("region") or "").strip(),
        "comuna": str(raw.get("comuna") or "").strip(),
        "direccion": str(raw.get("direccion") or "").strip(),
        "lat": lat,
        "lon": lon,
        "precios": precios,
        "combustibles": todos,
        "extra": {k: v for k, v in raw.items() if k not in CAMPOS_ESTACION},  # cualquier otro campo que entregue la API
    }


def normalizar_respuesta(estaciones_json, marcas_json=None, ahora_=None):
    """(estaciones normalizadas, descartados). Lanza ValueError si la respuesta no es usable."""
    data = estaciones_json.get("data") if isinstance(estaciones_json, dict) else None
    if not isinstance(data, list) or not data:
        raise ValueError("La respuesta no trae estaciones.")
    marcas = {}
    if isinstance(marcas_json, dict) and isinstance(marcas_json.get("data"), list):
        marcas = {m.get("id"): m.get("nombre") for m in marcas_json["data"] if isinstance(m, dict)}
    descartados = {}
    ahora_ = ahora_ or ahora()
    estaciones, vistos = [], set()
    for r in data:
        e = normalizar_estacion(r, marcas, descartados, ahora_)
        if not e:
            continue
        clave = (str(e["id"]), e["lat"], e["lon"], e["direccion"])  # solo se descartan filas idénticas repetidas
        if clave in vistos:
            descartados["estacion_duplicada"] = descartados.get("estacion_duplicada", 0) + 1
            continue
        vistos.add(clave)
        estaciones.append(e)
    if not estaciones:
        raise ValueError("Ninguna estación de la respuesta es válida.")
    return estaciones, descartados


# ---------- fuente RX Noticias (combustible.json) ----------
COMBUSTIBLES_RX = {"93": "Bencina 93", "95": "Bencina 95", "97": "Bencina 97", "DI": "Diésel", "KE": "Kerosene"}


@lru_cache(maxsize=16384)
def _norm_clave(texto):
    """Mayúsculas, sin tildes y sin puntuación (cada grupo de signos pasa a un espacio): «Av. Pérez-Rosales» → «AV PEREZ ROSALES»."""
    s = unicodedata.normalize("NFD", str(texto or ""))
    return re.sub(r"[^A-Z0-9]+", " ", "".join(c for c in s if unicodedata.category(c) != "Mn").upper()).strip()


def clave_estacion(codigo_comuna, marca, direccion):
    """Clave interna de una estación: código de comuna + marca + dirección normalizados. NO es un identificador oficial: si la
    estación cambia de marca o de dirección, la clave cambia."""
    return f"{str(codigo_comuna or '').strip()}|{_norm_clave(marca)}|{_norm_clave(direccion)}"


def id_estacion(clave):
    """Id interno, estable mientras no cambie la clave."""
    return "rx" + hashlib.sha256(clave.encode("utf-8")).hexdigest()[:12]


def _fecha_utc(texto):
    """Fecha ISO a datetime UTC sin zona (sin zona = ya se toma como UTC). None si no es válida."""
    try:
        f = datetime.fromisoformat(str(texto).strip())
    except ValueError:
        return None
    return f.astimezone(zona.utc).replace(tzinfo=None) if f.tzinfo else f


def _leer_rx(contenido, ahora_):
    """(generado, comunas) de un combustible.json. ValueError si no tiene la estructura esperada o la fecha de generación no sirve."""
    if not isinstance(contenido, dict):
        raise ValueError("el JSON no es un objeto")
    comunas = contenido.get("comunas")
    if not isinstance(comunas, list) or not comunas:
        raise ValueError("el JSON no trae comunas")
    generado = _fecha_utc(contenido.get("generado"))
    if generado is None:
        raise ValueError("el JSON no trae una fecha de generación válida")
    if generado > ahora_ + timedelta(days=1):  # una fecha futura bloquearía todas las actualizaciones siguientes
        raise ValueError(f"la fecha de generación ({generado:%Y-%m-%d %H:%M}) está en el futuro")
    return generado, comunas


def _indice_previo(previo):
    """Estaciones de la copia anterior por (comuna, marca, dirección) normalizadas; las claves repetidas se omiten por ambiguas."""
    indice, repetidas = {}, set()
    for e in (previo or {}).get("estaciones") or []:
        if not isinstance(e, dict):
            continue
        k = (_norm_clave(e.get("comuna")), _norm_clave(e.get("marca")), _norm_clave(e.get("direccion")))
        if k in indice:
            repetidas.add(k)
        indice[k] = e
    for k in repetidas:
        del indice[k]
    return indice


def _combustibles_ajenos(anterior, catalogo):
    """Combustibles de la copia anterior que esta fuente no entrega (GLP, GNC...), para no perderlos. Los que RX sí cubre (93, 95,
    97, diésel y kerosene, con o sin la «A» de asistido) los reemplaza RX."""
    ajenos = []
    for c in (anterior or {}).get("combustibles") or []:
        if not isinstance(c, dict):
            continue
        codigo = str(c.get("codigo") or "").strip()
        base = codigo[1:] if codigo.upper().startswith("A") and codigo[1:].upper() in catalogo else codigo
        if base.upper() in catalogo:
            continue
        ajenos.append({**c, "extra": {**(c.get("extra") or {}), "origen": "copia anterior"}})
    return ajenos


def _km(lat1, lon1, lat2, lon2):
    f1, f2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((f2 - f1) / 2) ** 2 + math.cos(f1) * math.cos(f2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(a))


def _quitar_coordenadas_incoherentes(estaciones):
    """Quita las coordenadas que quedan a más de MAX_KM_COMUNA de la mediana de las demás estaciones de su comuna (solo se juzga
    con 5 o más estaciones con coordenadas). Devuelve cuántas quitó."""
    por_comuna = defaultdict(list)
    for e in estaciones:
        if e["lat"] is not None:
            por_comuna[e["extra"].get("codigo_comuna") or e["comuna"]].append(e)
    quitadas = 0
    for grupo in por_comuna.values():
        if len(grupo) < 5:
            continue
        lat, lon = statistics.median(e["lat"] for e in grupo), statistics.median(e["lon"] for e in grupo)
        for e in grupo:
            if _km(lat, lon, e["lat"], e["lon"]) > MAX_KM_COMUNA:
                e["lat"] = e["lon"] = None
                quitadas += 1
    return quitadas


def normalizar_rx(contenido, previo=None, ahora_=None):
    """(estaciones, descartados, generado) a partir de un combustible.json de RX Noticias. Lanza ValueError si no es usable.

    - Los precios antiguos se conservan con su fecha: la antigüedad se muestra, no se usa para descartar.
    - Un precio con `atipico: true`, no numérico o fuera de RANGO_PRECIO se descarta; la estación se conserva igual.
    - Sin fecha en un precio no se inventa una: queda en None.
    - Coordenadas: tabla (semilla + caché) y, si no, la copia anterior; si la clave es ambigua o no hay coincidencia, None.
    - Lo que la copia anterior tenga y esta fuente no entregue (GLP, GNC...) se conserva en `combustibles`.
    """
    ahora_ = ahora_ or ahora()
    generado, comunas = _leer_rx(contenido, ahora_)
    cat = contenido.get("combustibles")
    catalogo = {**COMBUSTIBLES_RX, **({str(k).upper(): str(v) for k, v in cat.items()} if isinstance(cat, dict) else {})}
    descartados = {}

    def contar(motivo):
        descartados[motivo] = descartados.get(motivo, 0) + 1

    crudas = []
    for c in comunas:
        if not isinstance(c, dict):
            continue
        codigo, comuna, region = (str(c.get(k) or "").strip() for k in ("codigo_comuna", "comuna", "region"))
        for e in c.get("estaciones") or []:
            direccion = str(e.get("direccion") or "").strip() if isinstance(e, dict) else ""
            if not direccion or not codigo or not isinstance(e.get("precios"), dict):
                contar("estacion_invalida")
                continue
            marca = str(e.get("marca") or "").strip() or "Sin marca"
            crudas.append((codigo, comuna, region, marca, direccion, e["precios"], clave_estacion(codigo, marca, direccion)))
    if not crudas:
        raise ValueError("Ninguna estación del archivo es válida.")

    veces = Counter(c[-1] for c in crudas)
    tabla, anteriores = _tabla_coordenadas(), _indice_previo(previo)
    estaciones, vistas = [], Counter()
    for codigo, comuna, region, marca, direccion, crudos, clave in crudas:
        repetida = veces[clave] > 1  # dos estaciones con la misma clave: no se sabe cuál es cuál, no se les asignan coordenadas
        vistas[clave] += 1
        if repetida:
            contar("clave_repetida")
        precios, todos = {}, []
        for k, p in crudos.items():
            k = str(k).strip().upper()
            precio = a_float(p.get("precio")) if isinstance(p, dict) else None
            if isinstance(p, dict) and p.get("atipico"):
                contar("precio_atipico")
                continue
            if precio is None or not (RANGO_PRECIO[0] <= precio <= RANGO_PRECIO[1]):
                contar("precio_invalido")
                continue
            fecha = str(p.get("fecha") or "").strip()
            fecha = fecha if parsear_fecha(fecha) else None  # sin fecha válida no se inventa una
            atencion = "AutoServicio" if p.get("autoservicio") else "Asistido"
            todos.append({"codigo": k, "nombre": catalogo.get(k, k), "atencion": atencion, "unidad": "$/L", "precio": precio,
                          "fecha": fecha, "extra": {"vigente": p.get("vigente")}})
            if grupo_combustible(k):
                precios[grupo_combustible(k)] = {"precio": precio, "fecha": fecha, "atencion": atencion}
        anterior = anteriores.get((_norm_clave(comuna), _norm_clave(marca), _norm_clave(direccion)))
        todos += _combustibles_ajenos(anterior, catalogo)
        lat = lon = None
        if not repetida:
            par = tabla.get(clave)
            lat, lon = normalizar_coordenadas(*par) if par else (None, None)
            if lat is None and anterior:
                lat, lon = normalizar_coordenadas(anterior.get("lat"), anterior.get("lon"))
        estaciones.append({
            "id": id_estacion(clave if vistas[clave] == 1 else f"{clave}#{vistas[clave]}"),
            "marca": marca, "region": region, "comuna": comuna, "direccion": direccion, "lat": lat, "lon": lon,
            "precios": precios, "combustibles": todos,
            "extra": {"clave": clave, "codigo_comuna": codigo, **({"clave_repetida": True} if repetida else {})},
        })
    _completar_con_nominatim(estaciones)
    quitadas = _quitar_coordenadas_incoherentes(estaciones)
    if quitadas:
        descartados["coordenada_dudosa"] = quitadas
    sin = sum(1 for e in estaciones if e["lat"] is None)
    if sin:
        descartados["sin_coordenadas"] = sin
    return estaciones, descartados, generado


# ---------- coordenadas: tabla inicial + caché persistente + (opcional) Nominatim ----------
_memo = {}  # ruta -> (firma del archivo, contenido): evita releer varios MB en cada carga de la página


def _json_memo(ruta):
    """Contenido JSON de un archivo (recordado mientras no cambie su fecha ni su tamaño). None si no existe o no se puede leer."""
    try:
        est = ruta.stat()
        firma = (est.st_mtime_ns, est.st_size)
        if _memo.get(ruta, (None,))[0] == firma:
            return _memo[ruta][1]
        with open(ruta, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("No se pudo leer %s: %s", ruta.name, _sin_credenciales(e))
        return None
    _memo[ruta] = (firma, doc)
    return doc


def _ruta_coordenadas():
    return CARPETA_RESPALDO / "coordenadas.json"


def _tabla_coordenadas():
    """{clave de estación: (lat, lon)}. Une la tabla inicial que viaja con el proyecto (snapshot del 30-10-2026 de
    diland9/precios-combustibles-chile, datos CNE/SEC) con la caché persistente `coordenadas.json`, que gana si hay conflicto."""
    tabla = {}
    for doc in (_json_memo(ARCHIVO_SEMILLA), _json_memo(_ruta_coordenadas())):
        coords = doc.get("coords") if isinstance(doc, dict) else None
        for clave, v in (coords.items() if isinstance(coords, dict) else ()):
            par = (v.get("lat"), v.get("lon")) if isinstance(v, dict) else v
            if isinstance(par, (list, tuple)) and len(par) == 2:
                tabla[clave] = tuple(par)
    return tabla


def _geocodificar(est):
    """(lat, lon) que da Nominatim para la estación, o None. Solo acepta una coincidencia clara: dirección con número, un único
    resultado en Chile con el mismo número de casa y la misma comuna. Lanza requests.RequestException si falla la consulta."""
    direccion = est["direccion"]
    numero = re.search(r"(?<![\w-])(\d{1,5})(?![\w-])", direccion)
    if not numero or re.search(r"(?i)\b(km|kil[oó]metro|ruta|s/n|sn)\b", direccion):
        return None  # sin número de casa o dirección de carretera: no hay forma de confirmar la coincidencia
    calle = direccion[: numero.start()].strip(" ,.-")
    if not calle:
        return None
    agente = USER_AGENT + (f" contacto: {NOMINATIM_CONTACTO}" if NOMINATIM_CONTACTO else "")
    resp = requests.get(
        NOMINATIM_URL, timeout=min(TIMEOUT, 10), verify=VERIFICAR_TLS, headers={"User-Agent": agente, "Accept-Language": "es"},
        params={"street": f"{numero.group(1)} {calle}", "city": est["comuna"], "state": est["region"], "country": "Chile",
                "format": "jsonv2", "addressdetails": 1, "limit": 3},
    )
    resp.raise_for_status()
    comuna = _norm_clave(est["comuna"])
    resultados = resp.json()
    if not isinstance(resultados, list):
        return None
    buenos = []
    for r in resultados:
        if not isinstance(r, dict) or not isinstance(r.get("address"), dict):
            continue
        dir_ = r["address"]
        lugares = {_norm_clave(dir_.get(k)) for k in ("city", "town", "village", "municipality", "suburb", "county", "city_district")}
        if dir_.get("country_code") == "cl" and str(dir_.get("house_number", "")).strip() == numero.group(1) and comuna in lugares:
            buenos.append(r)
    if len(buenos) != 1:
        return None  # ninguno o varios: ambiguo, no se asigna
    lat, lon = normalizar_coordenadas(buenos[0].get("lat"), buenos[0].get("lon"))
    return (lat, lon) if lat is not None else None


def _completar_con_nominatim(estaciones):
    """Consulta Nominatim por las estaciones sin coordenadas (si BENCINA_NOMINATIM=1), de a NOMINATIM_MAX por descarga y a 1 consulta
    por segundo, y guarda cada resultado (también los «sin resultado», que no se repiten antes de 30 días) en coordenadas.json."""
    if not NOMINATIM_ACTIVO:
        return
    doc = _json_memo(_ruta_coordenadas())
    doc = doc if isinstance(doc, dict) else {}
    coords, vacias = dict(doc.get("coords") or {}), dict(doc.get("sin_resultado") or {})
    limite, consultadas, hubo_cambios = ahora() - timedelta(days=30), 0, False
    for e in estaciones:
        clave = e["extra"]["clave"]
        if e["lat"] is not None or e["extra"].get("clave_repetida") or clave in coords:
            continue
        visto = parsear_fecha(vacias.get(clave))
        if (visto and visto > limite) or consultadas >= NOMINATIM_MAX:
            continue
        if consultadas:
            time.sleep(1.1)
        consultadas += 1
        try:
            par = _geocodificar(e)
        except Exception as ex:  # es un complemento: pase lo que pase, la descarga de precios sigue
            log.warning("Nominatim falló (%s): se detiene la geocodificación de esta descarga.", _sin_credenciales(ex))
            break
        if par:
            coords[clave] = {"lat": par[0], "lon": par[1], "origen": "nominatim", "fecha": ahora().date().isoformat()}
            e["lat"], e["lon"] = par
        else:
            vacias[clave] = ahora().isoformat()
        hubo_cambios = True
    if hubo_cambios:
        try:
            CARPETA_RESPALDO.mkdir(parents=True, exist_ok=True)
            _escribir_atomico(_ruta_coordenadas(), json.dumps({"version": 1, "coords": coords, "sin_resultado": vacias}, ensure_ascii=False))
        except OSError as ex:
            log.error("No se pudo guardar coordenadas.json: %s", _sin_credenciales(ex))


# ---------- obtención ----------
def _sin_credenciales(texto):
    """Oculta credenciales antes de mostrar o registrar un texto: `usuario:clave@` dentro de una URL (aunque la clave lleve «@»)
    y parámetros como token=, key=, password=, clave=, secret=, apikey=."""
    t = re.sub(r"(?<=://)[^/\s]*@", "***@", str(texto))
    return re.sub(r"(?i)\b(token|access_token|key|api_key|apikey|password|pass|clave|secret)=[^&\s'\"]+", r"\1=***", t)


def _leer(resp, limite, truncar=False):
    """Lee el cuerpo por trozos y cuenta los bytes realmente recibidos (no confía en Content-Length)."""
    partes, total = [], 0
    for trozo in resp.iter_content(65536):
        total += len(trozo)
        if total > limite:
            if truncar:
                partes.append(trozo[: limite - (total - len(trozo))])
                break
            raise ValueError(f"la respuesta supera el límite de {limite / (1024 * 1024):g} MB y se rechazó sin procesarla")
        partes.append(trozo)
    return b"".join(partes)


def _muestra(datos, n=120):
    """Primeros caracteres de la respuesta, en una línea, para el registro de errores."""
    return " ".join(datos[:n].decode("utf-8", "replace").split())


def _get_json(ruta, url=None):
    """GET de un JSON (por defecto a la API de Bencina en Línea; con `url`, a esa dirección, p. ej. FUENTE_URL). Cuando falla, deja en el registro la URL, el código HTTP, el tipo de contenido
    y el comienzo de la respuesta, para poder saber por qué (un bloqueo, una página de error y un cambio de formato se ven distinto).
    El cuerpo se limita a MAX_BYTES: si lo supera se rechaza sin interpretarlo como JSON."""
    url = url or f"{API_BASE}/{ruta}"
    url_log = _sin_credenciales(url)  # lo que se registra nunca lleva credenciales
    resp = requests.get(
        url, timeout=TIMEOUT, verify=VERIFICAR_TLS, stream=True, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        tipo = (getattr(resp, "headers", None) or {}).get("Content-Type", "?")
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            log.warning("HTTP %s en %s (content-type %s): %r", resp.status_code, url_log, tipo, _sin_credenciales(_muestra(_leer(resp, 200, truncar=True))))
            raise
        declarado = str((getattr(resp, "headers", None) or {}).get("Content-Length", ""))
        if declarado.isdigit() and int(declarado) > MAX_BYTES:  # aviso temprano; el control real es el conteo de bytes
            raise ValueError(f"{ruta}: la respuesta declara {int(declarado) / (1024 * 1024):g} MB, más que el límite de {MAX_BYTES / (1024 * 1024):g} MB")
        try:
            cuerpo = _leer(resp, MAX_BYTES)
        except ValueError as e:
            log.warning("%s: %s (%s)", url_log, _sin_credenciales(e), tipo)
            raise ValueError(f"{ruta}: {e}") from e
        try:
            return json.loads(cuerpo)
        except ValueError as e:
            log.warning("Respuesta no JSON en %s (HTTP %s, content-type %s): %r", url_log, resp.status_code, tipo, _sin_credenciales(_muestra(cuerpo)))
            raise ValueError(f"{ruta} no devolvió JSON (HTTP {resp.status_code}, {tipo}): {_sin_credenciales(_muestra(cuerpo, 60))!r}") from e
    finally:
        resp.close()


def _descargar():
    """(respuesta de estaciones, respuesta de marcas o None). Sin marcas igual sirve: se reutilizan las anteriores."""
    estaciones_json = _get_json("busqueda_estacion_filtro")
    try:
        marcas_json = _get_json("marca_ciudadano")
    except (requests.RequestException, ValueError) as e:
        log.warning("No se pudo obtener la lista de marcas (%s).", _sin_credenciales(e))
        marcas_json = None
    return estaciones_json, marcas_json


def _desde_archivo(ruta):
    with open(ruta, encoding="utf-8") as f:
        contenido = json.load(f)
    generado = None
    if isinstance(contenido, dict) and "comunas" in contenido:  # formato de combustible.json (RX Noticias)
        estaciones, descartados, generado = normalizar_rx(contenido)
    else:
        estaciones, descartados = normalizar_respuesta(contenido.get("estaciones"), contenido.get("marcas"))
    return Datos(estaciones, "archivo", timezone.now(), "Datos de un archivo local, no de la consulta en vivo.", descartados, generado=generado)


# ---------- caché fresca (Django, 1 hora) ----------
def _leer_cache(clave):
    try:
        return cache.get(clave)
    except Exception as e:  # un archivo de caché dañado nunca debe tumbar la página
        log.warning("No se pudo leer la caché (%s): se ignora. (%s)", clave, _sin_credenciales(e))
        return None


def _escribir_cache(clave, valor, segundos):
    try:
        cache.set(clave, valor, segundos)
    except Exception as e:
        log.warning("No se pudo escribir la caché (%s): %s", clave, _sin_credenciales(e))


# ---------- respaldo permanente (JSON) ----------
def _ruta_respaldo(previo=False):
    return CARPETA_RESPALDO / ("respaldo.previo.json" if previo else "respaldo.json")


def _cargar_respaldo(ruta):
    """Documento del respaldo o None si no existe o no es válido. Nunca borra ni modifica el archivo."""
    try:
        est = ruta.stat()
        firma = (est.st_mtime_ns, est.st_size)
        if _memo.get(ruta, (None,))[0] == firma:
            return _memo[ruta][1]
        with open(ruta, encoding="utf-8") as f:
            doc = json.load(f)
        ok = (
            isinstance(doc, dict) and doc.get("version") == VERSION_RESPALDO
            and isinstance(doc.get("estaciones"), list) and doc["estaciones"]
            and all(isinstance(e, dict) and "id" in e and isinstance(e.get("precios"), dict) for e in doc["estaciones"])
        )
        if ok:
            datetime.fromisoformat(doc["descargado"])
        else:
            raise ValueError("estructura inesperada")
    except FileNotFoundError:
        return None
    except Exception as e:  # JSON corrupto, permisos, estructura distinta...
        log.warning("El respaldo %s no se pudo usar y se deja como está: %s", ruta.name, _sin_credenciales(e))
        return None
    _memo[ruta] = (firma, doc)
    return doc


def _leer_respaldo():
    """La última copia válida: respaldo.json y, si no sirve, la anterior (respaldo.previo.json)."""
    return _cargar_respaldo(_ruta_respaldo()) or _cargar_respaldo(_ruta_respaldo(previo=True))


def _escribir_atomico(destino, contenido):
    """Escribe en un archivo temporal de la misma carpeta y lo cambia de nombre: el destino nunca queda a medias."""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=destino.parent, prefix=".tmp-resp-", suffix=".json", delete=False, encoding="utf-8") as f:
            tmp = Path(f.name)
            f.write(contenido)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, destino)
    except BaseException:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        raise


PATRONES_TEMPORALES = (".tmp-resp-*.json", ".tmp-previo-*.json")  # los únicos nombres que crea este módulo


def _limpiar_temporales(carpeta, edad=3600):
    """Borra solo los temporales propios (PATRONES_TEMPORALES) con más de `edad` segundos sin modificarse, que quedaron
    abandonados por una caída a mitad de la escritura. Una escritura en curso termina en segundos, así que no se toca;
    cualquier otro archivo de la carpeta, incluidos los respaldos, queda como está."""
    limite = datetime.now().timestamp() - edad
    for patron in PATRONES_TEMPORALES:
        for t in carpeta.glob(patron):
            try:
                if t.is_file() and not t.is_symlink() and t.stat().st_mtime < limite:
                    t.unlink()
            except OSError:  # p. ej. en uso en Windows: se deja para la próxima vez
                pass


def _guardar_respaldo(doc):
    """Guarda `doc` como respaldo. La copia vigente (si es válida) pasa a respaldo.previo.json. True si quedó guardado.

    Garantías: cada archivo (respaldo.json y respaldo.previo.json) se publica entero con os.replace y nunca queda a medias,
    y los temporales tienen nombre único. NO es una única operación atómica: la rotación (copiar a previo) y la publicación
    del nuevo archivo son dos pasos. Si dos procesos guardan a la vez, ambos archivos siguen siendo válidos, pero puede ganar
    la descarga que terminó de publicar al final aunque no sea la más reciente, y la copia previa puede ser cualquiera de las
    dos versiones anteriores. Para este uso (una descarga por hora) no se justifica un bloqueo entre procesos.
    """
    tmp_prev = None
    try:
        CARPETA_RESPALDO.mkdir(parents=True, exist_ok=True)
        _limpiar_temporales(CARPETA_RESPALDO)
        actual = _ruta_respaldo()
        if _cargar_respaldo(actual):  # solo se rota una copia válida: una dañada no pisa a la anterior
            fd, nombre = tempfile.mkstemp(dir=CARPETA_RESPALDO, prefix=".tmp-previo-", suffix=".json")
            os.close(fd)
            tmp_prev = Path(nombre)
            shutil.copyfile(actual, tmp_prev)
            os.replace(tmp_prev, _ruta_respaldo(previo=True))
            tmp_prev = None
        _escribir_atomico(actual, json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
        return True
    except Exception as e:
        if tmp_prev is not None:
            tmp_prev.unlink(missing_ok=True)
        log.error("No se pudo guardar el respaldo en %s: %s", CARPETA_RESPALDO, _sin_credenciales(e))
        return False


def _n_registros(estaciones):
    """Cantidad de registros de precio (todos los combustibles) de una lista de estaciones."""
    return sum(len(e.get("combustibles") or e["precios"]) for e in estaciones)


def _validar_descarga(estaciones, previo, fraccion=FRACCION_MIN):
    """Texto con el motivo si la descarga no sirve para reemplazar la copia anterior; "" si está bien.
    `previo` es la referencia con la que se compara (un documento con `estaciones`); `fraccion`, lo mínimo que debe conservar de ella."""
    con_precio = sum(1 for e in estaciones if e["precios"])
    if len(estaciones) < MIN_ESTACIONES:
        return f"solo {len(estaciones)} estaciones"
    if con_precio < len(estaciones) * FRACCION_MIN:
        return f"solo {con_precio} de {len(estaciones)} estaciones traen precios"
    if previo:
        antes = previo["estaciones"]
        antes_con_precio = sum(1 for e in antes if e["precios"])
        if len(estaciones) < len(antes) * fraccion:
            return f"trae {len(estaciones)} estaciones y la copia anterior tenía {len(antes)}"
        if con_precio < antes_con_precio * fraccion:
            return f"trae {con_precio} estaciones con precio y la copia anterior tenía {antes_con_precio}"
        if _n_registros(estaciones) < _n_registros(antes) * fraccion:
            return f"trae {_n_registros(estaciones)} registros de precio y la copia anterior tenía {_n_registros(antes)}"
    return ""


def _marcas_validas(marcas_json):
    return isinstance(marcas_json, dict) and isinstance(marcas_json.get("data"), list) and bool(marcas_json["data"])


def obtener_datos(forzar=False):
    """Estaciones con precios. Nunca lanza excepciones: si todo falla devuelve Datos(ok=False)."""
    if ARCHIVO_LOCAL:
        try:
            return _desde_archivo(ARCHIVO_LOCAL)
        except (OSError, ValueError) as e:
            return Datos(aviso=f"No se pudo leer el archivo local ({e}).")

    if not forzar:
        fresco = _leer_cache(K_FRESCO)
        if isinstance(fresco, Datos) and fresco.ok:
            return fresco
        error = _leer_cache(K_ERROR)
        if error:  # falló hace poco: no insistir en cada carga de la página
            return _respaldo_o_error(error)

    previo = _leer_respaldo()
    return (_actualizar_api if FUENTE == "api" else _actualizar_rx)(previo)


def _actualizar_rx(previo):
    """Descarga combustible.json y actualiza el respaldo solo si es válido, su `generado` es estrictamente posterior y no trae
    mucho menos datos que la referencia: el respaldo persistente o, mientras no exista, los datos iniciales del proyecto
    (`combustible_inicial.json`). En cualquier otro caso (error, vacío, corrupto, incompleto, igual o más antiguo) se sigue con lo que hay."""
    referencia, nombre = previo, "el guardado"
    if not previo:
        referencia, nombre = _referencia_inicial(), "el que viene con el proyecto"
    try:
        contenido = _get_json("combustible.json", url=FUENTE_URL)
        generado, _ = _leer_rx(contenido, ahora())
        anterior = _fecha_utc((referencia or {}).get("generado"))
        if anterior and generado == anterior:  # nada nuevo: no se toca el respaldo
            datos = Datos(referencia["estaciones"], "rx", timezone.now(), "", referencia.get("descartados") or {}, generado=generado)
            _escribir_cache(K_FRESCO, datos, CACHE_SEGUNDOS)
            return datos
        if anterior and generado < anterior:
            return _fallo(f"RX Noticias publicó un archivo más antiguo ({generado:%d-%m-%Y %H:%M} UTC) que {nombre} "
                          f"({anterior:%d-%m-%Y %H:%M} UTC); se conserva {nombre}.", ValueError("generado anterior"))
        estaciones, descartados, generado = normalizar_rx(contenido, referencia)
    except requests.Timeout as e:
        return _fallo(f"RX Noticias no respondió en {TIMEOUT:g} s.", e)
    except requests.exceptions.SSLError as e:
        return _fallo(f"No se pudo verificar el certificado TLS de RX Noticias ({str(e)[-160:]}); la verificación no se desactiva.", e)
    except requests.ConnectionError as e:
        return _fallo(f"No se pudo conectar con RX Noticias ({type(e).__name__}: {str(e)[:150]}).", e)
    except requests.HTTPError as e:
        r = e.response
        return _fallo(f"RX Noticias respondió con error HTTP {r.status_code} en {_sin_credenciales(getattr(r, 'url', FUENTE_URL))}.", e)
    except (requests.RequestException, ValueError) as e:  # vacío, corrupto, HTML en vez de JSON, estructura distinta
        return _fallo(f"RX Noticias devolvió un archivo que no se pudo interpretar ({str(e)[:200]}).", e)

    motivo = _validar_descarga(estaciones, referencia, FRACCION_MIN_RX)
    if motivo:  # incompleta: no reemplaza lo que ya hay
        return _fallo(f"La descarga se rechazó porque parece incompleta ({motivo}); se conserva {nombre}.", ValueError(motivo))

    ahora_utc = timezone.now()
    avisos = [] if VERIFICAR_TLS else ["Conexión sin verificar el certificado TLS (BENCINA_VERIFICAR_TLS=0)."]
    if descartados.get("sin_coordenadas"):
        avisos.append(f"{descartados['sin_coordenadas']} estaciones no tienen coordenadas: salen en los listados pero no en el mapa.")
    doc = {
        "version": VERSION_RESPALDO,
        "descargado": ahora_utc.isoformat(),
        "generado": generado.isoformat(),
        "fuente": "rx",
        "completa": True,
        "endpoints": {"combustible.json": {"ok": True, "n": len(estaciones)}},
        "descartados": descartados,
        "estaciones": estaciones,
        "crudo": {"rx": contenido},  # el archivo original
    }
    if not _guardar_respaldo(doc):
        avisos.append("No se pudo guardar la copia local de los datos.")
    datos = Datos(estaciones, "rx", ahora_utc, " ".join(avisos), descartados, generado=generado)
    _escribir_cache(K_FRESCO, datos, CACHE_SEGUNDOS)
    try:
        cache.delete(K_ERROR)
    except Exception:
        pass
    return datos


def _actualizar_api(previo):
    """Ruta heredada (BENCINA_FUENTE=api): API directa de Bencina en Línea, con las mismas protecciones del respaldo."""
    try:
        estaciones_json, marcas_json = _descargar()
        marcas_ok = _marcas_validas(marcas_json)
        if not marcas_ok:  # marcas de la descarga anterior, si las hay
            marcas_json = ((previo or {}).get("crudo") or {}).get("marcas")
        estaciones, descartados = normalizar_respuesta(estaciones_json, marcas_json)
    except requests.Timeout as e:
        return _fallo(f"Bencina en Línea no respondió en {TIMEOUT:g} s.", e)
    except requests.exceptions.SSLError as e:
        return _fallo(
            f"No se pudo verificar el certificado TLS de la API ({str(e)[-160:]}). Si el certificado del servidor está vencido o "
            "mal configurado, hay que esperar a que el proveedor lo corrija; BENCINA_VERIFICAR_TLS=0 existe solo para un diagnóstico puntual, "
            "no es una solución.", e)
    except requests.ConnectionError as e:
        return _fallo(f"No se pudo conectar con Bencina en Línea ({type(e).__name__}: {str(e)[:150]}).", e)
    except requests.HTTPError as e:
        r = e.response
        return _fallo(f"Bencina en Línea respondió con error HTTP {r.status_code} en {_sin_credenciales(getattr(r, 'url', API_BASE))}.", e)
    except (requests.RequestException, ValueError) as e:  # JSON inválido o respuesta vacía
        return _fallo(f"Bencina en Línea devolvió una respuesta que no se pudo interpretar ({str(e)[:200]}).", e)

    motivo = _validar_descarga(estaciones, previo)
    if motivo:  # defectuosa o incompleta: no reemplaza la copia buena
        return _fallo(f"La descarga se rechazó porque parece incompleta ({motivo}); se conserva la copia anterior.", ValueError(motivo))

    ahora_utc = timezone.now()
    avisos = [] if VERIFICAR_TLS else ["Conexión con la API sin verificar el certificado TLS (BENCINA_VERIFICAR_TLS=0)."]
    if not marcas_ok:
        avisos.append("No se pudieron actualizar las marcas" + ("; se usan las de la descarga anterior." if marcas_json else "."))
    doc = {
        "version": VERSION_RESPALDO,
        "descargado": ahora_utc.isoformat(),
        "completa": marcas_ok,
        "endpoints": {
            "busqueda_estacion_filtro": {"ok": True, "n": len(estaciones)},
            "marca_ciudadano": {"ok": marcas_ok, "n": len((marcas_json or {}).get("data") or [])},
        },
        "descartados": descartados,
        "estaciones": estaciones,  # normalizadas: lo que usa la aplicación
        "crudo": {"estaciones": estaciones_json, "marcas": marcas_json},  # respuestas originales de la API
    }
    if not _guardar_respaldo(doc):
        avisos.append("No se pudo guardar la copia local de los datos.")
    datos = Datos(estaciones, "api", ahora_utc, " ".join(avisos), descartados, completa=marcas_ok)
    _escribir_cache(K_FRESCO, datos, CACHE_SEGUNDOS)
    try:
        cache.delete(K_ERROR)
    except Exception:
        pass
    return datos


def _fallo(mensaje, excepcion):
    mensaje = _sin_credenciales(mensaje)
    log.warning("%s (%s)", mensaje, _sin_credenciales(excepcion))
    _escribir_cache(K_ERROR, mensaje, REINTENTO_SEGUNDOS)
    return _respaldo_o_error(mensaje)


def _respaldo_o_error(mensaje):
    respaldo = _leer_respaldo()
    if respaldo:
        return Datos(
            respaldo["estaciones"], "respaldo", datetime.fromisoformat(respaldo["descargado"]),
            f"{mensaje} Se muestran los últimos precios obtenidos.", respaldo.get("descartados") or {},
            completa=bool(respaldo.get("completa", True)), generado=_fecha_utc(respaldo.get("generado")),
        )
    inicial = _datos_iniciales()
    if inicial:
        inicial.aviso = f"{mensaje} No había copia guardada: se muestran los datos que vienen con el proyecto."
        return inicial
    return Datos(aviso=f"{mensaje} No hay precios guardados para mostrar.")


def _referencia_inicial():
    """Los datos iniciales del proyecto como referencia (generado, estaciones y descartados) cuando todavía no hay respaldo; None si no hay."""
    inicial = _datos_iniciales()
    if not inicial:
        return None
    return {"generado": inicial.generado.isoformat(), "estaciones": inicial.estaciones, "descartados": inicial.descartados}


def _datos_iniciales():
    """Último recurso (sin respaldo y sin descarga): el combustible.json que viene con el proyecto, con la fecha propia de
    cada precio. Se marca como `respaldo` (no son precios actuales). None si el archivo no está o no se puede usar."""
    contenido = _json_memo(ARCHIVO_INICIAL)
    if not contenido:
        return None
    try:
        # ahora_ lejano: aquí no tiene sentido rechazar el archivo por tener «fecha futura» (solo depende del reloj del servidor)
        estaciones, descartados, generado = normalizar_rx(contenido, ahora_=datetime(9999, 1, 1))
    except ValueError as e:
        log.warning("Los datos iniciales del proyecto no se pudieron usar: %s", _sin_credenciales(e))
        return None
    return Datos(estaciones, "respaldo", generado, "", descartados, generado=generado)


# ---------- filtros ----------
def filtrar_estaciones(estaciones, comuna="", region="", texto=""):
    """Filtra por comuna, región y texto libre (marca, dirección o comuna), sin importar tildes.

    La comuna coincide exacta si existe una con ese nombre; si no, por texto contenido.
    """
    c, r, t = normalizar_texto(comuna), normalizar_texto(region), normalizar_texto(texto)
    if c:
        exactas = [e for e in estaciones if normalizar_texto(e["comuna"]) == c]
        estaciones = exactas or [e for e in estaciones if c in normalizar_texto(e["comuna"])]
    if r:
        estaciones = [e for e in estaciones if r in normalizar_texto(e["region"])]
    if t:
        estaciones = [
            e for e in estaciones if t in normalizar_texto(f"{e['marca']} {e['direccion']} {e['comuna']}")
        ]
    return list(estaciones)

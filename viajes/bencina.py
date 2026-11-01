"""Consulta y normalización de precios de Bencina en Línea (CNE).

API pública sin token: GET {API_BASE}/busqueda_estacion_filtro y /marca_ciudadano.
Formato de salida de cada estación (el mismo para cualquier fuente):

    {"id", "marca", "region", "comuna", "direccion", "lat", "lon",
     "precios": {"93": {"precio": 1586.0, "fecha": "2026-10-01 08:45:00", "atencion": "AutoServicio"}}}

`lat`/`lon` son None si la fuente no trae coordenadas válidas; `precios` solo incluye los
precios 93/95/97 que pasaron la validación (los demás se consideran "sin dato").
"""
import json
import logging
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from functools import lru_cache

import requests
import urllib3
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

log = logging.getLogger(__name__)

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

COMBUSTIBLES = ("93", "95", "97")
RANGO_PRECIO = (500, 2500)  # $/L; fuera de este rango se descarta (ceros, 99999, etc.)
MAX_DIAS = int(os.environ.get("BENCINA_MAX_DIAS", "30"))  # precios más antiguos no se usan
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


def _get_json(ruta):
    """GET a la API de Bencina en Línea. Cuando falla, deja en el registro la URL, el código HTTP, el tipo de contenido
    y el comienzo de la respuesta, para poder saber por qué (un bloqueo, una página de error y un cambio de formato se ven distinto).
    El cuerpo se limita a MAX_BYTES: si lo supera se rechaza sin interpretarlo como JSON."""
    url = f"{API_BASE}/{ruta}"
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
    estaciones, descartados = normalizar_respuesta(contenido.get("estaciones"), contenido.get("marcas"))
    return Datos(estaciones, "archivo", timezone.now(), "Datos de un archivo local, no de la consulta en vivo.", descartados)


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


_memo = {}  # ruta -> (firma del archivo, documento): evita releer varios MB en cada carga de la página


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


def _validar_descarga(estaciones, previo):
    """Texto con el motivo si la descarga no sirve para reemplazar la copia anterior; "" si está bien."""
    con_precio = sum(1 for e in estaciones if e["precios"])
    if len(estaciones) < MIN_ESTACIONES:
        return f"solo {len(estaciones)} estaciones"
    if con_precio < len(estaciones) * FRACCION_MIN:
        return f"solo {con_precio} de {len(estaciones)} estaciones traen precios"
    if previo:
        antes = previo["estaciones"]
        antes_con_precio = sum(1 for e in antes if e["precios"])
        if len(estaciones) < len(antes) * FRACCION_MIN:
            return f"trae {len(estaciones)} estaciones y la copia anterior tenía {len(antes)}"
        if con_precio < antes_con_precio * FRACCION_MIN:
            return f"trae {con_precio} estaciones con precio y la copia anterior tenía {antes_con_precio}"
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
            completa=bool(respaldo.get("completa", True)),
        )
    return Datos(aviso=f"{mensaje} No hay precios guardados para mostrar.")


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

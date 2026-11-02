import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

import requests
from django.core.cache import cache
from django.test import SimpleTestCase

from viajes import bencina

MUESTRA = Path(__file__).parent / "fixtures" / "muestra_api.json"
AHORA = datetime(2026, 10, 27, 12, 0)  # la muestra es del 22 al 26 de octubre de 2026


def cargar_muestra():
    """Muestra real (snapshot del 26-oct-2026 de Bencina en Línea) con casos límite."""
    return json.loads(MUESTRA.read_text(encoding="utf-8"))


class RespuestaFalsa:
    """Respuesta de requests (con stream=True): el cuerpo se lee por trozos con iter_content."""

    def __init__(self, datos=None, status=200, json_invalido=False, cuerpo=None, headers=None):
        self._datos, self.status_code, self._json_invalido, self._cuerpo = datos, status, json_invalido, cuerpo
        self.headers = headers or {}
        self.text = None  # si se fija, es lo que "llega" como cuerpo

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def _bytes(self):
        if self._cuerpo is not None:
            return self._cuerpo
        if self.text is not None:
            return self.text.encode()
        if self._json_invalido:
            return b"<html>no es JSON</html>"
        return json.dumps(self._datos).encode()

    def json(self):
        return self._datos

    def iter_content(self, chunk_size=1):
        cuerpo = self._bytes()
        for i in range(0, len(cuerpo), chunk_size):
            yield cuerpo[i:i + chunk_size]

    def close(self):
        pass


def api_falsa(muestra=None):
    """Reemplazo de requests.get que responde como la API de Bencina en Línea."""
    muestra = muestra or cargar_muestra()

    def get(url, **kwargs):
        if url.endswith("/busqueda_estacion_filtro"):
            return RespuestaFalsa(muestra["estaciones"])
        if url.endswith("/marca_ciudadano"):
            return RespuestaFalsa(muestra["marcas"])
        raise AssertionError(f"URL inesperada: {url}")

    return get


class ConCache(SimpleTestCase):
    """Cada prueba parte con la caché vacía y sin archivo local."""

    def setUp(self):
        cache.clear()
        parche = mock.patch("viajes.bencina.ARCHIVO_LOCAL", "")
        parche.start()
        self.addCleanup(parche.stop)
        # Las pruebas de esta clase y sus hijas usan la ruta heredada (API directa), sin los datos que vienen con el proyecto.
        # La fuente por defecto (RX Noticias) se prueba en ConRx.
        for nombre, valor in (("FUENTE", "api"), ("ARCHIVO_INICIAL", Path(tempfile.gettempdir()) / "no-existe-combustible-inicial.json")):
            p = mock.patch(f"viajes.bencina.{nombre}", valor)
            p.start()
            self.addCleanup(p.stop)
        carpeta = tempfile.TemporaryDirectory(prefix="bencina-prueba-")  # cada prueba con su propio respaldo en disco
        self.addCleanup(carpeta.cleanup)
        self.carpeta = Path(carpeta.name)
        parche_carpeta = mock.patch("viajes.bencina.CARPETA_RESPALDO", self.carpeta)
        parche_carpeta.start()
        self.addCleanup(parche_carpeta.stop)
        bencina._memo.clear()
        reloj = mock.patch("viajes.bencina.ahora", return_value=AHORA)
        reloj.start()
        self.addCleanup(reloj.stop)
        self.addCleanup(cache.clear)


# ---------- fuente RX Noticias ----------
INICIAL = Path(bencina.__file__).parent / "data" / "combustible_inicial.json"
AHORA_RX = datetime(2026, 11, 1, 12, 0)  # el archivo inicial se generó el 1-nov-2026 a las 08:30 UTC


def cargar_rx():
    """combustible.json de RX Noticias tal como se recibió (1.768 estaciones, 7.803 precios, generado 2026-11-01 08:30 UTC)."""
    return json.loads(INICIAL.read_text(encoding="utf-8"))


def rx_falsa(contenido=None, **kwargs):
    """Reemplazo de requests.get que responde como rxnoticias.com. `contenido` es un dict (se envía como JSON), o RespuestaFalsa si se quiere otra cosa."""
    def get(url, **kw):
        assert url == bencina.FUENTE_URL, f"URL inesperada: {url}"
        return contenido if isinstance(contenido, RespuestaFalsa) else RespuestaFalsa(contenido if contenido is not None else cargar_rx(), **kwargs)
    return get


class ConRx(ConCache):
    """Como ConCache pero con la fuente por defecto (RX Noticias), la tabla de coordenadas real y el reloj en el 1-nov-2026."""

    def setUp(self):
        super().setUp()
        for nombre, valor in (("FUENTE", "rx"), ("ARCHIVO_INICIAL", INICIAL), ("NOMINATIM_ACTIVO", False)):
            p = mock.patch(f"viajes.bencina.{nombre}", valor)
            p.start()
            self.addCleanup(p.stop)
        reloj = mock.patch("viajes.bencina.ahora", return_value=AHORA_RX)
        reloj.start()
        self.addCleanup(reloj.stop)

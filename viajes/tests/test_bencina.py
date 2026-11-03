import hashlib
import json
from datetime import date, datetime
from unittest import mock

import requests
from django.test import Client

from viajes import bencina

from .base import AHORA_RX, INICIAL, ConCache, ConRx, RespuestaFalsa, api_falsa, cargar_muestra, cargar_rx, rx_falsa


def estacion_por_id(datos, id_):
    return next(e for e in datos.estaciones if e["id"] == id_)


class NormalizacionTests(ConCache):
    def setUp(self):
        super().setUp()
        muestra = cargar_muestra()
        self.estaciones, self.descartados = bencina.normalizar_respuesta(muestra["estaciones"], muestra["marcas"])
        self.por_id = {e["id"]: e for e in self.estaciones}

    def test_coordenadas_con_coma_y_espacios(self):
        e = self.por_id[400]  # latitud '-32,873774', longitud ' -71,2423'
        self.assertEqual((e["lat"], e["lon"]), (-32.873774, -71.2423))

    def test_isla_de_pascua_es_valida(self):
        self.assertIsNotNone(self.por_id[363]["lat"])

    def test_coordenadas_invalidas_quedan_en_none(self):
        for lat, lon in [("", ""), (None, "-70"), ("0", "0"), ("abc", "-70"), ("-33", "70"), ("33", "-70")]:
            self.assertEqual(bencina.normalizar_coordenadas(lat, lon), (None, None), (lat, lon))

    def test_precios_absurdos_se_descartan(self):
        self.assertNotIn("93", self.por_id[909]["precios"])  # 99999.999
        self.assertEqual(self.por_id[2136]["precios"], {})  # 1.000 en los tres
        self.assertNotIn("93", self.por_id[2263]["precios"])  # precio vacío
        self.assertGreaterEqual(self.descartados["precio_invalido"], 5)

    def test_93_y_a93_se_unen_y_gana_el_mas_barato(self):
        p = self.por_id[29]["precios"]["93"]  # 93: 1586 autoservicio, A93: 1578 asistido
        self.assertEqual((p["precio"], p["atencion"]), (1578.0, "Asistido"))

    def test_precios_desactualizados_se_descartan(self):
        # estación 1519: 93 del 22-oct (1630) y A93 del 4-jun (1635, 145 días)
        p = self.por_id[1519]["precios"]["93"]
        self.assertEqual(p["precio"], 1630.0)
        self.assertGreaterEqual(self.descartados["precio_desactualizado"], 3)

    def test_limite_de_antiguedad(self):
        def estacion(fecha):
            return {"id": 1, "combustibles": [{"nombre_corto": "93", "precio": "1500", "precio_fecha": fecha}]}

        ahora = datetime(2026, 10, 27, 12, 0)
        for fecha, vigente in [("2026-09-27 12:00:00", True), ("2026-09-26 11:00:00", False), ("", True), (None, True)]:
            d = {}
            e = bencina.normalizar_estacion(estacion(fecha), {}, d, ahora)
            self.assertEqual("93" in e["precios"], vigente, fecha)

    def test_solo_93_95_97(self):
        for e in self.estaciones:
            self.assertLessEqual(set(e["precios"]), {"93", "95", "97"})

    def test_fecha_del_precio_y_fecha_ausente(self):
        self.assertEqual(self.por_id[400]["precios"]["93"]["fecha"], "2026-10-22 12:20:54")
        e = bencina.normalizar_estacion(
            {"id": 1, "combustibles": [{"nombre_corto": "93", "precio": "1500", "precio_fecha": None}]}, {}
        )
        self.assertIsNone(e["precios"]["93"]["fecha"])

    def test_marca_por_id_y_sin_marca(self):
        self.assertIn(self.por_id[29]["marca"], {"COPEC", "SHELL", "ARAMCO", "Sin Bandera"})
        e = bencina.normalizar_estacion({"id": 1, "marca": 999, "combustibles": []}, {1: "X"})
        self.assertEqual(e["marca"], "Sin marca")

    def test_estacion_malformada(self):
        descartados = {}
        for malo in (None, "texto", {}, {"id": None}, []):
            self.assertIsNone(bencina.normalizar_estacion(malo, {}, descartados))
        self.assertEqual(descartados["estacion_invalida"], 5)

    def test_respuestas_inutilizables(self):
        for malo in (None, [], {}, {"data": []}, {"data": "x"}, {"data": [None, 3]}):
            with self.assertRaises(ValueError):
                bencina.normalizar_respuesta(malo)


class FiltroTests(ConCache):
    def setUp(self):
        super().setUp()
        muestra = cargar_muestra()
        self.estaciones, _ = bencina.normalizar_respuesta(muestra["estaciones"], muestra["marcas"])

    def test_comuna_sin_tildes_ni_mayusculas(self):
        self.assertEqual({e["comuna"] for e in bencina.filtrar_estaciones(self.estaciones, comuna="valparaiso")}, {"Valparaíso"})

    def test_comuna_exacta_tiene_prioridad(self):
        r = bencina.filtrar_estaciones(self.estaciones, comuna="Arica")
        self.assertTrue(r and all(e["comuna"] == "Arica" for e in r))

    def test_region_y_texto(self):
        self.assertTrue(all("Magallanes" in e["region"] for e in bencina.filtrar_estaciones(self.estaciones, region="magallanes")))
        self.assertTrue(bencina.filtrar_estaciones(self.estaciones, texto="provid"))
        self.assertEqual(bencina.filtrar_estaciones(self.estaciones, comuna="Atlantida"), [])

    def test_sin_filtros_devuelve_todo(self):
        self.assertEqual(len(bencina.filtrar_estaciones(self.estaciones)), len(self.estaciones))


class ObtenerDatosTests(ConCache):
    def test_api_ok_y_cache(self):
        with mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()) as get:
            d1 = bencina.obtener_datos()
            d2 = bencina.obtener_datos()
        self.assertTrue(d1.ok)
        self.assertEqual((d1.fuente, d1.aviso), ("api", ""))
        self.assertIsNotNone(d1.consultado)
        self.assertEqual(get.call_count, 2)  # estaciones + marcas, una sola vez gracias a la caché
        self.assertEqual((d1.estaciones, d1.consultado), (d2.estaciones, d2.consultado))

    def test_forzar_consulta_ignora_cache(self):
        with mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()) as get:
            bencina.obtener_datos()
            bencina.obtener_datos(forzar=True)
        self.assertEqual(get.call_count, 4)

    def test_usa_timeout_y_user_agent(self):
        with mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()) as get:
            bencina.obtener_datos()
        kwargs = get.call_args.kwargs
        self.assertEqual(kwargs["timeout"], bencina.TIMEOUT)
        self.assertIn("proyectobencina", kwargs["headers"]["User-Agent"])

    def test_fallos_sin_respaldo_no_rompen(self):
        casos = {
            "timeout": requests.Timeout("lento"),
            "conexion": requests.ConnectionError("sin red"),
            "http": RespuestaFalsa(status=503),
            "json": RespuestaFalsa(json_invalido=True),
            "vacia": RespuestaFalsa({"data": []}),
        }
        for nombre, caso in casos.items():
            cache = bencina.cache
            cache.clear()
            efecto = caso if isinstance(caso, Exception) else (lambda *a, _c=caso, **k: _c)
            with self.subTest(nombre), mock.patch("viajes.bencina.requests.get", side_effect=efecto):
                d = bencina.obtener_datos()
            self.assertFalse(d.ok)
            self.assertEqual(d.estaciones, [])
            self.assertIn("No hay precios guardados", d.aviso)

    def test_tras_un_fallo_no_se_reintenta_de_inmediato(self):
        with mock.patch("viajes.bencina.requests.get", side_effect=requests.Timeout()) as get:
            bencina.obtener_datos()
            bencina.obtener_datos()
        self.assertEqual(get.call_count, 1)

    def test_falla_pero_hay_respaldo(self):
        with mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()):
            bueno = bencina.obtener_datos()
        bencina.cache.delete(bencina.K_FRESCO)  # la caché fresca venció, el respaldo sigue
        with mock.patch("viajes.bencina.requests.get", side_effect=requests.Timeout()):
            d = bencina.obtener_datos()
        self.assertTrue(d.ok)
        self.assertEqual(d.fuente, "respaldo")
        self.assertEqual(d.consultado, bueno.consultado)
        self.assertIn("últimos precios obtenidos", d.aviso)

    def test_si_falla_solo_marcas_igual_funciona(self):
        base = api_falsa()

        def get(url, **kw):
            if url.endswith("/marca_ciudadano"):
                raise requests.Timeout()
            return base(url, **kw)

        with mock.patch("viajes.bencina.requests.get", side_effect=get):
            d = bencina.obtener_datos()
        self.assertTrue(d.ok)
        self.assertTrue(all(e["marca"] == "Sin marca" for e in d.estaciones))

    def test_archivo_local(self):
        from .base import MUESTRA

        with mock.patch("viajes.bencina.ARCHIVO_LOCAL", str(MUESTRA)), mock.patch("viajes.bencina.requests.get") as get:
            d = bencina.obtener_datos()
        self.assertEqual(d.fuente, "archivo")
        self.assertTrue(d.ok)
        get.assert_not_called()

    def test_archivo_local_inexistente(self):
        with mock.patch("viajes.bencina.ARCHIVO_LOCAL", "/no/existe.json"):
            d = bencina.obtener_datos()
        self.assertFalse(d.ok)
        self.assertIn("archivo local", d.aviso)


class CasosLimiteApiTests(ConCache):
    def est(self, id_, precio="1500"):
        return {"id": id_, "combustibles": [{"nombre_corto": "93", "precio": precio, "precio_fecha": "2026-10-26 10:00:00"}]}

    def test_estaciones_duplicadas_se_cuentan_una_vez(self):
        data = {"data": [self.est(1, "1500"), self.est(1, "1400"), self.est(2)]}
        estaciones, descartados = bencina.normalizar_respuesta(data, None)
        self.assertEqual([e["id"] for e in estaciones], [1, 2])
        self.assertEqual(estaciones[0]["precios"]["93"]["precio"], 1500.0)  # se conserva la primera
        self.assertEqual(descartados["estacion_duplicada"], 1)

    def test_respuestas_inutilizables(self):
        for malo in (None, [], {}, {"data": None}, {"data": []}, {"data": [None, 5, "x", {"sin_id": 1}]}):
            with self.assertRaises(ValueError):
                bencina.normalizar_respuesta(malo, None)

    def test_precios_raros(self):
        for valor, ok in [("1500", True), ("1500,5", True), (" 1500 ", True), (1500, True), ("", False), (None, False),
                          ("abc", False), ("nan", False), ("inf", False), (0, False), (-5, False), (99999, False), (True, False)]:
            e = bencina.normalizar_estacion(self.est(1, valor), {}, None, None)
            self.assertEqual("93" in e["precios"], ok, valor)

    def test_marcas_con_formato_inesperado(self):
        for marcas in (None, [], {"data": "x"}, {"data": [1, None]}):
            estaciones, _ = bencina.normalizar_respuesta({"data": [self.est(1)]}, marcas)
            self.assertEqual(estaciones[0]["marca"], "Sin marca")

    def test_filtros_combinados_sin_resultados_y_con_tildes(self):
        estaciones, _ = bencina.normalizar_respuesta(
            {"data": [dict(self.est(1), comuna="Ñuñoa", region="Metropolitana", direccion="Av. Irarrázaval 1"),
                      dict(self.est(2), comuna="Providencia", region="Metropolitana", direccion="Av. Italia 2")]}, None)
        self.assertEqual(len(bencina.filtrar_estaciones(estaciones, comuna="nunoa")), 1)
        self.assertEqual(len(bencina.filtrar_estaciones(estaciones, texto="IRARRAZAVAL", region="metropolitana")), 1)
        self.assertEqual(bencina.filtrar_estaciones(estaciones, comuna="nunoa", texto="italia"), [])
        self.assertEqual(bencina.filtrar_estaciones(estaciones, region="Biobío"), [])


class DiagnosticoDeFallosTests(ConCache):
    def pedir(self, resp):
        with mock.patch("viajes.bencina.requests.get", return_value=resp), self.assertLogs("viajes.bencina", "WARNING") as logs:
            return bencina.obtener_datos(forzar=True), "\n".join(logs.output)

    def test_http_403_indica_codigo_y_url_y_queda_en_el_registro(self):
        r = RespuestaFalsa(None, status=403)
        r.text, r.headers, r.url = "<html>Access denied</html>", {"Content-Type": "text/html"}, "https://x/api/busqueda_estacion_filtro"
        d, log = self.pedir(r)
        self.assertFalse(d.ok)
        self.assertIn("HTTP 403", d.aviso)
        self.assertIn("HTTP 403", log)
        self.assertIn("Access denied", log)

    def test_respuesta_no_json_muestra_que_llego(self):
        r = RespuestaFalsa(None, json_invalido=True)
        r.text, r.headers = "<html>mantenimiento</html>", {"Content-Type": "text/html"}
        d, log = self.pedir(r)
        self.assertIn("text/html", d.aviso)
        self.assertIn("mantenimiento", log)

    def test_el_fallo_no_impide_recuperarse_despues(self):
        with mock.patch("viajes.bencina.requests.get", return_value=RespuestaFalsa(None, status=500)):
            self.assertFalse(bencina.obtener_datos().ok)
        with mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()):
            self.assertFalse(bencina.obtener_datos().ok)  # dentro del tiempo de espera no se insiste
            self.assertTrue(bencina.obtener_datos(forzar=True).ok)  # y forzar sí consulta de nuevo
            self.assertTrue(bencina.obtener_datos().ok)  # lo recuperado queda en caché


class CertificadoTlsTests(ConCache):
    def test_certificado_vencido_se_explica_y_no_se_oculta(self):
        err = requests.exceptions.SSLError("certificate verify failed: certificate has expired")
        with mock.patch("viajes.bencina.requests.get", side_effect=err):
            d = bencina.obtener_datos(forzar=True)
        self.assertFalse(d.ok)
        self.assertIn("certificado TLS", d.aviso)
        self.assertIn("certificate has expired", d.aviso)
        self.assertIn("BENCINA_VERIFICAR_TLS", d.aviso)

    def test_por_defecto_se_verifica_y_desactivarlo_avisa(self):
        with mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()) as get:
            d = bencina.obtener_datos(forzar=True)
        self.assertTrue(get.call_args.kwargs["verify"])
        self.assertEqual(d.aviso, "")
        bencina.cache.clear()
        with mock.patch("viajes.bencina.VERIFICAR_TLS", False), mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()) as get:
            d = bencina.obtener_datos(forzar=True)
        self.assertFalse(get.call_args.kwargs["verify"])
        self.assertIn("sin verificar", d.aviso)
        self.assertTrue(d.ok)


class RespaldoJsonTests(ConCache):
    """Respaldo permanente en respaldo.json: se guarda, no caduca, no se pisa con descargas malas y sobrevive a fallos."""

    def bajar(self, muestra=None, **kw):
        with mock.patch("viajes.bencina.requests.get", side_effect=api_falsa(muestra)):
            return bencina.obtener_datos(forzar=True, **kw)

    def caida(self, exc=None):
        with mock.patch("viajes.bencina.requests.get", side_effect=exc or requests.ConnectionError("sin red")):
            return bencina.obtener_datos(forzar=True)

    def doc(self, previo=False):
        return json.loads(bencina._ruta_respaldo(previo).read_text(encoding="utf-8"))

    def reiniciar(self):  # lo que pasa al cerrar y abrir Django: se pierde la memoria, queda el disco
        bencina.cache.clear()
        bencina._memo.clear()

    # --- A: primera descarga ---
    def test_primera_descarga_guarda_todo_en_disco(self):
        d = self.bajar()
        self.assertEqual(d.fuente, "api")
        doc = self.doc()
        self.assertEqual(len(doc["estaciones"]), len(d.estaciones))
        self.assertTrue(doc["completa"])
        self.assertTrue(doc["endpoints"]["marca_ciudadano"]["ok"])
        self.assertEqual(len(doc["crudo"]["estaciones"]["data"]), 35)  # respuesta original
        self.assertEqual(len(doc["crudo"]["marcas"]["data"]), len(cargar_muestra()["marcas"]["data"]))
        codigos = {c["codigo"] for e in doc["estaciones"] for c in e["combustibles"]}
        self.assertTrue({"93", "95", "97", "DI", "KE", "GNC", "A93", "A95", "A97", "ADI"} <= codigos)
        c = next(c for e in doc["estaciones"] for c in e["combustibles"] if c["codigo"] == "DI")
        self.assertTrue(c["nombre"] and c["unidad"] and c["precio"] and c["fecha"])  # nombre largo, unidad de cobro, precio y fecha

    def test_el_recomendador_sigue_viendo_solo_93_95_97(self):
        for e in self.bajar().estaciones:
            self.assertLessEqual(set(e["precios"]), {"93", "95", "97"})

    # --- B: reinicio, y sin caducidad ---
    def test_sobrevive_a_un_reinicio_y_no_caduca(self):
        self.bajar()
        doc = self.doc()
        doc["descargado"] = "2026-06-01T12:00:00+00:00"  # una copia de hace meses
        bencina._ruta_respaldo().write_text(json.dumps(doc), encoding="utf-8")
        self.reiniciar()
        d = self.caida()
        self.assertEqual(d.fuente, "respaldo")
        self.assertEqual(len(d.estaciones), 35)
        self.assertEqual(d.consultado.isoformat(), "2026-06-01T12:00:00+00:00")  # se informa su fecha real

    # --- C: API caída (todos los tipos de fallo) ---
    def test_api_caida_usa_la_copia_guardada(self):
        self.bajar()
        for exc in (requests.ConnectionError("x"), requests.Timeout("x"), requests.exceptions.SSLError("certificate has expired")):
            d = self.caida(exc)
            self.assertEqual(d.fuente, "respaldo", exc)
            self.assertTrue(d.ok)
        for r in (RespuestaFalsa(None, status=500), RespuestaFalsa(None, json_invalido=True)):
            with mock.patch("viajes.bencina.requests.get", return_value=r):
                self.assertEqual(bencina.obtener_datos(forzar=True).fuente, "respaldo")

    # --- D: descarga defectuosa ---
    def test_descarga_incompleta_no_pisa_la_copia_buena(self):
        self.bajar()
        antes = bencina._ruta_respaldo().read_bytes()
        malas = []
        m = cargar_muestra(); m["estaciones"]["data"] = m["estaciones"]["data"][:1]; malas.append(m)  # 1 sola estación
        m = cargar_muestra(); m["estaciones"]["data"] = m["estaciones"]["data"][:12]; malas.append(m)  # menos de la mitad
        m = cargar_muestra()
        for e in m["estaciones"]["data"]:
            e["combustibles"] = []
        malas.append(m)  # estaciones sin precios
        m = cargar_muestra(); m["estaciones"] = {"data": []}; malas.append(m)  # vacía
        m = cargar_muestra(); m["estaciones"] = {"error": "x"}; malas.append(m)  # otro formato
        for mala in malas:
            d = self.bajar(mala)
            self.assertEqual(d.fuente, "respaldo")
            self.assertEqual(len(d.estaciones), 35)
            self.assertEqual(bencina._ruta_respaldo().read_bytes(), antes)  # el archivo ni se tocó
        self.assertIn("incompleta", self.bajar(malas[0]).aviso)

    # --- marcas ---
    def test_si_fallan_las_marcas_se_conservan_las_anteriores(self):
        self.bajar()
        marcas_antes = {e["marca"] for e in self.doc()["estaciones"]}
        self.assertNotEqual(marcas_antes, {"Sin marca"})

        def sin_marcas(url, **kw):
            if url.endswith("marca_ciudadano"):
                raise requests.ConnectionError("x")
            return api_falsa()(url, **kw)

        with mock.patch("viajes.bencina.requests.get", side_effect=sin_marcas):
            d = bencina.obtener_datos(forzar=True)
        self.assertEqual(d.fuente, "api")
        self.assertFalse(d.completa)
        self.assertEqual({e["marca"] for e in d.estaciones}, marcas_antes)
        self.assertIn("marcas", d.aviso)
        doc = self.doc()
        self.assertFalse(doc["completa"])
        self.assertEqual(len(doc["crudo"]["marcas"]["data"]), len(cargar_muestra()["marcas"]["data"]))  # marcas previas conservadas

    def test_si_las_marcas_llegan_vacias_se_tratan_como_fallo(self):
        self.bajar()
        m = cargar_muestra(); m["marcas"] = {"data": []}
        d = self.bajar(m)
        self.assertFalse(d.completa)
        self.assertNotEqual({e["marca"] for e in d.estaciones}, {"Sin marca"})

    # --- escritura atómica ---
    def test_si_falla_la_escritura_no_se_pierde_la_copia_ni_se_deja_basura(self):
        self.bajar()
        antes = bencina._ruta_respaldo().read_bytes()
        with mock.patch("viajes.bencina.os.replace", side_effect=OSError("disco lleno")):
            d = self.bajar()
        self.assertEqual(d.fuente, "api")  # igual se muestran los datos recién descargados
        self.assertIn("No se pudo guardar", d.aviso)
        self.assertEqual(bencina._ruta_respaldo().read_bytes(), antes)
        self.assertEqual([p.name for p in self.carpeta.iterdir() if p.name.startswith(".tmp")], [])

    def test_la_copia_vigente_pasa_a_previo(self):
        self.bajar()
        primera = self.doc()["descargado"]
        self.bajar()
        self.assertEqual(self.doc(previo=True)["descargado"], primera)

    # --- archivos dañados ---
    def test_json_corrupto_usa_la_copia_previa_y_no_borra_nada(self):
        self.bajar(); self.bajar()
        bencina._ruta_respaldo().write_text('{"version": 1, "estaciones": [', encoding="utf-8")  # cortado a la mitad
        bencina._memo.clear()
        with self.assertLogs("viajes.bencina", "WARNING"):
            d = self.caida()
        self.assertEqual(d.fuente, "respaldo")
        self.assertEqual(len(d.estaciones), 35)
        self.assertTrue(bencina._ruta_respaldo().exists())  # el dañado se deja para revisarlo
        self.assertTrue(bencina._ruta_respaldo(previo=True).exists())

    def test_un_respaldo_danado_no_pisa_a_la_copia_previa_valida(self):
        self.bajar(); self.bajar()
        previo = bencina._ruta_respaldo(previo=True).read_bytes()
        bencina._ruta_respaldo().write_text("no es json", encoding="utf-8")
        bencina._memo.clear()
        self.bajar()  # descarga buena: reemplaza el dañado, sin rotarlo sobre el previo
        self.assertEqual(bencina._ruta_respaldo(previo=True).read_bytes(), previo)
        self.assertEqual(len(self.doc()["estaciones"]), 35)

    def test_todo_danado_o_con_estructura_rara_no_lanza_errores(self):
        for contenido in ("", "null", "[]", '{"version": 2}', '{"version": 1, "estaciones": [], "descargado": "x"}',
                          '{"version": 1, "estaciones": [1], "descargado": "2026-01-01T00:00:00+00:00"}', "\x00\x01"):
            self.carpeta.joinpath("respaldo.json").write_text(contenido, encoding="utf-8")
            bencina._memo.clear()
            d = self.caida()
            self.assertFalse(d.ok, contenido)
            self.assertIn("No hay precios guardados", d.aviso)

    def test_cache_fresca_danada_no_provoca_errores(self):
        self.bajar()
        with mock.patch("viajes.bencina.cache.get", side_effect=EOFError("pickle corrupto")), \
                mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()):
            d = bencina.obtener_datos()
        self.assertTrue(d.ok)
        with mock.patch("viajes.bencina.cache.get", side_effect=EOFError("x")), mock.patch("viajes.bencina.cache.set", side_effect=OSError("x")), \
                mock.patch("viajes.bencina.requests.get", side_effect=requests.ConnectionError("x")):
            self.assertEqual(bencina.obtener_datos().fuente, "respaldo")

    # --- E: recuperación ---
    def test_cuando_vuelve_la_api_se_actualiza_la_copia(self):
        self.bajar()
        self.assertEqual(self.caida().fuente, "respaldo")
        antes = self.doc()["descargado"]
        m = cargar_muestra(); m["estaciones"]["data"][0]["combustibles"][0]["precio"] = "1111"
        d = self.bajar(m)
        self.assertEqual(d.fuente, "api")
        self.assertNotEqual(self.doc()["descargado"], antes)
        self.assertIn(1111.0, [c["precio"] for c in self.doc()["estaciones"][0]["combustibles"]])
        self.assertEqual(self.doc(previo=True)["descargado"], antes)

    # --- F: sin copia ---
    def test_sin_copia_y_sin_api_el_mensaje_es_claro(self):
        d = self.caida()
        self.assertFalse(d.ok)
        self.assertIn("No hay precios guardados", d.aviso)
        self.assertEqual(d.estaciones, [])
        self.assertFalse(bencina._ruta_respaldo().exists())  # no se inventa ni se crea nada


class EndurecimientoTests(ConCache):
    def bajar(self, muestra=None):
        with mock.patch("viajes.bencina.requests.get", side_effect=api_falsa(muestra)):
            return bencina.obtener_datos(forzar=True)

    def caida(self, resp):
        with mock.patch("viajes.bencina.requests.get", return_value=resp):
            return bencina.obtener_datos(forzar=True)

    # --- 1. caché de Django separada del respaldo ---
    def test_la_cache_de_django_vive_en_su_propia_subcarpeta(self):
        import runpy
        import shutil
        import sys
        import tempfile

        carpeta = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, carpeta, True)
        with mock.patch.dict("os.environ", {"BENCINA_CACHE_DIR": carpeta, "DJANGO_DEBUG": "1"}), mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
            cfg = runpy.run_path("config/settings.py")
        self.assertEqual(cfg["CACHES"]["default"]["LOCATION"], f"{carpeta}/cache_django")
        self.assertEqual(cfg["BENCINA_RESPALDO_DIR"], carpeta)

    def test_limpiar_la_cache_de_django_no_toca_los_respaldos(self):
        from django.core.cache.backends.filebased import FileBasedCache

        self.bajar(); self.bajar()  # respaldo.json y respaldo.previo.json
        antes = {n: (self.carpeta / n).read_bytes() for n in ("respaldo.json", "respaldo.previo.json")}
        for ubicacion in (self.carpeta / "cache_django", self.carpeta):  # también si alguien la apuntara a la misma carpeta
            c = FileBasedCache(str(ubicacion), {"MAX_ENTRIES": 1, "CULL_FREQUENCY": 1})
            for i in range(5):
                c.set(f"k{i}", i, 60)  # fuerza la poda interna
            c.clear()
        self.assertEqual({n: (self.carpeta / n).read_bytes() for n in antes}, antes)

    # --- 2. tamaño máximo ---
    def test_respuesta_demasiado_grande_se_rechaza_sin_procesarla(self):
        self.bajar()
        antes = self.carpeta.joinpath("respaldo.json").read_bytes()
        grande = json.dumps({"data": [{"id": i, "relleno": "x" * 50} for i in range(400)]}).encode()
        with mock.patch("viajes.bencina.MAX_BYTES", 2000), mock.patch("viajes.bencina.json.loads", wraps=json.loads) as cargar_json:
            d = self.caida(RespuestaFalsa(cuerpo=grande, headers={"Content-Length": "10"}))  # Content-Length miente
            self.assertNotIn(grande, [c.args[0] for c in cargar_json.call_args_list])  # nunca se interpretó como JSON
        self.assertEqual(d.fuente, "respaldo")
        self.assertIn("límite", d.aviso)
        self.assertEqual(self.carpeta.joinpath("respaldo.json").read_bytes(), antes)
        with mock.patch("viajes.bencina.MAX_BYTES", 2000):  # y también si lo declara y sin Content-Length
            self.assertIn("límite", self.caida(RespuestaFalsa(cuerpo=b"x", headers={"Content-Length": "999999"})).aviso)
            self.assertIn("límite", self.caida(RespuestaFalsa(cuerpo=grande)).aviso)

    def test_el_limite_por_defecto_deja_pasar_la_muestra_real(self):
        self.assertGreaterEqual(bencina.MAX_BYTES, 10 * 1024 * 1024)
        self.assertEqual(self.bajar().fuente, "api")

    def test_json_cortado_o_invalido_no_reemplaza_el_respaldo(self):
        self.bajar()
        antes = self.carpeta.joinpath("respaldo.json").read_bytes()
        completo = json.dumps(cargar_muestra()["estaciones"]).encode()
        for cuerpo in (completo[: len(completo) // 2], b"", b"<html>", b"\xff\xfe\x00"):
            self.assertEqual(self.caida(RespuestaFalsa(cuerpo=cuerpo)).fuente, "respaldo")
            self.assertEqual(self.carpeta.joinpath("respaldo.json").read_bytes(), antes)

    # --- 3. escrituras simultáneas y fallos ---
    def test_escrituras_simultaneas_no_corrompen_el_respaldo(self):
        import threading

        self.bajar()
        base = self.carpeta.joinpath("respaldo.json").read_bytes()
        docs = []
        for i in range(8):
            d = json.loads(base)
            d["descargado"] = f"2026-11-0{1 + i % 8}T12:00:00+00:00"
            docs.append(d)
        resultados = []
        hilos = [threading.Thread(target=lambda d=d: resultados.append(bencina._guardar_respaldo(d))) for d in docs for _ in range(3)]
        for h in hilos: h.start()
        for h in hilos: h.join()
        bencina._memo.clear()
        for nombre in ("respaldo.json", "respaldo.previo.json"):
            doc = json.loads(self.carpeta.joinpath(nombre).read_text(encoding="utf-8"))  # ambos son JSON completos y válidos
            self.assertEqual(len(doc["estaciones"]), 35)
        self.assertEqual([p.name for p in self.carpeta.iterdir() if p.name.startswith(".tmp")], [])
        self.assertTrue(all(resultados))

    def test_fallo_al_rotar_conserva_los_datos_y_no_deja_temporales(self):
        self.bajar(); self.bajar()
        antes = {n: self.carpeta.joinpath(n).read_bytes() for n in ("respaldo.json", "respaldo.previo.json")}
        with mock.patch("viajes.bencina.shutil.copyfile", side_effect=OSError("disco lleno")):
            d = self.bajar()
        self.assertIn("No se pudo guardar", d.aviso)
        self.assertEqual({n: self.carpeta.joinpath(n).read_bytes() for n in antes}, antes)
        self.assertEqual([p.name for p in self.carpeta.iterdir() if p.name.startswith(".tmp")], [])

    def test_temporales_propios_abandonados_se_limpian_y_los_recientes_o_ajenos_no(self):
        import os
        import time

        viejos = [self.carpeta / ".tmp-resp-abc.json", self.carpeta / ".tmp-previo-xyz.json"]
        recientes = [self.carpeta / ".tmp-resp-activo.json", self.carpeta / ".tmp-previo-activo.json"]
        ajenos = [self.carpeta / ".tmp-ajeno.json", self.carpeta / ".tmp-viejo.json", self.carpeta / "notas.tmp", self.carpeta / "datos.json"]
        for t in viejos + recientes + ajenos:
            t.write_text("x")
        hace_2h = time.time() - 7200
        for t in viejos + ajenos:
            os.utime(t, (hace_2h, hace_2h))
        self.bajar()
        self.assertFalse(any(t.exists() for t in viejos))  # propios y abandonados
        self.assertTrue(all(t.exists() for t in recientes))  # podrían ser de una escritura en curso
        self.assertTrue(all(t.exists() for t in ajenos))  # no son de este módulo, aunque sean antiguos
        self.assertTrue(self.carpeta.joinpath("respaldo.json").exists())

    # --- 4. ambos respaldos dañados: la página responde, sin error 500 ---
    def test_con_ambos_respaldos_danados_la_pagina_no_da_500(self):
        from django.test import Client

        self.bajar(); self.bajar()
        for n in ("respaldo.json", "respaldo.previo.json"):
            self.carpeta.joinpath(n).write_text('{"version": 1, "estaciones": [', encoding="utf-8")
        bencina._memo.clear(); bencina.cache.clear()
        c = Client()
        with mock.patch("viajes.bencina.requests.get", side_effect=requests.ConnectionError("sin red")):
            r = c.post("/", {"accion": "recomendar", "vehiculo": "Kia Soluto 1.4", "km_mes": "1000"})
            j = c.get("/estaciones.json")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "No hay precios guardados")
        self.assertEqual(j.status_code, 503)
        self.assertEqual(c.post("/", {"accion": "repostaje", "litros": "40", "precio_litro": "1350"}).status_code, 200)

    # --- TLS ---
    def test_tls_verificado_por_defecto_y_un_error_de_certificado_no_se_salta(self):
        import os

        self.assertNotIn("BENCINA_VERIFICAR_TLS", os.environ)
        self.assertTrue(bencina.VERIFICAR_TLS)
        self.bajar()
        antes = self.carpeta.joinpath("respaldo.json").read_bytes()
        with mock.patch("viajes.bencina.requests.get", side_effect=requests.exceptions.SSLError("certificate has expired")) as get:
            d = bencina.obtener_datos(forzar=True)
        self.assertEqual(get.call_count, 1)  # no reintenta sin verificar
        self.assertTrue(get.call_args.kwargs["verify"])
        self.assertEqual(d.fuente, "respaldo")
        self.assertIn("solo para un diagnóstico", d.aviso)
        self.assertEqual(self.carpeta.joinpath("respaldo.json").read_bytes(), antes)

    # --- mensajes sin credenciales ---
    def test_los_mensajes_no_muestran_usuario_ni_clave_de_un_proxy(self):
        err = requests.ConnectionError("ProxyError('http://usuario:secreta@proxy.local:8080')")
        with mock.patch("viajes.bencina.requests.get", side_effect=err):
            d = bencina.obtener_datos(forzar=True)
        self.assertNotIn("secreta", d.aviso)
        self.assertNotIn("usuario", d.aviso)


class ConfiguracionDjangoTests(ConCache):
    def cargar(self, **env):
        import runpy
        import shutil
        import sys
        import tempfile

        carpeta = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, carpeta, True)
        base = {"BENCINA_CACHE_DIR": carpeta}
        for k in ("DJANGO_DEBUG", "DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS", "BENCINA_VERIFICAR_TLS"):
            base.setdefault(k, "")
        base.update(env)
        with mock.patch.dict("os.environ", base), mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
            import os
            for k in [k for k, v in base.items() if v == "" and k.startswith(("DJANGO_", "BENCINA_VERIF"))]:
                os.environ.pop(k, None)
            return runpy.run_path("config/settings.py")

    def test_el_desarrollo_local_arranca_sin_configurar_nada(self):
        cfg = self.cargar()
        self.assertTrue(cfg["DEBUG"])
        self.assertEqual(cfg["ALLOWED_HOSTS"], [])
        self.assertFalse(cfg.get("SESSION_COOKIE_SECURE", False))
        self.assertFalse(cfg.get("SECURE_SSL_REDIRECT", False))

    def test_produccion_exige_clave_hosts_y_tls(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured) as e:
            self.cargar(DJANGO_DEBUG="0")
        self.assertIn("DJANGO_SECRET_KEY", str(e.exception))
        self.assertIn("DJANGO_ALLOWED_HOSTS", str(e.exception))
        clave = "k" * 60
        with self.assertRaises(ImproperlyConfigured):
            self.cargar(DJANGO_DEBUG="0", DJANGO_SECRET_KEY="django-insecure-" + "a" * 60, DJANGO_ALLOWED_HOSTS="ejemplo.cl")
        with self.assertRaises(ImproperlyConfigured):
            self.cargar(DJANGO_DEBUG="0", DJANGO_SECRET_KEY=clave, DJANGO_ALLOWED_HOSTS="*")
        with self.assertRaises(ImproperlyConfigured) as e:
            self.cargar(DJANGO_DEBUG="0", DJANGO_SECRET_KEY=clave, DJANGO_ALLOWED_HOSTS="ejemplo.cl", BENCINA_VERIFICAR_TLS="0")
        self.assertIn("BENCINA_VERIFICAR_TLS", str(e.exception))

    def test_produccion_valida_activa_las_medidas_seguras(self):
        cfg = self.cargar(DJANGO_DEBUG="0", DJANGO_SECRET_KEY="k" * 60, DJANGO_ALLOWED_HOSTS="ejemplo.cl,www.ejemplo.cl", DJANGO_PROXY_SSL="1")
        self.assertFalse(cfg["DEBUG"])
        self.assertEqual(cfg["ALLOWED_HOSTS"], ["ejemplo.cl", "www.ejemplo.cl"])
        self.assertTrue(cfg["SESSION_COOKIE_SECURE"] and cfg["CSRF_COOKIE_SECURE"] and cfg["SECURE_SSL_REDIRECT"])
        self.assertEqual(cfg["SECURE_PROXY_SSL_HEADER"], ("HTTP_X_FORWARDED_PROTO", "https"))

    def test_los_respaldos_y_la_cache_no_se_pueden_descargar_por_http(self):
        from django.test import Client

        self.carpeta.joinpath("respaldo.json").write_text("{}")
        c = Client()
        for ruta in ("/.cache_bencina/respaldo.json", "/respaldo.json", "/static/respaldo.json", "/static/../.cache_bencina/respaldo.json", "/media/respaldo.json"):
            self.assertEqual(c.get(ruta).status_code, 404, ruta)


class CredencialesEnLogsTests(ConCache):
    CLAVES = ("secreta", "p@ss", "tok123", "usuario")

    def setUp(self):
        super().setUp()
        p = mock.patch("viajes.bencina.API_BASE", "http://usuario:p@ss-secreta@api.ejemplo.cl/api?token=tok123")
        p.start()
        self.addCleanup(p.stop)

    def revisar(self, d, logs):
        texto = d.aviso + "\n" + "\n".join(logs.output)
        for clave in self.CLAVES:
            self.assertNotIn(clave, texto, clave)
        return texto

    def probar(self, **kw):
        with self.assertLogs("viajes.bencina", "DEBUG") as logs:
            with mock.patch("viajes.bencina.requests.get", **kw):
                d = bencina.obtener_datos(forzar=True)
        return d, logs

    def test_sanear_textos(self):
        f = bencina._sin_credenciales
        self.assertEqual(f("http://u:p@ss@host/x"), "http://***@host/x")
        self.assertEqual(f("https://u:p@h:8080/a?b=1"), "https://***@h:8080/a?b=1")
        self.assertEqual(f("GET /x?api_key=abc&z=1 y token=xyz"), "GET /x?api_key=***&z=1 y token=***")
        self.assertEqual(f("sin credenciales https://host/api"), "sin credenciales https://host/api")

    def test_http_con_url_con_credenciales(self):
        class R(RespuestaFalsa):
            url = "http://usuario:p@ss-secreta@api.ejemplo.cl/api/x?token=tok123"

            def raise_for_status(self):
                raise requests.HTTPError("403 Client Error: Forbidden for url: " + self.url, response=self)

        r = R(None, status=403)
        r.text = "denegado para http://usuario:p@ss-secreta@api.ejemplo.cl con token=tok123"
        d, logs = self.probar(return_value=r)
        texto = self.revisar(d, logs)
        self.assertIn("403", texto)  # el diagnóstico útil se conserva

    def test_respuesta_no_json_demasiado_grande_y_errores_de_conexion(self):
        for kw in (
            {"return_value": RespuestaFalsa(cuerpo=b"<html>http://usuario:p@ss-secreta@x token=tok123</html>")},
            {"return_value": RespuestaFalsa(cuerpo=b"x" * 5000)},
            {"side_effect": requests.ConnectionError("fallo http://usuario:p@ss-secreta@api.ejemplo.cl/api?token=tok123")},
            {"side_effect": requests.Timeout("lento http://usuario:p@ss-secreta@api.ejemplo.cl")},
            {"side_effect": requests.exceptions.SSLError("certificate has expired http://usuario:p@ss-secreta@api.ejemplo.cl")},
            {"side_effect": requests.RequestException("raro http://usuario:p@ss-secreta@api.ejemplo.cl")},
        ):
            with mock.patch("viajes.bencina.MAX_BYTES", 1000):
                d, logs = self.probar(**kw)
            self.revisar(d, logs)

    def test_marcas_que_fallan_con_credenciales_en_el_mensaje(self):
        def get(url, **k):
            if url.endswith("marca_ciudadano"):
                raise requests.ConnectionError("fallo http://usuario:p@ss-secreta@api.ejemplo.cl/marca?token=tok123")
            return api_falsa()(url, **k)

        d, logs = self.probar(side_effect=get)
        self.revisar(d, logs)
        self.assertTrue(d.ok)


# =====================================================================================================================
# Fuente RX Noticias (combustible.json). Todas estas pruebas son LOCALES: usan el archivo real recibido (viajes/data/
# combustible_inicial.json, generado el 2026-11-01 08:30 UTC) y respuestas simuladas; ninguna toca rxnoticias.com ni Nominatim.
# =====================================================================================================================
TABLA_REAL = bencina._tabla_coordenadas  # la función real, antes de que las pruebas la sustituyan
AMBAR = ("02101", "ARAMCO", "Av. Antonio Rendic 6850")  # Antofagasta; 93/95/97 del 29-oct y diésel del 23-oct


def huella(ruta):
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


def con_generado(contenido, generado):
    contenido["generado"] = generado
    return contenido


def sumar_a_precios(contenido, n=100):
    for c in contenido["comunas"]:
        for e in c["estaciones"]:
            for p in e["precios"].values():
                p["precio"] += n
    return contenido


def estaciones_crudas(contenido):
    return [(c, e) for c in contenido["comunas"] for e in c["estaciones"]]


def por_clave(estaciones):
    return {e["extra"]["clave"]: e for e in estaciones}


class RxLecturaTests(ConRx):
    """1-7 y 16: lectura del archivo real, normalización, claves, coordenadas, precios antiguos y atípicos."""

    def test_lee_el_json_completo(self):
        raw = cargar_rx()
        estaciones, descartados, generado = bencina.normalizar_rx(raw)
        self.assertEqual(len(raw["comunas"]), 314)
        self.assertEqual(len(estaciones_crudas(raw)), 1768)
        self.assertEqual(sum(len(e["precios"]) for _, e in estaciones_crudas(raw)), 7803)
        self.assertEqual(len(estaciones), 1768)
        self.assertEqual(generado, datetime(2026, 11, 1, 8, 30, 52))
        self.assertEqual(sum(len(e["combustibles"]) for e in estaciones) + descartados["precio_atipico"], 7803)

    def test_normaliza_estacion_y_combustibles(self):
        est = por_clave(bencina.normalizar_rx(cargar_rx())[0])[bencina.clave_estacion(*AMBAR)]
        self.assertEqual(set(est), {"id", "marca", "region", "comuna", "direccion", "lat", "lon", "precios", "combustibles", "extra"})
        self.assertEqual((est["marca"], est["region"], est["comuna"], est["direccion"]), ("ARAMCO", "Antofagasta", "Antofagasta", "Av. Antonio Rendic 6850"))
        self.assertEqual(est["precios"], {
            "93": {"precio": 1661.0, "fecha": "2026-10-29", "atencion": "Asistido"},
            "95": {"precio": 1694.0, "fecha": "2026-10-29", "atencion": "Asistido"},
            "97": {"precio": 1722.0, "fecha": "2026-10-29", "atencion": "Asistido"},
        })
        di = next(c for c in est["combustibles"] if c["codigo"] == "DI")  # el diésel no está en `precios` pero sí se conserva
        self.assertEqual((di["nombre"], di["precio"], di["fecha"], di["unidad"]), ("Diésel", 1654.0, "2026-10-23", "$/L"))
        self.assertEqual({c["codigo"] for c in est["combustibles"]}, {"93", "95", "97", "DI"})
        todos = {c["codigo"] for e in bencina.normalizar_rx(cargar_rx())[0] for c in e["combustibles"]}
        self.assertEqual(todos, {"93", "95", "97", "DI", "KE"})

    def test_autoservicio_y_asistido(self):
        raw = cargar_rx()
        _, e = next((c, e) for c, e in estaciones_crudas(raw) if any(p["autoservicio"] for p in e["precios"].values()))
        est = por_clave(bencina.normalizar_rx(raw)[0])
        clave = next(k for k, v in est.items() if v["direccion"] == e["direccion"] and v["marca"] == e["marca"])
        k_auto = next(k for k, p in e["precios"].items() if p["autoservicio"])
        self.assertTrue(any(c["codigo"] == k_auto and c["atencion"] == "AutoServicio" for c in est[clave]["combustibles"]))

    def test_id_interno_estable_unico_y_con_clave(self):
        a, b = bencina.normalizar_rx(cargar_rx())[0], bencina.normalizar_rx(cargar_rx())[0]
        self.assertEqual([e["id"] for e in a], [e["id"] for e in b])
        self.assertEqual(len({e["id"] for e in a}), 1768)
        self.assertTrue(all(e["id"].startswith("rx") and len(e["id"]) == 14 for e in a))
        self.assertEqual(len({e["extra"]["clave"] for e in a}), 1768)
        raw = cargar_rx()
        _, e = estaciones_crudas(raw)[0]
        antes = a[0]["id"]
        e["direccion"] += " Bis"  # una dirección distinta es, para este id, otra estación (límite conocido)
        self.assertNotEqual(bencina.normalizar_rx(raw)[0][0]["id"], antes)

    def test_normalizacion_de_la_clave(self):
        base = bencina.clave_estacion("02101", "COPEC", "AV ANTONIO RENDIC 6850")
        self.assertEqual(bencina.clave_estacion(" 02101 ", "Copec", "Av.  Antonio Rendic 6850,"), base)
        self.assertEqual(bencina.clave_estacion("02101", "copec", "ÁV. ANTONIO RÉNDIC, 6850"), base)
        self.assertEqual(bencina.clave_estacion("13101", "Shell", "Av. Peñalolén-Sur 10"), bencina.clave_estacion("13101", "SHELL", "AV PENALOLEN SUR 10"))
        # no se unen estaciones distintas
        for otra in (("02201", "COPEC", "AV ANTONIO RENDIC 6850"), ("02101", "SHELL", "AV ANTONIO RENDIC 6850"), ("02101", "COPEC", "AV ANTONIO RENDIC 6851")):
            self.assertNotEqual(bencina.clave_estacion(*otra), base)
        self.assertNotEqual(bencina.clave_estacion("1", "A", "KM 5,5"), bencina.clave_estacion("1", "A", "KM 55"))

    def test_coordenadas_recuperadas_de_la_tabla(self):
        estaciones, descartados, _ = bencina.normalizar_rx(cargar_rx())
        self.assertTrue(all(e["lat"] is not None and e["lon"] is not None for e in estaciones))
        self.assertNotIn("sin_coordenadas", descartados)
        self.assertNotIn("coordenada_dudosa", descartados)
        self.assertTrue(all(-56.5 <= e["lat"] <= -17 and -110 <= e["lon"] <= -66 for e in estaciones))
        est = por_clave(estaciones)[bencina.clave_estacion(*AMBAR)]
        self.assertAlmostEqual(est["lat"], -23.609271, 5)
        self.assertAlmostEqual(est["lon"], -70.384434, 5)

    def test_estacion_sin_coincidencia_se_conserva_sin_coordenadas(self):
        raw = cargar_rx()
        c, e = next((c, e) for c, e in estaciones_crudas(raw) if e["direccion"] == AMBAR[2])
        e["direccion"] = "Calle que no está en la tabla 123"
        estaciones, descartados, _ = bencina.normalizar_rx(raw)
        est = next(x for x in estaciones if x["direccion"] == e["direccion"])
        self.assertEqual((est["lat"], est["lon"]), (None, None))
        self.assertEqual(est["precios"]["93"]["precio"], 1661.0)  # sigue con sus precios
        self.assertEqual((len(estaciones), descartados["sin_coordenadas"]), (1768, 1))

    def test_claves_repetidas_no_reciben_coordenadas(self):
        raw = cargar_rx()
        c = next(c for c in raw["comunas"] if c["comuna"] == "Antofagasta")
        c["estaciones"].append(json.loads(json.dumps(next(e for e in c["estaciones"] if e["direccion"] == AMBAR[2]))))
        estaciones, descartados, _ = bencina.normalizar_rx(raw)
        iguales = [e for e in estaciones if e["extra"]["clave"] == bencina.clave_estacion(*AMBAR)]
        self.assertEqual(len(iguales), 2)
        self.assertEqual(len({e["id"] for e in iguales}), 2)
        self.assertTrue(all(e["lat"] is None for e in iguales))
        self.assertEqual(descartados["clave_repetida"], 2)

    def test_coordenada_que_no_corresponde_a_la_comuna_se_descarta(self):
        tabla = bencina._tabla_coordenadas()
        clave = bencina.clave_estacion(*AMBAR)
        tabla[clave] = (-53.16, -70.91)  # Punta Arenas, a 2.000 km de Antofagasta
        with mock.patch("viajes.bencina._tabla_coordenadas", return_value=tabla):
            estaciones, descartados, _ = bencina.normalizar_rx(cargar_rx())
        self.assertEqual(descartados["coordenada_dudosa"], 1)
        self.assertIsNone(por_clave(estaciones)[clave]["lat"])
        self.assertEqual(sum(1 for e in estaciones if e["lat"] is None), 1)  # el resto queda igual

    def test_coordenadas_invalidas_de_la_tabla_no_se_usan(self):
        tabla = bencina._tabla_coordenadas()
        clave = bencina.clave_estacion(*AMBAR)
        tabla[clave] = (0, 0)  # fuera de Chile
        with mock.patch("viajes.bencina._tabla_coordenadas", return_value=tabla):
            est = por_clave(bencina.normalizar_rx(cargar_rx())[0])[clave]
        self.assertIsNone(est["lat"])

    def test_precios_antiguos_se_conservan_con_su_fecha(self):
        raw = cargar_rx()
        antiguos = [(c["codigo_comuna"], e["direccion"], k, p["fecha"]) for c, e in estaciones_crudas(raw) for k, p in e["precios"].items()
                    if (AHORA_RX.date() - date.fromisoformat(p["fecha"])).days > 30]
        self.assertEqual(len(antiguos), 193)  # los que `vigente: false` marca (ya pasaron 30 días), incluido uno de hace 430 días
        estaciones = bencina.normalizar_rx(raw)[0]
        self.assertEqual(len(estaciones), 1768)  # ninguna estación se elimina por tener precios viejos
        for codigo, direccion, k, fecha in antiguos:
            est = next(e for e in estaciones if e["extra"]["codigo_comuna"] == codigo and e["direccion"] == direccion)
            self.assertIn((k, fecha), [(c["codigo"], c["fecha"]) for c in est["combustibles"]])
        viejo = min(c["fecha"] for e in estaciones for c in e["combustibles"])
        self.assertEqual(viejo, "2025-08-28")
        sin_vigentes = [e for c, e in estaciones_crudas(raw) if not any(p["vigente"] for p in e["precios"].values())]
        self.assertEqual(len(sin_vigentes), 37)  # estaciones sin ningún precio vigente: igual salen
        self.assertTrue(all(e["combustibles"] for e in estaciones))

    def test_precio_atipico_se_descarta(self):
        raw = cargar_rx()
        estaciones, descartados, _ = bencina.normalizar_rx(raw)
        self.assertEqual(descartados, {"precio_atipico": 1})
        est = next(e for e in estaciones if e["comuna"] == "Peñalolén" and e["direccion"] == "Avda. Consistorial 5410" and e["marca"] == "ECCO")
        self.assertNotIn("KE", [c["codigo"] for c in est["combustibles"]])  # el kerosene de valor 1
        self.assertTrue(est["precios"])  # la estación y sus otros precios siguen

    def test_precios_invalidos_y_fechas_ausentes(self):
        raw = cargar_rx()
        _, e = next((c, e) for c, e in estaciones_crudas(raw) if e["direccion"] == AMBAR[2])
        e["precios"]["93"]["precio"] = 99999
        e["precios"]["95"]["precio"] = "abc"
        e["precios"]["97"].pop("fecha")
        e["precios"]["DI"]["fecha"] = "no-es-fecha"
        est = por_clave(bencina.normalizar_rx(raw)[0])[bencina.clave_estacion(*AMBAR)]
        self.assertEqual(set(est["precios"]), {"97"})
        self.assertIsNone(est["precios"]["97"]["fecha"])  # no se inventa una fecha
        self.assertIsNone(next(c for c in est["combustibles"] if c["codigo"] == "DI")["fecha"])

    def test_estructuras_inutilizables(self):
        casos = {
            "lista": [], "texto": "hola", "sin comunas": {"generado": "2026-11-01T08:00:00+00:00"},
            "comunas vacías": {"generado": "2026-11-01T08:00:00+00:00", "comunas": []},
            "sin generado": {"comunas": cargar_rx()["comunas"]},
            "generado inválido": {"generado": "ayer", "comunas": cargar_rx()["comunas"]},
            "generado en el futuro": {"generado": "2026-12-31T08:00:00+00:00", "comunas": cargar_rx()["comunas"]},
            "estaciones sin dirección": {"generado": "2026-11-01T08:00:00+00:00", "comunas": [{"comuna": "X", "codigo_comuna": "1", "estaciones": [{"marca": "A", "precios": {}}]}]},
        }
        for nombre, contenido in casos.items():
            with self.subTest(nombre), self.assertRaises(ValueError):
                bencina.normalizar_rx(contenido)

    def test_generado_con_zona_horaria_se_lleva_a_utc(self):
        self.assertEqual(bencina._fecha_utc("2026-11-01T05:30:52-03:00"), datetime(2026, 11, 1, 8, 30, 52))
        self.assertEqual(bencina._fecha_utc("2026-11-01T08:30:52Z"), datetime(2026, 11, 1, 8, 30, 52))
        self.assertIsNone(bencina._fecha_utc("nada"))


class RxDescargaTests(ConRx):
    """8-15 y 17: descarga, respaldo y recuperación, con respuestas simuladas."""

    def bajar(self, contenido=None, **kw):
        with mock.patch("viajes.bencina.requests.get", side_effect=rx_falsa(contenido, **kw)):
            return bencina.obtener_datos(forzar=True)

    def caida(self, exc):
        with mock.patch("viajes.bencina.requests.get", side_effect=exc):
            return bencina.obtener_datos(forzar=True)

    def doc(self, previo=False):
        return json.loads(bencina._ruta_respaldo(previo).read_text(encoding="utf-8"))

    def reiniciar(self):  # cerrar y abrir Django: se pierde la memoria, queda el disco
        bencina.cache.clear()
        bencina._memo.clear()

    def comprobar_conserva(self, d, huella_antes, n=1768):
        self.assertEqual(d.fuente, "respaldo")
        self.assertEqual(len(d.estaciones), n)
        self.assertTrue(d.ok)
        self.assertEqual(huella(bencina._ruta_respaldo()), huella_antes)  # el respaldo no se tocó

    def test_la_url_y_la_peticion(self):
        self.assertEqual(bencina.FUENTE_URL, "https://rxnoticias.com/datos/combustible.json")
        with mock.patch("viajes.bencina.requests.get", side_effect=rx_falsa()) as get:
            bencina.obtener_datos(forzar=True)
        self.assertEqual(get.call_count, 1)
        kw = get.call_args.kwargs
        self.assertEqual(get.call_args.args[0], "https://rxnoticias.com/datos/combustible.json")
        self.assertIs(kw["verify"], True)  # TLS verificado
        self.assertEqual(kw["timeout"], bencina.TIMEOUT)
        self.assertTrue(kw["stream"])
        self.assertIn("proyectobencina", kw["headers"]["User-Agent"])

    def test_primera_descarga_guarda_el_respaldo(self):
        d = self.bajar()
        self.assertEqual((d.fuente, len(d.estaciones), d.generado), ("rx", 1768, datetime(2026, 11, 1, 8, 30, 52)))
        doc = self.doc()
        self.assertEqual((doc["version"], doc["fuente"], doc["generado"]), (1, "rx", "2026-11-01T08:30:52"))
        self.assertEqual(len(doc["estaciones"]), 1768)
        self.assertEqual(doc["crudo"]["rx"]["generado"], "2026-11-01T08:30:52+00:00")  # el archivo original
        self.assertEqual(doc["endpoints"]["combustible.json"], {"ok": True, "n": 1768})
        self.assertFalse(bencina._ruta_respaldo(previo=True).exists())

    def test_descargas_defectuosas_conservan_el_ultimo_respaldo(self):
        self.bajar()
        antes = huella(bencina._ruta_respaldo())
        raw = json.dumps(cargar_rx(), ensure_ascii=False).encode()
        casos = {
            "archivo vacío": RespuestaFalsa(cuerpo=b""),
            "corrupto": RespuestaFalsa(cuerpo=b'{"generado": "2026-11-02T08:30:52+00:00", "comunas": [{{{'),
            "truncado": RespuestaFalsa(cuerpo=raw[: len(raw) // 2]),
            "HTML en vez de JSON": RespuestaFalsa(json_invalido=True),
            "HTML de error 502": RespuestaFalsa(cuerpo=b"<html>502 Bad Gateway</html>", status=502),
            "HTTP 403": RespuestaFalsa({"error": "no"}, status=403),
            "JSON de otro tipo": RespuestaFalsa([1, 2, 3]),
            "sin comunas": RespuestaFalsa(con_generado({"comunas": []}, "2026-11-02T08:30:52+00:00")),
            "demasiado grande": RespuestaFalsa(cuerpo=raw),
        }
        for nombre, respuesta in casos.items():
            with self.subTest(nombre):
                self.reiniciar()
                with mock.patch("viajes.bencina.MAX_BYTES", 1000 if nombre == "demasiado grande" else bencina.MAX_BYTES):
                    d = self.bajar(respuesta)
                self.comprobar_conserva(d, antes)
                self.assertIn("RX Noticias", d.aviso)

    def test_errores_de_conexion_y_timeout_conservan_el_respaldo(self):
        self.bajar()
        antes = huella(bencina._ruta_respaldo())
        for exc in (requests.ConnectionError("sin red"), requests.Timeout(), requests.exceptions.SSLError("certificate has expired"),
                    requests.exceptions.ChunkedEncodingError("cortado")):
            with self.subTest(type(exc).__name__):
                self.reiniciar()
                self.comprobar_conserva(self.caida(exc), antes)

    def test_timeout_y_certificado_se_explican(self):
        self.assertIn("no respondió", self.caida(requests.Timeout()).aviso)
        d = self.caida(requests.exceptions.SSLError("certificate has expired"))
        self.assertIn("certificado TLS", d.aviso)
        self.assertIn("no se desactiva", d.aviso)

    def test_generado_mas_antiguo_no_reemplaza(self):
        self.bajar()
        antes = huella(bencina._ruta_respaldo())
        self.reiniciar()
        viejo = sumar_a_precios(con_generado(cargar_rx(), "2026-10-30T08:30:52+00:00"))
        d = self.bajar(viejo)
        self.comprobar_conserva(d, antes)
        self.assertIn("más antiguo", d.aviso)
        self.assertEqual(por_clave(d.estaciones)[bencina.clave_estacion(*AMBAR)]["precios"]["93"]["precio"], 1661.0)  # no los +100

    def test_mismo_generado_no_reemplaza(self):
        self.bajar()
        antes = huella(bencina._ruta_respaldo())
        self.reiniciar()
        igual = sumar_a_precios(cargar_rx())  # mismo `generado` pero precios distintos: no se toma
        d = self.bajar(igual)
        self.assertEqual((d.fuente, len(d.estaciones)), ("rx", 1768))  # la fuente respondió; simplemente no hay novedades
        self.assertEqual(por_clave(d.estaciones)[bencina.clave_estacion(*AMBAR)]["precios"]["93"]["precio"], 1661.0)
        self.assertEqual(huella(bencina._ruta_respaldo()), antes)
        self.assertFalse(bencina._ruta_respaldo(previo=True).exists())

    def test_generado_posterior_actualiza_y_rota_la_copia(self):
        self.bajar()
        self.reiniciar()
        nuevo = sumar_a_precios(con_generado(cargar_rx(), "2026-11-02T08:30:52+00:00"))
        with mock.patch("viajes.bencina.ahora", return_value=datetime(2026, 11, 2, 12)):
            d = self.bajar(nuevo)
        self.assertEqual((d.fuente, d.generado), ("rx", datetime(2026, 11, 2, 8, 30, 52)))
        self.assertEqual(por_clave(d.estaciones)[bencina.clave_estacion(*AMBAR)]["precios"]["93"]["precio"], 1761.0)
        self.assertEqual(self.doc()["generado"], "2026-11-02T08:30:52")
        self.assertEqual(self.doc(previo=True)["generado"], "2026-11-01T08:30:52")  # la anterior sigue guardada

    def test_generado_futuro_no_bloquea_las_actualizaciones_siguientes(self):
        self.bajar()
        antes = huella(bencina._ruta_respaldo())
        self.reiniciar()
        d = self.bajar(con_generado(cargar_rx(), "2030-01-01T00:00:00+00:00"))
        self.comprobar_conserva(d, antes)
        self.assertEqual(self.doc()["generado"], "2026-11-01T08:30:52")

    def test_descarga_incompleta_no_pisa_la_copia_buena(self):
        self.bajar()
        antes = huella(bencina._ruta_respaldo())
        recortada = con_generado(cargar_rx(), "2026-11-02T08:30:52+00:00")
        recortada["comunas"] = recortada["comunas"][:5]
        self.reiniciar()
        with mock.patch("viajes.bencina.ahora", return_value=datetime(2026, 11, 2, 12)):
            d = self.bajar(recortada)
        self.comprobar_conserva(d, antes)
        self.assertIn("incompleta", d.aviso)
        sin_precios = con_generado(cargar_rx(), "2026-11-02T08:30:52+00:00")
        for _, e in estaciones_crudas(sin_precios)[:1200]:
            e["precios"] = {}
        self.reiniciar()
        with mock.patch("viajes.bencina.ahora", return_value=datetime(2026, 11, 2, 12)):
            self.comprobar_conserva(self.bajar(sin_precios), antes)

    def test_sobrevive_al_reinicio_y_no_caduca(self):
        self.bajar()
        self.reiniciar()
        with mock.patch("viajes.bencina.ahora", return_value=datetime(2027, 12, 31)):  # más de un año después
            d = self.caida(requests.ConnectionError("sin red"))
        self.assertEqual((d.fuente, len(d.estaciones), d.generado), ("respaldo", 1768, datetime(2026, 11, 1, 8, 30, 52)))
        self.assertEqual(sum(1 for e in d.estaciones if e["precios"]), 1751)  # los precios viejos se muestran, no se descartan

    def test_respaldo_danado_usa_la_copia_previa(self):
        self.bajar()
        self.reiniciar()
        self.bajar(sumar_a_precios(con_generado(cargar_rx(), "2026-11-01T10:00:00+00:00")))
        bencina._ruta_respaldo().write_text("{ roto", encoding="utf-8")
        self.reiniciar()
        d = self.caida(requests.ConnectionError("sin red"))
        self.assertEqual((d.fuente, len(d.estaciones), d.generado), ("respaldo", 1768, datetime(2026, 11, 1, 8, 30, 52)))
        self.assertEqual(bencina._ruta_respaldo().read_text(encoding="utf-8"), "{ roto")  # y no se borró

    def test_sin_respaldo_y_sin_descarga_se_usan_los_datos_que_vienen_con_el_proyecto(self):
        with self.con_inicial():
            d = self.caida(requests.ConnectionError("sin red"))
        self.assertEqual((d.fuente, len(d.estaciones), d.generado), ("respaldo", 1768, datetime(2026, 11, 1, 8, 30, 52)))
        self.assertIn("vienen con el proyecto", d.aviso)
        self.assertFalse(bencina._ruta_respaldo().exists())  # no se hace pasar por una descarga
        self.assertEqual(min(c["fecha"] for e in d.estaciones for c in e["combustibles"]), "2025-08-28")  # cada precio con su fecha real

    def test_primera_descarga_defectuosa_tambien_cae_en_los_datos_iniciales(self):
        with self.con_inicial():
            d = self.bajar(RespuestaFalsa(cuerpo=b"<html>no</html>"))
        self.assertEqual((d.fuente, len(d.estaciones)), ("respaldo", 1768))
        self.assertFalse(bencina._ruta_respaldo().exists())

    def test_sin_nada_el_mensaje_es_claro_y_no_lanza_errores(self):
        with mock.patch("viajes.bencina.ARCHIVO_INICIAL", self.carpeta / "no-existe.json"):
            d = self.caida(requests.ConnectionError("sin red"))
        self.assertFalse(d.ok)
        self.assertIn("No hay precios guardados", d.aviso)

    def test_datos_iniciales_danados_no_rompen(self):
        malo = self.carpeta / "inicial.json"
        malo.write_text("{ roto", encoding="utf-8")
        with mock.patch("viajes.bencina.ARCHIVO_INICIAL", malo):
            self.assertFalse(self.caida(requests.ConnectionError("sin red")).ok)
        malo.write_text('{"generado": "2026-11-01T00:00:00+00:00", "comunas": []}', encoding="utf-8")
        bencina._memo.clear()
        with mock.patch("viajes.bencina.ARCHIVO_INICIAL", malo):
            self.assertFalse(self.caida(requests.ConnectionError("sin red")).ok)

    def test_tras_un_fallo_no_se_reintenta_de_inmediato(self):
        with mock.patch("viajes.bencina.requests.get", side_effect=requests.ConnectionError("sin red")) as get:
            bencina.obtener_datos()
            bencina.obtener_datos()
        self.assertEqual(get.call_count, 1)

    def test_la_descarga_nueva_se_recupera_despues_de_un_fallo(self):
        self.caida(requests.ConnectionError("sin red"))
        self.reiniciar()
        self.assertEqual(self.bajar().fuente, "rx")
        self.assertTrue(bencina._ruta_respaldo().exists())

    def test_las_coordenadas_de_la_copia_anterior_se_reutilizan(self):
        self.bajar()
        self.reiniciar()
        with mock.patch("viajes.bencina._tabla_coordenadas", return_value={}):  # sin tabla: solo queda lo ya guardado
            d = self.bajar(con_generado(cargar_rx(), "2026-11-01T10:00:00+00:00"))
        self.assertEqual(sum(1 for e in d.estaciones if e["lat"] is not None), 1768)

    def test_sin_tabla_ni_copia_la_pagina_sigue_con_precios_pero_sin_mapa(self):
        with mock.patch("viajes.bencina._tabla_coordenadas", return_value={}):
            d = self.bajar()
        self.assertEqual(len(d.estaciones), 1768)
        self.assertTrue(all(e["lat"] is None and e["precios"] is not None for e in d.estaciones))
        self.assertEqual(d.descartados["sin_coordenadas"], 1768)
        self.assertIn("no tienen coordenadas", d.aviso)
        self.assertTrue(bencina._ruta_respaldo().exists())

    def test_combustibles_que_rx_no_entrega_se_conservan_desde_la_copia_anterior(self):
        estaciones, descartados, _ = bencina.normalizar_rx(cargar_rx())
        glp = {"codigo": "GLP", "nombre": "GLP vehicular", "atencion": "AutoServicio", "unidad": "$/L", "precio": 905.0, "fecha": "2026-10-20 10:00:00", "extra": {}}
        gnc = {"codigo": "GNC", "nombre": "GNC", "atencion": "AutoServicio", "unidad": "$/m3", "precio": 780.0, "fecha": "2026-10-21 10:00:00", "extra": {}}
        viejo_93 = {"codigo": "A93", "nombre": "Gasolina 93", "atencion": "Asistido", "unidad": "$/L", "precio": 1.0, "fecha": "2026-01-01", "extra": {}}
        clave = bencina.clave_estacion(*AMBAR)
        # copia anterior tal como la dejó la versión con la API: id numérico, sin `generado`, sin código de comuna
        anterior = [dict(e, id=i, extra={}) for i, e in enumerate(estaciones)]
        est = next(e for e in anterior if e["id"] == next(i for i, x in enumerate(estaciones) if x["extra"]["clave"] == clave))
        est["combustibles"] = est["combustibles"] + [glp, gnc, viejo_93]
        doc = {"version": 1, "descargado": "2026-10-31T12:00:00", "completa": True, "estaciones": anterior, "descartados": {}}
        self.assertTrue(bencina._guardar_respaldo(doc))
        d = self.bajar()
        self.assertEqual(d.fuente, "rx")
        nueva = por_clave(d.estaciones)[clave]
        codigos = [c["codigo"] for c in nueva["combustibles"]]
        self.assertEqual(sorted(codigos), ["93", "95", "97", "DI", "GLP", "GNC"])  # GLP y GNC se conservan; el A93 viejo lo reemplaza RX
        glp_nuevo = next(c for c in nueva["combustibles"] if c["codigo"] == "GLP")
        self.assertEqual((glp_nuevo["precio"], glp_nuevo["fecha"], glp_nuevo["extra"]["origen"]), (905.0, "2026-10-20 10:00:00", "copia anterior"))
        self.assertEqual(set(nueva["precios"]), {"93", "95", "97"})  # el recomendador sigue viendo solo 93/95/97
        # y quedan también en el respaldo en disco
        self.assertIn("GLP", [c["codigo"] for e in self.doc()["estaciones"] if e["extra"].get("clave") == clave for c in e["combustibles"]])

    def test_archivo_local_con_formato_rx(self):
        with mock.patch("viajes.bencina.ARCHIVO_LOCAL", str(INICIAL)):
            d = bencina.obtener_datos()
        self.assertEqual((d.fuente, len(d.estaciones), d.generado), ("archivo", 1768, datetime(2026, 11, 1, 8, 30, 52)))

    def test_la_url_con_credenciales_no_se_muestra_ni_se_registra(self):
        url = "http://usuario:p@ss-secreta@rxnoticias.com/datos/combustible.json?token=tok123"
        error = requests.ConnectionError(f"no se pudo conectar con {url}")
        with mock.patch("viajes.bencina.FUENTE_URL", url), self.assertLogs("viajes.bencina", level="WARNING") as logs:
            d = self.caida(error)
        texto = d.aviso + " ".join(logs.output)
        for secreto in ("secreta", "p@ss", "tok123", "usuario"):
            self.assertNotIn(secreto, texto)


def sin_estaciones(contenido, cuantas):
    """Quita las últimas `cuantas` estaciones del archivo (las comunas se conservan)."""
    quitar = {id(e) for _, e in estaciones_crudas(contenido)[-cuantas:]}
    for c in contenido["comunas"]:
        c["estaciones"] = [e for e in c["estaciones"] if id(e) not in quitar]
    return contenido


class RxReferenciaInicialTests(ConRx):
    """Mientras no hay respaldo persistente, la referencia son los datos iniciales del proyecto (combustible_inicial.json, 1.768
    estaciones, 7.802 registros de precio, generado el 2026-11-01 08:30 UTC): una descarga igual, anterior, incompleta o defectuosa no
    los reemplaza. Con respaldo persistente, la referencia es ese respaldo."""
    MAS_NUEVO = "2026-11-02T08:30:52+00:00"

    def setUp(self):
        super().setUp()
        p = self.con_inicial()
        p.start()
        self.addCleanup(p.stop)

    def bajar(self, contenido=None, ahora=datetime(2026, 11, 2, 12), **kw):
        with mock.patch("viajes.bencina.requests.get", side_effect=rx_falsa(contenido, **kw)), \
                mock.patch("viajes.bencina.ahora", return_value=ahora):
            return bencina.obtener_datos(forzar=True)

    def sin_respaldo(self):
        self.assertFalse(bencina._ruta_respaldo().exists())  # la fuente inicial sigue siendo la única copia: no se escribió nada

    def test_la_referencia_inicial_es_la_esperada(self):
        ref = bencina._referencia_inicial()
        self.assertEqual((len(ref["estaciones"]), bencina._n_registros(ref["estaciones"])), (1768, 7802))
        self.assertEqual(bencina._fecha_utc(ref["generado"]), datetime(2026, 11, 1, 8, 30, 52))

    def test_descarga_con_el_mismo_generado_que_los_datos_iniciales_se_rechaza(self):
        d = self.bajar(sumar_a_precios(cargar_rx()))  # mismo `generado`, precios distintos
        self.assertEqual((d.fuente, len(d.estaciones)), ("rx", 1768))
        self.assertEqual(por_clave(d.estaciones)[bencina.clave_estacion(*AMBAR)]["precios"]["93"]["precio"], 1661.0)  # los de siempre
        self.sin_respaldo()

    def test_descarga_mas_antigua_que_los_datos_iniciales_se_rechaza(self):
        d = self.bajar(sumar_a_precios(con_generado(cargar_rx(), "2026-10-30T08:30:52+00:00")))
        self.assertEqual((d.fuente, len(d.estaciones), d.generado), ("respaldo", 1768, datetime(2026, 11, 1, 8, 30, 52)))
        self.assertIn("más antiguo", d.aviso)
        self.assertIn("que viene con el proyecto", d.aviso)
        self.assertEqual(por_clave(d.estaciones)[bencina.clave_estacion(*AMBAR)]["precios"]["93"]["precio"], 1661.0)
        self.sin_respaldo()

    def test_descarga_completa_y_mas_nueva_se_acepta_y_crea_el_respaldo(self):
        d = self.bajar(sumar_a_precios(con_generado(cargar_rx(), self.MAS_NUEVO)))
        self.assertEqual((d.fuente, len(d.estaciones), d.generado), ("rx", 1768, datetime(2026, 11, 2, 8, 30, 52)))
        self.assertEqual(por_clave(d.estaciones)[bencina.clave_estacion(*AMBAR)]["precios"]["93"]["precio"], 1761.0)
        self.assertEqual(json.loads(bencina._ruta_respaldo().read_text(encoding="utf-8"))["generado"], "2026-11-02T08:30:52")

    def test_descarga_nueva_pero_incompleta_no_reemplaza_los_datos_iniciales(self):
        casos = {
            "5 comunas": lambda c: c.update(comunas=c["comunas"][:5]),
            "mitad de las estaciones": lambda c: sin_estaciones(c, 884),
            "70 % de las estaciones": lambda c: sin_estaciones(c, 530),  # pasaría con el umbral de 50 %, no con el de 80 %
            "sin diésel ni kerosene": lambda c: [p.pop(k) for _, e in estaciones_crudas(c) for p in [e["precios"]] for k in ("DI", "KE") if k in p],
        }
        for nombre, rompe in casos.items():
            with self.subTest(nombre):
                self.reiniciar()
                nuevo = con_generado(cargar_rx(), self.MAS_NUEVO)
                rompe(nuevo)
                d = self.bajar(nuevo)
                self.assertEqual((d.fuente, len(d.estaciones), d.generado), ("respaldo", 1768, datetime(2026, 11, 1, 8, 30, 52)))
                self.assertIn("incompleta", d.aviso)
                self.assertIn("que viene con el proyecto", d.aviso)
                self.sin_respaldo()

    def test_el_motivo_distingue_estaciones_precios_y_registros(self):
        ref = {"estaciones": bencina._referencia_inicial()["estaciones"]}
        def validar(mod):
            nuevo = con_generado(cargar_rx(), self.MAS_NUEVO)
            mod(nuevo)
            return bencina._validar_descarga(bencina.normalizar_rx(nuevo)[0], ref, bencina.FRACCION_MIN_RX)
        self.assertIn("estaciones y la copia anterior tenía 1768", validar(lambda c: sin_estaciones(c, 530)))
        self.assertIn("registros de precio y la copia anterior tenía 7802", validar(lambda c: [e["precios"].pop("DI", None) or e["precios"].pop("KE", None) for _, e in estaciones_crudas(c)]))
        self.assertEqual(validar(lambda c: sin_estaciones(c, 100)), "")  # perder el 6 % de las estaciones es normal

    def test_descarga_un_poco_menor_pero_razonable_se_acepta(self):
        d = self.bajar(sin_estaciones(con_generado(cargar_rx(), self.MAS_NUEVO), 150))  # 91 % de las estaciones
        self.assertEqual((d.fuente, len(d.estaciones)), ("rx", 1618))
        self.assertTrue(bencina._ruta_respaldo().exists())

    def test_descargas_defectuosas_dejan_los_datos_iniciales_y_no_crean_respaldo(self):
        casos = {
            "vacía": RespuestaFalsa(cuerpo=b""), "HTML": RespuestaFalsa(json_invalido=True),
            "truncada": RespuestaFalsa(cuerpo=b'{"generado": "2026-11-02T08:30:52+00:00", "comunas": [{"comuna": "A"'),
            "sin comunas": RespuestaFalsa(con_generado({"comunas": []}, self.MAS_NUEVO)),
            "HTTP 500": RespuestaFalsa({"error": "x"}, status=500),
        }
        for nombre, respuesta in casos.items():
            with self.subTest(nombre):
                self.reiniciar()
                d = self.bajar(respuesta)
                self.assertEqual((d.fuente, len(d.estaciones)), ("respaldo", 1768))
                self.sin_respaldo()
        for exc in (requests.ConnectionError("sin red"), requests.Timeout()):
            with self.subTest(type(exc).__name__), mock.patch("viajes.bencina.requests.get", side_effect=exc):
                self.reiniciar()
                self.assertEqual(len(bencina.obtener_datos(forzar=True).estaciones), 1768)
                self.sin_respaldo()

    def test_con_respaldo_persistente_la_referencia_es_el_respaldo(self):
        self.assertEqual(self.bajar(con_generado(cargar_rx(), self.MAS_NUEVO)).fuente, "rx")  # crea el respaldo (generado 11-02)
        antes = huella(bencina._ruta_respaldo())
        self.reiniciar()
        # más nuevo que los datos iniciales, pero más antiguo que el respaldo: se rechaza
        d = self.bajar(sumar_a_precios(con_generado(cargar_rx(), "2026-11-02T01:00:00+00:00")))
        self.assertEqual(d.fuente, "respaldo")
        self.assertIn("que el guardado", d.aviso)
        self.assertEqual(huella(bencina._ruta_respaldo()), antes)
        # y la exigencia de datos completos también se mide contra el respaldo
        self.reiniciar()
        nuevo = sin_estaciones(con_generado(cargar_rx(), "2026-11-03T08:30:52+00:00"), 530)
        d = self.bajar(nuevo, ahora=datetime(2026, 11, 3, 12))
        self.assertEqual((d.fuente, len(d.estaciones)), ("respaldo", 1768))
        self.assertIn("se conserva el guardado", d.aviso)
        self.assertEqual(huella(bencina._ruta_respaldo()), antes)

    def test_sin_datos_iniciales_ni_respaldo_la_primera_descarga_se_acepta(self):
        with mock.patch("viajes.bencina.ARCHIVO_INICIAL", self.carpeta / "no-hay.json"):
            d = self.bajar(sin_estaciones(cargar_rx(), 800))  # no hay con qué comparar: solo rigen las validaciones propias
        self.assertEqual((d.fuente, len(d.estaciones)), ("rx", 968))

    def test_datos_iniciales_danados_no_impiden_la_primera_descarga(self):
        malo = self.carpeta / "malo.json"
        malo.write_text("{ roto", encoding="utf-8")
        with mock.patch("viajes.bencina.ARCHIVO_INICIAL", malo):
            d = self.bajar(con_generado(cargar_rx(), self.MAS_NUEVO))
        self.assertEqual((d.fuente, len(d.estaciones)), ("rx", 1768))

    def reiniciar(self):
        bencina.cache.clear()
        bencina._memo.clear()


class RxVistasTests(ConRx):
    """16: el mapa, los filtros y el recomendador reciben los mismos campos de siempre con los datos de RX Noticias."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        p = mock.patch("viajes.bencina.requests.get", side_effect=rx_falsa())
        self.get = p.start()
        self.addCleanup(p.stop)

    def pedir(self, **params):
        r = self.client.get("/estaciones.json", params)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_estaciones_json_con_los_campos_del_mapa(self):
        j = self.pedir()
        self.assertEqual((len(j["estaciones"]), j["total"], j["fuente"], j["completa"]), (1768, 1768, "rx", True))
        for e in j["estaciones"]:
            self.assertEqual(set(e), {"id", "marca", "region", "comuna", "direccion", "lat", "lon", "precios"})
            self.assertIsInstance(e["lat"], float)
            self.assertIsInstance(e["lon"], float)
            for comb, p in e["precios"].items():
                self.assertIn(comb, ("93", "95", "97"))  # lo que el mapa filtra por combustible
                self.assertEqual(set(p), {"precio", "fecha", "atencion"})
                self.assertIsInstance(p["precio"], float)
        self.assertEqual(len(j["regiones"]), 16)
        self.assertEqual(len(j["comunas"]), 312)  # las 314 del archivo menos 2 que no tienen ninguna estación
        self.assertIn({"nombre": "Antofagasta", "region": "Antofagasta"}, j["comunas"])
        self.assertEqual(len({e["id"] for e in j["estaciones"]}), 1768)

    def test_filtros_por_comuna_region_y_texto(self):
        raw = cargar_rx()
        en_comuna = lambda nombre: sum(len(c["estaciones"]) for c in raw["comunas"] if c["comuna"] == nombre)
        self.assertEqual(len(self.pedir(comuna="ANTOFAGASTA")["estaciones"]), en_comuna("Antofagasta"))
        self.assertEqual(len(self.pedir(comuna="penalolen")["estaciones"]), en_comuna("Peñalolén"))  # sin tildes ni mayúsculas
        self.assertEqual(len(self.pedir(region="Antofagasta")["estaciones"]), sum(len(c["estaciones"]) for c in raw["comunas"] if c["region"] == "Antofagasta"))
        aramco = self.pedir(q="aramco")["estaciones"]
        self.assertEqual(len(aramco), 301)
        self.assertTrue(all(e["marca"] == "ARAMCO" for e in aramco))

    def test_estacion_sin_coordenadas_sale_en_la_lista_con_lat_lon_nulos(self):
        raw = cargar_rx()
        _, e = next((c, e) for c, e in estaciones_crudas(raw) if e["direccion"] == AMBAR[2])
        e["direccion"] = "Dirección desconocida 1"
        self.get.side_effect = rx_falsa(raw)
        j = self.pedir()
        est = next(x for x in j["estaciones"] if x["direccion"] == "Dirección desconocida 1")
        self.assertEqual((est["lat"], est["lon"]), (None, None))  # mapa.js la omite del mapa pero no del resto
        self.assertEqual(len(j["estaciones"]), 1768)
        self.assertIn("1 estaciones no tienen coordenadas", j["aviso"])

    def test_el_recomendador_usa_los_precios_de_rx(self):
        r = self.client.post("/", {"accion": "recomendar", "vehiculo": "Kia Soluto 1.4", "km_mes": "800", "comuna": ""})
        self.assertContains(r, "Recomendado para tu Kia Soluto 1.4")
        rec = r.context["resultado"]["recomendado"]
        self.assertIn(rec["combustible"], ("93", "95", "97"))
        self.assertRegex(rec["fecha_txt"], r"^\d{4}-\d{2}-\d{2}$")  # la fecha propia del precio, sin hora inventada
        self.assertEqual(r.context["resultado"]["n_estaciones"], 1768)
        self.assertContains(r, "RX Noticias")
        self.assertContains(r, "CC BY 4.0")

    def test_con_la_fuente_caida_la_pagina_y_el_mapa_siguen_con_datos(self):
        self.pedir()  # primera descarga: guarda el respaldo
        bencina.cache.clear()
        self.get.side_effect = requests.ConnectionError("sin red")
        j = self.pedir()
        self.assertEqual((j["fuente"], len(j["estaciones"])), ("respaldo", 1768))
        self.assertIn("RX Noticias", j["aviso"])
        r = self.client.post("/", {"accion": "recomendar", "vehiculo": "Kia Soluto 1.4", "km_mes": "800", "comuna": ""})
        self.assertContains(r, "Datos guardados, no son precios actuales")
        self.assertContains(r, "Recomendado para tu Kia Soluto 1.4")

    def test_sin_copia_y_sin_fuente_el_mapa_usa_los_datos_del_proyecto(self):
        self.get.side_effect = requests.ConnectionError("sin red")
        with self.con_inicial():
            j = self.pedir()
        self.assertEqual((j["fuente"], len(j["estaciones"])), ("respaldo", 1768))

    def test_los_combustibles_extra_no_llegan_al_mapa(self):
        for e in self.pedir()["estaciones"]:
            self.assertNotIn("combustibles", e)
            self.assertNotIn("extra", e)


class NominatimTests(ConRx):
    """Geocodificación opcional (apagada por defecto): se prueba con respuestas simuladas, nunca contra OpenStreetMap."""

    def setUp(self):
        super().setUp()
        self.consultas = []
        self.dormidas = []
        for p in (mock.patch("viajes.bencina._tabla_coordenadas", return_value={}),
                  mock.patch("viajes.bencina.time.sleep", side_effect=self.dormidas.append),
                  mock.patch("viajes.bencina.requests.get", side_effect=self.responder),
                  mock.patch("viajes.bencina.NOMINATIM_MAX", 3)):
            p.start()
            self.addCleanup(p.stop)
        self.modo = "bien"
        self.contenido = cargar_rx()

    def responder(self, url, **kw):
        if url == bencina.FUENTE_URL:
            return RespuestaFalsa(self.contenido)
        self.assertEqual(url, bencina.NOMINATIM_URL)
        self.consultas.append(kw)
        if self.modo == "caido":
            raise requests.ConnectionError("sin red")
        p = kw["params"]
        n = p["street"].split()[0]
        ok = {"lat": "-33.45", "lon": "-70.65", "address": {"country_code": "cl", "house_number": n, "city": p["city"]}}
        numero = len(self.consultas)
        if self.modo == "bien":
            return RespuestaFalsa([ok] if numero == 1 else [ok, dict(ok, lat="-33.46")] if numero == 2 else [dict(ok, address=dict(ok["address"], house_number="1"))])
        return RespuestaFalsa([])

    def bajar(self):
        return bencina.obtener_datos(forzar=True)

    def test_apagado_por_defecto_no_hace_ninguna_consulta(self):
        d = self.bajar()
        self.assertEqual((self.consultas, self.dormidas), ([], []))
        self.assertEqual(d.descartados["sin_coordenadas"], 1768)

    def test_activo_cumple_la_politica_y_solo_acepta_coincidencias_claras(self):
        with mock.patch("viajes.bencina.NOMINATIM_ACTIVO", True), mock.patch("viajes.bencina.NOMINATIM_CONTACTO", "equipo@example.cl"):
            d = self.bajar()
        self.assertEqual(len(self.consultas), 3)  # el máximo por descarga
        self.assertEqual(len(self.dormidas), 2)  # 1 consulta por segundo como máximo
        self.assertTrue(all(s >= 1 for s in self.dormidas))
        kw = self.consultas[0]
        self.assertIn("proyectobencina", kw["headers"]["User-Agent"])  # se identifica
        self.assertIn("equipo@example.cl", kw["headers"]["User-Agent"])
        self.assertIs(kw["verify"], True)
        self.assertEqual(kw["params"]["country"], "Chile")
        con = [e for e in d.estaciones if e["lat"] is not None]
        self.assertEqual(len(con), 1)  # la única coincidencia clara: la ambigua (2 resultados) y la de otro número no se asignan
        self.assertEqual((con[0]["lat"], con[0]["lon"]), (-33.45, -70.65))

    def test_resultados_se_guardan_y_no_se_repiten_consultas(self):
        with mock.patch("viajes.bencina.NOMINATIM_ACTIVO", True):
            self.bajar()
            primeras = [kw["params"]["street"] + kw["params"]["city"] for kw in self.consultas]
            doc = json.loads(bencina._ruta_coordenadas().read_text(encoding="utf-8"))
            self.assertEqual((len(doc["coords"]), len(doc["sin_resultado"])), (1, 2))
            self.assertEqual(next(iter(doc["coords"].values()))["origen"], "nominatim")
            self.contenido = con_generado(cargar_rx(), "2026-11-01T10:00:00+00:00")
            self.modo = "vacio"
            self.consultas.clear()
            self.bajar()
        nuevas = [kw["params"]["street"] + kw["params"]["city"] for kw in self.consultas]
        self.assertEqual(len(nuevas), 3)
        self.assertFalse(set(nuevas) & set(primeras))  # ni la encontrada ni las sin resultado se vuelven a pedir
        self.assertEqual(len(json.loads(bencina._ruta_coordenadas().read_text(encoding="utf-8"))["sin_resultado"]), 5)

    def test_la_coordenada_guardada_se_usa_en_la_descarga_siguiente_sin_consultar(self):
        with mock.patch("viajes.bencina.NOMINATIM_ACTIVO", True):
            self.bajar()
        self.consultas.clear()
        bencina._memo.clear()
        # tabla real (semilla + coordenadas.json), con una semilla vacía para ver solo lo que dejó Nominatim
        with mock.patch("viajes.bencina._tabla_coordenadas", side_effect=TABLA_REAL), \
                mock.patch("viajes.bencina.ARCHIVO_SEMILLA", self.carpeta / "sin-semilla.json"):
            estaciones = bencina.normalizar_rx(cargar_rx())[0]
        self.assertEqual(sum(1 for e in estaciones if e["lat"] is not None), 1)
        self.assertEqual(self.consultas, [])

    def test_la_tabla_une_la_semilla_y_la_cache_y_la_cache_gana(self):
        semilla = json.loads(bencina.ARCHIVO_SEMILLA.read_text(encoding="utf-8"))["coords"]
        clave = bencina.clave_estacion(*AMBAR)
        self.assertEqual(len(semilla), 1808)
        bencina._ruta_coordenadas().write_text(json.dumps({"version": 1, "coords": {
            clave: {"lat": -23.5, "lon": -70.4}, "99999|X|Y": {"lat": -33.0, "lon": -70.0}}}), encoding="utf-8")
        tabla = TABLA_REAL()
        self.assertEqual(len(tabla), 1809)
        self.assertEqual(tuple(tabla[clave]), (-23.5, -70.4))  # la caché tiene prioridad sobre la semilla
        self.assertEqual(tuple(tabla["99999|X|Y"]), (-33.0, -70.0))
        self.assertEqual(len([k for k in tabla if k != clave and k != "99999|X|Y"]), 1807)  # el resto viene de la semilla
        bencina._ruta_coordenadas().write_text("{ roto", encoding="utf-8")  # una caché dañada no rompe: queda la semilla
        bencina._memo.clear()
        self.assertEqual(len(TABLA_REAL()), 1808)

    def test_un_error_de_red_no_rompe_la_descarga_ni_marca_estaciones(self):
        self.modo = "caido"
        with mock.patch("viajes.bencina.NOMINATIM_ACTIVO", True):
            d = self.bajar()
        self.assertEqual((d.fuente, len(d.estaciones)), ("rx", 1768))
        self.assertEqual(len(self.consultas), 1)  # se detiene en el primer error
        self.assertFalse(bencina._ruta_coordenadas().exists())  # y no marca nada como «sin resultado»

    def test_direcciones_sin_numero_o_de_carretera_no_se_consultan(self):
        for direccion in ("Ruta 5 Norte Km 570", "Camino Real s/n", "Sin numero alguno", "123"):
            with self.subTest(direccion):
                est = {"direccion": direccion, "comuna": "Vallenar", "region": "Atacama"}
                self.assertIsNone(bencina._geocodificar(est))
        self.assertEqual(self.consultas, [])

    def test_resultado_de_otro_pais_comuna_o_fuera_de_chile_se_rechaza(self):
        est = {"direccion": "Av. Siempre Viva 742", "comuna": "Providencia", "region": "Metropolitana"}
        base = {"lat": "-33.43", "lon": "-70.61", "address": {"country_code": "cl", "house_number": "742", "city": "Providencia"}}
        casos = {
            "otro país": dict(base, address=dict(base["address"], country_code="ar")),
            "otra comuna": dict(base, address=dict(base["address"], city="Las Condes")),
            "otro número": dict(base, address=dict(base["address"], house_number="7")),
            "coordenadas fuera de Chile": dict(base, lat="40.4", lon="-3.7"),
        }
        for nombre, resultado in casos.items():
            with self.subTest(nombre), mock.patch("viajes.bencina.requests.get", return_value=RespuestaFalsa([resultado])):
                self.assertIsNone(bencina._geocodificar(est))
        with mock.patch("viajes.bencina.requests.get", return_value=RespuestaFalsa([base])):
            self.assertEqual(bencina._geocodificar(est), (-33.43, -70.61))


# ---------- revisión final: archivos válidos como JSON pero con la estructura dañada ----------
EST_BUENA = {"id": "x1", "marca": "Copec", "region": "Región Metropolitana", "comuna": "Santiago", "direccion": "Calle 1",
             "lat": -33.4, "lon": -70.6, "precios": {"93": {"precio": 1300.0, "fecha": "2026-10-30", "atencion": ""}}}


class RespaldoIncompletoTests(ConRx):
    """Un respaldo con JSON válido y `version` correcta pero con estaciones incompletas daba error 500 en el mapa o en el
    recomendador (KeyError: 'comuna' / 'precio'). Ahora se trata como no utilizable, igual que uno corrupto."""

    DANADAS = {
        "solo id y precios": {"id": "x", "precios": {}},
        "sin comuna": {k: v for k, v in EST_BUENA.items() if k != "comuna"},
        "precio sin valor": dict(EST_BUENA, precios={"93": {"fecha": "2026-10-30"}}),
        "precio en texto": dict(EST_BUENA, precios={"93": {"precio": "caro", "fecha": "2026-10-30"}}),
        "precio NaN": dict(EST_BUENA, precios={"93": {"precio": float("nan"), "fecha": None}}),
        "precio booleano": dict(EST_BUENA, precios={"93": {"precio": True, "fecha": None}}),
        "fecha numérica": dict(EST_BUENA, precios={"93": {"precio": 1300.0, "fecha": 5}}),
        "coordenada en texto": dict(EST_BUENA, lat="a", lon="b"),
        "marca nula": dict(EST_BUENA, marca=None),
        "la estación no es un objeto": "texto",
    }

    def setUp(self):
        super().setUp()
        self.client = Client()

    def escribir(self, estaciones, previo=False):
        doc = {"version": bencina.VERSION_RESPALDO, "descargado": "2026-10-30T10:00:00+00:00", "estaciones": estaciones}
        ruta = bencina._ruta_respaldo(previo=previo)
        ruta.write_text(json.dumps(doc), encoding="utf-8")
        bencina._memo.clear()
        return ruta

    def sin_red(self):
        return mock.patch("viajes.bencina.requests.get", side_effect=requests.ConnectionError("sin red"))

    def test_estaciones_incompletas_no_se_usan_ni_dan_500_y_el_archivo_no_se_toca(self):
        for nombre, est in self.DANADAS.items():
            with self.subTest(nombre):
                ruta = self.escribir([EST_BUENA, est])
                antes = ruta.read_bytes()
                with self.con_inicial(), self.sin_red():
                    bencina.cache.clear()  # sin el bloqueo de reintento del subcaso anterior
                    d = bencina.obtener_datos()
                    mapa = self.client.get("/estaciones.json")
                    pagina = self.client.post("/", {"accion": "recomendar", "vehiculo": "Kia Soluto 1.4", "km_mes": "1000"})
                self.assertEqual((d.fuente, d.inicial, len(d.estaciones)), ("respaldo", True, 1768))  # cae en los datos del proyecto
                self.assertEqual((mapa.status_code, pagina.status_code), (200, 200))
                self.assertNotContains(pagina, "No hay precios guardados")
                self.assertEqual(ruta.read_bytes(), antes)  # y no se borró ni se modificó
                bencina.cache.clear()

    def test_un_respaldo_incompleto_usa_la_copia_previa_valida(self):
        self.escribir([EST_BUENA, self.DANADAS["sin comuna"]])
        self.escribir([EST_BUENA], previo=True)
        with self.con_inicial(), self.sin_red():
            d = bencina.obtener_datos()
        self.assertEqual((d.fuente, d.inicial, [e["id"] for e in d.estaciones]), ("respaldo", False, ["x1"]))

    def test_un_respaldo_completo_sigue_funcionando(self):
        self.escribir([EST_BUENA, dict(EST_BUENA, id="x2", lat=None, lon=None, precios={})])
        with self.sin_red():
            d = bencina.obtener_datos()
            pagina = self.client.post("/", {"accion": "recomendar", "vehiculo": "Kia Soluto 1.4", "km_mes": "1000"})
        self.assertEqual((d.fuente, len(d.estaciones)), ("respaldo", 2))
        self.assertContains(pagina, "$1.300/L")


class ArchivoLocalInvalidoTests(ConCache):
    def test_json_que_no_es_un_objeto_o_con_estructura_rara_no_lanza_errores(self):
        casos = {"lista": [], "null": None, "número": 3, "comunas en texto": {"comunas": "x"},
                 "estaciones en número": {"estaciones": 5}, "estaciones en texto": {"estaciones": ["a"]}}
        for nombre, contenido in casos.items():
            with self.subTest(nombre):
                ruta = self.carpeta / "local.json"
                ruta.write_text(json.dumps(contenido), encoding="utf-8")
                with mock.patch("viajes.bencina.ARCHIVO_LOCAL", str(ruta)):
                    d = bencina.obtener_datos()
                    r = Client().get("/estaciones.json")
                self.assertFalse(d.ok)
                self.assertEqual(r.status_code, 503)

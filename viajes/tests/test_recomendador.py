from unittest import mock

import requests
from django.test import Client

from viajes import bencina, views

from .base import ConCache, ConRx, api_falsa, cargar_muestra


def estaciones_muestra():
    muestra = cargar_muestra()
    return bencina.normalizar_respuesta(muestra["estaciones"], muestra["marcas"])[0]


def auto(rendimiento=15.0, octanaje=None):
    return {"nombre": "Auto de prueba", "rendimiento": rendimiento, "octanaje_recomendado": octanaje, "fuente": "prueba"}


class BuscarAutoTests(ConCache):
    def test_exacto_parcial_sin_tildes(self):
        self.assertEqual(views.buscar_auto("Kia Soluto 1.4")[0]["nombre"], "Kia Soluto 1.4")
        self.assertEqual(views.buscar_auto("soluto")[0]["nombre"], "Kia Soluto 1.4")
        self.assertEqual(views.buscar_auto("citroen")[0]["nombre"], "Citroën C3 1.2")

    def test_ambiguo_inexistente_y_vacio(self):
        self.assertIn("varios parecidos", views.buscar_auto("suzuki")[1])
        self.assertIn("No encontramos", views.buscar_auto("Tesla")[1])
        self.assertIn("Escribe", views.buscar_auto("  ")[1])


class RecomendarTests(ConCache):
    def setUp(self):
        super().setUp()
        self.estaciones = estaciones_muestra()

    def test_elige_el_precio_mas_bajo_de_cada_combustible(self):
        r = views.recomendar_combustible(auto(15.0), 1000, self.estaciones)
        for o in r["opciones"]:
            minimo = min(e["precios"][o["combustible"]]["precio"] for e in self.estaciones if o["combustible"] in e["precios"])
            self.assertEqual(o["precio"], minimo)
            self.assertAlmostEqual(o["costo_km"], minimo / 15.0)
            self.assertAlmostEqual(o["gasto_mes"], minimo / 15.0 * 1000)

    def test_recomienda_el_mas_barato_si_el_octanaje_no_se_conoce(self):
        r = views.recomendar_combustible(auto(octanaje=None), 1000, self.estaciones)
        self.assertEqual(r["recomendado"], r["opciones"][0])
        self.assertTrue(any("no tenemos verificado" in j for j in r["justificacion"]))

    def test_respeta_el_octanaje_del_fabricante(self):
        r = views.recomendar_combustible(auto(octanaje=97), 1000, self.estaciones)
        self.assertEqual(r["recomendado"]["combustible"], "97")
        self.assertTrue(any("recomienda mínimo 97" in j for j in r["justificacion"]))
        self.assertEqual([o["cumple"] for o in r["opciones"] if o["combustible"] == "93"], [False])

    def test_incluye_estacion_y_fecha_del_precio(self):
        r = views.recomendar_combustible(auto(), 500, self.estaciones)
        rec = r["recomendado"]
        self.assertIn("·", rec["estacion"])
        self.assertTrue(rec["fecha_txt"])

    def test_sin_precios_devuelve_error(self):
        sin_precios = [dict(e, precios={}) for e in self.estaciones]
        self.assertIn("error", views.recomendar_combustible(auto(), 500, sin_precios))
        self.assertIn("error", views.recomendar_combustible(auto(), 500, []))

    def test_desempate_estable(self):
        a = {"id": 2, "marca": "A", "direccion": "x", "comuna": "c", "precios": {"93": {"precio": 1500.0, "fecha": None, "atencion": ""}}}
        b = dict(a, id=1)
        r1 = views.recomendar_combustible(auto(), 1, [a, b])
        r2 = views.recomendar_combustible(auto(), 1, [b, a])
        self.assertEqual(r1["recomendado"]["estacion"], r2["recomendado"]["estacion"])


class VistaTests(ConCache):
    def setUp(self):
        super().setUp()
        self.client = Client()
        parche = mock.patch("viajes.bencina.requests.get", side_effect=api_falsa())
        parche.start()
        self.addCleanup(parche.stop)

    def recomendar(self, **extra):
        datos = {"accion": "recomendar", "vehiculo": "Kia Soluto 1.4", "km_mes": "800,5", "comuna": ""}
        datos.update(extra)
        return self.client.post("/", datos)

    def test_get_funciona_sin_consultar_la_api(self):
        with mock.patch("viajes.bencina.requests.get") as get:
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        get.assert_not_called()

    def test_recomendacion_con_precios_reales(self):
        r = self.recomendar()
        self.assertContains(r, "Recomendado para tu Kia Soluto 1.4")
        self.assertContains(r, "Precio informado:")
        self.assertContains(r, "Bencina en Línea (CNE)")
        self.assertEqual(r.context["resultado"]["recomendado"]["combustible"], "93")

    def test_comuna_filtra_las_estaciones(self):
        r = self.recomendar(comuna="punta arenas")
        self.assertEqual(r.context["resultado"]["ambito"], "punta arenas")
        self.assertTrue(all("Punta Arenas" in o["estacion"] for o in r.context["resultado"]["opciones"]))

    def test_comuna_sin_estaciones(self):
        r = self.recomendar(comuna="Atlantida")
        self.assertContains(r, "No hay estaciones en la comuna")
        self.assertNotIn("resultado", r.context)

    def test_api_caida_no_rompe_la_pagina(self):
        with mock.patch("viajes.bencina.requests.get", side_effect=requests.Timeout()):
            r = self.recomendar()
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "No hay precios guardados")

    def test_api_caida_con_respaldo_avisa(self):
        self.recomendar()  # llena el respaldo
        bencina.cache.delete(bencina.K_FRESCO)
        with mock.patch("viajes.bencina.requests.get", side_effect=requests.Timeout()):
            r = self.recomendar()
        self.assertContains(r, "últimos precios obtenidos")
        self.assertIsNotNone(r.context["resultado"])

    def test_entradas_invalidas(self):
        r = self.recomendar(vehiculo="Tesla", km_mes="abc")
        self.assertContains(r, "No encontramos ese vehículo")
        self.assertContains(r, "Los km al mes deben ser un número")
        self.assertContains(self.recomendar(km_mes="0"), "hasta 30000")

    def test_calculadora_de_repostaje_sigue_igual(self):
        r = self.client.post("/", {"accion": "repostaje", "litros": "40", "precio_litro": "1350"})
        self.assertContains(r, "$54.000")
        self.assertContains(r, "40 L × $1.350/L")
        r = self.client.post("/", {"accion": "repostaje", "litros": "0", "precio_litro": "x"})
        self.assertContains(r, "Los litros deben ser un número")
        self.assertContains(r, "El precio por litro debe ser un número")


class SelectorMarcaModeloTests(ConCache):
    def test_agrupa_por_marca_y_ordena(self):
        g = views.agrupar_por_marca([("Toyota Yaris 1.5", 1, None, ""), ("Suzuki Swift 1.2 ISG", 1, None, ""), ("Toyota RAV4 2.0 AWD", 1, None, "")])
        self.assertEqual(list(g), ["Suzuki", "Toyota"])
        self.assertEqual(g["Toyota"], [["Toyota RAV4 2.0 AWD", "RAV4 2.0 AWD", False], ["Toyota Yaris 1.5", "Yaris 1.5", False]])

    def test_todos_los_autos_quedan_en_algun_modelo(self):
        autos = [(a["nombre"],) for a in views.catalogo.values()]
        g = views.agrupar_por_marca(autos)
        self.assertEqual(sum(len(v) for v in g.values()), len(autos))
        self.assertIn("Kia Soluto 1.4", [m[0] for v in g.values() for m in v])

    def test_la_pagina_trae_los_selectores_y_conserva_el_campo_de_texto(self):
        r = Client().get("/")
        self.assertContains(r, 'id="catalogo-marcas"')
        self.assertContains(r, 'id="sel-marca"')
        self.assertContains(r, 'id="dl-modelos"')  # marca y modelo se pueden escribir y sugieren opciones
        self.assertContains(r, 'name="vehiculo"')  # sin JavaScript sigue funcionando la búsqueda manual
        self.assertContains(r, 'data-ajax="#resultado-recomendador"')
        self.assertContains(r, 'data-ajax="#resultado-repostaje"')


class BusquedaConCatalogoGrandeTests(ConCache):
    def test_un_unico_auto_de_chile_gana_sobre_los_referenciales(self):
        for q, esperado in [("Mazda CX-5", "Mazda CX-5 2.0"), ("Toyota RAV4", "Toyota RAV4 2.0 AWD"),
                            ("Subaru Crosstrek", "Subaru Crosstrek 2.0 AWD"), ("Hyundai Accent", "Hyundai Accent HB 1.4")]:
            self.assertEqual(views.buscar_auto(q)[0]["nombre"], esperado, q)

    def test_si_hay_varios_de_chile_se_listan_primero_los_de_chile(self):
        auto, error = views.buscar_auto("toyota yaris")
        self.assertIsNone(auto)
        self.assertIn("Toyota Yaris 1.5", error)
        self.assertIn("Toyota Yaris Cross 1.5", error)

    def test_los_33_originales_se_encuentran_por_su_nombre_exacto(self):
        for n, *_ in views.AUTOS:
            self.assertEqual(views.buscar_auto(n)[0]["nombre"], n)
            self.assertFalse(views.buscar_auto(n.lower())[0]["referencial"])


class CatalogoReferencialTests(ConCache):
    def setUp(self):
        super().setUp()
        self.client = Client()
        parche = mock.patch("viajes.bencina.requests.get", side_effect=api_falsa())
        parche.start()
        self.addCleanup(parche.stop)

    def test_el_catalogo_crecio_y_los_33_originales_no_cambian(self):
        ref = [a for a in views.catalogo.values() if a["referencial"]]
        self.assertGreater(len(ref), 1000)
        originales = [a for a in views.catalogo.values() if not a["referencial"]]
        self.assertEqual(len(originales), len(views.AUTOS))
        soluto = views.catalogo[views.normalizar("Kia Soluto 1.4")]
        self.assertEqual((soluto["rendimiento"], soluto["octanaje_recomendado"]), (17.5, 93))

    def test_los_referenciales_son_validos_y_sin_octanaje(self):
        for a in views.catalogo.values():
            if a["referencial"]:
                self.assertGreater(a["rendimiento"], 0)
                self.assertLess(a["rendimiento"], 40)
                self.assertIsNone(a["octanaje_recomendado"])  # no se inventa el octanaje
                self.assertTrue(a["origen"] and a["fuente"])
                self.assertNotIn("  ", a["nombre"])

    def test_un_referencial_nunca_pisa_a_uno_de_chile(self):
        for n, r, o, f in views.AUTOS:
            self.assertFalse(views.catalogo[views.normalizar(n)]["referencial"])
            self.assertEqual(views.catalogo[views.normalizar(n)]["rendimiento"], r)

    def test_marcas_de_varias_palabras(self):
        g = views.agrupar_por_marca([(a["nombre"],) for a in views.catalogo.values()])
        self.assertIn("Land Rover", g)
        self.assertNotIn("Land", g)

    def test_resultado_de_un_referencial_avisa_que_es_referencial(self):
        nombre = next(a["nombre"] for a in views.catalogo.values() if a["referencial"] and a["origen"] == "Canadá")
        r = self.client.post("/", {"accion": "recomendar", "vehiculo": nombre, "km_mes": "800", "comuna": ""})
        self.assertContains(r, "Dato referencial")
        self.assertContains(r, "otro mercado")
        self.assertContains(r, "Todavía no tenemos verificado el octanaje")  # recomienda el más barato y avisa
        self.assertTrue(r.context["resultado"]["referencial"])

    def test_modelo_premium_en_canada_trae_advertencia_pero_no_cambia_la_eleccion(self):
        premium = next(a["nombre"] for a in views.catalogo.values() if a.get("premium") == "Sí")
        normal = next(a["nombre"] for a in views.catalogo.values() if a.get("premium") == "No")
        r = self.client.post("/", {"accion": "recomendar", "vehiculo": premium, "km_mes": "800", "comuna": ""})
        self.assertContains(r, "figura con gasolina premium")
        self.assertIsNone(r.context["resultado"]["oct_rec"])  # no se inventa el octanaje
        r = self.client.post("/", {"accion": "recomendar", "vehiculo": normal, "km_mes": "800", "comuna": ""})
        self.assertNotContains(r, "gasolina premium")

    def test_resultado_de_un_auto_chileno_no_dice_referencial(self):
        r = self.client.post("/", {"accion": "recomendar", "vehiculo": "Kia Soluto 1.4", "km_mes": "800", "comuna": ""})
        self.assertNotContains(r, "Dato referencial")


class EstacionesJsonTests(ConCache):
    def setUp(self):
        super().setUp()
        self.client = Client()

    def test_devuelve_estaciones_con_coordenadas_y_precios(self):
        with mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()):
            r = self.client.get("/estaciones.json")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(j["fuente"], "api")
        self.assertEqual(len(j["estaciones"]), 35)
        e = next(x for x in j["estaciones"] if x["id"] == 400)  # coordenadas con coma en la fuente
        self.assertEqual((e["lat"], e["lon"]), (-32.873774, -71.2423))
        self.assertEqual(set(e["precios"]), {"93", "95", "97"})
        self.assertEqual(set(e["precios"]["93"]), {"precio", "fecha", "atencion"})
        self.assertGreaterEqual(j["descartados"]["precio_invalido"], 5)

    def test_estaciones_sin_precio_valido_siguen_en_la_lista(self):
        with mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()):
            j = self.client.get("/estaciones.json").json()
        e = next(x for x in j["estaciones"] if x["id"] == 2136)  # los tres precios eran 1.000
        self.assertEqual(e["precios"], {})
        self.assertIsNotNone(e["lat"])

    def test_error_de_la_fuente_responde_503_con_mensaje(self):
        with mock.patch("viajes.bencina.requests.get", side_effect=requests.Timeout()):
            r = self.client.get("/estaciones.json")
        self.assertEqual(r.status_code, 503)
        self.assertIn("No hay precios guardados", r.json()["error"])
        self.assertEqual(r.json()["estaciones"], [])

    def test_solo_acepta_get(self):
        self.assertEqual(self.client.post("/estaciones.json").status_code, 405)

    def test_la_pagina_incluye_el_mapa(self):
        r = self.client.get("/")
        self.assertContains(r, 'id="mapa"')
        self.assertContains(r, "/estaciones.json")
        self.assertContains(r, "viajes/mapa.js")


class EstacionesJsonFiltrosTests(ConCache):
    """Filtros del mapa: region, comuna y q (valores esperados contados a mano sobre la muestra de 35 estaciones)."""

    def setUp(self):
        super().setUp()
        self.client = Client()

    def pedir(self, **params):
        with mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()):
            r = self.client.get("/estaciones.json", params)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_sin_filtros_devuelve_todo_y_las_listas_para_los_selectores(self):
        j = self.pedir()
        self.assertEqual((len(j["estaciones"]), j["total"]), (35, 35))
        self.assertEqual(len(j["regiones"]), 8)
        self.assertIn({"nombre": "Quillota", "region": "Valparaíso"}, j["comunas"])
        self.assertEqual(j["max_dias"], 30)

    def test_comuna_sin_importar_tildes_ni_mayusculas(self):
        j = self.pedir(comuna="PROVIDENCIA")
        self.assertEqual(len(j["estaciones"]), 5)
        self.assertEqual({e["comuna"] for e in j["estaciones"]}, {"Providencia"})
        self.assertEqual(j["total"], 35)  # total sigue siendo el del país

    def test_region(self):
        j = self.pedir(region="magallanes")
        self.assertEqual(len(j["estaciones"]), 5)
        self.assertTrue(all("Magallanes" in e["region"] for e in j["estaciones"]))

    def test_texto_libre_por_marca(self):
        j = self.pedir(q="shell")
        self.assertIn(400, {e["id"] for e in j["estaciones"]})
        self.assertTrue(all("SHELL" in e["marca"].upper() for e in j["estaciones"]))

    def test_filtros_combinados(self):
        j = self.pedir(region="Valparaíso", comuna="Quillota")
        self.assertEqual([e["id"] for e in j["estaciones"]], [400])
        self.assertEqual(self.pedir(region="Atacama", comuna="Quillota")["estaciones"], [])

    def test_sin_coincidencias_no_es_un_error(self):
        j = self.pedir(comuna="Atlantida", q="zzzz")
        self.assertEqual((j["estaciones"], j["total"]), ([], 35))

    def test_los_filtros_no_dejan_a_las_estaciones_sin_formato_comun(self):
        e = self.pedir(comuna="Quillota")["estaciones"][0]
        self.assertEqual(set(e), {"id", "marca", "region", "comuna", "direccion", "lat", "lon", "precios"})

    def test_parametros_enormes_o_raros_no_rompen(self):
        self.assertEqual(self.pedir(q="a" * 5000, comuna="<script>")["estaciones"], [])


class CasosLimiteTests(ConCache):
    def setUp(self):
        super().setUp()
        self.client = Client()

    def pedir(self, **datos):
        with mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()):
            return self.client.post("/", dict({"accion": "recomendar", "vehiculo": "Kia Soluto 1.4", "km_mes": "1000"}, **datos))

    def estacion(self, id_, **precios):
        return {"id": id_, "marca": "M", "direccion": "d", "comuna": "c",
                "precios": {k: {"precio": float(v), "fecha": None, "atencion": ""} for k, v in precios.items()}}

    def test_sin_combustible_compatible_no_dice_que_cumple(self):
        r = views.recomendar_combustible(auto(octanaje=97), 1000, [self.estacion(1, **{"93": 1500})])
        self.assertEqual(r["recomendado"]["combustible"], "93")
        texto = " ".join(r["justificacion"])
        self.assertNotIn("cumple el octanaje", texto)
        self.assertIn("Ninguna de las estaciones", texto)

    def test_ids_de_distinto_tipo_con_mismo_precio_no_fallan(self):
        r = views.recomendar_combustible(auto(), 1, [self.estacion(1, **{"93": 1500}), self.estacion("A-2", **{"93": 1500})])
        self.assertEqual(r["recomendado"]["precio"], 1500)

    def test_km_en_los_limites(self):
        for km, ok in [("30000", True), ("30001", False), ("0,5", True), ("-1", False), ("", False), ("1e400", False), ("nan", False), ("9" * 400, False)]:
            r = self.pedir(km_mes=km)
            self.assertEqual(r.context.get("resultado") is not None, ok, km)

    def test_comuna_inexistente_y_comuna_con_tildes(self):
        self.assertContains(self.pedir(comuna="Narnia"), "No hay estaciones en la comuna")
        for comuna in ("  VALPARAÍSO  ", "valparaiso", "punta arenas"):
            self.assertIsNotNone(self.pedir(comuna=comuna).context.get("resultado"), comuna)

    def test_vehiculos_ambiguos_inexistentes_y_con_espacios(self):
        self.assertContains(self.pedir(vehiculo="   "), "Escribe el nombre")
        self.assertContains(self.pedir(vehiculo="Tesla Model 3"), "No encontramos")
        self.assertIsNotNone(self.pedir(vehiculo="  KIA   soluto ").context.get("resultado"))

    def test_repostaje_limites_y_decimales(self):
        def rep(l, p):
            return self.client.post("/", {"accion": "repostaje", "litros": l, "precio_litro": p})

        self.assertContains(rep("500", "10000"), "$5.000.000")
        self.assertContains(rep("40,55", "1350"), "40,55 L")  # el detalle no redondea a un decimal
        self.assertContains(rep("40,55", "1350"), "$54.743")  # 54742,5 redondea hacia arriba
        self.assertContains(rep("0,5", "1350"), "$675")
        for l, p in [("501", "1000"), ("-3", "1000"), ("", "1000"), ("40", "10001"), ("40", "0"), ("abc", "xyz"), ("1e999", "1"), ("40", "nan")]:
            r = rep(l, p)
            self.assertIsNone(r.context.get("repostaje"), (l, p))
            self.assertEqual(r.status_code, 200)

    def test_json_con_filtros_raros(self):
        with mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()):
            for q in ("?comuna=%00", "?region=" + "x" * 500, "?q=%25%27%22<>", "?comuna=&region=&q="):
                r = self.client.get("/estaciones.json" + q)
                self.assertEqual(r.status_code, 200, q)
            self.assertEqual(self.client.post("/estaciones.json").status_code, 405)

    def test_json_503_si_no_hay_datos(self):
        with mock.patch("viajes.bencina.requests.get", side_effect=requests.ConnectionError()):
            r = self.client.get("/estaciones.json")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["estaciones"], [])


class RespaldoVisibleTests(ConCache):
    def datos_respaldo(self):
        self.client = Client()
        with mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()):
            bencina.obtener_datos()
        bencina.cache.delete(bencina.K_FRESCO)

    def test_recomendador_y_json_marcan_los_datos_guardados(self):
        self.datos_respaldo()
        with mock.patch("viajes.bencina.requests.get", side_effect=requests.ConnectionError("sin red")):
            r = self.client.post("/", {"accion": "recomendar", "vehiculo": "Kia Soluto 1.4", "km_mes": "1000"})
            j = self.client.get("/estaciones.json").json()
        self.assertContains(r, "Datos guardados, no son precios actuales")
        self.assertContains(r, "la última descarga correcta fue el")
        self.assertEqual(j["fuente"], "respaldo")
        self.assertTrue(j["consultado"])
        self.assertTrue(j["estaciones"])

    def test_con_datos_actuales_no_aparece_la_etiqueta(self):
        self.client = Client()
        with mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()):
            r = self.client.post("/", {"accion": "recomendar", "vehiculo": "Kia Soluto 1.4", "km_mes": "1000"})
        self.assertNotContains(r, "Datos guardados")


class RespaldoEnArchivoTests(ConCache):
    def test_los_ultimos_datos_sobreviven_a_un_reinicio(self):
        import tempfile
        from django.core.cache.backends.filebased import FileBasedCache

        with tempfile.TemporaryDirectory() as carpeta:
            with mock.patch("viajes.bencina.cache", FileBasedCache(carpeta, {})), \
                    mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()):
                self.assertEqual(bencina.obtener_datos().fuente, "api")
            # «reinicio»: caché nueva sobre la misma carpeta, sin entradas «frescas» y con la API caída
            nueva = FileBasedCache(carpeta, {})
            nueva.delete(bencina.K_FRESCO)
            with mock.patch("viajes.bencina.cache", nueva), \
                    mock.patch("viajes.bencina.requests.get", side_effect=requests.ConnectionError("sin red")):
                d = bencina.obtener_datos()
            self.assertEqual(d.fuente, "respaldo")
            self.assertTrue(d.ok)


# ---------- revisión final: precios antiguos y avisos ----------
def estacion_con_fecha(id_, fecha, precio=1300.0, comb="93"):
    return {"id": id_, "marca": "Copec", "region": "R", "comuna": "Santiago", "direccion": "Calle 1", "lat": None, "lon": None,
            "precios": {comb: {"precio": precio, "fecha": fecha, "atencion": ""}}}


class PrecioAntiguoTests(ConCache):
    """El reloj de ConCache está en el 27-oct-2026 12:00; MAX_DIAS = 30."""

    def recomendar(self, fecha, octanaje=None):
        return views.recomendar_combustible(auto(15.0, octanaje), 1000, [estacion_con_fecha(1, fecha)])

    def test_un_precio_reciente_no_se_marca_como_antiguo(self):
        r = self.recomendar("2026-10-25")
        self.assertFalse(r["recomendado"]["antiguo"])
        self.assertFalse(any("antiguo" in j for j in r["justificacion"]))

    def test_limite_de_30_dias(self):
        self.assertFalse(self.recomendar("2026-09-27")["recomendado"]["antiguo"])  # hace 30 días
        self.assertTrue(self.recomendar("2026-09-26")["recomendado"]["antiguo"])  # hace 31 días

    def test_un_precio_antiguo_se_advierte_con_su_fecha_y_no_cambia_el_calculo(self):
        r = self.recomendar("2026-08-01")
        rec = r["recomendado"]
        self.assertTrue(rec["antiguo"])
        self.assertEqual(rec["dias"], 87)
        texto = " ".join(r["justificacion"])
        self.assertIn("01-08-2026", texto)
        self.assertIn("hace 87 días", texto)
        self.assertAlmostEqual(rec["costo_km"], 1300 / 15.0)  # solo se advierte: el cálculo y la elección son los mismos
        self.assertAlmostEqual(rec["gasto_mes"], 1300 / 15.0 * 1000)

    def test_sin_fecha_o_con_fecha_ilegible_no_se_da_por_vigente(self):
        for fecha in (None, "", "no es una fecha"):
            with self.subTest(fecha):
                r = self.recomendar(fecha)
                self.assertTrue(r["recomendado"]["antiguo"])
                self.assertIsNone(r["recomendado"]["dias"])
                self.assertTrue(any("no trae una fecha válida" in j for j in r["justificacion"]))

    def test_una_fecha_futura_no_da_dias_negativos(self):
        self.assertEqual(self.recomendar("2026-12-01")["recomendado"]["dias"], 0)

    def test_se_advierte_tambien_una_opcion_antigua_que_no_es_la_recomendada(self):
        estaciones = [estacion_con_fecha(1, "2026-10-25", 1300.0, "93"), estacion_con_fecha(2, "2026-06-01", 1400.0, "97")]
        r = views.recomendar_combustible(auto(15.0, 97), 1000, estaciones)
        self.assertEqual(r["recomendado"]["combustible"], "97")
        self.assertTrue(r["recomendado"]["antiguo"])
        self.assertFalse([o for o in r["opciones"] if o["combustible"] == "93"][0]["antiguo"])
        self.assertTrue(any("El precio usado es del 01-06-2026" in j for j in r["justificacion"]))

    def test_la_pagina_muestra_el_aviso_y_la_fecha_del_precio_antiguo(self):
        datos = bencina.Datos([estacion_con_fecha(1, "2026-08-01")], "api", bencina.ahora(), "", {})
        with mock.patch("viajes.bencina.obtener_datos", return_value=datos):
            r = Client().post("/", {"accion": "recomendar", "vehiculo": "Kia Soluto 1.4", "km_mes": "1000"})
        self.assertContains(r, "precio antiguo")
        self.assertContains(r, "2026-08-01")
        self.assertContains(r, "hace 87 días")

    def test_la_pagina_no_marca_un_precio_reciente(self):
        datos = bencina.Datos([estacion_con_fecha(1, "2026-10-26")], "api", bencina.ahora(), "", {})
        with mock.patch("viajes.bencina.obtener_datos", return_value=datos):
            r = Client().post("/", {"accion": "recomendar", "vehiculo": "Kia Soluto 1.4", "km_mes": "1000"})
        self.assertNotContains(r, "precio antiguo")


class AvisosDeOctanajeTests(ConCache):
    def pagina(self, vehiculo, estaciones):
        datos = bencina.Datos(estaciones, "api", bencina.ahora(), "", {})
        with mock.patch("viajes.bencina.obtener_datos", return_value=datos):
            return Client().post("/", {"accion": "recomendar", "vehiculo": vehiculo, "km_mes": "1000"})

    def test_octanaje_sin_verificar_se_avisa_de_forma_visible(self):
        # un auto real del catálogo sin octanaje verificado: el aviso no depende de la lista «¿Por qué?»
        sin_verificar = next(a["nombre"] for a in views.catalogo.values() if a["octanaje_recomendado"] is None)
        r = self.pagina(sin_verificar, [estacion_con_fecha(1, "2026-10-26")])
        self.assertContains(r, "Octanaje del fabricante sin verificar")

    def test_octanaje_verificado_no_muestra_el_aviso_de_sin_verificar(self):
        r = self.pagina("Kia Soluto 1.4", [estacion_con_fecha(1, "2026-10-26")])
        self.assertNotContains(r, "Octanaje del fabricante sin verificar")

    def test_si_ningun_precio_cumple_el_octanaje_la_recomendada_queda_marcada(self):
        # auto que pide 97 y una sola estación con 93: ninguna opción cumple, se muestra la más barata sin decir que cumple
        r = views.recomendar_combustible(auto(15.0, 97), 1000, [estacion_con_fecha(1, "2026-10-26", 1300.0, "93")])
        self.assertFalse(r["recomendado"]["cumple"])
        self.assertFalse(any("cumple el octanaje" in j for j in r["justificacion"]))


class DatosInicialesEnLaPaginaTests(ConRx):
    def test_con_los_datos_del_proyecto_no_dice_consultado_ni_descarga_correcta(self):
        with self.con_inicial(), mock.patch("viajes.bencina.requests.get", side_effect=requests.ConnectionError("sin red")):
            r = Client().post("/", {"accion": "recomendar", "vehiculo": "Kia Soluto 1.4", "km_mes": "1000"})
            j = Client().get("/estaciones.json").json()
        self.assertContains(r, "Datos guardados, no son precios actuales")
        self.assertContains(r, "vienen con el proyecto")
        self.assertNotContains(r, "la última descarga correcta")
        self.assertNotContains(r, ", consultado ")
        self.assertTrue(j["inicial"])
        self.assertEqual(j["fuente"], "respaldo")

    def test_con_una_descarga_valida_inicial_es_falso(self):
        from .base import cargar_rx, rx_falsa
        contenido = cargar_rx()
        contenido["generado"] = "2026-11-01T11:00:00+00:00"
        with self.con_inicial(), mock.patch("viajes.bencina.requests.get", side_effect=rx_falsa(contenido)):
            j = Client().get("/estaciones.json").json()
        self.assertFalse(j["inicial"])


class NumerosChilenosTests(ConCache):
    """El punto agrupa miles («1.200» = 1200) y la coma es el decimal («0,5» = 0,5), como se escribe en Chile."""

    def test_a_numero_miles_y_decimales(self):
        casos = {
            "1200": 1200, "1.200": 1200, "12.500": 12500, "1.350": 1350, "1.200.000": 1200000, "+1.200": 1200,
            "1.350,5": 1350.5, "1.000,25": 1000.25, "0,5": 0.5, "800,5": 800.5, "40,55": 40.55,
            "40.5": 40.5, "1.25": 1.25, "0.500": 0.5, "1349.9": 1349.9, "  1.200  ": 1200, "-1.200": -1200,
        }
        for texto, esperado in casos.items():
            self.assertEqual(views.a_numero(texto), esperado, texto)

    def test_a_numero_coma_con_tres_digitos_es_decimal(self):
        # Decisión: la coma siempre es decimal («1,200» = 1,2), nunca separador de miles.
        self.assertEqual(views.a_numero("1,200"), 1.2)
        self.assertEqual(views.a_numero("1,350"), 1.35)

    def test_a_numero_invalidos(self):
        for texto in ["", "   ", "abc", "nan", "inf", "-inf", "1e999", "$1.350", "1 200", "1.200,5.5", "1,200,5", "1.35.5", "1.200.00", None]:
            self.assertIsNone(views.a_numero(texto), repr(texto))

    def recomendar(self, km):
        with mock.patch("viajes.bencina.requests.get", side_effect=api_falsa()):
            return self.client.post("/", {"accion": "recomendar", "vehiculo": "Kia Soluto 1.4", "km_mes": km, "comuna": ""})

    def setUp(self):
        super().setUp()
        self.client = Client()

    def test_km_con_punto_de_miles_da_lo_mismo_que_sin_punto(self):
        sin = self.recomendar("1200").context["resultado"]["recomendado"]["gasto_txt"]
        con = self.recomendar("1.200").context["resultado"]["recomendado"]["gasto_txt"]
        self.assertEqual(con, sin)
        self.assertEqual(self.recomendar("12.500").context["resultado"]["recomendado"]["gasto_mes"],
                         self.recomendar("12500").context["resultado"]["recomendado"]["gasto_mes"])

    def test_km_sobre_el_maximo_se_rechaza(self):
        for km in ("30.001", "1.200.000"):
            r = self.recomendar(km)
            self.assertIsNone(r.context.get("resultado"), km)
            self.assertContains(r, "hasta 30000")
        self.assertIsNotNone(self.recomendar("30.000").context.get("resultado"))

    def test_repostaje_con_miles(self):
        def rep(l, p):
            return self.client.post("/", {"accion": "repostaje", "litros": l, "precio_litro": p})

        r = rep("40", "1.350")
        self.assertContains(r, "$54.000")
        self.assertContains(r, "40 L × $1.350/L")
        self.assertContains(rep("40", "1.350,5"), "$54.020")  # 40 × 1350,5 = 54020
        self.assertContains(rep("0,5", "1.350"), "$675")
        self.assertContains(rep("500", "10.000"), "$5.000.000")  # en el límite

    def test_litros_con_miles_se_rechazan_por_el_maximo(self):
        # «1.000» litros es 1000 (no 1) y supera el máximo de 500.
        for litros in ("1.000", "40.500"):
            r = self.client.post("/", {"accion": "repostaje", "litros": litros, "precio_litro": "1350"})
            self.assertIsNone(r.context.get("repostaje"), litros)
            self.assertContains(r, "hasta 500")
        r = self.client.post("/", {"accion": "repostaje", "litros": "40", "precio_litro": "10.001"})
        self.assertIsNone(r.context.get("repostaje"))
        self.assertContains(r, "hasta 10000")

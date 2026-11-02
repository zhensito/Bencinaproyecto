# Bencuenta · Recomendador de combustible (Equipo 2 «Los Árabes», IWG400)

**Bencuenta** es una aplicación web en Django que usa **precios reales de bencinas en Chile** (RX Noticias, con datos de la CNE / Bencina en Línea) para decidir qué combustible conviene y cuánto se gasta. «Los Árabes» es el nombre del equipo; la página se llama Bencuenta.

## Funcionalidades

- **Recomendador de combustible.** Se elige el vehículo, los km al mes y, opcionalmente, una comuna. La app calcula para cada combustible (93, 95 y 97) el **costo por km = precio por litro ÷ rendimiento del auto** y el **gasto mensual = costo por km × km al mes**, usando la estación más barata de cada combustible (en la comuna indicada o en todo Chile). Recomienda el más barato **que cumpla el octanaje que pide el fabricante**; muestra el costo por km, el gasto mensual, la estación más barata con la fecha de su precio, todas las opciones ordenadas por costo/km y una explicación de por qué se recomienda ese combustible.
- **Selección del vehículo.** Dos modos: «Marca y modelo» (se escribe o se elige de una lista que sugiere opciones) y «Escribir» (búsqueda libre del nombre completo; si hay varios parecidos, el mensaje los lista). Sin JavaScript queda el campo de texto.
- **Catálogo de autos.** 33 modelos «de Chile» y cerca de 2.000 modelos referenciales de otros mercados (ver «Catálogo ampliado»). Cada resultado indica la fuente del rendimiento.
- **Calculadora de repostaje.** Litros × precio por litro = costo total (litros mayores que 0 y hasta 500; precio mayor que 0 y hasta 10.000). No depende de la API.
- **Mapa de estaciones** (Leaflet, mapa base de Esri). Muestra cada estación con coordenadas válidas, coloreada según el precio del combustible elegido (93, 95 o 97): verde, amarillo y rojo dividen las estaciones con precio en tres tramos de igual cantidad, y gris es «sin precio informado». La leyenda indica los rangos en pesos. Al tocar una estación se abre un globo con su dirección y los precios 93/95/97 con fecha y tipo de atención.
- **Filtros del mapa.** Región, comuna (con sugerencias), búsqueda por marca o dirección, botones 93/95/97, «Limpiar» y «Centrar mapa». La lista «Las más baratas» (hasta 15 estaciones) acompaña al mapa y al elegir una estación el mapa se centra en ella y abre su globo.
- **Ventana «Fuentes»** (menú superior): lista de dónde sale cada dato. Los mensajes de la página indican además la fecha de consulta, si se usan datos guardados o un archivo local y cuántos precios se descartaron.

### Diseño de la interfaz

- Estilo cálido en crema y verde, con tarjetas redondeadas, iconos coherentes y tipografías alojadas en el proyecto; adaptado a escritorio, tablet y móvil.
- Animaciones sutiles: botones que reaccionan al cursor y al clic, resultados y tarjetas de la lista que aparecen suavemente, indicadores de carga (en el botón al enviar y en el mapa mientras llegan las estaciones), la estación elegida queda resaltada y la ventana de fuentes se abre con una transición.
- **Fondo del encabezado:** carrusel horizontal automático con fotos panorámicas de Chile (Santiago, San Pedro de Atacama, Cordillera de la Sal, Valle de la Luna, Torres del Paine, Carretera Austral, Valparaíso, Pucón, Lago Llanquihue y Viña del Mar). El título va abajo a la izquierda sobre un degradado que sube desde el borde inferior (el paisaje queda limpio arriba) y las tarjetas se montan sobre el borde de la foto. Cada 8 segundos la foto se desliza lentamente a la siguiente, con un zoom muy suave mientras permanece, y la fila es continua, sin saltos. Un pie arriba a la derecha, alineado con el contenido, indica el lugar y enlaza a la página de la foto.
- **Carga y calidad de las fotos:** mientras llegan se ve un fondo verde con montañas y un brillo suave, y al aparecer la primera foto se funde sin que nada cambie de color. Las fotos se piden de a poco: en la fila nunca hay más de tres (más las que están en camino), y cada vez que una sale se pide la siguiente de la lista, así que no se bajan todas a la vez ni se acumulan. Se omiten automáticamente las que fallan, tardan más de 12 segundos, miden menos de 1.100 px de ancho, son verticales o casi cuadradas (se verían estiradas) o son demasiado oscuras; el carrusel sigue con las demás. Con fotos claras se refuerza el degradado detrás del texto. Si ninguna foto sirve, queda el fondo verde sin el brillo de carga. Con movimiento reducido se pide una sola foto y no hay carrusel. Si la pestaña se oculta a mitad de un desplazamiento, este se cierra en el acto. Las fotos no están en el repositorio: se cargan desde Wikimedia Commons (enlaces, autores y licencias en la ventana «Fuentes»).
- Se respeta la preferencia de **movimiento reducido** del sistema: con ella activada no hay animaciones ni desplazamiento suave, y el fondo se queda en la primera foto.

## Tecnologías

| Parte | Tecnología |
|---|---|
| Servidor | Python y Django (plantillas y vistas; sin base de datos); `requests` para consultar la API |
| Datos de precios | `combustible.json` de RX Noticias (datos de la CNE), sin token |
| Interfaz | HTML, CSS propio (`estilos.css`, con variables de diseño) y JavaScript sin frameworks |
| Mapa | Leaflet 1.9.4 (desde `unpkg.com`) y mapa base Esri World Street Map (`server.arcgisonline.com`) |
| Fotos del encabezado | Wikimedia Commons (enlazadas, no incluidas en el proyecto) |
| Tipografías e iconos | Nunito y Fraunces (archivos `.woff2` en el proyecto); iconos Lucide (sprite SVG en `_iconos.html`) |
| Pruebas | `unittest` de Django |

## Estructura

| Archivo | Qué hace |
|---|---|
| `viajes/bencina.py` | Consulta, normaliza y guarda en caché los datos de la API; filtra estaciones |
| `viajes/views.py` | Catálogo de autos, búsqueda de vehículos, cálculo de la recomendación, calculadora de repostaje y el endpoint `/estaciones.json` del mapa |
| `viajes/templates/viajes/inicio.html` | Página completa (se sirve en `/`); `_iconos.html` guarda los iconos |
| `viajes/static/viajes/estilos.css` | Todos los estilos y animaciones |
| `viajes/static/viajes/formulario.js` | Selector de marca/modelo, envío de cada formulario sin recargar, ventana de fuentes y carrusel de fotos del encabezado |
| `viajes/static/viajes/mapa.js` | Mapa, marcadores, leyenda, filtros y lista de estaciones |
| `viajes/data/catalogo_referencial.csv` | Modelos referenciales de otros mercados |
| `viajes/tests/` | Pruebas automáticas y la muestra real de la API |

Cada formulario se envía con JavaScript y solo reemplaza su propio resultado, de modo que calcular un repostaje no borra la recomendación. Si JavaScript falla, el envío normal recarga la página y sigue funcionando.

## Requisitos

- Python 3.11 o superior (probado con 3.13).
- Dependencias: `Django` y `requests` (ver `requirements.txt`).
- Conexión a internet para obtener los precios y cargar el mapa (Leaflet viene de `unpkg.com` y los mosaicos de `server.arcgisonline.com`). Las tipografías e iconos van incluidos en el proyecto.

No hay base de datos que configurar ni credenciales: la API de precios es pública y no usa token.

## Ejecutar en local

Desde la carpeta que contiene `manage.py` (en PowerShell):

```powershell
python -m pip install -r requirements.txt
python manage.py runserver
```

Abrir <http://127.0.0.1:8000/>. (Django avisará de migraciones sin aplicar: se puede ignorar, la app no usa base de datos.)

Opcional: crear antes un entorno virtual con `python -m venv venv` y activarlo con `venv\Scripts\activate`.

## Datos de prueba

| Caso | Entrada | Resultado esperado |
|---|---|---|
| 1. Principal | Vehículo `Kia Soluto 1.4`, `800` km/mes, comuna vacía | Recomienda bencina **93** y muestra costo por km, gasto mensual, estación más barata y fecha del precio. |
| 2. Alternativo | Vehículo `Tesla` (o un `km` con texto como `abc`) | Mensaje de error claro («No encontramos ese vehículo…» / «Los km al mes deben ser un número…»). |
| 3. Sin estaciones | Comuna `Atlantida` | «No hay estaciones en la comuna…». |
| Mapa | Elegir región `Magallanes…` o escribir `Providencia` en Comuna | El mapa y la lista «Las más baratas» se actualizan; el texto sobre el mapa indica «N estaciones (de TOTAL)». |

Los precios cambian cada día, por eso los resultados no traen cifras fijas.

## Sin internet o para una demostración estable

`BENCINA_JSON_LOCAL` hace que la app lea un archivo en vez de la API. Se incluye una muestra real (35 estaciones, 22 al 26 de octubre de 2026):

```powershell
$env:BENCINA_JSON_LOCAL = "viajes\tests\fixtures\muestra_api.json"
python manage.py runserver
```

La página avisa «archivo local» para no pasar esos datos por actuales.

Para desplegar fuera de tu equipo, la configuración se puede cambiar con variables de entorno (sin ellas todo sigue como en desarrollo): `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0` y `DJANGO_ALLOWED_HOSTS` (lista separada por comas).

La estación que viene repetida en la respuesta de la API (mismo `id`) se cuenta una sola vez. Sin conexión a internet el mapa no carga (Leaflet y los mosaicos son externos), pero el recomendador sí.

## Fuente de datos: RX Noticias

Los precios y estaciones salen de `https://rxnoticias.com/datos/combustible.json` (variable `BENCINA_FUENTE_URL`), que reemplaza a la API de Bencina en Línea (cuyo servidor tuvo el certificado TLS vencido). La ruta heredada de la API sigue en el código solo como opción (`BENCINA_FUENTE=api`) y no se usa por defecto.

**Atribución y licencia.** RX Noticias (2026). *Precios de combustible por comuna*. Fuente de origen: Comisión Nacional de Energía (CNE), publicada en Bencina en Línea. RX Noticias declara licenciar su recopilación (limpieza, orden por comuna y formato) bajo **CC BY 4.0**; los datos de origen son de la CNE y se publican bajo las condiciones de la CNE, que **no verificamos**. Las coordenadas vienen de la tabla del proyecto [diland9/precios-combustibles-chile](https://github.com/diland9/precios-combustibles-chile) (código MIT; datos públicos CNE/SEC; su README pide citar la fuente original).

**Qué trae y qué no.** Por estación: marca, dirección, comuna, región, código de comuna y, por combustible (93, 95, 97, diésel y kerosene), precio, fecha del precio, `vigente` y `autoservicio`. **No trae identificador de estación ni coordenadas, ni GLP ni GNC.** El archivo tiene además `generado` (cuándo lo generó RX Noticias), que **no es la fecha de los precios**: cada precio tiene la suya. `vigente` solo significa «el precio tiene hasta 30 días»; no se usa para descartar nada.

**Cómo se actualiza.** Cada hora (caché de 1 h) se descarga el archivo (con tiempo de espera, límite de tamaño y verificación TLS) y solo reemplaza al respaldo si es válido **y su `generado` es estrictamente posterior** al del respaldo guardado. Si el `generado` es igual, se conserva el respaldo (la fuente respondió, simplemente no hay novedades); si es anterior, se rechaza; una fecha de generación en el futuro (más de un día) también, para que no bloquee las actualizaciones siguientes. Además del `generado`, rigen las validaciones de siempre (mínimo de 10 estaciones, al menos la mitad con precios, sin perder más de la mitad respecto de la copia anterior).

**Si la fuente falla** (sin conexión, timeout, error HTTP, certificado, archivo vacío, corrupto, truncado, HTML en vez de JSON o demasiado grande), la página sigue con el **último respaldo válido**, sin límite de antigüedad y con la fecha de cada precio a la vista. Si no hay respaldo, usa `viajes/data/combustible_inicial.json` (copia del archivo recibido el 1-nov-2026, generado 08:30 UTC), marcado como datos guardados y **no como descarga**. Solo si tampoco existe, la página dice que no hay precios. Los precios viejos **no se eliminan** ni se eliminan estaciones por ello.

**Identificador de estación.** El archivo no tiene uno, así que el `id` interno es `rx` + 12 caracteres de un hash de la clave `código de comuna | marca | dirección`, con la marca y la dirección normalizadas (mayúsculas, sin tildes ni puntuación). **Límite:** si una estación cambia de marca o de dirección, la clave y el `id` cambian y se tratará como otra estación (pierde su coordenada hasta que se vuelva a encontrar). Si dos estaciones comparten clave, se les asigna un `id` distinto y ninguna recibe coordenadas.

**Coordenadas.** Se buscan, en este orden, en: (1) la tabla `viajes/data/coordenadas_semilla.json` (1.808 estaciones del snapshot del 30-10-2026 de diland9) más `.cache_bencina/coordenadas.json` (lo que se vaya guardando, que tiene prioridad); (2) la estación equivalente del respaldo anterior. Se descarta la que cae fuera de Chile o a más de 150 km de la mediana de las demás estaciones de su comuna (se juzga solo con 5 o más). Una estación sin coordenadas **se conserva en los listados y precios** y el mapa la omite; la página avisa cuántas son. Con el archivo del 1-nov-2026, las 1.768 estaciones encontraron coordenadas (prueba local, no contra la fuente real).

**OpenStreetMap (Nominatim), preparado y apagado.** Con `BENCINA_NOMINATIM=1` se consulta Nominatim por las estaciones sin coordenadas, **hasta `BENCINA_NOMINATIM_MAX` (5) por descarga y 1 consulta por segundo**, con `User-Agent` propio (puede incluir `BENCINA_NOMINATIM_CONTACTO`). Solo se acepta una coincidencia clara: dirección con número (no «Km», «Ruta» ni «s/n»), un único resultado en Chile con el mismo número y la misma comuna. Los resultados, también los «sin resultado» (no se repiten antes de 30 días), se guardan en `coordenadas.json`. **No se probó contra OpenStreetMap** desde el entorno de desarrollo.

## Cómo se manejan los datos

- `viajes/bencina.py` descarga y normaliza la fuente; `viajes/views.py` solo usa el resultado.
- **Caché de 1 hora** (en archivo, en `.cache_bencina/cache_django/`, separada del respaldo: limpiar la caché de Django no toca los respaldos). Si una consulta falla no se reintenta durante 60 s.
- **Respaldo permanente** (`.cache_bencina/respaldo.json`, más `respaldo.previo.json` con la copia anterior). Cada descarga válida y más reciente lo reescribe de forma atómica (archivo temporal y cambio de nombre, así nunca queda a medias) y **no caduca**: sobrevive a reinicios de Django y del equipo. Guarda la hora de la descarga, si fue completa o parcial y el estado de cada endpoint, las **respuestas originales** de la API (estaciones y marcas) y los **datos normalizados** que usa la aplicación. Además de 93, 95 y 97, conserva todos los combustibles que entrega la API (DI, KE, GNC, A93, A95, A97, ADI…) con su nombre largo, unidad de cobro, fecha y tipo de atención, y cualquier otro campo que venga en cada estación. El recomendador sigue usando solo 93, 95 y 97.
- **Límite de tamaño.** Cada respuesta de la API se lee por trozos y se cuentan los bytes realmente recibidos (ya descomprimidos); si pasan de `BENCINA_MAX_BYTES` (25 MB por defecto) se rechaza sin interpretarla como JSON y el respaldo no cambia. Se mantienen el tiempo de espera y la verificación TLS.
- **Escrituras: qué se garantiza y qué no.** Cada archivo (`respaldo.json` y `respaldo.previo.json`) se escribe en un temporal de nombre único y se publica entero con `os.replace`, así que **nunca queda a medias**; si algo falla, la copia anterior se conserva. Pero la rotación (copiar la copia vigente a `respaldo.previo.json`) y la publicación del archivo nuevo son **dos pasos**, no una única operación atómica, y no hay bloqueo entre procesos (no se justifica para una descarga por hora). Si dos procesos guardan exactamente a la vez, ambos archivos siguen siendo válidos, pero puede quedar como vigente la descarga que terminó de publicar al final aunque no sea la más reciente, y la copia previa puede ser una de las dos versiones anteriores. Si el proceso se cae entre ambos pasos, `respaldo.previo.json` y `respaldo.json` pueden quedar iguales.
- **Temporales.** Solo se crean (y se limpian) archivos con los nombres `.tmp-resp-*.json` y `.tmp-previo-*.json`. Al guardar se borran los que lleven más de una hora sin modificarse (restos de una caída); los recientes, que podrían ser de una escritura en curso, y cualquier otro archivo de la carpeta no se tocan.
- **Credenciales en registros y mensajes.** Todo texto que muestra o registra el módulo pasa por un saneado que oculta `usuario:clave@` dentro de una URL (también si la clave contiene `@`) y parámetros como `token=`, `key=` o `password=`; se aplica a las URL, a los mensajes de las excepciones y al comienzo de las respuestas que se registran.
- **Tiempo de espera.** `BENCINA_TIMEOUT` (15 s) es el tiempo de espera de `requests` **para conectar y para cada lectura**; no es un límite de duración total de la descarga. Una respuesta que llegue con cuentagotas, sin pasar nunca de 15 s entre trozos y sin superar `BENCINA_MAX_BYTES`, podría tardar mucho más. No se agregó un límite total para no complicar el código; mientras tanto, la descarga ocurre una vez por hora y, tras un fallo, no se reintenta durante 60 s.
- **Protección del respaldo.** Una descarga no reemplaza la copia buena si trae menos de 10 estaciones, si menos de la mitad tiene precios o si pierde más de la mitad de las estaciones (o de las estaciones con precio) de la copia anterior; en ese caso se conserva la copia y el aviso dice por qué. Si falla solo el endpoint de marcas, se reutilizan las de la descarga anterior y la descarga queda marcada como parcial. Un JSON corrupto o ilegible se ignora (se prueba con la copia anterior) sin borrarlo y sin provocar un error 500.
- **Cuando la fuente no responde** se muestran los datos guardados con la etiqueta «Datos guardados, no son precios actuales» y la fecha de su última descarga, por antiguos que sean (no se vuelven a filtrar por antigüedad). Sin ninguna copia, la página lo dice y no inventa datos.
- Se descartan precios fuera de $500–$2.500/L y los marcados `atipico`, pero **no** los antiguos con RX Noticias (la ruta heredada de la API sí descarta los de más de `BENCINA_MAX_DIAS`); las coordenadas con coma decimal se interpretan y las que caen fuera de Chile se ignoran (esa estación queda en la lista, sin marcador). «A93» (asistido) y «93» (autoservicio) se juntan en un solo combustible, con el precio más bajo.
- Con RX Noticias cada estación se identifica por un `id` interno derivado de su clave (ver arriba); con la ruta heredada, por el `id` de la API.

Variables de entorno opcionales:

| Variable | Por defecto | Para qué |
|---|---|---|
| `BENCINA_FUENTE` | `rx` | `rx` (RX Noticias) o `api` (ruta heredada de la API de Bencina en Línea) |
| `BENCINA_FUENTE_URL` | `https://rxnoticias.com/datos/combustible.json` | URL del archivo de RX Noticias |
| `BENCINA_NOMINATIM` | `0` | `1` activa la geocodificación opcional con Nominatim (ver arriba) |
| `BENCINA_NOMINATIM_MAX` / `BENCINA_NOMINATIM_CONTACTO` | `5` / vacío | Consultas por descarga y contacto para el `User-Agent` |
| `BENCINA_API_BASE` | `https://api.bencinaenlinea.cl/api` | URL base de la API (solo con `BENCINA_FUENTE=api`) |
| `BENCINA_TIMEOUT` | `15` | Segundos de espera por consulta |
| `BENCINA_VERIFICAR_TLS` | `1` | La verificación TLS está siempre activa por defecto y nunca se desactiva sola. `0` existe solo como **diagnóstico puntual** (la página avisa que la conexión no está verificada) y **no se permite en producción** |
| `BENCINA_CACHE_SEGUNDOS` | `3600` | Duración de la caché fresca (el respaldo permanente no caduca) |
| `BENCINA_CACHE_DIR` | `.cache_bencina` | Carpeta del respaldo permanente (la caché de Django va en su subcarpeta `cache_django`) |
| `BENCINA_MAX_BYTES` | `26214400` | Tamaño máximo (bytes) de una respuesta de la API |
| `BENCINA_MAX_DIAS` | `30` | Antigüedad máxima de un precio (solo con `BENCINA_FUENTE=api`) |
| `BENCINA_JSON_LOCAL` | vacío | Leer un archivo local (formato de `combustible.json` o de la API) en vez de descargar |

## Uso de IA

El código y la documentación se desarrollaron con ayuda de Claude (Anthropic) y, para reunir datos de rendimiento de autos, con una segunda IA de búsqueda. Lo declaramos aquí para que se pueda revisar con transparencia. Todas las funcionalidades y decisiones de diseño fueron dirigidas por el equipo; la IA las implementó. El equipo revisó y probó cada etapa antes de aceptarla.

**Por qué el historial de GitHub muestra a Claude como autor de casi todos los commits.** El proyecto se trabajó primero como borrador en Visual Studio (app Django, vista inicial y plantilla `inicio.html` con el diseño del equipo) y no estaba publicado en GitHub. Los commits se hicieron al subirlo y mejorarlo por etapas con Claude (precios reales, mapa, filtros, pruebas), y por eso figuran a su nombre. Que aparezcan en pocos commits no refleja el trabajo previo ni las decisiones del equipo.

| Qué hizo la IA | Qué definió, pidió y verificó el equipo |
|---|---|
| Escribió y modificó el código de Django, el normalizador de la API (`viajes/bencina.py`), el mapa (`mapa.js`) y las pruebas automáticas, y revisó el código existente para eliminar contradicciones y errores pequeños antes de publicarlo. | Definió qué debe hacer la aplicación y pidió los cambios: quitar altitud, latitud y tipo de camino porque no cambiaban qué combustible conviene, y justificar según el octanaje que recomienda el fabricante para cuidar el motor. |
| Propuso integrar Bencina en Línea y evaluó otras fuentes (ver «Fuentes evaluadas»). | Pidió usar datos reales en vez de cargarlos a mano, el buscador de vehículos, la calculadora de repostaje, el mapa y los filtros. |
| | Probó la aplicación en su computador y reportó los errores, por ejemplo el bloqueo de los mosaicos del mapa, que se corrigió cambiando de proveedor. |

Cómo se verificó el trabajo de la IA: 67 pruebas automáticas (`python manage.py test`), una prueba en navegador con una muestra real de 1.810 estaciones, la ejecución local con datos en vivo y la revisión de que el costo por km (precio ÷ rendimiento) coincide con el cálculo a mano. Hay límites que seguimos sin resolver y están en «Limitaciones conocidas»: el octanaje del fabricante solo está cargado para un modelo y no todos los rendimientos del catálogo están verificados.

## Pruebas

```powershell
python manage.py test
```

175 pruebas (fuente RX Noticias —lectura, claves, coordenadas, respaldo, vistas y Nominatim—, normalización, selector de marca y modelo, catálogo referencial, caché, errores de red, recomendador, calculadora, filtros y endpoint del mapa). Usan una muestra real guardada en `viajes/tests/fixtures/`, no consultan internet.

## Catálogo ampliado (datos referenciales)

Además de los 33 modelos de `AUTOS` (en `views.py`), la app carga `viajes/data/catalogo_referencial.csv` (~2.000 modelos). Salen de datos públicos de **otros mercados**, no de autos vendidos en Chile:

| Origen | Qué es | Límite |
|---|---|---|
| Canadá (NRCan, 2019–2026) | Consumo combinado (mediana entre versiones); gasolina regular o premium | Ciclo de prueba norteamericano. Mismo nombre no implica mismo motor que en Chile. |
| México (CONUEE, 2023) | Catálogo de Rendimientos, solo algunas marcas | Convertido desde PDF; se dejaron fuera las marcas con nombres de versión confusos. |
| Brasil (Inmetro PBEV, 2024) | Consumo con gasolina | La gasolina brasileña lleva etanol: el rendimiento es menor que en Chile. |

Cómo se usan:
- Los 33 modelos originales tienen prioridad: un modelo referencial nunca los reemplaza.
- En pantalla, un resultado con estos datos lleva la etiqueta **«Dato referencial · origen»** y un aviso en la justificación. Son una estimación del rendimiento.
- **No se conoce su octanaje**: se recomienda el combustible más barato y se avisa que no está verificado. Si la fuente canadiense marca el modelo como premium, la justificación lo advierte («confirma en el manual»), pero esa estimación no se usa para elegir el combustible.
- Quedan fuera diésel, eléctricos y enchufables. Los híbridos no enchufables sí están (cargan solo bencina). Algunos modelos casi iguales pueden aparecer dos veces con distinto nombre de versión.
- Atribución: los datos de Canadá contienen información de Natural Resources Canada publicada bajo la Open Government Licence – Canada; los de México son de la CONUEE y los de Brasil del programa PBE Veicular (Inmetro).

## Fuentes evaluadas

| Fuente | Estado | Motivo |
|---|---|---|
| `combustible.json` de [RX Noticias](https://rxnoticias.com/datos/) | **Integrada** (fuente por defecto) | Precios con fecha propia por estación y combustible, sin token; CC BY 4.0. Sin id ni coordenadas (ver «Fuente de datos»). **La descarga real desde `rxnoticias.com` no se pudo comprobar** desde el entorno de desarrollo (el proxy la bloquea); se probó con el archivo recibido y con respuestas simuladas. |
| API de Bencina en Línea (CNE) | Heredada, no por defecto | Su certificado TLS venció el 31-10-2026 y no se desactivó la verificación. Sigue en el código con `BENCINA_FUENTE=api`. |
| [diland9/precios-combustibles-chile](https://github.com/diland9/precios-combustibles-chile) (MIT) | **Solo coordenadas** | Su snapshot del 30-10-2026 es la tabla inicial de coordenadas. No se usa para precios: se alimenta de la misma API y no tiene datos posteriores al 30-10. No se copió su código. |
| API de la CNE (`api.cne.cl` / `api.tramites.cne.cl`) | No integrada | No se pudo consultar desde el entorno de desarrollo. `api.cne.cl` declara acceso gratuito con registro. Bencina en Línea es la plataforma de la CNE, así que probablemente ofrece los mismos datos, y hoy no falta ninguno que justifique agregarla. Si se integra, la credencial debe ir en una variable de entorno. |
| Nominatim (OpenStreetMap) | Preparada, apagada | Para estaciones nuevas sin coordenadas (`BENCINA_NOMINATIM=1`). No probada contra OpenStreetMap. El mapa base es de Esri (su atribución cita a OpenStreetMap contributors). |
| [0carloscastillo0/stationAPI](https://github.com/0carloscastillo0/stationapi) | Descartada | Es un servidor Flask sobre la misma API de Bencina en Línea; no publica datos propios. |

## Limitaciones conocidas

- **La descarga real desde `rxnoticias.com` no se pudo comprobar** desde el entorno donde se desarrolló (el proxy la bloquea). Se probó con el archivo real recibido el 1-nov-2026 y con respuestas simuladas; la primera ejecución con internet en tu computador o en el servidor es la verificación real. No sabemos cada cuánto actualiza RX Noticias (la página no lo declara) ni de dónde obtiene los datos. Si falla, la página lo indica y usa los últimos datos guardados.
- **Las coordenadas son una tabla fija** (snapshot del 30-10-2026): las estaciones nuevas, o las que cambien de marca o dirección, quedan sin coordenadas (salen en los listados, no en el mapa) hasta que se agreguen a `coordenadas.json` o se active Nominatim, que no se probó contra OpenStreetMap. Nominatim no resuelve bien direcciones de carretera («Km», «Ruta»), que son el 17 % de las direcciones del archivo.
- **El texto bajo el mapa** («Fuente: Bencina en Línea (CNE)…») está escrito en `mapa.js`, que no se modificó en esta etapa; el resto de la página sí cita a RX Noticias.
- RX Noticias no entrega GLP ni GNC: si había datos de esos combustibles en un respaldo anterior, se conservan con su fecha original, pero no se actualizan.
- **Catálogo de autos:** son 33 modelos «de Chile» (rendimiento de Consumo Vehicular, de los fabricantes o de Autocosmos; la fuente se muestra en cada resultado; no todos están verificados) más ~2.000 modelos **referenciales** de otros mercados (ver «Catálogo ampliado»). El **octanaje del fabricante solo está cargado para el Kia Soluto**; para el resto se recomienda el más barato y se avisa.
- El mapa base es de Esri (World Street Map, con atribución, sin API key). Se descartaron `tile.openstreetmap.org` (bloqueó los mosaicos con 403 en pruebas locales) y CARTO (exige API key). Para un sitio con mucho tráfico habría que revisar las condiciones de uso del proveedor.
- La lista junto al mapa muestra solo las 15 estaciones más baratas del filtro actual; el mapa dibuja todas las que tienen coordenadas.
- Las fotos del encabezado se enlazan desde Wikimedia Commons y no se pudieron abrir desde el entorno de desarrollo: el carrusel y sus filtros de calidad se probaron con imágenes de prueba. Hay que revisar en el navegador que carguen y que sean las deseadas (el código descarta las que fallen, sean pequeñas, verticales u oscuras).
- Leaflet se carga desde un CDN: sin internet el mapa no aparece (el recomendador y la calculadora sí funcionan).
- Las animaciones y la apariencia se probaron en Chromium; no se probó en otros navegadores.
- `SECRET_KEY` y `DEBUG` son de desarrollo (`config/settings.py`). Para un despliegue público hay que cambiarlos.

## Publicar el proyecto (producción)

En local no hace falta configurar nada (`DEBUG` activo, clave de desarrollo). Para publicarlo hay que definir `DJANGO_DEBUG=0`; entonces Django **no arranca** si falta algo de lo siguiente:

| Variable | Obligatoria | Uso |
|---|---|---|
| `DJANGO_DEBUG=0` | sí | Activa el modo producción |
| `DJANGO_SECRET_KEY` | sí | Clave propia y secreta, de al menos 50 caracteres (no la de desarrollo) |
| `DJANGO_ALLOWED_HOSTS` | sí | Dominios del sitio separados por comas, sin `*` |
| `DJANGO_SSL_REDIRECT` | no (`1`) | `0` desactiva la redirección a HTTPS |
| `DJANGO_HSTS_SEGUNDOS` | no (`0`) | Súbalo (p. ej. `31536000`) cuando HTTPS ya funcione |
| `DJANGO_PROXY_SSL=1` | solo con proxy | Confía en `X-Forwarded-Proto` de un proxy de confianza |
| `DJANGO_CSRF_ORIGINS` | si hace falta | Orígenes `https://…` de confianza para CSRF |

En producción también se activan las cookies de sesión y CSRF seguras. Antes de publicar, ejecute `DJANGO_DEBUG=0 … python manage.py check --deploy` (con las variables anteriores); solo quedará el aviso de HSTS mientras `DJANGO_HSTS_SEGUNDOS` sea 0. `.cache_bencina/` está en la raíz del proyecto, fuera de las carpetas estáticas, por lo que el servidor no la expone; no la mueva a una carpeta servida. Los archivos estáticos hay que servirlos con el servidor web (el proyecto no define `STATIC_ROOT`).

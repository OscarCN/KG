import re
import os
import ast
import json
import time
import copy
import math
import itertools
import pickle

import numpy as np
import pandas as pn
import openai
from openai import OpenAI
from collections import defaultdict, Counter

from es.es import get_docs


OPENAI_PROJECT_ID = 'proj_hG3dijzH50mvRZDFs6aASmqS'
OPENAI_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_ORGANIZATION = os.getenv('OPENAI_ORGANIZATION', 'org-OSGYrp5SnEAis7CDgoxEmNiu')
OPENAI_MODEL = 'gpt-4o'

openai.api_key = OPENAI_KEY
openai.project = OPENAI_PROJECT_ID
openai.organization = OPENAI_ORGANIZATION

client = OpenAI(
    api_key=OPENAI_KEY,
    organization=OPENAI_ORGANIZATION,
    project=OPENAI_PROJECT_ID
)


default_req = {'doctype': 'news',
 'source': [],
 'intervals': [],
 'keywords': [],
 'phrases': [],
 'geo_filter': False,
 'sort': 'impact',
 'categories': {},
 'topic_id': [],
 'page_number': 0,
 'page_size': 10000,
 'period': 'd',
 'source_tier': [],
 'bounding_box': []}

default_req_op = {'doctype': 'news',
 'source': [],
 'intervals': [],
 'keywords': {"AND": [], "OR": [], "NOT": []},
 'phrases': {"AND": [], "OR": [], "NOT": []},
 'geo_filter': False,
 'sort': 'impact',
 'categories': {},
 'topic_id': [],
 'page_number': 0,
 'page_size': 10000,
 'period': 'd',
 'source_tier': [],
 'bounding_box': []}


def wire_req(kw=None, _not=None, kw_bound=None, categories=None, doctype='topics', period='d', bbox=None):
    if _not is not None:
        req = copy.deepcopy(default_req_op)

        if kw is not None:
            if isinstance(kw, list):
                req['phrases']['AND'] += kw
            else:
                req['phrases']['AND'].append(kw)

        if _not is not None:
            if isinstance(_not, list):
                req['phrases']['NOT'] += _not
            else:
                req['phrases']['NOT'].append(_not)

    else:
        req = copy.deepcopy(default_req)

        if kw is not None:
            if isinstance(kw, list):
                req['phrases'] += kw
            else:
                req['phrases'].append(kw)

    if bbox is not None:
        req['bounding_box'] = bbox
        req['geo_filter'] = True
        req['location_type'] = 'mentioned'

    req['period'] = period
    req['doctype'] = doctype

    assert categories is None or isinstance(categories, dict)
    if categories is not None:
        req['categories'] = categories

    return req


def cleanstr(st):
    st = st.lower()
    st = st.replace('á', 'a')
    st = st.replace('é', 'e')
    st = st.replace('í', 'i')
    st = st.replace('ó', 'o')
    st = st.replace('ú', 'u')
    st = st.replace('ñ', 'n')

    return st



#searches = pn.read_excel('../scripts/resources/kg/events.xlsx')
searches = pn.read_excel('../resources/kg/events_leon.xlsx')  # TODO: REMOVE BBOX

searches = searches.assign(subsection=searches['subsection'].map(lambda t: '' if pn.isnull(t) else t))

cache_docs = dict()

for i_row, r in searches.iterrows():

    if not pn.isnull(r['period']):
        period = r['period']

    if not pn.isnull(r['kw']):
        kws = [t.replace('"', '').strip() for t in r['kw'].split(',')]

        location = [t.replace('"', '').strip() for t in r['location'].split(',') if t != ''] if not pn.isnull(r['location']) else []

        add_prod = (kws, )
        if len(location):
            add_prod += (location, )

        kws = list(map(list, itertools.product(*add_prod)))

        dc = []
        if not pn.isnull(r['dismiss_categories']):
            dc = r['dismiss_categories'].split('|')
    else:
        dc = []
        kws = [None]

    if not pn.isnull(r['not']):
        _not = [t.replace('"', '').strip() for t in r['not'].split(',')]
    else:
        _not = None

    bbox = None
    if 'bbox' in r and not pn.isnull(r['bbox']):
        bbox = ast.literal_eval(r['bbox'])

    categories = {'level_1': [], 'level_2': []}
    if not pn.isnull(r['categories']):
        categories = {
            'level_1': [cat.strip() for cat in r['categories'].split('|') if '>' not in cat],
            'level_2': [cat.strip() for cat in r['categories'].split('|') if '>' in cat]}

    for kw in kws:
        print('processing', kw, categories)

        cache_key = ((i_row,) + tuple(kw)) if isinstance(kw, list) else (i_row, kw)
        key_cats = tuple(categories.get('level_1', []) + categories.get('level_2', []))
        if len(key_cats) > 0:
            cache_key += key_cats

        if cache_key not in cache_docs:
            req = wire_req(kw, _not=_not, categories=categories, doctype='news', period=period, bbox=bbox)
            _docs = get_docs(req, fields=['_id', 'date_created', 'author_name', 'source.name', 'title', 'url', 'text', 'summary', 'fb_likes', 'event_ids', 'custom_categories', 'locations_mentioned'])

            req = wire_req(kw, _not=_not, categories=categories, doctype='topics', period=period, bbox=bbox)
            topics = get_docs(req)

            intopics = {t['topic_id'] for t in topics}
            _docs = [t for t in _docs if str(t.get('event_ids', '')) not in intopics]
            _docs += topics

            _docs = [t for t in _docs if not any(any(cat in act for act in t.get('custom_categories', dict()).get('level_2', [])) for cat in dc)]

            cache_docs[cache_key] = _docs
            print(len(_docs), cache_key)


all_docs = list()
seen = set()
for k in cache_docs.values():
    for doc in k:
        if doc.get('url', doc['id']) not in seen:

            seen.add(doc.get('url', doc['id']))

            doc['id'] = doc.get('url', doc['id'])
            doc['text'] = doc.get('summary', doc.get('text', ''))
            doc['title'] = doc.get('topic_name', doc.get('title', ''))

            all_docs.append(doc)

url_doc = dict(zip([t['id'] for t in all_docs], all_docs))

from datetime import datetime
from dateutil.parser import parse

date_now = datetime.now().strftime('%d/%m/%Y')
DAYS_ES = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo']
MONTHS_ES = [
    'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
    'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre'
]

def format_human_date(dt):
    dt_local = dt.astimezone()
    dt_naive = dt_local.replace(tzinfo=None)

    weekday = DAYS_ES[dt_local.weekday()]  # weekday() → 0 for Monday
    day = dt_local.day
    month = MONTHS_ES[dt_local.month - 1]
    year = dt_local.year

    formatted_date = f"{weekday} {day} de {month} del {year}"

    isoformat = dt_naive.strftime('%d/%m/%Y')

    return isoformat + ' - o bien, ' + formatted_date

sys_1_str = 'Eres un modelo para extraer información estructurada de eventos en artículos de noticias, se te van a presentar instrucciones para resolver la tarea, algunos ejemplos, y posteriormente una noticia para que extraigas la información'


event_task = '''
Buscamos eventos descritos en un artículo de noticias. Buscamos eventos de cualquier tipo, que tengan una fecha específica o un rango de fechas, y que esta se mencione en la noticia. Ejemplos de eventos son conciertos, festivales, eventos deportivos, anuncios planeados, cierres de carreteras, manifestaciones, congresos, fiestas, etc.

Para cada evento mencionado en la nota que tenga una fecha (exacta o estimada), extrae los siguientes campos. Si no se menciona un campo, deja su valor null. No inventes eventos, solo extrae lo que está relacionado explícitamente.

Para cada evento que encuentres en la noticia, si se menciona la fecha (real o estimada, o un periodo), extrae la siguiente información:



1. Tipo de evento (tipo_evento): obtén el tipo de evento del siguiente catálogo, usando la categoría que sea más específica al evento (e.g. Concierto es más específico que Evento cultural):

"Concierto"
"Festival"
"Fiesta"
"Feria"
"Evento cultural"
"Evento religioso"
"Evento deportivo"
"Cierre de calle o carretera"
"Suspensión de operaciones"
"Manifestación"
"Congreso"
"Exposición"
"Convención"
"Conferencia"
"Incendio"
"Explosion"
"Inundación"
"Accidente"
"Emergencia"
"Inauguración"
"Robo"
"Asalto"
"Balacera"
"Homicidio"
"Enfrentamiento"
"Ataque"
"Detención"
"Evento político"
"Evento económico"
"Evento climático"
"Evento de seguridad"  (se refiere a eventos de seguridad pública, policía, etc. tales como asalto, ataques, balaceras, detenciones, etc.)
"Entrada en vigor de una ley o regulación"
"Otro"



2. Subtipo de evento (subtipo_evento): propón un subtipo de evento dado el contexto. Por ejemplo, si el tipo es "Evento deportivo", el subtipo podría ser "Partido de fútbol", "Partido de beisbol", etc.



3. Estatus del evento (estatus): el estatus actual que se anuncia en la noticia, del siguiente catálogo:

"Planeado"
"Pasado"
"Suspendido"
"En transcurso"
"Cancelado"
"Pospuesto"



4. Afluencia estimada (afluencia): Si es un evento en el que participan personas, y la noticia menciona un estimado de personas. Por ejemplo, para un congreso o concierto se puede mencionar la afluencia estimada. Responde con el texto tal cual se menciona en la nota (mencion), y un estimado razonable que tú hagas en formato de número (afluencia)

Por ejemplo:

{"afluencia": { "mencion": "más de 5 mil personas", "afluencia": 5000 }}

Si no se menciona, deja el valor vacío.



5. Capacidad del lugar (capacidad): Si el evento es en un lugar físico, como un estadio, teatro, sala de convenciones, etc. y la noticia menciona la capacidad del lugar. Responde con el texto tal cual se menciona en la nota (mencion), y un estimado razonable que tú hagas en formato de número (capacidad)

Por ejemplo:

{"capacidad": { "mencion": "15 mil personas", "capacidad": 15000 }}



6. Fecha del evento (fecha_evento): Es la fecha del evento, responde con el texto tal cual se menciona en la nota (mencion), un periodo de fechas, que sean el inicio y fin del evento (fecha), si no se menciona un periodo, pon solo la fecha de inicio, y la zona horaria, si esta se menciona (zona_horaria). 

Si solo se menciona un periodo aproximado (por ejemplo, "finales del año", o "el proximo año", etc.), interpreta un periodo estimado razonable, y escribe la fecha inicial del periodo junto con un rango de precisión en días, en el que muy probablemente se encuentre la fecha real. Por ejemplo: {"mencion": "inicio de 2022", "fecha": {"inicio": "01/01/2022T00:00:00", "fin": null}, "precision": 90 }, aquí son 90 días o 3 meses de precisión, un trimestre. Otro ejemplo: {"mencion": "el proximo año", "fecha": {"inicio": "01/01/2026T00:00:00", "fin": null}, "precision": 365 }

cuatro ejemplos:

"fecha_evento": { "mencion": "durante el mes de agosto de este año", "fecha": {"inicio": "2025-08-01T00:00:00", "fin": "2025-08-30T00:00:00"}, "precision": 30 }

"fecha_evento": { "mencion": "a las 4 de la tarde del próximo martes 4 de julio", "fecha": { "inicio": "2025-07-04T16:00:00", "fin": null } }

"fecha_evento": { "mencion": "mañana 4 de julio a las 4 de la tarde, hora del este ", "fecha": { "inicio": "2025-07-04T16:00:00", "fin": null }, "zona_horaria": "hora del este" }

"fecha_evento": { "mencion": "julio del 2026", "fecha": { "inicio": "2025-07-01T00:00:00", "fin": null }, "precision": 30 }

Usa ese formato estandarizado de fechas "YYYY-mm-ddTHH:MM:SS"
Como contexto, la fecha de hoy es {date_now}



7. Nombre del evento (nombre): Si el evento tiene un nombre, o se puede llamar de alguna forma, escribelo, de otra manera deja el valor en null. Por ejemplo: "Partido América-Toluca", "Cuarta Feria de la Fresa", "Entrada en vigor de la ley 457C"



8. Descripción del evento (descripcion): Redacta un texto libre con una descripción breve del evento.



9. Etiquetas (etiquetas): palabras clave que encuentres o intuyas que pueden describir el evento

Por ejemplo:

"etiquetas": ["fútbol", "Clásico", "América", "Chivas", "Estadio Azteca"]



10. Contexto (contexto): Si la información existe, escribe un texto libre resumiendo el contexto general del evento.



11. Precio (precio): Si el evento tiene costo para los asistentes, y este se menciona, o si se menciona un rango de precios. Responde con el texto tal cual se menciona en la nota (mencion), un estimado razonable que tú hagas en formato de número (rango_precio), con el rango inferior y superior, y si se menciona la moneda (moneda)

Tres ejemplos:

{"precio": { "mencion": "precios que van desde 500 hasta 2500 pesos", "rango_precio": {"inferior": 500, "superior": 2500}, "moneda": "pesos" }}

{"precio": { "mencion": "el evento no tiene costo", "rango_precio": {"inferior": 0, "superior": 0}, "moneda": null }}

{"precio": { "mencion": "los boletos cuestan 450 dolares", "rango_precio": {"inferior": 450, "superior": 450}, "moneda": "dolares" }}



12. Ubicación (ubicacion): Si el evento sucede en alguna ubicación, con la información disponible en la nota, extrae los datos de ubicación de manera estructurada, en el siguiente formato ejemplo:

"ubicacion": {
    "pais": "México",
    "estado": "Michoacán",
    "ciudad_municipio": "Morelia",
    "colonia": null,
    "zona": "zona centro",
    "calle": "Mariano Escobedo",
    "numero": null,
    "lugar": "Teatro de la Ciudad"
}
En el campo de ciudad_municipio escribe la ciudad o municipio, si se menciona. En el campo de estado escribe el estado o provincia, si se menciona.

En el campo de "lugar" escribe el nombre propio del lugar, sólo lugares geográficos, o que puedan ser ubicados en un mapa, si el lugar tiene nombre y este se menciona. El lugar se refiere a una ubicación con una dirección específica en una calle, avenida, carretera, etc. Como puede ser un teatro, universidad, plaza, parque, centro de convenciones, edificio con un nombre, etc.
Sólo escribe nombres propios de lugares, no agregues referencias espaciales ni descripciones. 
Ejemplos de valores INCORRECTOS son: "distintos recintos de Guanajuato y municipios cercanos", "casa de Ozzy Osbourne", "canal oficial de YouTube de Super Junior", "bibliotecas públicas". Si no se menciona el nombre propio del lugar o lugares, que lo identifique únicamente, deja el valor null.

Si el evento sucede en varias ubicaciones, escribe una lista con los datos de cada ubicación:   "ubicacion": [ { "pais": "México", "estado": null... }, { "pais": "México", "estado": null... }, ... }


13. Relevancia en la noticia (relevancia): La relevancia que tiene el evento en la noticia, si se menciona secundariamente o tiene un papel importante en la nota { "relevancia": 1 }, escoge entre:

1 Si el evento es relevante en la nota
2 Si el evento se menciona secundariamente
3 Si el evento sólo se menciona implícitamente



Formato de respuesta:

Responde con una lista en formato JSON, donde cada elemento representa un evento detectado en la nota. No añadas texto adicional fuera del JSON.

Ejemplo de formato esperado:


[
  {
    "tipo_evento": "Evento deportivo",
    "subtipo_evento": "Partido de fútbol",
    "estatus": "Planeado",
    "afluencia": {
      "mencion": "se espera la asistencia de más de 60 mil aficionados",
      "afluencia": 60000
    },
    "capacidad": {
      "mencion": "el Estadio Azteca tiene capacidad para 87 mil personas",
      "capacidad": 87000
    },
    "fecha_evento": {
      "mencion": "el próximo domingo 10 de agosto a las 6 de la tarde",
      "fecha": {
        "inicio": "2025-08-10T18:00:00",
        "fin": null
      }
    },
    "nombre": "Clásico América vs Chivas",
    "descripcion": "Partido de alto perfil entre los equipos América y Chivas en el Estadio Azteca, correspondiente a la jornada 5 del torneo Apertura 2025.",
    "contexto": "El evento ha generado gran expectación y se espera que haya un operativo de seguridad especial coordinado entre la Secretaría de Seguridad Ciudadana y la Liga MX. Se recomienda llegar con anticipación debido a cierres parciales en las avenidas cercanas.",
    "precio": {
      "mencion": "los boletos cuestan entre 300 y 1500 pesos",
      "precio": {
        "inferior": 300,
        "superior": 1500
      },
      "moneda": "pesos"
    },
    "ubicacion": {
      "pais": "México",
      "estado": "Ciudad de México"
      "ciudad_municipio": "Ciudad de México",
      "colonia": "Santa Úrsula Coapa",
      "zona": "zona sur",
      "calle": "Calzada de Tlalpan",
      "numero": null,
      "lugar": "Estadio Azteca"
    },
    "relevancia": 1
  }
]


'''


body_p = '''

La noticia es la siguiente:

{body}
'''

f = open('/Users/oscarcuellar/ocn/media/kg/spanish_EL/scripts/data/etc/events_tagged.pkl', 'rb')
tagged = pickle.load(f)
f.close()

sample_req = {
        "custom_id": None,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": OPENAI_MODEL,
            "messages": [],
            "temperature": 0.3
        }
    }

tag_batch = dict()
def tag_events_batched(_id, txt, article_date):
    if _id in tagged:
        return tagged[_id]

    if _id not in tag_batch:
        _usr_1 = event_task.replace('{date_now}', format_human_date(article_date))
        _usr_2 = body_p.format(body=txt)

        messages = [
         {"role": "system", "content": sys_1_str},
         {"role": "user", "content": _usr_1},
         {"role": "user", "content": _usr_2},
        ]

        openai_req = copy.deepcopy(sample_req)
        openai_req['custom_id'] = str(_id)
        openai_req['body']['messages'] = messages

        tag_batch[_id] = openai_req

    return tag_batch[_id]


#for task_idx, task_r in searches.iterrows():
#    # TODO: SUPPORT MORE CONCEPTS
#    concept = task_r['subsection']

concept = 'eventos'
for doc_idx, doc in enumerate(all_docs):
    if doc_idx % 10000 == 0:
        print(concept, doc_idx)
    key = (concept, doc['id'])

    art_date = parse(doc.get('date_created', doc.get('topic_date')))
    r = tag_events_batched(key, doc['text'][:3500], article_date=art_date)


batch_size = 25000

keys = list(tag_batch.keys())

requests = [tag_batch[k] for k in keys]

rdir = '/Users/oscarcuellar/ocn/media/kg/spanish_EL/scripts/data/etc/'

date_suff = datetime.now().strftime('%Y%m%d')
fname = rdir + f"requests{date_suff}.jsonl"

with open(fname, "w") as f:
    f.write("\n".join(json.dumps(task) for task in requests))


upload = openai.files.create(file=open(fname, "rb"), purpose="batch")

# Submit the batch job
batch = openai.batches.create(
    input_file_id=upload.id,
    endpoint="/v1/chat/completions",
    completion_window="24h"  # "24h" or "48h" currently supported
)

print(f"Batch ID: {batch.id}")

"batch_68991c4298548190b8671f620018cd63"


'/Users/oscarcuellar/ocn/media/kg/spanish_EL/scripts/data/etc/requests20251011.jsonl'
"batch_68eb2097c974819087058dca0126d482"


status = openai.batches.retrieve(batch.id)
print(f"Batch status: {status.status}")

if status.status == "completed":
    print(f"Download result from: {status.output_file_id}")
    result_file = openai.files.retrieve_content(status.output_file_id)

    with open(rdir + f"results{date_suff}.jsonl", "w") as f:
        f.write(result_file)




def parse_response(r):
    jr = json.loads(r)
    cid = ast.literal_eval(jr['custom_id'])
    try:
        return cid, json.loads(jr['response']['body']['choices'][0]['message']['content'].replace('`', '').replace('json', ''))
    except:
        return cid, dict()

url_res = dict([parse_response(t) for t in result_file.split('\n') if t != ''])

data = list()
for doc_idx, doc in enumerate(all_docs):
    if doc_idx % 5000 == 0:
        print(concept, doc_idx)
    key = (concept, doc['id'])

    r = url_res[key]
    tagged[key] = json.dumps(r)
    for d in r:
        d['url'] = doc['id']
        d['article_date'] = doc['date_created']
        data.append(d)

f = open('/Users/oscarcuellar/ocn/media/kg/spanish_EL/scripts/data/etc/events_tagged.pkl', 'wb')
pickle.dump(tagged, f)
f.close()

event_data = list()
newdata = copy.deepcopy(data)

for i, d in enumerate(newdata):
    if 'ubicacion' in d and isinstance(d['ubicacion'], list):
        for u in d['ubicacion']:
            ta = copy.deepcopy(d)
            ta['ubicacion'] = u
            event_data.append(ta)
        continue
    event_data.append(d)

for i, d in enumerate(event_data):
    if 'ubicacion' in d and not pn.isnull(d['ubicacion']):
        for field in ['pais', 'estado', 'ciudad_municipio', 'colonia', 'zona', 'calle', 'numero', 'lugar']:
            d[field] = d['ubicacion'].get(field)

        for field in ['pais', 'estado', 'ciudad_municipio', 'colonia', 'zona', 'calle', 'numero', 'lugar']:
            if isinstance(d[field], list):
                d[field] = [t for t in d[field] if t]
                d[field] = '|'.join(d[field])
                print(field, d[field])

    if 'ubicacion' in d: del d['ubicacion']

    if not pn.isnull(d['fecha_evento']) and d['fecha_evento'] != '':
        d['fecha_evento_uns'] = d['fecha_evento'].get('mencion')
        if not pn.isnull(d['fecha_evento'].get('fecha')):
            d['fecha_inicio'] = d['fecha_evento'].get('fecha', dict()).get('inicio')
            d['fecha_fin'] = d['fecha_evento'].get('fecha', dict()).get('fin')
        d['fecha_precision'] = d['fecha_evento'].get('precision')

    del d['fecha_evento']

    if 'precio' in d and not pn.isnull(d['precio']) and d['precio'] != '':
        d['precio_uns'] = d['precio'].get('mencion')
        if not pn.isnull(d['precio'].get('rango_precio')):
            d['precio_inf'] = d['precio'].get('rango_precio', dict()).get('inferior')
            d['precio_sup'] = d['precio'].get('rango_precio', dict()).get('superior')
        d['moneda'] = d['precio'].get('moneda')
    if 'precio' in d: del d['precio']

    if 'afluencia' in d and not pn.isnull(d['afluencia']) and d['afluencia'] != '':
        d['afluencia_uns'] = d['afluencia'].get('mencion')
        d['afluencia'] = d['afluencia'].get('afluencia')

    if 'capacidad' in d and not pn.isnull(d['capacidad']) and d['capacidad'] != '':
        d['capacidad_uns'] = d['capacidad'].get('mencion')
        d['capacidad'] = d['capacidad'].get('capacidad')


pn.DataFrame(newdata).to_excel(f'~/Downloads/eventos{date_suff}.xlsx', index=False)


########################################################################################################################
# SYNCHRONOUS VERSION ##################################################################################################

org_length = len(tagged)
def tag_events(_id, txt):
    if _id not in tagged:
        _usr_1 = event_task.replace('{date_now}', datetime.now().isoformat())
        _usr_2 = body_p.format(body=txt)

        messages = [
         {"role": "system", "content": sys_1_str},
         {"role": "user", "content": _usr_1},
         {"role": "user", "content": _usr_2},
        ]

        retries = 0
        retry = True
        while retry:
            try:
                response = client.chat.completions.create(
                 model=OPENAI_MODEL,
                 messages=messages,
                 temperature=0.3,
                )
                retry = False
            except Exception as ex:
                print('sleeping', ex)
                time.sleep(15)
                retries += 1
                if retries > 2:
                    retry = False

        try:
            tagged[_id] = response.choices[0].message.content.replace('`', '').replace('json', '')
        except:
            print('exception parsing', _id)
            tagged[_id] = response

    return tagged[_id]


data = list()
for task_idx, task_r in searches.iterrows():
    # TODO: SUPPORT MORE CONCEPTS
    concept = task_r['subsection']

    for doc_idx, doc in enumerate(all_docs):
        if doc_idx % 50 == 0:
            print(concept, doc_idx)
        key = (concept, doc['id'])

        r = tag_events(key, doc['text'][:3500])
        r = json.loads(r)
        for d in r:
            d['url'] = doc['id']
            d['article_date'] = doc['date_created']
            data.append(d)

_tagged = {('eventos', k[1]): v for k, v in tagged.items()}

assert len(tagged) > org_length
f = open('/Users/oscarcuellar/ocn/media/kg/spanish_EL/scripts/data/etc/events_tagged.pkl', 'wb')
pickle.dump(_tagged, f)
f.close()

for d in data:
    if not pn.isnull(d['ubicacion']):
        for field in ['pais', 'estado', 'ciudad', 'colonia', 'zona', 'calle', 'numero', 'lugar']:
            d[field] = d['ubicacion'].get(field)

    del d['ubicacion']

    if not pn.isnull(d['fecha_evento']) and d['fecha_evento'] != '':
        d['fecha_evento_uns'] = d['fecha_evento'].get('mencion')
        if not pn.isnull(d['fecha_evento'].get('fecha')):
            d['fecha_inicio'] = d['fecha_evento'].get('fecha', dict()).get('inicio')
            d['fecha_fin'] = d['fecha_evento'].get('fecha', dict()).get('fin')
    del d['fecha_evento']

    if not pn.isnull(d['precio']) and d['precio'] != '':
        d['precio_uns'] = d['precio'].get('mencion')
        if not pn.isnull(d['precio'].get('rango_precio')):
            d['precio_inf'] = d['precio'].get('rango_precio', dict()).get('inferior')
            d['precio_sup'] = d['precio'].get('rango_precio', dict()).get('superior')
        d['moneda'] = d['precio'].get('moneda')
    del d['precio']

    if not pn.isnull(d['afluencia']) and d['afluencia'] != '':
        d['afluencia_uns'] = d['afluencia'].get('mencion')
        d['afluencia'] = d['afluencia'].get('afluencia')

    if not pn.isnull(d['capacidad']) and d['capacidad'] != '':
        d['capacidad_uns'] = d['capacidad'].get('mencion')
        d['capacidad'] = d['capacidad'].get('capacidad')



pn.DataFrame(data).to_excel('~/Downloads/eventos_qro.xlsx', index=False)


'''

list of locations, except streets

Santo Oficio y Santa Inquisición
Costa Rica, cerca del bulevar Mariano Escobedo
cruce de los bulevares Timoteo Lozano y Delta
Torre Moroleón y Torre Guanajuato
entre los bulevares Juan Alonso de Torres y Paseo del Moral
Salomón esquina con Vesta
cruce de las calles Anda y Pípila
intersección de las calles Tabasco y República de Costa Rica
'''





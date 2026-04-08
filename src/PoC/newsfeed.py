import re
import os
import json
import time
import copy
import math
import itertools

import numpy as np
import pandas as pn
from openai import OpenAI
from collections import defaultdict, Counter
from utils.connections import es_query, es_topics_query
from utils.es import get_docs


OPENAI_PROJECT_ID = 'proj_hG3dijzH50mvRZDFs6aASmqS'
OPENAI_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_ORGANIZATION = os.getenv('OPENAI_ORGANIZATION', 'org-OSGYrp5SnEAis7CDgoxEmNiu')
OPENAI_MODEL = 'gpt-4o'

client = OpenAI(
    api_key=OPENAI_KEY,
    organization=OPENAI_ORGANIZATION,
    project=OPENAI_PROJECT_ID
)


sys_1 = 'Eres un modelo para clasificar la relevancia de artículos de noticias hacia una empresa, se te van a presentar instrucciones para resolver la tarea, algunos ejemplos, y posteriormente artículos para clasificar'


f = open('/Users/oscarcuellar/ocn/media/kg/spanish_EL/scripts/resources/prompt_contexts/abilia', 'r')
usr_1 = f.read()
f.close()

old_usr_1 = '''

Contexto de la empresa:

Bocar es una empresa de manufactura de componentes para automóviles basada en México, sus productos incluyen componentes estructurales para vehículos, 
componentes para motores, piezas de plástico para interiores, y otros componentes para vehículos.

Bocar se basa en el desarrollo y producción de conjuntos complejos fabricados a partir de fundición de aluminio a alta presión y fundición en molde semipermanente, 
así como componentes de plástico moldeados por inyección.

Sus principales insumos son aluminio y plásticos

Tiene plantas de producción en Lerma, Estado de México, en San Luis Potosí, México, y en Chihuahua

Sus principales clientes son compañias del sector automotriz y de la industria de autopartes, entre ellas están: 
Audi, BMW, Ford, General Motors, Honda, Mazda, Mercedes Benz, Nissan, Toyota, Volkswagen, Bosch, Hitachi, Benteler, BorgWarner, Bühler, Rotax, Draxlmaier, Faurecia, Antolin, Hella, entre otros.


las noticias que se te van a presentar son relativas a {section}, {subsection}

el objetivo de monitorear esto es: 
{objective}


algunos de los factores que pueden afectar a bocar son los siguientes:

Anuncios y novedades sobre inversiones de sus clientes y competidores y la industria en general, en México y el mundo
Salarios en el sector y en el país, 
Acuerdos entre compañías del sector, asociaciones y cámaras de la industria, 
Lanzamientos y anuncios de nuevos productos por parte de clientes y competidores,
Regulaciones locales, nacionales e internacionales que afecten a la industria o al comercio

en particular, de {section} buscamos 
{what}

Tu tarea es seleccionar las notas que refieren eventos que pueden afectar a Bocar. Deberás catalogar cada nota de acuerdo al impacto que tienen:

Nivel 1 (Crítico): Eventos que hablan directamente de la empresa, sus productos, sus directivos, etc
Nivel 2 (Muy importante): Noticias que afectan a la industria y que pueden afectar directamente a Bocar, como regulaciones, sindicatos, huelgas, suministro de energía que pueda afectar al sector, o en donde Bocar tiene ubicadas sus plantas, etc.
Nivel 3 (Importante): Eventos que afectan a sus clientes, competencia, proveedores, insumos, llegada de nuevos potenciales clientes, etc 
Nivel 4 (Relevante): Noticias que afectan a toda la industria, como aranceles, regulaciones, sindicatos, suministro etc.
Nivel 5 (No relevante): Noticias que no tienen impacto para la empresa.

Se te va a presentar una lista de noticias con un identificador, su título y un extracto, para que selecciones las notas relevantes

responde en formato json como el siguiente ejemplo:

'''

usr_2 = '''
las noticias que se te van a presentar son relativas a {section}, {subsection}

el objetivo de monitorear esto es: 
{objective}


en particular, de {section} buscamos 
{what}


Tu tarea es seleccionar las notas que refieren eventos que pueden afectar a Abilia. Deberás catalogar cada nota de acuerdo al impacto que tienen:
{relevance}

Tu tarea es analizar cada noticia y determinar si es relevante para el cliente, asignándole un score de 1 a 3


Se te va a presentar una lista de noticias con un identificador, su título y un extracto, para que selecciones las notas relevantes


'''

'''
Tu tarea es seleccionar las notas que refieren eventos que pueden afectar o interesar a Abilia. Deberás catalogar cada nota de acuerdo al impacto que tienen:
{relevance}

Tu tarea es analizar cada noticia y determinar si es relevante para el cliente, asignándole un score de 1 a 5, y clasificándola conforme a los siguientes criterios:

Se te va a presentar una lista de noticias con un identificador, su título y un extracto, para que selecciones las notas relevantes
'''

example = '''
[
{"id": 0, "nivel": 3},
{"id": 1, "nivel": 3},
{"id": 2, "nivel": 2},
{"id": 3, "nivel": 1},
{"id": 4, "nivel": 1}
]


sin añadir ningún texto adicional, sólo el json

'''

usr_3 = '''
las notas son las siguientes:

{extracts}

'''

tagged = dict()


def tag(_id, txt, fields):
    if _id not in tagged:
        _usr_1 = usr_1
        _usr_2 = usr_2.format(**fields) + example
        _usr_3 = usr_3.format(extracts=txt)

        messages = [
         {"role": "system", "content": sys_1},
         {"role": "user", "content": _usr_1},
         {"role": "user", "content": _usr_2},
         {"role": "user", "content": _usr_3}
        ]

        retries = 0
        retry = True
        while retry:
            try:
                response = client.chat.completions.create(
                 model=OPENAI_MODEL,
                 messages=messages,
                 temperature=0.7,
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
 'page_size': 200,
 'period': 'd',
 'source_tier': [],
 'bounding_box': []}

def wire_req(kw, kw_bound=None, categories=None, doctype='topics', period='d', intervals=None):
    req = copy.deepcopy(default_req)
    if intervals is not None:
        req['intervals'] = intervals
    req['period'] = period
    req['doctype'] = doctype
    if isinstance(kw, list):
        req['phrases'] += kw
    else:
        req['phrases'].append(kw)

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


def get_highlights(doc, kw, hl_length=50):
    if isinstance(kw, list):
        kw = kw[0]

    txt = doc['text']
    title = doc.get('title', doc.get('topic_name', ''))

    txt = title + '.- ' + txt

    kw = cleanstr(kw)
    hl = ''
    offset = 700
    k = 0
    while k < 4 and kw in txt[offset:]:
        #assert k != 3
        c_idx = cleanstr(txt[offset:]).index(kw)

        hl += '\n...' + txt[offset:][max(0, c_idx-hl_length):c_idx + len(kw)+hl_length] + '...'

        offset = offset + c_idx + len(kw)

        k += 1

    #return (txt[:500] + hl).replace('\n', ' ')
    return txt[:700]


searches = pn.read_excel('../scripts/resources/onboarding/abilia_kw.xlsx')

searches = searches[~pn.isnull(searches.relevance)]  # TODO: OJO

searches = searches.assign(subsection=searches['subsection'].map(lambda t: '' if pn.isnull(t) else t),
                           kw_bound=searches['kw_bound'].map(lambda t: '' if pn.isnull(t) else t),
                           location=searches['location'].map(lambda t: '' if pn.isnull(t) else t))

searches = searches[~pn.isnull(searches.detail)]

period = 'y'
intervals = [["2025-08-13T00:00", "2025-11-12T00:00"]]


cache_docs = dict()
for i_row, r in searches.iterrows():
    fields = r[['section', 'subsection', 'objective', 'what', 'relevance', 'location']].to_dict()

    kws = [t.replace('"', '').strip() for t in r['kw'].split(',')]

    # kws = re.findall(r'\"(.*?)\"', r['kw'])

    kws_bound = [t.replace('"', '').strip() for t in r['kw_bound'].split(',') if t != '']
    location = [t.replace('"', '').strip() for t in r['location'].split(',') if t != '']

    add_prod = (kws, )
    if len(kws_bound):
        add_prod += (kws_bound, )
    if len(location):
        add_prod += (location, )

    kws = list(map(list, itertools.product(*add_prod)))

    dc = []
    if not pn.isnull(r['dismiss_categories']):
        dc = r['dismiss_categories'].split('|')

    for kw in kws:
        req = wire_req(kw, doctype='news', period=period, intervals=intervals)
        _docs = get_docs(req)

        req = wire_req(kw, doctype='topics', period=period, intervals=intervals)
        topics = get_docs(req)

        intopics = {t['topic_id'] for t in topics}
        _docs = [t for t in _docs if str(t.get('event_ids', '')) not in intopics]
        _docs += topics

        _docs = [t for t in _docs if not any(any(cat in act for act in t.get('custom_categories', dict()).get('level_2', [])) for cat in dc)]

        cache_key = (i_row,) + tuple(kw) if isinstance(kw, list) else (i_row, kw)
        print(len(_docs), cache_key)
        cache_docs[cache_key] = _docs


seen = set()

counts = dict()

all_docs = dict()
docs_kws = dict()
doc_tagkey = defaultdict(list)
docs_section = dict()
docs_subsection = dict()
for i_row, r in searches.iterrows():
    fields = r[['section', 'subsection', 'objective', 'what', 'relevance', 'location']].to_dict()

    kws = [t.replace('"', '').strip() for t in r['kw'].split(',')]

    # kws = re.findall(r'\"(.*?)\"', r['kw'])

    kws_bound = [t.replace('"', '').strip() for t in r['kw_bound'].split(',') if t != '']
    location = [t.replace('"', '').strip() for t in r['location'].split(',') if t != '']

    add_prod = (kws, )
    if len(kws_bound):
        add_prod += (kws_bound, )
    if len(location):
        add_prod += (location, )

    kws = list(map(list, itertools.product(*add_prod)))

    for kw in kws:
        #print(kw)
        cache_key = (i_row,) + tuple(kw) if isinstance(kw, list) else (i_row, kw)
        _docs = cache_docs[cache_key]

        counts[tuple(kw) if isinstance(kw, list) else (kw,)] = len(_docs)

        #_docs = [t for t in _docs if (i_row, t['id']) not in seen]

        if len(_docs):
            n_before_seen = len(_docs)
            _docs = [t for t in _docs if (i_row, t['id']) not in seen]
            seen.update(set([(i_row, t['id']) for t in _docs]))
            n_after_seen = len(_docs)

            for d in _docs:
                d['text'] = d.get('summary', d.get('text', ''))
                d['title'] = d.get('topic_name', d.get('title', ''))
                d['relevance'] = 0

            for i, d in enumerate(_docs):
                d['_idx'] = i

            batch_size = 10
            n_batches = math.ceil(len(_docs) / batch_size)

            relevances = list()

            for idx_batch in range(n_batches):


                extr = ['id: ' + str(t['_idx']) + ', extracto: ' + get_highlights(t, kw) for t in _docs[idx_batch*batch_size:(idx_batch+1)*batch_size]]
                extr = '\n\n'.join(extr)

                _id = (tuple(kw) + (str(idx_batch),)) if isinstance(kw, list) else (kw, str(idx_batch))
                print(idx_batch / n_batches, 'n_before_seen', n_before_seen, 'n_after_seen', n_after_seen, _id)

                for d in _docs[idx_batch*batch_size:(idx_batch+1)*batch_size]:
                    doc_tagkey[d['id']].append(_id)

                res = tag(_id=_id, txt=extr, fields=fields)

                relevances += json.loads(res)

            if isinstance(relevances, list) and len(relevances) > 0 and isinstance(relevances[0], dict):
                dct_relevances = {t['id']: t['nivel'] for t in relevances}
                '''
                if any(t['id'] == 'https://vanguardia.com.mx/coahuila/saltillo/pierde-la-vida-al-caer-instalando-un-anuncio-en-ramos-arizpe-FH15561994' for t in _docs):
                    _idx = [t for t in _docs if t['id'] == 'https://vanguardia.com.mx/coahuila/saltillo/pierde-la-vida-al-caer-instalando-un-anuncio-en-ramos-arizpe-FH15561994'][0]['_idx']
                    rr = dct_relevances[_idx]
                    print('IN', i_row, kw, ', relevance:', rr)
                '''
                for d in _docs:
                    if d['_idx'] not in dct_relevances:
                        print('------------- missing', d['_idx'], kw)
                        continue
                    d['relevance'] += (3 - dct_relevances[d['_idx']])

                    all_docs[d['id']] = d
                    docs_kws[d['id']] = kw
                    docs_section[d['id']] = fields['section']
                    docs_subsection[d['id']] = fields['subsection']


x = [{
        'relevance': t['relevance'],
        'title': t['title'],
        'id':  t['id'],
        'text': t['text'],
        'date_created': t['date_created'],
        'section': docs_section[t['id']],
        'subsection': docs_subsection[t['id']],
        'kws':docs_kws[t['id']]
     } for t in sorted(all_docs.values(), key=lambda t: -t['relevance'])]

feed_df = pn.DataFrame(x)
relevant = feed_df[feed_df.relevance >= 2]
relevant.drop_duplicates('title')  # TODO: OJO

#relevant.to_excel('~/Downloads/desarrollos_inmobiliario_relevant.xlsx', index=False)



sectionsmap = {
    'Regulación y permisos': 38,
    'Infraestructura y condiciones de habitabilidad': 41,
    'Sector inmobiliario y competencia': 40,
    'Financiamiento y servicios vinculados a la vivienda': 39
}

# STG
# conn = psycopg2.connect('postgresql+psycopg2://backend:backend_password_super_secret@192.168.1.50:5433/proto06')
# conn = psycopg2.connect('postgresql+psycopg2://backend:backend_password_super_secret@localhost:5433/proto06')

add_to_collection = False
if add_to_collection:
    import psycopg2

    conn = psycopg2.connect('postgresql://backend:backend_password_super_secret@192.168.1.50:5433/proto06')

    cur = conn.cursor()

    for i, r in relevant.iterrows():
        col_id = sectionsmap[r['section']]
        doctype = 'news' if len(r['id']) != 17 else 'topics'
        docid = r['id']
        insrt_stmt = f"insert into collections_referencing (collection_id, document_type, document_id) values ({col_id}, '{doctype}', '{docid}')"
        cur.execute(insrt_stmt)



########################################################################################################################
# structured ###########################################################################################################
########################################################################################################################

from datetime import datetime
date_now = datetime.now().strftime('%d/%m/%Y')


sys_1_str = 'Eres un modelo para extraer información estructurada de artículos de noticias, se te van a presentar instrucciones para resolver la tarea, algunos ejemplos, y posteriormente una noticia para que extraigas la información'

body_p = '''

La nota es la siguiente:

{body}
'''

def tag_structured(_id, task, txt):
    if _id not in tagged:
        _usr_1 = task
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
                 temperature=0.7,
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

from dateutil.parser import parse as dateparse

structured = searches[~pn.isnull(searches.detail)]

data = list()
for task_idx, task_r in structured.iterrows():

    for doc_idx, doc in relevant[relevant.subsection == concept].iterrows():
        date_art = dateparse(doc['date_created']).strftime('%d/%m/%Y')
        task = task_r['detail'].replace('{date_art}', date_art)
        concept = task_r['subsection']

        if doc_idx % 10 == 0:
            print(concept, doc_idx)
        key = (task_idx, doc['id'])
        r = tag_structured(key, task, doc['text'])
        r = json.loads(r)
        for d in r:
            d['url'] = doc['id']
            d['article_date'] = doc['date_created']
            data.append(d)

_data = copy.deepcopy(data)

for d in data:
    for field in ['pais', 'estado', 'ciudad_municipio', 'colonia', 'zona', 'calle', 'numero', 'lugar']:
        d[field] = d['ubicacion'][field]

    del d['ubicacion']

    if not pn.isnull(d['fecha_anuncio']) and d['fecha_anuncio'] != '':
        d['fecha_anuncio_uns'] = d['fecha_anuncio'].get('mencion')
        d['fecha_anuncio'] = d['fecha_anuncio'].get('fecha')

    if not pn.isnull(d['fecha_finalizacion']) and d['fecha_finalizacion'] != '':
        d['fecha_finalizacion_uns'] = d['fecha_finalizacion'].get('mencion')
        d['fecha_finalizacion'] = d['fecha_finalizacion'].get('fecha')

    if 'inversion' in d:
        d['precio'] = d['inversion']
    if not pn.isnull(d['precio']) and isinstance(d['precio'], dict):
        d['precio_uns'] = d['precio'].get('mencion')
        d['inversion'] = d['precio'].get('valor')
        d['moneda_inversion'] = d['precio'].get('moneda')


pn.DataFrame(data).to_excel('~/Downloads/desarrollos_20251112.xlsx', index=False)



x = [{'relevance':t['relevance'], 'title': t['title'], 'id':  t['id'], 'text': t['text'], 'date_created': t['date_created'], 'kws':docs_kws[t['id']]} for t in sorted(all_docs.values(), key=lambda t: -t['relevance'])]
feed_df = pn.DataFrame(x)

relevant = feed_df[feed_df.relevance >= 2]






########################################################################################################################
# events ###############################################################################################################
########################################################################################################################

from datetime import datetime
date_now = datetime.now().strftime('%d/%m/%Y')


sys_1_str = 'Eres un modelo para extraer información estructurada de artículos de noticias, se te van a presentar instrucciones para resolver la tarea, algunos ejemplos, y posteriormente una noticia para que extraigas la información'



body_p = '''

La nota es la siguiente:

{body}
'''

def tag_events(_id, txt):
    if _id not in tagged:
        _usr_1 = event_task.format(date_now=datetime.now().isoformat())
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
                 temperature=0.7,
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


structured = searches[~pn.isnull(searches.detail)]

data = list()
for task_idx, task_r in structured.iterrows():

    task = task_r['detail'].replace('{date_now}', date_now)
    concept = task_r['subsection']

    for doc_idx, doc in relevant[relevant.subsection == concept].iterrows():
        if doc_idx % 10 == 0:
            print(concept, doc_idx)
        key = (task_idx, doc['id'])
        r = tag_structured(key, task, doc['text'])
        r = json.loads(r)
        for d in r:
            d['url'] = doc['id']
            d['article_date'] = doc['date_created']
            data.append(d)


for d in data:
    for field in ['pais', 'ciudad', 'colonia', 'zona', 'calle', 'numero']:
        d[field] = d['ubicacion'][field]

    del d['ubicacion']

    if not pn.isnull(d['fecha_anuncio']) and d['fecha_anuncio'] != '':
        d['fecha_anuncio_uns'] = d['fecha_anuncio']['mencion']
        d['fecha_anuncio'] = d['fecha_anuncio']['fecha']

    if not pn.isnull(d['fecha_finalizacion']) and d['fecha_finalizacion'] != '':
        d['fecha_finalizacion_uns'] = d['fecha_finalizacion'].get('mencion')
        d['fecha_finalizacion'] = d['fecha_finalizacion'].get('fecha')


pn.DataFrame(data).to_excel('~/Downloads/desarrollos.xlsx', index=False)









_, _docs = es_query(event_id='20250227946208301')




_, _d = es_topics_query(_id='20250228567114578')
_, _d = es_query(topic_id='20250228567114578')








# reporte mensual
import psycopg2
import pandas as pn

from pymongo import MongoClient
from utils.es import get_docs

from collections import defaultdict, Counter

'''
MONGO_AUTHDB=admin
MONGO_COLLECTION_NEWS_SOURCES=CrawlersAll
MONGO_DB_NEWS_SOURCES=admin_app
#MONGO_HOST=34.68.153.112
MONGO_HOST=192.168.1.55
#MONGO_HOST=localhost
MONGO_PASSWORD=YellowScreen84
MONGO_PORT=27017
MONGO_USER=robot
'''
def get_mongo_connection():
    connection_string = "mongodb://robot:YellowScreen84@localhost:27017/admin"
    client = MongoClient(connection_string, connect=False)

    return client

mongoconn = get_mongo_connection()
cr = mongoconn.admin_app.CrawlersAll.find()

l = list()
for site in cr:
    st = site['stats']
    del site['stats']
    site.update(st)
    l.append(site)

sites = pn.DataFrame(l)

sites = sites[['minutes_to_sleep', 'domain', 'crawler_type', 'sitio', 'depth', 'source', 'reuters_trust_pct', 'tier', 'location_author_formatted_name']]

site_tier = dict(sites[['sitio', 'tier']].values)
site_loc = dict(sites[['sitio', 'location_author_formatted_name']].values)


kg_conn = psycopg2.connect('postgresql://postgres:sert3ch13@localhost:5435/kgprod')
kg_cur = kg_conn.cursor()

kg_cur.execute("select * from sentiment_entity where query_id=219 and article_date > '2025-04-07 00:00:00'")
sentiments = pn.DataFrame(kg_cur.fetchall(), columns=[t.name for t in kg_cur.description])

sentiments['tier'] = sentiments['source'].map(site_tier.get)
sentiments['source_location'] = sentiments['source'].map(site_loc.get)

sentiments[['id', 'entity', 'entity_name', 'entity_description', 'entity_type', 'url', 'query_id', 'source', 'sentiment_reason', 'tier', 'source_location']].drop_duplicates('source').to_excel('~/Downloads/LDGML_sources.xlsx', index=False)

handsources = pn.read_excel('~/Downloads/LDGML_sources_handtag.xlsx')
site_tier.update(dict(handsources[['source', 'tier']].values))
site_loc.update(dict(handsources[['source', 'source_location']].values))

sentiments['tier'] = sentiments['source'].map(site_tier.get)
sentiments['source_location'] = sentiments['source'].map(site_loc.get)


sentiments[(sentiments.source_location == 'Mexico') & (sentiments.sentiment=='positivo')][['sentiment_reason']].to_excel('~/Downloads/ldg_nacional_pos.xlsx', index=False)
sentiments[(sentiments.source_location == 'Mexico') & (sentiments.sentiment=='negativo')][['sentiment_reason']].to_excel('~/Downloads/ldg_nacional_neg.xlsx', index=False)

sentiments[(sentiments.source_location == 'Guanajuato, Mexico') & (sentiments.sentiment == 'positivo')][['sentiment_reason']].to_excel('~/Downloads/ldg_local_pos.xlsx', index=False)
sentiments[(sentiments.source_location == 'Guanajuato, Mexico') & (sentiments.sentiment == 'negativo')][['sentiment_reason']].to_excel('~/Downloads/ldg_local_neg.xlsx', index=False)

Counter(sentiments[(sentiments.source_location == 'Mexico')]['source']).most_common(20)

sent_counts = (sentiments.groupby(['source','sentiment'])['id'].
               agg('count').
               reset_index().
               pivot_table(index='source', columns='sentiment', values='id').
               fillna(0))

sent_counts['total'] = sent_counts.sum(axis=1)
sent_counts = sent_counts.sort_values('total', ascending=False)
plot_sent_counts = sent_counts.head(20)



########################################################################################################################
########################################################################################################################

import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt

import numpy as np

def plot_sentiment_bar_chart(data, site_names, sentiment_labels, colors,
                             xlabel='Number of Articles', ylabel='Sites',
                             title='Sentiment Distribution by Site'):
    """
    Plots a horizontal stacked bar chart of sentiment distribution by site.

    Parameters:
    - data: 2D list or NumPy array of shape (n_sites, n_sentiments)
    - site_names: List of site names (y-axis)
    - sentiment_labels: List of sentiment labels (e.g., ['Positive', 'Neutral', 'Negative'])
    - colors: List of colors for each sentiment
    - xlabel: Label for the x-axis
    - ylabel: Label for the y-axis
    - title: Title of the chart
    """

    data = np.array(data)
    n_sites = len(site_names)
    indices = np.arange(n_sites)

    # Start plotting
    fig, ax = plt.subplots(figsize=(10, 6))

    # Initialize bottom for stacking
    left = np.zeros(n_sites)

    for i in range(data.shape[1]):
        ax.barh(indices, data[:, i], left=left, label=sentiment_labels[i], color=colors[i])
        left += data[:, i]

    # Customizations
    ax.set_yticks(indices)
    ax.set_yticklabels(site_names)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend()

    plt.tight_layout()
    plt.show()

# Example usage
data = [
    [30, 20, 10],  # Site A
    [15, 25, 10],  # Site B
    [20, 15, 25],  # Site C
]

site_names = ['Site A', 'Site B', 'Site C']
sentiment_labels = ['Positive', 'Neutral', 'Negative']
colors = ['green', 'gray', 'red']

plot_sent_counts['mezclado'] = plot_sent_counts['mezclado'] + plot_sent_counts['irrelevante']

plot_sentiment_bar_chart(plot_sent_counts[['mezclado', 'negativo', 'neutral', 'positivo']].values,
                         plot_sent_counts.index.tolist(),
                         ['Mezclado', 'Negativo', 'Neutral', 'Positivo'],
                         ['gray', (166/255, 55/255, 63/255), (144/255, 120/255, 173/255), (.145, .11, .608)],
                         xlabel='Notas', ylabel='',
                         title='Sentimiento por sitio')

########################################################################################################################

req = {'source_tier': [],
 'doctype': 'news',
 'source': [],
 'period': 'w',
 'intervals': [],
 'keywords': [],
 'phrases': ['alejandra gutiérrez', 'informe'],
 'bounding_box': [],
 'location_type': 'text',
 'sort': 'date_created',
 'categories': {},
 'topic_id': [],
 'page_number': 0,
 'page_size': 2000,
 'categories_page_number': {},
 'cvegeo': []}

req = {'doctype': 'news',
 'source': [],
 'period': 'm',
 'intervals': [],
 'keywords': [],
 'phrases': ['san miguel de allende'],
 'geo_filter': True,
 'by_source_tier': False,
 'location_type': 'text',
 'sort': 'news_count',
 'categories': {},
 'topic_id': [],
 'phrases_on': False,
 'page_number': 0,
 'page_size': 5000,
 'categories_page_number': {},
 'save_search': False,
 'search_name': '',
 'wordcloud_size': 100,
 'alerts_active': False,
 'by_period': True,
 'ids': [],
 'cvegeo': [],
 'bounding_box': [],
 'search_id': 198,
 'alerts_conditions': {}}

_docs = get_docs(req)


sma_cats = Counter([k for t in _docs for k in t['custom_categories']['level_1']])



















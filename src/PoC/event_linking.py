import json
import copy
import random
import traceback

import numpy as np
import pandas as pn

from collections import defaultdict
from dateutil.parser import parse
from datetime import datetime, timedelta
from tools.lsh import LSHCache

LOCAL_REDIS_SETTINGS = {'host': '127.0.0.1', 'port': 6379, 'db': 0}

cache_2_ = LSHCache(n=200, b=100, r=2, n_shingles=[3, 2], redis_settings=LOCAL_REDIS_SETTINGS)
def lsh_jac_dist(x, y):
    x_set = set(cache_2_._get_tokens(x) + cache_2_._get_shingles(x))
    y_set = set(cache_2_._get_tokens(y) + cache_2_._get_shingles(y))

    return 1. - float(len(x_set.intersection(y_set))) / len(x_set.union(y_set))


def date_list(start: datetime, end: datetime):
    if pn.isnull(start) and pn.isnull(end):
        return None
    if pn.isnull(start):
        return [end.date()]
    if pn.isnull(end):
        return [start.date()]

    if start.date() > end.date():
        raise ValueError("start date must be earlier than or equal to end date")

    start_date = start.date()
    end_date = end.date()
    days = (end_date - start_date).days

    return [start_date + timedelta(days=i) for i in range(days + 1)]


def midpoint_date(d1: datetime | None, d2: datetime | None) -> datetime | None:
    """
    Return the middle (average) datetime between two datetimes.
    """
    if pn.isnull(d1) and pn.isnull(d2):
        return None
    if pn.isnull(d1):
        return d2
    if pn.isnull(d2):
        return d1

    if d1 > d2:
        d1, d2 = d2, d1

    delta = d2 - d1
    return d1 + delta / 2


ID_LENGTHS = [4, 6, 9, 13, 17, 22, 26]

fields = ['tipo_evento', 'subtipo_evento', 'estatus', 'afluencia', 'capacidad',
          'nombre', 'descripcion', 'etiquetas', 'contexto', 'url', 'article_date',
          'pais', 'estado', 'ciudad', 'colonia', 'zona', 'calle', 'numero',
          'lugar', 'fecha_evento_uns', 'fecha_inicio', 'fecha_fin', 'precio_uns',
          'precio_inf', 'precio_sup', 'moneda', 'afluencia_uns', 'capacidad_uns',
          'precision_level', 'geoid', 'level_2', 'level_3', 'formatted_name',
          'matched_lat', 'matched_lon']

def preprocess_event(event_data):

    event_data['fecha_inicio'] = parse(event_data['fecha_inicio']) if not pn.isnull(event_data['fecha_inicio']) else np.nan
    event_data['fecha_fin'] = parse(event_data['fecha_fin']) if not pn.isnull(event_data['fecha_fin']) else np.nan

    if pn.isnull(event_data['fecha_inicio']):
        event_data['fecha_inicio'] = parse(event_data['article_date']).replace(tzinfo=None).replace(hour=0, minute=0, second=0, microsecond=0)

    if pn.isnull(event_data['fecha_inicio']) and pn.isnull(event_data['fecha_fin']):
        return None

    reference_date = event_data['fecha_inicio'] if not pn.isnull(event_data['fecha_inicio']) else event_data['fecha_fin']

    if not (2010 < reference_date.year < 2030):
        return None

    if pn.isnull(event_data['geoid']):
        return None

    return event_data


def match_type(event1, event2):
    not_match_subtype = [('partido', 'torneo')]

    s1 = str(event1['subtipo_evento']).lower()
    s2 = str(event2['subtipo_evento']).lower()

    if any([(nm1 in s1 and nm2 in s2) or (nm1 in s2 and nm2 in s1) for nm1, nm2 in not_match_subtype]):
        return False

    if event1['tipo_evento'] == event2['tipo_evento']:
        return True

    return False

def event_euc_dist(event1, event2):
    return np.linalg.norm(np.array([event1['matched_lat'], event1['matched_lon']]) - np.array([event2['matched_lat'], event2['matched_lon']]))


def name_match_score(nombre1, nombre2):
    if any(pn.isnull(t) for t in [nombre1, nombre2]):
        name_match = int(str(nombre1) == str(nombre2))
    else:
        name_match = 1 - lsh_jac_dist(nombre1, nombre2)
    return name_match


def compare_events(event1, event2):
    event1_dates = date_list(event1['fecha_inicio'], event1['fecha_fin'])
    event2_dates = date_list(event2['fecha_inicio'], event2['fecha_fin'])

    date_match = False
    hour_match = False
    if set(event1_dates).intersection(set(event2_dates)):
        date_match = True
        hour_match = True
        if event1['fecha_inicio'].day == event2['fecha_inicio'].day and event1['fecha_inicio'].hour != event2['fecha_inicio'].hour:
            hour_match = False

    type_match = match_type(event1, event2)

    euc_dist = event_euc_dist(event1, event2)

    location_match = False
    for level in [7, 5, 3]:
        if event1['precision_level'] >= level and event2['precision_level'] >= level:
            location_match = event1['geoid'][:ID_LENGTHS[level-1]] == event2['geoid'][:ID_LENGTHS[level-1]]
            break

    location_match = location_match or euc_dist < .0035

    name_match = name_match_score(event1['nombre'], event2['nombre'])

    return location_match and type_match and date_match, [location_match, type_match, date_match, hour_match, name_match]


def merge_event(base_event, new_event):
    merged = copy.deepcopy(base_event)

    if new_event['precision_level'] > merged['precision_level']:
        merge_location_fields = ['precision_level', 'geoid', 'level_2', 'level_3', 'formatted_name', 'matched_lon', 'matched_lat']
        for field in merge_location_fields:

            merged[field] = new_event[field]

    fillna_fields = ['pais', 'estado', 'colonia', 'zona', 'calle', 'numero', 'lugar', 'afluencia', 'capacidad',
                     'nombre', 'fecha_fin', 'precio_inf', 'precio_sup', 'moneda']

    for field in fillna_fields:
        if pn.isnull(merged[field]):
            merged[field] = new_event[field]

    fill_latest_fields = ['estatus', 'precio_inf', 'precio_sup', 'contexto', 'afluencia']

    if base_event['last_article_date'] < new_event['article_date']:
        base_event['last_article_date'] = new_event['article_date']
        for field in fill_latest_fields:
            if pn.isnull(new_event[field]):
                merged[field] = new_event[field]

    # SUPPOSED TO HAVE BETTER DATE PRECISION
    # TODO: USE ACTUAL PRECISION
    if new_event['fecha_inicio'] > merged['fecha_inicio']:
        merged['fecha_inicio'] = new_event['fecha_inicio']

    if not pn.isnull(new_event['fecha_fin']) and new_event['fecha_fin'] < merged['fecha_fin']:
        merged['fecha_fin'] = new_event['fecha_fin']

    new_source_obj = sources_obj(new_event)
    new_source_obj.update({'name_match_score': name_match_score(base_event['nombre'], new_event['nombre'])})

    merged['sources'].append(new_source_obj)

    return merged

def sources_obj(event_data):
    sources_fields = ['url', 'article_date', 'fecha_evento_uns', 'precio_uns', 'afluencia_uns',
                      'capacidad_uns', 'subtipo_evento', 'contexto', 'estatus', 'fecha_precision', 'nombre']

    return {field: event_data.get(field) for field in sources_fields }


def create_event(event_data):
    _event = copy.deepcopy(event_data)
    _event['id'] = _event['fecha_inicio'].strftime('%Y%m%d') + _event['geoid'][:ID_LENGTHS[2]] + '_' + str(random.randint(100000, 1000000))

    start_date = _event['fecha_inicio']
    end_date = _event['fecha_fin'] if not pn.isnull(_event['fecha_fin']) else _event['fecha_inicio']

    _event['sources'] = [sources_obj(_event)]
    _event['last_article_date'] = _event['article_date']

    for key in _event['sources'][0].keys():
        if key not in {'subtipo_evento', 'contexto', 'estatus', 'nombre'} and key in _event:
            del _event[key]

    for c_date in date_list(start_date, end_date):
        date_events[c_date.strftime('%Y%m%d')].add(_event['id'])

    for level in [7, 5, 3]:
        if _event['precision_level'] >= level:
            level_id = _event['geoid'][:ID_LENGTHS[level-1]]
            place_events[f'level_{level}'][level_id].add(_event['id'])

    events[_event['id']] = _event

    return _event


events_df = pn.read_excel('/Users/oscarcuellar/Downloads/eventos20250826_geocoded_devpipe.xlsx')
events_df = events_df.sort_values('article_date')
events_df.index = range(len(events_df))

events = dict()
seen_urls = set()

date_events = defaultdict(set)
place_events = {'level_3': defaultdict(set), 'level_5': defaultdict(set), 'level_7': defaultdict(set)}

for i, row in events_df.iterrows():

    try:
        event = preprocess_event(row[fields].to_dict())
        #assert i != 5149
        if event is None or event['precision_level'] < 3:
            # TODO: PRECISION LEVEL < 3 DOESN'T CREATE EVENT, BUT CAN MATCH
            continue

        #seen_urls.add(event['url'])

        date_keys = [t.strftime('%Y%m%d') for t in date_list(event['fecha_inicio'], event['fecha_fin'])]

        date_candidates = set()
        for date_key in date_keys:
            date_candidates = date_events[date_key].union(date_candidates)

        location_candidates = {'level_3': dict(), 'level_5': dict(), 'level_7': dict()}


        merged = None
        for level in [7, 5, 3]:
            if merged is None and event['precision_level'] >= level:

                level_id = event['geoid'][:ID_LENGTHS[level-1]]
                location_candidates[f'level_{level}'] = place_events[f'level_{level}'][level_id].intersection(date_candidates)

                if len(location_candidates[f'level_{level}']) > 0:
                    # COMPARE & MERGE EVENT

                    #assert i > 48 and str(event['geoid']).startswith('_4842111400010772')
                    for cnd_event_id in location_candidates[f'level_{level}']:
                        is_match, concepts = compare_events(event, events[cnd_event_id])

                        if is_match:
                            merged = merge_event(events[cnd_event_id], event)
                            #assert i <= 48 or not str(event['geoid']).startswith('_4842111400010772')
                            events[cnd_event_id] = merged
                            print(i, round(concepts[-1], 2), events[cnd_event_id]['id'], 'merged', events[cnd_event_id]['nombre'], ' -- ', event['nombre'])

                            break
                            #assert False
        if merged is None:
            created_event = create_event(event)

    except AssertionError as e:
        raise
    except Exception:
        traceback.print_exc()
        print('-----'*10, ' FAILED', i)

list_events = list()
for _e in events.values():
    e = copy.deepcopy(_e)
    e['sources_count'] = len(e['sources'])
    e['sources'] = json.dumps(e['sources'], indent=4)
    list_events.append(e)

pn.DataFrame(list_events).to_excel('~/Downloads/events_test.xlsx', index=False)

'''

fecha de apertura

Enforce type 1 catalogue
enforce type 2 catalogue?
date precision in days: report date range start
date precision, how exact

many places, name only
add estado
relevancia del evento en la noticia


'''




















